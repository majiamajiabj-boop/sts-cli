from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


class ViewerTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "logs" / "attempts").mkdir(parents=True)
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<!doctype html><title>viewer</title>", encoding="utf-8")
        (self.static / "app.js").write_text("'use strict';", encoding="utf-8")
        (self.static / "styles.css").write_text("body{}", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def _write_json(self, path: Path, value):
        path.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def _make_attempt(
        self,
        index: int,
        *,
        completed: bool = True,
        authoritative: bool = True,
        seed: int | None = None,
        with_detail: bool = False,
    ) -> Path:
        attempt_id = f"attempt-{index:02d}"
        directory = self.root / "logs" / "attempts" / attempt_id
        directory.mkdir()
        seed_value = seed if seed is not None else 900000000000000000 + index
        result = {
            "schema_version": 2,
            "policy_version": "fast-policy-v5",
            "strategy_revision": "test-revision",
            "decision_hash": "decision-hash",
            "controller_hash": "controller-hash",
            "attempt_id": attempt_id,
            "goal_mode": "HEART",
            "selection": {
                "selection_id": f"selection-{index}",
                "created_at": 1000.0 + index,
                "algorithm": "test",
                "reason": "test-run",
            },
            "selection_id": f"selection-{index}",
            "run_type": "standard",
            "termination_kind": "game_over",
            "authoritative_game_over": authoritative,
            "screen_type": "GAME_OVER",
            "run_id": f"IRONCLAD:0:{seed_value}",
            "seed": seed_value,
            "terminal_state_seq": 5000 + index,
            "character": "IRONCLAD",
            "ascension_level": 0,
            "act": 2,
            "observed_max_act": 2,
            "floor": 20 + index,
            "current_hp": 0,
            "max_hp": 80,
            "victory": False,
            "heart_defeated": False,
            "keys": {"ruby": True, "emerald": False, "sapphire": True},
            "actions": 300 + index,
            "deck": [
                {
                    "id": "Strike_R",
                    "name": "打击",
                    "upgrades": 0,
                    "type": "ATTACK",
                    "rarity": "BASIC",
                    "cost": 1,
                },
                {
                    "id": "Strike_R",
                    "name": "打击",
                    "upgrades": 0,
                    "type": "ATTACK",
                    "rarity": "BASIC",
                    "cost": 1,
                },
            ],
            "relics": [{"id": "Burning Blood", "name": "燃烧之血", "counter": -1}],
            "potions": [
                {"id": "Potion Slot", "name": "药水栏", "slot": 0},
                {"id": "Fire Potion", "name": "火焰药水", "slot": 1},
            ],
        }
        self._write_json(directory / "run-result.json", result)

        if completed:
            controller_exit = {
                "attempt_id": attempt_id,
                "run_id": result["run_id"],
                "seed": seed_value,
                "character": "IRONCLAD",
                "selection_id": result["selection_id"],
                "terminal_state_seq": result["terminal_state_seq"],
                "controller_exit_status": "clear",
                "exit_code": 0,
                "observed_at": 2000.0 + index,
            }
            self._write_json(directory / "controller-exit.json", controller_exit)

        if with_detail:
            audit = {
                "attempt_id": attempt_id,
                "audit_status": "issues",
                "release_gate_passed": False,
                "issue_count": 1,
                "review_finding_count": 1,
                "eligible_unknown_count": 2,
                "oracle_disagreement_count": 0,
                "death_observed": True,
                "death_replay": {"status": "clear"},
                "model_advice": {
                    "records": 7,
                    "remote_consultations": 5,
                    "remote_valid_recommendations": 4,
                    "semantic_choice_changes": 2,
                    "semantic_adoption_rate": 0.5,
                    "model_agreements": 1,
                    "effective_overrides": 2,
                    "fallbacks": 1,
                    "local_cache_hits": 1,
                    "estimated_cost_usd": 0.003,
                    "remote_latency_ms": {"average": 120.5, "maximum": 210},
                },
                "issues": [
                    {
                        "kind": "high_impact_default_choice",
                        "act": 1,
                        "floor": 4,
                        "reason": "test-reason",
                    }
                ],
                "review_findings": [
                    {
                        "kind": "high_value_resource_loss_choice",
                        "act": 1,
                        "floor": 0,
                        "resolved": False,
                    }
                ],
            }
            self._write_json(directory / "run-audit.json", audit)
            replay = {
                "attempt_id": attempt_id,
                "status": "clear",
                "replay_kind": "death",
                "death_observed": True,
                "issue_count": 0,
                "eligible_unknown_count": 0,
                "turns": [
                    {
                        "turn": 7,
                        "complete": True,
                        "first_state_seq": 10,
                        "last_state_seq": 12,
                        "actions": [
                            {
                                "action": "play",
                                "before_seq": 10,
                                "after_seq": 11,
                                "selected_target_id": "card-one",
                                "hand_before": [
                                    {
                                        "card_instance_id": "card-one",
                                        "id": "Strike_R",
                                        "name": "打击",
                                    }
                                ],
                                "player_before": {
                                    "current_hp": 8,
                                    "max_hp": 80,
                                    "block": 0,
                                    "energy": 1,
                                },
                                "monsters_before": [
                                    {
                                        "id": "BookOfStabbing",
                                        "current_hp": 20,
                                        "max_hp": 160,
                                        "block": 0,
                                        "intent": "ATTACK",
                                        "move_adjusted_damage": 7,
                                        "move_hits": 5,
                                    }
                                ],
                                "actual_outcome": {
                                    "player_hp_loss": 0,
                                    "player_hp_gain": 0,
                                    "enemy_hp_loss": 6,
                                },
                            }
                        ],
                    }
                ],
            }
            self._write_json(directory / "death-replay.json", replay)
        return directory

    def test_recent_runs_are_completed_authoritative_and_limited(self):
        for index in range(12):
            self._make_attempt(index)
        self._make_attempt(20, completed=False)
        self._make_attempt(21, authoritative=False)

        payload = server.RunStore(self.root).recent_runs()

        self.assertEqual(payload["count"], 10)
        self.assertEqual(payload["runs"][0]["attempt_id"], "attempt-11")
        self.assertEqual(payload["runs"][-1]["attempt_id"], "attempt-02")
        self.assertNotIn("attempt-20", {item["attempt_id"] for item in payload["runs"]})
        self.assertNotIn("attempt-21", {item["attempt_id"] for item in payload["runs"]})

    def test_seed_is_serialized_as_an_exact_string(self):
        exact_seed = 9223372036854775807
        self._make_attempt(1, seed=exact_seed)

        run = server.RunStore(self.root).recent_runs()["runs"][0]

        self.assertEqual(run["seed"], "9223372036854775807")

    def test_detail_is_compact_and_maps_selected_card(self):
        self._make_attempt(3, with_detail=True)

        detail = server.RunStore(self.root).run_detail("attempt-03")

        self.assertIsNotNone(detail)
        self.assertEqual(detail["build"]["deck"][0]["count"], 2)
        self.assertEqual(detail["build"]["potions"][0]["name"], "火焰药水")
        action = detail["death_replay"]["turns"][0]["actions"][0]
        self.assertEqual(action["selected_name"], "打击")
        self.assertEqual(action["outcome"]["enemy_hp_loss"], 6)
        self.assertEqual(detail["audit_detail"]["issues"][0]["floor"], 4)
        model = detail["run"]["audit"]["model"]
        self.assertEqual(model["remote_consultations"], 5)
        self.assertEqual(model["remote_valid_recommendations"], 4)
        self.assertEqual(model["semantic_choice_changes"], 2)
        self.assertEqual(model["semantic_adoption_rate"], 0.5)
        self.assertEqual(model["average_latency_ms"], 120.5)
        self.assertEqual(model["estimated_cost_usd"], 0.003)

    def test_missing_audit_has_zeroed_model_summary(self):
        self._make_attempt(5)

        run = server.RunStore(self.root).recent_runs()["runs"][0]

        self.assertFalse(run["audit"]["model"]["available"])
        self.assertEqual(run["audit"]["model"]["remote_consultations"], 0)
        self.assertEqual(run["audit"]["model"]["semantic_choice_changes"], 0)
        self.assertIsNone(run["audit"]["model"]["semantic_adoption_rate"])

    def test_queries_do_not_modify_repository_files(self):
        directory = self._make_attempt(4, with_detail=True)
        (self.root / "state.json").write_text("live state must be ignored", encoding="utf-8")
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in [self.root / "state.json", *directory.iterdir()]
            if path.is_file()
        }

        store = server.RunStore(self.root)
        store.recent_runs()
        store.run_detail("attempt-04")

        after = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in before
        }
        self.assertEqual(before, after)

    def test_http_surface_is_get_only_and_loopback_ready(self):
        self._make_attempt(5)
        httpd = server.build_server(
            self.root, host="127.0.0.1", port=0, static_root=self.static
        )
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=3) as response:
                payload = json.load(response)
            self.assertEqual(payload["mode"], "read_only")

            with urllib.request.urlopen(f"{base}/api/runs", timeout=3) as response:
                runs = json.load(response)
            self.assertEqual(runs["count"], 1)

            request = urllib.request.Request(f"{base}/api/runs", data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 405)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)

    def test_server_rejects_non_loopback_binding(self):
        self._make_attempt(6)
        with self.assertRaisesRegex(ValueError, "loopback"):
            server.build_server(
                self.root, host="0.0.0.0", port=0, static_root=self.static
            )


if __name__ == "__main__":
    unittest.main()
