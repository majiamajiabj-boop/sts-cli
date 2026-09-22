import argparse
import json
import os
import time
import uuid
from pathlib import Path

import campaign_selector


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
META_PATH = ROOT / "state-meta.json"
COMMAND_PATH = ROOT / "command.json"
RECEIPT_PATH = ROOT / "action-receipt.json"
RUN_CONTEXT_PATH = ROOT / "run-context.json"
REQUIRED_POLICY_VERSION = "fast-policy-v5"
ATTEMPT_BINDING_FIELDS = (
    "attempt_id",
    "run_id",
    "seed",
    "character",
    "ascension_level",
    "run_type",
    "decision_hash",
    "controller_hash",
    "policy_version",
    "selection_id",
    "selection_digest",
)


def read_json(path):
    for attempt in range(100):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError):
            if attempt == 99:
                raise
            time.sleep(0.02)


def load_state():
    return read_json(STATE_PATH)


def current_sequence():
    try:
        return int(load_state()["state_seq"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return 0


def atomic_write_json(path, payload):
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    os.replace(temp_path, path)


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _valid_seed(value):
    return type(value) is int or _nonempty_string(value)


def _validate_attempt_binding(binding, state, *, require_selection=False):
    """Validate one complete attempt identity against an active game frame."""

    if not isinstance(binding, dict):
        raise ValueError("active attempt binding is not an object")
    missing = [field for field in ATTEMPT_BINDING_FIELDS if field not in binding]
    if missing:
        raise ValueError(
            "active attempt binding is incomplete: " + ",".join(missing)
        )
    game = state.get("game_state")
    if not isinstance(game, dict):
        raise ValueError("active state is missing game_state")
    seed = game.get("seed")
    character = game.get("class")
    ascension_level = game.get("ascension_level")
    expected_run_id = f"{character}:{ascension_level}:{seed}"
    required_strings = (
        "attempt_id",
        "run_id",
        "character",
        "run_type",
        "decision_hash",
        "controller_hash",
        "policy_version",
        "selection_id",
        "selection_digest",
    )
    if any(not _nonempty_string(binding.get(field)) for field in required_strings):
        raise ValueError("active attempt binding contains an invalid string field")
    if (
        not _valid_seed(seed)
        or not _valid_seed(binding.get("seed"))
        or type(binding.get("seed")) is not type(seed)
        or binding.get("seed") != seed
        or binding.get("character") != character
        or type(ascension_level) is not int
        or type(binding.get("ascension_level")) is not int
        or binding.get("ascension_level") != ascension_level
        or ascension_level != 0
        or binding.get("run_id") != expected_run_id
        or binding.get("run_type") != "standard"
        or game.get("is_standard_run") is not True
        or binding.get("policy_version") != REQUIRED_POLICY_VERSION
    ):
        raise ValueError("active attempt binding does not match authoritative state")
    if require_selection:
        if binding.get("schema_version") != 2:
            raise ValueError("run context schema_version is not 2")
        if binding.get("goal_mode") != "HEART":
            raise ValueError("run context goal_mode is not HEART")
        if binding.get("terminal_state_seq") is not None:
            raise ValueError("terminal run context cannot bind an active command")
        selection = binding.get("selection")
        if not isinstance(selection, dict):
            raise ValueError("run context selection is missing")
        expected_selection = {
            "selection_id": binding["selection_id"],
            "decision_hash": binding["decision_hash"],
            "controller_hash": binding["controller_hash"],
            "policy_version": binding["policy_version"],
            "character": binding["character"],
            "ascension_level": binding["ascension_level"],
            "run_type": binding["run_type"],
            "goal_mode": "HEART",
            "algorithm": "beta-thompson-v1",
        }
        for field, expected in expected_selection.items():
            observed = selection.get(field)
            if type(observed) is not type(expected) or observed != expected:
                raise ValueError(f"run context selection mismatch: {field}")
        if binding.get("selection_digest") != (
            campaign_selector.selection_digest(selection)
        ):
            raise ValueError("run context selection digest mismatch")
    return {
        field: binding[field]
        for field in ATTEMPT_BINDING_FIELDS
    }


def _active_attempt_binding(state, context_path=None):
    """Resolve the bridge-authored projection or the first-frame context."""

    if state.get("in_game") is not True:
        return {}
    projected_fields = [field for field in ATTEMPT_BINDING_FIELDS if field in state]
    if projected_fields:
        if len(projected_fields) != len(ATTEMPT_BINDING_FIELDS):
            raise ValueError("active state carries a partial attempt binding")
        return _validate_attempt_binding(state, state)

    context_path = RUN_CONTEXT_PATH if context_path is None else Path(context_path)
    try:
        context = read_json(context_path)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise ValueError("active state has no validated run context") from exc
    return _validate_attempt_binding(context, state, require_selection=True)


def bound_payload(state, action, **fields):
    payload = {
        "id": str(uuid.uuid4()),
        "action": action,
        "policy_version": REQUIRED_POLICY_VERSION,
        "expected_seq": state["state_seq"],
        "decision_id": state["decision_id"],
        "phase": state["phase"],
        **fields,
    }
    if str(action).lower() != "state" and state.get("in_game") is True:
        binding = _active_attempt_binding(state)
        caller_binding = set(fields) & set(ATTEMPT_BINDING_FIELDS)
        if caller_binding:
            raise ValueError(
                "attempt binding must come from authoritative state/context"
            )
        payload.update(binding)
    return payload


def wait_for_receipt(request_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            receipt = read_json(RECEIPT_PATH)
            if receipt.get("request_id") == request_id and receipt.get("status") in {"succeeded", "failed", "rejected"}:
                return receipt
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise TimeoutError(f"no final action receipt after {timeout:.1f}s")


def send_payload(payload, timeout=15.0):
    if payload.get("action") != "state" and payload.get("policy_version") != REQUIRED_POLICY_VERSION:
        raise ValueError(
            f"state-changing request requires policy_version={REQUIRED_POLICY_VERSION}"
        )
    atomic_write_json(COMMAND_PATH, payload)
    return wait_for_receipt(payload["id"], timeout)


def summarize(state):
    game = state.get("game_state") or {}
    text = f"seq={state.get('state_seq')} phase={state.get('phase')} decision={state.get('decision_id')}"
    if game:
        text += f" act={game.get('act')} floor={game.get('floor')} hp={game.get('current_hp')}/{game.get('max_hp')} screen={game.get('screen_type')}"
    return text


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    sub.add_parser("raw")
    send = sub.add_parser("send-json")
    send.add_argument("payload")
    send.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    if args.action == "raw":
        print(json.dumps(load_state(), ensure_ascii=False, indent=2))
    elif args.action == "status":
        print(summarize(load_state()))
    else:
        payload = json.loads(args.payload)
        receipt = send_payload(payload, args.timeout)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        if receipt.get("success"):
            state = load_state()
            if state.get("state_seq") != receipt.get("result_state_seq"):
                raise RuntimeError("receipt/result state sequence mismatch")
            if receipt.get("requested_target_id") != receipt.get("resolved_target_id"):
                raise RuntimeError("requested/resolved target mismatch")
            print(summarize(state))


if __name__ == "__main__":
    main()
