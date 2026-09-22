import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import autoplay
import autoplay_runner
import campaign_attempt
import campaign_selector
import cohort_review
import death_replay
import freeze_manifest
import independent_oracle
import pre_run_binding
import strategy_audit
from test_freeze_manifest import (
    bind_bridge_runtime_sources,
    install_replay_bundle,
)


EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def replay_evidence(decision_hash="decision-a"):
    return {
        "schema_version": 1,
        "history_coverage_version": 2,
        "target_decision_hash": decision_hash,
        "status": "clear",
        "release_gate_passed": True,
        "issue_count": 0,
        "eligible_unknown_count": 0,
        "source_case_count": 1,
        "current_case_count": 1,
        "historical_case_count": 0,
        "audited_case_count": 1,
        "classified_not_applicable_count": 0,
        "resolved_case_count": 0,
        "unresolved_case_count": 0,
        "issue_case_count": 0,
        "historical_audited_count": 0,
        "historical_classified_not_applicable_count": 0,
        "historical_resolved_count": 0,
        "historical_unresolved_count": 0,
        "historical_issue_case_count": 0,
        "resolution_evidence": {
            "authority_version": None,
            "target_decision_hash": decision_hash,
            "status": "not_applicable",
            "resolved_count": 0,
            "resolution_failure_count": 0,
            "failures": [],
        },
        "not_applicable_classification_counts": {},
        "historical_not_applicable_classification_counts": {},
        "not_applicable_check_counts": {},
        "historical_not_applicable_check_counts": {},
        "fixture_case_count": 1,
        "fixture_audited_count": 1,
        "missing_fixture_phases": [],
        "case_results": [{
            "historical": False,
            "classification": "audited",
            "authority": "canonical test authority",
            "reason": "independent semantic replay",
            "not_applicable_checks": [],
            "issues": [],
            "unknowns": [],
        }],
        "fixture_results": [{
            "classification": "audited",
            "authority": "canonical fixture authority",
            "reason": "independent fixture replay",
            "issues": [],
            "unknowns": [],
        }],
    }


def verification():
    process = {
        "returncode": 0,
        "stdout_size": 0,
        "stderr_size": 0,
        "stdout_sha256": EMPTY_HASH,
        "stderr_sha256": EMPTY_HASH,
    }
    return {
        "full_tests": {**process, "test_count": 1},
        "git_diff_check": {**process, "test_count": None},
        "runtime_artifacts_unchanged": True,
        "runtime_artifacts_before_sha256": "e" * 64,
        "runtime_artifacts_after_sha256": "e" * 64,
}


def install_runtime_provenance(root, *, stderr_prefix=b"old-prefix"):
    root = Path(root)
    if not (root / "bridge.py").exists():
        (root / "bridge.py").write_text("BRIDGE = 2\n", encoding="utf-8")
    if not (root / "launch_game.py").exists():
        (root / "launch_game.py").write_text(
            "LAUNCHER = 2\n", encoding="utf-8"
        )
    jar = b"communication-mod"
    (root / "CommunicationMod.jar").write_bytes(jar)
    game = root / "runtime-game"
    (game / "mods").mkdir(parents=True, exist_ok=True)
    (game / "jre" / "bin").mkdir(parents=True, exist_ok=True)
    (game / "mods" / "CommunicationMod.jar").write_bytes(jar)
    bridge_stderr = game / "communication_mod_errors.log"
    if not bridge_stderr.exists():
        bridge_stderr.write_bytes(stderr_prefix)
    launch_log = root / "logs" / "launches" / "launch-test.log"
    launch_log.parent.mkdir(parents=True, exist_ok=True)
    if not launch_log.exists():
        launch_log.write_bytes(b"")
    jar_hash = hashlib.sha256(jar).hexdigest()
    launch = {
        "schema_version": 2,
        "launch_id": "launch-test",
        "started_at": 10.0,
        "launch_log_path": "logs/launches/launch-test.log",
        "launch_start_record_path": (
            "logs/launches/launch-test.start.json"
        ),
        "launcher_pid": 110,
        "java_pid": 111,
        "command": [
            str((game / "jre" / "bin" / "java.exe").resolve()),
            "-jar",
            str((game / "ModTheSpire.jar").resolve()),
            "--skip-intro",
            "--mods",
            "basemod,CommunicationMod",
        ],
        "game_dir": str(game.resolve()),
        "root_communication_mod_jar_sha256": jar_hash,
        "installed_communication_mod_jar_sha256": jar_hash,
        "bridge_stderr_path": str(bridge_stderr.resolve()),
        "bridge_stderr_start_offset": len(stderr_prefix),
        "bridge_stderr_start_sha256": hashlib.sha256(
            stderr_prefix
        ).hexdigest(),
        "restart_predecessor_record_path": None,
        "restart_predecessor_record_sha256": None,
    }
    (root / launch["launch_start_record_path"]).write_text(
        json.dumps(launch), encoding="utf-8"
    )
    (root / "launch-latest.json").write_text(
        json.dumps(launch), encoding="utf-8"
    )
    (root / "bridge-instance.json").write_text(json.dumps({
        "schema_version": 2,
        "protocol_version": 2,
        "instance_token": "bridge-test",
        "bridge_pid": 222,
        "parent_java_pid": 111,
        "launch_id": "launch-test",
        "bridge_sha256": hashlib.sha256(
            (root / "bridge.py").read_bytes()
        ).hexdigest(),
        "started_at": 11.0,
    }), encoding="utf-8")
    return bridge_stderr


def install_freeze(root):
    install_runtime_provenance(root)
    source = root / "policy.py"
    if not source.exists():
        source.write_text("VALUE = 1\n", encoding="utf-8")
    replay_report = install_replay_bundle(root)
    bind_bridge_runtime_sources(root)
    with patch.object(
        freeze_manifest,
        "_fresh_policy_hashes",
        return_value={
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
        },
    ):
        manifest = freeze_manifest.build_manifest(
            root,
            "decision-a",
            "controller-a",
            verification=verification(),
            decision_case_replay=replay_report,
            generated_at=10.0,
        )
    freeze_manifest.write_manifest(root / "freeze-manifest.json", manifest)


def terminal(index=1, *, hidden=False, heart=False):
    character = "DEFECT"
    selection_id = f"selection-{index}"
    value = {
        "schema_version": 2,
        "policy_version": "fast-policy-v5",
        "attempt_id": f"attempt-{index}",
        "run_id": f"{character}:0:{index}",
        "seed": index,
        "goal_mode": "HEART",
        "character": character,
        "class": character,
        "ascension_level": 0,
        "run_type": "standard",
        "decision_hash": "decision-a",
        "controller_hash": "controller-a",
        "selection_id": selection_id,
        "state_seq": 1000 + index,
        "terminal_state_seq": 1000 + index,
        "termination_kind": "game_over",
        "authoritative_game_over": True,
        "screen_type": "GAME_OVER",
        "victory": heart,
        "heart_defeated": heart,
        "act": 4 if hidden else 3,
        "floor": 55 if hidden else 40,
        "observed_max_act": 4 if hidden else 3,
        "actions": 300,
        "current_hp": 0 if not heart else 12,
        "max_hp": 80,
        "deck": [{"id": "Strike_R"}],
        "relics": [{"id": "Burning Blood"}],
        "potions": [],
        "keys": {"ruby": True, "emerald": True, "sapphire": True},
        "selection": {
            "selection_id": selection_id,
            "algorithm": "beta-thompson-v1",
            "created_at": 100.0 + index,
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
            "goal_mode": "HEART",
            "policy_version": "fast-policy-v5",
            "ascension_level": 0,
            "run_type": "standard",
            "character": character,
        },
    }
    value["selection_digest"] = campaign_selector.selection_digest(
        value["selection"]
    )
    return value


def authoritative_combat_state(
    binding, item, state_seq, hp, monsters, *, terminal_state=False
):
    game = {
        "seed": item["seed"],
        "class": item["character"],
        "ascension_level": item["ascension_level"],
        "act": item["act"],
        "floor": item["floor"],
        "current_hp": hp,
        "max_hp": 80,
        "block": 0,
        "gold": 99,
        "room_phase": "COMPLETE" if terminal_state else "COMBAT",
        "screen_type": "GAME_OVER" if terminal_state else "NONE",
        "deck": [{"id": "Strike_R"}],
        "relics": [{"id": "Burning Blood"}],
        "potions": [],
        "keys": dict(item["keys"]),
        "has_ruby_key": item["keys"]["ruby"],
        "has_emerald_key": item["keys"]["emerald"],
        "has_sapphire_key": item["keys"]["sapphire"],
        "combat_state": {
            "player": {
                "current_hp": hp,
                "max_hp": 80,
                "block": 0,
                "energy": 0,
                "powers": [],
                "orbs": [],
            },
            "monsters": monsters,
        },
    }
    if terminal_state:
        game.update({
            "screen_state": {"victory": item["victory"]},
            "run_victory": item["victory"],
            "heart_defeated": item["heart_defeated"],
        })
    return {
        **{
            key: value for key, value in binding.items()
            if key != "schema_version"
        },
        "protocol_version": 2,
        "state_seq": state_seq,
        "phase": "GAME_OVER" if terminal_state else "COMBAT",
        "game_state": game,
    }


def complete_combat_record(binding, item):
    heart = item["heart_defeated"] is True
    if heart:
        monsters = [{
            "enemy_instance_id": "enemy:heart",
            "id": "CorruptHeart",
            "current_hp": 1,
            "block": 0,
            "intent": "ATTACK",
            "move_adjusted_damage": 1,
            "move_hits": 1,
            "powers": [
                {"id": "BeatOfDeathPower", "amount": 1},
                {"id": "InvinciblePower", "amount": 300},
            ],
            "is_gone": False,
            "half_dead": False,
        }]
        hp_before, hp_after = 13, 12
        hand = [{
            "card_instance_id": "card:strike",
            "id": "Strike_R",
            "type": "ATTACK",
            "damage": 1,
            "block": 0,
            "cost": 1,
            "is_playable": True,
            "has_target": True,
        }]
        legal = [
            {"choice_id": "action:end", "action": "end", "legal": True},
            {
                "choice_id": "play:card:strike:enemy:heart",
                "action": "play",
                "legal": True,
            },
        ]
        candidates = [
            {"choice_id": "action:end", "selected": False},
            {
                "choice_id": "play:card:strike:enemy:heart",
                "selected": True,
            },
        ]
        action = "play"
        selected = "play:card:strike:enemy:heart"
        damage_model = {
            "deterministic": True,
            "hero_to_monsters_predicted": 1,
            "hero_to_monsters_actual": 1,
        }
        after_monsters = []
    else:
        monster_id = "SpireShield" if item["hidden_entry"] else "Cultist"
        monsters = [{
            "enemy_instance_id": "enemy:fatal",
            "id": monster_id,
            "current_hp": 20,
            "block": 0,
            "intent": "ATTACK",
            "move_adjusted_damage": 5,
            "move_hits": 1,
            "powers": [],
            "is_gone": False,
            "half_dead": False,
        }]
        hp_before, hp_after = 5, 0
        hand = []
        legal = [
            {"choice_id": "action:end", "action": "end", "legal": True},
        ]
        candidates = [{"choice_id": "action:end", "selected": True}]
        action = "end"
        selected = "action:end"
        damage_model = {
            "deterministic": True,
            "monsters_to_hero_predicted": 5,
            "monsters_to_hero_actual": 5,
        }
        after_monsters = monsters

    before_seq = item["terminal_state_seq"] - 1
    after_seq = item["terminal_state_seq"]
    before_state = authoritative_combat_state(
        binding, item, before_seq, hp_before, monsters
    )
    before_state["game_state"]["combat_state"]["player"]["energy"] = (
        1 if heart else 0
    )
    after_state = authoritative_combat_state(
        binding,
        item,
        after_seq,
        hp_after,
        after_monsters,
        terminal_state=True,
    )
    record = {
        **binding,
        "record_type": "decision",
        "combat_id": f"combat:{item['attempt_id']}",
        "turn": 1,
        "before_seq": before_seq,
        "after_seq": after_seq,
        "phase": "COMBAT",
        "act": item["act"],
        "floor": item["floor"],
        "action": action,
        "resolved_target_id": selected,
        "hp_after": hp_after,
        "player_before": {
            "current_hp": hp_before,
            "max_hp": 80,
            "block": 0,
            "energy": 1 if heart else 0,
            "powers": [],
            "orbs": [],
        },
        "monsters_before": monsters,
        "hand_before": hand,
        "draw_pile_before": [],
        "discard_pile_before": [],
        "exhaust_pile_before": [],
        "potions_before": [],
        "relics_before": [{"id": "Burning Blood", "counter": 0}],
        "legal_actions_before": legal,
        "decision": {
            "decision_type": "COMBAT",
            "reason": "complete bound test combat",
            "candidates": candidates,
            "model_advice": {"status": "skipped"},
        },
        "damage_model": damage_model,
        "decision_outcome": {
            "player_hp_loss": hp_before - hp_after,
            "hp_delta": hp_after - hp_before,
        },
        "authoritative_state_before": before_state,
        "authoritative_state_after": after_state,
    }
    if heart:
        record.update({
            "card_instance_id": "card:strike",
            "enemy_instance_id": "enemy:heart",
            "true_combat_end": True,
        })
    return record


def replay(result):
    binding = {
        field: result[field]
        for field in autoplay_runner.BINDING_FIELDS
        if field != "schema_version"
    }
    death = result["victory"] is False and result["current_hp"] <= 0
    heart = result["heart_defeated"] is True
    if death or heart:
        return {
            **binding,
            "schema_version": 1,
            "replay_kind": "death" if death else "act4_terminal",
            "status": "clear",
            "issue_count": 0,
            "eligible_unknown_count": 0,
            "death_observed": death,
            "issues": [],
            "unknowns": [],
            "turns": [{"turn": 1, "complete": True, "actions": [{}]}],
            "counterfactuals": [],
        }
    return {
        **binding,
        "schema_version": 1,
        "replay_kind": "not_applicable",
        "status": "not_applicable",
        "issue_count": 0,
        "eligible_unknown_count": 0,
        "death_observed": False,
        "issues": [],
        "unknowns": [],
        "turns": [],
        "counterfactuals": [],
    }


def clear_oracle(result):
    coverage = {
        key: {
            "eligible": 0,
            "evaluated": 0,
            "unknown": 0,
            "issues": 0,
            "status": "not_applicable",
        }
        for key in cohort_review.ORACLE_REQUIRED_COVERAGE_KEYS
    }
    coverage["protocol_binding"].update({
        "eligible": 1,
        "evaluated": 1,
        "status": "clear",
    })
    return {
        "oracle_version": "independent-oracle-v2",
        "coverage_contract_version": 2,
        "binding_fields": [
            "attempt_id", "run_id", "seed", "character",
            "ascension_level", "decision_hash", "controller_hash",
            "policy_version", "selection_id", "terminal_state_seq",
        ],
        "required_coverage_keys": sorted(
            cohort_review.ORACLE_REQUIRED_COVERAGE_KEYS
        ),
        "decision_hash": result["decision_hash"],
        "attempt_id": result["attempt_id"],
        "status": "clear",
        "issue_count": 0,
        "eligible_unknown_count": 0,
        "disagreement_count": 0,
        "coverage": coverage,
        "issues": [],
        "unknowns": [],
        "blind_reviews": [],
    }


def audit(result):
    bound_replay = replay(result)
    death_observed = (
        result["victory"] is False and result["current_hp"] <= 0
    )
    return {
        "schema_version": 2,
        "audit_engine_sha256": strategy_audit.audit_engine_sha256(),
        "policy_version": result["policy_version"],
        "attempt_id": result["attempt_id"],
        "run_id": result["run_id"],
        "seed": result["seed"],
        "character": result["character"],
        "ascension_level": result["ascension_level"],
        "run_type": result["run_type"],
        "decision_hash": result["decision_hash"],
        "controller_hash": result["controller_hash"],
        "selection_id": result["selection_id"],
        "selection_digest": result["selection_digest"],
        "terminal_state_seq": result["terminal_state_seq"],
        "termination_kind": "game_over",
        "audit_status": "clear",
        "release_gate_passed": True,
        "protocol_correctness": {"status": "clear"},
        "mechanics_coverage": {"status": "clear"},
        "strategy_quality": {"status": "clear"},
        "issue_count": 0,
        "review_finding_count": 0,
        "oracle_disagreement_count": 0,
        "eligible_unknown": 0,
        "eligible_unknown_count": 0,
        "model_conflict_count": 0,
        "code_changed": False,
        "independent_oracle": clear_oracle(result),
        "death_observed": death_observed,
        "death_replay": bound_replay,
    }


def state(result):
    return {
        "protocol_version": 2,
        "in_game": True,
        "ready_for_command": True,
        "phase": "GAME_OVER",
        "decision_id": f"terminal-{result['attempt_id']}",
        "legal_actions": ["proceed", "state"],
        "policy_version": result["policy_version"],
        "state_seq": result["terminal_state_seq"],
        "attempt_id": result["attempt_id"],
        "run_id": result["run_id"],
        "seed": result["seed"],
        "character": result["character"],
        "ascension_level": result["ascension_level"],
        "run_type": result["run_type"],
        "decision_hash": result["decision_hash"],
        "controller_hash": result["controller_hash"],
        "selection_id": result["selection_id"],
        "selection_digest": result["selection_digest"],
        "terminal_state_seq": result["terminal_state_seq"],
        "game_state": {
            "screen_type": "GAME_OVER",
            "screen_state": {"victory": result["victory"]},
            "class": result["character"],
            "ascension_level": result["ascension_level"],
            "seed": result["seed"],
            "is_standard_run": True,
            "run_victory": result["victory"],
            "heart_defeated": result["heart_defeated"],
            "act": result["act"],
            "floor": result["floor"],
            "current_hp": result["current_hp"],
            "max_hp": result["max_hp"],
            "deck": result["deck"],
            "relics": result["relics"],
            "potions": result["potions"],
            "has_ruby_key": result["keys"]["ruby"],
            "has_emerald_key": result["keys"]["emerald"],
            "has_sapphire_key": result["keys"]["sapphire"],
        },
    }


def context(result):
    return {
        "schema_version": 2,
        "policy_version": result["policy_version"],
        "attempt_id": result["attempt_id"],
        "run_id": result["run_id"],
        "seed": result["seed"],
        "character": result["character"],
        "ascension_level": result["ascension_level"],
        "run_type": result["run_type"],
        "decision_hash": result["decision_hash"],
        "controller_hash": result["controller_hash"],
        "selection_id": result["selection_id"],
        "selection_digest": result["selection_digest"],
        "terminal_state_seq": result["terminal_state_seq"],
        "selection": result["selection"],
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def install_runner_start_acceptance(root, result):
    history = campaign_selector.load_history(root / "run-history.jsonl")
    eligible = campaign_selector.eligible_attempts(
        history, result["decision_hash"], result["controller_hash"]
    )
    selection = campaign_selector.select_character(
        history,
        result["decision_hash"],
        controller_hash=result["controller_hash"],
    )
    selection.update({
        "selection_id": result["selection_id"],
        "created_at": result["selection"]["created_at"],
    })
    campaign_selector.validate_selection(
        selection,
        result["decision_hash"],
        result["controller_hash"],
        eligible_attempt_ids=[item["attempt_id"] for item in eligible],
    )
    character = selection["character"]
    result.update({
        "run_id": f"{character}:0:{result['seed']}",
        "character": character,
        "class": character,
        "selection": selection,
    })
    result["selection_digest"] = campaign_selector.selection_digest(
        selection
    )
    write_json(root / "next-run-selection.json", selection)
    menu = {
        "protocol_version": 2,
        "in_game": False,
        "ready_for_command": True,
        "state_seq": 10,
        "phase": "MAIN_MENU",
        "decision_id": "menu-decision",
    }
    payload = {
        "id": "request-" + selection["selection_id"],
        "expected_seq": 10,
        "decision_id": "menu-decision",
        "phase": "MAIN_MENU",
        "selection_id": selection["selection_id"],
        "selection_digest": result["selection_digest"],
        "decision_hash": result["decision_hash"],
        "controller_hash": result["controller_hash"],
        "character": result["character"],
        "ascension_level": 0,
        "run_type": "standard",
        "target_id": f"run:{result['character']}:a0:standard",
    }
    record = pre_run_binding.build_start_acceptance_record(
        payload, menu, selection, created_at=10.0
    )
    path = pre_run_binding.start_acceptance_path(
        root, selection["selection_id"]
    )
    pre_run_binding._write_json_once_idempotent(path, record)
    return record


def land_child_artifacts(root, result, *, mutate=None, include_audit=True):
    bound_audit = audit(result)
    bound_state = state(result)
    bound_context = context(result)
    selection = {
        **result["selection"],
        **{
            field: result[field]
            for field in autoplay_runner.BINDING_FIELDS
        },
        "schema_version": 2,
        "policy_version": result["policy_version"],
    }
    binding = {
        field: result[field]
        for field in (
            "schema_version", "policy_version", "attempt_id", "run_id",
            "seed", "character", "ascension_level", "run_type",
            "decision_hash", "controller_hash", "selection_id",
            "selection_digest",
            "terminal_state_seq",
        )
    }
    fixture_item = {
        **result,
        "hidden_entry": result["observed_max_act"] >= 4,
    }
    artifacts = {
        "result": result,
        "audit": bound_audit,
        "state": bound_state,
        "context": bound_context,
        "selection": selection,
        "trace_records": [
            {
                **binding,
                "record_type": "controller_start",
                "state_seq": result["terminal_state_seq"] - 2,
            },
            complete_combat_record(binding, fixture_item),
            {**result, "record_type": "terminal_result"},
        ],
    }
    if mutate is not None:
        mutate(artifacts)
    trace_terminals = [
        record for record in artifacts["trace_records"]
        if isinstance(record, dict)
        and record.get("record_type") == "terminal_result"
    ]
    if len(trace_terminals) != 1:
        raise AssertionError(
            "runner fixture trace must contain exactly one terminal_result"
        )
    if "record_type" in artifacts["result"]:
        raise AssertionError(
            "run-result fixture must remain an artifact, not a trace record"
        )
    artifacts["audit"]["death_replay"] = death_replay.build_death_replay(
        artifacts["trace_records"], terminal=trace_terminals[0]
    )
    artifacts["audit"]["death_observed"] = artifacts["audit"][
        "death_replay"
    ]["death_observed"]
    artifacts["audit"]["independent_oracle"] = (
        independent_oracle.audit_records(
            artifacts["trace_records"],
            result["decision_hash"],
            result["attempt_id"],
            artifacts={
                "run_context": artifacts["context"],
                "run_result": artifacts["result"],
                "state": artifacts["state"],
                "selection": artifacts["selection"],
            },
        )
    )
    directory = autoplay_runner.attempt_directory(root, result["attempt_id"])
    write_json(root / "run-result.json", artifacts["result"])
    write_json(root / "run-context.json", artifacts["context"])
    write_json(root / "state.json", artifacts["state"])
    write_json(directory / "run-result.json", artifacts["result"])
    write_json(directory / "terminal-state.json", artifacts["state"])
    write_json(directory / "selection.json", artifacts["selection"])
    write_json(directory / "run-context.json", artifacts["context"])
    write_json(
        directory / "death-replay.json",
        artifacts["audit"]["death_replay"],
    )
    (directory / "autoplay.log").write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in artifacts["trace_records"]
        ),
        encoding="utf-8",
    )
    if include_audit:
        write_json(directory / "run-audit.json", artifacts["audit"])
    history = [
        {**result, "record_type": "terminal_result"},
    ]
    if include_audit:
        history.append({
            **{
                field: bound_audit.get(field)
                for field in cohort_review.HISTORY_AUDIT_FIELDS
            },
            "record_type": "run_audit",
        })
    with (root / "run-history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(item) + "\n" for item in history))


class AutoplayRunnerTests(unittest.TestCase):
    def run_operational_failure(
        self,
        root,
        result,
        *,
        mutate_embedded=None,
        mutate_live=None,
        mutate_preexisting_fallback=None,
    ):
        install_freeze(root)
        install_runner_start_acceptance(root, result)
        nonterminal_binding = {
            field: result[field]
            for field in autoplay_runner.BINDING_FIELDS
            if field != "terminal_state_seq"
        }
        live = {
            **nonterminal_binding,
            "protocol_version": 2,
            "in_game": True,
            "state_seq": result["terminal_state_seq"],
            "phase": "COMBAT_TURN_1",
            "decision_id": "decision-operational",
            "ready_for_command": True,
            "available_commands": ["play", "end", "state"],
            "game_state": {
                "class": result["character"],
                "ascension_level": result["ascension_level"],
                "seed": result["seed"],
                "act": 2,
                "floor": 27,
                "current_hp": 42,
                "max_hp": 80,
                "combat_state": {
                    "player": {
                        "current_hp": 42,
                        "max_hp": 80,
                        "block": 0,
                        "energy": 3,
                        "powers": [],
                        "orbs": [],
                    },
                    "monsters": [{
                        "enemy_instance_id": "enemy:snake-plant",
                        "id": "SnakePlant",
                        "current_hp": 77,
                        "max_hp": 77,
                    }],
                },
            },
        }
        embedded = autoplay.authoritative_state_snapshot(live)
        embedded.update({
            "protocol_version": live["protocol_version"],
            "in_game": live["in_game"],
            "terminal_state_seq": result["terminal_state_seq"],
        })
        for field in autoplay.ATTEMPT_BINDING_FIELDS:
            embedded[field] = nonterminal_binding[field]
        if mutate_embedded is not None:
            mutate_embedded(embedded)
        if mutate_live is not None:
            mutate_live(live)
        result.update({
            "termination_kind": "operational_error",
            "authoritative_game_over": False,
            "screen_type": "NONE",
            "victory": False,
            "heart_defeated": False,
            "error": "TypeError",
            "message": "tuple does not support item assignment",
            "last_authoritative_state": embedded,
        })
        if mutate_preexisting_fallback is not None:
            fallback = json.loads(json.dumps(embedded))
            mutate_preexisting_fallback(fallback)
            write_json(
                autoplay_runner.attempt_directory(
                    root, result["attempt_id"]
                ) / "last-authoritative-state.json",
                fallback,
            )

        def failed_child(*args, **kwargs):
            (root / "next-run-selection.json").unlink()
            write_json(root / "run-result.json", result)
            bound_context = context(result)
            # The game is still active; only the operational result and its
            # embedded snapshot own the failure-bound terminal marker.
            bound_context["terminal_state_seq"] = None
            write_json(root / "run-context.json", bound_context)
            write_json(root / "state.json", live)
            (root / "run-history.jsonl").write_text(
                json.dumps({**result, "record_type": "terminal_result"})
                + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                args[0], 1, stdout=b"", stderr=b"planner failed\n"
            )

        return autoplay_runner.run_controller(
            root,
            max_actions=5,
            python_executable="python-test",
            subprocess_run=failed_child,
        )

    def run_fake(
        self,
        root,
        result,
        *,
        returncode=0,
        stderr=b"",
        mutate=None,
        bridge_append=b"",
    ):
        if not (root / "freeze-manifest.json").exists():
            install_freeze(root)
        install_runner_start_acceptance(root, result)
        if (root / "run-result.json").exists():
            # Model a newly-started attempt: its current context no longer
            # matches the stale terminal result from the previous attempt.
            write_json(
                root / "run-context.json",
                {"attempt_id": result["attempt_id"]},
            )

        def fake_run(*args, **kwargs):
            land_child_artifacts(root, result, mutate=mutate)
            (root / "next-run-selection.json").unlink()
            if bridge_append:
                launch = json.loads(
                    (root / "launch-latest.json").read_text(
                        encoding="utf-8"
                    )
                )
                with Path(launch["bridge_stderr_path"]).open("ab") as handle:
                    handle.write(bridge_append)
            return subprocess.CompletedProcess(
                args[0],
                returncode,
                stdout=(json.dumps(result) + "\n").encode("utf-8"),
                stderr=stderr,
            )

        return autoplay_runner.run_controller(
            root,
            max_actions=5,
            python_executable="python-test",
            subprocess_run=fake_run,
        )

    def test_clean_child_lands_unique_bound_exit_and_becomes_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal()
            receipt = self.run_fake(root, result)

            self.assertEqual("clear", receipt["controller_exit_status"])
            self.assertEqual("fast-policy-v5", receipt["policy_version"])
            self.assertEqual(0, receipt["exit_code"])
            self.assertEqual(0, receipt["stderr_size"])
            self.assertEqual("clear", receipt[
                "bridge_stderr_evidence_status"
            ])
            self.assertEqual(0, receipt["bridge_stderr_delta_size"])
            self.assertEqual(
                receipt["bridge_stderr_start_size"],
                receipt["bridge_stderr_end_size"],
            )
            self.assertGreater(receipt["bridge_stderr_start_size"], 0)
            self.assertEqual(
                EMPTY_HASH, receipt["bridge_stderr_delta_sha256"]
            )
            self.assertEqual(64, len(receipt["freeze_manifest_sha256"]))
            self.assertEqual(64, len(receipt["freeze_source_digest"]))
            self.assertEqual(
                receipt["launch_id"], receipt["freeze_launch_id"]
            )
            self.assertEqual(110, receipt["freeze_launcher_pid"])
            self.assertEqual(111, receipt["freeze_java_pid"])
            self.assertEqual(222, receipt["freeze_bridge_pid"])
            self.assertEqual(
                receipt["launch_id"],
                receipt["freeze_bridge_launch_id"],
            )
            self.assertEqual(
                "bridge-test", receipt["freeze_bridge_instance_token"]
            )
            self.assertEqual(64, len(receipt[
                "freeze_bridge_instance_sha256"
            ]))
            self.assertEqual(64, len(receipt[
                "freeze_bridge_sha256"
            ]))
            self.assertEqual(64, len(receipt[
                "freeze_launch_record_sha256"
            ]))
            self.assertEqual(
                "logs/launches/launch-test.start.json",
                receipt["freeze_launch_start_record_path"],
            )
            self.assertEqual(
                receipt["freeze_launch_record_sha256"],
                receipt["freeze_launch_start_record_sha256"],
            )
            self.assertEqual(
                receipt["freeze_root_communication_mod_jar_sha256"],
                receipt["freeze_installed_communication_mod_jar_sha256"],
            )
            self.assertEqual(10.0, receipt["freeze_generated_at"])
            self.assertEqual(result["attempt_id"], receipt["attempt_id"])
            attempt_dir = autoplay_runner.attempt_directory(
                root, result["attempt_id"]
            )
            self.assertEqual(
                (json.dumps(result) + "\n").encode("utf-8"),
                (attempt_dir / "controller.stdout.log").read_bytes(),
            )
            self.assertEqual(
                b"", (attempt_dir / "controller.stderr.log").read_bytes()
            )
            self.assertEqual(
                receipt,
                json.loads(
                    (attempt_dir / "controller-exit.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )
            records = campaign_selector.load_history(
                root / "run-history.jsonl"
            )
            self.assertEqual(
                [result["attempt_id"]],
                [
                    item["attempt_id"]
                    for item in campaign_selector.eligible_attempts(
                        records, "decision-a", "controller-a"
                    )
                ],
            )
            cohort = json.loads(
                (root / "cohort-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [result["attempt_id"]], cohort["valid_attempt_ids"]
            )

    def test_final_child_gate_rejects_post_acceptance_mutation(self):
        for kind in ("history", "source", "sidecar"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install_freeze(root)
                result = terminal(401)
                install_runner_start_acceptance(root, result)
                if kind == "history":
                    orphan = {
                        "record_type": "run_audit",
                        "schema_version": 2,
                        "attempt_id": "attempt-after-acceptance",
                        "decision_hash": "decision-a",
                        "controller_hash": "controller-a",
                        "selection_id": "selection-orphan",
                        "selection_digest": "f" * 64,
                    }
                    (root / "run-history.jsonl").write_text(
                        json.dumps(orphan) + "\n", encoding="utf-8"
                    )
                elif kind == "source":
                    (root / "policy.py").write_text(
                        "VALUE = 999\n", encoding="utf-8"
                    )
                else:
                    path = pre_run_binding.start_acceptance_path(
                        root, result["selection_id"]
                    )
                    sidecar = json.loads(path.read_text(encoding="utf-8"))
                    sidecar["accepted_receipt"][
                        "selection_digest"
                    ] = "0" * 64
                    path.write_text(json.dumps(sidecar), encoding="utf-8")
                child = Mock(side_effect=AssertionError("child spawned"))
                with self.assertRaises(autoplay_runner.RunnerError):
                    autoplay_runner.run_controller(
                        root,
                        max_actions=5,
                        python_executable="python-test",
                        subprocess_run=child,
                    )
                child.assert_not_called()
                self.assertTrue((root / "next-run-selection.json").exists())

    def test_non_core_selection_tamper_forces_issues_and_zero_eligibility(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(306)

            def tamper(artifacts):
                artifacts["context"]["selection"]["reason"] = (
                    "tampered-after-start-acceptance"
                )

            receipt = self.run_fake(root, result, mutate=tamper)
            self.assertEqual("issues", receipt["controller_exit_status"])
            self.assertIn(
                "selection_digest",
                receipt["controller_exit_audit"]["reason"],
            )
            records = campaign_selector.load_history(
                root / "run-history.jsonl"
            )
            self.assertEqual(
                [],
                campaign_selector.eligible_attempts(
                    records, "decision-a", "controller-a"
                ),
            )

    def test_bridge_stderr_append_forces_p1_exit_but_old_prefix_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(307)
            receipt = self.run_fake(
                root,
                result,
                bridge_append=b"bridge traceback\n",
            )
            self.assertEqual("issues", receipt["controller_exit_status"])
            self.assertEqual("issues", receipt[
                "bridge_stderr_evidence_status"
            ])
            self.assertEqual(
                len(b"bridge traceback\n"),
                receipt["bridge_stderr_delta_size"],
            )
            self.assertEqual(
                hashlib.sha256(b"bridge traceback\n").hexdigest(),
                receipt["bridge_stderr_delta_sha256"],
            )
            self.assertEqual(
                "communication_mod_error_delta_nonempty",
                receipt["controller_exit_audit"]["reason"],
            )
            audit = json.loads((
                autoplay_runner.attempt_directory(root, result["attempt_id"])
                / "controller-exit-audit.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual("P1", audit["issues"][0]["severity"])
            self.assertEqual(
                receipt["bridge_stderr_delta_sha256"],
                audit["issues"][0]["bridge_stderr_delta_sha256"],
            )

    def test_nonzero_or_stderr_lands_p1_exit_audit_and_is_a_barrier(self):
        for returncode, stderr in ((3, b"boom"), (0, b"warning")):
            with self.subTest(returncode=returncode, stderr=stderr):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    result = terminal(returncode + len(stderr) + 10)
                    receipt = self.run_fake(
                        root,
                        result,
                        returncode=returncode,
                        stderr=stderr,
                    )
                    self.assertEqual("issues", receipt["controller_exit_status"])
                    self.assertEqual(
                        "P1", receipt["controller_exit_audit"]["severity"]
                    )
                    report = json.loads(
                        (
                            autoplay_runner.attempt_directory(
                                root, result["attempt_id"]
                            )
                            / "controller-exit-audit.json"
                        ).read_text(encoding="utf-8")
                    )
                    self.assertEqual("issues", report["audit_status"])
                    self.assertFalse(report["release_gate_passed"])
                    self.assertEqual(
                        "controller_exit_failure", report["issues"][0]["kind"]
                    )
                    self.assertEqual(
                        result["terminal_state_seq"],
                        report["last_authoritative_state"]["state_seq"],
                    )
                    records = campaign_selector.load_history(
                        root / "run-history.jsonl"
                    )
                    self.assertEqual(
                        [],
                        campaign_selector.eligible_attempts(
                            records, "decision-a"
                        ),
                    )
                    cohort = json.loads(
                        (root / "cohort-report.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual([], cohort["valid_attempt_ids"])

    def test_source_change_during_child_is_a_p1_not_a_clean_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(15)

            def mutate(_artifacts):
                (root / "policy.py").write_text(
                    "VALUE = 2\n", encoding="utf-8"
                )

            receipt = self.run_fake(root, result, mutate=mutate)
            self.assertEqual("issues", receipt["controller_exit_status"])
            self.assertIn(
                "post-run freeze validation failed",
                receipt["controller_exit_audit"]["reason"],
            )
            self.assertEqual(
                "P1", receipt["controller_exit_audit"]["severity"]
            )
            records = campaign_selector.load_history(
                root / "run-history.jsonl"
            )
            self.assertEqual(
                [],
                campaign_selector.eligible_attempts(records, "decision-a"),
            )
            exits = [
                item for item in records
                if item.get("record_type") == "controller_exit"
            ]
            self.assertEqual(1, len(exits))
            self.assertFalse(any(
                item.get("controller_exit_status") == "clear"
                for item in exits
            ))

    def test_tampered_freeze_source_digest_is_a_p1(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(151)

            def mutate(_artifacts):
                path = root / "freeze-manifest.json"
                manifest = json.loads(path.read_text(encoding="utf-8"))
                manifest["source_digest"] = "f" * 64
                path.write_text(json.dumps(manifest), encoding="utf-8")

            receipt = self.run_fake(root, result, mutate=mutate)
            self.assertEqual("issues", receipt["controller_exit_status"])
            self.assertIn(
                "source changed after the freeze checkpoint",
                receipt["controller_exit_audit"]["reason"],
            )

    def test_extra_or_mismatched_stdout_cannot_land_clean_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(16)
            install_freeze(root)
            install_runner_start_acceptance(root, result)

            def fake_run(*args, **kwargs):
                land_child_artifacts(root, result)
                (root / "next-run-selection.json").unlink()
                stdout = (
                    json.dumps(result) + "\n" + '{"unexpected":true}\n'
                ).encode("utf-8")
                return subprocess.CompletedProcess(
                    args[0], 0, stdout=stdout, stderr=b""
                )

            receipt = autoplay_runner.run_controller(
                root,
                max_actions=5,
                python_executable="python-test",
                subprocess_run=fake_run,
            )
            self.assertEqual("issues", receipt["controller_exit_status"])
            self.assertIn(
                "stdout is not exactly one terminal JSON line",
                receipt["controller_exit_audit"]["reason"],
            )

    def test_ascii_terminal_stdout_preserves_non_ascii_result(self):
        result = terminal(161)
        result["display_name"] = "遗物"
        stdout = json.dumps(result, ensure_ascii=True).encode("utf-8")
        evidence = autoplay_runner._terminal_stdout_evidence(
            stdout, result
        )
        self.assertEqual(1, evidence["stdout_line_count"])
        self.assertEqual(64, len(evidence["stdout_semantic_sha256"]))

    def test_sixth_clean_exit_automatically_lands_clear_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(1, 7):
                self.run_fake(root, terminal(index))
            review_path = root / "cohort-review.json"
            before = review_path.read_bytes()
            review = json.loads(before.decode("utf-8"))
            self.assertEqual("six_without_hidden", review["review_kind"])
            self.assertEqual("clear", review["review_status"])
            self.assertTrue(review["release_gate_passed"])
            self.assertEqual(6, len(review["attempts"]))
            self.assertTrue(all(
                set(item["source_digests"])
                == cohort_review.SOURCE_DIGEST_KEYS
                for item in review["attempts"]
            ))

            report = autoplay_runner._refresh_cohort(root, "decision-a")
            self.assertTrue(
                cohort_review.validate_review_gate(
                    review,
                    report,
                    root / "logs" / "attempts",
                )
            )
            self.assertEqual(before, review_path.read_bytes())

    def test_act4_review_issue_blocks_and_restart_does_not_rerun_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(18, hidden=True)

            def hide_act4_combat_evidence(artifacts):
                for record in artifacts["trace_records"]:
                    if record.get("combat_id"):
                        record["act"] = 3

            with self.assertRaisesRegex(
                autoplay_runner.RunnerError, "review is not clear"
            ):
                self.run_fake(
                    root, result, mutate=hide_act4_combat_evidence
                )
            review_path = root / "cohort-review.json"
            before = review_path.read_bytes()
            review = json.loads(before.decode("utf-8"))
            self.assertEqual("act4_without_heart", review["review_kind"])
            self.assertEqual("issues", review["review_status"])
            self.assertFalse(review["release_gate_passed"])

            calls = []

            def must_not_run(*args, **kwargs):
                calls.append((args, kwargs))
                raise AssertionError("completed controller was rerun")

            with self.assertRaisesRegex(
                autoplay_runner.RunnerError, "review is not clear"
            ):
                autoplay_runner.run_controller(
                    root,
                    max_actions=5,
                    python_executable="python-test",
                    subprocess_run=must_not_run,
                )
            self.assertEqual([], calls)
            self.assertEqual(before, review_path.read_bytes())

    def test_terminal_run_victory_mismatch_prevents_menu_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(180, hidden=True)

            def hide_act4_combat_evidence(artifacts):
                for record in artifacts["trace_records"]:
                    if record.get("combat_id"):
                        record["act"] = 3

            with self.assertRaisesRegex(
                autoplay_runner.RunnerError, "review is not clear"
            ):
                self.run_fake(
                    root, result, mutate=hide_act4_combat_evidence
                )
            terminal_state = state(result)
            terminal_state["game_state"]["run_victory"] = True
            send = Mock()
            with self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError,
                "terminal game_state run_victory mismatch",
            ):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: terminal_state,
                    send_payload_fn=send,
                )
            send.assert_not_called()

    def test_nonclear_act4_audit_gets_attempt_bound_blocking_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(181, hidden=True)

            def fail_audit(artifacts):
                artifacts["audit"].update({
                    "audit_status": "inconclusive",
                    "release_gate_passed": False,
                    "issue_count": 1,
                    "review_finding_count": 1,
                    "eligible_unknown": 1,
                    "eligible_unknown_count": 1,
                    "protocol_correctness": {"status": "inconclusive"},
                })

            receipt = self.run_fake(root, result, mutate=fail_audit)
            self.assertEqual("issues", receipt["controller_exit_status"])
            attempt_dir = autoplay_runner.attempt_directory(
                root, result["attempt_id"]
            )
            review = json.loads(
                (attempt_dir / "attempt-blocking-review.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                "act4_failure_noneligible", review["review_kind"]
            )
            self.assertFalse(review["release_gate_passed"])
            self.assertFalse(review["eligible_for_cohort"])
            self.assertEqual(result["attempt_id"], review["attempt_id"])
            self.assertEqual(
                result["terminal_state_seq"], review["terminal_state_seq"]
            )
            self.assertEqual("inconclusive", review["audit"]["status"])
            self.assertTrue(all(
                value is not None
                for value in review["artifact_sha256"].values()
            ))
            cohort = json.loads(
                (root / "cohort-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual([], cohort["valid_attempt_ids"])

    def test_heart_candidate_cannot_skip_automatic_final_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(19, hidden=True, heart=True)
            receipt = self.run_fake(root, result)
            self.assertEqual("clear", receipt["controller_exit_status"])
            review = json.loads(
                (root / "cohort-review.json").read_text(encoding="utf-8")
            )
            self.assertEqual("heart_victory_final", review["review_kind"])
            self.assertEqual("clear", review["review_status"])
            self.assertTrue(review["release_gate_passed"])
            self.assertEqual([], review["findings"])
            self.assertEqual(
                "CorruptHeart",
                review["attempts"][0]["trace_summary"][
                    "act4_combats"
                ][0]["monsters"][0],
            )

    def test_wrong_artifact_binding_lands_issues_not_a_clean_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(20)

            def mutate(artifacts):
                artifacts["selection"]["controller_hash"] = "wrong"

            receipt = self.run_fake(root, result, mutate=mutate)
            self.assertEqual("issues", receipt["controller_exit_status"])
            self.assertIn(
                "controller_exit_evidence_invalid",
                receipt["controller_exit_audit"]["reason"],
            )
            self.assertEqual(
                [],
                campaign_selector.eligible_attempts(
                    campaign_selector.load_history(root / "run-history.jsonl"),
                    "decision-a",
                ),
            )

    def test_duplicate_exit_is_refused_without_appending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = self.run_fake(root, terminal(30))
            with self.assertRaisesRegex(
                autoplay_runner.RunnerError, "already exists"
            ):
                autoplay_runner.append_controller_exit(root, receipt)
            exits = [
                item for item in campaign_selector.load_history(
                    root / "run-history.jsonl"
                )
                if item.get("record_type") == "controller_exit"
            ]
            self.assertEqual(1, len(exits))

    def test_runner_restart_reconciles_clean_receipt_without_child_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.run_fake(root, terminal(301))
            history_path = root / "run-history.jsonl"
            before = history_path.read_bytes()
            calls = []

            def must_not_run(*args, **kwargs):
                calls.append((args, kwargs))
                raise AssertionError("completed controller was rerun")

            reconciled = autoplay_runner.run_controller(
                root,
                max_actions=5,
                python_executable="python-test",
                subprocess_run=must_not_run,
            )
            self.assertEqual(first, reconciled)
            self.assertEqual([], calls)
            self.assertEqual(before, history_path.read_bytes())

    def test_runner_restart_completes_orphan_exit_sidecar_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(306)
            with patch.object(
                autoplay_runner,
                "_append_history",
                side_effect=OSError("injected history append failure"),
            ):
                with self.assertRaisesRegex(
                    OSError, "injected history append failure"
                ):
                    self.run_fake(root, result)

            attempt_dir = autoplay_runner.attempt_directory(
                root, result["attempt_id"]
            )
            self.assertTrue((attempt_dir / "controller-exit.json").exists())
            self.assertEqual(
                0,
                len([
                    item for item in campaign_selector.load_history(
                        root / "run-history.jsonl"
                    )
                    if item.get("record_type") == "controller_exit"
                ]),
            )

            child = Mock(side_effect=AssertionError("child was rerun"))
            recovered = autoplay_runner.run_controller(
                root,
                max_actions=5,
                python_executable="python-test",
                subprocess_run=child,
            )

            child.assert_not_called()
            self.assertEqual("clear", recovered["controller_exit_status"])
            exits = [
                item for item in campaign_selector.load_history(
                    root / "run-history.jsonl"
                )
                if item.get("record_type") == "controller_exit"
            ]
            self.assertEqual([recovered], exits)

    def test_pending_new_selection_prevents_reusing_previous_exit(self):
        """Model the exact START -> runner order across two attempts."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.run_fake(root, terminal(302))
            second = terminal(303)
            pending = root / "next-run-selection.json"
            install_runner_start_acceptance(root, second)
            calls = []

            def run_second(*args, **kwargs):
                calls.append(args[0])
                self.assertTrue(pending.exists())
                # load_or_create_context writes the new context first and then
                # consumes the exact selection.
                pending.unlink()
                land_child_artifacts(root, second)
                return subprocess.CompletedProcess(
                    args[0],
                    0,
                    stdout=(json.dumps(second) + "\n").encode("utf-8"),
                    stderr=b"",
                )

            observed = autoplay_runner.run_controller(
                root,
                max_actions=5,
                python_executable="python-test",
                subprocess_run=run_second,
            )
            self.assertEqual(1, len(calls))
            self.assertNotEqual(first["attempt_id"], observed["attempt_id"])
            self.assertEqual(second["attempt_id"], observed["attempt_id"])
            self.assertFalse(pending.exists())
            self.assertEqual(
                [first["attempt_id"], second["attempt_id"]],
                [
                    item["attempt_id"]
                    for item in campaign_selector.eligible_attempts(
                        campaign_selector.load_history(
                            root / "run-history.jsonl"
                        ),
                        "decision-a",
                        "controller-a",
                    )
                ],
            )

    def test_operational_error_uses_exact_embedded_state_when_root_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            result = terminal(304)
            install_runner_start_acceptance(root, result)
            live = {
                **{
                    field: result[field]
                    for field in autoplay_runner.BINDING_FIELDS
                    if field != "terminal_state_seq"
                },
                "protocol_version": 2,
                "in_game": True,
                "state_seq": result["terminal_state_seq"],
                "phase": "EVENT",
                "decision_id": "event-304",
                "ready_for_command": True,
                "available_commands": ["choose", "state"],
                "game_state": {
                    "class": result["character"],
                    "ascension_level": result["ascension_level"],
                    "seed": result["seed"],
                    "room_phase": "COMPLETE",
                    "screen_type": "EVENT",
                },
            }
            embedded = autoplay.authoritative_state_snapshot(live)
            embedded.update({
                "protocol_version": 2,
                "in_game": True,
                "terminal_state_seq": result["terminal_state_seq"],
            })
            for field in autoplay.ATTEMPT_BINDING_FIELDS:
                embedded[field] = result[field]
            result.update({
                "termination_kind": "operational_error",
                "authoritative_game_over": False,
                "screen_type": "EVENT",
                "victory": False,
                "heart_defeated": False,
                "last_authoritative_state": embedded,
            })

            def failed_child(*args, **kwargs):
                (root / "next-run-selection.json").unlink()
                write_json(root / "run-result.json", result)
                write_json(root / "run-context.json", context(result))
                (root / "state.json").write_text("{", encoding="utf-8")
                (root / "run-history.jsonl").write_text(
                    json.dumps({**result, "record_type": "terminal_result"})
                    + "\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    args[0], 1, stdout=b"", stderr=b"bridge failed\n"
                )

            receipt = autoplay_runner.run_controller(
                root,
                max_actions=5,
                python_executable="python-test",
                subprocess_run=failed_child,
            )
            self.assertEqual("issues", receipt["controller_exit_status"])
            fallback = json.loads(
                (
                    autoplay_runner.attempt_directory(
                        root, result["attempt_id"]
                    )
                    / "last-authoritative-state.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                result["last_authoritative_state"], fallback
            )
            self.assertEqual(
                result["terminal_state_seq"],
                receipt["terminal_state_seq"],
            )

    def test_operational_error_live_frame_without_terminal_marker_lands_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(306)

            receipt = self.run_operational_failure(root, result)

            self.assertEqual("issues", receipt["controller_exit_status"])
            self.assertEqual(result["attempt_id"], receipt["attempt_id"])
            attempt_dir = autoplay_runner.attempt_directory(
                root, result["attempt_id"]
            )
            self.assertEqual(
                result["last_authoritative_state"],
                json.loads(
                    (attempt_dir / "last-authoritative-state.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )
            sidecar = json.loads(
                (attempt_dir / "controller-exit.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt, sidecar)
            matching_exits = [
                item
                for item in campaign_selector.load_history(
                    root / "run-history.jsonl"
                )
                if item.get("attempt_id") == result["attempt_id"]
                and item.get("record_type") == "controller_exit"
            ]
            self.assertEqual([receipt], matching_exits)

    def test_operational_error_rejects_tampered_embedded_binding_and_sequence(self):
        mutations = {
            "attempt": lambda state: state.__setitem__(
                "attempt_id", "attempt-forged"
            ),
            "run": lambda state: state.__setitem__(
                "run_id", "DEFECT:0:999999"
            ),
            "decision_hash": lambda state: state.__setitem__(
                "decision_hash", "decision-forged"
            ),
            "controller_hash": lambda state: state.__setitem__(
                "controller_hash", "controller-forged"
            ),
            "selection": lambda state: state.__setitem__(
                "selection_id", "selection-forged"
            ),
            "state_seq": lambda state: state.__setitem__(
                "state_seq", state["state_seq"] + 1
            ),
            "terminal_state_seq": lambda state: state.__setitem__(
                "terminal_state_seq", state["terminal_state_seq"] + 1
            ),
        }
        for index, (label, mutation) in enumerate(
            mutations.items(), start=307
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(
                    autoplay_runner.RunnerError,
                    "controller failure could not be uniquely bound",
                ):
                    self.run_operational_failure(
                        Path(directory), terminal(index),
                        mutate_embedded=mutation,
                    )

    def test_operational_error_rejects_live_state_disagreement(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                autoplay_runner.RunnerError,
                "controller failure could not be uniquely bound",
            ):
                self.run_operational_failure(
                    Path(directory),
                    terminal(312),
                    mutate_live=lambda state: state["game_state"].__setitem__(
                        "current_hp", 41
                    ),
                )

    def test_operational_error_rejects_truncated_embedded_projection(self):
        mutations = {
            "phase": lambda state: state.pop("phase"),
            "decision_id": lambda state: state.pop("decision_id"),
            "ready_for_command": lambda state: state.pop(
                "ready_for_command"
            ),
            "available_commands": lambda state: state.pop(
                "available_commands"
            ),
        }
        for index, (label, mutation) in enumerate(
            mutations.items(), start=313
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(
                    autoplay_runner.RunnerError,
                    "controller failure could not be uniquely bound",
                ):
                    self.run_operational_failure(
                        Path(directory), terminal(index),
                        mutate_embedded=mutation,
                    )

    def test_operational_error_rejects_missing_embedded_nested_facts(self):
        mutations = {
            "game_state": lambda state: state.pop("game_state"),
            "player_energy": lambda state: state["game_state"][
                "combat_state"
            ]["player"].pop("energy"),
            "monster_hp": lambda state: state["game_state"][
                "combat_state"
            ]["monsters"][0].pop("current_hp"),
        }
        for index, (label, mutation) in enumerate(
            mutations.items(), start=317
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(
                    autoplay_runner.RunnerError,
                    "controller failure could not be uniquely bound",
                ):
                    self.run_operational_failure(
                        Path(directory), terminal(index),
                        mutate_embedded=mutation,
                    )

    def test_operational_error_rejects_live_other_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                autoplay_runner.RunnerError,
                "controller failure could not be uniquely bound",
            ):
                self.run_operational_failure(
                    Path(directory), terminal(320),
                    mutate_live=lambda state: state.__setitem__(
                        "attempt_id", "attempt-other"
                    ),
                )

    def test_operational_error_rejects_preexisting_fallback_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                autoplay_runner.RunnerError,
                "controller failure could not be uniquely bound",
            ):
                self.run_operational_failure(
                    Path(directory), terminal(321),
                    mutate_preexisting_fallback=lambda state: state[
                        "game_state"
                    ].__setitem__("current_hp", 41),
                )

    def test_operational_error_rejects_misbound_embedded_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            result = terminal(305)
            install_runner_start_acceptance(root, result)
            embedded = {
                **{
                    field: result[field]
                    for field in autoplay_runner.BINDING_FIELDS
                },
                "protocol_version": 2,
                "in_game": True,
                "state_seq": result["terminal_state_seq"],
                "phase": "EVENT",
                "game_state": {},
            }
            embedded["selection_id"] = "wrong-selection"
            result.update({
                "termination_kind": "operational_error",
                "authoritative_game_over": False,
                "screen_type": "EVENT",
                "last_authoritative_state": embedded,
            })

            def failed_child(*args, **kwargs):
                (root / "next-run-selection.json").unlink()
                write_json(root / "run-result.json", result)
                write_json(root / "run-context.json", context(result))
                (root / "state.json").write_text("{", encoding="utf-8")
                (root / "run-history.jsonl").write_text(
                    json.dumps({**result, "record_type": "terminal_result"})
                    + "\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    args[0], 1, stdout=b"", stderr=b"bridge failed\n"
                )

            with self.assertRaisesRegex(
                autoplay_runner.RunnerError,
                "controller failure could not be uniquely bound",
            ):
                autoplay_runner.run_controller(
                    root,
                    max_actions=5,
                    python_executable="python-test",
                    subprocess_run=failed_child,
                )
            self.assertFalse(
                (
                    autoplay_runner.attempt_directory(
                        root, result["attempt_id"]
                    )
                    / "last-authoritative-state.json"
                ).exists()
            )

    def test_duplicate_restart_detects_sidecar_history_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = terminal(31)
            receipt = self.run_fake(root, result)
            sidecar_path = (
                autoplay_runner.attempt_directory(root, result["attempt_id"])
                / "controller-exit.json"
            )
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar["stdout_size"] += 1
            sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
            with self.assertRaisesRegex(
                autoplay_runner.RunnerError, "sidecar/history mismatch"
            ):
                autoplay_runner.append_controller_exit(root, receipt)

    def test_schema_protocol_and_policy_binding_fail_closed(self):
        mutations = (
            lambda artifacts: artifacts["selection"].__setitem__(
                "schema_version", 1
            ),
            lambda artifacts: artifacts["state"].__setitem__(
                "protocol_version", 1
            ),
            lambda artifacts: artifacts["state"].__setitem__(
                "policy_version", "wrong"
            ),
        )
        for index, mutate in enumerate(mutations, start=40):
            with self.subTest(index=index):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    result = terminal(index)
                    try:
                        receipt = self.run_fake(root, result, mutate=mutate)
                    except autoplay_runner.RunnerError:
                        receipts = []
                    else:
                        receipts = [receipt]
                    self.assertFalse(
                        any(
                            item.get("controller_exit_status") == "clear"
                            for item in receipts
                        )
                    )

    def test_subprocess_exception_writes_unbound_blocked_launch_incident(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def explode(*args, **kwargs):
                raise OSError("cannot launch")

            with self.assertRaises(autoplay_runner.RunnerError):
                autoplay_runner.run_controller(root, subprocess_run=explode)
            incidents = list(
                (root / "logs" / "blocked-launches").glob(
                    "*/blocked-launch-incident.json"
                )
            )
            self.assertEqual(1, len(incidents))
            incident = json.loads(incidents[0].read_text(encoding="utf-8"))
            self.assertEqual("unverified", incident["binding_status"])
            self.assertFalse((root / "run-history.jsonl").exists())

    def test_runner_fingerprint_covers_runner_and_both_gate_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production_root = Path(autoplay_runner.__file__).resolve().parent
            sources = autoplay_runner.fingerprint_sources(production_root)
            relative_sources = {
                path.relative_to(production_root).as_posix()
                for path in sources
            }
            self.assertTrue({
                "autoplay.py", "autoplay_runner.py", "launch_game.py",
                "bridge.py", "strategy_audit.py", "independent_oracle.py",
                "decision-case-resolutions.json",
                "decision-case-trace-evidence.json",
                "CommunicationMod.jar",
            }.issubset(relative_sources))
            self.assertTrue(any(
                name.startswith(
                    "src/CommunicationMod-1.2.1/src/main/java/communicationmod/"
                )
                for name in relative_sources
            ))
            for source_path in sources:
                relative = source_path.relative_to(production_root)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source_path.read_bytes())
            before = autoplay_runner.runner_fingerprint(root)
            (root / "autoplay_runner.py").write_text(
                "changed", encoding="utf-8"
            )
            after = autoplay_runner.runner_fingerprint(root)
            self.assertEqual(64, len(before))
            self.assertNotEqual(before, after)
            before_campaign = after
            (root / "campaign_attempt.py").write_text(
                "changed campaign", encoding="utf-8"
            )
            after_campaign = autoplay_runner.runner_fingerprint(root)
            self.assertNotEqual(before_campaign, after_campaign)
            before_fixture = after_campaign
            (root / "test_fixtures" / "decision-cases-v2.jsonl").write_text(
                "changed fixture", encoding="utf-8"
            )
            self.assertNotEqual(
                before_fixture, autoplay_runner.runner_fingerprint(root)
            )


if __name__ == "__main__":
    unittest.main()
