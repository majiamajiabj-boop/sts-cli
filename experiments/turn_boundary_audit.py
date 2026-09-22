"""Compare current END projections with recorded real-game next-turn HP.

No game/controller imports or actions. This checks stable HP only, not a full
state reconstruction or a certification of the native search simulator.
"""
import argparse
import collections
import copy
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src/spirecomm-master"))
from spirecomm.ai.agent import SimpleAgent
from spirecomm.ai import combat_predictor
from spirecomm.spire.character import PlayerClass
from spirecomm.spire.game import Game


def restore_aliases(value):
    if isinstance(value, list):
        return [restore_aliases(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {k: restore_aliases(v) for k, v in value.items()}
    if "card_instance_id" in result:
        result.setdefault("uuid", result["card_instance_id"])
    return result


def boundary_reason(record):
    before = record.get("authoritative_state_before") or {}
    after = record.get("authoritative_state_after") or {}
    for key in ("attempt_id", "run_id", "decision_hash", "controller_hash"):
        if not before.get(key) or before[key] != after.get(key) or before[key] != record.get(key):
            return "missing_or_mismatched_" + key
    if before.get("state_seq") != record.get("before_seq") or after.get("state_seq") != record.get("after_seq"):
        return "sequence_mismatch"
    if after["state_seq"] <= before["state_seq"]:
        return "nonadvancing_sequence"
    if not before.get("ready_for_command") or not after.get("ready_for_command"):
        return "not_ready_boundary"
    bg = before.get("game_state") or {}
    ag = after.get("game_state") or {}
    if (bg.get("act"), bg.get("floor")) != (ag.get("act"), ag.get("floor")):
        return "different_floor"
    if not after.get("phase", "").startswith("COMBAT_TURN_"):
        return "terminal_or_noncombat_boundary"
    bt = (bg.get("combat_state") or {}).get("turn")
    at = (ag.get("combat_state") or {}).get("turn")
    if type(bt) is not int or at != bt + 1:
        return "not_exactly_next_turn"
    return None


def project(before):
    state = restore_aliases(copy.deepcopy(before["game_state"]))
    # Map topology is absent from the bounded audit projection and is never
    # consumed by END evaluation. Do not synthesize missing combat fields.
    state["map"] = []
    game = Game.from_json(state, before["available_commands"])
    outcome = combat_predictor.projected_turn_outcome(game)
    planner = SimpleAgent(chosen_class=PlayerClass[state["class"]], goal_mode="HEART").combat_planner
    planner._best_plan(game, [], outcome.attack_hp_loss, outcome.total_hp_loss, 0)
    detail = planner._last_initial_search
    return {
        "stable_hp": detail.get("projected_player_hp_after_turn"),
        "gross_hp_loss": detail["projected_hp_loss"],
        "healing_before_attacks": detail["player_end_turn_healing_before_attacks"],
        "tier": detail["tier"],
        # This derived value is diagnostic only. It must not masquerade as
        # the planner's stable HP or survival classification.
        "derived_net_hp": max(0, min(game.player.max_hp,
            game.player.current_hp - detail["projected_hp_loss"]
            + detail["player_end_turn_healing_before_attacks"])),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=Path, default=ROOT / "logs/attempts")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-match", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rows, counts, attempts, source_hashes = [], collections.Counter(), [], {}
    for directory in sorted(args.attempts.iterdir()):
        if not directory.is_dir():
            continue
        try:
            uuid.UUID(directory.name)
        except ValueError:
            continue
        path = directory / "autoplay.log"
        if not path.is_file():
            raise FileNotFoundError("missing attempt trace: " + str(path))
        attempts.append(path.parent.name)
        source_hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    record = json.loads(line)
                except ValueError:
                    counts["malformed_log_lines"] += 1
                    continue
                if record.get("record_type") != "decision" or record.get("action") != "end":
                    continue
                row = {"attempt_id": path.parent.name, "file": str(path.relative_to(ROOT)),
                       "line": line_number, "state_seq": record.get("before_seq"),
                       "floor": record.get("floor"), "turn": record.get("turn"),
                       "historical_decision_hash": record.get("decision_hash"),
                       "historical_controller_hash": record.get("controller_hash")}
                reason = "attempt_directory_mismatch" if record.get("attempt_id") != path.parent.name else boundary_reason(record)
                if reason:
                    row.update(status="excluded", reason=reason)
                else:
                    before, after = record["authoritative_state_before"], record["authoritative_state_after"]
                    row.update(observed_hp=after["game_state"]["current_hp"],
                               historical_prediction=record.get("projected_player_hp_after_turn_before"))
                    try:
                        row["current"] = project(before)
                        hp = row["current"]["stable_hp"]
                        row["status"] = "missing_stable_prediction" if hp is None else "match" if hp == row["observed_hp"] else "mismatch"
                    except (KeyError, TypeError, ValueError, AttributeError) as exc:
                        row.update(status="unsupported_snapshot", reason=type(exc).__name__ + ": " + str(exc))
                counts[row["status"]] += 1
                rows.append(row)
    source_files = [Path(__file__), *sorted((ROOT / "src/spirecomm-master/spirecomm").rglob("*.py"))]
    report = {"scope": "current END candidate stable HP versus real recorded next turn; not full replay",
              "attempts": attempts, "counts": dict(counts), "rows": rows,
              "input_sha256": source_hashes,
              "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files},
              "limitations": ["historical combat counters and move history may be missing",
                              "derived_net_hp alone does not certify temporal survival",
                              "terminal/noncombat transitions excluded explicitly"]}
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"attempts": len(attempts), "counts": dict(counts)}))
    if args.require_match and (
        counts["match"] == 0 or any(counts[k] for k in (
            "malformed_log_lines", "mismatch", "missing_stable_prediction", "unsupported_snapshot"))
        or any(r["status"] == "excluded" and r["reason"] != "terminal_or_noncombat_boundary" for r in rows)
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
