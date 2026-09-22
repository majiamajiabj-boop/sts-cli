import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_context
import pre_run_binding


def state_for(
    character="THE_SILENT",
    *,
    silent=True,
    defect=True,
    ironclad=True,
    keys=True,
    seed=123,
):
    return {
        "state_seq": 10,
        "key_system_unlocked": keys,
        "silent_third_act_win": silent,
        "defect_third_act_win": defect,
        "ironclad_third_act_win": ironclad,
        "game_state": {
            "class": character,
            "ascension_level": 0,
            "seed": seed,
            "is_standard_run": True,
        },
    }


def install_start_acceptance(root, selection):
    menu = {
        "protocol_version": 2,
        "in_game": False,
        "ready_for_command": True,
        "state_seq": 1,
        "phase": "MAIN_MENU",
        "decision_id": "menu-decision",
    }
    payload = {
        "id": "request-" + selection["selection_id"],
        "expected_seq": 1,
        "decision_id": "menu-decision",
        "phase": "MAIN_MENU",
        "selection_id": selection["selection_id"],
        "selection_digest": pre_run_binding.selection_digest(selection),
        "decision_hash": selection["decision_hash"],
        "controller_hash": selection["controller_hash"],
        "character": selection["character"],
        "ascension_level": 0,
        "run_type": "standard",
        "target_id": f"run:{selection['character']}:a0:standard",
    }
    record = pre_run_binding.build_start_acceptance_record(
        payload, menu, selection, created_at=10.0
    )
    path = pre_run_binding.start_acceptance_path(
        root, selection["selection_id"]
    )
    pre_run_binding._write_json_once_idempotent(path, record)
    return record


class RunContextTests(unittest.TestCase):
    @staticmethod
    def selection(
        character="THE_SILENT",
        decision_hash="decision-a",
        controller_hash="controller-a",
    ):
        return {
            "selection_id": "selection-1",
            "decision_hash": decision_hash,
            "controller_hash": controller_hash,
            "character": character,
            "algorithm": "beta-thompson-v1",
            "goal_mode": "HEART",
            "policy_version": "fast-policy-v5",
            "ascension_level": 0,
            "run_type": "standard",
        }

    def test_all_prerequisites_select_heart_mode(self):
        self.assertEqual(run_context.determine_goal_mode(state_for()), "HEART")

    def test_unlock_mode_enforces_goal_order(self):
        state = state_for(silent=False, defect=False, ironclad=False, keys=False)
        self.assertEqual(run_context.determine_goal_mode(state), "UNLOCK")
        self.assertEqual(run_context.required_unlock_character(state), "THE_SILENT")
        run_context.validate_active_run(state, "UNLOCK")

    def test_unlock_mode_rejects_wrong_active_character(self):
        state = state_for(
            character="DEFECT", silent=False, defect=False, ironclad=False, keys=False,
        )
        with self.assertRaises(run_context.RunContextError):
            run_context.validate_active_run(state, "UNLOCK")

    def test_all_prerequisites_without_final_act_stays_unlock_mode(self):
        state = state_for(keys=False)
        self.assertEqual(run_context.determine_goal_mode(state), "UNLOCK")
        self.assertEqual(run_context.required_unlock_character(state), "THE_SILENT")
        run_context.validate_active_run(state, "UNLOCK")

    def test_final_act_cannot_precede_prerequisites(self):
        with self.assertRaises(run_context.RunContextError):
            run_context.determine_goal_mode(state_for(silent=False, keys=True))

    def test_progression_flags_require_consistent_booleans(self):
        malformed = state_for()
        malformed["silent_third_act_win"] = "false"
        with self.assertRaises(run_context.RunContextError):
            run_context.determine_goal_mode(malformed)

        conflicting = state_for()
        conflicting["game_state"]["key_system_unlocked"] = False
        with self.assertRaises(run_context.RunContextError):
            run_context.determine_goal_mode(conflicting)

    def test_context_is_reused_only_for_same_run_and_hashes(self):
        state = state_for()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-context.json"
            first = run_context.load_or_create_context(
                state, "decision-a", "controller-a", path,
                selection=self.selection(),
            )
            second = run_context.load_or_create_context(state, "decision-a", "controller-a", path)
            with self.assertRaises(run_context.RunContextError):
                run_context.load_or_create_context(
                    state, "decision-b", "controller-a", path,
                    selection=self.selection(decision_hash="decision-b"),
                )
        self.assertEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(first["goal_mode"], "HEART")

    def test_matching_pending_selection_is_consumed_and_audited(self):
        state = state_for(character="IRONCLAD")
        selection = self.selection(character="IRONCLAD")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "next-run-selection.json"
            path.write_text(__import__("json").dumps(selection), encoding="utf-8")
            consumed = run_context.consume_pending_selection(
                path, state, "decision-a", "controller-a"
            )
            self.assertFalse(path.exists())
        self.assertEqual(selection, consumed)

    def test_mismatched_pending_selection_fails_closed(self):
        state = state_for(character="DEFECT")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "next-run-selection.json"
            path.write_text(
                __import__("json").dumps(self.selection(character="IRONCLAD")),
                encoding="utf-8",
            )
            with self.assertRaises(run_context.RunContextError):
                run_context.consume_pending_selection(
                    path, state, "decision-a", "controller-a"
                )
            self.assertTrue(path.exists())

    def test_new_heart_context_requires_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(run_context.RunContextError):
                run_context.load_or_create_context(
                    state_for(), "decision-a", "controller-a",
                    Path(directory) / "run-context.json",
                )

    def test_new_heart_context_consumes_selection_after_reuse_check(self):
        state = state_for()
        with tempfile.TemporaryDirectory() as directory:
            context_path = Path(directory) / "run-context.json"
            selection_path = Path(directory) / "next-run-selection.json"
            selection_path.write_text(
                __import__("json").dumps(self.selection()), encoding="utf-8"
            )
            install_start_acceptance(directory, self.selection())
            first = run_context.load_or_create_context(
                state,
                "decision-a",
                "controller-a",
                context_path,
                selection_path=selection_path,
            )
            self.assertFalse(selection_path.exists())
            second = run_context.load_or_create_context(
                state,
                "decision-a",
                "controller-a",
                context_path,
                selection_path=selection_path,
            )
        self.assertEqual(first["attempt_id"], second["attempt_id"])

    def test_context_write_failure_preserves_pending_selection(self):
        state = state_for()
        with tempfile.TemporaryDirectory() as directory:
            context_path = Path(directory) / "run-context.json"
            selection_path = Path(directory) / "next-run-selection.json"
            selection_path.write_text(
                __import__("json").dumps(self.selection()), encoding="utf-8"
            )
            install_start_acceptance(directory, self.selection())
            with patch.object(
                run_context, "_atomic_write", side_effect=OSError("disk failure")
            ):
                with self.assertRaises(OSError):
                    run_context.load_or_create_context(
                        state,
                        "decision-a",
                        "controller-a",
                        context_path,
                        selection_path=selection_path,
                    )

            self.assertTrue(selection_path.exists())
            self.assertFalse(context_path.exists())

    def test_context_reuse_finishes_interrupted_selection_consumption(self):
        state = state_for()
        selection = self.selection()
        with tempfile.TemporaryDirectory() as directory:
            context_path = Path(directory) / "run-context.json"
            selection_path = Path(directory) / "next-run-selection.json"
            context = run_context.create_context(
                state, "decision-a", "controller-a", selection
            )
            context_path.write_text(
                __import__("json").dumps(context), encoding="utf-8"
            )
            selection_path.write_text(
                __import__("json").dumps(selection), encoding="utf-8"
            )
            install_start_acceptance(directory, selection)

            reused = run_context.load_or_create_context(
                state,
                "decision-a",
                "controller-a",
                context_path,
                selection_path=selection_path,
            )

            self.assertEqual(context["attempt_id"], reused["attempt_id"])
            self.assertFalse(selection_path.exists())

    def test_context_reuse_rejects_a_different_pending_selection(self):
        state = state_for()
        selection = self.selection()
        with tempfile.TemporaryDirectory() as directory:
            context_path = Path(directory) / "run-context.json"
            selection_path = Path(directory) / "next-run-selection.json"
            context = run_context.create_context(
                state, "decision-a", "controller-a", selection
            )
            context_path.write_text(
                __import__("json").dumps(context), encoding="utf-8"
            )
            selection_path.write_text(
                __import__("json").dumps(
                    {**selection, "selection_id": "selection-new"}
                ),
                encoding="utf-8",
            )
            install_start_acceptance(directory, selection)

            with self.assertRaises(run_context.RunContextError):
                run_context.load_or_create_context(
                    state,
                    "decision-a",
                    "controller-a",
                    context_path,
                    selection_path=selection_path,
                )

            self.assertTrue(selection_path.exists())

    def test_terminal_same_seed_consumes_new_selection_into_new_attempt(self):
        state = state_for()
        old_selection = self.selection()
        new_selection = {
            **old_selection,
            "selection_id": "selection-2",
            "reason": "second-attempt-same-seed",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context_path = root / "run-context.json"
            selection_path = root / "next-run-selection.json"
            old_context = {
                **run_context.create_context(
                    state, "decision-a", "controller-a", old_selection
                ),
                "terminal_state_seq": 99,
            }
            context_path.write_text(
                __import__("json").dumps(old_context), encoding="utf-8"
            )
            selection_path.write_text(
                __import__("json").dumps(new_selection), encoding="utf-8"
            )
            install_start_acceptance(root, new_selection)

            replacement = run_context.load_or_create_context(
                state,
                "decision-a",
                "controller-a",
                context_path,
                selection_path=selection_path,
            )

            self.assertNotEqual(
                old_context["attempt_id"], replacement["attempt_id"]
            )
            self.assertEqual("selection-2", replacement["selection_id"])
            self.assertIsNone(replacement["terminal_state_seq"])
            self.assertFalse(selection_path.exists())
            self.assertEqual(
                replacement,
                __import__("json").loads(
                    context_path.read_text(encoding="utf-8")
                ),
            )

    def test_terminal_same_seed_without_new_selection_is_blocked(self):
        state = state_for()
        with tempfile.TemporaryDirectory() as directory:
            context_path = Path(directory) / "run-context.json"
            terminal_context = {
                **run_context.create_context(
                    state,
                    "decision-a",
                    "controller-a",
                    self.selection(),
                ),
                "terminal_state_seq": 99,
            }
            context_path.write_text(
                __import__("json").dumps(terminal_context),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                run_context.RunContextError, "requires a new pending"
            ):
                run_context.load_or_create_context(
                    state,
                    "decision-a",
                    "controller-a",
                    context_path,
                    selection_path=(
                        Path(directory) / "next-run-selection.json"
                    ),
                )

    def test_terminal_same_seed_context_write_failure_preserves_old_and_pending(self):
        state = state_for()
        old_selection = self.selection()
        new_selection = {**old_selection, "selection_id": "selection-2"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context_path = root / "run-context.json"
            selection_path = root / "next-run-selection.json"
            old_context = {
                **run_context.create_context(
                    state, "decision-a", "controller-a", old_selection
                ),
                "terminal_state_seq": 99,
            }
            context_path.write_text(
                __import__("json").dumps(old_context), encoding="utf-8"
            )
            selection_path.write_text(
                __import__("json").dumps(new_selection), encoding="utf-8"
            )
            install_start_acceptance(root, new_selection)

            with patch.object(
                run_context, "_atomic_write", side_effect=OSError("disk")
            ):
                with self.assertRaises(OSError):
                    run_context.load_or_create_context(
                        state,
                        "decision-a",
                        "controller-a",
                        context_path,
                        selection_path=selection_path,
                    )

            self.assertTrue(selection_path.exists())
            self.assertEqual(
                old_context,
                __import__("json").loads(
                    context_path.read_text(encoding="utf-8")
                ),
            )

    def test_run_type_and_ascension_must_be_authoritative(self):
        missing_standard = state_for()
        del missing_standard["game_state"]["is_standard_run"]
        with self.assertRaises(run_context.RunContextError):
            run_context.validate_active_run(missing_standard, "HEART")
        missing_ascension = state_for()
        del missing_ascension["game_state"]["ascension_level"]
        with self.assertRaises(run_context.RunContextError):
            run_context.validate_active_run(missing_ascension, "HEART")
        for invalid_ascension in (False, "0", 0.0):
            invalid = state_for()
            invalid["game_state"]["ascension_level"] = invalid_ascension
            with self.assertRaises(run_context.RunContextError):
                run_context.validate_active_run(invalid, "HEART")

    def test_attempt_and_selection_ids_must_be_nonempty_strings(self):
        state = state_for()
        for invalid_id in (7, [], {}, "", "   "):
            selection = {
                **self.selection(),
                "selection_id": invalid_id,
            }
            with self.assertRaises(run_context.RunContextError):
                run_context.create_context(
                    state, "decision-a", "controller-a", selection
                )
        with self.assertRaises(run_context.RunContextError):
            run_context.create_context(
                state,
                "decision-a",
                "controller-a",
                {**self.selection(), "ascension_level": False},
            )

        context = run_context.create_context(
            state,
            "decision-a",
            "controller-a",
            self.selection(),
        )
        for invalid_id in (7, [], {}, "", "\t"):
            invalid_context = {**context, "attempt_id": invalid_id}
            with self.assertRaises(run_context.RunContextError):
                run_context.validate_context_binding(
                    state,
                    invalid_context,
                    "decision-a",
                    "controller-a",
                )
        with self.assertRaises(run_context.RunContextError):
            run_context.validate_context_binding(
                state,
                {**context, "ascension_level": False},
                "decision-a",
                "controller-a",
            )

    def test_seed_must_be_authoritative(self):
        missing_seed = state_for()
        del missing_seed["game_state"]["seed"]
        with self.assertRaises(run_context.RunContextError):
            run_context.validate_active_run(missing_seed, "HEART")
        null_seed = state_for()
        null_seed["game_state"]["seed"] = None
        with self.assertRaises(run_context.RunContextError):
            run_context.authoritative_run_id(null_seed["game_state"])
        for invalid_seed in ("", "   ", True, False, 1.5, [], {}):
            whitespace_seed = state_for(seed=invalid_seed)
            with self.assertRaises(run_context.RunContextError):
                run_context.authoritative_run_id(whitespace_seed["game_state"])
        zero_seed = state_for(seed=0)
        self.assertEqual(
            "THE_SILENT:0:0",
            run_context.authoritative_run_id(zero_seed["game_state"]),
        )

    def test_context_seed_type_must_match_authoritative_seed(self):
        state = state_for(seed=1)
        context = run_context.create_context(
            state,
            "decision-a",
            "controller-a",
            selection=self.selection(),
        )
        for invalid in (True, 1.0):
            malformed = {**context, "seed": invalid}
            with self.assertRaises(run_context.RunContextError):
                run_context.validate_context_binding(
                    state, malformed, "decision-a", "controller-a"
                )

    def test_terminal_context_load_requires_exact_run_and_hashes(self):
        state = state_for(character="DEFECT", seed=321)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-context.json"
            context = run_context.load_or_create_context(
                state,
                "decision-a",
                "controller-a",
                path,
                selection=self.selection(character="DEFECT"),
            )
            matching = run_context.load_matching_context(
                state, "decision-a", "controller-a", path
            )
            changed_seed = state_for(character="DEFECT", seed=322)
            with self.assertRaises(run_context.RunContextError):
                run_context.load_matching_context(
                    changed_seed, "decision-a", "controller-a", path
                )
            with self.assertRaises(run_context.RunContextError):
                run_context.load_matching_context(
                    state, "decision-b", "controller-a", path
                )
        self.assertEqual(context["attempt_id"], matching["attempt_id"])

    def test_selection_must_bind_the_exact_controller_hash(self):
        with self.assertRaises(run_context.RunContextError):
            run_context.create_context(
                state_for(),
                "decision-a",
                "controller-a",
                self.selection(controller_hash="controller-other"),
            )

    def test_terminal_sequence_is_atomically_bound_once(self):
        state = state_for(character="IRONCLAD")
        context = run_context.create_context(
            state,
            "decision-a",
            "controller-a",
            self.selection(character="IRONCLAD"),
        )
        terminal = {
            **state,
            "state_seq": 44,
            "game_state": {
                **state["game_state"],
                "screen_type": "GAME_OVER",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-context.json"
            bound = run_context.bind_terminal_state(
                path,
                terminal,
                context,
                "decision-a",
                "controller-a",
            )
            persisted = __import__("json").loads(
                path.read_text(encoding="utf-8")
            )
            rebound = run_context.bind_terminal_state(
                path,
                terminal,
                bound,
                "decision-a",
                "controller-a",
            )

        self.assertEqual(44, persisted["terminal_state_seq"])
        self.assertEqual(bound, rebound)


if __name__ == "__main__":
    unittest.main()
