"""Seal the fresh, tested distribution using the existing auto-play release gate.

Runs only during packaging. No game, bridge or controller process is launched.
"""
import argparse
import hashlib
import json
from pathlib import Path


def validate_shipped_sources(root, proof):
    files = proof.get("packaged_sources")
    if not isinstance(files, dict) or not files:
        raise ValueError("缺少已测试源码与发布源码的逐文件绑定。")
    for relative, expected in files.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("发布源码文件不存在或越界：" + relative)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("发布源码在测试后发生变化：" + relative)


def seal(root, proof):
    import decision_case_corpus
    import decision_case_replay
    import freeze_manifest
    validate_shipped_sources(root, proof)
    if (root / "decision-cases.jsonl").exists() or (root / "decision-case-corpus-sources.json").exists():
        raise ValueError("只能为新建发布目录初始化空白案例集；不得清除已使用的历史。")
    freeze_manifest._validate_verification_evidence(proof["verification"])
    cases_path = root / "decision-cases.jsonl"
    fixtures = decision_case_replay.load_cases(root / "test_fixtures/decision-cases-v2.jsonl")
    if not fixtures or any(not str(case.get("attempt_id", "")).startswith("fixture-") for case in fixtures):
        raise ValueError("新安装只能使用明确标记的合成回归案例。")
    cases_path.write_text("".join(json.dumps(case, ensure_ascii=True) + "\n" for case in fixtures), encoding="utf-8")
    manifest_path = root / "decision-case-corpus-sources.json"
    manifest = decision_case_corpus.build_manifest(cases_path, [])
    decision_case_corpus.write_manifest(manifest_path, manifest)
    cases, evidence = decision_case_corpus.load_case_corpus(cases_path, manifest_path, with_evidence=True)
    hashes = freeze_manifest._fresh_policy_hashes(root)
    report = decision_case_replay.audit_cases(cases, hashes["decision_hash"],
        fixture_cases=decision_case_replay.load_cases(root / "test_fixtures/decision-cases-v2.jsonl"),
        case_corpus_evidence=evidence)
    decision_case_replay.write_report(root / "decision-case-replay.json", report)
    checkpoint = freeze_manifest.build_static_preflight(root, hashes["decision_hash"], hashes["controller_hash"],
        verification=proof["verification"], decision_case_replay=report)
    freeze_manifest.write_manifest(root / "release-preflight.json", checkpoint)
    freeze_manifest.validate_static_preflight(checkpoint, root, hashes["decision_hash"], hashes["controller_hash"])
    return {"static_gate": "clear", "test_count": checkpoint["full_tests"]["test_count"],
            "fixture_count": report["fixture_case_count"], "bootstrap_fixture_count": len(cases), "personal_history_count": 0, **hashes}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verification", type=Path, required=True)
    args = parser.parse_args()
    result = seal(Path(__file__).resolve().parent, json.loads(args.verification.read_text(encoding="utf-8")))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
