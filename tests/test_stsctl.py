import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import stsctl
import campaign_selector


SELECTION = {
    "selection_id": "selection-stsctl-binding",
    "decision_hash": "decision-stsctl-binding",
    "controller_hash": "controller-stsctl-binding",
    "policy_version": "fast-policy-v5",
    "character": "IRONCLAD",
    "ascension_level": 0,
    "run_type": "standard",
    "goal_mode": "HEART",
    "algorithm": "beta-thompson-v1",
}
ATTEMPT = {
    "attempt_id": "attempt-stsctl-binding",
    "run_id": "IRONCLAD:0:123",
    "seed": 123,
    "character": "IRONCLAD",
    "ascension_level": 0,
    "run_type": "standard",
    "decision_hash": "decision-stsctl-binding",
    "controller_hash": "controller-stsctl-binding",
    "policy_version": "fast-policy-v5",
    "selection_id": "selection-stsctl-binding",
    "selection_digest": campaign_selector.selection_digest(SELECTION),
}


def active_state(*, projected=True, seed=123):
    state = {
        "protocol_version": 2,
        "state_seq": 17,
        "decision_id": "decision:active",
        "phase": "COMBAT_TURN_1",
        "in_game": True,
        "game_state": {
            "class": "IRONCLAD",
            "ascension_level": 0,
            "seed": seed,
            "is_standard_run": True,
        },
    }
    if projected:
        state.update(ATTEMPT)
    return state


def run_context(**changes):
    context = {
        "schema_version": 2,
        "goal_mode": "HEART",
        "terminal_state_seq": None,
        **ATTEMPT,
        "selection": dict(SELECTION),
    }
    context.update(changes)
    return context


class BoundPayloadAttemptTests(unittest.TestCase):
    def test_active_projection_supplies_every_attempt_field(self):
        payload = stsctl.bound_payload(
            active_state(), "end", target_id="action:end"
        )

        self.assertEqual(
            ATTEMPT,
            {
                field: payload[field]
                for field in stsctl.ATTEMPT_BINDING_FIELDS
            },
        )

    def test_first_active_frame_uses_exact_validated_run_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-context.json"
            path.write_text(json.dumps(run_context()), encoding="utf-8")
            with mock.patch.object(stsctl, "RUN_CONTEXT_PATH", path):
                payload = stsctl.bound_payload(
                    active_state(projected=False),
                    "end",
                    target_id="action:end",
                )

        self.assertEqual(
            ATTEMPT,
            {
                field: payload[field]
                for field in stsctl.ATTEMPT_BINDING_FIELDS
            },
        )

    def test_first_active_frame_rejects_context_for_wrong_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-context.json"
            path.write_text(json.dumps(run_context()), encoding="utf-8")
            with mock.patch.object(stsctl, "RUN_CONTEXT_PATH", path):
                with self.assertRaisesRegex(
                    ValueError, "does not match authoritative state"
                ):
                    stsctl.bound_payload(
                        active_state(projected=False, seed=124), "end"
                    )

    def test_first_active_frame_rejects_stale_selection_binding(self):
        context = run_context()
        context["selection"] = {
            **context["selection"],
            "selection_id": "selection-from-another-attempt",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-context.json"
            path.write_text(json.dumps(context), encoding="utf-8")
            with mock.patch.object(stsctl, "RUN_CONTEXT_PATH", path):
                with self.assertRaisesRegex(
                    ValueError, "run context selection mismatch"
                ):
                    stsctl.bound_payload(
                        active_state(projected=False), "end"
                    )

    def test_first_active_frame_rejects_terminal_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-context.json"
            path.write_text(
                json.dumps(run_context(terminal_state_seq=16)),
                encoding="utf-8",
            )
            with mock.patch.object(stsctl, "RUN_CONTEXT_PATH", path):
                with self.assertRaisesRegex(ValueError, "terminal run context"):
                    stsctl.bound_payload(
                        active_state(projected=False), "end"
                    )

    def test_partial_active_projection_is_rejected(self):
        state = active_state()
        del state["selection_id"]
        with self.assertRaisesRegex(ValueError, "partial attempt binding"):
            stsctl.bound_payload(state, "end")

    def test_caller_cannot_self_report_attempt_binding(self):
        with self.assertRaisesRegex(
            ValueError, "must come from authoritative state/context"
        ):
            stsctl.bound_payload(
                active_state(), "end", attempt_id=ATTEMPT["attempt_id"]
            )

    def test_read_only_state_and_main_menu_start_are_pre_run_exceptions(self):
        active = active_state(projected=False)
        state_payload = stsctl.bound_payload(active, "state")
        self.assertNotIn("attempt_id", state_payload)

        menu = {
            "state_seq": 18,
            "decision_id": "decision:menu",
            "phase": "MAIN_MENU",
            "in_game": False,
        }
        start_payload = stsctl.bound_payload(menu, "start")
        self.assertNotIn("attempt_id", start_payload)


if __name__ == "__main__":
    unittest.main()
