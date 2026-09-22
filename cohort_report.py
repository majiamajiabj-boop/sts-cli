"""Build a fail-closed report for the current audited Heart-run cohort."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import campaign_selector


SCHEMA_VERSION = 2
BATCH_SIZE = 6


def _attempt_started_at(terminal):
    for value in (
        terminal.get("started_at"),
        terminal.get("time"),
        (terminal.get("selection") or {}).get("created_at"),
    ):
        if type(value) in {int, float} and value > 0:
            return value
        if isinstance(value, str) and value.strip():
            return value
    return None


def _attempt_ended_at(terminal, audit):
    for value in (
        audit.get("generated_at"),
        terminal.get("finished_at"),
        terminal.get("time"),
    ):
        if type(value) in {int, float} and value > 0:
            return value
        if isinstance(value, str) and value.strip():
            return value
    return None


def build_cohort_report(
    records, decision_hash, *, performance_hash=None, generated_at=None,
):
    """Return the trailing continuous cohort admitted by the release gate."""

    triples = campaign_selector.eligible_attempt_triples(records, decision_hash)
    performance_triples = (
        campaign_selector.eligible_performance_attempt_triples(
            records, performance_hash
        )
        if performance_hash is not None
        else []
    )
    attempts = []
    for terminal, audit, controller_exit in triples:
        observed_max_act = terminal["observed_max_act"]
        attempts.append({
            "attempt_id": terminal["attempt_id"],
            "run_id": terminal["run_id"],
            "seed": terminal["seed"],
            "character": terminal["character"],
            "ascension_level": terminal["ascension_level"],
            "run_type": terminal["run_type"],
            "policy_version": terminal["policy_version"],
            "decision_hash": terminal["decision_hash"],
            "performance_hash": terminal.get("performance_hash"),
            "controller_hash": terminal["controller_hash"],
            "selection_id": terminal["selection_id"],
            "selection_digest": terminal["selection_digest"],
            "terminal_state_seq": terminal["terminal_state_seq"],
            "act": terminal["act"],
            "floor": terminal["floor"],
            "observed_max_act": observed_max_act,
            "hidden_entry": observed_max_act >= 4,
            "victory": terminal["victory"],
            "heart_defeated": terminal["heart_defeated"],
            "keys": dict(terminal["keys"]),
            "actions": terminal["actions"],
            "audit_status": audit["audit_status"],
            "release_gate_passed": audit["release_gate_passed"],
            "issue_count": audit["issue_count"],
            "review_finding_count": audit["review_finding_count"],
            "oracle_disagreement_count": audit[
                "oracle_disagreement_count"
            ],
            "eligible_unknown": audit["eligible_unknown"],
            "model_conflict_count": campaign_selector.audit_model_conflict_count(
                audit
            ),
            "controller_exit_code": controller_exit["exit_code"],
            "controller_stderr_size": controller_exit["stderr_size"],
            "controller_stdout_size": controller_exit["stdout_size"],
            "controller_stdout_sha256": controller_exit["stdout_sha256"],
            "controller_stderr_sha256": controller_exit["stderr_sha256"],
            "controller_stdout_line_count": controller_exit[
                "stdout_line_count"
            ],
            "controller_stdout_semantic_sha256": controller_exit[
                "stdout_semantic_sha256"
            ],
            "freeze_manifest_sha256": controller_exit[
                "freeze_manifest_sha256"
            ],
            "freeze_source_digest": controller_exit[
                "freeze_source_digest"
            ],
            "freeze_generated_at": controller_exit[
                "freeze_generated_at"
            ],
            "controller_exit_clean": True,
            # A valid streak is reset on a decision/controller hash change and
            # explicitly rejects a true code_changed marker.
            "code_changed": False,
            "started_at": _attempt_started_at(terminal),
            "ended_at": _attempt_ended_at(terminal, audit),
        })

    attempt_count = len(attempts)
    hidden_entry_count = sum(item["hidden_entry"] for item in attempts)
    performance_attempts = [
        {
            "attempt_id": terminal["attempt_id"],
            "decision_hash": terminal["decision_hash"],
            "controller_hash": terminal["controller_hash"],
            "character": terminal["character"],
            "floor": terminal["floor"],
            "observed_max_act": terminal["observed_max_act"],
            "hidden_entry": terminal["observed_max_act"] >= 4,
            "heart_defeated": terminal["heart_defeated"],
        }
        for terminal, _, _ in performance_triples
    ]
    controller_hash = attempts[0]["controller_hash"] if attempts else None
    current_batch_count = attempt_count % BATCH_SIZE
    if attempt_count and current_batch_count == 0:
        current_batch_count = BATCH_SIZE
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": campaign_selector.POLICY_VERSION,
        "decision_hash": decision_hash,
        "performance_hash": performance_hash,
        "controller_hash": controller_hash,
        "generated_at": time.time() if generated_at is None else generated_at,
        "cohort_started_at": (
            attempts[0]["started_at"] if attempts else None
        ),
        "cohort_ended_at": attempts[-1]["ended_at"] if attempts else None,
        "cohort_attempt_count": attempt_count,
        "valid_attempt_ids": [item["attempt_id"] for item in attempts],
        "hidden_entry_count": hidden_entry_count,
        "performance_cohort_attempt_count": len(performance_attempts),
        "performance_valid_attempt_ids": [
            item["attempt_id"] for item in performance_attempts
        ],
        "performance_hidden_entry_count": sum(
            item["hidden_entry"] for item in performance_attempts
        ),
        "performance_heart_win_count": sum(
            item["heart_defeated"] for item in performance_attempts
        ),
        "performance_attempts": performance_attempts,
        "batch_size": BATCH_SIZE,
        "completed_batches": attempt_count // BATCH_SIZE,
        "current_batch_count": current_batch_count,
        "code_changed": False,
        "attempts": attempts,
    }


def write_report(path, report):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, default=Path("run-history.jsonl"))
    parser.add_argument("--decision-hash", required=True)
    parser.add_argument("--performance-hash")
    parser.add_argument("--write", type=Path)
    args = parser.parse_args()
    try:
        report = build_cohort_report(
            campaign_selector.load_history(args.history),
            args.decision_hash,
            performance_hash=args.performance_hash,
        )
    except (campaign_selector.HistoryValidationError, ValueError) as exc:
        parser.error(str(exc))
    if args.write is not None:
        write_report(args.write, report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
