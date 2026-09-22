"""Build the expensive, source-bound half of the gameplay release gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import freeze_manifest


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "release-preflight.json"


def build_and_write(
    root,
    decision_hash,
    controller_hash,
    replay_path,
    output_path,
):
    root = Path(root).resolve()
    try:
        replay = json.loads(Path(replay_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise freeze_manifest.FreezeManifestError(
            "DecisionCase replay report is unreadable"
        ) from exc
    preflight = freeze_manifest.build_static_preflight(
        root,
        decision_hash,
        controller_hash,
        decision_case_replay=replay,
    )
    freeze_manifest.write_manifest(output_path, preflight)
    return preflight


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Run full tests, git diff --check and DecisionCase revalidation "
            "before starting the game."
        )
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--decision-hash", required=True)
    parser.add_argument("--controller-hash", required=True)
    parser.add_argument(
        "--decision-case-replay",
        type=Path,
        default=ROOT / "decision-case-replay.json",
    )
    parser.add_argument("--write", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        preflight = build_and_write(
            args.root,
            args.decision_hash,
            args.controller_hash,
            args.decision_case_replay,
            args.write,
        )
    except freeze_manifest.FreezeManifestError as exc:
        print(json.dumps({
            "status": "error",
            "error": str(exc),
        }, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps({
        "status": "clear",
        "preflight_path": str(args.write.resolve()),
        "source_digest": preflight["source_digest"],
        "source_file_count": preflight["source_file_count"],
        "test_count": preflight["full_tests"]["test_count"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
