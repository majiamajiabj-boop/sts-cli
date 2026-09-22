# DeepSeek macro adviser

This integration uses the 2026-07-31 DeepSeek model as an optional, fail-safe
macro ranking service. It never emits gameplay commands and never participates
in combat planning.

## Model and modes

- API model id for every decision type: `deepseek-v4-flash`
- official deployed model version: `DeepSeek-V4-Flash-0731`
- default endpoint: `https://api.deepseek.com/chat/completions` with an
  explicit dynamic JSON contract and strict local validation
- optional transport: `/responses`; HTTP 400/422 schema rejection falls back
  to Chat JSON mode
- modes: `off`, `shadow`, `assist`
- default mode: `off`

Every decision type uses that same model. Ordinary card, event, Neow, shop,
map, campfire, grid and persistent run-plan choices use non-thinking mode with
temperature 0. Boss relic and Sapphire-key opportunity cost use `high`
reasoning. Live contract probes found that `max` did not complete within the
unattended latency budget, so it is not used in live play. Time and output
budgets remain bounded per decision, and critical decisions have reserved run
budget that ordinary consultations cannot consume.

`DeepSeek-V4-Flash-0731` is the model version shown by DeepSeek's official
pricing page, while `deepseek-v4-flash` is the callable API id. When the
response contains `system_fingerprint`, the first value observed for the
release-label/model pair is persisted locally and a change opens the circuit
breaker instead of applying the response.

## Security configuration

The simplest local setup is the ignored `.env` file in the project root. Fill
in only the first value; the remaining defaults enable selectively gated
macro assistance whenever `autoplay.py` starts:

```dotenv
DEEPSEEK_API_KEY=<local secret>
DEEPSEEK_API_TRANSPORT=chat_json
# If Clash/Mihomo owns DNS but the system proxy is disabled, set its mixed port:
DEEPSEEK_PROXY_URL=http://127.0.0.1:7897
STS_MACRO_ADVISOR_MODE=assist
STS_MACRO_ADVISOR_SCOPES=CARD_REWARD,BOSS_RELIC,SHOP,SAPPHIRE_KEY,EVENT,MAP,CAMPFIRE,GRID,NEOW,RUN_PLAN
DEEPSEEK_CACHE_WARMUP=true
```

The controller reads only the allowlisted DeepSeek/adviser settings.
Existing process environment values take precedence over `.env`. The file is
ignored by Git; `.env.example` is the committed secret-free template. Never
put the key in a prompt, trace, command receipt, Goal message or committed
file. `DEEPSEEK_PROXY_URL` accepts only an HTTP(S) proxy URL. It is operational
routing metadata: it is excluded from config representations, decision traces
and `DECISION_HASH`, and request failures never echo it.

PowerShell environment variables remain available when a one-off override is
needed:

```powershell
$env:DEEPSEEK_API_KEY = "<local secret>"
$env:STS_MACRO_ADVISOR_MODE = "shadow"
```

Optional configuration:

```powershell
$env:DEEPSEEK_API_BASE = "https://api.deepseek.com"
$env:DEEPSEEK_PROXY_URL = "http://127.0.0.1:7897"
$env:DEEPSEEK_CACHE_WARMUP = "true"
$env:STS_MACRO_ADVISOR_SCOPES = "CARD_REWARD,BOSS_RELIC,SHOP,SAPPHIRE_KEY,EVENT,MAP,CAMPFIRE,GRID,NEOW,RUN_PLAN"
```

Changing the adviser mode changes `DECISION_HASH`. Pause the current
controller and begin a fresh random run; never switch modes inside a run.
Each noncritical scope has a deterministic local-margin gate, so enabling it
does not mean calling the service for every choice. The scope list and every
per-type model/reasoning/fusion setting are fingerprinted and therefore start
a new cohort when changed.

The `adaptive-consult-v3` scheduler suppresses a clear local winner, adds a
per-decision-type confidence band below the ordinary margin, and suppresses
repeat reviews whose meaningful surface has not changed. Candidate count can
widen only that type's close-call band. The same low-value gate applies to
billable `shadow` traffic as well as `assist`; shadow is reserved for disputed
surfaces, not a census of obvious local wins. Explicit uncertainty, hard review
flags and critical scopes bypass the gate. Audit output separates semantic
choice changes from local confirmations and protocol-only confirmations such
as `action:return`, so a menu-close acknowledgement cannot inflate model
adoption.

## Decision boundary

The adviser may rank only candidates produced and hard-filtered by
`SimpleAgent` for:

- permanent card rewards;
- a persistent soft run/deck direction refreshed at act/archetype milestones;
- Neow rewards;
- boss relics;
- the next shop action;
- Act 1/2 Sapphire-key opportunity cost;
- generic fallback events;
- final safe map entrances;
- every non-mandatory campfire choice, including a low-cost Act 1 Ruby-key
  candidate when HP and recovery constraints make Recall safe;
- permanent upgrade/remove/transform/copy grids.

Combat actions, Match and Keep, temporary combat card choices, bound multi-step
selections, protocol targets, mandatory keys, the low-HP campfire Rest floor,
lethal event options and Runic Dome remain deterministic.

## Context and caching

`PromptAssembler` emits six ordered message blocks:

1. global immutable instructions;
2. common policy and compact game knowledge;
3. immutable current-character strategy knowledge;
4. an immutable generic JSON output contract;
5. slowly changing cache context (ascension, immutable relic catalog, unique
   deck-card facts and acquisition-ordered deck references);
6. the current volatile state, candidates and compact advice binding.

Volatile identifiers are last. The JSON suffix uses a deliberate insertion
order and must not be alphabetically sorted. Chat JSON mode does not append a
second full schema after that volatile suffix: the stable contract references
the candidate ids and advice id already supplied by the suffix, while strict
local validation remains authoritative. The server context cache is automatic
and best-effort. A role-level asynchronous warm-up sends exactly the same
four-message stable prefix, with no synthetic tail, so the first live decision
can reuse that complete boundary. Warm-up failure never affects gameplay.

The volatile suffix contains act/boss/key progress, deck-plan estimates,
potions, relic counters/descriptions, Heart deadlines, gold, HP, floor,
decision context, candidates and protocol binding. Those fields never move in
front of the fifth-message cache boundary. Deck cards use the immutable
`macro-wire-v4` column legend: unique strategic fact rows preserve every
runtime value and description, while an acquisition-ordered reference list
preserves exact copy count without sending per-copy UUIDs. Relic identity is
split from its aligned mutable runtime row so a counter change cannot invalidate
the following deck prefix; the mutable runtime row stays in the volatile suffix.
Exact selectable UUIDs remain in candidate ids for
card rewards and permanent card grids.

Candidate facts use a request-local catalog with exact integer references.
Card facts use the same positional columns as the deck; identical GRID copies
therefore share one fact row without losing their distinct candidate ids.

Attempt, run, state-sequence, decision and request-state hashes remain part of
the local advice id and stale-response validation, but only the derived
`advice_id` is model-visible. A compact output guard supplies the required
ranking count; the exact legal ids already live in candidates and are checked
strictly after parsing, avoiding a second volatile copy of every UUID.

The response contract requires confidence on a 0-to-1 scale and one ranking
for every legal candidate. Ranking scores use a normalized 0-to-100 scale,
where higher is better; candidate ids may not repeat. Local validation remains
authoritative even when the selected transport provides only JSON mode rather
than server-enforced schema validation.

A separate local idempotency cache is keyed by attempt, run, state, decision,
snapshot, prompt, knowledge and expected release. It prevents a controller
restart from purchasing a second model answer for the same authoritative
choice. Local cache files live under `.deepseek-advisor-cache/` and are ignored
by Git.

## Fusion and fallback

The deterministic choice is computed first, but its identity and local scores
are hidden from the model. In `assist` mode the model can change it only when:

- the response passes strict local validation;
- the authoritative protocol binding has not changed;
- confidence meets the per-type threshold;
- the local regret is within the per-type cap;
- the model's own normalized score margin is large enough;
- bounded score fusion or a high-confidence direct recommendation changes the
  ordering without exceeding the local regret cap.

Clear deterministic margins skip the API in both `shadow` and `assist` unless
the decision policy is marked `always_consult`; CAMPFIRE uses that mode so the
model sees every ambiguous legal fire while the hard Rest/Recall decisions
bypass it. Timeout, network
failure, 401/402/403,
429/5xx exhaustion, empty output, invalid JSON, duplicate/unknown candidates,
schema mismatch, stale state, model/fingerprint drift, budget exhaustion or a
low-confidence result all keep the original rule choice. External failures
must never become `SafetyError` or an operational game result.

The compatibility fallback does not loosen local validation: its JSON is
normalized into the same response shape and must pass the same candidate,
confidence, ranking, rationale and release-fingerprint checks. The model's
fixed `schema_version` echo is shape-checked and normalized to the local
authoritative version; it is not decision data. Advice ids, candidate ids,
complete rankings and choice/ranking consistency remain strict.

Initial limits are versioned in `macro_policy.py`: 30 strategic calls, 120
seconds total provider wait and USD 0.05 estimated cost per continuous
controller process. Warm-ups are counted separately from strategic calls but
still consume provider wait/cost. Four calls, 12 seconds and USD 0.01 are
reserved for critical scopes. All scopes use the same 0731 Flash model with
`none` thinking: repeated live probes showed that `high` exceeded even a
35-second bound on compact Boss-relic requests, making it slower and less
useful than a valid bounded response. Critical choices use a 10-second bound;
ordinary calls retain their 6-to-8-second bounds. Card rewards, campfires and
persistent run plans accept validated 0.70-confidence advice because the
provider commonly emits that calibrated value. Historical campfire replay
exposed Ruby/Sapphire/Emerald confusion, so the static prompt now defines each
key mechanism explicitly, while local regret caps and score-margin gates still
prevent unsafe direct overrides. Three consecutive infrastructure failures or
more than 20 percent total failures in a full 20-call window opens the circuit;
auth/account and model-release safety failures stop consultation immediately.
The disk advice cache prevents a controller restart from buying the same
authoritative decision twice, but aggregate counters currently restart with
the controller.

## Audit

Every completed recommendation records only sanitized metadata: requested and
returned model, fingerprint, advice id, candidate ids, rule/model/final choice,
confidence, reason codes, status, error class, latency, token usage, cache hit
tokens and estimated cost. Prompts, headers, API keys and service error bodies
are not written to traces.

`strategy_audit.py` aggregates adviser status, semantic choice changes, local
confirmations, protocol-only confirmations, fallbacks, local cache hits,
DeepSeek prefix-cache token ratio, latency and estimated cost. The semantic
adoption rate excludes protocol-only confirmations.
`campaign_selector.py` continues to use `DECISION_HASH`; the hash now includes
mode, model, release label, prompt, knowledge, serialization and fusion policy.

## Validation sequence

1. Run all unit tests with adviser networking replaced by fake clients.
2. Start `shadow` and inspect at least 30 complete random runs, with at least ten
   runs per supported character.
3. Require zero illegal executions, zero hard-rule overrides and zero model-
   caused operational errors.
4. Enable `assist` first for card rewards and boss relics, then expand by
   decision type only after the previous cohort is clean.
5. Use `heart_defeated` as the primary randomized A/B endpoint. API fallbacks
   remain in the assist arm. Seed replay is allowed only in an isolated
   benchmark profile, never in official autonomous history.

Without `DEEPSEEK_API_KEY`, `shadow` and `assist` safely retain deterministic
choices and record `missing_api_key`; no real-service quality or cache claim can
be made until a key is supplied locally.
