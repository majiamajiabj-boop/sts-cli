"""Paired *offline* combat experiment. Never starts the game or a controller.

All arms use the same native transition model, starting world and no potions.
The candidate samples future draw/RNG independently. This is not a Heart-win test.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src/spirecomm-master"))
from spirecomm.ai.agent import SimpleAgent
from spirecomm.spire.character import PlayerClass
from spirecomm.spire.game import Game
from spirecomm.communication.action import EndTurnAction, PlayCardAction


class NativeLab:
    def __init__(self, binary, error_file):
        self.errors = Path(error_file).open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.errors, text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.responses = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def call(self, request):
        self.process.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
        self.process.stdin.flush()
        try:
            line = self.responses.get(timeout=60)
        except queue.Empty as exc:
            raise RuntimeError("native operation timeout") from exc
        if line is None:
            raise RuntimeError(f"native process exited: {self.process.poll()}")
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.errors.close()


def current_action(agent, observation):
    game = Game.from_json(observation["game_state"], ["play", "end"])
    if observation["select_task"] == "HEADBUTT":
        # Same retrieval scorer as the current planner, with no simulator lookahead.
        card = max(game.discard_pile,
                   key=lambda c: agent.combat_planner.score_retrieval_card(game, c))
        choices = [a for a in observation["actions"] if a.get("card_uuid") == card.uuid]
    else:
        action = agent.get_next_action_in_game(game)
        if isinstance(action, EndTurnAction):
            choices = [a for a in observation["actions"] if a["kind"] == "end"]
        elif isinstance(action, PlayCardAction):
            card = action.card if action.card is not None else game.hand[action.card_index]
            target = (action.target_monster.monster_index if action.target_monster is not None
                      else action.target_index)
            choices = [a for a in observation["actions"] if a["kind"] == "play"
                       and a["card_uuid"] == card.uuid
                       and (not card.has_target or a["target"] == target)]
        else:
            raise RuntimeError(f"unsupported baseline action: {type(action).__name__}")
    if len(choices) != 1:
        raise RuntimeError(f"baseline action must bind uniquely, found {len(choices)}")
    return choices[0]["token"]


def run_battle(binary, spec, world, policy, output, worlds, simulations, max_decisions):
    lab = NativeLab(binary, output.with_suffix(".stderr.log"))
    timings = []
    trace = []
    result = {"policy": policy, "execution_world": world, "status": "error"}
    try:
        obs = lab.call({"op": "start", "spec": spec, "world": world,
                        "expose_execution_draw_order":policy=="current_v5_ordered",
                        "remember_topdeck":policy=="sampled_search_memory"})
        result["initial_observation_sha256"] = hashlib.sha256(
            json.dumps(obs, sort_keys=True).encode()).hexdigest()
        # The ordered baseline deliberately sees the same pile order as live
        # CommunicationMod. Compare initial states after removing ONLY that
        # declared information difference, and retain both hashes for audit.
        canonical=json.loads(json.dumps(obs))
        cs=canonical["game_state"]["combat_state"]
        cs["draw_pile_order_known"]=False
        cs["draw_pile"].sort(key=lambda c:int(c["uuid"]))
        result["initial_canonical_state_sha256"]=hashlib.sha256(
            json.dumps(canonical,sort_keys=True).encode()).hexdigest()
        result["entry_hp"] = obs["hp"]
        agent = SimpleAgent(chosen_class=PlayerClass.IRONCLAD, goal_mode="HEART")
        for decision in range(max_decisions):
            if obs["outcome"] != "ongoing":
                result.update(status=obs["outcome"], end_hp=obs["hp"], turns=obs["turn"])
                break
            before = time.perf_counter()
            if policy in {"sampled_search", "sampled_search_adaptive", "sampled_search_memory"}:
                choice = lab.call({"op": "search", "planning_seed": 424242 + decision,
                                   "worlds": worlds, "simulations": simulations,
                                   "allocation": "adaptive_quarter" if policy == "sampled_search_adaptive" else "balanced"})
                token = choice["token"]
            else:
                choice = {}
                token = current_action(agent, obs)
            timings.append((time.perf_counter() - before) * 1000)
            action = next(a for a in obs["actions"] if a["token"] == token)
            cs = obs["game_state"]["combat_state"]
            trace.append({"decision":decision, "turn":obs["turn"], "hp":obs["hp"],
                          "energy":cs["player"]["energy"], "block":cs["player"]["block"],
                          "hand":[[c["id"],c["uuid"],c["cost"]] for c in cs["hand"]],
                          "action":action, "search":choice})
            obs = lab.call({"op":"step", "revision":obs["revision"], "token":token})
        else:
            # The final permitted action may itself end the battle.
            result.update(status=obs["outcome"] if obs["outcome"] != "ongoing" else "cutoff",
                          end_hp=obs["hp"], turns=obs["turn"])
    except (RuntimeError, ValueError, KeyError, TypeError, BrokenPipeError) as exc:
        result["error"] = str(exc)
    finally:
        lab.close()
    result["decisions"] = len(timings)
    result["latency_ms"] = {"mean":sum(timings)/max(1,len(timings)),
                            "max":max(timings,default=0)}
    output.write_text(json.dumps({"result":result,"trace":trace}, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--binary",type=Path,default=ROOT/"experiments/build/blind-combat-lab.exe")
    parser.add_argument("--execution-worlds",type=int,default=4)
    parser.add_argument("--world-start",type=int,default=0)
    parser.add_argument("--comparison",choices=("draw_order", "allocation", "topdeck", "mechanisms"),default="draw_order")
    parser.add_argument("--search-worlds",type=int,default=4)
    parser.add_argument("--simulations",type=int,default=300)
    parser.add_argument("--max-decisions",type=int,default=250)
    args=parser.parse_args()
    if min(args.execution_worlds,args.search_worlds,args.simulations,args.max_decisions) < 1:
        parser.error("all budgets must be positive")
    if args.world_start < 0:
        parser.error("world start must be nonnegative")
    policies = {
        "draw_order": ("current_v5_ordered", "current_v5_unordered", "sampled_search"),
        "allocation": ("current_v5_ordered", "sampled_search", "sampled_search_adaptive"),
        "topdeck": ("current_v5_ordered", "sampled_search", "sampled_search_memory"),
        "mechanisms": ("current_v5_ordered", "sampled_search", "sampled_search_adaptive", "sampled_search_memory"),
    }[args.comparison]
    if args.search_worlds > 32 or args.simulations > 50000:
        parser.error("native search budget exceeds bounded maximum")
    upstream=ROOT/".tools/sts_lightspeed"
    revision=subprocess.check_output(["git","-C",str(upstream),"rev-parse","HEAD"],text=True).strip()
    if revision!="46e14e4adc23d2c1c738df8902a6f297109fee50":
        parser.error("upstream revision differs from validated pin")
    if subprocess.check_output(["git","-C",str(upstream),"status","--porcelain","--untracked-files=no"],text=True).strip():
        parser.error("upstream contains local modifications")
    args.output.mkdir(parents=True,exist_ok=False)
    rows=[]
    tracked_sources = [p for directory in (ROOT/"src/spirecomm-master/spirecomm",ROOT/"experiments/native")
                       for p in sorted(directory.rglob("*")) if p.suffix in {".py",".cpp",".h"}]
    tracked_sources += [Path(__file__).resolve(), ROOT/"autoplay.py", ROOT/"bridge.py",
                        ROOT/"src/CommunicationMod-1.2.1/src/main/java/communicationmod/GameStateConverter.java"]
    report={"baseline_draw_order":{"current_v5_unordered":"unknown","current_v5_ordered":"same_as_live_protocol"}, "input_sha256":{}, "live_activation":False,"future_rng_isolated":True,"rows":rows,
            "model_adaptations":["Rage initial native cost corrected from 1 to Java-verified 0; native curse -3 exported as Java -2; all deck costs checked"],
            "binary_sha256":hashlib.sha256(args.binary.read_bytes()).hexdigest(),
            "upstream_revision":revision,
            "source_sha256":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in tracked_sources},
            "search_allocations":{"sampled_search":"always balanced; inherited floor 2048 does not change allocation",
                                  "sampled_search_adaptive":"UCT after max(1, simulations // (4 * root_action_count)) samples per action"},
            "configuration":{k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},
            "limitations":["native model not certified against Java transitions",
                           "fresh encounters, not historical replay", "potions disabled for both",
                           "only sampled_search_memory retains Headbutt topdeck; it forgets at enemy turns and random insertion",
                           "baseline Headbutt uses retrieval scorer instead of screen handler",
                           "sampled perfect-information continuations can be optimistic",
                           "internal monster state is not reconstructed from protocol"]}
    archive=args.output/"source-snapshot.zip"
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED) as snapshot:
        for path in tracked_sources:
            payload=path.read_bytes()
            name=path.relative_to(ROOT).as_posix()
            if hashlib.sha256(payload).hexdigest()!=report["source_sha256"][str(path.relative_to(ROOT))]:
                raise RuntimeError("source changed while capturing experiment: "+name)
            snapshot.writestr(name,payload)
    report["source_archive_sha256"]=hashlib.sha256(archive.read_bytes()).hexdigest()
    for path in sorted(args.inputs.glob("*.input.json")):
        spec=json.loads(path.read_text(encoding="utf-8"))
        report["input_sha256"][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        if spec["game_state"]["class"]!="IRONCLAD":continue
        (args.output/path.name).write_text(json.dumps(spec,indent=2),encoding="utf-8")
        # Native potion effects must be absent even when the source held potions.
        spec["game_state"]["potions"]=[]
        for world in range(args.world_start, args.world_start + args.execution_worlds):
            for policy in policies:
                name=f"{path.stem}-w{world}-{policy}"
                result=run_battle(args.binary,spec,world,policy,args.output/f"{name}.json",
                                  args.search_worlds,args.simulations,args.max_decisions)
                result["scenario"]=path.stem
                rows.append(result)
                (args.output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
                print(json.dumps({k:result.get(k) for k in ("scenario","execution_world","policy","status","end_hp","error")}),flush=True)
    report["distribution"]={policy:dict(collections.Counter(r["status"] for r in rows if r["policy"]==policy))
                            for policy in policies}
    report["paired_initial_states_match"]=bool(rows) and all(
        rows[i].get("initial_canonical_state_sha256") is not None and
        len({r.get("initial_canonical_state_sha256") for r in rows[i:i+len(policies)]})==1
        for i in range(0,len(rows),len(policies)))
    report["live_gate"]="blocked_pending_model_and_policy_validation"
    (args.output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps({"distribution":report["distribution"],"paired":report["paired_initial_states_match"]}))
    if not rows or not report["paired_initial_states_match"] or any(r["status"] in {"error","cutoff"} for r in rows):
        raise SystemExit(1)


if __name__=="__main__":main()
