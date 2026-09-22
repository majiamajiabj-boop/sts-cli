"""Authoritative per-attempt context for autonomous Slay the Spire runs.

The one-run controller used to infer its objective from whichever rule happened
to inspect ``key_system_unlocked``.  That allowed unlock-only preferences to
leak into Heart attempts and made result cohorts impossible to compare.  This
module freezes the objective and strategy identity at controller start.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import campaign_selector
import pre_run_binding


SCHEMA_VERSION = 2
POLICY_VERSION = "fast-policy-v5"
GOAL_UNLOCK = "UNLOCK"
GOAL_HEART = "HEART"
SUPPORTED_CHARACTERS = ("IRONCLAD", "THE_SILENT", "DEFECT")
HEART_SELECTION_ALGORITHM = "beta-thompson-v1"
UNLOCK_CHARACTER_ORDER = ("THE_SILENT", "DEFECT", "IRONCLAD")
PROGRESSION_FLAGS = {
    "THE_SILENT": "silent_third_act_win",
    "DEFECT": "defect_third_act_win",
    "IRONCLAD": "ironclad_third_act_win",
}


class RunContextError(RuntimeError):
    """Raised when authoritative progression and the active run disagree."""


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _flag(state, name):
    game = state.get("game_state") or {}
    sources = []
    if name in state:
        sources.append(("state", state[name]))
    if name in game:
        sources.append(("game_state", game[name]))
    if not sources:
        raise RunContextError(f"authoritative state is missing {name}")
    if any(type(value) is not bool for _, value in sources):
        raise RunContextError(f"authoritative {name} must be a boolean")
    if len(sources) == 2 and sources[0][1] != sources[1][1]:
        raise RunContextError(
            f"authoritative {name} conflicts between state and game_state"
        )
    return sources[0][1]


def progression_snapshot(state):
    return {
        "key_system_unlocked": _flag(state, "key_system_unlocked"),
        **{
            flag_name: _flag(state, flag_name)
            for flag_name in PROGRESSION_FLAGS.values()
        },
    }


def determine_goal_mode(state):
    progression = progression_snapshot(state)
    wins = [progression[name] for name in PROGRESSION_FLAGS.values()]
    key_unlocked = progression["key_system_unlocked"]
    if not key_unlocked:
        # The game's final-act setting is authoritative.  A profile can retain
        # all three per-character win flags while that setting is still false;
        # do not fabricate a key unlock from the derived prerequisite record.
        # A fresh standard victory is then required for the game itself to
        # persist the final-act transition.
        return GOAL_UNLOCK
    if all(wins):
        return GOAL_HEART
    raise RunContextError(
        "key_system_unlocked cannot be true before every supported character "
        "prerequisite is complete"
    )


def required_unlock_character(state):
    progression = progression_snapshot(state)
    for character in UNLOCK_CHARACTER_ORDER:
        if not progression[PROGRESSION_FLAGS[character]]:
            return character
    # When the per-character records are complete but the game's final-act
    # setting remains false, replay the first deterministic supported class.
    # This lets the authoritative game setting transition through a normal
    # victory instead of treating the contradictory profile as a Heart run.
    return UNLOCK_CHARACTER_ORDER[0]


def authoritative_run_id(game):
    seed = game.get("seed")
    if "seed" not in game or not _valid_seed(seed):
        raise RunContextError("authoritative state has a missing or invalid seed")
    return ":".join(
        str(value)
        for value in (
            game.get("class"),
            game.get("ascension_level"),
            seed,
        )
    )


def _valid_seed(seed):
    """Accept only stable JSON scalar forms emitted by the game protocol."""

    return type(seed) is int or (
        isinstance(seed, str) and bool(seed.strip())
    )


def _validated_game_identity(state):
    game = state.get("game_state") or {}
    character = game.get("class")
    if character not in SUPPORTED_CHARACTERS:
        raise RunContextError(f"unsupported or missing active character: {character}")
    if "ascension_level" not in game or game.get("ascension_level") is None:
        raise RunContextError("authoritative state is missing ascension_level")
    if type(game["ascension_level"]) is not int:
        raise RunContextError("authoritative ascension_level is invalid")
    ascension_level = game["ascension_level"]
    if ascension_level != 0:
        raise RunContextError("autonomous goal requires ascension level 0")
    if game.get("is_standard_run") is not True:
        raise RunContextError("autonomous goal requires a standard run")
    authoritative_run_id(game)
    return game


def validate_active_run(state, goal_mode):
    game = _validated_game_identity(state)
    if goal_mode not in {GOAL_UNLOCK, GOAL_HEART}:
        raise RunContextError(f"unsupported goal mode: {goal_mode}")
    character = game.get("class")
    if goal_mode == GOAL_UNLOCK:
        required = required_unlock_character(state)
        if character != required:
            raise RunContextError(
                f"unlock run must use {required}, but active run is {character}"
            )


def validate_heart_selection(selection, game, decision_hash, controller_hash):
    if not isinstance(selection, dict):
        raise RunContextError("a Heart attempt requires an audited campaign selection")
    if (
        selection.get("decision_hash") != decision_hash
        or selection.get("controller_hash") != controller_hash
        or selection.get("character") != game.get("class")
        or selection.get("algorithm") != HEART_SELECTION_ALGORITHM
        or selection.get("goal_mode") != GOAL_HEART
        or selection.get("policy_version") != "fast-policy-v5"
        or type(selection.get("ascension_level")) is not int
        or selection.get("ascension_level") != 0
        or selection.get("run_type") != "standard"
        or not _nonempty_string(selection.get("selection_id"))
    ):
        raise RunContextError("campaign selection does not match the active Heart run")


def _selection_digest(selection):
    try:
        return campaign_selector.selection_digest(selection)
    except ValueError as exc:
        raise RunContextError(str(exc)) from exc


def validate_context_binding(state, context, decision_hash, controller_hash):
    """Verify that a frozen context belongs to the exact authoritative run."""

    game = _validated_game_identity(state)
    if not isinstance(context, dict):
        raise RunContextError("run context is missing or invalid")
    if context.get("schema_version") != SCHEMA_VERSION:
        raise RunContextError("run context schema does not match")
    if context.get("policy_version") != POLICY_VERSION:
        raise RunContextError("run context policy version does not match")
    if not _nonempty_string(context.get("attempt_id")):
        raise RunContextError("run context is missing attempt_id")
    if context.get("goal_mode") not in {GOAL_UNLOCK, GOAL_HEART}:
        raise RunContextError("run context has an unsupported goal mode")
    if context.get("run_id") != authoritative_run_id(game):
        raise RunContextError("run context belongs to a different seed")
    context_seed = context.get("seed")
    game_seed = game.get("seed")
    if (
        not _valid_seed(context_seed)
        or type(context_seed) is not type(game_seed)
        or context_seed != game_seed
    ):
        raise RunContextError("run context seed does not match the active run")
    if context.get("character") != game.get("class"):
        raise RunContextError("run context character does not match the active run")
    if (
        type(context.get("ascension_level")) is not int
        or context.get("ascension_level") != 0
        or context.get("run_type") != "standard"
    ):
        raise RunContextError("run context is not A0 standard")
    if context.get("decision_hash") != decision_hash:
        raise RunContextError("run context decision hash does not match")
    if context.get("controller_hash") != controller_hash:
        raise RunContextError("run context controller hash does not match")
    if not _nonempty_string(context.get("selection_id")):
        raise RunContextError("run context is missing selection_id")
    selection = context.get("selection")
    if (
        not isinstance(selection, dict)
        or selection.get("selection_id") != context.get("selection_id")
    ):
        raise RunContextError("run context selection binding does not match")
    expected_digest = _selection_digest(selection)
    if context.get("selection_digest") != expected_digest:
        raise RunContextError("run context selection digest does not match")
    terminal_state_seq = context.get("terminal_state_seq")
    if terminal_state_seq is not None and type(terminal_state_seq) is not int:
        raise RunContextError("run context terminal_state_seq is invalid")
    if context.get("goal_mode") == GOAL_HEART:
        validate_heart_selection(
            context.get("selection"), game, decision_hash, controller_hash
        )
    return context


def create_context(state, decision_hash, controller_hash, selection=None):
    game = state.get("game_state") or {}
    goal_mode = determine_goal_mode(state)
    validate_active_run(state, goal_mode)
    if goal_mode == GOAL_HEART:
        validate_heart_selection(
            selection, game, decision_hash, controller_hash
        )
    attempt_id = str(uuid.uuid4())
    frozen_selection = selection or {
        "selection_id": f"active-run:{attempt_id}",
        "algorithm": "active-run-fallback-v1",
        "reason": "active_run_authoritative_state",
        "eligible_attempt_ids": [],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "attempt_id": attempt_id,
        "run_id": authoritative_run_id(game),
        "seed": game.get("seed"),
        "goal_mode": goal_mode,
        "character": game.get("class"),
        "ascension_level": int(game.get("ascension_level", 0) or 0),
        "run_type": "standard",
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "selection_id": frozen_selection["selection_id"],
        "selection_digest": _selection_digest(frozen_selection),
        "started_state_seq": state.get("state_seq"),
        "terminal_state_seq": None,
        "progression_at_start": progression_snapshot(state),
        "selection": frozen_selection,
    }


def bind_terminal_state(path, state, context, decision_hash, controller_hash):
    """Persist the exact authoritative terminal sequence in the run context."""

    if ((state.get("game_state") or {}).get("screen_type")) != "GAME_OVER":
        raise RunContextError("terminal context binding requires GAME_OVER")
    validated = validate_context_binding(
        state, context, decision_hash, controller_hash
    )
    terminal_state_seq = state.get("state_seq")
    if type(terminal_state_seq) is not int:
        raise RunContextError("authoritative terminal state_seq is invalid")
    existing_terminal = validated.get("terminal_state_seq")
    if existing_terminal not in (None, terminal_state_seq):
        raise RunContextError("run context is already bound to another terminal")
    bound = {**validated, "terminal_state_seq": terminal_state_seq}
    _atomic_write(path, bound)
    return bound


def _atomic_write(path, payload):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_or_create_context(
    state,
    decision_hash,
    controller_hash,
    path,
    selection=None,
    selection_path=None,
):
    path = Path(path)
    game = state.get("game_state") or {}
    current_run_id = authoritative_run_id(game)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RunContextError("existing run context is unreadable")
        if not isinstance(existing, dict) or not existing.get("run_id"):
            raise RunContextError("existing run context is invalid")
        if existing.get("run_id") == current_run_id:
            try:
                existing = validate_context_binding(
                    state, existing, decision_hash, controller_hash
                )
            except RunContextError as exc:
                raise RunContextError(
                    "the active seed belongs to a different or invalid "
                    "strategy context; start a fresh run"
                ) from exc
            existing_terminal = existing.get("terminal_state_seq")
            if existing_terminal is not None:
                if determine_goal_mode(state) != GOAL_HEART:
                    raise RunContextError(
                        "a terminal context cannot be reused for an unlock run"
                    )
                if selection_path is None or not Path(selection_path).exists():
                    raise RunContextError(
                        "a terminal same-seed Heart context requires a new "
                        "pending campaign selection"
                    )
                next_selection = read_pending_selection(
                    selection_path, state, decision_hash, controller_hash
                )
                try:
                    pre_run_binding.load_start_acceptance(
                        Path(selection_path).resolve().parent,
                        next_selection,
                    )
                except pre_run_binding.PreRunBindingError as exc:
                    raise RunContextError(str(exc)) from exc
                if (
                    next_selection.get("selection_id")
                    == existing.get("selection_id")
                ):
                    raise RunContextError(
                        "a terminal same-seed context requires a new selection_id"
                    )
                replacement = create_context(
                    state,
                    decision_hash,
                    controller_hash,
                    selection=next_selection,
                )
                _atomic_write(path, replacement)
                _delete_pending_selection(selection_path, next_selection)
                return replacement
            # A crash or filesystem error can occur after the context is
            # durably written but before its exact pending selection is
            # deleted.  Finish that transaction on retry so the same
            # selection can never leak into a later seed.
            if (
                selection_path is not None
                and existing.get("goal_mode") == GOAL_HEART
                and Path(selection_path).exists()
            ):
                try:
                    pre_run_binding.load_start_acceptance(
                        Path(selection_path).resolve().parent,
                        existing.get("selection"),
                    )
                except pre_run_binding.PreRunBindingError as exc:
                    raise RunContextError(str(exc)) from exc
                _delete_pending_selection(
                    selection_path, existing.get("selection")
                )
            return existing
    if determine_goal_mode(state) == GOAL_HEART and selection is None:
        if selection_path is None:
            raise RunContextError(
                "a new Heart attempt requires a pending campaign selection"
            )
        selection = read_pending_selection(
            selection_path, state, decision_hash, controller_hash
        )
        try:
            pre_run_binding.load_start_acceptance(
                Path(selection_path).resolve().parent, selection
            )
        except pre_run_binding.PreRunBindingError as exc:
            raise RunContextError(str(exc)) from exc
        pending_selection = selection
    else:
        pending_selection = None
    context = create_context(state, decision_hash, controller_hash, selection=selection)
    _atomic_write(path, context)
    if pending_selection is not None:
        _delete_pending_selection(selection_path, pending_selection)
    return context


def load_matching_context(state, decision_hash, controller_hash, path):
    """Load an existing context without creating one or consuming selection."""

    path = Path(path)
    try:
        context = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunContextError("matching run context is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RunContextError("matching run context is unreadable") from exc
    return validate_context_binding(state, context, decision_hash, controller_hash)


def load_context(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def read_pending_selection(path, state, decision_hash, controller_hash):
    """Read and validate a selector record without consuming it."""

    path = Path(path)
    try:
        selection = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunContextError("pending campaign selection is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RunContextError("pending campaign selection is unreadable") from exc
    game = _validated_game_identity(state)
    try:
        validate_heart_selection(
            selection,
            game,
            decision_hash,
            controller_hash,
        )
    except RunContextError as exc:
        raise RunContextError(
            "pending campaign selection does not match the active run"
        ) from exc
    return selection


def _delete_pending_selection(path, expected_selection):
    """Delete only the exact selection that was durably bound to a context."""

    path = Path(path)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunContextError("pending campaign selection disappeared") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RunContextError("pending campaign selection became unreadable") from exc
    if current != expected_selection:
        raise RunContextError("pending campaign selection changed before consumption")
    try:
        path.unlink()
    except OSError as exc:
        raise RunContextError("pending campaign selection could not be consumed") from exc


def consume_pending_selection(path, state, decision_hash, controller_hash):
    """Consume a selector record only when it matches the active run."""

    selection = read_pending_selection(
        path, state, decision_hash, controller_hash
    )
    _delete_pending_selection(path, selection)
    return selection
