# sts-cli Codex instructions

These instructions apply to work under this directory.

## Autonomous Slay the Spire goal

When the user asks Codex to play, clear, continue, retry, monitor, or autonomously operate Slay the Spire, read `AUTONOMOUS_GOAL.md` completely before taking any gameplay or process action. Treat every requirement in that file as part of the user's goal even when the Goal card or thread summary is shorter.

For an autonomous run, `fast-policy-v5` is the only authorized live policy. Do not run `autoplay_shared_v4.py` or use `shared_combat_v4.py` as the live controller. References to `shared-combat-v4` may remain in historical logs, progress notes, archived adapters, and negative tests; they are not permission to activate it.

Do not start or restart gameplay merely for a code-review, explanation, or diagnostic request. Process control is authorized only when the user asks to run, continue, or apply the autonomous gameplay goal.

Before changing strategy code, ensure no live `autoplay.py` process is issuing actions. After changing `bridge.py`, restart the CommunicationMod external process or the game so the new Python module is loaded. After changing `autoplay.py`, agent code, planners, or predictors, start a fresh controller process.
