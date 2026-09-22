import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import assistant_paths as paths
from assistant_advisor import Advice, AdviceSession, FrameError, frame_token, recommend
from assistant_runtime import ModeLock, advisor_command, start_advisor, start_auto


def envelope(state, seq=1):
    return {"schema_version": 1, "mode": "advisor", "session": "offline-test", "state_seq": seq,
            "observed_at": time.time(), "ready": True, "state": state}


def synthetic_state(screen="REST"):
    game = {"class": "IRONCLAD", "current_hp": 20, "max_hp": 80, "floor": 5, "act": 1,
            "gold": 99, "seed": 1, "ascension_level": 0, "relics": [], "deck": [], "potions": [], "map": [],
            "screen_type": screen, "is_screen_up": True, "room_phase": "COMPLETE", "room_type": "RestRoom",
            "choice_list": ["rest", "smith"], "screen_state": {"has_rested": False, "rest_options": ["rest", "smith"]}}
    if screen == "COMBAT_REWARD":
        game.update(room_type="MonsterRoom", choice_list=["gold"], screen_state={"rewards": [{"reward_type": "GOLD", "gold": 20}]})
    if screen == "MAP":
        nodes = [{"x": 0, "y": 0, "symbol": "M", "children": [{"x": 0, "y": 1}]}, {"x": 0, "y": 1, "symbol": "R", "children": []}]
        game.update(room_type="MonsterRoom", choice_list=["0"], map=nodes, screen_state={"current_node": nodes[0], "next_nodes": [nodes[1]], "boss_available": False})
    return {"in_game": True, "ready_for_command": True, "available_commands": ["choose", "proceed"], "game_state": game}


def replay_scenes():
    found = {}
    def walk(value):
        if isinstance(value, dict):
            raw = value.get("game_state")
            if isinstance(raw, dict) and raw.get("screen_type"):
                found.setdefault(raw["screen_type"], {"in_game": True, "ready_for_command": True,
                    "available_commands": value.get("available_commands", ["choose"]), "game_state": raw})
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for name in ("attempt_failure_frames.json", "survival_system_frames.json"):
        path = paths.ROOT / "test_fixtures" / name
        if path.is_file():
            walk(json.loads(path.read_text(encoding="utf-8")))
    return found


class PortablePathsTests(unittest.TestCase):
    def test_unicode_space_library_manifest_and_mods(self):
        with tempfile.TemporaryDirectory(prefix="尖塔 路径 ") as tmp:
            base = Path(tmp)
            steam, library = base / "Steam 客户端", base / "游戏库 Space"
            (steam / "steamapps").mkdir(parents=True)
            (steam / "steamapps/libraryfolders.vdf").write_text('"libraryfolders" { "1" { "path" "' + str(library).replace("\\", "\\\\") + '" } }', encoding="utf-8")
            apps = library / "steamapps"
            game = apps / "common/尖塔 游戏"
            (game / "jre/bin").mkdir(parents=True)
            (apps / "appmanifest_646570.acf").write_text('"installdir" "尖塔 游戏"', encoding="utf-8")
            for p in (game / "desktop-1.0.jar", game / "jre/bin/java.exe"):
                p.touch()
            for name, appid in paths.WORKSHOP.items():
                p = apps / "workshop/content/646570" / appid / (name + ".jar")
                p.parent.mkdir(parents=True)
                p.touch()
            libraries = paths.steam_libraries([steam])
            self.assertIn(library.resolve(), libraries)
            self.assertEqual(paths.discover_games(libraries), [game.resolve()])
            info = paths.installation(game, settings={}, libraries=libraries)
            self.assertEqual(info.errors(), [])
            command = advisor_command(info, base / "建议 状态.json")
            self.assertIn(str(info.mts), command)
            self.assertIn("尖塔", command[2])

    def test_missing_dependencies_chinese_and_manual_mod_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = paths.installation(tmp, settings={"basemod": str(Path(tmp) / "单独.jar")}, libraries=[])
            self.assertEqual(len(info.errors()), 4)
            self.assertIn("未找到游戏", info.errors()[0])
            self.assertEqual(info.basemod.name, "单独.jar")

    def test_unicode_java_properties(self):
        self.assertEqual(paths.java_property("C:/游戏 A/😀"), r"C\:/\u6e38\u620f A/\ud83d\ude00")

    def test_mode_lock_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = ModeLock(tmp).acquire()
            code = "from assistant_runtime import ModeLock; import sys; ModeLock(sys.argv[1]).acquire()"
            result = subprocess.run([sys.executable, "-c", code, tmp], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            lock.close()
            result = subprocess.run([sys.executable, "-c", code, tmp], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_game_prevents_install_and_launch(self):
        with patch("assistant_runtime.running_game_processes", return_value=[{"ProcessId": 123}]), patch("assistant_runtime.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "123"):
                start_advisor({})
            spawn.assert_not_called()


class AdvisorTests(unittest.TestCase):
    def test_synthetic_rest_and_rewards(self):
        for scene in ("REST", "COMBAT_REWARD", "MAP"):
            with self.subTest(scene=scene):
                result = recommend(envelope(synthetic_state(scene)))
                self.assertTrue(result.action)
                self.assertIn("生命", result.reason)

    def test_historical_replay_all_available_scenes_never_executes(self):
        scenes = replay_scenes()
        required = {"NONE", "CARD_REWARD", "SHOP_SCREEN", "BOSS_REWARD", "GRID", "HAND_SELECT", "EVENT"}
        if not scenes:
            self.skipTest("历史回放数据未安装；请在源码工作区执行该测试")
        self.assertTrue(required <= scenes.keys())
        import spirecomm.communication.action as actions
        from contextlib import ExitStack
        with ExitStack() as stack:
            spies = []
            for cls in vars(actions).values():
                if isinstance(cls, type) and issubclass(cls, actions.Action) and "execute" in cls.__dict__:
                    spies.append(stack.enter_context(patch.object(cls, "execute", side_effect=AssertionError("顾问禁止执行动作"))))
            stack.enter_context(patch("socket.socket", side_effect=AssertionError("顾问不应联网")))
            for scene in sorted(required):
                with self.subTest(scene=scene):
                    state = copy.deepcopy(scenes[scene])
                    # Trace compaction renames UUID; restore only the serialized ID.
                    def restore(value):
                        if isinstance(value, dict):
                            if "card_instance_id" in value:
                                value.setdefault("uuid", value["card_instance_id"])
                            for item in list(value.values()): restore(item)
                        elif isinstance(value, list):
                            for item in value: restore(item)
                    restore(state)
                    state["game_state"].setdefault("map", [])
                    raw = state["game_state"]
                    screen = raw.get("screen_state") or {}
                    if "choose" in state["available_commands"] and "choice_list" not in raw:
                        # Compacted trace omits labels; restore the visible domain.
                        raw["choice_list"] = [c.get("name", str(i)) for i, c in enumerate(screen.get("cards", screen.get("options", [])))]
                    if scene == "EVENT":
                        screen.setdefault("event_name", screen.get("event_id", ""))
                        screen.setdefault("body_text", "")
                    before = copy.deepcopy(state)
                    result = recommend(envelope(state))
                    self.assertTrue(result.action)
                    self.assertTrue(result.target)
                    self.assertTrue(result.reason)
                    self.assertEqual(state, before)
            for spy in spies:
                spy.assert_not_called()

    def test_stale_busy_bad_source_and_clock_clear_advice(self):
        session = AdviceSession()
        good = envelope(synthetic_state())
        session.observe(good)
        session.advice = Advice(session.token, "old", "", "", "", {})
        for key, value in (("observed_at", time.time() - 10), ("ready", False), ("mode", "auto"), ("state_seq", None), ("observed_at", time.time()+20)):
            with self.subTest(key=key, value=value):
                session.observe(good)
                session.advice = Advice(session.token, "old", "", "", "", {})
                session.observe({**good, key: value})
                self.assertIsNone(session.advice)

    def test_content_changes_invalidate_even_if_sequence_reused(self):
        frame = envelope(synthetic_state())
        token = frame_token(frame)
        frame["state"]["game_state"]["gold"] += 1
        self.assertNotEqual(token, frame_token(frame))

    def test_delayed_results_cannot_reappear_after_new_state(self):
        ready, release = threading.Event(), threading.Event()
        def calculate(frame):
            ready.set()
            release.wait(3)
            return Advice(frame_token(frame), "old", "", "", "", {})
        session = AdviceSession(calculate)
        session.observe(envelope(synthetic_state()))
        session.start_pending()
        self.assertTrue(ready.wait(1))
        session.observe(envelope(synthetic_state(), seq=2))
        release.set()
        deadline = time.monotonic()+2
        while session.completed.empty() and time.monotonic()<deadline:
            time.sleep(.01)
        session.collect()
        self.assertIsNone(session.advice)
        self.assertFalse(session.busy)

    def test_unsupported_character_is_explicit(self):
        frame = envelope(synthetic_state())
        frame["state"]["game_state"]["class"] = "WATCHER"
        with self.assertRaisesRegex(FrameError, "此角色暂无建议"):
            recommend(frame)

    def test_local_advisor_has_no_controller_import(self):
        import ast
        tree = ast.parse((paths.ROOT / "assistant_advisor.py").read_text(encoding="utf-8"))
        forbidden = {"bridge", "stsctl", "autoplay", "subprocess", "socket", "deepseek_macro"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(forbidden.intersection(n.name.split(".")[0] for n in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], forbidden)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, {"execute", "send_message", "write_text", "write_bytes"})


if __name__ == "__main__":
    unittest.main()
