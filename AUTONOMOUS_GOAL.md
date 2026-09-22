# Fully autonomous Slay the Spire goal

## Objective and completion condition

Use the controller in this directory to complete the standard game prerequisites and then enter Act 4 and defeat the Corrupt Heart. A Heart flag alone is not sufficient. Completion requires one schema-v2 `fast-policy-v5`, standard A0, authoritative `GAME_OVER` result with both `victory=true` and `heart_defeated=true`, exact attempt/run/seed/character/hash/selection/terminal-sequence binding across all per-attempt artifacts, a clean outer controller exit, and a strict audit/review release gate with no issue, review finding, eligible unknown, or independent-oracle disagreement.

An Act 3 victory, unlocking the key system, reaching Act 4, or merely seeing the Heart is progress, not completion.

The current protocol supports `IRONCLAD`, `THE_SILENT`, and `DEFECT`. It does not currently support Watcher. Never claim Watcher completion without first implementing and verifying protocol support in a separately authorized code change.

## Unattended-operation contract

The user does not operate the game UI. Codex must autonomously:

- inspect and, when needed, launch or restart the game and CommunicationMod bridge;
- leave a death screen, return to the main menu, select a supported character, and start each run;
- choose Neow rewards, cards, map nodes, events, shops, campfires, boss rewards, potions, and keys;
- play every combat turn through the structured protocol;
- read results, diagnose normal failures, and start fresh attempts;
- continue until the completion condition is met.

Do not ask the user to click, select, play a card, start a run, or retry. A normal death, controller exit, stale-state rejection, receipt timeout, or need to start a new seed is not a user blocker.

Ask for user involvement only when new authority is required, Windows denies a necessary scoped process action, the game fails to launch after three verified attempts, or `state.json` still fails to update after three verified bridge/game restarts.

## Authoritative policy and protocol

- Live policy: `fast-policy-v5`.
- Live single-attempt entry point: `campaign_attempt.py run`. It alone creates a selector-backed selection, issues START, invokes `autoplay_runner.py`, and supervises `autoplay.py`.
- Protocol version: 2.
- Every state-changing request must carry `policy_version=fast-policy-v5` and bind the latest `state_seq`, `decision_id`, and `phase`.
- Choices bind `option_id`; cards bind `card_instance_id`; targeted actions bind `enemy_instance_id`; potions bind `potion_instance_id`.
- Never use display names or remembered list indexes as authoritative targets.
- Never run `autoplay_shared_v4.py` or activate `shared_combat_v4.py`.
- Never change the live policy back to `shared-combat-v4`.
- Do not send raw, unbound gameplay commands around `bridge.py`.

Before beginning or resuming unattended play, verify:

1. `autoplay.py` declares `POLICY_VERSION = "fast-policy-v5"`.
2. `bridge.py` and `stsctl.py` declare `REQUIRED_POLICY_VERSION = "fast-policy-v5"`.
3. `stsctl.bound_payload` includes the required policy version.
4. `agent.py` routes combat decisions through `FastCombatPlanner`.
5. No old `autoplay.py` or `autoplay_shared_v4.py` controller is still issuing actions.
6. `decision_case_replay.py` is clear for the current decision hash.
7. `freeze-manifest.json` was created after the complete test suite and `git diff --check` passed, and still validates against the current source tree.

```powershell
python -m unittest discover -p "test_*.py"
```

If verification fails, stop before issuing gameplay actions and fix only the verified implementation problem. Re-run the full suite afterward.

## Process lifecycle

Run these examples from the repository root with the configured developer Python runtime:

```text
python
```

The game launcher is:

```powershell
python launch_game.py
```

For a normal interactive cold start, use `quick-start.cmd`. It starts the same
provenance-bound launcher in the background and returns as soon as the exact
protocol-v2 bridge and a fresh authoritative state are ready.

CommunicationMod launches `bridge.py` as its attached external process. Do not run `bridge.py` as an unrelated standalone terminal process because it expects CommunicationMod JSON on standard input.

Before stopping a process, inspect its full command line and resolve the exact PID. Stop only processes whose command lines unambiguously match this workspace's `autoplay.py`, `autoplay_shared_v4.py`, `bridge.py`, `launch_game.py`, or ModTheSpire game instance. Never broadly stop all Python or Java processes.

Changes to `bridge.py` require restarting the CommunicationMod external process or restarting the game. Changes to `autoplay.py`, `agent.py`, `combat_planner.py`, or `combat_predictor.py` require a fresh `autoplay.py` process. Python processes do not automatically reload edited modules.

Wait for `state.json` to update and for the bridge to expose protocol version 2 before controlling the game.

## Run orchestration

Read the authoritative progression flags on every new-run decision. Do not rely only on historical notes.

1. If `silent_third_act_win` is false, run `THE_SILENT` at A0 until it becomes true.
2. If `defect_third_act_win` is false, run `DEFECT` at A0 until it becomes true.
3. If `ironclad_third_act_win` is false, run `IRONCLAD` at A0 until it becomes true.
4. Once the key system is unlocked, use `campaign_selector.py` to choose among all three supported characters from a homogeneous HEART/A0/standard/decision-hash cohort, then pursue all three keys and the Heart. Do not compare legacy records, different decision hashes, unlock runs, or operational errors as losses.

Before the first attempt for a changed source tree, obtain the current hashes, replay the fixed/current DecisionCases, and create the immutable verification checkpoint:

```powershell
$decisionHash = & python -c "import sys; sys.path.insert(0, '.'); import autoplay; print(autoplay.DECISION_HASH)"
$controllerHash = & python -c "import sys; sys.path.insert(0, '.'); import autoplay; print(autoplay.CONTROLLER_HASH)"
python decision_case_replay.py --decision-hash $decisionHash --write decision-case-replay.json
python freeze_manifest.py --decision-hash $decisionHash --controller-hash $controllerHash --decision-case-replay decision-case-replay.json --write freeze-manifest.json
```

`quick-campaign.cmd` is the fast equivalent for a complete new attempt. It
runs or reuses the exact-source static preflight before launching the game,
then performs only live runtime binding on the critical path and starts the
same authoritative `campaign_attempt.py run` entry point. It returns after a
new attempt ID is bound to an authoritative in-game state while the supervised
attempt continues in the background. A cached preflight never bypasses a
check: policy or source drift invalidates it, and Java/bridge provenance is
always recaptured immediately before START.

For every changed decision/controller hash, the selector first enforces one fixed six-run comparison subcohort in deterministic round-robin order: `IRONCLAD, THE_SILENT, DEFECT, IRONCLAD, THE_SILENT, DEFECT`. A structurally complete game with only P1/unknown audit findings counts toward this character quota but never becomes release-eligible or updates the Thompson posterior. Do not change strategy code inside a six-run subcohort; aggregate the completed subcohort and make at most a small set of repeated, evidence-backed changes afterward. An explicitly requested `--p0-only-batch` may run four such subcohorts consecutively under the same frozen hash (24 attempts total); each subcohort remains bound to the same deterministic sequence, and P0/protocol/operational evidence still stops the batch. Once the matrix is complete, the selector retains its longer-term bootstrap target of three release-eligible Heart attempts per character and then uses Beta-Thompson sampling. A character absent from the last six eligible attempts is selected by the starvation guard. `campaign_attempt.py` calls this selector directly; never hand-write a selection or call START around it.

On `GAME_OVER`, preserve the screen until `autoplay_runner.py` has written the unique ordered `terminal_result -> run_audit -> controller_exit` chain and verified the real stdout/stderr sidecars. A clean non-Heart attempt may then use `campaign_attempt.py return-to-main-menu`. An issues/inconclusive attempt must first be diagnosed and repaired under a new passing freeze, then use the explicit maintenance return path; that path records `cohort_eligible=false` and can never rehabilitate the failed attempt. The only exception is an explicitly user-requested `--p0-only-batch`: a structurally complete `issues` or `inconclusive` audit with clear protocol/mechanics dimensions, intact evidence, zero protocol/operational events, and no explicit P0 may leave through the recorded `p0_only_validation` transition and advance only the bounded 24-attempt test matrix. It remains permanently non-release-eligible and does not update the posterior. An inconclusive protocol/mechanics dimension or any P0 still blocks. Never relaunch raw `autoplay.py` on an initial `GAME_OVER` screen.

Start target IDs are:

- `run:IRONCLAD:a0:standard`
- `run:THE_SILENT:a0:standard`
- `run:DEFECT:a0:standard`

Run exactly one complete attempt through the orchestrator:

```powershell
python campaign_attempt.py run --decision-hash $decisionHash --controller-hash $controllerHash --max-actions 5000
```

Never run multiple live controllers or a validation controller alongside the live controller.

At the end of each attempt, read `run-result.json` and the relevant new records in `autoplay.log`. Verify that newly generated records say `fast-policy-v5`. Record at least the character, act, floor, HP, victory, Heart result, keys, deck, relics, action count, and primary failure cause. Historical v4 log records are not evidence that the new run used v4; distinguish records by timestamp and sequence.

Trace retention is bounded: unbound global records fail closed at 8 MiB and
each attempt segment fails closed at 128 MiB without truncating evidence. The
DecisionCase corpus is independently bounded at 128 MiB. Post-run replay uses
the compact schema-3 evidence bundle and never rescans the historical
gigabyte-scale root trace; an overflow is an operational stop, not a reason to
silently rotate or delete the prefix.

For schema-v2 attempts, run `strategy_audit.py` with both the attempt's `decision_hash` and `attempt_id` so every finding remains bound to one run and seed:

```powershell
$attempt = Get-Content -Raw run-result.json | ConvertFrom-Json
python strategy_audit.py --trace logs\attempts\$($attempt.attempt_id)\autoplay.log --decision-hash $attempt.decision_hash --attempt-id $attempt.attempt_id --require-clear
```

Any non-clear audit, protocol event, active unsupported mechanism, eligible unknown, oracle disagreement, incomplete candidate surface, or incomplete death/Act4 replay stops the next launch immediately in normal autonomous play. In an explicitly requested `--p0-only-batch`, complete P1/unknown evidence may advance only the fixed test matrix under the narrow transition above; it never passes the release gate. Treat damage underprediction, an end turn with affordable mitigation against predicted loss, or an unexplained play into a doomed target as a review signal. Do not mix old-schema findings or other attempt IDs into the current report.

Use a new random run after failure. Do not reload a seed to undo choices or deaths.

## Combat correctness

Recompute every decision from the latest authoritative state. Do not reuse a previous state's hand or target indexes.

For every character and enemy, account for all serialized effects that can materially change damage, survival, target priority, or end-of-turn state, including:

- current HP, block, intangible, plated armor, buffer, artifact, flight, thorns, invincibility, regeneration, half-dead, resurrection, escape, split, and stasis-like states;
- poison, poison decay, Noxious Fumes, Corpse Explosion, Catalyst, burn and other end-of-turn damage;
- strength, dexterity, vulnerable, weak, frail, multi-hit attacks, enemy scaling, and intent changes;
- orb passive effects, evoke effects, focus, loop, dark-orb targeting, lightning randomness, frost block, and plasma energy;
- player powers, relic triggers, potion effects, energy changes, card draw, discard, exhaust, retain, stance-like state when supported, and on-kill rewards.

An enemy projected to die before it can perform another material action is a doomed target. Do not spend additional single-target damage or poison on a doomed target unless the action has a larger verified benefit, such as Corpse Explosion propagation, an on-kill reward, energy/draw generation, required splash setup, or preventing a special trigger. The trace must make the reason recoverable.

In multi-enemy fights, compare the real incoming-damage reduction from killing or weakening each enemy. Prefer eliminating one dangerous scaling or attacking enemy over distributing damage without a tactical reason. Handle special enemies such as Gremlin Nob, Slavers, Orb Walkers, Reptomancer summons, Darklings, Awakened One, Time Eater, Donu/Deca, Shield/Spear, and the Heart according to their mechanics rather than a generic lowest-HP rule.

Character-specific planning must include:

- Ironclad: strength scaling, vulnerable, exhaust, status generation, self-damage benefits, healing, Barricade/Body Slam interactions, and lethal racing.
- Silent: natural poison death, poison decay, Catalyst, Corpse Explosion, shivs, discard, dexterity, weak, Wraith Form, and delayed damage.
- Defect: orb slots and order, passive versus evoke value, focus, Loop, Capacitor, dark-orb setup, frost defense, plasma energy, and power setup time.

Block only the HP loss whose prevention is strategically valuable. Avoid wasting energy on block that cannot affect survival or future state, but do not trade irreplaceable HP for small damage when the deck already has a safe line.

Do not end a turn with unused energy and a safe playable mitigation card when that card would reduce authoritative projected HP loss. This is a marginal fallback, not a command to empty the hand: preserve valid exceptions such as Gremlin Nob Enrage, Time Eater's twelfth card, Beat of Death, Orichalcum, Ice Cream, or another verified larger cost.

Use potions proactively when they materially improve survival or prevent severe attrition, especially in elites, bosses, Act 4, or projected lethal turns. Do not die while retaining a potion that had a clearly beneficial legal use.

## Non-combat decisions

Route planning must balance immediate survival, future recovery, deck strength, shops, elites, keys, and boss readiness. Avoid optional elites at unsafe HP when a materially safer legal route exists. Avoid low-value normal combats in later acts when safer, higher-value nodes are available.

Card, relic, shop, event, campfire, and boss-reward choices must evaluate the current character, deck, relics, potions, gold, act, boss, HP, and key plan. Verify upgrades, removals, transforms, and purchases by exact instance and by their authoritative post-action effects. This prevents requesting card A while the game applies the action to card B.

For the Match and Keep event, remember every revealed card ID, avoid clicking the same first card twice, select a known matching pair as soon as it exists, and wait for the previous pair to resolve before sending the next choice. A repeated known-card click may leave the serialized option text unchanged, so the bridge must use the event's authoritative ready frame rather than requiring a changed decision ID.

For Heart attempts, plan all keys explicitly:

- Ruby key at a campfire without sacrificing a run-critical upgrade or recovery window unnecessarily.
- Emerald key by selecting and defeating the burning elite when the route and deck can survive it.
- Sapphire key by taking it from a chest while accounting for the relic opportunity cost.

## Speed and strategy changes

Use the fast local planner for ordinary actions. Deeper evaluation is appropriate for elites, bosses, Act 4, projected lethal turns, key choices, or genuinely close candidate scores. Do not invoke an external model for every card play and do not delay normal actions for verbose reporting.

The optional DeepSeek macro adviser is governed by `DEEPSEEK_MACRO.md`. It may
only rerank already-legal non-combat macro candidates and is disabled by
default. `shadow` records advice without applying it; `assist` applies only a
schema-valid, current-state, high-confidence, bounded recommendation. Combat,
protocol bindings, mandatory keys and hard survival filters remain local.
External API failure must return the precomputed deterministic action and must
never become `SafetyError` or an operational result. Adviser mode, model,
release label, prompt, knowledge and fusion configuration are part of
`DECISION_HASH`, so changing any of them requires a fresh controller and a new
random run. Never place `DEEPSEEK_API_KEY` in this repository or a trace.

Do not edit strategy files while a controller is live. One failure is not sufficient evidence for a strategy rewrite. Require at least two comparable, reproducible failures or a direct protocol/correctness violation. Pause gameplay, preserve the trace, make the smallest general fix, add or update a regression test, run the full suite, restart the affected process, and then begin a fresh run.

Never optimize solely for one observed Silent poison case. Fix the underlying projected-effect or target-selection rule so analogous Ironclad, Silent, and Defect situations are handled consistently.

## Reporting and stopping

During normal autonomous operation, concise progress updates are sufficient. Do not stop merely to report a death. Continue with the next attempt.

When a Heart candidate appears, keep the terminal evidence intact and run the literal `--require-clear` audit, independent oracle, Act4 replay/review, complete test suite, and `git diff --check`. Mark the Goal complete only after all final bindings, clean-exit evidence, zero-count gates, and the authoritative Heart result pass together.
