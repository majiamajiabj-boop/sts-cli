"""Bounded functional autoplay sessions using the guarded campaign entry point.

Strategy findings remain non-release-eligible. Protocol/mechanics failures stop
the session through the existing P0-only transition gate.
"""
import argparse
import json
import time
import uuid
from pathlib import Path

from assistant_paths import ROOT
from campaign_attempt import OSLease
from quick_campaign import start_campaign


def run_count(value):
    if isinstance(value, bool) or str(value).strip() not in {str(n) for n in range(1, 25)}:
        raise ValueError("自动托管局数请输入 1 至 24 的整数。")
    return int(value)


def run_batch(root, runs, *, start=start_campaign):
    root = Path(root).resolve()
    count = run_count(runs)
    directory = root / "data"
    directory.mkdir(parents=True, exist_ok=True)
    with OSLease(directory / "auto-batch.lock", "自动托管"):
        state = {"session_id": str(uuid.uuid4()), "requested": count,
                 "completed": 0, "status": "starting", "attempts": []}

        def publish():
            state["updated_at"] = time.time()
            path = directory / "auto-batch.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)

        def started(attempt):
            state.update(status="running", attempt_id=attempt["attempt_id"])
            publish()

        publish()
        try:
            for index in range(count):
                state.update(status="starting", current=index + 1)
                publish()
                launch = start(root, p0_only_batch=True, wait_for_completion=True,
                               on_started=started)
                result = json.loads((root / "run-result.json").read_text(encoding="utf-8"))
                if (result.get("attempt_id") != launch.get("attempt_id")
                        or result.get("attempt_id") in {a["attempt_id"] for a in state["attempts"]}
                        or result.get("termination_kind") != "game_over"):
                    raise RuntimeError("终局记录缺失、重复或与本局不匹配，已停止自动开局。")
                state["attempts"].append({key: result.get(key) for key in
                    ("attempt_id", "character", "victory", "heart_defeated", "floor")})
                state["completed"] += 1
                publish()
            state["status"] = "completed"
        except Exception as exc:
            state.update(status="error", error=str(exc))
            publish()
            raise
        publish()
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description="按指定局数连续自动托管；策略结果不代表通关保证。")
    parser.add_argument("--runs", type=run_count, default=1)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        result = run_batch(args.root, args.runs)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
