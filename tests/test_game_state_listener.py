"""Execute the production Java listener against a controlled dungeon surface.

Only engine data containers are stubbed; readiness logic is compiled directly
from GameStateListener.java. No game process or graphics runtime is launched.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/CommunicationMod-1.2.1/src/main/java/communicationmod/GameStateListener.java"

STUBS = {
    "communicationmod/CommandExecutor.java": """
package communicationmod;
public class CommandExecutor { public static boolean isInDungeon() { return true; } }
""",
    "com/megacrit/cardcrawl/actions/GameActionManager.java": """
package com.megacrit.cardcrawl.actions;
import java.util.ArrayList;
public class GameActionManager {
    public enum Phase { WAITING_ON_USER, EXECUTING_ACTIONS }
    public Phase phase = Phase.EXECUTING_ACTIONS;
    public ArrayList<Object> preTurnActions = new ArrayList<>();
    public ArrayList<Object> actions = new ArrayList<>();
    public ArrayList<Object> cardQueue = new ArrayList<>();
}
""",
    "com/megacrit/cardcrawl/core/CardCrawlGame.java": """
package com.megacrit.cardcrawl.core;
public class CardCrawlGame {
    public enum GameMode { CHAR_SELECT }
    public static GameMode mode;
    public static Object mainMenuScreen;
}
""",
    "com/megacrit/cardcrawl/rooms/AbstractRoom.java": """
package com.megacrit.cardcrawl.rooms;
public class AbstractRoom {
    public enum RoomPhase { COMBAT, COMPLETE }
    public RoomPhase phase = RoomPhase.COMBAT;
    public static class Event { public float waitTimer; }
    public Event event = new Event();
}
""",
    "com/megacrit/cardcrawl/rooms/EventRoom.java": """
package com.megacrit.cardcrawl.rooms;
public class EventRoom extends AbstractRoom {}
""",
    "com/megacrit/cardcrawl/rooms/VictoryRoom.java": """
package com.megacrit.cardcrawl.rooms;
public class VictoryRoom extends AbstractRoom {
    public enum EventType { HEART }
    public EventType eType;
}
""",
    "com/megacrit/cardcrawl/neow/NeowRoom.java": """
package com.megacrit.cardcrawl.neow;
public class NeowRoom extends com.megacrit.cardcrawl.rooms.AbstractRoom {}
""",
    "com/megacrit/cardcrawl/dungeons/AbstractDungeon.java": """
package com.megacrit.cardcrawl.dungeons;
import java.util.ArrayList;
import com.megacrit.cardcrawl.actions.GameActionManager;
import com.megacrit.cardcrawl.rooms.AbstractRoom;
public class AbstractDungeon {
    public enum CurrentScreen { NONE, HAND_SELECT, GRID, DEATH, DOOR_UNLOCK, NO_INTERACT }
    public static CurrentScreen screen;
    public static boolean isScreenUp, isFadingOut, isFadingIn;
    public static GameActionManager actionManager = new GameActionManager();
    public static AbstractRoom room = new AbstractRoom();
    public static AbstractRoom getCurrRoom() { return room; }
    public static class Player { public boolean endTurnQueued; public int gold = 99; }
    public static Player player = new Player();
    public static class Monsters { public boolean areMonstersBasicallyDead() { return false; } }
    public static Monsters getMonsters() { return new Monsters(); }
    public static ArrayList<Object> effectList = new ArrayList<>(), effectsQueue = new ArrayList<>(),
        topLevelEffects = new ArrayList<>(), topLevelEffectsQueue = new ArrayList<>();
}
""",
    "communicationmod/ListenerBoundaryTest.java": """
package communicationmod;
import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.megacrit.cardcrawl.actions.GameActionManager;
public class ListenerBoundaryTest {
    static void expect(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
    static void open(AbstractDungeon.CurrentScreen screen) {
        AbstractDungeon.screen = screen;
        AbstractDungeon.isScreenUp = true;
        expect(GameStateListener.checkForDungeonStateChange(), "selection must open");
    }
    static void close() {
        GameStateListener.registerCommandExecution();
        AbstractDungeon.screen = AbstractDungeon.CurrentScreen.NONE;
        AbstractDungeon.isScreenUp = false;
        expect(!GameStateListener.checkForDungeonStateChange(), "closing screen must settle");
    }
    public static void main(String[] args) {
        GameStateListener.resetStateVariables();
        GameStateListener.signalTurnStart();
        AbstractDungeon.actionManager.actions.add(new Object());
        open(AbstractDungeon.CurrentScreen.HAND_SELECT);
        // Burst/Survivor closes one selection and opens the next while the
        // DiscardAction queue necessarily remains occupied waiting for input.
        for (int i = 0; i < 3; i++) {
            close();
            open(i == 1 ? AbstractDungeon.CurrentScreen.GRID : AbstractDungeon.CurrentScreen.HAND_SELECT);
            GameStateListener.registerCommandExecution();
            GameStateListener.registerStateChange();
            expect(GameStateListener.checkForDungeonStateChange(),
                "reopened selection must publish its selected-card/confirm state");
            expect(GameStateListener.isWaitingForCommand(), "confirmation must be actionable");
        }
        // The replacement draw following the LAST selection still has to
        // settle. Reopening must not disable the normal close-screen guard.
        close();
        for (int i = 0; i < 8; i++) {
            expect(!GameStateListener.checkForDungeonStateChange(), "queued draw cannot publish combat");
        }
        AbstractDungeon.actionManager.actions.clear();
        AbstractDungeon.actionManager.phase = GameActionManager.Phase.WAITING_ON_USER;
        expect(GameStateListener.checkForDungeonStateChange(), "settled draw must publish combat");
        // Empty queues immediately after close are insufficient: retain the
        // bounded update barrier, since the next draw may enqueue later.
        GameStateListener.registerCommandExecution();
        open(AbstractDungeon.CurrentScreen.HAND_SELECT);
        close();
        for (int i = 0; i < 3; i++) {
            expect(!GameStateListener.checkForDungeonStateChange(), "close barrier ended too early");
        }
        expect(GameStateListener.checkForDungeonStateChange(), "empty queues must eventually settle");
        System.out.println("listener transition checks passed");
    }
}
""",
}


class GameStateListenerBehaviorTests(unittest.TestCase):
    def test_reopened_selection_and_followup_draw_boundaries(self):
        executable = "javac.exe" if os.name == "nt" else "javac"
        local = ROOT / ".tools/jdk8/jdk8u492-b09/bin" / executable
        compiler = str(local) if local.is_file() else shutil.which("javac")
        if not compiler:
            self.fail("JDK required for executable listener regression (set PATH)")
        java = str(Path(compiler).with_name("java.exe" if os.name == "nt" else "java"))
        with tempfile.TemporaryDirectory(prefix="sts-listener-") as tmp:
            root = Path(tmp)
            sources = []
            for name, content in STUBS.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                sources.append(str(path))
            result = subprocess.run([compiler, "-encoding", "UTF-8", "-d", tmp, str(SOURCE), *sources],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            result = subprocess.run([java, "-cp", tmp, "communicationmod.ListenerBoundaryTest"],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
