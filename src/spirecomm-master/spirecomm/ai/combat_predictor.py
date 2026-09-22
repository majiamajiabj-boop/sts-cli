"""Conservative combat projections shared by every supported character.

The original SimpleAgent chose targets from current HP alone.  That wastes
attacks on monsters which are already guaranteed to die before acting and it
also overestimates incoming damage.  This module deliberately models only
effects that can be established from the current CommunicationMod frame.  If
an interaction is uncertain, the monster stays active rather than being
optimistically declared dead.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DamagePacket:
    """One deterministic enemy-side damage source in the current turn.

    CommunicationMod's normal move fields cover declared attack intents, but
    some powers execute damage from a non-attack intent.  Representing both as
    packets keeps threat, block, Buffer, Intangible, Torii, and Tungsten Rod on
    one calculation path instead of adding encounter-specific score bonuses.
    """

    source: str
    damage_per_hit: int
    hits: int = 1
    torii_eligible: bool = True
    vulnerable_eligible: bool = True


@dataclass(frozen=True)
class PlayerDamageEvent:
    """One ordered player-side HP-loss event before enemy attacks."""

    source: str
    amount: int
    blockable: bool = False


@dataclass(frozen=True)
class PlayerDamageOutcome:
    hp_loss: int
    block: int
    buffer: int
    # Actual post-Block/Buffer/Tungsten HP-loss packets.  Keeping their
    # sources preserves trigger semantics for powers such as Rupture: a
    # blocked or one-point Tungsten-nullified packet is not a trigger, while
    # two distinct Pain/LoseHP actions are two triggers even when their total
    # loss is identical to one larger packet.
    hp_loss_events: tuple = ()


@dataclass(frozen=True)
class AttackHitReaction:
    """State changes which resolve between two enemy Attack hits.

    Powers such as Static Discharge observe the positive attack damage after
    Block/Buffer, but before Torii and Tungsten Rod reduce the HP loss.  Their
    queued actions then resolve before the next DamageAction in a multi-hit
    move.  Keeping this deliberately small hook in the common attack pipeline
    lets the planner add newly evoked Frost Block and stop a killed attacker
    without duplicating Intangible/Block/Buffer/relic ordering.
    """

    block_gain: int = 0
    stop_attacker: bool = False
    killed_monsters: tuple = ()


@dataclass(frozen=True)
class EnemyReactionOutcome:
    """Enemy state after one reflected THORNS damage packet."""

    stop_attacker: bool = False
    killed_monsters: tuple = ()


@dataclass(frozen=True)
class TurnDamageOutcome:
    end_turn_hp_loss: int
    attack_hp_loss: int
    next_turn_start_hp_loss: int
    block: int
    buffer: int
    end_turn_hp_loss_events: tuple = ()
    next_turn_start_hp_loss_events: tuple = ()
    # Fairy in a Bottle is an automatic death replacement, not an ordinary
    # playable potion. Keep gross damage for attrition/mitigation scoring,
    # while exposing the stable HP result which telemetry observes.
    fairy_revive_consumed: bool = False
    fairy_revive_healing: int = 0
    initial_player_hp: int = None
    final_player_hp: int = None
    end_turn_healing: int = 0

    @property
    def total_hp_loss(self):
        """All deterministic HP loss before the next player decision."""

        return (
            self.end_turn_hp_loss
            + self.attack_hp_loss
            + self.next_turn_start_hp_loss
        )

    @property
    def player_hp_delta(self):
        if self.initial_player_hp is None or self.final_player_hp is None:
            return None
        return self.final_player_hp - self.initial_player_hp

    @property
    def player_survives(self):
        if self.final_player_hp is None:
            return None
        return self.final_player_hp > 0


def _token(value):
    return str(value or "").replace("_", " ").replace("-", " ").lower()


def relic_ids(game):
    """Return normalized relic identifiers from the authoritative frame.

    CommunicationMod has emitted both display-name-like ids (``Paper Frog``)
    and compact ids (``paperphrog``) over the lifetime of the bridge.  Keeping
    this normalization in one place prevents a relic effect from silently
    disappearing merely because a run was recorded by a different protocol
    version.
    """

    return {
        _token(getattr(relic, "relic_id", ""))
        for relic in getattr(game, "relics", []) or []
    }


def fairy_in_a_bottle_healing(game):
    """Return the exact automatic revive heal, or zero when unavailable.

    Fairy Potion is serialized as unusable because it triggers from the
    player's death hook. Sacred Bark doubles its potency and Magic Flower
    modifies the revive heal. Toy Ornithopter does not trigger because the
    automatic death hook does not count as using a potion. Mark of the Bloom
    prevents the revive heal.
    """

    potion_tokens = {
        _token(getattr(potion, "potion_id", "")).replace(" ", "")
        for potion in getattr(game, "potions", []) or []
    }
    if not potion_tokens & {
        "fairypotion", "fairyinabottle", "fairy",
    }:
        return 0
    compact_ids = {value.replace(" ", "") for value in relic_ids(game)}
    if "markofthebloom" in compact_ids:
        return 0
    player = getattr(game, "player", None)
    max_hp = max(0, int(getattr(player, "max_hp", 0) or 0))
    if max_hp <= 0:
        return 0
    potency = 60 if "sacredbark" in compact_ids else 30
    fairy_heal = max(1, (max_hp * potency) // 100)
    if "magicflower" in compact_ids:
        fairy_heal = (fairy_heal * 3 + 1) // 2
    return min(max_hp, fairy_heal)


def _relic_handler_aliases(handler, *aliases):
    return {_token(alias): handler for alias in aliases}


# Exact coverage is a contract, not a recognition list.  Every id in this
# mapping names the deterministic transition which owns its effect in the
# predictor/planner.  Keeping the handler declaration beside the id prevents
# a relic from being reported as exact merely because somebody added its name
# to a set.  The handler values are stable diagnostic capability ids; tests
# exercise the corresponding problem semantics.
EXACT_BRANCH_RELIC_HANDLERS = {
    "akabeko": "first_attack_bonus",
    "letter opener": "periodic_skill_aoe",
    "charon's ashes": "exhaust_aoe",
    "charons ashes": "exhaust_aoe",
    "paper frog": "vulnerable_multiplier",
    "paper phrog": "vulnerable_multiplier",
    "paperfrog": "vulnerable_multiplier",
    "paperphrog": "vulnerable_multiplier",
    "boot": "minimum_unblocked_attack_damage",
    "the boot": "minimum_unblocked_attack_damage",
    "theboot": "minimum_unblocked_attack_damage",
    "calipers": "retained_block_decay",
    "calipers relic": "retained_block_decay",
    "bronze scales": "player_thorns",
    "bronze scales relic": "player_thorns",
    "bronze scalespower": "player_thorns",
    "bronzescales": "player_thorns",
    "pen nib": "next_attack_double_damage",
    "pennib": "next_attack_double_damage",
    "torii": "small_hit_reduction",
    "tungsten rod": "hp_loss_reduction",
    "tungstenrod": "hp_loss_reduction",
    "orichalcum": "empty_block_floor",
    "frozen core": "empty_orb_slot_frost",
    "frozencore": "empty_orb_slot_frost",
    "chemical x": "x_cost_bonus",
    "chemicalx": "x_cost_bonus",
    "odd mushroom": "weak_damage_multiplier",
    "oddmushroom": "weak_damage_multiplier",
    "magic flower": "combat_healing_multiplier",
    "magicflower": "combat_healing_multiplier",
    "cables": "rightmost_orb_passive",
    "shuriken": "periodic_attack_strength",
    "kunai": "periodic_attack_dexterity",
    "nunchaku": "periodic_attack_energy",
    "ink bottle": "periodic_card_draw",
    "inkbottle": "periodic_card_draw",
    "ornamental fan": "periodic_attack_block",
    "ornamentalfan": "periodic_attack_block",
    "velvet choker": "per_turn_card_limit",
    "velvetchoker": "per_turn_card_limit",
    "runic cube": "hp_loss_event_draw",
    "runiccube": "hp_loss_event_draw",
    # Planner-owned exact transitions.  These do not all change the enemy
    # damage packet directly, but they do change the legal/value-bearing card
    # branch and therefore belong in the same exact coverage contract.
    "blue candle": "curse_play_hp_loss_and_exhaust",
    "bluecandle": "curse_play_hp_loss_and_exhaust",
    "champion belt": "vulnerable_applies_weak",
    "championbelt": "vulnerable_applies_weak",
    "hovering kite": "first_discard_energy",
    "hoveringkite": "first_discard_energy",
    "paper crane": "weak_damage_reduction",
    "paper krane": "weak_damage_reduction",
    "papercrane": "weak_damage_reduction",
    "paperkrane": "weak_damage_reduction",
    "snake skull": "poison_application_bonus",
    "snakeskull": "poison_application_bonus",
    "snecko skull": "poison_application_bonus",
    "sneckoskull": "poison_application_bonus",
    "tough bandages": "discard_block",
    "toughbandages": "discard_block",
    "art of war": "no_attack_next_turn_energy",
    "artofwar": "no_attack_next_turn_energy",
    "stone calendar": "turn_seven_end_turn_aoe",
    "stonecalendar": "turn_seven_end_turn_aoe",
    "sundial": "shuffle_counter_energy",
}

EXACT_BRANCH_COMBAT_RELIC_IDS = frozenset(EXACT_BRANCH_RELIC_HANDLERS)

# Compatibility alias used by existing traces and audit consumers.  From
# coverage contract v2 onward, "modeled" means deterministic exact-branch
# coverage; partially reflected or heuristically recognized relics are not
# allowed in this set.
MODELED_COMBAT_RELIC_IDS = EXACT_BRANCH_COMBAT_RELIC_IDS

# These relics influence decisions but are not deterministically closed by the
# current branch simulator.  Listing them as heuristic is intentionally
# different from claiming exact coverage.
HEURISTIC_COMBAT_RELIC_IDS = frozenset({
    # Active Strength is visible in the frame, but crossing the half-HP
    # threshold through same-branch self damage is not simulated.
    "red skull", "redskull",
    # Delayed block/energy and energy-retention opportunity value are not
    # advanced by the current bounded turn state.
    "self forming clay", "selfformingclay",
    "ice cream", "icecream",
    # The potion policy applies Sacred Bark to every deterministic potion it
    # can re-plan, but random/discovery potions still use a conservative
    # estimate. Centennial Puzzle's future draw is useful after a real HP-loss
    # event, yet the bounded hand search deliberately waits for the next
    # authoritative frame instead of inventing the drawn cards.
    "sacred bark", "sacredbark",
    "centennial puzzle", "centennialpuzzle",
    # Random-card/resource triggers are recognized by the policy but cannot
    # be expanded into a concrete same-frame hand without inventing cards.
    "dead branch", "deadbranch",
    # Lizard Tail changes lethal-combat utility and potion/risk decisions.
    # Its revive is resolved between authoritative frames, so the bounded
    # turn simulator cannot claim an exact HP transition; keeping it
    # heuristic makes per-relic audits fail closed instead of incorrectly
    # classifying the relic as unrelated to combat.
    "lizard tail", "lizardtail",
    "gremlin horn", "gremlinhorn",
    "unceasing top", "unceasingtop",
    # Playing a Power resolves Mummified Hand before the next decision
    # frame. The planner gives the trigger bounded expected energy value,
    # then re-plans from the authoritative zero-cost card instead of
    # inventing which random card was selected.
    "mummified hand", "mummifiedhand",
    # Tingsha deals a deterministic 3 damage on each discard when there is
    # one living target; multi-target fights use the planner's conservative
    # expected random-target estimate, so coverage is intentionally heuristic.
    "tingsha",
    # The Specimen transfers Poison to a random living enemy. The target is
    # intentionally not guessed inside the bounded branch simulator: the next
    # authoritative frame contains the actual transfer and poison value,
    # while a same-frame replay cannot know which enemy the game RNG selected.
    # This conservative heuristic classification avoids a false P0 mechanics
    # coverage failure without claiming an exact random-target transition.
    "the specimen", "thespecimen",
})

# Explicitly known combat effects for which the simulator has neither an
# exact transition nor a decision heuristic.  Keeping this category separate
# from unclassified relics distinguishes a known implementation gap from an
# entirely new/unknown id.
UNSUPPORTED_COMBAT_RELIC_IDS = frozenset()

# Some relics do affect a run/combat, but their authoritative effect is
# already present in the current frame (for example Clockwork Souvenir's
# Artifact power or Ring of the Snake's extra opening draw).  They should be
# visible in diagnostics without being falsely labelled as damage-model gaps.
STATE_REFLECTED_RELIC_IDS = frozenset({
    "clockworksouvenir", "clockwork souvenir", "ring of the snake",
    "ringofthesnake", "ring of the serpent", "ringoftheserpent",
    "cursed key", "cursedkey",
    # These effects are represented by authoritative powers, orbs, intent
    # values, and opening-hand state in the frame used by the planner.
    "cracked core", "crackedcore", "gambling chip", "gamblingchip",
    # Tools of the Trade resolves its draw/discard trigger before the
    # planner receives the next authoritative turn frame.  The resulting
    # hand/discard state is already serialized, so replaying the relic at
    # audit time would invent cards and falsely block mechanics coverage.
    "tools of the trade", "toolsofthetrade",
    "philosopher's stone", "philosopher stone", "philosophers stone",
    "philosopherstone",
    # Compact ids whose combat effect is already visible in powers, energy,
    # dexterity/strength, or the opening frame.
    "vajra", "mutagenicstrength", "oddlysmoothstone", "oddly smooth stone",
    "lantern",
    # These relics change an authoritative value which is already present in
    # the frame (enemy HP, card upgrades, player Buffer, max HP, or shop
    # removal price). They must not be mistaken for omitted combat formulas.
    "preserved insect", "preservedinsect", "whetstone",
    "fossilized helix", "fossilizedhelix", "smiling mask", "smilingmask",
    "strawberry",
    # Lee's Waffle applies its +max-HP/full-heal effect when acquired.  The
    # resulting HP/max-HP values are already authoritative in later frames,
    # so it must not block the mechanics-coverage gate as an unknown relic.
    "lee's waffle", "lees waffle",
    # These relics have already changed the authoritative opening frame or
    # a persistent stat by the time combat decisions are recorded.  Keeping
    # their aliases here prevents a valid state effect from being reported as
    # an omitted damage/mitigation formula.
    "astrolabe", "bottled flame", "bottledflame",
    "bottled lightning", "bottledlightning", "bottled tornado",
    "bottledtornado", "bag of preparation", "bagofpreparation",
    "bag of marbles", "bagofmarbles", "blood vial", "bloodvial",
    "anchor", "thread and needle", "threadandneedle", "captain's wheel",
    "captains wheel", "captainswheel", "horn cleat", "horncleat",
    "datadisk", "data disk", "runic capacitor", "runiccapacitor",
    "symbiotic virus", "symbioticvirus",
    "twisted funnel", "twistedfunnel", "ginger", "turnip", "pear",
    "girya", "darkstone periapt", "darkstoneperiapt", "frozen eye",
    "frozeneye",
    # Their combat contribution is already serialized as Strength, Weak,
    # Intangible, upgraded cards, opening hand/power state, or adjusted card
    # damage.  Register both display and compact runtime ids so the audit does
    # not confuse a naming alias with a missing mechanic.
    "brimstone", "du vu doll", "duvudoll", "enchiridion",
    "incense burner", "incenseburner", "mark of pain", "markofpain",
    "ninja scroll", "ninjascroll", "pocketwatch", "red mask", "redmask",
    "toxic egg 2", "toxicegg2", "war paint", "warpaint",
    "wrist blade", "wristblade",
    # Their current combat result is already authoritative in the frame:
    # hidden intent/card order, starting orb, current orb slots, retained
    # hand, and current energy.  No exact same-branch trigger is claimed.
    "runic dome", "runicdome", "nuclear battery", "nuclearbattery",
    "inserter", "runic pyramid", "runicpyramid",
    # Opening/periodic energy and Snecko costs/draws are already present in
    # the authoritative energy, hand and card-cost fields for this decision.
    "ancient tea set", "ancientteaset", "happy flower", "happyflower",
    "snecko eye", "sneckoeye",
    # Slaver's Collar's conditional energy is already included in the
    # authoritative turn energy for elite and boss combats.
    "slavers collar", "slaverscollar",
    # Emotion Chip triggers orb passives at the start of a turn after an HP
    # loss.  The resulting orb/monster state is serialized before the next
    # planner frame, so replaying the trigger during bounded evaluation would
    # double-count its damage/block effects.
    "emotion chip", "emotionchip",
    # Mercury Hourglass resolves at the start of the player's turn. Its
    # damage has already changed authoritative monster HP before the planner
    # receives a playable frame, so replaying it at terminal evaluation would
    # double-count the packet and could fabricate a same-turn kill.
    "mercury hourglass", "mercuryhourglass",
})

# These run-level relics have a concrete, independently observable protocol
# contract.  The handler names are evidence requirements (not executable
# planner dispatch): the independent oracle must prove the corresponding raw
# reward/counter transition before a run can clear.  Keeping this mapping
# separate prevents a relic from being made "supported" by merely adding its
# name to the broad non-combat allow-list.
OBSERVABLE_NONCOMBAT_RELIC_HANDLERS = {
    **_relic_handler_aliases(
        "elite_combat_reward_contains_two_stably_bound_relics",
        "Black Star", "BlackStar",
    ),
    **_relic_handler_aliases(
        "standard_card_reward_count_minus_two",
        "Busted Crown", "BustedCrown",
    ),
    **_relic_handler_aliases(
        "prevent_curse_gain_and_consume_one_counter",
        "Omamori",
    ),
}


# These do not alter the current combat damage/mitigation calculation.  Their
# run-level effects are handled by route/reward code or by the authoritative
# HP/gold/energy snapshot.  Observable contracts above remain members of this
# category, but are also emitted with their exact audit handler.
NONCOMBAT_RELIC_IDS = frozenset({
    "burning blood", "burningblood", "black blood", "blackblood",
    # Bird Faced Urn is priced by the run-level sustain model (power-count
    # based expected healing).  It does not change the current turn's enemy
    # damage/mitigation transition, so classify it as noncombat here instead
    # of allowing every combat frame to become an unclassified relic gap.
    "bird faced urn", "birdfacedurn",
    "old coin", "oldcoin", "meat on the bone", "meatonthebone",
    "mango", "golden idol", "goldenidol", "tiny chest", "tinychest",
    "pandora's box", "pandora box", "pandoras box", "pandorasbox",
    "frozen egg 2", "frozenegg2", "molten egg 2",
    "moltenegg2", "regal pillow", "regalpillow",
    "neowsblessing", "neow's blessing",
    "potion belt", "potionbelt",
    "meal ticket", "mealticket", "pantograph", "toy ornithopter",
    "toyornithopter", "bloody idol", "bloodyidol", "mark of the bloom",
    "markofthebloom", "coffee dripper", "coffeedripper", "sozu",
    "ectoplasm",
    # Eternal Feather is a rest-site recovery effect.  It is not part of
    # combat arithmetic, but the route planner and campfire audit consume the
    # authoritative deck-size based recovery, so leaving it unregistered made
    # every later combat frame look like an unexplained relic omission.
    "eternal feather", "eternalfeather",
    # Route/reward/rest/shop/event effects observed by the autonomous corpus.
    # Their acquisition can still be audited by the macro candidate contract,
    # but they do not belong in current-turn combat arithmetic.
    "calling bell", "callingbell", "dream catcher", "dreamcatcher",
    "empty cage", "emptycage", "fusion hammer", "fusionhammer",
    "juzu bracelet", "juzubracelet", "matryoshka", "maw bank", "mawbank",
    "membership card", "membershipcard",
    "nloth's gift", "nloths gift", "nlothsgift",
    "peace pipe", "peacepipe", "prayer wheel", "prayerwheel",
    "question card", "questioncard", "shovel", "singing bowl",
    "singingbowl", "spirit poop", "spiritpoop", "the courier",
    "thecourier", "tiny house", "tinyhouse", "white beast statue",
    "whitebeaststatue", "winged greaves", "wingedgreaves",
}) | frozenset(OBSERVABLE_NONCOMBAT_RELIC_HANDLERS)


# Mechanics coverage answers whether a transition is numerically represented.
# Strategy coverage is deliberately separate: a state-reflected relic such as
# Pocketwatch can still need an explicit opportunity-cost calculation in the
# current turn.  Every decision handler below names the value boundary that a
# per-relic audit is expected to evaluate.
RELIC_DECISION_STRATEGY_HANDLERS = {
    **EXACT_BRANCH_RELIC_HANDLERS,
    **_relic_handler_aliases(
        "hp_loss_future_block_value",
        "Self Forming Clay", "SelfFormingClay",
    ),
    **_relic_handler_aliases(
        "first_hp_loss_draw_value",
        "Centennial Puzzle", "CentennialPuzzle",
    ),
    **_relic_handler_aliases(
        "three_card_turn_budget", "Pocketwatch",
    ),
    **_relic_handler_aliases(
        "retained_energy", "Ice Cream", "IceCream",
    ),
    **_relic_handler_aliases(
        "symmetric_strength_growth", "Brimstone",
    ),
    **_relic_handler_aliases(
        "state_reflected_half_hp_strength", "Red Skull", "RedSkull",
    ),
    **_relic_handler_aliases(
        "random_hand_cost_zero_after_power",
        "Mummified Hand", "MummifiedHand",
    ),
}

# A selected relic using one of these handlers must expose an explicit
# downside term in its purchase score.  Keeping the contract next to the
# strategy registry lets the audit automatically cover future relic aliases
# that share the same liability model instead of maintaining a relic-id list.
NEGATIVE_RELIC_STRATEGY_HANDLERS = frozenset({
    "symmetric_strength_growth",
})

# Known action-sensitive relics which still lack a closed current-turn value
# model. Reporting them as unsupported is intentional: adding the relic name
# to a passive allow-list must never make the strategy audit turn green.
# The Specimen is deliberately heuristic above because its random transfer is
# resolved by the next authoritative frame.
UNSUPPORTED_RELIC_STRATEGY_IDS = frozenset()

RELIC_STRATEGY_COVERAGE_CONTRACT_VERSION = 2


def relic_strategy_coverage_contract_violations():
    """Return ambiguous declarations in the decision-value registry."""

    missing_handlers = sorted(
        relic_id for relic_id, handler in RELIC_DECISION_STRATEGY_HANDLERS.items()
        if not str(handler or "").strip()
    )
    decision_unsupported_overlap = sorted(
        set(RELIC_DECISION_STRATEGY_HANDLERS)
        & set(UNSUPPORTED_RELIC_STRATEGY_IDS)
    )
    return {
        "decision_without_handler": missing_handlers,
        "decision_unsupported_overlap": decision_unsupported_overlap,
    }


def relic_strategy_coverage(game):
    """Classify every present relic by strategy-decision support.

    Category priority is part of the contract.  A relic with an explicit
    decision handler is modeled even when its eventual trigger appears only
    in the next authoritative frame; an explicitly unsupported action relic
    remains blocking even if its current power happens to be state-reflected.
    """

    if isinstance(game, dict):
        present = sorted({
            _token(item.get("id") or item.get("relic_id"))
            for item in game.get("relics") or []
            if isinstance(item, dict)
            and (item.get("id") or item.get("relic_id"))
        })
    else:
        present = sorted(relic_ids(game))
    decision_modeled = [
        item for item in present
        if item in RELIC_DECISION_STRATEGY_HANDLERS
    ]
    unsupported = [
        item for item in present
        if item not in RELIC_DECISION_STRATEGY_HANDLERS
        and item in UNSUPPORTED_RELIC_STRATEGY_IDS
    ]
    heuristic = [
        item for item in present
        if item not in RELIC_DECISION_STRATEGY_HANDLERS
        and item not in UNSUPPORTED_RELIC_STRATEGY_IDS
        and item in HEURISTIC_COMBAT_RELIC_IDS
    ]
    passive = [
        item for item in present
        if item not in RELIC_DECISION_STRATEGY_HANDLERS
        and item not in UNSUPPORTED_RELIC_STRATEGY_IDS
        and item not in HEURISTIC_COMBAT_RELIC_IDS
        and (
            item in STATE_REFLECTED_RELIC_IDS
            or item in EXACT_BRANCH_COMBAT_RELIC_IDS
        )
    ]
    noncombat = [
        item for item in present
        if item not in RELIC_DECISION_STRATEGY_HANDLERS
        and item not in UNSUPPORTED_RELIC_STRATEGY_IDS
        and item not in HEURISTIC_COMBAT_RELIC_IDS
        and item not in STATE_REFLECTED_RELIC_IDS
        and item not in EXACT_BRANCH_COMBAT_RELIC_IDS
        and item in NONCOMBAT_RELIC_IDS
    ]
    classified = set(
        decision_modeled + unsupported + heuristic + passive + noncombat
    )
    unclassified = [item for item in present if item not in classified]
    return {
        "coverage_contract_version": (
            RELIC_STRATEGY_COVERAGE_CONTRACT_VERSION
        ),
        "decision_modeled_relic_ids": decision_modeled,
        "decision_handlers": {
            item: RELIC_DECISION_STRATEGY_HANDLERS[item]
            for item in decision_modeled
        },
        "passive_relic_ids": passive,
        "heuristic_relic_ids": heuristic,
        "unsupported_relic_ids": unsupported,
        "noncombat_relic_ids": noncombat,
        "unclassified_relic_ids": unclassified,
    }


def relic_coverage_contract_violations():
    """Return registry errors which would make coverage labels ambiguous."""

    registries = {
        "exact_branch": EXACT_BRANCH_COMBAT_RELIC_IDS,
        "state_reflected": STATE_REFLECTED_RELIC_IDS,
        "heuristic": HEURISTIC_COMBAT_RELIC_IDS,
        "unsupported": UNSUPPORTED_COMBAT_RELIC_IDS,
        "noncombat": NONCOMBAT_RELIC_IDS,
    }
    owners = {}
    overlaps = []
    for category, ids in registries.items():
        for relic_id in ids:
            previous = owners.setdefault(relic_id, category)
            if previous != category:
                overlaps.append((relic_id, previous, category))
    missing_handlers = sorted(
        relic_id for relic_id in EXACT_BRANCH_COMBAT_RELIC_IDS
        if not str(EXACT_BRANCH_RELIC_HANDLERS.get(relic_id) or "").strip()
    )
    observable_without_handler = sorted(
        relic_id for relic_id in OBSERVABLE_NONCOMBAT_RELIC_HANDLERS
        if not str(
            OBSERVABLE_NONCOMBAT_RELIC_HANDLERS.get(relic_id) or ""
        ).strip()
    )
    observable_not_noncombat = sorted(
        set(OBSERVABLE_NONCOMBAT_RELIC_HANDLERS) - set(NONCOMBAT_RELIC_IDS)
    )
    return {
        "overlaps": sorted(overlaps),
        "exact_without_handler": missing_handlers,
        "observable_noncombat_without_handler": observable_without_handler,
        "observable_noncombat_not_registered": observable_not_noncombat,
    }


def relic_model_coverage(game):
    """Return normalized four-level relic coverage for trace audits.

    Legacy ``modeled_*`` fields remain aliases of ``exact_branch_*`` so old
    consumers continue to work without treating heuristic recognition as an
    exact combat formula.
    """

    if isinstance(game, dict):
        present = sorted(
            _token(item.get("id") or item.get("relic_id"))
            for item in game.get("relics") or []
            if isinstance(item, dict)
        )
    else:
        present = sorted(relic_ids(game))
    exact_branch = [
        item for item in present if item in EXACT_BRANCH_COMBAT_RELIC_IDS
    ]
    state_reflected = [
        item for item in present if item in STATE_REFLECTED_RELIC_IDS
    ]
    heuristic = [
        item for item in present if item in HEURISTIC_COMBAT_RELIC_IDS
    ]
    unsupported = [
        item for item in present if item in UNSUPPORTED_COMBAT_RELIC_IDS
    ]
    noncombat = [item for item in present if item in NONCOMBAT_RELIC_IDS]
    observable_noncombat_handlers = {
        item: OBSERVABLE_NONCOMBAT_RELIC_HANDLERS[item]
        for item in present
        if item in OBSERVABLE_NONCOMBAT_RELIC_HANDLERS
    }
    unclassified = [
        item for item in present
        if item not in EXACT_BRANCH_COMBAT_RELIC_IDS
        and item not in STATE_REFLECTED_RELIC_IDS
        and item not in HEURISTIC_COMBAT_RELIC_IDS
        and item not in UNSUPPORTED_COMBAT_RELIC_IDS
        and item not in NONCOMBAT_RELIC_IDS
    ]
    return {
        "coverage_contract_version": 2,
        "exact_branch_relic_ids": exact_branch,
        "modeled_relic_ids": exact_branch,
        "state_reflected_relic_ids": state_reflected,
        "heuristic_relic_ids": heuristic,
        "unsupported_relic_ids": unsupported,
        "noncombat_relic_ids": noncombat,
        "observable_noncombat_handlers": observable_noncombat_handlers,
        "unclassified_relic_ids": unclassified,
        "exact_branch_count": len(exact_branch),
        "modeled_count": len(exact_branch),
        "state_reflected_count": len(state_reflected),
        "heuristic_count": len(heuristic),
        "unsupported_count": len(unsupported),
        "noncombat_count": len(noncombat),
        "unclassified_count": len(unclassified),
    }


# Active-power coverage follows the same evidence contract as relic coverage,
# but it is role-sensitive.  The same power can be represented differently on
# each side of combat: for example, a player's existing Vulnerable modifier is
# already folded into serialized monster intent damage, while Vulnerable on a
# monster must be applied target-locally by the ordered attack branch.
#
# An ``exact_branch`` entry is therefore allowed only when it names the
# concrete mechanism which advances that power during the current decision
# horizon.  These handler ids are diagnostics, not executable dispatch names;
# tests assert that every exact alias has one and that no role/category
# overlaps exist.  This avoids the old failure mode where adding a power name
# to a recognition set silently advertised coverage that did not exist.
POWER_COVERAGE_CONTRACT_VERSION = 1


def _power_handler_aliases(handler, *aliases):
    return {_token(alias): handler for alias in aliases}


EXACT_BRANCH_POWER_HANDLERS = {
    "player": {
        **_power_handler_aliases(
            "insert_dazed_after_each_non_attack_and_invalidate_draw_order",
            "Hex", "HexPower",
        ),
        **_power_handler_aliases(
            "gain_strength_after_each_actual_self_hp_loss_packet",
            "Rupture", "RupturePower",
        ),
        **_power_handler_aliases(
            "retain_player_block_at_turn_boundary",
            "Barricade", "BarricadePower", "Blur", "BlurPower",
        ),
        **_power_handler_aliases(
            "reject_new_block_while_no_block_is_active",
            "No Block", "NoBlockPower",
        ),
        **_power_handler_aliases(
            "resolve_player_damage_cap_per_event",
            "Intangible", "IntangiblePlayer", "IntangiblePower",
        ),
        **_power_handler_aliases(
            "consume_buffer_layers_in_ordered_damage_pipeline",
            "Buffer", "BufferPower",
        ),
        **_power_handler_aliases(
            "consume_artifact_against_branch_local_debuffs",
            "Artifact", "ArtifactPower",
        ),
        **_power_handler_aliases(
            "add_end_turn_block_before_hand_damage",
            "Metallicize", "MetallicizePower", "Plated Armor",
            "PlatedArmor", "PlatedArmorPower",
        ),
        **_power_handler_aliases(
            "resolve_player_end_turn_hp_loss_event",
            "Combust", "CombustPower", "Constricted", "ConstrictedPower",
        ),
        **_power_handler_aliases(
            "resolve_player_next_turn_start_hp_loss_event",
            "Brutality", "BrutalityPower",
        ),
        **_power_handler_aliases(
            "resolve_player_end_turn_regeneration",
            "Regeneration", "RegenerationPower",
        ),
        **_power_handler_aliases(
            "reflect_damage_per_declared_enemy_attack_hit",
            "Thorns", "ThornsPower", "Flame Barrier",
            "FlameBarrierPower",
        ),
        **_power_handler_aliases(
            "channel_lightning_after_unblocked_attack_damage",
            "Static Discharge", "StaticDischarge", "StaticDischargePower",
        ),
        **_power_handler_aliases(
            "grant_block_after_each_attack_card",
            "Rage", "RagePower",
        ),
        **_power_handler_aliases(
            "grant_block_after_each_card",
            "After Image", "AfterImage", "AfterImagePower",
        ),
        **_power_handler_aliases(
            "add_shiv_damage_in_ordered_attack_branch",
            "Accuracy", "AccuracyPower",
        ),
        **_power_handler_aliases(
            "deal_aoe_after_each_card",
            "A Thousand Cuts", "Thousand Cuts", "ThousandCutsPower",
        ),
        **_power_handler_aliases(
            "deal_thorns_damage_after_each_landed_enemy_debuff",
            "Sadistic", "Sadistic Nature", "SadisticNaturePower",
        ),
        **_power_handler_aliases(
            "grant_block_on_each_exhaust",
            "Feel No Pain", "FeelNoPain", "FeelNoPainPower",
        ),
        **_power_handler_aliases(
            "draw_on_each_exhaust_with_hand_cap",
            "Dark Embrace", "DarkEmbrace", "DarkEmbracePower",
        ),
        **_power_handler_aliases(
            "set_skill_cost_zero_and_exhaust_in_branch",
            "Corruption", "CorruptionPower",
        ),
        **_power_handler_aliases(
            "draw_on_power_play_with_pile_and_hand_caps",
            "Heat Sink", "Heat Sinks", "Heatsink", "Heatsinks",
            "HeatsinkPower", "HeatsinksPower",
        ),
        **_power_handler_aliases(
            "channel_one_lightning_per_existing_stack_after_power_play",
            "Storm", "StormPower",
        ),
        **_power_handler_aliases(
            "repeat_next_attack_resolution",
            "Double Tap", "DoubleTap", "DoubleTapPower",
        ),
        **_power_handler_aliases(
            "repeat_first_card_resolution",
            "Echo Form", "EchoForm", "EchoFormPower",
        ),
        **_power_handler_aliases(
            "make_lightning_damage_all_living_enemies",
            "Electrodynamics", "ElectrodynamicsPower", "Electro",
        ),
        **_power_handler_aliases(
            "prevent_draw_transitions",
            "No Draw", "NoDraw", "NoDrawPower",
        ),
        **_power_handler_aliases(
            "double_next_attack_damage",
            "Pen Nib", "PenNib", "PenNibPower",
        ),
    },
    "monster": {
        **_power_handler_aliases(
            "amplify_only_lightning_and_dark_orb_packets",
            "Lock-On", "Lock On", "Lockon", "LockOn", "LockOnPower",
        ),
        **_power_handler_aliases(
            "add_one_wound_per_unblocked_attack_hit",
            "Painful Stabs", "PainfulStabs", "PainfulStabsPower",
        ),
        **_power_handler_aliases(
            "preserve_nonfinal_darkling_knockdown_until_group_death",
            "Life Link", "LifeLink", "LifeLinkPower",
        ),
        **_power_handler_aliases(
            "apply_target_local_vulnerable_attack_multiplier",
            "Vulnerable", "VulnerablePower",
        ),
        **_power_handler_aliases(
            "resolve_enemy_intangible_damage_cap_per_event",
            "Intangible", "IntangiblePlayer", "IntangiblePower",
        ),
        **_power_handler_aliases(
            "consume_artifact_against_branch_local_debuffs",
            "Artifact", "ArtifactPower",
        ),
        **_power_handler_aliases(
            "resolve_poison_tick_and_branch_local_poison_changes",
            "Poison", "PoisonPower",
        ),
        **_power_handler_aliases(
            "resolve_corpse_explosion_death_cascade",
            "Corpse Explosion", "CorpseExplosion", "CorpseExplosionPower",
        ),
        **_power_handler_aliases(
            "apply_slow_multiplier_per_card_played",
            "Slow", "SlowPower",
        ),
        **_power_handler_aliases(
            "cancel_current_intent_and_add_guardian_block_at_threshold",
            "Mode Shift", "ModeShift", "ModeShiftPower",
        ),
        **_power_handler_aliases(
            "apply_vulnerable_on_owner_death_before_later_attacks",
            "Spore Cloud", "SporeCloud", "SporeCloudPower",
        ),
        **_power_handler_aliases(
            "emit_exploder_damage_packet_at_countdown_one",
            "Explosive", "ExplosivePower",
        ),
        **_power_handler_aliases(
            "account_for_enemy_regeneration_after_surviving_action",
            "Regenerate", "RegeneratePower",
        ),
        **_power_handler_aliases(
            "reduce_flight_stack_and_damage_per_attack_hit",
            "Flight", "FlightPower",
        ),
        **_power_handler_aliases(
            "increase_block_after_each_attack_hit",
            "Malleable", "MalleablePower",
        ),
        **_power_handler_aliases(
            "apply_one_time_block_after_complete_attack_card",
            "Curl Up", "CurlUp", "CurlUpPower",
        ),
        **_power_handler_aliases(
            "resolve_reactive_damage_per_hit_or_attack_card",
            "Thorns", "ThornsPower", "Sharp Hide", "SharpHidePower",
        ),
        **_power_handler_aliases(
            "resolve_hp_loss_after_each_player_card",
            "Beat of Death", "BeatOfDeath", "BeatOfDeathPower",
        ),
        **_power_handler_aliases(
            "enforce_twelve_card_turn_limit_and_strength_reset",
            "Time Warp", "TimeWarp", "TimeWarpPower",
        ),
        **_power_handler_aliases(
            "gain_strength_after_skill_card",
            "Anger", "AngerPower", "Angry", "Enrage", "EnragePower",
        ),
        **_power_handler_aliases(
            "gain_strength_after_power_card",
            "Curiosity", "CuriosityPower",
        ),
        **_power_handler_aliases(
            "preserve_phase_one_death_and_revive_transition",
            "Unawakened", "UnawakenedPower",
        ),
        **_power_handler_aliases(
            "advance_existing_choke_damage_after_later_cards",
            # Base-game ChokePower serializes as ``Choked`` in live
            # CommunicationMod frames. Keep the class-style aliases for
            # fixtures and older adapters, but bind the live id to the same
            # exact per-card HP-loss branch.
            "Choke", "ChokePower", "Choked", "ChokedPower",
        ),
    },
}


# State-reflected powers have already changed authoritative card, intent,
# energy, orb, targeting, or current-stat fields in the frame.  This category
# deliberately does not claim that their future-duration transitions are
# simulated.
STATE_REFLECTED_POWER_IDS = {
    "player": frozenset(_token(item) for item in {
        "Strength", "StrengthPower", "Dexterity", "DexterityPower",
        "Weak", "Weakened", "WeakPower", "Frail", "FrailPower",
        "Vulnerable", "VulnerablePower", "Focus", "FocusPower",
        # Noxious Fumes fires at the start of the player's turn. Its current
        # application is therefore already visible in enemy Poison when a
        # decision frame is received; a newly played copy must not be counted
        # before the current enemy turn.
        "Noxious Fumes", "NoxiousFumes", "NoxiousFumesPower",
        # Draw Reduction has already reduced the authoritative hand drawn for
        # this player turn before a decision frame is emitted. The bounded
        # planner never advances a second player-turn draw, so its entire
        # current-horizon effect is reflected in the serialized hand.
        "Draw Reduction", "DrawReduction", "DrawReductionPower",
        # DrawCardNextTurnPower (serialized by CommunicationMod as
        # ``Draw Card``) resolves before the next actionable player frame.
        # The bounded planner does not advance a second player-turn draw, so
        # the resulting extra cards are authoritative hand state when they
        # can first affect a decision.
        "Draw Card", "Draw", "DrawCard", "DrawCardNextTurn",
        "DrawCardNextTurnPower",
        # DoubleDamagePower.atDamageGive multiplies NORMAL packets before
        # AbstractCard.damage is serialized. Trust that displayed packet;
        # multiplying again here would turn double damage into quadruple.
        "Double Damage", "DoubleDamage", "DoubleDamagePower",
        # PhantasmalPower applies Double Damage at the NEXT player-turn start.
        # The pending flag has no same-turn damage effect. Its resolved
        # multiplier is reflected in that next authoritative hand; this label
        # does not claim an exact simulation of future draws or power expiry.
        "Phantasmal", "PhantasmalPower",
        # Mayhem resolves its automatic top-card play at the start of the
        # player turn, before the next actionable frame. The resulting hand,
        # piles, powers, HP and enemy state are therefore authoritative input
        # to this planner's single-turn horizon.
        "Mayhem", "MayhemPower",
        # Equilibrium has already retained the authoritative current hand.
        # The bounded planner does not advance another player-turn discard.
        "Retain Cards", "Equilibrium", "EquilibriumPower",
        # Tools of the Trade performs its draw/discard trigger before the
        # next actionable frame.  The resulting hand and discard pile are
        # authoritative, so replaying the trigger in the bounded planner
        # would double-apply it.
        "Tools of the Trade", "ToolsOfTheTrade", "ToolsOfTheTradePower",
        "Entangled", "Surrounded", "BackAttack",
    }),
    "monster": frozenset(_token(item) for item in {
        "Strength", "StrengthPower", "Weak", "Weakened", "WeakPower",
        "Shackled", "Minion", "Split", "BackAttack",
    }),
}


# These powers are state-reflected only because the trace contains enough raw
# state to verify their lifecycle independently.  In particular Stasis is not
# a damage-planner approximation: the oracle binds the removed card instance
# to the owning Bronze Orb and requires that exact card to return to hand when
# the owner dies.
OBSERVABLE_STATE_POWER_HANDLERS = {
    "player": {},
    "monster": {
        **_power_handler_aliases(
            "bronze_orb_stasis_remove_card_then_return_to_hand_on_owner_death",
            "Stasis", "StasisPower",
        ),
    },
}
STATE_REFLECTED_POWER_IDS = {
    role: frozenset(ids) | frozenset(OBSERVABLE_STATE_POWER_HANDLERS[role])
    for role, ids in STATE_REFLECTED_POWER_IDS.items()
}


# Heuristic powers influence scoring or a conservative envelope, but their
# complete state transition is not closed by the bounded simulator.
HEURISTIC_COMBAT_POWER_IDS = {
    "player": frozenset(_token(item) for item in {
        "Confusion", "Flex", "DexLoss", "Bias", "Wraith Form v2",
        "Ritual", "Repair",
        # Their current-frame result is observable and their future value is
        # scored, but the bounded turn state does not advance every future
        # start-of-turn trigger.
        "Loop", "LoopPower", "Energized", "EnergizedBlue", "Berserk",
        "Demon Form", "DemonForm", "DemonFormPower",
        # Burst duplicates the next Skill resolution.  The bounded planner
        # applies that extra resolution while the authoritative next frame
        # settles the copied Skill exact effects, so active Burst is a
        # conservative heuristic rather than an unclassified power.
        "Burst", "BurstPower",
        # These powers are visible in the current frame and their generated
        # cards/duplication are handled by the authoritative next frame.  The
        # bounded simulator does not advance every future trigger, so record
        # them as conservative heuristics rather than leaving active runs
        # permanently unsupported in the audit gate.
        "Magnetism", "Evolve", "DuplicationPower",
        # Infinite Blades creates its Shiv at the start of the player's turn,
        # before CommunicationMod publishes the next decision frame.  The
        # generated card is therefore authoritative input to the ordinary
        # card/Accuracy/After Image simulation; only farther-future triggers
        # remain outside the bounded search, matching Magnetism above.
        "Infinite Blades", "InfiniteBlades", "InfiniteBladesPower",
        # Amplify duplicates the next Power resolution.  The bounded planner
        # does not invent the copied Power's future effects; the next
        # authoritative frame exposes them, so classify the active marker as
        # a conservative heuristic instead of leaving it unclassified.
        "Amplify", "AmplifyPower",
        # Envenom's exact poison result is exposed by the authoritative frame
        # after every selected Attack. The planner never claims that poison as
        # same-frame lethal, so active Envenom is a conservative heuristic.
        "Envenom", "EnvenomPower",
        # Panache exposes its live 1..5 countdown as Power.amount. The combat
        # planner gives crossings a bounded AoE score without mutating enemy
        # HP or claiming exact lethal because the upgraded 14-damage payload
        # is not serialized on the active Power.
        "Panache", "PanachePower",
        # Rebound is a one-shot future pile-order modifier. The authoritative
        # frame after the next card exposes the resulting piles, while the
        # bounded planner deliberately makes no exact draw-order or lethal
        # claim from the active power itself.
        "Rebound", "ReboundPower",
        # Self-Forming Clay's stored block resolves only at the next player
        # turn boundary; it cannot mitigate the current enemy turn.  The next
        # authoritative frame exposes the resulting block before more choices.
        "Next Turn Block", "NextTurnBlock",
        # The bounded planner does not choose Juggernaut's random target, so
        # this is intentionally heuristic rather than exact-branch coverage.
        # Every immediately queued block-gain trigger is nevertheless bounded
        # in autoplay.damage_model_context and independently reconstructed by
        # independent_oracle before the authoritative next frame is accepted.
        "Juggernaut", "JuggernautPower",
        # These powers create a random power/card at turn start. The bounded
        # simulator intentionally does not invent the random result; the next
        # authoritative frame captures it. Classify them as heuristic so they
        # do not block an otherwise complete mechanics audit.
        "Creative AI", "CreativeAIPower", "Hello", "Hello World",
        "HelloWorld", "HelloWorldPower",
        # Fire Breathing triggers when a Status or Curse is drawn.  The
        # bounded simulator does not advance every future draw trigger, but
        # the active power and resulting damage are visible in authoritative
        # frames.  Keep it conservative/heuristic so a normal run is not
        # blocked by a known deferred trigger without claiming exact damage.
        "Fire Breathing", "FireBreathing", "FireBreathingPower",
    }),
    "monster": frozenset(_token(item) for item in {
        "Ritual", "RitualPower", "Generic Strength Up Power",
        "Fading", "Shifting", "ShiftingPower", "Reactive",
        "ReactivePower", "Compulsive", "Thievery",
        "Metallicize", "MetallicizePower", "Plated Armor",
        "PlatedArmor", "PlatedArmorPower", "Barricade",
        "BarricadePower",
        # CommunicationMod exposes the configured cap but not how much of it
        # remains this turn.  The lethal predictor explicitly fails closed
        # while this power is active, so classify that conservative handler
        # instead of reporting every Heart frame as an unknown power.
        "Invincible", "InvinciblePower",
    }),
}


# These are known base-game effects which are not advanced by either the exact
# branch or a reliable scoring approximation.  Listing them here is useful:
# an audit can distinguish an understood implementation gap from a genuinely
# new CommunicationMod id.  In particular, do not promote a power merely
# because a card which creates it has a generic setup score.
UNSUPPORTED_COMBAT_POWER_IDS = {
    "player": frozenset(_token(item) for item in {
        "Lock-On", "Lock On", "LockOnPower",
        "TheBomb0", "Night Terror",
    }),
    "monster": frozenset(_token(item) for item in {
        "Hex", "HexPower",
        "Juggernaut", "JuggernautPower", "Fire Breathing",
        "FireBreathingPower", "Envenom", "EnvenomPower",
        "Sadistic Nature", "SadisticNaturePower", "Panache",
        "PanachePower",
    }),
}


def power_coverage_contract_violations():
    """Return role-aware registry errors which invalidate coverage labels."""

    overlaps = []
    exact_without_handler = []
    observable_state_without_handler = []
    observable_state_not_registered = []
    for role in ("player", "monster"):
        registries = {
            "exact_branch": set(EXACT_BRANCH_POWER_HANDLERS[role]),
            "state_reflected": set(STATE_REFLECTED_POWER_IDS[role]),
            "heuristic": set(HEURISTIC_COMBAT_POWER_IDS[role]),
            "unsupported": set(UNSUPPORTED_COMBAT_POWER_IDS[role]),
        }
        owners = {}
        for category, ids in registries.items():
            for power_id in ids:
                previous = owners.setdefault(power_id, category)
                if previous != category:
                    overlaps.append((role, power_id, previous, category))
        exact_without_handler.extend(
            (role, power_id)
            for power_id, handler in EXACT_BRANCH_POWER_HANDLERS[role].items()
            if not str(handler or "").strip()
        )
        observable_state_without_handler.extend(
            (role, power_id)
            for power_id, handler in
            OBSERVABLE_STATE_POWER_HANDLERS[role].items()
            if not str(handler or "").strip()
        )
        observable_state_not_registered.extend(
            (role, power_id)
            for power_id in OBSERVABLE_STATE_POWER_HANDLERS[role]
            if power_id not in STATE_REFLECTED_POWER_IDS[role]
        )
    return {
        "overlaps": sorted(overlaps),
        "exact_without_handler": sorted(exact_without_handler),
        "observable_state_without_handler": sorted(
            observable_state_without_handler
        ),
        "observable_state_not_registered": sorted(
            observable_state_not_registered
        ),
    }


def classify_active_power(power, owner_role):
    """Classify one serialized/parsed active Power with raw identity intact."""

    role = "monster" if str(owner_role).lower() == "monster" else "player"
    if isinstance(power, dict):
        raw_id = power.get("id") or power.get("power_id")
        raw_name = power.get("name") or power.get("power_name")
        amount = power.get("amount")
    else:
        raw_id = getattr(power, "power_id", None)
        raw_name = getattr(power, "power_name", None)
        amount = getattr(power, "amount", None)
    candidates = []
    for value in (raw_id, raw_name):
        token = _token(value)
        if token and token not in candidates:
            candidates.append(token)
    normalized = _token(raw_id or raw_name)
    category = "unclassified"
    handler = None
    registry_id = None
    registries = (
        ("exact_branch", EXACT_BRANCH_POWER_HANDLERS[role]),
        ("state_reflected", STATE_REFLECTED_POWER_IDS[role]),
        ("heuristic", HEURISTIC_COMBAT_POWER_IDS[role]),
        ("unsupported", UNSUPPORTED_COMBAT_POWER_IDS[role]),
    )
    # Prefer the protocol id, falling back to display name aliases only when
    # the id is unknown.  Registry categories are disjoint for each role.
    for candidate in candidates:
        for candidate_category, registry in registries:
            if candidate in registry:
                category = candidate_category
                registry_id = candidate
                if candidate_category == "exact_branch":
                    handler = registry[candidate]
                elif candidate_category == "state_reflected":
                    handler = OBSERVABLE_STATE_POWER_HANDLERS[role].get(
                        candidate
                    )
                break
        if registry_id is not None:
            break
    return {
        "owner_role": role,
        "raw_id": raw_id,
        "raw_name": raw_name,
        "normalized_id": normalized,
        "registry_id": registry_id,
        "amount": amount,
        "category": category,
        "handler": handler,
    }


def power_model_coverage(game):
    """Return active player/monster Power coverage for trace diagnostics.

    Both parsed ``Game`` objects and raw CommunicationMod game-state dicts are
    accepted so the trace records the exact ids that arrived over the wire.
    """

    active = []
    if isinstance(game, dict):
        combat = game.get("combat_state") or game
        player = combat.get("player") or {}
        for power in player.get("powers") or []:
            if not isinstance(power, dict):
                continue
            entry = classify_active_power(power, "player")
            entry["owner_instance_id"] = "player"
            active.append(entry)
        for position, monster in enumerate(combat.get("monsters") or []):
            if not isinstance(monster, dict):
                continue
            owner_id = (
                monster.get("enemy_instance_id")
                or monster.get("id")
                or monster.get("name")
                or f"monster:{position}"
            )
            for power in monster.get("powers") or []:
                if not isinstance(power, dict):
                    continue
                entry = classify_active_power(power, "monster")
                entry["owner_instance_id"] = owner_id
                entry["monster_index"] = monster.get(
                    "monster_index", position
                )
                active.append(entry)
    else:
        player = getattr(game, "player", None)
        for power in getattr(player, "powers", []) or []:
            entry = classify_active_power(power, "player")
            entry["owner_instance_id"] = "player"
            active.append(entry)
        for position, monster in enumerate(getattr(game, "monsters", []) or []):
            owner_id = (
                getattr(monster, "enemy_instance_id", None)
                or getattr(monster, "monster_id", None)
                or getattr(monster, "name", None)
                or f"monster:{position}"
            )
            for power in getattr(monster, "powers", []) or []:
                entry = classify_active_power(power, "monster")
                entry["owner_instance_id"] = owner_id
                entry["monster_index"] = getattr(
                    monster, "monster_index", position
                )
                active.append(entry)

    by_category = {
        category: [
            entry for entry in active if entry["category"] == category
        ]
        for category in (
            "exact_branch", "state_reflected", "heuristic", "unsupported",
            "unclassified",
        )
    }
    result = {
        "coverage_contract_version": POWER_COVERAGE_CONTRACT_VERSION,
        "active_powers": active,
        "active_power_count": len(active),
    }
    for category, entries in by_category.items():
        ids = sorted({
            entry["normalized_id"] for entry in entries
            if entry.get("normalized_id")
        })
        result[f"{category}_power_ids"] = ids
        result[f"{category}_count"] = len(entries)
    # Compatibility vocabulary matching the older relic audit.
    result["modeled_power_ids"] = result["exact_branch_power_ids"]
    result["modeled_count"] = result["exact_branch_count"]
    return result


def relic_counter(game, *names, default=0):
    """Return a normalized counter for the first matching relic."""

    wanted = {_token(name) for name in names}
    for relic in getattr(game, "relics", []) or []:
        if _token(getattr(relic, "relic_id", "")) in wanted:
            try:
                return int(getattr(relic, "counter", default) or 0)
            except (TypeError, ValueError):
                return int(default or 0)
    return int(default or 0)


def runic_cube_draw(game, hp_loss_events=0):
    """Return Runic Cube's deterministic draw count for HP-loss events.

    The relic draws one card whenever the player loses HP, not once per point
    of damage.  Card-level planning only knows whether its ordered damage
    packet reached HP, so callers pass the number of confirmed loss events and
    leave random enemy-turn draws to the next authoritative re-plan.
    """

    if not {"runic cube", "runiccube"} & relic_ids(game):
        return 0
    return max(0, int(hp_loss_events or 0))


def akabeko_ready(game):
    """Whether Akabeko's first-Attack bonus is still available."""

    ids = relic_ids(game)
    if not {"akabeko"} & ids:
        return False
    counter = relic_counter(game, "Akabeko", default=-1)
    # Live bridges generally expose a one-shot counter.  Older frames use
    # -1 for relics without a counter; in that format turn one is the only
    # safe point at which the first-Attack trigger can still be assumed.
    if counter > 0:
        return True
    return counter < 0 and int(getattr(game, "turn", 1) or 1) <= 1


def akabeko_bonus(game):
    return 8 if akabeko_ready(game) else 0


def periodic_relic_trigger(game, relic_name, period, prior_count=0):
    """Return whether a counter-based per-turn relic fires on this action."""

    if _token(relic_name) not in relic_ids(game):
        return False
    counter = relic_counter(game, relic_name, default=0)
    return (counter + max(0, int(prior_count or 0)) + 1) % max(1, int(period)) == 0


def letter_opener_damage(game, prior_skills=0):
    """Return Letter Opener's direct AOE packet when the next Skill fires it."""

    return 5 if periodic_relic_trigger(game, "Letter Opener", 3, prior_skills) else 0


def charons_ashes_damage(game):
    """Return Charon's Ashes direct damage per exhausted card."""

    return 3 if {"charons ashes", "charon's ashes"} & relic_ids(game) else 0


def vulnerable_damage_multiplier(game):
    """Return the player Attack multiplier against a Vulnerable monster.

    Vulnerable is normally 150%.  Paper Frog/Phrog changes only this
    target-local modifier to 175%; it does not affect Poison, orbs, Combust,
    Corpse Explosion, or any incoming damage to the player.
    """

    ids = relic_ids(game)
    if {
        "paper frog",
        "paperfrog",
        "paper phrog",
        "paperphrog",
    } & ids:
        return 1.75
    return 1.5


def slow_damage_multiplier(monster, extra_cards=0, *, slow_override=None):
    """Return Giant Head/Slow's target-local Attack multiplier.

    ``Slow`` is applied to the damage of the next Attack after all player
    modifiers and before the target's Block.  Its amount increases once per
    card played during the turn, so ordered-turn search supplies the number
    of cards already simulated in ``extra_cards``.  Keeping this helper
    target-local is important: a serialized ``Card.damage`` cannot contain a
    different Slow amount for each enemy.
    """

    if slow_override is None:
        amount = signed_power_amount(monster, "Slow", "SlowPower")
    else:
        amount = int(slow_override or 0)
    amount = max(0, amount)
    if amount <= 0:
        return 1.0
    amount += max(0, int(extra_cards or 0))
    return 1.0 + amount * 0.1


def floor_damage_product(*factors):
    """Floor a positive damage multiplier chain without binary drift.

    Base-game damage multipliers are simple decimal fractions.  Their binary
    float product can land immediately below an integer (for example
    ``30 * 1.2 * 1.5``), so plain ``int`` undercounts a mathematically exact
    packet by one.  The tiny tolerance is far below any real fractional
    damage step and only restores that exact-integer boundary.
    """

    product = 1.0
    for factor in factors:
        product *= max(0.0, float(factor or 0.0))
    return max(0, int(product + 1e-9))


def boot_minimum_damage(game):
    """Return The Boot's per-hit minimum Attack damage, if present.

    The game applies the low-damage floor after target Block has consumed a
    positive Attack hit.  ``0`` is used instead of ``None`` so callers can
    pass the value through the same per-hit path without special casing.
    """

    ids = relic_ids(game)
    return 5 if {"boot", "the boot", "theboot"} & ids else 0


def attack_relic_modifiers(game):
    """Keyword arguments for target-side relic effects on player Attacks."""

    return {
        "vulnerable_multiplier": vulnerable_damage_multiplier(game),
        "minimum_damage": boot_minimum_damage(game),
    }


def calipers_retained_block(game, block):
    """Return block retained through the next start-of-turn reset.

    Calipers removes 15 Block instead of removing all Block.  The ordinary
    reset happens at the start of the player's next turn, after the current
    enemy turn has already used the full Block amount.  This helper therefore
    models future retained value only; current-turn attack prediction must
    use ``projected_end_block`` without this haircut.  Barricade and an active
    Blur power retain the full amount and bypass the Calipers haircut.
    """

    amount = max(0, int(block or 0))
    player = getattr(game, "player", None)
    if has_power(player, "Barricade", "BarricadePower", "Blur", "BlurPower"):
        return amount
    ids = relic_ids(game)
    if {"calipers", "calipers relic"} & ids:
        return max(0, amount - 15)
    return 0


def stone_calendar_damage(game):
    """Return Stone Calendar's deterministic turn-seven AOE packet."""

    if not {"stone calendar", "stonecalendar"} & relic_ids(game):
        return 0
    # The base-game relic fires at the end of turn seven.  The authoritative
    # turn number is more stable than historical counter serialization (some
    # bridge versions expose -1 for ordinary relic counters).
    return 52 if int(getattr(game, "turn", 0) or 0) == 7 else 0


def stone_calendar_hp_loss(game, monster):
    """Return Stone Calendar HP loss after Block and Intangible."""

    return attack_hp_loss(
        monster,
        stone_calendar_damage(game),
        hits=1,
        vulnerable_eligible=False,
    )


def bronze_scales_damage(game):
    """Return Bronze Scales' reactive damage for the current frame.

    Normal relic counters are serialized as ``3``.  Some CommunicationMod
    frames use ``-1`` for relics without a consumable counter, so a present
    Bronze Scales relic with a non-positive counter still means its standard
    three reflected damage. The effect is applied once per declared Attack
    hit by :func:`_projected_attack_outcome`, including a hit fully absorbed
    by Block. Thorns damage is a direct packet and ignores the attacker's
    Block.
    """

    for relic in getattr(game, "relics", []) or []:
        if _token(getattr(relic, "relic_id", "")) in {
            "bronze scales",
            "bronze scales relic",
            "bronze scalespower",
            "bronzescales",
        }:
            counter = int(getattr(relic, "counter", 0) or 0)
            return counter if counter > 0 else 3
    return 0


def pen_nib_ready(game):
    """Whether Pen Nib will double the next Attack in this frame.

    Ready Pen Nib is exposed as ``PenNibPower`` by some bridge versions and
    only as a relic counter (9 before the tenth Attack) by others.  The
    planner uses this predicate for both forms, while separately preserving
    whether the serialized card damage already contains the doubling.
    """

    player = getattr(game, "player", None)
    if has_power(player, "Pen Nib", "PenNibPower"):
        return True
    for relic in getattr(game, "relics", []) or []:
        if _token(getattr(relic, "relic_id", "")) not in {
            "pen nib",
            "pennib",
        }:
            continue
        counter = int(getattr(relic, "counter", 0) or 0)
        if counter >= 9:
            return True
    return False


def power_amount(character, *names):
    return max(0, signed_power_amount(character, *names))


def signed_power_amount(character, *names):
    """Return a power amount without clamping signed powers such as Focus."""

    wanted = {_token(name) for name in names}
    # CommunicationMod power ids are not stable about the conventional
    # ``Power`` suffix.  For example, live Heart frames expose
    # ``BeatOfDeath`` while historical fixtures and several BaseMod versions
    # expose ``BeatOfDeathPower``.  Treat only a trailing suffix as an alias;
    # this keeps the match exact without depending on localized power names.
    wanted_bases = {
        token[:-5] if token.endswith("power") and len(token) > 5 else token
        for token in wanted
    }
    for power in getattr(character, "powers", []) or []:
        power_id = _token(getattr(power, "power_id", ""))
        power_name = _token(getattr(power, "power_name", ""))
        power_tokens = {power_id, power_name}
        power_bases = {
            token[:-5]
            if token.endswith("power") and len(token) > 5
            else token
            for token in power_tokens
        }
        if power_tokens & wanted or power_bases & wanted_bases:
            return int(getattr(power, "amount", 0) or 0)
    return 0


def has_power(character, *names):
    wanted = {_token(name) for name in names}
    wanted_bases = {
        token[:-5] if token.endswith("power") and len(token) > 5 else token
        for token in wanted
    }
    for power in getattr(character, "powers", []) or []:
        power_id = _token(getattr(power, "power_id", ""))
        power_name = _token(getattr(power, "power_name", ""))
        power_tokens = {power_id, power_name}
        power_bases = {
            token[:-5]
            if token.endswith("power") and len(token) > 5
            else token
            for token in power_tokens
        }
        if power_tokens & wanted or power_bases & wanted_bases:
            return True
    return False


def can_gain_block(game_or_character):
    """Whether new block may be created in the current player state.

    Panic Button's No Block power does not erase block that already exists;
    it only makes every later gainBlock call a no-op.  Keeping this predicate
    here gives the end-turn predictor and ordered planner one definition.
    """

    player = getattr(game_or_character, "player", game_or_character)
    return not has_power(player, "No Block", "NoBlockPower")


def block_after_extra_dexterity(character, raw_gain, extra_dexterity=0):
    """Return the marginal Block from a same-turn Dexterity change.

    Card ``block`` values in a CommunicationMod frame already include the
    player's authoritative Dexterity and Frail.  A branch-local source such
    as Footwork or Kunai therefore cannot simply add its Dexterity amount to
    a later card's serialized Block: Frail applies after Dexterity and the
    game's integer rounding can reduce the marginal gain.  ``raw_gain`` is
    the unmodified gainBlock amount for the source; callers that cannot recover
    it should pass ``None`` and receive the conservative ``floor(0.75 *
    delta)`` fallback below.
    """

    delta = max(0, int(extra_dexterity or 0))
    if delta <= 0:
        return 0
    player = getattr(character, "player", character)
    if has_power(player, "Frail", "FrailPower"):
        if raw_gain is None:
            # Never overstate a rescue when a dynamic card (for example an
            # X-cost block) does not expose its pre-Dexterity amount.
            return (delta * 3) // 4
        base = max(0, int(raw_gain or 0)) + signed_power_amount(
            player, "Dexterity", "DexterityPower"
        )
        before = max(0, int(base * 0.75))
        after = max(0, int((base + delta) * 0.75))
        return max(0, after - before)
    return delta


def resolve_player_damage_events(
    game, events, block=0, buffer_layers=0, *, force_intangible=False
):
    """Resolve events as Intangible -> Block -> Buffer -> Tungsten."""

    relic_ids = {
        _token(getattr(relic, "relic_id", ""))
        for relic in getattr(game, "relics", []) or []
    }
    tungsten = "tungsten rod" in relic_ids or "tungstenrod" in relic_ids
    intangible = force_intangible or has_power(
        getattr(game, "player", None), "Intangible", "IntangiblePlayer"
    )
    remaining_block = max(0, int(block or 0))
    remaining_buffer = max(0, int(buffer_layers or 0))
    hp_loss = 0
    hp_loss_events = []
    for event in events:
        event_loss = max(0, int(getattr(event, "amount", 0) or 0))
        if event_loss <= 0:
            continue
        if intangible:
            event_loss = 1
        if getattr(event, "blockable", False):
            absorbed = min(remaining_block, event_loss)
            remaining_block -= absorbed
            event_loss -= absorbed
        if event_loss <= 0:
            continue
        if remaining_buffer > 0:
            remaining_buffer -= 1
            continue
        if tungsten:
            event_loss = max(0, event_loss - 1)
        if event_loss > 0:
            hp_loss_events.append(
                PlayerDamageEvent(
                    str(getattr(event, "source", "") or ""),
                    event_loss,
                    bool(getattr(event, "blockable", False)),
                )
            )
        hp_loss += event_loss
    return PlayerDamageOutcome(
        hp_loss,
        remaining_block,
        remaining_buffer,
        tuple(hp_loss_events),
    )


def resolve_non_attack_damage_events(
    game, amounts, buffer_layers=0, *, bufferable=False, block=0
):
    """Compatibility wrapper for ordered player-side damage amounts."""

    outcome = resolve_player_damage_events(
        game,
        tuple(
            PlayerDamageEvent("non_attack", amount, blockable=bufferable)
            for amount in amounts
        ),
        block=block,
        buffer_layers=buffer_layers,
    )
    return outcome.hp_loss, outcome.buffer


def living_monsters(game):
    return [
        monster
        for monster in getattr(game, "monsters", []) or []
        if getattr(monster, "current_hp", 0) > 0
        and not getattr(monster, "half_dead", False)
        and not getattr(monster, "is_gone", False)
    ]


def is_intangible(monster):
    return has_power(monster, "Intangible", "IntangiblePlayer")


def has_unresolved_damage_cap(monster):
    # CommunicationMod exposes Invincible's configured cap, but not the
    # remaining cap for the current turn.  Treating the Heart as guaranteed
    # dead would therefore be unsafe.
    return has_power(monster, "Invincible", "InvinciblePower")


def poison_damage_before_action(monster):
    poison = power_amount(monster, "Poison")
    if poison <= 0:
        return 0
    return 1 if is_intangible(monster) else poison


def monster_regeneration_amount(monster):
    """Return the enemy Regenerate tick exposed by the current frame."""

    return power_amount(monster, "Regenerate", "RegeneratePower")


def projected_monster_end_turn_healing(
    monster, hp_after_player, *, combat_ends_before_enemy_turn=False
):
    """Return HP an enemy will regain after its next material turn.

    This is deliberately a post-player-action helper.  Regenerate does not
    make a living target safe to leave at one HP: it heals after the enemy's
    action, while a target killed before that boundary receives no heal.
    """

    hp = max(0, int(hp_after_player or 0))
    if hp <= 0 or combat_ends_before_enemy_turn:
        return 0
    amount = monster_regeneration_amount(monster)
    if amount <= 0:
        return 0
    max_hp = max(hp, int(getattr(monster, "max_hp", hp) or hp))
    return min(amount, max(0, max_hp - hp))


def lock_on_amount(monster):
    """Return Lock-On duration across protocol id/name variants."""

    return power_amount(
        monster,
        "Lockon",
        "Lock-On",
        "Lock On",
        "LockOnPower",
    )


def orb_damage_after_lock_on(monster, amount, *, lock_on_override=None):
    """Apply base-game ``AbstractOrb.applyLockOn`` integer rounding.

    Lock-On is not a generic target damage modifier.  Lightning and Dark
    actions explicitly call ``applyLockOn`` for each target/packet; Attacks,
    Poison and unrelated THORNS/direct packets bypass this helper.
    """

    amount = max(0, int(amount or 0))
    locked = (
        lock_on_amount(monster) > 0
        if lock_on_override is None
        else int(lock_on_override or 0) > 0
    )
    return int(amount * 1.5) if locked and amount > 0 else amount


def _lightning_damage(game, monster, living_count):
    # A passive Lightning orb is guaranteed to hit only when one monster is
    # alive.  In multi-enemy fights the target is random, so do not reserve a
    # kill based on it.
    if living_count != 1:
        return 0
    raw = sum(
        orb_damage_after_lock_on(
            monster,
            max(0, int(getattr(orb, "passive_amount", 0) or 0)),
        )
        for orb in getattr(getattr(game, "player", None), "orbs", []) or []
        if _token(getattr(orb, "orb_id", "")) == "lightning"
    )
    return attack_hp_loss(
        monster,
        raw,
        hits=sum(
            1
            for orb in getattr(
                getattr(game, "player", None), "orbs", []
            ) or []
            if _token(getattr(orb, "orb_id", "")) == "lightning"
        ),
        vulnerable_eligible=False,
    )


def _combust_damage(game, monster):
    player = getattr(game, "player", None)
    amount = power_amount(player, "Combust")
    if amount <= 0:
        return 0
    return attack_hp_loss(
        monster, amount, hits=1, vulnerable_eligible=False
    )


def attack_damage_after_target_modifiers(
    monster,
    raw_damage,
    hits=1,
    *,
    vulnerable_override=None,
    vulnerable_multiplier=1.5,
    minimum_damage=0,
    slow_override=None,
    extra_slow_cards=0,
):
    """Return Attack damage after target Vulnerable/Intangible, before Block.

    ``Card.damage`` is shared by every possible target, so CommunicationMod
    cannot fold an enemy-local Vulnerable power into that serialized value.
    Apply the complete target multiplier chain to each hit with one final
    integer round down, then apply Intangible to each hit.  In the game,
    Vulnerable and Slow both operate on the same floating-point packet; flooring
    between them underpredicts combinations such as Paper Frog + Slow.
    Non-Attack packets deliberately
    bypass this helper because Poison, orbs, Combust, Choke, and Corpse
    Explosion do not receive the Vulnerable multiplier.

    ``vulnerable_override`` is used by ordered-turn search after a card in the
    simulated branch applies or consumes a debuff.  ``None`` means to read the
    authoritative monster power in the current frame. ``minimum_damage`` is
    retained for API compatibility, but The Boot's low-damage floor is applied
    by :func:`attack_hp_loss` *after each hit has consumed Block*.  ``Slow`` is a
    target-local multiplier whose amount may grow as cards are played in the
    same turn; ``slow_override`` supplies the complete amount while
    ``extra_slow_cards`` adds the branch-local card count.
    """

    raw_damage = max(0, int(raw_damage or 0))
    hits = max(1, int(hits or 1))
    vulnerable = (
        has_power(monster, "Vulnerable")
        if vulnerable_override is None
        else int(vulnerable_override or 0) > 0
    )
    per_hit, remainder = divmod(raw_damage, hits)
    adjusted = 0
    intangible = is_intangible(monster)
    flight_active_for_resolution = (
        power_amount(monster, "Flight", "FlightPower") > 0
    )
    vulnerable_multiplier = max(0.0, float(vulnerable_multiplier or 0))
    minimum_damage = max(0, int(minimum_damage or 0))
    slow_multiplier = slow_damage_multiplier(
        monster,
        extra_cards=extra_slow_cards,
        slow_override=slow_override,
    )
    target_multiplier = slow_multiplier * (
        vulnerable_multiplier if vulnerable else 1.0
    )
    for hit_index in range(hits):
        amount = per_hit + int(hit_index < remainder)
        if amount > 0 and target_multiplier != 1.0:
            amount = floor_damage_product(amount, target_multiplier)
        flight_applied = flight_active_for_resolution and amount > 0
        if flight_applied:
            # A card queues all of its damage packets before its single
            # Flight-reduction action resolves.  Consequently every hit from
            # this card sees the Flight state that existed when the card
            # started resolving, even when the remaining stack count is lower
            # than the hit count.
            amount = int(amount * 0.5)
        if intangible and amount > 0:
            amount = 1
        adjusted += amount
    return adjusted


def attack_hp_loss(
    monster,
    raw_damage,
    hits=1,
    extra_block=0,
    *,
    vulnerable_eligible=True,
    vulnerable_override=None,
    vulnerable_multiplier=1.5,
    minimum_damage=0,
    slow_override=None,
    extra_slow_cards=0,
):
    """Return HP loss from a player Attack after target modifiers and Block.

    Set ``vulnerable_eligible=False`` for non-Attack damage packets which use
    this block/intangible utility path.
    """

    raw_damage = max(0, int(raw_damage or 0))
    hits = max(1, int(hits or 1))
    minimum_damage = max(0, int(minimum_damage or 0))
    block = max(
        0,
        int(getattr(monster, "block", 0) or 0) + int(extra_block or 0),
    )

    # Most effects can be summed before Block.  The Boot is the important
    # exception: the game first consumes Block for *each* Attack hit, then
    # raises a positive 1--4 unblocked packet to five.  Treating the floor as
    # a pre-Block modifier underpredicts every real packet such as 12 damage
    # into 11 Block (the live A0 trace observed five, not one), and also loses
    # the per-hit ordering of Sword Boomerang/Twin Strike.
    per_hit, remainder = divmod(raw_damage, hits)
    packets = []
    vulnerable = (
        has_power(monster, "Vulnerable")
        if vulnerable_override is None
        else int(vulnerable_override or 0) > 0
    )
    intangible = is_intangible(monster)
    flight_active_for_resolution = (
        power_amount(monster, "Flight", "FlightPower") > 0
    )
    slow_multiplier = slow_damage_multiplier(
        monster,
        extra_cards=extra_slow_cards,
        slow_override=slow_override,
    )
    vulnerable_multiplier = max(0.0, float(vulnerable_multiplier or 0))
    target_multiplier = (
        slow_multiplier
        * (vulnerable_multiplier if vulnerable else 1.0)
        if vulnerable_eligible else 1.0
    )
    for hit_index in range(hits):
        amount = per_hit + int(hit_index < remainder)
        if amount > 0 and target_multiplier != 1.0:
            amount = floor_damage_product(amount, target_multiplier)
        flight_applied = (
            vulnerable_eligible and flight_active_for_resolution and amount > 0
        )
        if flight_applied:
            amount = int(amount * 0.5)
        if intangible and amount > 0:
            amount = 1
        packets.append(max(0, amount))

    hp_loss = 0
    for amount in packets:
        absorbed = min(block, amount)
        block -= absorbed
        unblocked = max(0, amount - absorbed)
        if minimum_damage > 0 and unblocked > 0:
            unblocked = max(unblocked, minimum_damage)
        hp_loss += unblocked
    return hp_loss


def weak_attack_per_hit_after_target_modifiers(
    game,
    card,
    monster,
    serialized_damage,
    hits,
    *,
    vulnerable_override=None,
    slow_override=None,
    extra_slow_cards=0,
):
    """Recover one exact Weak Attack hit across all target multipliers.

    CommunicationMod serializes ``card.damage`` after the player's Weak
    modifier has already been floored.  The game instead keeps the underlying
    packet as a float while applying target Vulnerable and Slow, then truncates
    once.  Reconstruct the pre-Weak packet only when base damage, Strength and
    every serialized hit prove it; card-specific or hidden modifiers therefore
    remain on the conservative fallback path.
    """

    hits = max(1, int(hits or 1))
    serialized_damage = max(0, int(serialized_damage or 0))
    player = getattr(game, "player", None)
    if not has_power(player, "Weak", "Weakened"):
        return None
    serialized_per_hit, remainder = divmod(serialized_damage, hits)
    if remainder:
        return None
    base_damage = max(0, int(getattr(card, "base_damage", 0) or 0))
    strength = signed_power_amount(player, "Strength", "StrengthPower")
    card_id = _token(getattr(card, "card_id", ""))
    compact_card_id = card_id.replace(" ", "")
    if compact_card_id == "perfectedstrike":
        strike_count = sum(
            1
            for deck_card in getattr(game, "deck", []) or []
            if "strike" in _token(getattr(deck_card, "card_id", ""))
        )
        magic_number = int(getattr(card, "magic_number", 0) or 0)
        pre_weak_damage = max(
            0, base_damage + magic_number * strike_count + strength,
        )
    elif compact_card_id == "heavyblade":
        strength_multiplier = max(
            1, int(getattr(card, "magic_number", 0) or 0),
        )
        pre_weak_damage = max(
            0, base_damage + strength * strength_multiplier,
        )
    else:
        pre_weak_damage = max(0, base_damage + strength)
    if (
        pre_weak_damage <= 0
        or serialized_per_hit != int(pre_weak_damage * 0.75)
    ):
        return None
    vulnerable = (
        has_power(monster, "Vulnerable")
        if vulnerable_override is None
        else int(vulnerable_override or 0) > 0
    )
    target_multiplier = slow_damage_multiplier(
        monster,
        extra_cards=extra_slow_cards,
        slow_override=slow_override,
    )
    if vulnerable:
        target_multiplier *= vulnerable_damage_multiplier(game)
    return floor_damage_product(
        pre_weak_damage, 0.75, target_multiplier
    )


def card_attack_hp_loss(game, card, monster, *, target=None):
    """Return one current-card Attack packet from authoritative card facts.

    ``Card.damage`` already contains ordinary player-side modifiers.  Weak is
    the narrow exception relevant to the live audit: the bridge serializes its
    floored value, while the game combines base damage plus Strength, Weak and
    target Vulnerable/Slow before the final truncation.  Reconstruct that
    packet only when the base/dynamic pair proves it; otherwise retain the
    serialized packet and the common target-side modifier path.
    """

    raw_damage, hits = card_attack_profile(game, card, target=target)
    raw_damage = max(0, int(raw_damage or 0))
    hits = max(1, int(hits or 1))
    exact_per_hit = weak_attack_per_hit_after_target_modifiers(
        game, card, monster, raw_damage, hits,
    )
    if exact_per_hit is not None:
        return attack_hp_loss(
            monster,
            exact_per_hit * hits,
            hits=hits,
            vulnerable_override=0,
            slow_override=0,
            vulnerable_multiplier=vulnerable_damage_multiplier(game),
            minimum_damage=boot_minimum_damage(game),
        )
    return attack_hp_loss(
        monster,
        raw_damage,
        hits=hits,
        **attack_relic_modifiers(game),
    )


def card_energy_cost(game, card):
    """Energy consumed by a card for conservative same-turn search."""

    cost = int(getattr(card, "cost", 0) or 0)
    if cost == -1:
        return max(0, int(getattr(getattr(game, "player", None), "energy", 0) or 0))
    return max(0, cost)


def x_cost_effect(game, card=None, *, energy_override=None, upgraded_bonus=False):
    """Return the effect count for an X-cost card in the requested state.

    ``game.player.energy`` is authoritative for the current frame.  Ordered
    turn search can pass a branch-local override after earlier cards have
    spent or generated energy.  Chemical X and card upgrade bonuses are
    effects rather than energy, so they apply in both cases.
    """

    if energy_override is None:
        energy = int(
            getattr(getattr(game, "player", None), "energy", 0) or 0
        )
    else:
        energy = int(energy_override or 0)
    value = max(0, energy)
    relic_ids = {
        _token(getattr(relic, "relic_id", ""))
        for relic in getattr(game, "relics", []) or []
    }
    if "chemical x" in relic_ids:
        value += 2
    # Only a subset of X-cost cards gain one additional *effect* when
    # upgraded.  Whirlwind+/Skewer+/Reinforced Body+ improve their per-effect
    # number instead; treating every upgrade as X+1 makes those cards appear
    # useful at zero energy and can emit a literal no-op play.
    upgrade_adds_one_effect = {
        "collect",
        "doppelganger",
        "malaise",
        "multi cast",
        "multicast",
        "transmutation",
    }
    if (
        upgraded_bonus
        and card is not None
        and _token(getattr(card, "card_id", ""))
        in upgrade_adds_one_effect
    ):
        value += max(0, int(getattr(card, "upgrades", 0) or 0))
    return value


def card_attack_profile(
    game,
    card,
    *,
    energy_override=None,
    target=None,
    poisoned_override=None,
):
    """Return guaranteed ``(raw damage, hit count)`` for one enemy.

    CommunicationMod's live ``card.damage`` already includes player-side
    modifiers such as Strength, Weak, and Double Damage. Do not apply them
    again; target-local Vulnerable is applied by ``attack_hp_loss`` (or by ordered
    branch state), after a concrete enemy has been selected.  This function
    adds deterministic hit counts and deliberately refuses to assign random
    attacks to one target in multi-enemy fights.
    """

    card_id = _token(getattr(card, "card_id", ""))
    hits = {
        "dagger spray": 2,
        "glass knife": 2,
        "twin strike": 2,
        "riddle with holes": 5,
        "pummel": 4 + int(getattr(card, "upgrades", 0) or 0),
        "sword boomerang": 3 + int(getattr(card, "upgrades", 0) or 0),
        # Rip and Tear deals two independent random packets.  It is not an
        # AOE attack: with several enemies the packets may land on either
        # target, but the total number of packets is still authoritative.
        "rip and tear": 2,
        # CommunicationMod includes Empty slots in player.orbs.  Barrage only
        # fires once per occupied slot, so counting the serialized list can
        # invent two extra hits at the default three-slot capacity.
        "barrage": sum(
            1
            for orb in getattr(getattr(game, "player", None), "orbs", []) or []
            if _token(getattr(orb, "orb_id", "")) not in {"", "empty"}
        ),
    }.get(card_id, 1)

    if card_id == "fiend fire":
        hits = max(0, len(getattr(game, "hand", []) or []) - 1)
    elif card_id == "flechettes":
        hits = sum(
            1 for hand_card in getattr(game, "hand", []) or []
            if str(getattr(getattr(hand_card, "type", None), "name", "")) == "SKILL"
        )
    elif card_id == "bane":
        poisoned = (
            bool(poisoned_override)
            if poisoned_override is not None
            else target is not None and power_amount(target, "Poison") > 0
        )
        # Bane queues a second copy of its damage action only when Poison is
        # already present as the card begins resolving.  Callers simulating
        # an ordered branch can override the authoritative target power after
        # an earlier Deadly Poison.
        hits = 2 if poisoned else 1

    if card_id in {"skewer", "whirlwind"}:
        hits = x_cost_effect(game, card, energy_override=energy_override)

    # CommunicationMod exposes AbstractCard.damage: the current displayed
    # packet after player-side modifiers (including Strength, Weak, and
    # temporary Strength such as Flex) have already been applied.  Reapplying
    # the visible power stack would double-count it and make the planner's
    # first-hit prediction disagree with the authoritative HP delta.
    damage_per_hit = max(0, int(getattr(card, "damage", 0) or 0))
    # Passive-doomed monsters remain valid random targets until the end-turn
    # effect actually kills them, so Sword Boomerang cannot claim guaranteed
    # damage on one concrete enemy.  Its hit count is still authoritative,
    # though: every random hit can trigger Thorns and other reactive damage.
    if card_id == "sword boomerang" and len(living_monsters(game)) > 1:
        return 0, max(0, hits)
    return damage_per_hit * max(0, hits), max(0, hits)


def _corpse_explosion_stacks(monster):
    return power_amount(monster, "Corpse Explosion", "CorpseExplosionPower")


def projected_doomed_monsters(game):
    """Return monsters guaranteed to die before taking their next action.

    Existing Poison ignores block. Deterministic effects which resolve before
    the enemy's current move are added conservatively. Noxious Fumes is not
    damage and its next application happens only at the following player-turn
    start, after this move. Corpse Explosion cascades are evaluated iteratively.
    Invincible targets are never declared doomed because the remaining damage
    cap is absent from the protocol.
    """

    living = living_monsters(game)
    doomed_ids = set()
    projected_hp = {id(monster): int(monster.current_hp) for monster in living}

    for monster in living:
        if has_unresolved_damage_cap(monster):
            continue
        damage = poison_damage_before_action(monster)
        damage += _lightning_damage(game, monster, len(living))
        damage += _combust_damage(game, monster)
        damage += stone_calendar_hp_loss(game, monster)
        projected_hp[id(monster)] -= damage
        if projected_hp[id(monster)] <= 0:
            doomed_ids.add(id(monster))

    exploded_ids = set()
    while True:
        new_sources = [
            monster
            for monster in living
            if id(monster) in doomed_ids
            and id(monster) not in exploded_ids
            and _corpse_explosion_stacks(monster) > 0
        ]
        if not new_sources:
            break
        for source in new_sources:
            exploded_ids.add(id(source))
            raw = int(getattr(source, "max_hp", 0) or 0) * _corpse_explosion_stacks(source)
            for target in living:
                if target is source or id(target) in doomed_ids or has_unresolved_damage_cap(target):
                    continue
                projected_hp[id(target)] -= attack_hp_loss(
                    target,
                    raw,
                    hits=1,
                    vulnerable_eligible=False,
                )
                if projected_hp[id(target)] <= 0:
                    doomed_ids.add(id(target))
    return [monster for monster in living if id(monster) in doomed_ids]


def passive_damage_before_action(game, monster, living_count=None):
    """Deterministic enemy HP loss which resolves before its current move.

    Noxious Fumes is deliberately excluded: it applies Poison at the next
    player-turn start, after the displayed enemy move, and does not deal an
    immediate damage packet when it applies.
    """

    living_count = (
        len(living_monsters(game))
        if living_count is None
        else max(0, int(living_count or 0))
    )
    return max(0, (
        poison_damage_before_action(monster)
        + _lightning_damage(game, monster, living_count)
        + _combust_damage(game, monster)
        + stone_calendar_hp_loss(game, monster)
    ))


def passive_action_damage_before_move(game, monster, living_count=None):
    """Damage that is authoritative before the monster's current move.

    CommunicationMod traces show Noxious Fumes being applied after the
    current enemy action.  Counting that application here makes a monster
    with (for example) 11 HP and 9 Poison look dead, suppresses its attack,
    and can leave a low-HP player with an unpredicted hit.  Existing Poison,
    deterministic orbs, and Combust are still available before the move.
    """

    living_count = (
        len(living_monsters(game))
        if living_count is None
        else max(0, int(living_count or 0))
    )
    return max(0, (
        poison_damage_before_action(monster)
        + _lightning_damage(game, monster, living_count)
        + _combust_damage(game, monster)
        + stone_calendar_hp_loss(game, monster)
    ))


def passive_action_suppressed_monsters(game):
    """Enemies whose displayed move is replaced before it can resolve.

    This is intentionally narrower than ``projected_doomed_monsters``.  It
    also covers deterministic phase changes such as Slime Boss splitting,
    where the enemy remains present but its stale attack is cancelled.

    Transient is deliberately absent: Fading reaches zero only after its
    current attack resolves, so even a serialized Fading=1 intent remains
    lethal unless damage, block, or another real transition prevents it.
    """

    living = living_monsters(game)
    suppressed = []
    # This module's token normalizer preserves word separators. Runtime
    # CommunicationMod ids use AcidSlime_L/SpikeSlime_L, which become
    # "acidslime l"/"spikeslime l" rather than the compact ids used by
    # combat_planner. Keep both historical spellings explicit.
    split_ids = {
        "slimeboss",
        "acidslimel",
        "spikeslimel",
        "acidslime l",
        "spikeslime l",
    }
    projected_hp = {
        id(monster): max(
            0, int(getattr(monster, "current_hp", 0) or 0)
        )
        for monster in living
    }
    dead_ids = set()
    acted_ids = set()
    exploded_ids = set()

    def resolve_corpse_explosions(initial_sources):
        queue = list(initial_sources)
        while queue:
            source = queue.pop(0)
            source_id = id(source)
            stacks = _corpse_explosion_stacks(source)
            if source_id in exploded_ids or stacks <= 0:
                continue
            exploded_ids.add(source_id)
            raw = max(
                0, int(getattr(source, "max_hp", 0) or 0) * stacks
            )
            for target in living:
                target_id = id(target)
                if (
                    target is source
                    or target_id in dead_ids
                    or has_unresolved_damage_cap(target)
                ):
                    continue
                projected_hp[target_id] -= attack_hp_loss(
                    target,
                    raw,
                    hits=1,
                    vulnerable_eligible=False,
                )
                if projected_hp[target_id] <= 0:
                    dead_ids.add(target_id)
                    if target_id not in acted_ids:
                        suppressed.append(target)
                    if _corpse_explosion_stacks(target) > 0:
                        queue.append(target)

    for monster in living:
        if has_unresolved_damage_cap(monster):
            acted_ids.add(id(monster))
            continue
        if id(monster) in dead_ids:
            if monster not in suppressed:
                suppressed.append(monster)
            continue
        damage = passive_action_damage_before_move(game, monster, len(living))
        current_hp = max(0, int(getattr(monster, "current_hp", 0) or 0))
        projected_hp[id(monster)] -= damage
        final_hp = max(0, projected_hp[id(monster)])
        if final_hp <= 0:
            suppressed.append(monster)
            dead_ids.add(id(monster))
            resolve_corpse_explosions([monster])
            continue
        monster_id = _token(getattr(monster, "monster_id", ""))
        max_hp = max(0, int(getattr(monster, "max_hp", 0) or 0))
        if (
            monster_id in split_ids
            and current_hp > max_hp // 2 >= final_hp
        ):
            suppressed.append(monster)
            continue
        mode_shift = power_amount(
            monster, "Mode Shift", "ModeShift", "ModeShiftPower"
        )
        if mode_shift > 0 and damage >= mode_shift:
            suppressed.append(monster)
            continue
        # The Champ's half-health transition changes his *next* intent.  The
        # currently serialized attack still resolves, including when a player
        # card crosses the threshold. Unlike Split or Mode Shift, Anger is
        # selected by getMove rather than interrupting the current action.
        acted_ids.add(id(monster))
    return suppressed


def active_monsters(game):
    living = living_monsters(game)
    doomed_ids = {id(monster) for monster in projected_doomed_monsters(game)}
    return [monster for monster in living if id(monster) not in doomed_ids]


def attack_monsters(game):
    """Living monsters whose current displayed move still resolves.

    ``active_monsters`` is deliberately a target-selection set: a monster
    that will die from its existing Poison tick should not receive another
    attack or poison card. It is not safe for forecasting the incoming hit of
    the current enemy turn. This set uses only effects confirmed to suppress
    that move immediately.
    """

    living = living_monsters(game)
    suppressed_ids = {
        id(monster) for monster in passive_action_suppressed_monsters(game)
    }
    return [monster for monster in living if id(monster) not in suppressed_ids]


def projected_deaths_apply_player_vulnerable(
    game, doomed_monsters=None, artifact_layers=None
):
    """Whether guaranteed deaths apply Vulnerable before surviving attacks.

    Fungi Beast's Spore Cloud resolves when the poisoned beast dies, before
    the remaining monsters take their turns.  Merely removing the doomed
    attacker therefore underestimates every later attack unless the player
    already has Vulnerable or enough Artifact to absorb all death triggers.
    Keeping this as a state transition also covers card/orb/Combust kills
    supplied by the ordered planner rather than special-casing poison.
    """

    player = getattr(game, "player", None)
    if player is None or has_power(player, "Vulnerable"):
        return False
    doomed = (
        passive_action_suppressed_monsters(game)
        if doomed_monsters is None
        else list(doomed_monsters)
    )
    spore_triggers = sum(
        1
        for monster in doomed
        if power_amount(monster, "Spore Cloud", "SporeCloudPower") > 0
    )
    # Passive Lightning chooses a random target. It cannot reserve a concrete
    # kill for targeting, but survival must still cover the branch where it
    # kills a non-attacking Fungi Beast and Spore Cloud strengthens every
    # later attack. One possible trigger is sufficient unless Artifact
    # absorbs it.
    if doomed_monsters is None and _possible_random_lightning_spore_death(game):
        spore_triggers += 1
    artifact = (
        power_amount(player, "Artifact")
        if artifact_layers is None
        else max(0, int(artifact_layers or 0))
    )
    return spore_triggers > artifact


def _possible_random_lightning_spore_death(game):
    """Whether passive random Lightning can kill a non-attacking spore beast."""

    living = living_monsters(game)
    if len(living) <= 1:
        return False
    player = getattr(game, "player", None)
    lightning = [
        orb
        for orb in getattr(player, "orbs", []) or []
        if _token(getattr(orb, "orb_id", "")) == "lightning"
        and int(getattr(orb, "passive_amount", 0) or 0) > 0
    ]
    if not lightning:
        return False
    for monster in living:
        if (
            power_amount(monster, "Spore Cloud", "SporeCloudPower") <= 0
            or monster_damage_packets(monster)
        ):
            continue
        possible_loss = _lightning_damage(game, monster, 1)
        if possible_loss >= max(
            1, int(getattr(monster, "current_hp", 0) or 0)
        ):
            return True
    return False


def _post_death_vulnerable_damage(game, damage):
    """Apply newly gained Vulnerable to a serialized pre-trigger intent."""

    damage = max(0, int(damage or 0))
    relic_ids = {
        _token(getattr(relic, "relic_id", ""))
        for relic in getattr(game, "relics", []) or []
    }
    if "odd mushroom" in relic_ids or "oddmushroom" in relic_ids:
        return damage * 5 // 4
    return damage * 3 // 2


def monster_threat(monster):
    return sum(packet.damage_per_hit * packet.hits for packet in monster_damage_packets(monster))


def next_turn_attack_upper_bound(game, monster):
    """Return a fail-closed bound for a known monster's next attack.

    This is deliberately not a generic intent prediction API.  Most enemy
    move rolls are hidden, so unknown encounters return zero rather than
    inventing a future attack.  Acid Slime (M) is useful and observable at
    A0: Tackle cannot repeat immediately, while Corrosive Spit and Lick may
    be followed by Tackle.  Above A16 that restriction changes and the
    current move alone no longer narrows the maximum.
    """

    if _token(getattr(monster, "monster_id", "")) != "acidslime m":
        return 0
    intent = getattr(monster, "intent", None)
    intent_id = _token(getattr(intent, "name", intent))
    if intent_id not in {"attack", "attack debuff", "debuff"}:
        return 0

    ascension = max(0, int(getattr(game, "ascension_level", 0) or 0))
    tackle_damage = 12 if ascension >= 2 else 10
    corrosive_spit_damage = 8 if ascension >= 2 else 7
    if ascension < 17 and intent_id == "attack":
        return corrosive_spit_damage
    return tackle_damage


def monster_damage_packets(monster, unknown_intent_damage=0):
    """Return deterministic damage packets the monster will execute now."""

    packets = []
    damage = max(0, int(getattr(monster, "move_adjusted_damage", 0) or 0))
    hits = max(0, int(getattr(monster, "move_hits", 0) or 0))
    if damage > 0 and hits > 0:
        packets.append(DamagePacket("declared_attack", damage, hits))
    elif unknown_intent_damage > 0 and str(getattr(monster, "intent", "")).upper().endswith("NONE"):
        # Runic Dome hides intents by serializing NONE and no damage.  Never
        # turn that missing information into a free end turn.
        packets.append(DamagePacket("hidden_intent", unknown_intent_damage, 1))

    monster_id = _token(getattr(monster, "monster_id", ""))
    explosive = power_amount(monster, "Explosive")
    if monster_id == "exploder" and explosive == 1:
        # Exploder's countdown power deals THORNS damage. It is blockable and
        # still goes through Intangible, Buffer and Tungsten Rod, but Torii's
        # DamageType check explicitly excludes it.
        packets.append(
            DamagePacket("exploder_explosion", 30, 1, False, False)
        )
    return packets


def monster_pre_attack_block_gain(monster):
    """Return deterministic Block which resolves before this attack move.

    Spheric Guardian's ATTACK_DEFEND move queues its 15 Block before the
    attack.  Reflected Thorns/Bronze Scales damage therefore hits that Block,
    even when the authoritative player-turn frame still shows zero enemy
    Block.  Keep this exact encounter transition beside the shared enemy
    attack pipeline so every character observes the same ordering.
    """

    monster_id = _token(
        getattr(monster, "monster_id", "")
    ).replace(" ", "")
    intent = getattr(monster, "intent", None)
    intent_id = _token(getattr(intent, "name", intent))
    if monster_id == "sphericguardian" and intent_id == "attack defend":
        return 15
    return 0


def monster_attack_healing(monster, final_hp_damage):
    """Healing caused by the current declared Attack hit.

    Shelled Parasite's Suck heals for the HP damage which actually reached
    the player.  CommunicationMod exposes Suck as ``ATTACK_BUFF``; its other
    attack has a plain ``ATTACK`` intent.  Keep this calculation beside the
    shared enemy-attack pipeline so Block, Buffer, Torii, and Tungsten Rod
    have already reduced the value before it can heal the monster.
    """

    monster_id = _token(
        getattr(monster, "monster_id", "")
    ).replace(" ", "")
    intent = getattr(monster, "intent", None)
    intent_id = _token(getattr(intent, "name", intent))
    if monster_id != "shelledparasite" or intent_id != "attack buff":
        return 0
    return max(0, int(final_hp_damage or 0))


def incoming_damage(game):
    player = getattr(game, "player", None)
    intangible = has_power(player, "Intangible", "IntangiblePlayer")
    new_vulnerable = projected_deaths_apply_player_vulnerable(game)
    total = 0
    relic_ids = {_token(getattr(relic, "relic_id", "")) for relic in getattr(game, "relics", []) or []}
    hidden = 20 if "runicdome" in relic_ids or "runic dome" in relic_ids else 0
    for monster in attack_monsters(game):
        for packet in monster_damage_packets(monster, hidden):
            damage = packet.damage_per_hit
            if new_vulnerable and packet.vulnerable_eligible and damage > 0:
                damage = _post_death_vulnerable_damage(game, damage)
            if intangible and damage > 0:
                damage = 1
            total += damage * packet.hits
    return total


def _projected_attack_outcome(
    game,
    extra_block=0,
    excluded_monsters=None,
    extra_buffer=0,
    force_intangible=False,
    block_override=None,
    buffer_override=None,
    active_monsters_override=None,
    per_hit_damage_bonus=None,
    per_hit_damage_includes_player_vulnerable=None,
    force_player_vulnerable=None,
    damage_packet_overrides=None,
    attack_hp_loss_reduction=0,
    enemy_attack_healing=None,
    attack_hit_reaction=None,
    attacker_is_active=None,
    enemy_reaction_damage=None,
    enemy_pre_attack_block_gain=None,
    player_thorns_override=None,
    player_flame_barrier_override=None,
    player_damage=None,
):
    player = getattr(game, "player", None)
    if player is None:
        return 0, 0, 0
    block = (
        max(0, int(block_override or 0))
        if block_override is not None
        else projected_end_block(
            game,
            extra_block=(
                max(0, int(extra_block or 0))
                if can_gain_block(game)
                else 0
            ),
        )
    )
    intangible = force_intangible or has_power(player, "Intangible", "IntangiblePlayer")
    buffer = (
        max(0, int(buffer_override or 0))
        if buffer_override is not None
        else power_amount(player, "Buffer") + max(0, int(extra_buffer or 0))
    )
    relic_ids = {_token(getattr(relic, "relic_id", "")) for relic in getattr(game, "relics", []) or []}
    has_torii = "torii" in relic_ids
    has_tungsten = "tungsten rod" in relic_ids or "tungstenrod" in relic_ids
    serialized_thorns = max(
        0, power_amount(player, "Thorns", "ThornsPower")
    )
    bronze_scales = bronze_scales_damage(game)
    hp_loss = 0
    remaining_attack_hp_loss_reduction = max(
        0, int(attack_hp_loss_reduction or 0)
    )
    pre_attack_deaths = (
        passive_action_suppressed_monsters(game)
        if force_player_vulnerable is None
        else list(excluded_monsters or [])
    )
    pre_attack_spore_triggers = sum(
        1
        for monster in pre_attack_deaths
        if power_amount(
            monster, "Spore Cloud", "SporeCloudPower"
        ) > 0
    )
    if (
        force_player_vulnerable is None
        and _possible_random_lightning_spore_death(game)
    ):
        pre_attack_spore_triggers += 1
    serialized_artifact = power_amount(player, "Artifact")
    death_applies_vulnerable = (
        pre_attack_spore_triggers > serialized_artifact
        if force_player_vulnerable is None
        else bool(force_player_vulnerable)
    )
    new_vulnerable = (
        not has_power(player, "Vulnerable")
        and death_applies_vulnerable
    )
    artifact_remaining = max(
        0, serialized_artifact - pre_attack_spore_triggers
    )
    if death_applies_vulnerable:
        artifact_remaining = 0
    excluded_ids = {id(monster) for monster in (excluded_monsters or [])}

    monsters = (
        attack_monsters(game)
        if active_monsters_override is None
        else list(active_monsters_override)
    )
    hidden_intent_damage = 20 if (
        "runicdome" in relic_ids or "runic dome" in relic_ids
    ) else 0
    player_thorns = (
        max(0, int(player_thorns_override or 0))
        if player_thorns_override is not None
        else serialized_thorns
    )
    # Bronze Scales and Caltrops both use ThornsPower, so they are one
    # stacked reaction packet. CommunicationMod normally serializes Bronze
    # Scales in that power; the relic is only a compatibility fallback for a
    # frame which omits it. A branch override already owns this effective
    # stack and must not receive another relic packet.
    if player_thorns_override is None and player_thorns <= 0:
        player_thorns = bronze_scales
    player_thorns_source = (
        "bronze_scales"
        if (
            serialized_thorns <= 0
            and player_thorns == bronze_scales
            and bronze_scales > 0
        )
        else "player_thorns"
    )
    # Flame Barrier is a separate power and queues its own THORNS
    # DamageAction. Do not merge it into ThornsPower: enemy Block,
    # Intangible, and immediate transitions can change between the packets.
    player_flame_barrier = (
        max(0, int(player_flame_barrier_override or 0))
        if player_flame_barrier_override is not None
        else max(
            0,
            power_amount(
                player, "Flame Barrier", "FlameBarrierPower"
            ),
        )
    )
    spore_cloud_resolved = set()
    vulnerable_included_ids = set(
        per_hit_damage_includes_player_vulnerable or ()
    )

    def apply_reactive_death_effects(monster):
        """Advance death powers before the next monster Attack packet."""

        nonlocal artifact_remaining, new_vulnerable
        monster_key = id(monster)
        if monster_key in spore_cloud_resolved:
            return
        spore_cloud_resolved.add(monster_key)
        if (
            has_power(player, "Vulnerable")
            or new_vulnerable
            or power_amount(
                monster, "Spore Cloud", "SporeCloudPower"
            ) <= 0
        ):
            return
        if artifact_remaining > 0:
            artifact_remaining -= 1
        else:
            new_vulnerable = True

    for monster in monsters:
        if id(monster) in excluded_ids:
            continue
        if callable(attacker_is_active) and not attacker_is_active(monster):
            continue
        # Existing Poison and other deterministic start-of-enemy-turn damage
        # resolve before this monster attacks. Reflection therefore starts
        # from the post-passive HP, not the serialized player-turn HP. Without
        # this transition a poisoned Fungi Beast appeared to survive Thorns,
        # hiding the Spore Cloud Vulnerable applied before the next attacker.
        monster_hp = max(
            0,
            int(getattr(monster, "current_hp", 0) or 0)
            - passive_action_damage_before_move(
                game, monster, len(living_monsters(game))
            ),
        )
        pre_attack_block = monster_pre_attack_block_gain(monster)
        monster_block = (
            max(0, int(getattr(monster, "block", 0) or 0))
            + pre_attack_block
        )
        if pre_attack_block > 0 and callable(enemy_pre_attack_block_gain):
            enemy_pre_attack_block_gain(monster, pre_attack_block)
        killed_by_thorns = False
        packets = (damage_packet_overrides or {}).get(id(monster))
        if packets is None:
            packets = monster_damage_packets(monster, hidden_intent_damage)
        for packet in packets:
            raw_per_hit = max(0, packet.damage_per_hit + int(
                (per_hit_damage_bonus or {}).get(id(monster), 0) or 0
            ))
            for _ in range(packet.hits):
                if (
                    callable(attacker_is_active)
                    and not attacker_is_active(monster)
                ):
                    killed_by_thorns = True
                    break
                per_hit = raw_per_hit
                if (
                    new_vulnerable
                    and packet.vulnerable_eligible
                    and per_hit > 0
                    and id(monster) not in vulnerable_included_ids
                ):
                    per_hit = _post_death_vulnerable_damage(game, per_hit)
                if intangible and per_hit > 0:
                    per_hit = 1
                blocked = min(block, per_hit)
                block -= blocked
                unblocked = per_hit - blocked

                reaction_damage = 0
                final_hp_damage = 0
                if unblocked > 0:
                    if buffer > 0:
                        buffer -= 1
                    else:
                        # Player powers receive this value before relic
                        # ``onAttacked`` hooks (Torii) and ``onLoseHpLast``
                        # hooks (Tungsten Rod).  Static Discharge therefore
                        # still fires when either relic later reduces the
                        # actual HP loss to zero.
                        reaction_damage = unblocked
                        if (
                            packet.torii_eligible
                            and has_torii
                            and unblocked <= 5
                        ):
                            unblocked = 1
                        if has_tungsten:
                            unblocked = max(0, unblocked - 1)
                        final_hp_damage = unblocked
                        prevented = min(
                            final_hp_damage,
                            remaining_attack_hp_loss_reduction,
                        )
                        final_hp_damage -= prevented
                        remaining_attack_hp_loss_reduction -= prevented
                        applied_hp_damage = final_hp_damage
                        if callable(player_damage):
                            applied_hp_damage = player_damage(
                                final_hp_damage
                            )
                        hp_loss += max(
                            0, int(applied_hp_damage or 0)
                        )

                # Suck's HealAction resolves from the final HP damage before
                # player on-hit reactions (Static Discharge/Thorns).  Apply
                # it to the predictor's concrete enemy copy for direct
                # callers, and notify the planner so its branch-local HP and
                # terminal classification advance through the same event.
                requested_healing = (
                    monster_attack_healing(monster, final_hp_damage)
                    if packet.source == "declared_attack"
                    else 0
                )
                if requested_healing > 0:
                    monster_max_hp = max(
                        monster_hp,
                        int(
                            getattr(monster, "max_hp", monster_hp)
                            or monster_hp
                        ),
                    )
                    applied_healing = min(
                        requested_healing,
                        max(0, monster_max_hp - monster_hp),
                    )
                    monster_hp += applied_healing
                    if callable(enemy_attack_healing):
                        enemy_attack_healing(
                            monster,
                            requested_healing,
                        )

                stopped_by_reaction = False
                if (
                    reaction_damage > 0
                    and packet.source == "declared_attack"
                    and callable(attack_hit_reaction)
                ):
                    reaction = attack_hit_reaction(
                        monster,
                        reaction_damage,
                        final_hp_damage,
                    )
                    if reaction is not None:
                        block += max(
                            0,
                            int(getattr(reaction, "block_gain", 0) or 0),
                        )
                        stopped_by_reaction = bool(
                            getattr(reaction, "stop_attacker", False)
                        )
                        for killed_monster in tuple(
                            getattr(reaction, "killed_monsters", ()) or ()
                        ):
                            apply_reactive_death_effects(killed_monster)

                # Ordinary Thorns (Bronze Scales + Caltrops) and Flame
                # Barrier both trigger on a declared Attack hit, even when
                # the hit was fully blocked. They remain separate damage
                # packets so Block, Intangible and immediate enemy
                # transitions resolve between them.
                if packet.source == "declared_attack":
                    reaction_packets = (
                        (player_thorns, player_thorns_source),
                        (player_flame_barrier, "flame_barrier"),
                    )
                    for reaction_amount, reaction_source in reaction_packets:
                        if reaction_amount <= 0:
                            continue
                        reflected = (
                            1
                            if is_intangible(monster)
                            else reaction_amount
                        )
                        callback_killed = False
                        if callable(enemy_reaction_damage):
                            reaction = enemy_reaction_damage(
                                monster, reflected, reaction_source
                            )
                            callback_killed = bool(
                                getattr(
                                    reaction, "stop_attacker", False
                                )
                            )
                            for killed_monster in tuple(
                                getattr(
                                    reaction, "killed_monsters", ()
                                )
                                or ()
                            ):
                                apply_reactive_death_effects(
                                    killed_monster
                                )
                        else:
                            reflected_blocked = min(
                                monster_block, reflected
                            )
                            monster_block -= reflected_blocked
                            monster_hp -= reflected - reflected_blocked
                        if (
                            (
                                callback_killed
                                or (
                                    not callable(enemy_reaction_damage)
                                    and monster_hp <= 0
                                )
                            )
                            and not has_unresolved_damage_cap(monster)
                        ):
                            killed_by_thorns = True
                            apply_reactive_death_effects(monster)
                            break
                if killed_by_thorns:
                    # A true reaction kill prevents later damage actions in
                    # the same multi-hit move.
                    break
                if stopped_by_reaction:
                    # The current hit already resolved.  A queued Lightning/
                    # Dark evoke which kills its source prevents only the
                    # later DamageActions in the same multi-hit move.
                    killed_by_thorns = True
                    break
            if killed_by_thorns:
                break
    return hp_loss, buffer, block


def projected_attack_hp_loss(
    game,
    extra_block=0,
    excluded_monsters=None,
    extra_buffer=0,
    force_intangible=False,
    block_override=None,
    buffer_override=None,
    active_monsters_override=None,
    per_hit_damage_bonus=None,
    per_hit_damage_includes_player_vulnerable=None,
    force_player_vulnerable=None,
):
    """Estimate HP loss from deterministic enemy damage this turn.

    This includes declared attacks and power-triggered damage such as an
    Exploder at countdown one. Unlike ``incoming_damage``, it applies current
    and end-turn block plus deterministic player-side reducers. Optional
    hypothetical Buffer and Intangible values let the planner compare
    defensive powers without mutating the authoritative state.
    """

    return _projected_attack_outcome(
        game,
        extra_block=extra_block,
        excluded_monsters=excluded_monsters,
        extra_buffer=extra_buffer,
        force_intangible=force_intangible,
        block_override=block_override,
        buffer_override=buffer_override,
        active_monsters_override=active_monsters_override,
        per_hit_damage_bonus=per_hit_damage_bonus,
        per_hit_damage_includes_player_vulnerable=(
            per_hit_damage_includes_player_vulnerable
        ),
        force_player_vulnerable=force_player_vulnerable,
    )[0]


def projected_buffer_remaining(game, extra_block=0, excluded_monsters=None):
    """Return existing Buffer stacks left after the declared attacks."""

    return _projected_attack_outcome(
        game,
        extra_block=extra_block,
        excluded_monsters=excluded_monsters,
    )[1]


def _end_orb_block(game):
    """Block created in the relic/orb END phase before hand effects."""

    player = getattr(game, "player", None)
    if not can_gain_block(player):
        return 0
    serialized = list(getattr(player, "orbs", []) or [])
    occupied = [
        orb
        for orb in serialized
        if _token(getattr(orb, "orb_id", "")) not in {"", "empty"}
    ]
    block = sum(
        max(0, int(getattr(orb, "passive_amount", 0) or 0))
        for orb in occupied
        if _token(getattr(orb, "orb_id", "")) == "frost"
    )
    relic_ids = {
        _token(getattr(relic, "relic_id", ""))
        for relic in getattr(game, "relics", []) or []
    }
    if (
        occupied
        and "cables" in relic_ids
        and _token(getattr(occupied[0], "orb_id", "")) == "frost"
    ):
        block += max(
            0, int(getattr(occupied[0], "passive_amount", 0) or 0)
        )
    slot_count = max(3, len(serialized), len(occupied))
    if "frozen core" in relic_ids or "frozencore" in relic_ids:
        if len(occupied) < slot_count:
            focus = signed_power_amount(player, "Focus")
            block += max(0, 2 + focus)
    return block


def _projected_pre_hand_block(
    game,
    *,
    current_block_override=None,
    extra_block=0,
    orb_block_override=None,
):
    player = getattr(game, "player", None)
    if player is None:
        return 0
    block = (
        max(0, int(current_block_override or 0))
        if current_block_override is not None
        else max(0, int(getattr(player, "block", 0) or 0))
    )
    if not can_gain_block(player):
        return block
    block += max(0, int(extra_block or 0))
    relic_ids = {
        _token(getattr(relic, "relic_id", ""))
        for relic in getattr(game, "relics", []) or []
    }
    if block == 0 and "orichalcum" in relic_ids:
        block = 6
    block += (
        _end_orb_block(game)
        if orb_block_override is None
        else max(0, int(orb_block_override or 0))
    )
    return block


def projected_end_block(
    game,
    *,
    current_block_override=None,
    extra_block=0,
    passive_block_override=None,
):
    player = getattr(game, "player", None)
    if player is None:
        return 0
    # Metallicize/Plated Armor use atEndOfTurnPreEndTurnCards, as do the relic
    # and orb phases represented here. Their block is therefore available to
    # Burn/Decay and other later hand effects.
    block = _projected_pre_hand_block(
        game,
        current_block_override=current_block_override,
        extra_block=extra_block,
        orb_block_override=(
            passive_block_override
            if passive_block_override is not None
            else None
        ),
    )
    if passive_block_override is None and can_gain_block(player):
        block += power_amount(player, "Metallicize")
        block += power_amount(player, "Plated Armor", "PlatedArmor")
    # Block remains intact through the enemy turn.  Calipers changes the
    # ordinary Block reset at the start of the *next* player turn, so its
    # 15-point retention haircut belongs only in ``calipers_retained_block``
    # and must not reduce mitigation here.
    return block


def projected_player_block(game):
    return projected_end_block(game)


def _player_end_turn_damage_events(
    game,
    extra_combust_hp_loss=0,
    *,
    hand_override=None,
    hand_size_override=None,
):
    player = getattr(game, "player", None)
    events = []
    hand = list(
        (getattr(game, "hand", []) or [])
        if hand_override is None
        else hand_override
    )
    hand_size = (
        len(hand)
        if hand_size_override is None
        else max(len(hand), max(0, int(hand_size_override or 0)))
    )
    for card in hand:
        card_id = _token(getattr(card, "card_id", ""))
        if card_id == "burn":
            events.append(
                PlayerDamageEvent(
                    "burn",
                    4 if int(getattr(card, "upgrades", 0) or 0) > 0 else 2,
                    True,
                )
            )
        elif card_id == "decay":
            events.append(PlayerDamageEvent("decay", 2, True))
        elif card_id == "regret":
            # Regret queues while the full hand is still intact. A simulated
            # turn may have played/exhausted known cards and may also have
            # drawn cards which are not part of the bounded candidate set.
            events.append(PlayerDamageEvent("regret", hand_size, False))
    pending_combust = max(0, int(extra_combust_hp_loss or 0))
    found_combust = False
    for power in getattr(player, "powers", []) or []:
        power_id = _token(getattr(power, "power_id", ""))
        power_name = _token(getattr(power, "power_name", ""))
        if power_id in {"constricted", "constrictedpower"} or power_name == "constricted":
            amount = max(0, int(getattr(power, "amount", 0) or 0))
            if amount > 0:
                events.append(
                    PlayerDamageEvent("constricted", amount, True)
                )
        elif power_id in {"combust", "combustpower"} or power_name == "combust":
            found_combust = True
            amount = max(0, int(getattr(power, "misc", 0) or 0)) or 1
            events.append(
                PlayerDamageEvent(
                    "combust", amount + pending_combust, False
                )
            )
            pending_combust = 0
    if pending_combust > 0 and not found_combust:
        events.append(
            PlayerDamageEvent("combust", pending_combust, False)
        )
    return events


def player_end_turn_hp_loss(game):
    """Conservative HP/damage estimate before monsters take actions."""

    outcome = resolve_player_damage_events(
        game,
        _player_end_turn_damage_events(game),
        block=projected_player_block(game),
        buffer_layers=power_amount(getattr(game, "player", None), "Buffer"),
    )
    return outcome.hp_loss


def _player_next_turn_start_damage_events(
    game, *, extra_brutality_amount=0
):
    """Return deterministic HP-loss events before the next player decision.

    Stacked Brutality resolves as one HP-loss event whose amount matches the
    power amount. Keeping this event after enemy attacks matters because a
    surviving Buffer can consume the whole event and Tungsten Rod reduces the
    stacked event only once.
    """

    player = getattr(game, "player", None)
    amount = (
        power_amount(player, "Brutality", "BrutalityPower")
        + max(0, int(extra_brutality_amount or 0))
    )
    if amount > 0:
        return (PlayerDamageEvent("brutality", amount, False),)
    return ()


def _player_next_turn_start_outcome(
    game, *, buffer_layers=0, extra_brutality_amount=0
):
    return resolve_player_damage_events(
        game,
        _player_next_turn_start_damage_events(
            game, extra_brutality_amount=extra_brutality_amount
        ),
        block=0,
        buffer_layers=max(0, int(buffer_layers or 0)),
    )


def player_next_turn_start_hp_loss(
    game, *, buffer_layers=None, extra_brutality_amount=0
):
    """HP loss at the next turn start, before another action is available."""

    if buffer_layers is None:
        buffer_layers = power_amount(getattr(game, "player", None), "Buffer")
    return _player_next_turn_start_outcome(
        game,
        buffer_layers=buffer_layers,
        extra_brutality_amount=extra_brutality_amount,
    ).hp_loss


def projected_end_turn_healing(game, *, player_hp_override=None, preceding_hp_loss=0):
    """Deterministic Regeneration healing before the enemy attack.

    Damage telemetry observes only the stable HP before and after END, so its
    comparable net prediction must include this heal. Magic Flower uses the
    game's half-up integer rounding (1 -> 2, 3 -> 5, 5 -> 8).
    """

    player = getattr(game, "player", None)
    if player is None:
        return 0
    regeneration = power_amount(player, "Regeneration", "RegenerationPower")
    if regeneration <= 0:
        return 0
    relic_ids = {
        _token(getattr(relic, "relic_id", ""))
        for relic in getattr(game, "relics", []) or []
    }
    if "magic flower" in relic_ids or "magicflower" in relic_ids:
        regeneration = (regeneration * 3 + 1) // 2
    current_hp = (
        max(0, int(player_hp_override or 0))
        if player_hp_override is not None
        else max(0, int(getattr(player, "current_hp", 0) or 0))
    )
    current_hp = max(0, current_hp - max(0, int(preceding_hp_loss or 0)))
    if current_hp <= 0:
        return 0
    max_hp = max(current_hp, int(getattr(player, "max_hp", current_hp) or current_hp))
    return min(max(0, max_hp - current_hp), regeneration)


def projected_turn_outcome(
    game,
    *,
    extra_block=0,
    excluded_monsters=None,
    block_override=None,
    buffer_override=None,
    passive_block_override=None,
    extra_end_turn_events=(),
    extra_combust_hp_loss=0,
    extra_brutality_amount=0,
    hand_override=None,
    hand_size_override=None,
    active_monsters_override=None,
    per_hit_damage_bonus=None,
    per_hit_damage_includes_player_vulnerable=None,
    force_intangible=False,
    force_player_vulnerable=None,
    combat_ends_before_next_turn=False,
    player_hp_override=None,
    attack_hp_loss_reduction=0,
    damage_packet_overrides=None,
    enemy_attack_healing=None,
    attack_hit_reaction=None,
    attacker_is_active=None,
    enemy_reaction_damage=None,
    enemy_pre_attack_block_gain=None,
    player_thorns_override=None,
    player_flame_barrier_override=None,
    combat_ended_after_attacks=None,
    player_health_observer=None,
):
    """Resolve deterministic loss through the next player decision boundary."""

    player = getattr(game, "player", None)
    initial_player_hp = (
        max(0, int(player_hp_override or 0))
        if player_hp_override is not None
        else max(0, int(getattr(player, "current_hp", 0) or 0))
    )
    fairy_healing = fairy_in_a_bottle_healing(game)
    health = None
    if fairy_healing > 0:
        health = {
            "hp": initial_player_hp,
            "max_hp": max(
                initial_player_hp,
                int(getattr(player, "max_hp", initial_player_hp) or 0),
            ),
            "available": True,
            "consumed": False,
            "revive_healing": 0,
            "dead": initial_player_hp <= 0,
        }

        def apply_health_damage(amount):
            amount = max(0, int(amount or 0))
            if amount <= 0 or health["dead"]:
                return 0
            applied = min(health["hp"], amount)
            health["hp"] -= applied
            if health["hp"] <= 0:
                if health["available"]:
                    health["available"] = False
                    health["consumed"] = True
                    healed = min(health["max_hp"], fairy_healing)
                    health["hp"] = healed
                    health["revive_healing"] += healed
                if health["hp"] <= 0:
                    health["dead"] = True
            return applied
    else:
        apply_health_damage = None
    if (
        active_monsters_override is None
        and _end_orbs_kill_all_before_player_events(game)
    ):
        # End-orb triggers precede Burn/Decay/Regret and player end-turn
        # powers. Killing the final enemy here ends combat immediately.
        return TurnDamageOutcome(
            end_turn_hp_loss=0,
            attack_hp_loss=0,
            next_turn_start_hp_loss=0,
            block=projected_end_block(
                game,
                current_block_override=block_override,
                extra_block=extra_block,
                passive_block_override=passive_block_override,
            ),
            buffer=(
                max(0, int(buffer_override or 0))
                if buffer_override is not None
                else power_amount(player, "Buffer")
            ),
            end_turn_hp_loss_events=(),
            next_turn_start_hp_loss_events=(),
            initial_player_hp=initial_player_hp,
            final_player_hp=initial_player_hp,
        )
    initial_block = projected_end_block(
        game,
        current_block_override=block_override,
        extra_block=extra_block,
        passive_block_override=passive_block_override,
    )
    initial_buffer = (
        max(0, int(buffer_override or 0))
        if buffer_override is not None
        else power_amount(player, "Buffer")
    )
    end_outcome = resolve_player_damage_events(
        game,
        tuple(
            _player_end_turn_damage_events(
                game,
                extra_combust_hp_loss=extra_combust_hp_loss,
                hand_override=hand_override,
                hand_size_override=hand_size_override,
            )
        )
        + tuple(
            event
            if isinstance(event, PlayerDamageEvent)
            else PlayerDamageEvent("planned_end_turn", event, False)
            for event in extra_end_turn_events
        ),
        block=initial_block,
        buffer_layers=initial_buffer,
        force_intangible=force_intangible,
    )
    resolved_end_turn_loss = end_outcome.hp_loss
    end_turn_healing = 0
    if health is not None:
        resolved_end_turn_loss = sum(
            apply_health_damage(event.amount)
            for event in end_outcome.hp_loss_events
        )
        if not health["dead"]:
            end_turn_healing = projected_end_turn_healing(
                game,
                player_hp_override=health["hp"],
                preceding_hp_loss=0,
            )
            health["hp"] = min(
                health["max_hp"], health["hp"] + end_turn_healing
            )
    else:
        end_turn_healing = projected_end_turn_healing(
            game, player_hp_override=initial_player_hp,
            preceding_hp_loss=end_outcome.hp_loss,
        )
    # Gross damage remains separate from the chronological HP ledger. Healing
    # before enemy attacks cannot revive a player killed by earlier hand damage
    # or be banked beyond max HP to offset a later attack.
    hp_before_attacks = max(0, initial_player_hp - end_outcome.hp_loss) + end_turn_healing
    reaction_player_hp = health["hp"] if health is not None else hp_before_attacks

    def publish_player_health():
        if callable(player_health_observer):
            player_health_observer(reaction_player_hp)

    def apply_attack_damage(amount):
        nonlocal reaction_player_hp
        if health is not None:
            applied = apply_health_damage(amount)
            reaction_player_hp = health["hp"]
        else:
            # Preserve gross overkill for scoring, but gate reactions using
            # actual surviving HP. Fairy uses the existing per-packet ledger.
            applied = max(0, int(amount or 0))
            reaction_player_hp = max(0, reaction_player_hp - applied)
        publish_player_health()
        return applied

    # End-hand damage, automatic revivals and regeneration all precede attacks.
    # Publish again after every hit, before any attached player reaction runs.
    publish_player_health()
    effective_excluded = list(excluded_monsters or [])
    effective_per_hit_bonus = dict(per_hit_damage_bonus or {})
    if active_monsters_override is None:
        living = living_monsters(game)
        effective_excluded.extend(passive_action_suppressed_monsters(game))
        for monster in living:
            if _token(getattr(monster, "monster_id", "")) != "transient":
                continue
            # Poison, Lightning, and Combust resolve before Transient's
            # current move and Shifting removes one Strength per HP lost.
            # move_adjusted_damage already includes Weak, so the Strength
            # delta must pass through the same 75% (or Paper Crane 60%)
            # multiplier. Subtracting raw Poison produced live predictions
            # of 2 and 0 where the authoritative losses were 8 and 3.
            # Noxious Fumes is deliberately excluded because its new
            # application occurs after the current attack.
            passive_damage = passive_action_damage_before_move(
                game, monster, len(living)
            )
            if passive_damage > 0:
                weak_multiplier = 1.0
                if power_amount(monster, "Weak", "Weakened") > 0:
                    ids = relic_ids(game)
                    weak_multiplier = (
                        0.6
                        if {
                            "paper crane", "paper krane",
                            "papercrane", "paperkrane",
                        } & ids
                        else 0.75
                    )
                effective_per_hit_bonus[id(monster)] = (
                    int(effective_per_hit_bonus.get(id(monster), 0) or 0)
                    - int(passive_damage * weak_multiplier)
                )
    attack_loss, final_buffer, final_block = _projected_attack_outcome(
        game,
        excluded_monsters=effective_excluded,
        block_override=end_outcome.block,
        buffer_override=end_outcome.buffer,
        active_monsters_override=active_monsters_override,
        per_hit_damage_bonus=effective_per_hit_bonus,
        per_hit_damage_includes_player_vulnerable=(
            per_hit_damage_includes_player_vulnerable
        ),
        force_intangible=force_intangible,
        force_player_vulnerable=force_player_vulnerable,
        damage_packet_overrides=damage_packet_overrides,
        attack_hp_loss_reduction=attack_hp_loss_reduction,
        enemy_attack_healing=enemy_attack_healing,
        attack_hit_reaction=attack_hit_reaction,
        attacker_is_active=attacker_is_active,
        enemy_reaction_damage=enemy_reaction_damage,
        enemy_pre_attack_block_gain=enemy_pre_attack_block_gain,
        player_thorns_override=player_thorns_override,
        player_flame_barrier_override=player_flame_barrier_override,
        player_damage=apply_attack_damage,
    )
    hp_after_attacks = max(0, hp_before_attacks - attack_loss)
    next_turn_start_hp_loss = 0
    if (
        not combat_ends_before_next_turn
        and not (
            callable(combat_ended_after_attacks)
            and combat_ended_after_attacks()
        )
        and (
            not health["dead"]
            if health is not None
            else hp_after_attacks > 0
        )
    ):
        start_outcome = _player_next_turn_start_outcome(
            game,
            buffer_layers=final_buffer,
            extra_brutality_amount=extra_brutality_amount,
        )
        next_turn_start_hp_loss = start_outcome.hp_loss
        if health is not None:
            next_turn_start_hp_loss = sum(
                apply_health_damage(event.amount)
                for event in start_outcome.hp_loss_events
            )
        final_buffer = start_outcome.buffer
    return TurnDamageOutcome(
        end_turn_hp_loss=resolved_end_turn_loss,
        attack_hp_loss=attack_loss,
        next_turn_start_hp_loss=next_turn_start_hp_loss,
        block=final_block,
        buffer=final_buffer,
        end_turn_hp_loss_events=end_outcome.hp_loss_events,
        next_turn_start_hp_loss_events=(
            start_outcome.hp_loss_events
            if next_turn_start_hp_loss > 0
            else ()
        ),
        fairy_revive_consumed=bool(
            health is not None and health["consumed"]
        ),
        fairy_revive_healing=(
            int(health["revive_healing"])
            if health is not None else 0
        ),
        initial_player_hp=initial_player_hp,
        final_player_hp=(
            int(health["hp"]) if health is not None
            else max(0, hp_after_attacks - next_turn_start_hp_loss)
        ),
        end_turn_healing=end_turn_healing,
    )


def projected_turn_hp_loss(game, extra_block=0):
    """Estimate all HP loss that will occur if the player ends the turn now.

    Enemy attacks and player-side end-turn effects are kept as separate
    helpers because only the attack portion can normally be reduced by more
    block.  Callers that decide whether the turn is lethal must use this
    combined value instead of looking at enemy intent alone.
    """

    outcome = projected_turn_outcome(game, extra_block=extra_block)
    return outcome.total_hp_loss


def safe_to_wait_for_passive_kills(game):
    living = living_monsters(game)
    if not living:
        return False
    if _end_orbs_kill_all_before_player_events(game, living):
        return True
    if active_monsters(game):
        return False
    # A target can be doomed by a later Noxious/Poison tick while its current
    # displayed move still resolves.  Waiting is only safe when every living
    # target is already in the immediate-action-suppressed set.
    if attack_monsters(game):
        return False
    player = getattr(game, "player", None)
    if player is None:
        return False

    # Hand statuses and player powers (including Constricted/Combust) resolve
    # before the enemy-side poison tick. A passively doomed final enemy is
    # therefore safe to wait for only when those earlier events are nonlethal.
    return player_end_turn_hp_loss(game) < int(
        getattr(player, "current_hp", 0) or 0
    )


def _end_orbs_kill_all_before_player_events(game, living=None):
    """Whether deterministic end-orb triggers end combat before hand effects."""

    living = list(living if living is not None else living_monsters(game))
    if len(living) != 1:
        return False
    target = living[0]
    if has_unresolved_damage_cap(target):
        return False
    orbs = [
        orb
        for orb in getattr(getattr(game, "player", None), "orbs", []) or []
        if _token(getattr(orb, "orb_id", "")) not in {"", "empty"}
    ]
    amounts = [
        max(0, int(getattr(orb, "passive_amount", 0) or 0))
        for orb in orbs
        if _token(getattr(orb, "orb_id", "")) == "lightning"
    ]
    relic_ids = {
        _token(getattr(relic, "relic_id", ""))
        for relic in getattr(game, "relics", []) or []
    }
    if (
        orbs
        and "cables" in relic_ids
        and _token(getattr(orbs[0], "orb_id", "")) == "lightning"
    ):
        amounts.append(
            max(0, int(getattr(orbs[0], "passive_amount", 0) or 0))
        )
    hp = max(0, int(getattr(target, "current_hp", 0) or 0))
    block = max(0, int(getattr(target, "block", 0) or 0))
    for amount in amounts:
        amount = orb_damage_after_lock_on(target, amount)
        if is_intangible(target) and amount > 0:
            amount = 1
        absorbed = min(block, amount)
        block -= absorbed
        hp -= max(0, amount - absorbed)
        if hp <= 0:
            return True
    return False


def choose_attack_target(game, candidates=None):
    candidates = list(candidates if candidates is not None else active_monsters(game))
    if not candidates:
        candidates = living_monsters(game)
    if not candidates:
        return None

    # Prefer enemies whose threat is large relative to the damage still
    # required to remove them.  Stable tuple components keep the result
    # deterministic when two enemies are equivalent.
    def score(monster):
        effective_hp = max(1, int(getattr(monster, "current_hp", 0) or 0) + int(getattr(monster, "block", 0) or 0))
        threat = monster_threat(monster)
        return (threat / effective_hp, threat, -effective_hp, -int(getattr(monster, "monster_index", 0) or 0))

    return max(candidates, key=score)
