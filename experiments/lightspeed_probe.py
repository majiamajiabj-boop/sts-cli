"""Offline feasibility probe; never imports or invokes a live controller.

The prospective battle results are an oracle baseline, NOT live-policy win rates:
upstream searches the same hidden RNG state it subsequently executes.
"""
from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

UPSTREAM_REVISION = "46e14e4adc23d2c1c738df8902a6f297109fee50"
ROOT = Path(__file__).resolve().parents[1]
ENCOUNTERS = {
    ("SphericGuardian",): "Spheric Guardian",
    ("Shelled Parasite",): "Shell Parasite",
    ("Centurion", "Healer"): "Centurion And Healer",
    ("SnakePlant",): "Snake Plant",
    ("Snecko",): "Snecko",
    ("BronzeAutomaton",): "AUTOMATON",
    ("Champ",): "CHAMP",
    ("Chosen",): "Chosen",
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def is_attempt(path):
    try:
        return path.is_dir() and str(uuid.UUID(path.name)) == path.name
    except ValueError:
        return False


def latest_attempts(root, count=6):
    paths = sorted((p for p in (root / "logs/attempts").iterdir() if is_attempt(p)),
                   key=lambda p: (p.stat().st_mtime_ns, p.name))
    if len(paths) < count:
        raise ValueError(f"need {count} attempts, found {len(paths)}")
    rows = []
    for path in paths[-count:]:
        selection = read_json(path / "selection.json")
        rows.append((float(selection["created_at"]), path))
    rows.sort(key=lambda row: (row[0], row[1].name))
    return [path for _, path in rows]


def import_gaps(state):
    """Necessary fields only; passing this check is not simulator certification."""
    game = state.get("game_state", {})
    combat = game.get("combat_state", {})
    gaps = set()
    if game.get("class") not in {"IRONCLAD", "DEFECT"}:
        gaps.add("unsupported_character")
    for monster in combat.get("monsters", []):
        if not monster.get("is_gone") or monster.get("half_dead"):
            if type(monster.get("move_id")) is not int:
                gaps.add("monster.move_id")
    for field in ("cards_played_this_turn", "attacks_played_this_turn",
                  "skills_played_this_turn", "powers_played_this_combat",
                  "times_damaged"):
        if type(combat.get(field)) is not int:
            gaps.add(f"combat.{field}")
    if game.get("class") == "DEFECT":
        for field in ("lightning_channeled", "frost_channeled"):
            if type(combat.get(field)) is not int:
                gaps.add(f"combat.{field}")
        if type(combat.get("emotion_chip_pending")) is not bool:
            gaps.add("combat.emotion_chip_pending")
    return sorted(gaps)


def prospective_spec(state, encounter):
    """Start a NEW simulated encounter with the permanent deck, not a replay."""
    game = copy.deepcopy(state["game_state"])
    if game["class"] not in {"IRONCLAD", "DEFECT"}:
        raise ValueError("unsupported character")
    for card in game["deck"]:
        # Dynamic permanent values cannot be reconstructed from the card ID.
        if card["id"] in {"RitualDagger", "Genetic Algorithm", "Searing Blow"}:
            raise ValueError(f"dynamic permanent card requires verified import: {card['id']}")
        card["bottled"] = any(card.get(key, False) for key in (
            "in_bottle_flame", "in_bottle_lightning", "in_bottle_tornado"))
    game.pop("combat_state", None)
    return {"game_state": game, "candidates": [], "targets": [encounter]}


def collect(root):
    inventory, checkpoints = [], []
    for attempt in latest_attempts(root):
        result = read_json(attempt / "run-result.json")
        counts, frames, seen, candidates = collections.Counter(), 0, set(), []
        trace = attempt / "autoplay.log"
        for line in trace.open(encoding="utf-8"):
            row = json.loads(line)
            if row.get("record_type") != "decision" or row.get("action") not in {"play", "end", "potion"}:
                continue
            state = row.get("authoritative_state_before") or {}
            game = state.get("game_state") or {}
            combat = game.get("combat_state") or {}
            if not combat:
                continue
            frames += 1
            counts.update(import_gaps(state))
            floor = game["floor"]
            if game["act"] != 2 or floor in seen:
                continue
            seen.add(floor)
            encounter = ENCOUNTERS.get(tuple(m["id"] for m in combat["monsters"] if not m.get("is_gone")))
            if encounter and game["class"] in {"IRONCLAD", "DEFECT"}:
                candidates.append({"attempt_id": attempt.name, "floor": floor,
                    "state_seq": state["state_seq"], "turn": combat["turn"],
                    "encounter": encounter, "state": state})
        inventory.append({"attempt_id": attempt.name, "character": result["character"],
            "decision_hash": result["decision_hash"], "controller_hash": result["controller_hash"],
            "final_act": result["act"], "final_floor": result["floor"],
            "combat_frames": frames, "missing_or_unsupported_counts": dict(counts),
            "trace_sha256": digest(trace)})
        # Fixed coverage: first and last recognized Act-2 encounter, no cherry-picking outcomes.
        if candidates:
            checkpoints.append(candidates[0])
            if candidates[-1]["floor"] != candidates[0]["floor"]:
                checkpoints.append(candidates[-1])
    return inventory, checkpoints


def evaluate(binary, checkpoint, output, worlds, simulations, millis):
    row = {key: value for key, value in checkpoint.items() if key != "state"}
    tag = f"{row['attempt_id'][:8]}-f{row['floor']}"
    try:
        spec = prospective_spec(checkpoint["state"], row["encounter"])
    except ValueError as error:
        return {**row, "status": "unsupported", "reason": str(error)}
    input_path = output / f"{tag}.input.json"
    input_path.write_text(json.dumps(spec, ensure_ascii=True), encoding="utf-8")
    command = [str(binary), "--battle-eval", str(input_path), str(simulations), "0",
               str(worlds), str(millis), "160"]
    started = time.monotonic()
    try:
        proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=180)
        row.update(elapsed_seconds=round(time.monotonic()-started, 3), exit_code=proc.returncode,
                   input_sha256=digest(input_path))
        if proc.returncode:
            return {**row, "status": "error", "reason": proc.stderr[-1500:]}
        result = json.loads(proc.stdout)
        (output / f"{tag}.output.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return {**row, "status": "ok", "aggregate": result["targets"][0]["aggregate"]}
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        return {**row, "status": "error", "reason": str(error)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worlds", type=int, default=4)
    parser.add_argument("--simulations", type=int, default=300)
    parser.add_argument("--millis", type=int, default=40)
    args = parser.parse_args()
    if min(args.worlds, args.simulations, args.millis) < 1:
        parser.error("budgets must be positive")
    source = args.root / ".tools/sts_lightspeed"
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"], text=True)
    if revision != UPSTREAM_REVISION or dirty:
        raise RuntimeError("upstream source must match the clean pinned revision")
    binary = source / "build/card-reward-eval.exe"
    self_test = subprocess.run([str(binary), "--self-test"], capture_output=True, text=True, timeout=30, check=True)
    args.output.mkdir(parents=True, exist_ok=False)
    inventory, checkpoints = collect(args.root)
    report = {"schema_version": 1, "upstream_revision": revision,
        "binary_sha256": digest(binary), "self_test": json.loads(self_test.stdout),
        "budgets": {"worlds": args.worlds, "simulations": args.simulations, "millis": args.millis},
        "live_activation": False, "compatibility_gate": "blocked_missing_state_and_information_boundary",
        "limitations": ["historical combat snapshots lack mandatory import fields",
            "fresh encounters are not historical replay", "upstream evaluator shares hidden RNG with search",
            "potions are disabled", "no same-world local-policy baseline", "no whole-run or Heart win-rate claim"],
        "attempts": inventory, "prospective_battles": []}
    report_path = args.output / "report.json"
    for checkpoint in checkpoints:
        row = evaluate(binary, checkpoint, args.output, args.worlds, args.simulations, args.millis)
        report["prospective_battles"].append(row)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(row), flush=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report={report_path}", flush=True)
    if any(row["status"] != "ok" for row in report["prospective_battles"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
