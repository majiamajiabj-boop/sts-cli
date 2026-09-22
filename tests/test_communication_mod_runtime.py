import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMAND_EXECUTOR = (
    ROOT
    / "src"
    / "CommunicationMod-1.2.1"
    / "src"
    / "main"
    / "java"
    / "communicationmod"
    / "CommandExecutor.java"
)
CHOICE_SCREEN_UTILS = COMMAND_EXECUTOR.with_name("ChoiceScreenUtils.java")


class CommunicationModRuntimeTests(unittest.TestCase):
    def test_wheel_intro_prefers_private_spin_state_machine_over_dialog_buttons(self):
        source = CHOICE_SCREEN_UTILS.read_text(encoding="utf-8")
        make_choice = source[source.index("public static void makeEventChoice") :]
        spin_branch = make_choice.index("isWheelSpinScreen")
        dialog_branch = make_choice.index("activeButtons.get(choice).pressed")
        self.assertLess(spin_branch, dialog_branch)
        self.assertIn('"buttonPressed", true', make_choice)
        self.assertIn("invokeWheelButtonEffect(wheel);", make_choice)
        self.assertIn("invokeWheelButtonEffect(wheel)", make_choice)
        self.assertIn('"screen")', make_choice)
        self.assertIn('"INTRO".equalsIgnoreCase', make_choice)
        self.assertIn("((Enum<?>) screen).name()", make_choice)

    def test_start_and_resume_force_fast_automation_mode(self):
        source = COMMAND_EXECUTOR.read_text(encoding="utf-8")
        self.assertEqual(2, source.count("Settings.FAST_MODE = true;"))
        converter = COMMAND_EXECUTOR.with_name("GameStateConverter.java")
        self.assertIn(
            'response.put("fast_mode", Settings.FAST_MODE);',
            converter.read_text(encoding="utf-8"),
        )

    def test_noncombat_state_waits_for_action_manager_settlement(self):
        listener = (
            COMMAND_EXECUTOR.with_name("GameStateListener.java")
        )
        source = listener.read_text(encoding="utf-8")
        helper_start = source.index(
            "private static boolean isActionManagerReadyForCommand()"
        )
        helper_end = source.index(
            "    /**\n     * Detects whether the game state is stable",
            helper_start,
        )
        helper = source[helper_start:helper_end]
        self.assertIn("GameActionManager.Phase.WAITING_ON_USER", helper)
        self.assertIn("preTurnActions.isEmpty()", helper)
        self.assertIn("actions.isEmpty()", helper)
        self.assertIn("cardQueue.isEmpty()", helper)
        gate = source.index(
            "if (!inCombat && !isActionManagerReadyForCommand())"
        )
        death_check = source.index(
            "newScreen == AbstractDungeon.CurrentScreen.DEATH"
        )
        self.assertLess(gate, death_check)

    def test_player_state_exposes_authoritative_orb_capacity(self):
        converter = COMMAND_EXECUTOR.with_name("GameStateConverter.java")
        source = converter.read_text(encoding="utf-8")
        self.assertIn(
            'jsonPlayer.put("max_orbs", player.maxOrbs);',
            source,
        )

    def test_frozen_jar_contains_fast_mode_binding(self):
        with zipfile.ZipFile(ROOT / "CommunicationMod.jar") as archive:
            bytecode = archive.read(
                "communicationmod/CommandExecutor.class"
            )
        self.assertIn(b"FAST_MODE", bytecode)
        with zipfile.ZipFile(ROOT / "CommunicationMod.jar") as archive:
            converter = archive.read(
                "communicationmod/GameStateConverter.class"
            )
        self.assertIn(b"fast_mode", converter)
        self.assertIn(b"max_orbs", converter)
        self.assertIn(
            b"com.megacrit.cardcrawl.events.city.TheLibrary", converter
        )
        with zipfile.ZipFile(ROOT / "CommunicationMod.jar") as archive:
            listener = archive.read(
                "communicationmod/GameStateListener.class"
            )
        self.assertIn(
            b"combatScreenCloseUpdatesRemaining", listener
        )
        self.assertIn(
            b"COMBAT_SCREEN_CLOSE_SETTLE_UPDATES", listener
        )


if __name__ == "__main__":
    unittest.main()
