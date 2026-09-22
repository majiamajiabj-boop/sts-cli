"""Read-only local dashboard for completed Slay the Spire attempts.

The server deliberately ignores live transport files and active attempt logs.
Only immutable per-attempt artifacts with an authoritative GAME_OVER result
and a controller exit receipt are eligible for the dashboard.
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import re
import threading
import time
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


API_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
RECENT_RUN_LIMIT = 10
ATTEMPT_COMPONENT = re.compile(r"^[A-Za-z0-9._-]{1,160}$")

CHARACTER_NAMES = {
    "IRONCLAD": "铁甲战士",
    "THE_SILENT": "静默猎手",
    "DEFECT": "故障机器人",
    "WATCHER": "观者",
}

STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
}


class ArtifactError(ValueError):
    """Raised when a completed-attempt artifact is not safe to display."""


def _finite_number(value: Any) -> float | None:
    if type(value) not in {int, float}:
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _nonnegative_int(value: Any, default: int = 0) -> int:
    return value if type(value) is int and value >= 0 else default


def _nonnegative_number(value: Any, default: float = 0.0) -> float:
    numeric = _finite_number(value)
    return numeric if numeric is not None and numeric >= 0 else default


def _model_advice_summary(audit: dict[str, Any] | None) -> dict[str, Any]:
    advice = audit.get("model_advice") if isinstance(audit, dict) else None
    available = isinstance(advice, dict)
    source = advice if available else {}
    latency = (
        source.get("remote_latency_ms")
        if isinstance(source.get("remote_latency_ms"), dict)
        else {}
    )
    adoption_rate = _finite_number(source.get("semantic_adoption_rate"))
    if adoption_rate is not None and not 0 <= adoption_rate <= 1:
        adoption_rate = None
    return {
        "available": available,
        "records": _nonnegative_int(source.get("records")),
        "remote_consultations": _nonnegative_int(
            source.get("remote_consultations")
        ),
        "remote_valid_recommendations": _nonnegative_int(
            source.get("remote_valid_recommendations")
        ),
        "semantic_choice_changes": _nonnegative_int(
            source.get("semantic_choice_changes")
        ),
        "semantic_adoption_rate": adoption_rate,
        "model_agreements": _nonnegative_int(source.get("model_agreements")),
        "effective_overrides": _nonnegative_int(
            source.get("effective_overrides")
        ),
        "fallbacks": _nonnegative_int(source.get("fallbacks")),
        "local_cache_hits": _nonnegative_int(source.get("local_cache_hits")),
        "average_latency_ms": _nonnegative_number(latency.get("average")),
        "maximum_latency_ms": _nonnegative_number(latency.get("maximum")),
        "estimated_cost_usd": _nonnegative_number(
            source.get("estimated_cost_usd")
        ),
    }


def _bool_keys(value: Any) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {
        "ruby": source.get("ruby") is True,
        "emerald": source.get("emerald") is True,
        "sapphire": source.get("sapphire") is True,
    }


def _safe_text(value: Any, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def _read_json(path: Path, *, max_bytes: int) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ArtifactError(f"cannot stat {path.name}") from exc
    if size <= 0 or size > max_bytes:
        raise ArtifactError(f"{path.name} has an invalid size")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read {path.name}") from exc
    if not isinstance(value, dict):
        raise ArtifactError(f"{path.name} is not an object")
    return value


def _compact_card_groups(cards: Any) -> list[dict[str, Any]]:
    if not isinstance(cards, list):
        return []
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        upgrades = _nonnegative_int(card.get("upgrades"))
        key = (
            _safe_text(card.get("id"), "unknown"),
            _safe_text(card.get("name"), _safe_text(card.get("id"), "未知卡牌")),
            upgrades,
            _safe_text(card.get("type"), "UNKNOWN"),
            _safe_text(card.get("rarity"), "UNKNOWN"),
            card.get("cost") if type(card.get("cost")) is int else None,
        )
        if key not in grouped:
            order.append(key)
            grouped[key] = {
                "id": key[0],
                "name": key[1],
                "upgrades": key[2],
                "type": key[3],
                "rarity": key[4],
                "cost": key[5],
                "count": 0,
            }
        grouped[key]["count"] += 1
    return [grouped[key] for key in order]


def _compact_relics(relics: Any) -> list[dict[str, Any]]:
    if not isinstance(relics, list):
        return []
    compact = []
    for relic in relics:
        if not isinstance(relic, dict):
            continue
        compact.append({
            "id": _safe_text(relic.get("id"), "unknown"),
            "name": _safe_text(
                relic.get("name"), _safe_text(relic.get("id"), "未知遗物")
            ),
            "counter": relic.get("counter")
            if type(relic.get("counter")) is int else None,
        })
    return compact


def _compact_potions(potions: Any) -> list[dict[str, Any]]:
    if not isinstance(potions, list):
        return []
    compact = []
    for potion in potions:
        if not isinstance(potion, dict):
            continue
        potion_id = _safe_text(potion.get("id"))
        if potion_id.lower() in {"potion slot", "potion_slot"}:
            continue
        compact.append({
            "id": potion_id or "unknown",
            "name": _safe_text(potion.get("name"), potion_id or "未知药水"),
            "slot": potion.get("slot") if type(potion.get("slot")) is int else None,
        })
    return compact


def _brief_finding(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    brief: dict[str, Any] = {}
    for field in (
        "kind",
        "severity",
        "reason",
        "audit_channel",
        "act",
        "floor",
        "phase",
        "turn",
        "before_seq",
        "resolved",
    ):
        item = value.get(field)
        if item is None or isinstance(item, (str, int, float, bool)):
            if item is not None:
                brief[field] = item
    missing = value.get("missing_evidence")
    if isinstance(missing, list):
        brief["missing_evidence"] = [
            item for item in missing[:6] if isinstance(item, str)
        ]
    return brief if brief else None


def _selected_item(action: dict[str, Any]) -> tuple[str | None, str | None]:
    selected = action.get("selected_target_id")
    if not isinstance(selected, str):
        return None, None
    for collection_name in ("hand_before", "potions_before"):
        collection = action.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            identifiers = {
                item.get("card_instance_id"),
                item.get("uuid"),
                item.get("potion_instance_id"),
            }
            if selected in identifiers:
                return _safe_text(item.get("name"), _safe_text(item.get("id"))), _safe_text(
                    item.get("id")
                )
    return None, selected


def _compact_action(action: Any) -> dict[str, Any] | None:
    if not isinstance(action, dict):
        return None
    kind = _safe_text(action.get("action"), _safe_text(action.get("final_action"), "unknown"))
    selected_name, selected_id = _selected_item(action)
    player = action.get("player_before") if isinstance(action.get("player_before"), dict) else {}
    outcome = action.get("actual_outcome") if isinstance(action.get("actual_outcome"), dict) else {}
    prediction = action.get("prediction") if isinstance(action.get("prediction"), dict) else {}
    labels = {
        "play": "打出卡牌",
        "end": "结束回合",
        "potion": "使用药水",
        "discard_potion": "丢弃药水",
    }
    return {
        "kind": kind,
        "label": labels.get(kind, kind or "未知行动"),
        "selected_name": selected_name,
        "selected_id": selected_id,
        "before_seq": action.get("before_seq")
        if type(action.get("before_seq")) is int else None,
        "after_seq": action.get("after_seq")
        if type(action.get("after_seq")) is int else None,
        "player_before": {
            "hp": player.get("current_hp")
            if type(player.get("current_hp")) is int else None,
            "max_hp": player.get("max_hp")
            if type(player.get("max_hp")) is int else None,
            "block": player.get("block") if type(player.get("block")) is int else None,
            "energy": player.get("energy")
            if type(player.get("energy")) is int else None,
        },
        "outcome": {
            "player_hp_loss": _nonnegative_int(outcome.get("player_hp_loss")),
            "player_hp_gain": _nonnegative_int(outcome.get("player_hp_gain")),
            "enemy_hp_loss": _nonnegative_int(outcome.get("enemy_hp_loss")),
        },
        "prediction": {
            "incoming_predicted": prediction.get("monsters_to_hero_predicted")
            if type(prediction.get("monsters_to_hero_predicted")) is int else None,
            "incoming_actual": prediction.get("monsters_to_hero_actual")
            if type(prediction.get("monsters_to_hero_actual")) is int else None,
        },
    }


def _compact_monster(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "id": _safe_text(value.get("id"), "unknown"),
        "name": _safe_text(value.get("name"), _safe_text(value.get("id"), "未知敌人")),
        "hp": value.get("current_hp") if type(value.get("current_hp")) is int else None,
        "max_hp": value.get("max_hp") if type(value.get("max_hp")) is int else None,
        "block": value.get("block") if type(value.get("block")) is int else None,
        "intent": _safe_text(value.get("intent"), "UNKNOWN"),
        "move_damage": value.get("move_adjusted_damage")
        if type(value.get("move_adjusted_damage")) is int else None,
        "move_hits": value.get("move_hits")
        if type(value.get("move_hits")) is int else None,
    }


def _compact_replay(replay: dict[str, Any] | None) -> dict[str, Any] | None:
    if replay is None:
        return None
    turns = []
    raw_turns = replay.get("turns") if isinstance(replay.get("turns"), list) else []
    for raw_turn in raw_turns[-3:]:
        if not isinstance(raw_turn, dict):
            continue
        raw_actions = raw_turn.get("actions") if isinstance(raw_turn.get("actions"), list) else []
        actions = [item for item in (_compact_action(action) for action in raw_actions) if item]
        first_action = raw_actions[0] if raw_actions and isinstance(raw_actions[0], dict) else {}
        first_player = first_action.get("player_before") if isinstance(first_action.get("player_before"), dict) else {}
        raw_monsters = first_action.get("monsters_before") if isinstance(first_action.get("monsters_before"), list) else []
        monsters = [item for item in (_compact_monster(monster) for monster in raw_monsters) if item]
        turns.append({
            "turn": raw_turn.get("turn") if type(raw_turn.get("turn")) is int else None,
            "complete": raw_turn.get("complete") is True,
            "first_state_seq": raw_turn.get("first_state_seq")
            if type(raw_turn.get("first_state_seq")) is int else None,
            "last_state_seq": raw_turn.get("last_state_seq")
            if type(raw_turn.get("last_state_seq")) is int else None,
            "player_at_start": {
                "hp": first_player.get("current_hp")
                if type(first_player.get("current_hp")) is int else None,
                "max_hp": first_player.get("max_hp")
                if type(first_player.get("max_hp")) is int else None,
                "block": first_player.get("block")
                if type(first_player.get("block")) is int else None,
                "energy": first_player.get("energy")
                if type(first_player.get("energy")) is int else None,
            },
            "monsters": monsters,
            "actions": actions,
        })
    return {
        "status": _safe_text(replay.get("status"), "unknown"),
        "replay_kind": _safe_text(replay.get("replay_kind"), "unknown"),
        "death_observed": replay.get("death_observed") is True,
        "terminal_cause": replay.get("terminal_cause"),
        "issue_count": _nonnegative_int(replay.get("issue_count")),
        "eligible_unknown_count": _nonnegative_int(
            replay.get("eligible_unknown_count")
        ),
        "turns": turns,
    }


class RunStore:
    """In-memory cached, read-only view over immutable attempt archives."""

    def __init__(self, repository_root: Path):
        self.repository_root = repository_root.resolve()
        self.attempt_root = (self.repository_root / "logs" / "attempts").resolve()
        if self.attempt_root.exists() and not self.attempt_root.is_dir():
            raise FileNotFoundError(f"attempt archive is missing: {self.attempt_root}")
        self._lock = threading.RLock()
        self._json_cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}

    def _cached_json(self, path: Path, *, max_bytes: int) -> dict[str, Any]:
        try:
            stat = path.stat()
        except OSError as exc:
            raise ArtifactError(f"cannot stat {path.name}") from exc
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._json_cache.get(path)
            if cached is not None and cached[0] == signature:
                return cached[1]
        value = _read_json(path, max_bytes=max_bytes)
        with self._lock:
            self._json_cache[path] = (signature, value)
        return value

    def _attempt_dirs(self) -> list[Path]:
        try:
            children = list(self.attempt_root.iterdir())
        except OSError:
            return []
        return [
            child
            for child in children
            if child.is_dir()
            and not child.is_symlink()
            and ATTEMPT_COMPONENT.fullmatch(child.name)
        ]

    def _core_record(self, directory: Path) -> dict[str, Any] | None:
        result_path = directory / "run-result.json"
        exit_path = directory / "controller-exit.json"
        if not result_path.is_file() or not exit_path.is_file():
            return None
        try:
            result = self._cached_json(result_path, max_bytes=2 * 1024 * 1024)
            controller_exit = self._cached_json(exit_path, max_bytes=512 * 1024)
        except ArtifactError:
            return None

        attempt_id = result.get("attempt_id")
        if (
            attempt_id != directory.name
            or controller_exit.get("attempt_id") != attempt_id
            or result.get("termination_kind") != "game_over"
            or result.get("authoritative_game_over") is not True
            or str(result.get("screen_type") or "").upper() != "GAME_OVER"
            or type(result.get("victory")) is not bool
            or type(result.get("heart_defeated")) is not bool
        ):
            return None
        for field in (
            "run_id",
            "seed",
            "character",
            "selection_id",
            "terminal_state_seq",
        ):
            if type(controller_exit.get(field)) is not type(result.get(field)) or controller_exit.get(field) != result.get(field):
                return None

        ended_at = _finite_number(controller_exit.get("observed_at"))
        if ended_at is None:
            try:
                ended_at = exit_path.stat().st_mtime
            except OSError:
                ended_at = 0.0
        selection = result.get("selection") if isinstance(result.get("selection"), dict) else {}
        started_at = _finite_number(selection.get("created_at"))
        deck = result.get("deck") if isinstance(result.get("deck"), list) else []
        relics = result.get("relics") if isinstance(result.get("relics"), list) else []
        potions = _compact_potions(result.get("potions"))
        observed_max_act = result.get("observed_max_act")
        if type(observed_max_act) is not int:
            observed_max_act = result.get("act") if type(result.get("act")) is int else 0

        return {
            "attempt_id": attempt_id,
            "run_id": _safe_text(result.get("run_id")),
            # JavaScript numbers cannot safely represent every Slay the Spire seed.
            "seed": str(result.get("seed")),
            "character": _safe_text(result.get("character"), "UNKNOWN"),
            "character_name": CHARACTER_NAMES.get(
                _safe_text(result.get("character")),
                _safe_text(result.get("character"), "未知角色"),
            ),
            "ascension_level": result.get("ascension_level")
            if type(result.get("ascension_level")) is int else None,
            "run_type": _safe_text(result.get("run_type")),
            "goal_mode": _safe_text(result.get("goal_mode")),
            "policy_version": _safe_text(result.get("policy_version")),
            "act": result.get("act") if type(result.get("act")) is int else 0,
            "observed_max_act": observed_max_act,
            "floor": result.get("floor") if type(result.get("floor")) is int else 0,
            "current_hp": result.get("current_hp")
            if type(result.get("current_hp")) is int else None,
            "max_hp": result.get("max_hp")
            if type(result.get("max_hp")) is int else None,
            "victory": result.get("victory"),
            "heart_defeated": result.get("heart_defeated"),
            "keys": _bool_keys(result.get("keys")),
            "actions": _nonnegative_int(result.get("actions")),
            "deck_count": len(deck),
            "relic_count": len(relics),
            "potion_count": len(potions),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": max(0.0, ended_at - started_at)
            if started_at is not None else None,
            "controller_exit_status": _safe_text(
                controller_exit.get("controller_exit_status"), "unknown"
            ),
            "controller_exit_code": controller_exit.get("exit_code")
            if type(controller_exit.get("exit_code")) is int else None,
            "archive_name": directory.name,
        }

    def _audit(self, directory: Path, attempt_id: str) -> dict[str, Any] | None:
        path = directory / "run-audit.json"
        if not path.is_file():
            return None
        try:
            audit = self._cached_json(path, max_bytes=8 * 1024 * 1024)
        except ArtifactError:
            return None
        return audit if audit.get("attempt_id") == attempt_id else None

    def _audit_summary(
        self, directory: Path, attempt_id: str
    ) -> dict[str, Any]:
        audit = self._audit(directory, attempt_id)
        if audit is None:
            return {
                "status": "missing",
                "release_gate_passed": False,
                "issue_count": None,
                "review_finding_count": None,
                "eligible_unknown_count": None,
                "oracle_disagreement_count": None,
                "death_observed": None,
                "death_replay_status": "missing",
                "model": _model_advice_summary(None),
            }
        death_replay = audit.get("death_replay") if isinstance(audit.get("death_replay"), dict) else {}
        return {
            "status": _safe_text(audit.get("audit_status"), "unknown"),
            "release_gate_passed": audit.get("release_gate_passed") is True,
            "issue_count": audit.get("issue_count")
            if type(audit.get("issue_count")) is int else None,
            "review_finding_count": audit.get("review_finding_count")
            if type(audit.get("review_finding_count")) is int else None,
            "eligible_unknown_count": audit.get("eligible_unknown_count")
            if type(audit.get("eligible_unknown_count")) is int else None,
            "oracle_disagreement_count": audit.get("oracle_disagreement_count")
            if type(audit.get("oracle_disagreement_count")) is int else None,
            "death_observed": audit.get("death_observed")
            if type(audit.get("death_observed")) is bool else None,
            "death_replay_status": _safe_text(death_replay.get("status"), "missing"),
            "model": _model_advice_summary(audit),
        }

    def recent_runs(self, limit: int = RECENT_RUN_LIMIT) -> dict[str, Any]:
        limit = max(1, min(RECENT_RUN_LIMIT, int(limit)))
        records = []
        for directory in self._attempt_dirs():
            core = self._core_record(directory)
            if core is not None:
                records.append((directory, core))
        records.sort(key=lambda item: (item[1]["ended_at"], item[1]["attempt_id"]), reverse=True)
        selected = records[:limit]
        runs = []
        for rank, (directory, core) in enumerate(selected, start=1):
            runs.append({
                **core,
                "rank": rank,
                "audit": self._audit_summary(directory, core["attempt_id"]),
            })

        floors = [run["floor"] for run in runs]
        actions = [run["actions"] for run in runs]
        deepest = max(
            runs,
            key=lambda run: (run["observed_max_act"], run["floor"]),
            default=None,
        )
        character_counts = Counter(run["character"] for run in runs)
        return {
            "api_version": API_VERSION,
            "generated_at": time.time(),
            "source": "completed_attempt_archives",
            "count": len(runs),
            "limit": limit,
            "stats": {
                "victories": sum(run["victory"] for run in runs),
                "heart_victories": sum(run["heart_defeated"] for run in runs),
                "act4_entries": sum(run["observed_max_act"] >= 4 for run in runs),
                "average_floor": round(sum(floors) / len(floors), 1) if floors else 0,
                "average_actions": round(sum(actions) / len(actions), 1) if actions else 0,
                "deepest": {
                    "act": deepest["observed_max_act"],
                    "floor": deepest["floor"],
                    "attempt_id": deepest["attempt_id"],
                } if deepest else None,
                "characters": dict(character_counts),
            },
            "runs": runs,
        }

    def run_detail(self, attempt_id: str) -> dict[str, Any] | None:
        if not ATTEMPT_COMPONENT.fullmatch(attempt_id):
            return None
        directory = self.attempt_root / attempt_id
        if not directory.is_dir() or directory.is_symlink():
            return None
        core = self._core_record(directory)
        if core is None:
            return None
        try:
            result = self._cached_json(
                directory / "run-result.json", max_bytes=2 * 1024 * 1024
            )
        except ArtifactError:
            return None
        audit = self._audit(directory, attempt_id)
        replay = None
        replay_path = directory / "death-replay.json"
        if replay_path.is_file():
            try:
                candidate = self._cached_json(replay_path, max_bytes=8 * 1024 * 1024)
                if candidate.get("attempt_id") == attempt_id:
                    replay = candidate
            except ArtifactError:
                replay = None

        issues = []
        findings = []
        if audit is not None:
            raw_issues = audit.get("issues") if isinstance(audit.get("issues"), list) else []
            raw_findings = audit.get("review_findings") if isinstance(audit.get("review_findings"), list) else []
            issues = [item for item in (_brief_finding(value) for value in raw_issues[:8]) if item]
            findings = [item for item in (_brief_finding(value) for value in raw_findings[:8]) if item]

        selection = result.get("selection") if isinstance(result.get("selection"), dict) else {}
        return {
            "api_version": API_VERSION,
            "run": {
                **core,
                "audit": self._audit_summary(directory, attempt_id),
            },
            "build": {
                "deck": _compact_card_groups(result.get("deck")),
                "relics": _compact_relics(result.get("relics")),
                "potions": _compact_potions(result.get("potions")),
            },
            "selection": {
                "reason": _safe_text(selection.get("reason")),
                "algorithm": _safe_text(selection.get("algorithm")),
                "created_at": _finite_number(selection.get("created_at")),
                "decision_hash": _safe_text(result.get("decision_hash")),
                "controller_hash": _safe_text(result.get("controller_hash")),
                "strategy_revision": _safe_text(result.get("strategy_revision")),
            },
            "audit_detail": {
                "issues": issues,
                "review_findings": findings,
            },
            "death_replay": _compact_replay(replay),
        }


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "STSCompletedRunViewer/1.0"

    @property
    def viewer_server(self) -> "ViewerHTTPServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, _format: str, *args: Any) -> None:
        # Keep the service silent and file-free during normal local use.
        return

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, value: Any) -> None:
        body = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _not_found(self) -> None:
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _serve_static(self, relative_name: str) -> None:
        path = self.viewer_server.static_root / relative_name
        if not path.is_file() or path.parent != self.viewer_server.static_root:
            self._not_found()
            return
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "static_file_unreadable"}
            )
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/javascript"}:
            mime = f"{mime}; charset=utf-8"
        self._send_bytes(
            HTTPStatus.OK,
            body,
            mime,
            cache_control="no-cache",
        )

    def _handle_get(self) -> None:
        request_path = unquote(urlsplit(self.path).path)
        if request_path in STATIC_FILES:
            self._serve_static(STATIC_FILES[request_path])
            return
        if request_path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "mode": "read_only",
                    "api_version": API_VERSION,
                },
            )
            return
        if request_path == "/api/runs":
            self._send_json(HTTPStatus.OK, self.viewer_server.store.recent_runs())
            return
        prefix = "/api/runs/"
        if request_path.startswith(prefix):
            attempt_id = request_path[len(prefix):]
            detail = self.viewer_server.store.run_detail(attempt_id)
            if detail is None:
                self._not_found()
            else:
                self._send_json(HTTPStatus.OK, detail)
            return
        self._not_found()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._handle_get()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._handle_get()

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed


class ViewerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: RunStore,
        static_root: Path,
    ):
        self.store = store
        self.static_root = static_root.resolve()
        super().__init__(address, ViewerHandler)


def build_server(
    repository_root: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    static_root: Path | None = None,
) -> ViewerHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("viewer may only bind to the local loopback interface")
    static = static_root or Path(__file__).resolve().parent / "static"
    if not static.is_dir():
        raise FileNotFoundError(f"static directory is missing: {static}")
    return ViewerHTTPServer((host, port), RunStore(repository_root), static)


def main() -> None:
    parser = argparse.ArgumentParser(description="只读查看最近十局已完成对局")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="sts-cli 仓库目录",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("viewer may only bind to the local loopback interface")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    server = build_server(args.root, host=args.host, port=args.port)
    shown_host = "127.0.0.1" if args.host in {"localhost", "::1"} else args.host
    print(f"Slay the Spire 对局看板：http://{shown_host}:{server.server_port}")
    print("只读模式：仅展示已经完成并归档的对局。按 Ctrl+C 停止。")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
