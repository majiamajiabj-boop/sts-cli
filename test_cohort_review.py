import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import autoplay
import cohort_review
import death_replay
import independent_oracle
import strategy_audit


def attempt(index, *, hidden=False, heart=False):
    return {
        "schema_version": 2,
        "policy_version": "fast-policy-v5",
        "attempt_id": f"attempt-{index}",
        "run_id": f"IRONCLAD:0:{index}",
        "seed": index,
        "character": "IRONCLAD",
        "ascension_level": 0,
        "run_type": "standard",
        "decision_hash": "decision-a",
        "controller_hash": "controller-a",
        "selection_id": f"selection-{index}",
        "selection_digest": "d" * 64,
        "terminal_state_seq": 1000 + index,
        "act": 4 if hidden else 3,
        "floor": 55 if hidden else 50,
        "victory": heart,
        "heart_defeated": heart,
        "hidden_entry": hidden,
        "keys": {"ruby": True, "emerald": True, "sapphire": True},
        "actions": 200 + index,
        "controller_exit_code": 0,
        "controller_stderr_size": 0,
        "controller_stdout_size": 0,
        "controller_exit_clean": True,
        "controller_stdout_sha256": "0" * 64,
        "controller_stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "controller_stdout_line_count": 1,
        "controller_stdout_semantic_sha256": "0" * 64,
        "freeze_manifest_sha256": "a" * 64,
        "freeze_source_digest": "b" * 64,
        "freeze_generated_at": 50.0,
    }


def cohort(attempts):
    return {
        "schema_version": 2,
        "policy_version": "fast-policy-v5",
        "decision_hash": "decision-a",
        "controller_hash": "controller-a",
        "attempts": attempts,
    }


def clear_oracle(item):
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
        "binding_fields": list(independent_oracle.ATTEMPT_BINDING_FIELDS)
        + ["terminal_state_seq"],
        "required_coverage_keys": sorted(
            cohort_review.ORACLE_REQUIRED_COVERAGE_KEYS
        ),
        "disagreement_kinds": sorted(
            independent_oracle.DISAGREEMENT_KINDS
        ),
        "decision_hash": item["decision_hash"],
        "attempt_id": item["attempt_id"],
        "status": "clear",
        "issue_count": 0,
        "eligible_unknown_count": 0,
        "disagreement_count": 0,
        "coverage": coverage,
        "issues": [],
        "unknowns": [],
        "blind_reviews": [],
    }


def clear_audit(item, *, replay_status="clear"):
    value = {
        **{key: item[key] for key in (
            "schema_version", "policy_version", "attempt_id", "run_id",
            "seed", "character", "ascension_level", "run_type",
            "decision_hash", "controller_hash", "selection_id",
            "selection_digest",
            "terminal_state_seq",
        )},
        "audit_engine_sha256": strategy_audit.audit_engine_sha256(),
        "audit_status": "clear",
        "release_gate_passed": True,
        "protocol_correctness": {"status": "clear"},
        "mechanics_coverage": {"status": "clear"},
        "strategy_quality": {"status": "clear"},
        "issue_count": 0,
        "review_finding_count": 0,
        "eligible_unknown_count": 0,
        "oracle_disagreement_count": 0,
        "issues": [],
        "review_findings": [],
        "code_changed": False,
        "independent_oracle": clear_oracle(item),
        "death_replay": {
            "status": replay_status, "issue_count": 0,
            "eligible_unknown_count": 0,
        },
    }
    return value


def _authoritative_combat_state(
    binding, item, state_seq, hp, monsters, *, terminal=False
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
        "room_phase": "COMPLETE" if terminal else "COMBAT",
        "screen_type": "GAME_OVER" if terminal else "NONE",
        "deck": [{"id": "Strike_R"}],
        "relics": [{"id": "Burning Blood"}],
        "potions": [],
        "keys": dict(item["keys"]),
        "has_ruby_key": item["keys"]["ruby"],
        "has_emerald_key": item["keys"]["emerald"],
        "has_sapphire_key": item["keys"]["sapphire"],
        "combat_state": {
            "player": {
                "current_hp": hp, "max_hp": 80, "block": 0,
                "energy": 0, "powers": [], "orbs": [],
            },
            "monsters": monsters,
        },
    }
    if terminal:
        game.update({
            "screen_state": {"victory": item["victory"]},
            "run_victory": item["victory"],
            "heart_defeated": item["heart_defeated"],
        })
    return {
        **{key: value for key, value in binding.items()
           if key != "schema_version"},
        "protocol_version": 2,
        "state_seq": state_seq,
        "phase": "GAME_OVER" if terminal else "COMBAT",
        "game_state": game,
    }


def _observable_state(item, hp):
    return {
        "current_hp": hp,
        "max_hp": 80,
        "gold": 99,
        "block": 0,
        "deck": [{"id": "Strike_R"}],
        "relics": [{"id": "Burning Blood"}],
        "potions": [],
        "keys": dict(item["keys"]),
    }


def _structured_zero_consequence(raw_text, *, route=None):
    reason = "map_choice_changes_only_the_bound_route_entrance"
    known = {
        field: {
            "status": "known",
            "authority": "production_mechanics_projection",
            "reason": reason,
        }
        for field in (
            "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
            "relic_changes", "potion_changes", "curse",
            "probabilistic_outcomes", "current_cost", "future_costs",
        )
    }
    value = {
        "schema_version": 1,
        "scope": "immediate_protocol_transition",
        "hp_delta": 0,
        "max_hp_delta": 0,
        "gold_delta": 0,
        "card_changes": {
            "gain": [], "remove": [], "upgrade": [], "transform": [],
        },
        "relic_changes": {"gain": [], "remove": [], "counter": []},
        "potion_changes": {"gain": [], "remove": [], "replace": []},
        "curse": {
            "gain": [], "remove": [], "probability": 0.0,
            "omamori_applicable": False,
            "omamori_charges_consumed": 0,
        },
        "probabilistic_outcomes": [],
        "current_cost": {"gold": 0, "hp": 0, "max_hp": 0},
        "future_costs": [],
        "raw_effect_text": raw_text,
        "uncertainty": [
            "future room outcomes remain probabilistic; entrance is exact"
        ],
        "uncertainty_classification": {
            "status": "classified_future",
            "authority": "protocol_map_coordinate",
            "reason": (
                "route entrance is deterministic and future rooms are out "
                "of immediate scope"
            ),
        },
        "field_knowledge": known,
    }
    if route is not None:
        value["route"] = route
    return value


def _complete_map_record(binding, item):
    hp = 13 if item["heart_defeated"] else 5
    before_seq = item["terminal_state_seq"] - 4
    after_seq = item["terminal_state_seq"] - 3
    before_state = _authoritative_combat_state(
        binding, item, before_seq, hp, []
    )
    after_state = _authoritative_combat_state(
        binding, item, after_seq, hp, []
    )
    for state in (before_state, after_state):
        state["phase"] = "MAP"
        game = state["game_state"]
        game["room_phase"] = "MAP"
        game["screen_type"] = "MAP"
        game.pop("combat_state", None)
    target = {
        "kind": "map_node", "x": 1, "y": 1,
        "symbol": "M", "label": "Monster",
    }
    raw_text = "Monster Monster"
    consequence = _structured_zero_consequence(
        raw_text, route={"symbol": "M", "x": 1, "y": 1}
    )
    choice = independent_oracle.canonical_choice(
        "map:1",
        choice_index=0,
        label="Monster",
        raw_text=raw_text,
        semantic_id="M@1,1",
        target=target,
        consequences=consequence,
        local_score=1,
        final_source="local",
        uncertainty=(
            "future room outcomes remain probabilistic; entrance is exact"
        ),
        selected=True,
    )
    record = {
        **binding,
        "record_type": "decision",
        "before_seq": before_seq,
        "after_seq": after_seq,
        "phase": "MAP",
        "act": item["act"],
        "floor": item["floor"],
        "action": "choose",
        "available_options_before": [{
            "option_id": "map:1",
            "choice_index": 0,
            "label": "Monster",
            "target": target,
        }],
        "available_commands_before": [],
        "legal_choices_before": [choice],
        "selected_choice_id": "map:1",
        "selected_choice_ids": ["map:1"],
        "requested_target_id": "map:1",
        "resolved_target_id": "map:1",
        "observable_state_before": _observable_state(item, hp),
        "observable_state_after": _observable_state(item, hp),
        "decision_outcome": {
            "current_hp_delta": 0,
            "max_hp_delta": 0,
            "gold_delta": 0,
            "block_delta": 0,
            "deck": {"added": [], "removed": [], "changed": []},
            "relics": {"added": [], "removed": [], "changed": []},
            "potions": {"added": [], "removed": [], "changed": []},
            "keys_before": dict(item["keys"]),
            "keys_after": dict(item["keys"]),
        },
        "authoritative_state_before": before_state,
        "authoritative_state_after": after_state,
        "decision": {
            "decision_type": "MAP",
            "selected_choice_id": "map:1",
            "reason": "complete bound map choice",
            "candidates": [choice],
            "model_advice": {
                "status": "skipped",
                "final_choice_ids": ["map:1"],
            },
        },
    }
    record["authoritative_choice_settlement"] = (
        autoplay.authoritative_choice_settlement(record)
    )
    return record


def _complete_combat_record(binding, item):
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
                "action": "play", "legal": True,
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
    before_state = _authoritative_combat_state(
        binding, item, before_seq, hp_before, monsters
    )
    before_state["game_state"]["combat_state"]["player"]["energy"] = (
        1 if heart else 0
    )
    after_state = _authoritative_combat_state(
        binding, item, after_seq, hp_after, after_monsters, terminal=True
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
            "current_hp": hp_before, "max_hp": 80, "block": 0,
            "energy": 1 if heart else 0, "powers": [], "orbs": [],
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


def write_attempt(root, item, *, unknown_record=False, bad_audit=False):
    directory = root / item["attempt_id"]
    directory.mkdir(parents=True)
    binding = {key: item[key] for key in (
        "schema_version", "policy_version", "attempt_id", "run_id",
        "seed", "character", "ascension_level", "run_type",
        "decision_hash", "controller_hash", "selection_id",
        "selection_digest",
        "terminal_state_seq",
    )}
    result = {
        **binding,
        "class": item["character"],
        "state_seq": item["terminal_state_seq"],
        "victory": item["victory"],
        "heart_defeated": item["heart_defeated"],
        "act": item["act"],
        "observed_max_act": item["act"],
        "floor": item["floor"],
        "current_hp": 0 if not item["victory"] else 12,
        "max_hp": 80,
        "potions": [],
        "deck": [{"id": "Strike_R"}],
        "relics": [{"id": "Burning Blood"}],
        "keys": item["keys"],
        "actions": item["actions"],
        "termination_kind": "game_over",
        "authoritative_game_over": True,
        "screen_type": "GAME_OVER",
    }
    state = _authoritative_combat_state(
        binding,
        item,
        item["terminal_state_seq"],
        result["current_hp"],
        [],
        terminal=True,
    )
    stdout = (json.dumps(result) + "\n").encode("utf-8")
    item["controller_stdout_size"] = len(stdout)
    stderr = b""
    semantic = hashlib.sha256(json.dumps(
        result, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    item.update({
        "controller_stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "controller_stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "controller_stdout_line_count": 1,
        "controller_stdout_semantic_sha256": semantic,
    })
    controller_exit = {
        **binding,
        "record_type": "controller_exit",
        "controller_exit_status": "clear",
        "exit_code": 0,
        "stdout_size": len(stdout),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_size": 0,
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "stdout_line_count": 1,
        "stdout_semantic_sha256": semantic,
        "freeze_manifest_sha256": "a" * 64,
        "freeze_source_digest": "b" * 64,
        "freeze_generated_at": 50.0,
    }
    records = [
        {
            **binding,
            "record_type": "controller_start",
            "state_seq": item["terminal_state_seq"] - 5,
        },
        _complete_map_record(binding, item),
        _complete_combat_record(binding, item),
        {**result, "record_type": "terminal_result"},
    ]
    if unknown_record:
        records.insert(2, {
            **binding,
            "record_type": "mystery",
            "state_seq": item["terminal_state_seq"],
        })
    oracle = independent_oracle.audit_records(
        records,
        item["decision_hash"],
        item["attempt_id"],
        artifacts={
            "run_context": binding,
            "run_result": result,
            "state": state,
            "selection": binding,
        },
    )
    replay = death_replay.build_death_replay(
        records, terminal=records[-1]
    )
    if not unknown_record and (
        oracle.get("status") != "clear" or replay.get("status") != "clear"
    ):
        raise AssertionError({"oracle": oracle, "replay": replay})
    audit = clear_audit(item)
    audit["independent_oracle"] = oracle
    audit["death_replay"] = replay
    audit["death_observed"] = replay["death_observed"]
    if bad_audit:
        audit["audit_status"] = "inconclusive"
        audit["release_gate_passed"] = False
    artifacts = {
        "run-context.json": binding,
        "run-result.json": result,
        "terminal-state.json": state,
        "selection.json": binding,
        "run-audit.json": audit,
        "death-replay.json": replay,
        "controller-exit.json": controller_exit,
    }
    for name, value in artifacts.items():
        (directory / name).write_text(json.dumps(value), encoding="utf-8")
    (directory / "controller.stdout.log").write_bytes(stdout)
    (directory / "controller.stderr.log").write_bytes(stderr)
    (directory / "autoplay.log").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    with (root / "run-history.jsonl").open("a", encoding="utf-8") as handle:
        for record in (
            {**result, "record_type": "terminal_result"},
            {
                **{
                    field: audit.get(field)
                    for field in cohort_review.HISTORY_AUDIT_FIELDS
                },
                "record_type": "run_audit",
            },
            controller_exit,
        ):
            handle.write(json.dumps(record) + "\n")


def _rewrite_history_audit(root, item, audit):
    path = root / "run-history.jsonl"
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    replacement = {
        **{
            field: audit.get(field)
            for field in cohort_review.HISTORY_AUDIT_FIELDS
        },
        "record_type": "run_audit",
    }
    replaced = 0
    for index, record in enumerate(records):
        if (
            record.get("record_type") == "run_audit"
            and record.get("attempt_id") == item["attempt_id"]
        ):
            records[index] = replacement
            replaced += 1
    if replaced != 1:
        raise AssertionError(f"expected one history audit row, saw {replaced}")
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _refresh_derived_evidence(root, item):
    directory = root / item["attempt_id"]
    records = [
        json.loads(line)
        for line in (directory / "autoplay.log").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    artifacts = {
        name: json.loads((directory / filename).read_text(encoding="utf-8"))
        for name, filename in {
            "run_context": "run-context.json",
            "run_result": "run-result.json",
            "state": "terminal-state.json",
            "selection": "selection.json",
        }.items()
    }
    replay = death_replay.build_death_replay(
        records,
        terminal=next(
            record for record in records
            if record.get("record_type") == "terminal_result"
        ),
    )
    oracle = independent_oracle.audit_records(
        records,
        item["decision_hash"],
        item["attempt_id"],
        artifacts=artifacts,
    )
    audit_path = directory / "run-audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit.update({
        "independent_oracle": oracle,
        "death_replay": replay,
        "death_observed": replay["death_observed"],
        "issue_count": (
            oracle["issue_count"] + replay["issue_count"]
        ),
        "eligible_unknown_count": (
            oracle["eligible_unknown_count"]
            + replay["eligible_unknown_count"]
        ),
        "oracle_disagreement_count": oracle["disagreement_count"],
    })
    if audit["issue_count"] or audit["oracle_disagreement_count"]:
        audit["audit_status"] = "issues"
        audit["release_gate_passed"] = False
    elif audit["eligible_unknown_count"]:
        audit["audit_status"] = "inconclusive"
        audit["release_gate_passed"] = False
    else:
        audit["audit_status"] = "clear"
        audit["release_gate_passed"] = True
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    (directory / "death-replay.json").write_text(
        json.dumps(replay), encoding="utf-8"
    )
    _rewrite_history_audit(root, item, audit)
    return audit, replay, oracle


def write_deep_evidence(root):
    (root / "decision-case-replay.json").write_text(json.dumps({
        "schema_version": 1,
        "history_coverage_version": 2,
        "target_decision_hash": "decision-a",
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
    }), encoding="utf-8")
    (root / "fixed-seed-replay.json").write_text(json.dumps({
        "schema_version": 1,
        "policy_version": "fast-policy-v5",
        "decision_hash": "decision-a",
        "controller_hash": "controller-a",
        "status": "clear",
        "release_gate_passed": True,
        "issue_count": 0,
        "eligible_unknown_count": 0,
        "required_seeds": [101, 202],
        "seeds": [101, 202],
        "comparisons": [
            {
                "seed": seed,
                "status": "clear",
                "release_gate_passed": True,
                "issue_count": 0,
                "eligible_unknown_count": 0,
            }
            for seed in (101, 202)
        ],
    }), encoding="utf-8")


class CohortReviewTests(unittest.TestCase):
    def test_audit_clear_requires_current_engine_digest(self):
        value = clear_audit(attempt(1))
        self.assertTrue(cohort_review._audit_is_clear(value))
        value["audit_engine_sha256"] = "0" * 64
        self.assertFalse(cohort_review._audit_is_clear(value))
        value.pop("audit_engine_sha256")
        self.assertFalse(cohort_review._audit_is_clear(value))

    def test_six_without_hidden_requires_exact_last_six(self):
        items = [attempt(index) for index in range(7)]
        self.assertEqual(
            cohort_review.REVIEW_SIX_WITHOUT_HIDDEN,
            cohort_review.review_requirement(cohort(items[:6])),
        )
        self.assertIsNone(cohort_review.review_requirement(cohort(items)))

    def test_act4_failure_takes_immediate_precedence(self):
        items = [attempt(1), attempt(2, hidden=True)]
        self.assertEqual(
            cohort_review.REVIEW_ACT4_FAILURE,
            cohort_review.review_requirement(cohort(items)),
        )

    def test_heart_victory_requires_immediate_power_bound_review(self):
        item = attempt(1, hidden=True, heart=True)
        self.assertEqual(
            cohort_review.REVIEW_HEART_VICTORY,
            cohort_review.review_requirement(cohort([item])),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item)
            clear = cohort_review.build_review(
                cohort([item]), root,
                cohort_review.REVIEW_HEART_VICTORY,
            )
            self.assertEqual("clear", clear["review_status"])
            self.assertTrue(
                cohort_review.validate_review_gate(clear, cohort([item]), root)
            )

            trace_path = root / item["attempt_id"] / "autoplay.log"
            rows = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]
            combat = next(
                row for row in rows if row.get("phase") == "COMBAT"
            )
            combat["monsters_before"][0]["powers"] = []
            trace_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            _refresh_derived_evidence(root, item)
            blocked = cohort_review.build_review(
                cohort([item]), root,
                cohort_review.REVIEW_HEART_VICTORY,
            )
            self.assertEqual("issues", blocked["review_status"])
            self.assertTrue(any(
                finding.get("kind") == "act4_heart_power_evidence_missing"
                for finding in blocked["findings"]
            ))

    def test_two_no_hidden_batches_require_deep_review(self):
        items = [attempt(index) for index in range(12)]
        self.assertEqual(
            cohort_review.REVIEW_TWELVE_WITHOUT_HIDDEN_DEEP,
            cohort_review.review_requirement(cohort(items)),
        )

    def test_deep_review_requires_fixed_case_and_seed_evidence(self):
        items = [attempt(index) for index in range(12)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for item in items:
                write_attempt(root, item)
            missing = cohort_review.build_review(
                cohort(items), root,
                cohort_review.REVIEW_TWELVE_WITHOUT_HIDDEN_DEEP,
            )
            self.assertEqual("issues", missing["review_status"])
            self.assertGreater(missing["eligible_unknown_count"], 0)
            self.assertTrue(
                cohort_review.validate_review_binding(
                    missing, cohort(items), root
                )
            )
            self.assertFalse(
                cohort_review.validate_review_gate(missing, cohort(items), root)
            )

            write_deep_evidence(root)
            report = cohort_review.build_review(
                cohort(items), root,
                cohort_review.REVIEW_TWELVE_WITHOUT_HIDDEN_DEEP,
            )
            self.assertEqual("clear", report["review_status"])
            self.assertTrue(
                cohort_review.validate_review_gate(report, cohort(items), root)
            )
            self.assertIn(
                "fixed_seed_replay",
                report["deep_review_evidence"]["source_digests"],
            )

    def test_clear_review_binds_sources_and_satisfies_gate(self):
        items = [attempt(index) for index in range(6)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for item in items:
                write_attempt(root, item)
            report = cohort_review.build_review(
                cohort(items), root,
                cohort_review.REVIEW_SIX_WITHOUT_HIDDEN,
                generated_at=99.0,
            )
            self.assertEqual(
                cohort_review.SOURCE_DIGEST_KEYS,
                set(report["attempts"][0]["source_digests"]),
            )
            self.assertEqual(
                1,
                report["attempts"][0]["controller_exit"]["stdout_line_count"],
            )
            self.assertEqual(
                64,
                len(report["attempts"][0]["controller_exit"][
                    "stdout_semantic_sha256"
                ]),
            )
            self.assertEqual(
                64,
                len(report["attempts"][0][
                    "independent_oracle_canonical_sha256"
                ]),
            )
            self.assertEqual(
                64,
                len(report["attempts"][0][
                    "death_replay_canonical_sha256"
                ]),
            )
            self.assertEqual(
                cohort_review.EMPTY_SHA256,
                report["attempts"][0]["controller_exit"]["stderr_sha256"],
            )
            self.assertTrue(
                cohort_review.validate_review_gate(
                    report, cohort(items), root
                )
            )
        self.assertEqual("clear", report["review_status"])
        self.assertTrue(report["release_gate_passed"])
        self.assertEqual(6, report["reviewed_attempt_count"])
        self.assertEqual(6, len(report["death_positions"]))
        self.assertFalse(
            cohort_review.validate_review_gate(report, cohort(items))
        )

    def test_unknown_trace_type_is_p1_and_blocks_gate(self):
        item = attempt(1, hidden=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item, unknown_record=True)
            report = cohort_review.build_review(
                cohort([item]), root,
                cohort_review.REVIEW_ACT4_FAILURE,
            )
        self.assertEqual("issues", report["review_status"])
        self.assertFalse(report["release_gate_passed"])
        self.assertEqual("P1", report["findings"][0]["severity"])
        self.assertFalse(cohort_review.validate_review_gate(report, cohort([item])))

    def test_six_run_review_with_one_unknown_trace_is_blocking(self):
        items = [attempt(index) for index in range(6)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, item in enumerate(items):
                write_attempt(root, item, unknown_record=index == 4)
            report = cohort_review.build_review(
                cohort(items), root,
                cohort_review.REVIEW_SIX_WITHOUT_HIDDEN,
            )
            self.assertEqual("issues", report["review_status"])
            self.assertTrue(any(
                finding.get("kind") == "unknown_trace_record_type"
                and finding.get("attempt_id") == items[4]["attempt_id"]
                for finding in report["findings"]
            ))
            self.assertFalse(
                cohort_review.validate_review_gate(report, cohort(items), root)
            )

    def test_nonclear_attempt_audit_is_not_silently_reviewed_clear(self):
        item = attempt(1, hidden=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item, bad_audit=True)
            report = cohort_review.build_review(
                cohort([item]), root,
                cohort_review.REVIEW_ACT4_FAILURE,
            )
        self.assertEqual("issues", report["review_status"])
        self.assertEqual("attempt_audit_not_clear", report["findings"][0]["kind"])

    def test_missing_artifact_fails_closed(self):
        item = attempt(1, hidden=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item)
            (root / item["attempt_id"] / "death-replay.json").unlink()
            with self.assertRaises(cohort_review.CohortReviewError):
                cohort_review.build_review(
                    cohort([item]), root,
                    cohort_review.REVIEW_ACT4_FAILURE,
                )

    def test_tampered_controller_stream_or_exit_binding_fails_closed(self):
        item = attempt(1, hidden=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item)
            attempt_dir = root / item["attempt_id"]
            (attempt_dir / "controller.stderr.log").write_bytes(b"warning")
            with self.assertRaisesRegex(
                cohort_review.CohortReviewError, "stderr"
            ):
                cohort_review.build_review(
                    cohort([item]), root,
                    cohort_review.REVIEW_ACT4_FAILURE,
                )

            (attempt_dir / "controller.stderr.log").write_bytes(b"")
            sidecar_path = attempt_dir / "controller-exit.json"
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar["selection_id"] = "wrong-selection"
            sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
            with self.assertRaisesRegex(
                cohort_review.CohortReviewError, "selection_id binding"
            ):
                cohort_review.build_review(
                    cohort([item]), root,
                    cohort_review.REVIEW_ACT4_FAILURE,
                )

    def test_history_terminal_audit_exit_chain_is_strict_and_unique(self):
        item = attempt(1, hidden=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item)
            history_path = root / "run-history.jsonl"
            rows = [
                json.loads(line)
                for line in history_path.read_text(encoding="utf-8").splitlines()
            ]
            history_path.write_text(
                "".join(
                    json.dumps(row) + "\n"
                    for row in (rows[1], rows[0], rows[2])
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                cohort_review.CohortReviewError, "out of order"
            ):
                cohort_review.build_review(
                    cohort([item]), root,
                    cohort_review.REVIEW_ACT4_FAILURE,
                )

            history_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows + [rows[2]]),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                cohort_review.CohortReviewError, "not unique"
            ):
                cohort_review.build_review(
                    cohort([item]), root,
                    cohort_review.REVIEW_ACT4_FAILURE,
                )

    def test_gate_rejects_stale_attempt_list_or_missing_digest(self):
        items = [attempt(index) for index in range(6)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for item in items:
                write_attempt(root, item)
            report = cohort_review.build_review(
                cohort(items), root,
                cohort_review.REVIEW_SIX_WITHOUT_HIDDEN,
            )
        stale = {**report, "attempt_ids": report["attempt_ids"][:-1]}
        self.assertFalse(cohort_review.validate_review_gate(stale, cohort(items)))
        missing = json.loads(json.dumps(report))
        missing["attempts"][0]["source_digests"].pop("trace")
        self.assertFalse(cohort_review.validate_review_gate(missing, cohort(items)))

    def test_gate_rehashes_sources_when_attempt_root_is_supplied(self):
        item = attempt(1, hidden=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item)
            report = cohort_review.build_review(
                cohort([item]), root,
                cohort_review.REVIEW_ACT4_FAILURE,
            )
            self.assertTrue(
                cohort_review.validate_review_gate(report, cohort([item]), root)
            )
            trace = root / item["attempt_id"] / "autoplay.log"
            trace.write_text(
                trace.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            self.assertFalse(
                cohort_review.validate_review_gate(report, cohort([item]), root)
            )

    def test_gate_rebuilds_report_and_rejects_tampered_nested_summary(self):
        items = [attempt(index) for index in range(6)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for item in items:
                write_attempt(root, item)
            report = cohort_review.build_review(
                cohort(items), root,
                cohort_review.REVIEW_SIX_WITHOUT_HIDDEN,
            )
            tampered = json.loads(json.dumps(report))
            tampered["cohort_evidence"]["oracle_reports"][0][
                "independent_oracle"
            ]["status"] = "issues"
            self.assertEqual("clear", tampered["review_status"])
            self.assertFalse(
                cohort_review.validate_review_gate(
                    tampered, cohort(items), root
                )
            )
            self.assertFalse(
                cohort_review.validate_review_gate(report, cohort(items))
            )

    def test_replay_must_equal_run_audit_embedded_object(self):
        item = attempt(1, hidden=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item)
            replay_path = root / item["attempt_id"] / "death-replay.json"
            replay = json.loads(replay_path.read_text(encoding="utf-8"))
            replay["counterfactuals"] = [{"fabricated": True}]
            replay_path.write_text(json.dumps(replay), encoding="utf-8")

            with self.assertRaisesRegex(
                cohort_review.CohortReviewError,
                "differs from run-audit embedded object",
            ):
                cohort_review.build_review(
                    cohort([item]), root,
                    cohort_review.REVIEW_ACT4_FAILURE,
                )

    def test_replay_and_embedded_audit_cannot_be_forged_together(self):
        item = attempt(1, hidden=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item)
            attempt_dir = root / item["attempt_id"]
            replay_path = attempt_dir / "death-replay.json"
            audit_path = attempt_dir / "run-audit.json"
            replay = json.loads(replay_path.read_text(encoding="utf-8"))
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            replay["forged_producer_claim"] = "still-clear"
            audit["death_replay"] = replay
            replay_path.write_text(json.dumps(replay), encoding="utf-8")
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            _rewrite_history_audit(root, item, audit)

            with self.assertRaisesRegex(
                cohort_review.CohortReviewError,
                "death-replay differs from independent trace rebuild",
            ):
                cohort_review.build_review(
                    cohort([item]), root,
                    cohort_review.REVIEW_ACT4_FAILURE,
                )

    def test_replay_rebuild_requires_one_last_trace_terminal(self):
        for mutation, error in (
            ("missing", "missing or not unique"),
            ("duplicate", "missing or not unique"),
            ("out_of_order", "out of order"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                item = attempt(1, hidden=True)
                root = Path(directory)
                write_attempt(root, item)
                trace_path = root / item["attempt_id"] / "autoplay.log"
                rows = [
                    json.loads(line)
                    for line in trace_path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if line.strip()
                ]
                terminal = rows.pop()
                if mutation == "duplicate":
                    rows.extend([terminal, json.loads(json.dumps(terminal))])
                elif mutation == "out_of_order":
                    rows.insert(1, terminal)
                trace_path.write_text(
                    "".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    cohort_review.CohortReviewError, error
                ):
                    cohort_review.build_review(
                        cohort([item]), root,
                        cohort_review.REVIEW_ACT4_FAILURE,
                    )

    def test_trace_terminal_is_exactly_cross_bound_before_replay(self):
        item = attempt(1, hidden=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item)
            attempt_dir = root / item["attempt_id"]
            records = [
                json.loads(line)
                for line in (attempt_dir / "autoplay.log").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            result = json.loads(
                (attempt_dir / "run-result.json").read_text(
                    encoding="utf-8"
                )
            )
            state = json.loads(
                (attempt_dir / "terminal-state.json").read_text(
                    encoding="utf-8"
                )
            )
            expected = cohort_review._binding(item)
            observed = cohort_review._trace_terminal_for_replay(
                records, expected, result, state
            )
            self.assertIs(observed, records[-1])

            cases = []
            for field, value in (
                ("schema_version", 1),
                ("authoritative_game_over", False),
                ("screen_type", "NONE"),
                ("termination_kind", "operational_error"),
                ("state_seq", item["terminal_state_seq"] - 1),
                ("terminal_state_seq", item["terminal_state_seq"] - 1),
                ("selection_id", "wrong-selection"),
                ("keys", {"ruby": False, "emerald": True, "sapphire": True}),
            ):
                changed_records = json.loads(json.dumps(records))
                changed_records[-1][field] = value
                cases.append((f"terminal_{field}", changed_records, result, state))

            changed_result = json.loads(json.dumps(result))
            changed_result["current_hp"] = 1
            cases.append(("result_current_hp", records, changed_result, state))
            changed_state = json.loads(json.dumps(state))
            changed_state["game_state"]["current_hp"] = 1
            cases.append(("state_current_hp", records, result, changed_state))
            changed_state_keys = json.loads(json.dumps(state))
            changed_state_keys["game_state"]["has_sapphire_key"] = False
            cases.append(("state_keys", records, result, changed_state_keys))

            for name, changed_records, changed_result, changed_state in cases:
                with self.subTest(name=name), self.assertRaises(
                    cohort_review.CohortReviewError
                ):
                    cohort_review._trace_terminal_for_replay(
                        changed_records,
                        expected,
                        changed_result,
                        changed_state,
                    )

    def test_embedded_oracle_cannot_be_forged_with_its_audit(self):
        item = attempt(1, hidden=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item)
            audit_path = root / item["attempt_id"] / "run-audit.json"
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit["independent_oracle"][
                "forged_producer_claim"
            ] = "still-clear"
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            _rewrite_history_audit(root, item, audit)

            with self.assertRaisesRegex(
                cohort_review.CohortReviewError,
                "independent oracle differs from isolated trace rebuild",
            ):
                cohort_review.build_review(
                    cohort([item]), root,
                    cohort_review.REVIEW_ACT4_FAILURE,
                )

    def test_oracle_contract_rejects_zero_or_incomplete_coverage(self):
        item = attempt(1, hidden=True)
        expected = cohort_review._binding(item)
        for mutation in ("zero", "unevaluated"):
            with self.subTest(mutation=mutation):
                bound_audit = clear_audit(item)
                oracle = bound_audit["independent_oracle"]
                if mutation == "zero":
                    oracle["coverage"]["protocol_binding"].update({
                        "eligible": 0,
                        "evaluated": 0,
                        "status": "not_applicable",
                    })
                else:
                    oracle["coverage"]["protocol_binding"]["evaluated"] = 0
                with self.assertRaises(cohort_review.CohortReviewError):
                    cohort_review._validate_oracle_contract(
                        oracle, expected
                    )

    def test_terminal_state_facts_are_cross_bound_to_result(self):
        mutations = {
            "run_victory": True,
            "heart_defeated": True,
            "seed": 999,
            "class": "THE_SILENT",
            "ascension_level": 1,
            "act": 3,
            "current_hp": 1,
        }
        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                item = attempt(1, hidden=True)
                root = Path(directory)
                write_attempt(root, item)
                state_path = root / item["attempt_id"] / "terminal-state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["game_state"][field] = value
                state_path.write_text(json.dumps(state), encoding="utf-8")

                with self.assertRaisesRegex(
                    cohort_review.CohortReviewError,
                    f"game_state\\.{field} mismatch",
                ):
                    cohort_review.build_review(
                        cohort([item]), root,
                        cohort_review.REVIEW_ACT4_FAILURE,
                    )

    def test_heart_trace_support_requires_full_binding(self):
        item = attempt(1, hidden=True, heart=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_attempt(root, item)
            trace_path = root / item["attempt_id"] / "autoplay.log"
            rows = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]
            act4 = next(row for row in rows if row.get("act") == 4)
            act4.pop("selection_id")
            trace_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                cohort_review.CohortReviewError,
                "Act4 trace record.*selection_id binding mismatch",
            ):
                cohort_review.build_review(
                    cohort([item]), root,
                    cohort_review.REVIEW_HEART_VICTORY,
                )

    def test_fixed_seed_evidence_requires_unique_complete_clear_matrix(self):
        items = [attempt(index) for index in range(12)]
        mutations = {
            "duplicate_seed": lambda value: value.update({
                "seeds": [101, 101],
            }),
            "missing_comparison": lambda value: value.update({
                "comparisons": value["comparisons"][:1],
            }),
            "nonclear_comparison": lambda value: value["comparisons"][0].update({
                "status": "issues",
            }),
            "missing_prescribed_seed": lambda value: value.update({
                "required_seeds": [101],
            }),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for item in items:
                    write_attempt(root, item)
                write_deep_evidence(root)
                path = root / "fixed-seed-replay.json"
                value = json.loads(path.read_text(encoding="utf-8"))
                mutate(value)
                path.write_text(json.dumps(value), encoding="utf-8")

                report = cohort_review.build_review(
                    cohort(items), root,
                    cohort_review.REVIEW_TWELVE_WITHOUT_HIDDEN_DEEP,
                )
                self.assertEqual("issues", report["review_status"])
                self.assertIn(
                    "deep_review_fixed_seed_replay_not_clear",
                    {finding["kind"] for finding in report["findings"]},
                )


if __name__ == "__main__":
    unittest.main()
