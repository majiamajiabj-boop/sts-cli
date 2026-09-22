import copy
import math
from dataclasses import dataclass

from spirecomm.ai import combat_predictor
from spirecomm.communication.action import EndTurnAction, PlayCardAction
from spirecomm.spire.card import CardType
from spirecomm.spire.character import Intent


def _token(value):
    return "".join(character for character in str(value or "").lower() if character.isalnum())


_RANDOM_MULTI_TARGET_ATTACKS = frozenset({"ripandtear"})


def _is_random_multi_target_attack(game, card, target=None):
    """Return whether an untargeted Attack has hidden multi-enemy targets.

    Rip and Tear has two random hit packets, not one packet per living enemy.
    Keep this predicate narrow: other untargeted attacks in the planner are
    deterministic AOE or have their own explicit handling.
    """

    if target is not None or getattr(card, "type", None) != CardType.ATTACK:
        return False
    if _token(getattr(card, "card_id", "")) not in _RANDOM_MULTI_TARGET_ATTACKS:
        return False
    return len(combat_predictor.active_monsters(game)) > 1


def _monster_key(monster):
    return (_token(getattr(monster, "monster_id", "")), int(getattr(monster, "monster_index", 0) or 0))


def _candidate_attack_progress(game, candidate):
    """Return ``(raw_damage, hp_damage, block_progress)`` for an Attack.

    ``_Candidate.damage`` is deliberately the damage that penetrates current
    enemy Block.  That is correct for HP-loss accounting, but it is not a
    progress metric for Barricade/Plated Armor or for a last Velvet Choker
    slot: an attack can remove six points of persistent durability while
    reporting zero HP damage.  Keep this helper narrow and side-effect free;
    callers still apply their normal hazard and energy gates.
    """

    if candidate is None or getattr(candidate.card, "type", None) != CardType.ATTACK:
        return 0, 0, 0
    target = getattr(candidate, "target", None)
    if target is None:
        # AOE/random cards have no single target binding.  If exactly one
        # active enemy remains, use that authoritative enemy to recover the
        # raw packet and current Block; otherwise keep raw progress unknown.
        active = combat_predictor.active_monsters(game)
        if len(active) != 1:
            return 0, max(0, int(getattr(candidate, "damage", 0) or 0)), 0
        target = active[0]
    try:
        raw_damage, _hits = combat_predictor.card_attack_profile(
            game, candidate.card, target=target
        )
    except (AttributeError, TypeError, ValueError):
        raw_damage = getattr(candidate.card, "damage", 0)
    raw_damage = max(0, int(raw_damage or 0))
    hp_damage = max(0, int(getattr(candidate, "damage", 0) or 0))
    target_block = max(0, int(getattr(target, "block", 0) or 0))
    return raw_damage, hp_damage, max(0, min(raw_damage, target_block))


def _candidate_has_authoritative_target(game, candidate):
    """Whether a candidate can be issued without a card target binding.

    Area/random attacks are serialized with ``has_target=False``.  With one
    active enemy left, issuing that card without an enemy id is still
    deterministic; with multiple enemies it would claim progress for an
    unbound random target and must remain excluded from this fallback.
    """

    if getattr(candidate, "target", None) is not None:
        return True
    if getattr(getattr(candidate, "card", None), "type", None) != CardType.ATTACK:
        return False
    return len(combat_predictor.active_monsters(game)) == 1


@dataclass
class _Candidate:
    card: object
    target: object
    base_score: float
    mitigation: int
    intrinsic_mitigation: int
    damage: int
    kills: bool
    order_score: float
    block_gain: int = 0
    orichalcum_block_gain: int = 0
    orichalcum_single_mitigation: int = 0
    end_turn_relief: int = 0
    self_hp_cost: int = 0
    reactive_hp_cost: int = 0
    post_reactive_block: int = 0
    self_damage_events: tuple = ()
    reactive_damage_events: tuple = ()
    thorns_damage_events: tuple = ()
    sharp_hide_damage_events: tuple = ()
    buffer_gain: int = 0
    buffer_mitigation: int = 0
    state_block_mitigation: int = 0
    intangible_mitigation: int = 0
    end_turn_damage_events: tuple = ()
    end_turn_aoe_damage: int = 0
    extra_combust_hp_loss: int = 0
    lifecycle_kind: str = ""
    expected_remaining_turns: int = 0
    expected_trigger_count: int = 0
    expected_lifecycle_benefit: float = 0.0
    expected_lifecycle_cost: float = 0.0
    lifecycle_adjustment: float = 0.0
    intangible_turns: int = 0
    post_intangible_turns: int = 0
    gambler_alternative: bool = False
    hand_additions: int = 0
    # Runic Cube draws once for each HP-loss event.  Keep the count separate
    # from ordinary card draw so a self-damage card is not treated as if its
    # draw were guaranteed without the relic.
    runic_cube_draws: int = 0
    # Self-Forming Clay grants Block next turn once per real HP-loss event.
    # Keep that delayed value separate from current mitigation and Block.
    self_forming_clay_hp_loss_events: int = 0
    self_forming_clay_future_block: int = 0
    self_forming_clay_credit: float = 0.0
    healing_gain: int = 0
    # Positive value which is not merely this card's immediate Block.  The
    # ordered planner uses this to distinguish a genuinely useful defensive
    # card (for example a trigger/draw/heal setup) from a second pure block
    # after the authoritative frame already covers the current attack.
    non_block_utility: float = 0.0
    future_turn_weak_value: float = 0.0
    # Persistent block powers can protect several known future attack turns
    # even when the enemy is currently debuffing or otherwise not attacking.
    future_turn_block_value: float = 0.0
    # Energy effects which resolve at the start of the following turn (for
    # example Outmaneuver or Conserve Battery).  They must not be added to
    # the current branch energy, but they are still real setup value when the
    # current turn is already covered.
    future_turn_energy_value: float = 0.0
    champ_transition: bool = False
    champ_transition_ready: bool = True
    champ_transition_penalized: bool = False
    slime_transition: bool = False
    slime_transition_penalized: bool = False
    # Damage-based mitigation (a kill or Mode Shift) is recomputed from the
    # simulated monster state.  Keeping the remainder separate prevents the
    # whole-turn search from counting the same killed attacker twice.
    stateful_mitigation: int = 0


@dataclass(frozen=True)
class _ProjectedOrb:
    orb_id: str
    passive_amount: int
    evoke_amount: int


@dataclass(frozen=True)
class _TurnState:
    """One bounded, deterministic branch of the current player turn."""

    used: frozenset
    energy: int
    # Cards added by this speculative branch only. Time Warp, Slow and Ink
    # Bottle already serialize progress from authoritative card plays, so
    # seeding this with the controller's confirmed total double-counts every
    # earlier play after the first refreshed frame.
    cards_played: int
    # Confirmed cards already played this authoritative turn plus cards in
    # this branch. Time Warp exposes its own counter; Normality does not, so
    # the planner maintains this separate confirmed-progress value.
    normality_cards_played: int
    score: float
    plan: tuple
    hp: tuple
    block: tuple
    poison: tuple
    artifact: tuple
    vulnerable: tuple
    # Enemy Weak must be branch-local for conditional cards such as Heel
    # Hook.  A Neutralize selected earlier in this searched sequence is not
    # present in the authoritative frame yet, and Artifact can consume it.
    weak: tuple
    # Lock-On is target-local and applies only to Lightning/Dark packets.
    # Duration still matters for same-turn evocations/passives, while the
    # authoritative card damage must remain untouched.
    lock_on: tuple
    corpse_explosion: tuple
    damage_value: tuple
    poison_value: tuple
    # Enemy-local effects created inside the candidate sequence.  Choke is
    # especially important for phase transitions: damage from later card-play
    # triggers can be the packet which pushes Slime Boss below half health.
    choke: tuple = ()
    # Guardian's Mode Shift counter is reduced by cumulative, per-hit damage.
    # Keeping the remaining counter in the branch lets two modest attacks (or
    # an early hit of a multi-hit card) enter Defensive Mode correctly.
    mode_shift_remaining: tuple = ()
    # Per-hit reactive defenses must live in the branch because one hit can
    # change the damage or block observed by the next hit/card.
    curl_up_block: tuple = ()
    malleable_next_block: tuple = ()
    flight_stacks: tuple = ()
    orbs: tuple = ()
    orb_slots: int = 3
    player_artifact: int = 0
    player_buffer: int = 0
    player_block: int = 0
    # Panic Button's No Block power is created inside the searched branch;
    # later duplicated resolutions and hand cards must not fabricate Block.
    player_no_block: bool = False
    player_intangible: bool = False
    player_intangible_turns: int = 0
    # A same-turn power such as Berserk can make the player Vulnerable after
    # the authoritative frame was serialized. Keep that transition in the
    # branch so the later enemy attack is not scored with stale mitigation.
    player_vulnerable: bool = False
    player_hp: int = 1
    # Rage can be played earlier in the candidate sequence.  Reading only
    # the authoritative player powers misses that same-turn transition and
    # makes every later Attack look as if it grants no block.
    player_rage: int = 0
    # These powers can all change the value, cost, or exhaust result of a
    # later card in the same candidate sequence.  Keep their branch-local
    # values beside Rage instead of repeatedly consulting the authoritative
    # frame, which does not yet contain a setup card selected by this search.
    player_after_image: int = 0
    # Damage/value modifiers which become active for cards played later in
    # this exact branch.  Serialized hand damage only reflects powers present
    # in the authoritative frame, so a newly played Accuracy or A Thousand
    # Cuts must live in the branch just like After Image.
    player_accuracy: int = 0
    player_thousand_cuts: int = 0
    player_sadistic_nature: int = 0
    player_feel_no_pain: int = 0
    player_dark_embrace: int = 0
    player_corruption: bool = False
    # Bullet Time changes every card already in hand to zero cost and blocks
    # all later draw for the turn. The authoritative frame does not expose
    # that transition until after the card resolves, so both effects must be
    # branch-local for ordered search.
    player_bullet_time: bool = False
    # Heat Sinks triggers from layers which existed before the current Power
    # card began resolving.  Keeping it branch-local lets a newly played copy
    # affect later Powers without incorrectly drawing for itself.
    player_heatsinks: int = 0
    # Storm follows the same existing-layer rule: only stacks which were
    # active before a Power card was played channel Lightning for that card.
    # A newly played Storm therefore enables later Powers without triggering
    # itself.  This must be branch-local so Storm -> Power ordering is visible
    # to the bounded whole-turn search.
    player_storm: int = 0
    # Double Tap is consumed by the next Attack, not by the next card. Keep
    # the remaining duplicated attacks in branch state so only the first
    # qualifying attack receives the extra full resolution.
    player_double_tap: int = 0
    # Burst follows the same pattern for Skills. The amount counts how many
    # upcoming Skill cards receive one extra full resolution; playing Burst
    # while a prior Burst is active can itself be duplicated.
    player_burst: int = 0
    # Duplication Potion applies to the next card, irrespective of whether it
    # is an Attack, Skill, or Power.  The base game queues one purge-on-use
    # copy for each manually played card and consumes one power layer; queued
    # copies cannot consume another layer.  Keep that remaining-card budget
    # branch-local so stacked potion uses duplicate successive cards instead
    # of multiplying the first card's resolution count.
    player_duplication: int = 0
    # Every successful Attack useCard resolution, including Echo/Double Tap
    # autoplays.  Attack-trigger relic counters advance per resolution rather
    # than per card selected from hand.
    attack_resolutions_played: int = 0
    # Successful useCard resolutions contributed by the first hand action in
    # this branch.  Persist this conservative count across the authoritative
    # refresh so Normality does not lose autoplayed copies.
    first_action_resolution_count: int = 0
    # Pen Nib's serialized damage bonus belongs only to the first resolution
    # of the next Attack. Double Tap/Echo Form repeat the card after that
    # power has been consumed, and later hand cards must use normal damage.
    player_pen_nib: bool = False
    # Akabeko applies its bonus to the first Attack resolution only.  Keep it
    # branch-local because the authoritative card damage does not include
    # this relic-side packet on every bridge version.
    player_akabeko_ready: bool = False
    # Kunai grants Dexterity after its triggering Attack; later Block gains in
    # the same ordered branch must see that delta even though the frame has
    # not been refreshed yet.
    player_dexterity_bonus: int = 0
    # Focus gained by Defragment/Biased Cognition changes existing and newly
    # channelled orbs immediately. The authoritative orb amounts only contain
    # Focus from the start of this frame, so the delta belongs here.
    player_focus_bonus: int = 0
    # Electrodynamics changes every Lightning passive/evoke into a hit on all
    # living enemies.  A copy played inside this searched turn is not visible
    # in the authoritative player powers yet, so keep the toggle branch-local.
    player_electrodynamics: bool = False
    # Enemy-turn reactive powers selected earlier in this same searched turn
    # are not present in the authoritative frame yet.  Keep their exact
    # stacks so Static Discharge, Caltrops and Flame Barrier affect the attack
    # phase reached by this branch.
    player_static_discharge: int = 0
    # Bronze Scales and Caltrops share one stacked ThornsPower packet.
    # Flame Barrier is a distinct power and must remain a second packet so
    # enemy Block, Intangible, and transitions resolve between reactions.
    player_thorns: int = 0
    player_flame_barrier: int = 0
    # Rupture gains its amount once per actual self-owned HP-loss packet, not
    # per point lost.  Hex inserts its amount of Dazed at random positions in
    # the draw pile whenever a non-Attack is played.
    player_rupture: int = 0
    player_hex: int = 0
    rupture_triggers: int = 0
    rupture_strength_gained: int = 0
    # Extra Strength gained by cards inside this simulated turn.  Serialized
    # card.damage already contains the authoritative Strength present at the
    # start of the frame; this delta is only for setup cards played earlier in
    # the candidate sequence (Flex, Inflame, Spot Weakness, and Limit Break).
    player_strength_bonus: int = 0
    # Flex is useful only if a later attack consumes the same-turn Strength,
    # Artifact preserves it, or Limit Break converts part of it to permanent
    # Strength.  Track the still-unconsumed temporary portion so a zero-cost
    # setup card cannot earn generic score immediately before END.
    unconsumed_temporary_strength: int = 0
    # Strength gained by monsters while exploring this exact card order.
    # Curiosity is the important base-game case: every Power played while
    # Awakened One is alive increases each later attack hit.  Keeping the
    # delta per monster makes an attack-before-Power line observably different
    # from Power-before-attack and lets the terminal own the real ordering.
    enemy_strength_bonus: tuple = ()
    # Strength reductions applied inside this searched turn are target-local
    # and modify every hit before Block/on-hit powers. This covers permanent
    # reductions (Disarm/Malaise) and current-turn reductions (Dark
    # Shackles/Piercing Wail).
    enemy_strength_reduction: tuple = ()
    remaining_hand_indexes: tuple = ()
    hand_size: int = 0
    # CommunicationMod exposes the exact draw-pile size.  Track deterministic
    # draws inside the branch so Aggregate cannot repeatedly price the
    # turn-start pile.  ``-1`` means a reshuffle made the exact size unknown;
    # resource calculations then fail closed.
    draw_pile_size: int = 0
    # Sundial depends on the number of real shuffle events, not merely on
    # whether the next draw order is known.  Keep exact pile counts on a
    # separate channel because ``draw_pile_size=-1`` intentionally means the
    # concrete post-shuffle order is unknown to Aggregate and terminal binds.
    sundial_draw_pile_size: int = 0
    sundial_discard_pile_size: int = 0
    sundial_counter: int = 0
    sundial_shuffle_count: int = 0
    sundial_energy_gained: int = 0
    # Void loses one Energy when it is drawn, including cards drawn by an
    # autoplayed Echo Form copy.  Keep both the concrete count and the paid
    # debt in branch state so a later card cannot spend energy which the
    # authoritative draw sequence already removed.
    drawn_void_count: int = 0
    draw_energy_loss: int = 0
    # Hex randomizes otherwise-known draw-pile order.  Existing concrete hand
    # cards remain searchable, but terminal binding must re-plan from the
    # authoritative frame and no later Aggregate/deterministic draw may claim
    # the old ordering.
    future_draw_pile_unknown: bool = False
    generated_dazed: int = 0
    # Turbo's discard insertion is per resolution, including copies.
    generated_voids: int = 0
    # Sneaky Strike is conditional on a discard which already resolved this
    # turn, not merely on owning a discard card.  The authoritative bridge
    # supplies the initial value; deterministic branch transitions advance it.
    discarded_this_turn: bool = False
    # Hovering Kite grants one Energy on the first discard of the turn.  The
    # authoritative discard counter proves whether that trigger was already
    # spent; speculative branches retain the exact one-shot gain separately
    # so a discard card can fund a later card in the same searched line.
    hovering_kite_energy_gained: int = 0
    # Delayed energy is deliberately distinct from current energy.  This is
    # both an audit field and a state invariant for Flying Knee/Outmaneuver/
    # Conserve Battery.
    next_turn_energy: int = 0
    # Battle Trance and equivalent effects suppress every later draw this
    # turn.  Keeping this in the branch makes draw-card order observable
    # instead of awarding the same fixed utility when the draw is played at
    # zero energy or after No Draw is already active.
    no_draw: bool = False
    neutralized: frozenset = frozenset()
    mitigation: int = 0
    end_turn_relief: int = 0
    self_hp_cost: int = 0
    # HP-loss effects intentionally paid by selected cards (including
    # Pain/Blue Candle), excluding enemy reactions such as Beat of Death,
    # Thorns, and Sharp Hide.
    voluntary_self_hp_cost: int = 0
    reactive_hp_cost: int = 0
    self_forming_clay_hp_loss_events: int = 0
    raw_orichalcum_block: int = 0
    generated_block: int = 0
    end_turn_damage_events: tuple = ()
    end_turn_aoe_damage: int = 0
    extra_combust_hp_loss: int = 0
    # Brutality powers selected inside this branch are not present in the
    # authoritative frame yet.  Keep their added stack count so the next-turn
    # start event is simulated as one combined event with existing stacks.
    extra_brutality_amount: int = 0
    # Discounted multi-turn downside left after the candidate's strategic
    # benefit.  This is not current HP loss, but it can prevent a locally safe
    # Wraith Form line from receiving a categorical safety tier over a much
    # healthier long-horizon setup line.
    lifecycle_liability: float = 0.0
    # Some cards forcibly exhaust other cards still in hand.  Preserve the
    # independently evaluated future value of any lifecycle card they consume
    # so that immediate Block/damage does not receive that strategic loss for
    # free.  Terminal scoring waives this cost when the branch ends combat.
    forced_exhaust_lifecycle_cost: float = 0.0
    forced_exhaust_lifecycle_cards: tuple = ()
    # All-Out Attack discards an unknown concrete card.  The authoritative
    # next frame will reveal which one; until then no named follow-up card is
    # guaranteed to remain in hand, so the current branch must not expand it.
    random_hand_unknown: bool = False
    # Writhing Mass rerolls its intent after positive nonlethal Attack damage.
    # The old serialized intent is invalid from that point onward.  The beam
    # may still value intent-independent follow-ups, but terminal binding must
    # stop at the first affected action and the risk model uses only a bounded
    # encounter envelope until the authoritative frame supplies the new move.
    writhing_mass_intent_unknown: frozenset = frozenset()
    # Observable net enemy HP reduction after the branch's first action.
    # This is an audit field, not a score input: unlike Candidate.damage it
    # includes target HP caps, Flight, block reactions and secondary packets.
    first_action_enemy_hp_loss: int = -1
    # Random multi-target Lightning cannot be assigned to one concrete enemy
    # during search.  Keep its expected aggregate HP loss separately so the
    # first-action guard can still value real damage without pretending that a
    # particular target or kill was guaranteed.
    first_action_expected_enemy_hp_loss: int = -1
    forced_end: bool = False


class FastCombatPlanner:
    """Small, deterministic turn planner.

    The game is still authoritative after every action.  This planner only
    chooses the next action, but scores the affordable remainder of the hand
    so that a locally attractive card does not consume energy needed by a
    better combination.  Its state space is bounded by hand size, energy and
    currently useful mitigation, which keeps it comfortably below the game's
    animation/state round-trip time.
    """

    DRAW_COUNTS = {
        "acrobatics": 3,
        "adrenaline": 2,
        "backflip": 2,
        "battletrance": 3,
        "burningpact": 2,
        "compiledriver": 1,
        "coolheaded": 1,
        "daggerthrow": 1,
        "deepbreath": 1,
        "escapeplan": 1,
        "expertise": 2,
        "finesse": 1,
        "flashofsteel": 1,
        "masterofstrategy": 3,
        "offering": 3,
        "overclock": 2,
        "pommelstrike": 1,
        "prepared": 1,
        "quickslash": 1,
        "reboot": 4,
        "shrugitoff": 1,
        "skim": 3,
        "steampower": 2,
        "sweepingbeam": 1,
        "warcry": 1,
    }

    ENERGY_VALUES = {
        "adrenaline": 1,
        "bloodletting": 2,
        "concentrate": 2,
        "fission": 2,
        "offering": 2,
        "seeingred": 2,
        "tactician": 1,
        "turbo": 2,
    }

    INTRINSIC_SELF_DAMAGE = {
        "bloodletting": 3,
        "hemokinesis": 2,
        "jax": 3,
        "offering": 6,
    }

    NEXT_TURN_ENERGY_VALUES = {
        "conservebattery": 1,
        "flyingknee": 1,
        "outmaneuver": 2,
    }

    UPGRADED_DRAW_BONUSES = {
        "acrobatics": 1,
        "battletrance": 1,
        "burningpact": 1,
        "coolheaded": 1,
        "masterofstrategy": 1,
        "offering": 2,
        "overclock": 1,
        "pommelstrike": 1,
        "prepared": 1,
        "skim": 1,
    }

    WEAK_CARDS = {
        "clothesline",
        "cripplingpoison",
        "gofortheeyes",
        "legsweep",
        "neutralize",
        "shockwave",
        "suckerpunch",
        "uppercut",
        "waveofthehand",
    }

    AOE_WEAK_CARDS = {"cripplingpoison", "shockwave"}

    STRENGTH_DOWN_CARDS = {"darkshackles", "disarm", "piercingwail"}

    INTANGIBLE_CARDS = {"apparition", "ghostly", "wraithform", "wraithformv2"}

    VULNERABLE_CARDS = {
        "bash", "beamcell", "shockwave", "terror", "thunderclap", "trip", "uppercut",
    }

    # These cards have no normal tactical output, but when the authoritative
    # state says they are playable they can be removed from the combat deck.
    # Cleaning them with otherwise unused energy improves later draw quality.
    DEAD_STATUS_CARDS = {"burn", "dazed", "slimed", "void", "wound"}

    # Pure defensive Skills whose printed effect cannot consume or mutate a
    # second card.  With Corruption and Dark Embrace active, playing one for
    # zero energy is also a guaranteed exhaust/draw operation.  Keeping this
    # list narrow prevents the fallback below from treating True Grit, Second
    # Wind, or other hand-mutating Skills as harmless draw-cycle actions.
    SAFE_CORRUPTED_BLOCK_CYCLE_CARDS = {
        "autoshields",
        "backflip",
        "blur",
        "chargedbattery",
        "defendb",
        "defendg",
        "defendr",
        "deflect",
        "dodgeandroll",
        "escapeplan",
        "finesse",
        "goodinstincts",
        "leap",
        "shrugitoff",
        "steambarrier",
    }

    # These are the narrow exceptions to the normal "do not spend cards on
    # an enemy already guaranteed to die" rule.  Corpse Explosion can turn a
    # reserved death into immediate area damage; the attacks grant permanent
    # rewards only when they deliver the killing blow themselves.
    ON_KILL_BENEFIT_CARDS = {
        "feed",
        "handofgreed",
        "lessonlearned",
        "ritualdagger",
    }
    ON_KILL_BONUS = {
        "feed": 18,
        "handofgreed": 14,
        "lessonlearned": 15,
        "ritualdagger": 12,
    }
    NONFATAL_EXHAUST_PENALTY = {
        # Feed is normally worth preserving for its permanent Fatal reward.
        # A small penalty still allowed Feed -> ordinary lethal, exhausting
        # the card without raising max HP.  Survival tiers can override this
        # value when nonfatal damage is genuinely necessary, but an otherwise
        # equivalent lethal line must keep Feed for the killing blow.
        "feed": 24,
        "lessonlearned": 8,
        "ritualdagger": 7,
    }

    DOOMED_TARGET_BENEFIT_CARDS = {
        "bite",
        "corpseexplosion",
        *ON_KILL_BENEFIT_CARDS,
    }

    DOOMED_RESOURCE_CARDS = {
        "alchemize",
        "bandageup",
        "geneticalgorithm",
        "reaper",
        "selfrepair",
        *DOOMED_TARGET_BENEFIT_CARDS,
    }

    POISON_CARDS = {
        "bouncingflask",
        "cripplingpoison",
        "deadlypoison",
        "noxiousfumes",
        "poisonedstab",
        "corpseexplosion",
    }

    SCALING_ENEMY_BONUS = {
        "awakenedone": 7,
        "bookofstabbing": 7,
        "chosen": 9,
        "cultist": 10,
        "dagger": 12,
        "fatgremlin": 7,
        "gremlinleader": 8,
        "gremlinwizard": 10,
        "gianthead": 18,
        "hexaghost": 9,
        # CommunicationMod serializes the Act 2 Mystic as ``Healer`` in the
        # Centurion encounter.  Keep both identifiers bound to the same
        # support pressure; otherwise every heal/buff turn is priced as if
        # leaving the support enemy alive were free.
        "healer": 9,
        "madgremlin": 7,
        "mystic": 9,
        "orbwalker": 11,
        "reptomancer": 11,
        "shieldgremlin": 8,
        "shelledparasite": 5,
        "sneakygremlin": 8,
        "snakeplant": 5,
        "spiker": 4,
        "spirespear": 13,
        "spireshield": 8,
        "timeeater": 7,
        "writhingmass": 5,
    }

    # Recurring effects which permanently erode the player's output cannot
    # be represented by the enemy's current intent alone.  Lagavulin, for
    # example, shows two ATTACK intents between Siphon Soul turns; a planner
    # which only prices the displayed move repeatedly prefers two Defends and
    # a weak hit until Strength and Dexterity have both collapsed.
    #
    # Store mechanics here, not strategy points.  ``_persistent_debuff_pressure``
    # converts the losses into the current deck's expected lost Attack hits
    # and Block applications, so the same encounter is urgent for all player
    # classes without assuming that one point of Strength/Dexterity has a
    # fixed value in every deck.
    PERSISTENT_DEBUFF_PROFILES = {
        "lagavulin": {
            "cycle_turns": 3,
            "attack_turns_per_cycle": 2,
            "strength_loss": 1,
            "dexterity_loss": 1,
        },
    }

    # Conservative A0 attack packets reachable after a non-attack turn. These
    # are mechanics, not arbitrary strategy points: every Nemesis attack is
    # at least the 6x3 multi-hit packet, while its 45-damage Scythe is worse.
    # Keep only two guaranteed upcoming attack opportunities so random move
    # ordering cannot overstate the duration of Weak or persistent Block.
    FUTURE_ATTACK_PROFILES = {
        "nemesis": ((6, 3), (6, 3)),
    }

    POWER_VALUES = {
        "afterimage": 10,
        "barricade": 8,
        "biasedcognition": 13,
        "capacitor": 7,
        "combust": 7,
        "corruption": 14,
        "darkembrace": 10,
        "defragment": 12,
        "demonform": 10,
        "echoform": 15,
        "electrodynamics": 15,
        "feelnopain": 10,
        "footwork": 12,
        "inflame": 8,
        "loop": 8,
        "noxiousfumes": 10,
        "toolsofthetrade": 7,
        "welllaidplans": 8,
        "wraithform": 13,
        "wraithformv2": 18,
    }

    # The Champ cleanses debuffs immediately after crossing half health, then
    # follows with Execute.  A fixed card priority therefore performs badly:
    # it can spend Catalyst on a small poison stack that is about to be erased,
    # or cross the phase line before playing a scaling/defensive setup card.
    # These constants are deliberately local to that one encounter so normal
    # combats retain the same fast bounded search.
    CHAMP_TRANSITION_PENALTY = 24.0
    CHAMP_CATALYST_MIN_BURST = 24

    # These enemies immediately replace their current intent with Split when
    # damage takes them to half health or below.  They remain targetable for
    # the rest of the player turn, and every further point of damage lowers
    # the HP inherited by *both* children.
    SPLIT_MONSTER_IDS = {"slimeboss", "acidslimel", "spikeslimel"}

    # A terminal line is only safe to bind when the authoritative hand can
    # be expected to remain the same until the next planned card.  These
    # cards draw, discard, exhaust, or otherwise mutate the hand through a
    # choice/random resolution.  The simulator can still score them in the
    # normal beam, but a later state must re-plan instead of blindly replaying
    # a stale lethal sequence (for example Concentrate can discard Catalyst).
    TERMINAL_PLAN_UNSAFE_HAND_CARDS = {
        "acrobatics",
        "adrenaline",
        "backflip",
        "battletrance",
        "bouncingflask",
        "burst",
        "calculatedgamble",
        "concentrate",
        "daggerthrow",
        "deepbreath",
        "distraction",
        "expertise",
        "masterofstrategy",
        "offering",
        "prepared",
        "reboot",
        "reflex",
        "secrettechnique",
        "secretweapon",
        "skim",
        "stormofsteel",
        "survivor",
        "tactician",
        "truegrit",
        "unload",
        "amplify",
    }

    def __init__(self, priorities):
        self.priorities = priorities
        self.focus_key = None
        self.combat_key = None
        self.last_decision = {}
        # A terminal beam line is a promise, not merely a score hint.  When
        # the first card of a searched lethal line is accepted, keep the
        # remaining UUID/target sequence bound to the same authoritative
        # combat turn.  Without this, the next state refresh could freely
        # re-plan a different second card after a draw, leaving the audit's
        # true_combat_end assertion detached from the action sequence that
        # actually ran (notably Pommel Strike -> Whirlwind versus Strike ->
        # Whirlwind against Curl Up enemies).
        self._terminal_plan = None
        self._terminal_plan_selected_uuid = None
        # A reactive-progress fallback is intentionally limited to one
        # confirmed card per combat turn.  Keep the selected UUID pending so
        # a rejected controller action can be retried; it only counts as used
        # after the authoritative hand no longer contains that card.
        self._reactive_progress_pending = None
        self._card_play_turn_key = None
        self._confirmed_cards_played = 0
        self._confirmed_card_resolutions = 0
        self._confirmed_attack_resolutions = 0
        self._card_play_pending_uuid = None
        self._card_play_pending_resolutions = 1
        self._card_play_pending_is_attack = False
        self._disable_calculated_gamble_rescue = False

    def set_priorities(self, priorities):
        self.priorities = priorities
        self.focus_key = None
        self.combat_key = None
        self.last_decision = {}
        self._terminal_plan = None
        self._terminal_plan_selected_uuid = None
        self._reactive_progress_pending = None
        self._card_play_turn_key = None
        self._confirmed_cards_played = 0
        self._confirmed_card_resolutions = 0
        self._confirmed_attack_resolutions = 0
        self._card_play_pending_uuid = None
        self._card_play_pending_resolutions = 1
        self._card_play_pending_is_attack = False
        self._disable_calculated_gamble_rescue = False

    def choose_target(self, game):
        monsters = combat_predictor.active_monsters(game)
        if not monsters:
            monsters = combat_predictor.living_monsters(game)
        if not monsters:
            return None
        return self._prepare_focus(game, monsters)

    def score_temporary_card(self, game, card):
        """Tactical value for potion/discovery cards that are played now.

        These choices do not enter the permanent deck, so deck-tier reward
        rankings are the wrong objective (most visibly for Power Potion).
        """

        # DiscoveryAction is the authoritative provenance shared by potion
        # card choices and the in-combat Discovery family.  Those cards are
        # made zero-cost for this turn.  Do not infer that rule merely from a
        # CARD_REWARD screen: permanent combat/event rewards use the same
        # surface and must retain their printed cost.
        if _token(getattr(game, "current_action", "")) != "discoveryaction":
            self._last_temporary_card_search = {
                "mode": "static_fallback",
                "reason": "zero_cost_provenance_not_confirmed",
            }
            return self._static_temporary_card_score(game, card)

        # The card-reward screen interrupts an already-live combat turn.  A
        # pending action may have disappeared from the authoritative hand
        # immediately before this screen opened, so confirm that transition
        # once before cloning any alternatives.  Every isolated search below
        # inherits these same-turn counters; otherwise Choker/Normality can
        # incorrectly reopen several card plays for each scored option.
        self._sync_confirmed_card_plays(game)

        if len(getattr(game, "hand", []) or []) >= 10:
            self._last_temporary_card_search = {
                "mode": "fail_closed",
                "reason": "temporary_card_cannot_enter_full_hand",
            }
            return -1000000.0

        card_id = _token(getattr(card, "card_id", ""))
        if (
            int(getattr(card, "cost", 0) or 0) == -1
            and self._x_effect(
                game,
                card,
                upgraded_bonus=True,
                energy_override=max(
                    0, int(getattr(game.player, "energy", 0) or 0)
                ),
            ) <= 0
        ):
            # A generated X card still reads the current Energy pool for its
            # effect.  At E0, setting its visible cost to zero does not create
            # a Skewer/Whirlwind hit; prefer any proven executable option.
            self._last_temporary_card_search = {
                "mode": "fail_closed",
                "reason": "zero_effect_generated_x_card",
                "card_id": getattr(card, "card_id", None),
            }
            return -1000000.0

        baseline = self._temporary_turn_search(game)
        if card_id == "offering":
            candidate = self._temporary_offering_continuation(game, card)
            if candidate is False:
                return -1000000.0
        else:
            candidate = self._temporary_turn_search(game, card)
        if baseline is None or candidate is None:
            # No exact terminal evidence is available.  Preserve the former
            # static utility only as this narrow fallback; an explicitly
            # unsupported or illegal line returned above never reaches it.
            self._last_temporary_card_search = {
                "mode": "static_fallback",
                "reason": "one_turn_search_evidence_unavailable",
            }
            return self._static_temporary_card_score(game, card)

        candidate_evidence, prefix_loss, metadata = candidate
        baseline_evidence, _, _ = baseline
        candidate_rank = self._temporary_terminal_rank(
            game, candidate_evidence, prefix_loss=prefix_loss
        )
        baseline_rank = self._temporary_terminal_rank(
            game, baseline_evidence
        )
        cross_turn_liability = (
            self._temporary_berserk_cross_turn_liability(
                game, card, candidate_evidence
            )
        )
        score = (
            candidate_rank
            - baseline_rank
            - cross_turn_liability["score_penalty"]
        )
        # Self-Forming Clay is a bounded next-turn resource.  Offering's
        # prefix happens outside the continuation planner, so add its exact
        # event credit here, but never carry it past a true combat end.
        score += float(
            metadata.get("self_forming_clay_applied_credit", 0.0) or 0.0
        )
        self._last_temporary_card_search = {
            "mode": "authoritative_one_turn_search",
            "card_id": getattr(card, "card_id", None),
            "prefix_hp_loss": int(prefix_loss),
            "baseline_rank": round(baseline_rank, 3),
            "candidate_rank": round(candidate_rank, 3),
            "marginal_score": round(score, 3),
            "baseline": dict(baseline_evidence),
            "candidate": dict(candidate_evidence),
            "cross_turn_liability": cross_turn_liability,
            **metadata,
        }
        return score

    def _retrieval_card_tactical_value(
        self,
        game,
        card,
        *,
        energy,
        corruption=False,
        bullet_time=False,
    ):
        """Value a card which will enter the current hand immediately.

        Seek and Hologram resolve before their Grid is shown. A high static
        priority is worthless when the remaining energy cannot play the
        selected card, which was the source of late Seek -> Echo Form choices.
        Return None for those non-executable targets so callers can fail
        closed instead of treating next-turn value as current-turn output.
        """

        energy = max(0, int(energy or 0))
        cost = self._state_card_cost(
            card,
            energy,
            corruption=corruption,
            bullet_time=bullet_time,
        )
        if cost > energy:
            return None
        if int(getattr(card, "cost", 0) or 0) == -1 and self._x_effect(
            game,
            card,
            upgraded_bonus=True,
            energy_override=energy,
        ) <= 0:
            return None
        return self._static_temporary_card_score(game, card)

    def score_retrieval_card(self, game, card):
        """Tactical Grid score after Seek/Hologram has already resolved."""

        player = getattr(game, "player", None)
        value = self._retrieval_card_tactical_value(
            game,
            card,
            energy=max(0, int(getattr(player, "energy", 0) or 0)),
            corruption=combat_predictor.has_power(
                player, "Corruption", "CorruptionPower"
            ),
        )
        return -1000000.0 if value is None else value

    def _best_seek_target_value(
        self,
        game,
        *,
        energy,
        corruption=False,
        bullet_time=False,
    ):
        """Best executable draw-pile target at this exact branch point."""

        values = []
        for card in getattr(game, "draw_pile", []) or []:
            if _token(getattr(card, "card_id", "")) == "seek":
                # Avoid recursively pricing another tutor as if it were a
                # concrete payoff. The authoritative refresh can reconsider
                # a second Seek after the first selected card is known.
                continue
            value = self._retrieval_card_tactical_value(
                game,
                card,
                energy=energy,
                corruption=corruption,
                bullet_time=bullet_time,
            )
            if value is not None:
                values.append(float(value))
        return max(values, default=0.0)

    def _static_temporary_card_score(self, game, card):
        """Legacy local utility, used only when exact search is unavailable."""

        monsters = combat_predictor.active_monsters(game)
        if not monsters:
            monsters = combat_predictor.living_monsters(game)
        if not monsters:
            return -1000.0
        preferred = self._prepare_focus(game, monsters)
        incoming = combat_predictor.incoming_damage(game)
        turn_outcome = combat_predictor.projected_turn_outcome(game)
        attack_loss = turn_outcome.attack_hp_loss
        total_loss = turn_outcome.total_hp_loss
        targets = monsters if getattr(card, "has_target", False) else [None]
        candidates = [
            self._candidate(
                game, card, target, preferred, incoming, attack_loss, total_loss
            )
            for target in targets
        ]
        hp = max(1, int(getattr(game.player, "current_hp", 1) or 1))
        mitigation_weight = 5.0 if total_loss >= hp else 1.8 if total_loss > 4 else 0.45
        return max(
            candidate.base_score
            + min(attack_loss, max(0, candidate.intrinsic_mitigation)) * mitigation_weight
            for candidate in candidates
        )

    @staticmethod
    def _temporary_terminal_rank(game, evidence, *, prefix_loss=0):
        """Encode exact terminal facts as a stable scalar choice contract."""

        initial_hp = max(
            1, int(getattr(getattr(game, "player", None), "current_hp", 1) or 1)
        )
        actual_loss = max(0, int(prefix_loss or 0)) + max(
            0, int(evidence.get("actual_loss", 0) or 0)
        )
        remaining_hp = initial_hp - actual_loss
        survives = remaining_hp > 0
        initial_enemy_hp = sum(
            max(0, int(getattr(monster, "current_hp", 0) or 0))
            for monster in combat_predictor.living_monsters(game)
        )
        final_enemy_hp = sum(
            max(0, int(value or 0))
            for value in evidence.get("final_enemy_hp", [])
        )
        progress = max(0, initial_enemy_hp - final_enemy_hp)
        rank = 1000000.0 if survives else -1000000.0
        if survives and evidence.get("true_combat_end"):
            rank += 100000.0
        rank += remaining_hp * 100.0
        rank += progress * 10.0
        rank += min(
            1000.0,
            max(-1000.0, float(evidence.get("plan_score", 0.0) or 0.0)),
        ) * 0.01
        return rank

    def _temporary_berserk_cross_turn_liability(
        self, game, card, evidence,
    ):
        """Price one bounded, observable carry-over Vulnerable exposure.

        Temporary-card search is exact only through the current enemy turn.
        A base Berserk can therefore look free when generated Block absorbs
        its immediate Vulnerable multiplier even though one stack remains
        for the following enemy turn.  Use only the marginal damage already
        demonstrated by surviving enemies' visible attack packets; this is
        strategic debt, not a fabricated prediction of their next intents.

        The guard is deliberately narrow: permanent reward cards never reach
        it, one-turn/upgraded Berserk has no carry-over debt, and current-turn
        HP loss remains owned by the exact terminal search.
        """

        empty = {
            "kind": "none",
            "observed_vulnerable_attack_delta": 0,
            "score_penalty": 0.0,
        }
        if _token(getattr(card, "card_id", "")) != "berserk":
            return empty
        vulnerable_turns = max(
            0, int(getattr(card, "magic_number", 0) or 0)
        )
        if vulnerable_turns <= 1:
            return empty
        if (
            bool(evidence.get("true_combat_end"))
            or not bool(evidence.get("player_vulnerable"))
            or max(0, int(evidence.get("actual_loss", 0) or 0)) > 0
            or max(0, int(evidence.get("final_player_block", 0) or 0)) <= 0
        ):
            return empty
        player = getattr(game, "player", None)
        if (
            combat_predictor.has_power(player, "Vulnerable")
            or combat_predictor.power_amount(player, "Artifact") > 0
            or combat_predictor.has_power(player, "Barricade", "Blur")
        ):
            return empty

        monsters = combat_predictor.active_monsters(game)
        final_hp = list(evidence.get("final_enemy_hp", []) or [])
        branch_damage = list(
            evidence.get("branch_enemy_attack_damage_per_hit", []) or []
        )
        observed_delta = 0
        exposed_indexes = []
        for index, monster in enumerate(monsters):
            if index >= len(final_hp) or int(final_hp[index] or 0) <= 0:
                continue
            if combat_predictor.monster_threat(monster) <= 0:
                continue
            serialized = max(
                0, int(getattr(monster, "move_adjusted_damage", 0) or 0)
            )
            if index < len(branch_damage):
                vulnerable_damage = max(
                    0, int(branch_damage[index] or 0)
                )
                per_hit_delta = max(0, vulnerable_damage - serialized)
            else:
                per_hit_delta = max(
                    0,
                    self._branch_enemy_attack_damage_delta(
                        game, monster, player_vulnerable=True
                    ),
                )
            hits = max(1, int(getattr(monster, "move_hits", 0) or 0))
            packet_delta = per_hit_delta * hits
            if packet_delta <= 0:
                continue
            observed_delta += packet_delta
            exposed_indexes.append(index)

        if observed_delta <= 0:
            return empty
        # Temporary terminal rank values current HP at 100 points.  Charge a
        # discounted tenth of that rate because the next intent is unknown,
        # and cap the debt at two HP-equivalents so this remains a tiebreaking
        # strategic correction rather than a generic ban on Berserk.
        score_penalty = min(200.0, float(observed_delta) * 10.0)
        return {
            "kind": "berserk_carryover_vulnerable_exposure",
            "vulnerable_turns": int(vulnerable_turns),
            "exposed_enemy_indexes": exposed_indexes,
            "observed_vulnerable_attack_delta": int(observed_delta),
            "score_penalty": score_penalty,
        }

    def _temporary_combat_card_copy(self, game, card):
        """Return the generated this-turn card with conservative live stats."""

        generated = copy.deepcopy(card)
        if int(getattr(generated, "cost", 0) or 0) != -1:
            generated.cost = 0
        generated.is_playable = True
        player = getattr(game, "player", None)
        if (
            getattr(generated, "type", None) == CardType.ATTACK
            and int(getattr(generated, "damage", 0) or 0) <= 0
            and int(getattr(generated, "base_damage", -1) or 0) >= 0
        ):
            damage = max(
                0,
                int(getattr(generated, "base_damage", 0) or 0)
                + combat_predictor.signed_power_amount(
                    player, "Strength", "StrengthPower"
                ),
            )
            if combat_predictor.has_power(player, "Weak", "Weakened"):
                damage = int(damage * 0.75)
            generated.damage = damage
        if (
            getattr(generated, "type", None) == CardType.SKILL
            and int(getattr(generated, "block", 0) or 0) <= 0
            and int(getattr(generated, "base_block", -1) or 0) >= 0
        ):
            block = max(
                0,
                int(getattr(generated, "base_block", 0) or 0)
                + combat_predictor.signed_power_amount(
                    player, "Dexterity", "DexterityPower"
                ),
            )
            if combat_predictor.has_power(player, "Frail", "FrailPower"):
                block = int(block * 0.75)
            generated.block = block
        if (
            getattr(generated, "type", None) == CardType.ATTACK
            and combat_predictor.has_power(player, "Entangled")
        ):
            generated.is_playable = False
        return generated

    def _temporary_turn_search(self, game, card=None):
        """Search a fresh cloned current turn with one generated card added."""

        try:
            hypothetical = copy.deepcopy(game)
            generated = None
            if card is not None:
                generated = self._temporary_combat_card_copy(
                    hypothetical, card
                )
                if not getattr(generated, "is_playable", False):
                    return None
                hypothetical.hand.append(generated)
            planner = FastCombatPlanner(self.priorities)
            self._seed_temporary_search_progress(planner, hypothetical)
            planner.choose_card_action(hypothetical)
        except (AttributeError, TypeError, ValueError):
            return None

        decision = dict(getattr(planner, "last_decision", {}) or {})
        evidence = dict(decision.get("search", {}) or {})
        if "actual_loss" not in evidence or "final_enemy_hp" not in evidence:
            return None
        evidence["plan_score"] = float(
            decision.get("plan_score", 0.0) or 0.0
        )
        planned_ids = [
            item.get("card_id")
            for item in decision.get("planned_sequence", []) or []
            if isinstance(item, dict)
        ]
        return evidence, 0, {
            "continuation_reason": decision.get("reason"),
            "continuation_card_ids": planned_ids,
            "source_confirmed_cards_played": int(
                self._confirmed_cards_played or 0
            ),
            "source_confirmed_card_resolutions": int(
                self._confirmed_card_resolutions or 0
            ),
            "generated_card_used_first": bool(
                generated is not None
                and decision.get("card_id")
                == getattr(generated, "card_id", None)
            ),
        }

    def _seed_temporary_search_progress(
        self,
        planner,
        game,
        *,
        added_cards=0,
        added_resolutions=0,
    ):
        """Copy confirmed same-turn progress into one isolated search."""

        planner._card_play_turn_key = planner._combat_turn_key(game)
        planner._confirmed_cards_played = max(
            0,
            int(self._confirmed_cards_played or 0)
            + max(0, int(added_cards or 0)),
        )
        planner._confirmed_card_resolutions = max(
            0,
            int(self._confirmed_card_resolutions or 0)
            + max(0, int(added_resolutions or 0)),
        )

    def _temporary_prefix_resolution_count(self, game, card):
        """Return legal useCard resolutions for an immediately chosen card."""

        card_type = getattr(card, "type", None)
        requested = (
            1
            + int(self._echo_form_duplicates_first_card(game, None))
            + int(
                card_type == CardType.SKILL
                and combat_predictor.power_amount(
                    game.player, "Burst", "BurstPower"
                ) > 0
            )
            + int(
                card_type in {CardType.ATTACK, CardType.SKILL, CardType.POWER}
                and combat_predictor.power_amount(
                    game.player, "Duplication", "DuplicationPower"
                ) > 0
            )
        )
        limits = [requested]
        time_warp_remaining = self._time_warp_remaining(game)
        if time_warp_remaining is not None:
            limits.append(max(0, int(time_warp_remaining or 0)))
        choker_remaining = self._velvet_choker_remaining(
            game,
            self._confirmed_card_resolutions,
            branch_cards_played=0,
        )
        if choker_remaining is not None:
            limits.append(max(0, int(choker_remaining or 0)))
        if any(
            _token(getattr(hand_card, "card_id", "")) == "normality"
            for hand_card in getattr(game, "hand", []) or []
        ):
            limits.append(
                max(0, 3 - int(self._confirmed_card_resolutions or 0))
            )
        return max(0, min(limits))

    def _temporary_offering_continuation(self, game, card):
        """Resolve Offering's exact HP/energy/draw prefix, then re-search."""

        def fail(reason):
            self._last_temporary_card_search = {
                "mode": "fail_closed",
                "reason": reason,
                "card_id": getattr(card, "card_id", None),
            }
            return False

        hand = list(getattr(game, "hand", []) or [])
        player = getattr(game, "player", None)
        if player is None or combat_predictor.has_power(
            player, "No Draw", "NoDrawPower"
        ):
            return fail("offering_draw_not_available")
        if any(
            _token(getattr(hand_card, "card_id", ""))
            in {"normality", "pain", "bloodforblood"}
            for hand_card in hand
        ):
            return fail("offering_hand_trigger_not_exact")
        if self._time_warp_remaining(game) is not None or self._has_relic(
            game, "Velvet Choker"
        ):
            return fail("offering_card_limit_not_exact")

        unsupported_player_powers = {
            "afterimage", "afterimagepower", "buffer", "burst",
            "burstpower", "confusion", "corruption", "darkembrace",
            "darkembracepower", "duplication", "duplicationpower",
            "echoform", "echoformpower", "evolve", "evolvepower", "hex",
            "hexpower", "feelnopain", "feelnopainpower", "panache",
            "rupture", "rupturepower",
            "thousandcuts", "thousandcutspower",
        }
        if any(
            _token(getattr(power, "power_id", ""))
            in unsupported_player_powers
            or _token(getattr(power, "power_name", ""))
            in unsupported_player_powers
            for power in getattr(player, "powers", []) or []
        ):
            return fail("offering_player_trigger_not_exact")
        unsupported_relics = {
            "bluecandle", "centennialpuzzle", "charonsashes",
            "deadbranch", "inkbottle", "letteropener", "redskull",
            "runiccube", "strangespoon",
        }
        if any(
            _token(getattr(relic, "relic_id", "")) in unsupported_relics
            for relic in getattr(game, "relics", []) or []
        ):
            return fail("offering_relic_trigger_not_exact")
        unsupported_enemy_powers = {
            "beatofdeath", "beatofdeathpower", "choke", "chokepower",
            "enrage", "enragepower", "slow", "slowpower", "timewarp",
            "timewarppower",
        }
        if any(
            _token(getattr(power, "power_id", ""))
            in unsupported_enemy_powers
            or _token(getattr(power, "power_name", ""))
            in unsupported_enemy_powers
            for monster in combat_predictor.living_monsters(game)
            for power in getattr(monster, "powers", []) or []
        ):
            return fail("offering_enemy_card_trigger_not_exact")

        offering_resolutions = self._temporary_prefix_resolution_count(
            game, card
        )
        if offering_resolutions <= 0:
            return fail("offering_card_resolution_limit_reached")
        draw_count = (
            self._card_draw_count(card, game) * offering_resolutions
        )
        draw_pile = list(getattr(game, "draw_pile", []) or [])
        if (
            draw_count <= 0
            or draw_count > len(draw_pile)
            or draw_count > max(0, 10 - len(hand))
        ):
            return fail("offering_draw_or_hand_limit_not_exact")
        if getattr(game, "draw_pile_order_known", True) is not True:
            return fail("offering_draw_order_unknown")
        drawn = list(reversed(draw_pile[-draw_count:]))
        unsupported_conditional_draws = {
            "clash", "grandfinale", "reflex", "signaturemove", "tactician",
        }
        if any(
            _token(getattr(drawn_card, "card_id", ""))
            in unsupported_conditional_draws
            for drawn_card in drawn
        ):
            return fail("offering_drawn_card_legality_not_exact")

        self_outcome = combat_predictor.resolve_player_damage_events(
            game,
            tuple(
                combat_predictor.PlayerDamageEvent(
                    "offering", 6, False
                )
                for _ in range(offering_resolutions)
            ),
            block=max(0, int(getattr(player, "block", 0) or 0)),
            buffer_layers=0,
        )
        (
            clay_hp_loss_events,
            clay_future_block,
            clay_credit,
        ) = self._self_forming_clay_value(
            game, len(self_outcome.hp_loss_events)
        )
        prefix_loss = int(self_outcome.hp_loss or 0)
        current_hp = max(0, int(getattr(player, "current_hp", 0) or 0))
        if prefix_loss >= current_hp:
            return fail("offering_self_damage_is_lethal")

        hypothetical = copy.deepcopy(game)
        hypothetical.player.current_hp = current_hp - prefix_loss
        if hasattr(hypothetical, "current_hp"):
            hypothetical.current_hp = hypothetical.player.current_hp
        copied_drawn = list(reversed(hypothetical.draw_pile[-draw_count:]))
        hypothetical.draw_pile = hypothetical.draw_pile[:-draw_count]
        void_debt = sum(
            1
            for drawn_card in copied_drawn
            if _token(getattr(drawn_card, "card_id", "")) == "void"
        )
        hypothetical.player.energy = max(
            0,
            int(getattr(player, "energy", 0) or 0)
            + self._energy_gain(card, game) * offering_resolutions
            - void_debt,
        )
        hypothetical.hand = list(hypothetical.hand) + copied_drawn
        entangled = combat_predictor.has_power(
            hypothetical.player, "Entangled"
        )
        for hand_card in hypothetical.hand:
            card_type = getattr(hand_card, "type", None)
            cost = int(getattr(hand_card, "cost", 0) or 0)
            hand_card.is_playable = bool(
                card_type in {CardType.ATTACK, CardType.SKILL, CardType.POWER}
                and (cost == -1 or cost <= hypothetical.player.energy)
                and not (entangled and card_type == CardType.ATTACK)
            )
        hypothetical.current_action = None

        try:
            planner = FastCombatPlanner(self.priorities)
            self._seed_temporary_search_progress(
                planner,
                hypothetical,
                added_cards=1,
                added_resolutions=offering_resolutions,
            )
            planner.choose_card_action(hypothetical)
        except (AttributeError, TypeError, ValueError):
            return fail("offering_continuation_search_failed")
        decision = dict(getattr(planner, "last_decision", {}) or {})
        evidence = dict(decision.get("search", {}) or {})
        if "actual_loss" not in evidence or "final_enemy_hp" not in evidence:
            return fail("offering_continuation_has_no_terminal_evidence")
        evidence["plan_score"] = float(
            decision.get("plan_score", 0.0) or 0.0
        )
        return evidence, prefix_loss, {
            "continuation_reason": decision.get("reason"),
            "continuation_card_ids": [
                item.get("card_id")
                for item in decision.get("planned_sequence", []) or []
                if isinstance(item, dict)
            ],
            "offering_drawn_card_ids": [
                getattr(drawn_card, "card_id", None)
                for drawn_card in drawn
            ],
            "offering_void_energy_debt": int(void_debt),
            "offering_resolution_count": int(offering_resolutions),
            "self_forming_clay_hp_loss_events": int(
                clay_hp_loss_events
            ),
            "self_forming_clay_future_block": int(clay_future_block),
            "self_forming_clay_credit": round(clay_credit, 3),
            "self_forming_clay_applied_credit": round(
                0.0 if evidence.get("true_combat_end") else clay_credit,
                3,
            ),
            "self_forming_clay_current_turn_mitigation": 0,
            "self_forming_clay_credit_authority": (
                "offering_prefix_hp_loss_events"
            ),
            "continuation_confirmed_cards_played": int(
                self._confirmed_cards_played + 1
            ),
            "continuation_confirmed_card_resolutions": int(
                self._confirmed_card_resolutions + offering_resolutions
            ),
        }

    def _current_combat_key(self, game):
        return (
            int(getattr(game, "act", 0) or 0),
            int(getattr(game, "floor", 0) or 0),
            str(getattr(game, "room_type", "")),
        )

    def _clear_terminal_plan(self):
        self._terminal_plan = None
        self._terminal_plan_selected_uuid = None

    def _arm_terminal_plan(self, game, plan, search):
        """Bind a proven lethal line or deterministic Demon Form setup line.

        The ordered beam is re-run after every authoritative action.  That is
        normally desirable, but it must not replace a line which explicitly
        proved ``true_combat_end``: doing so makes the proof unobservable and
        can turn a lethal AOE follow-up into an avoidable extra enemy turn.
        Demon Form needs the same narrow protection when the ordered search
        already accepted a deterministic prefix followed by the power.  A
        fresh search after that prefix otherwise compares the remaining
        three Energy against immediate damage again and silently abandons
        the long-fight plan.  Only the prefix through Demon Form is bound;
        subsequent actions are always replanned.  Both bindings fail closed
        if any card lacks a stable UUID.
        """

        if not isinstance(search, dict) or search.get(
            "writhing_mass_replan_required"
        ):
            self._clear_terminal_plan()
            return
        plan = list(plan or ())
        terminal_binding = bool(search.get("true_combat_end"))
        setup_index = next(
            (
                index for index, candidate in enumerate(plan)
                if index > 0
                and _token(
                    getattr(getattr(candidate, "card", None), "card_id", "")
                ) in {"demonform", "echoform"}
            ),
            None,
        )
        setup_binding = not terminal_binding and setup_index is not None
        if not terminal_binding and not setup_binding:
            self._clear_terminal_plan()
            return
        if setup_binding:
            setup_card = getattr(plan[setup_index], "card", None)
            baseline_loss = combat_predictor.projected_turn_outcome(
                game
            ).total_hp_loss
            lifecycle = self._lifecycle_evaluation(
                game, setup_card, baseline_loss
            )
            promised_loss = search.get("actual_loss")
            if (
                not isinstance(lifecycle, dict)
                or int(lifecycle.get("triggers", 0) or 0) < 2
                or float(lifecycle.get("adjustment", 0.0) or 0.0) <= 0.0
                or not isinstance(promised_loss, (int, float))
                or int(promised_loss) >= max(
                    1, int(getattr(game.player, "current_hp", 0) or 0)
                )
            ):
                self._clear_terminal_plan()
                return
            # The binding exists only to keep the already-selected setup
            # promise.  Never replay speculative actions after the power.
            plan = plan[:setup_index + 1]
        if self._has_relic(game, "Necronomicon"):
            # Necronomicon's private per-turn ``activated`` flag is not on
            # the authoritative relic surface yet.  One-action scoring may
            # remain conservative, but a stale multi-action terminal promise
            # must never omit its copy hooks or reactive costs.
            self._clear_terminal_plan()
            return
        entries = []
        heatsinks_layers = self._heatsinks_layers(game)
        if combat_predictor.power_amount(
            getattr(game, "player", None), "Burst", "BurstPower"
        ) > 0:
            # The search models exact duplicated poison/debuff effects, but
            # several Skill-only draw/block/heal effects remain deliberately
            # conservative. Re-plan after every authoritative Burst action
            # instead of binding a stale terminal continuation.
            self._clear_terminal_plan()
            return
        visible_card_uuids = {
            getattr(card, "uuid", None)
            for card in (getattr(game, "hand", []) or [])
            if getattr(card, "uuid", None)
        }
        unknown_future_cards = bool(search.get("future_draw_pile_unknown"))
        for candidate in plan or ():
            card = getattr(candidate, "card", None)
            card_id = _token(getattr(card, "card_id", ""))
            if (
                card_id in self.TERMINAL_PLAN_UNSAFE_HAND_CARDS
                or self._card_draw_count(card, game) > 0
                or (
                    getattr(card, "type", None) == CardType.POWER
                    and heatsinks_layers > 0
                )
            ):
                self._clear_terminal_plan()
                return
            card_uuid = getattr(card, "uuid", None)
            if not card_uuid:
                self._clear_terminal_plan()
                return
            # A Hex/unknown-draw marker is not by itself a reason to discard
            # an otherwise fully visible lethal line.  It remains fail-closed
            # when the line actually depends on a card which was not present
            # in the authoritative hand, while visible no-draw cards remain
            # safely bindable by UUID.
            if unknown_future_cards and card_uuid not in visible_card_uuids:
                self._clear_terminal_plan()
                return
            target = getattr(candidate, "target", None)
            entries.append({
                "card_id": getattr(card, "card_id", None),
                "card_uuid": card_uuid,
                "target_key": (
                    list(_monster_key(target)) if target is not None else None
                ),
            })
            if card_id == "heatsinks":
                heatsinks_layers += max(
                    1, int(getattr(card, "magic_number", 0) or 0)
                )
        if len(entries) < 2:
            self._clear_terminal_plan()
            return
        self._terminal_plan = {
            "context": self._combat_turn_key(game),
            "entries": entries,
            "search": dict(search),
            "plan_binding": (
                "terminal_combat_end"
                if terminal_binding else "demon_form_setup"
            ),
        }
        self._terminal_plan_selected_uuid = None

    def _current_action_search_projection(self, game, card, target):
        """Re-simulate one cached action against its authoritative frame."""

        active = list(combat_predictor.active_monsters(game))
        if not active:
            active = list(combat_predictor.living_monsters(game))
        if not active:
            return None
        incoming = combat_predictor.incoming_damage(game)
        turn_outcome = combat_predictor.projected_turn_outcome(game)
        attack_loss = turn_outcome.attack_hp_loss
        total_loss = turn_outcome.total_hp_loss
        preferred = target or combat_predictor.choose_attack_target(
            game, active
        )
        groups = self._build_candidates(
            game,
            [card],
            active,
            preferred,
            incoming,
            attack_loss,
            total_loss,
        )
        if target is not None:
            groups = [[
                candidate
                for candidate in groups[0]
                if candidate.target is target
            ]]
        act_budget = {1: 8, 2: 4, 3: 2, 4: 0}.get(
            int(getattr(game, "act", 0) or 0), 2
        )
        risk_budget = min(
            act_budget,
            max(
                0,
                int(getattr(game.player, "current_hp", 0) or 0) // 8,
            ),
        )
        previous_search = dict(getattr(self, "_last_search", {}) or {})
        previous_initial = dict(
            getattr(self, "_last_initial_search", {}) or {}
        )
        previous_fallback = getattr(self, "_last_verified_fallback", None)
        try:
            self._best_plan(
                game,
                groups,
                attack_loss,
                total_loss,
                risk_budget,
            )
            return dict(getattr(self, "_last_search", {}) or {})
        finally:
            self._last_search = previous_search
            self._last_initial_search = previous_initial
            self._last_verified_fallback = previous_fallback

    def _continue_terminal_plan(self, game):
        """Return the next card of a still-valid terminal plan, if any."""

        pending = self._terminal_plan
        if not pending:
            return None
        if pending.get("context") != self._combat_turn_key(game):
            self._clear_terminal_plan()
            return None
        # A terminal plan is armed before the bridge confirms the first
        # action.  If a stale plan survives until the Choker cap, fail closed
        # rather than issuing a seventh card command.
        choker_remaining = self._velvet_choker_remaining(game)
        if choker_remaining is not None and choker_remaining <= 0:
            self._clear_terminal_plan()
            return None
        if (
            choker_remaining is not None
            and len(pending.get("entries") or ()) > choker_remaining
        ):
            self._clear_terminal_plan()
            return None

        hand = list(getattr(game, "hand", []) or [])
        selected_uuid = self._terminal_plan_selected_uuid
        entries = pending.get("entries") or []
        advanced = False
        # The first entry is the card already returned by the previous call.
        # Only advance after the authoritative hand no longer contains it;
        # this also makes a rejected/retried action idempotent.
        while entries:
            card_uuid = entries[0].get("card_uuid")
            visible = next(
                (
                    card for card in hand
                    if getattr(card, "uuid", None) == card_uuid
                ),
                None,
            )
            if visible is not None:
                break
            entries.pop(0)
            selected_uuid = None
            advanced = True
        if not entries:
            self._clear_terminal_plan()
            return None

        # A plan is armed only after its first card has been returned.  If a
        # caller restored planner memory without that marker, fail closed and
        # let the ordinary search choose the first card again.
        if selected_uuid is None and not advanced:
            self._clear_terminal_plan()
            return None

        entry = entries[0]
        card = visible
        if getattr(card, "is_playable", False) is False:
            self._clear_terminal_plan()
            return None
        if combat_predictor.card_energy_cost(game, card) > max(
            0, int(getattr(game.player, "energy", 0) or 0)
        ):
            self._clear_terminal_plan()
            return None
        # A terminal sequence is bound across authoritative bridge frames.
        # Re-check resources that are consumed by the card itself before
        # replaying a continuation: a previous Fission/Dualcast/Consume may
        # have emptied the orb queue, or the bridge may have refreshed empty
        # slots after an earlier action.  The normal ordered search already
        # rejects this branch; the continuation path must not bypass that
        # guard and claim a no-op card is part of a lethal line.
        if (
            self._orb_requires_occupied(
                _token(getattr(card, "card_id", ""))
            )
            and not self._occupied_orbs(game)
        ):
            self._clear_terminal_plan()
            return None

        target = None
        target_key = entry.get("target_key")
        if target_key is not None:
            target = next(
                (
                    monster
                    for monster in combat_predictor.active_monsters(game)
                    if list(_monster_key(monster)) == list(target_key)
                ),
                None,
            )
            if target is None:
                self._clear_terminal_plan()
                return None
            # A terminal line may include a target which was alive when the
            # first card was selected but became passively doomed after an
            # authoritative refresh.  Replaying another ordinary attack on
            # that target is redundant; fail closed and let the normal search
            # re-plan against the remaining active enemies.
            if id(target) in {
                id(monster)
                for monster in combat_predictor.projected_doomed_monsters(game)
            }:
                self._clear_terminal_plan()
                return None
        self._terminal_plan_selected_uuid = getattr(card, "uuid", None)
        current_search = self._current_action_search_projection(
            game, card, target
        )
        plan_binding = pending.get("plan_binding", "terminal_combat_end")
        if plan_binding == "demon_form_setup":
            current_loss = (
                current_search.get("actual_loss")
                if isinstance(current_search, dict) else None
            )
            promised_loss = (pending.get("search") or {}).get("actual_loss")
            lifecycle = self._lifecycle_evaluation(
                game,
                card,
                combat_predictor.projected_turn_outcome(game).total_hp_loss,
            )
            if (
                _token(getattr(card, "card_id", ""))
                not in {"demonform", "echoform"}
                or not isinstance(current_loss, (int, float))
                or not isinstance(promised_loss, (int, float))
                or int(current_loss) > int(promised_loss)
                or int(current_loss) >= max(
                    1, int(getattr(game.player, "current_hp", 0) or 0)
                )
                or not isinstance(lifecycle, dict)
                or int(lifecycle.get("triggers", 0) or 0) < 2
                or float(lifecycle.get("adjustment", 0.0) or 0.0) <= 0.0
            ):
                self._clear_terminal_plan()
                return None
        continuation_damage = (
            current_search.get("first_action_expected_enemy_hp_loss")
            if current_search
            else None
        )
        if continuation_damage is None and current_search:
            continuation_damage = current_search.get(
                "first_action_enemy_hp_loss"
            )
        if continuation_damage is None:
            continuation_damage = 0
        if (
            not current_search
            and getattr(getattr(card, "type", None), "name", "") == "ATTACK"
        ):
            raw_damage, hits = combat_predictor.card_attack_profile(game, card)
            if raw_damage > 0 and hits > 0:
                targets = [target] if target is not None else list(
                    combat_predictor.living_monsters(game)
                )
                continuation_damage = sum(
                    self._current_attack_hp_loss(
                        game, card, monster, raw_damage, hits,
                    )
                    for monster in targets
                    if monster is not None
                )
        self.last_decision = {
            "reason": (
                "strategic_setup_plan_continuation"
                if plan_binding == "demon_form_setup"
                else "terminal_plan_continuation"
            ),
            "card_id": getattr(card, "card_id", None),
            "card_damage": int(continuation_damage),
            "target_key": list(_monster_key(target)) if target is not None else None,
            "planned_sequence": [dict(item) for item in entries],
            # The original whole-line search is stale after every bridge
            # refresh. Current-frame simulation includes Corpse Explosion,
            # A Thousand Cuts, orb overflow and discard relic packets for the
            # exact card whose consequence the audit will observe.
            "search": (
                current_search
                if current_search
                else dict(pending.get("search") or {})
            ),
            "plan_binding": plan_binding,
        }
        if target is not None:
            return self._play_card_action(game, card, target)
        return self._play_card_action(game, card)

    def _prepare_focus(self, game, monsters):
        combat_key = self._current_combat_key(game)
        if combat_key != self.combat_key:
            self.combat_key = combat_key
            self.focus_key = None

        forced = self._encounter_priority_target(game, monsters)
        if forced is not None:
            # Encounter summons can change the incoming damage by far more
            # than ordinary focus hysteresis allows.  Commit to the urgent add
            # immediately instead of continuing to tunnel the boss.
            self.focus_key = _monster_key(forced)
            return forced

        by_key = {_monster_key(monster): monster for monster in monsters}
        focused = by_key.get(self.focus_key)
        challenger = max(monsters, key=self._target_priority)
        if focused is None:
            if len(monsters) > 1 and all(
                self._is_slime_family(monster) for monster in monsters
            ):
                # A controller may be restarted between authoritative frames,
                # so the in-memory focus is only a cache.  Recover an already
                # established slime damage line from observable HP instead of
                # letting the currently attacking sibling erase that progress.
                # At a fresh/equal split there is no evidence to recover and
                # ordinary whole-turn threat scoring remains authoritative.
                invested = [
                    monster
                    for monster in monsters
                    if int(getattr(monster, "current_hp", 0) or 0)
                    < max(1, int(getattr(monster, "max_hp", 0) or 0))
                ]
                if invested:
                    recovered = min(
                        invested,
                        key=lambda monster: (
                            (
                                int(getattr(monster, "current_hp", 0) or 0)
                                + int(getattr(monster, "block", 0) or 0)
                            )
                            / max(
                                1,
                                int(getattr(monster, "max_hp", 0) or 0),
                            ),
                            int(getattr(monster, "current_hp", 0) or 0)
                            + int(getattr(monster, "block", 0) or 0),
                            -self._target_priority(monster),
                        ),
                    )
                    self.focus_key = _monster_key(recovered)
                    return recovered
            return challenger

        focused_score = self._target_priority(focused)
        challenger_score = self._target_priority(challenger)
        if self._is_slime_family(focused) and all(
            self._is_slime_family(monster) for monster in monsters
        ):
            # Current intent is a control concern, not a reason to throw away
            # damage already invested toward a kill/split threshold.  The
            # whole-turn search may still target an attacker when doing so
            # improves real HP loss; this only keeps the persistent damage
            # focus from oscillating left/right with alternating intents.
            return focused
        # Hysteresis prevents intent changes from scattering damage.  A newly
        # urgent target can still override the commitment.
        if challenger is not focused and challenger_score > focused_score * 1.7 + 5:
            return challenger
        return focused

    def _healer_support_priority_target(self, game, monsters):
        """Break stale damage focus while a healer can erase that damage.

        The Centurion/Mystic encounter is serialized with ``Healer`` as the
        support monster id.  A generic focus hysteresis is useful in most
        multi-enemy fights, but here it can lock every single-target attack
        onto Centurion while the untouched Healer repeatedly restores the
        investment.  Override only from authoritative encounter facts: a
        living support enemy, another living enemy, and either observable
        wounds, a support intent, or an already-established Healer focus.

        Two narrow exceptions remain authoritative.  Do not abandon an
        ordinary single-card true kill on the current primary target.  On a
        currently lethal enemy turn, switch from the support only when the
        most dangerous attacker also has an ordinary single-card true kill;
        merely pointing damage at that attacker does not reduce incoming.
        Multi-card exact kills are still selected by the guaranteed-combo
        pass which runs immediately after focus preparation.
        """

        healers = [
            monster
            for monster in monsters
            if _token(getattr(monster, "monster_id", ""))
            in {"healer", "mystic"}
        ]
        if not healers:
            return None
        healer = max(healers, key=self._target_priority)
        companions = [monster for monster in monsters if monster is not healer]
        if not companions:
            return None

        support_intent = getattr(healer, "intent", None) in {
            Intent.BUFF,
            Intent.DEFEND_BUFF,
            Intent.MAGIC,
        }
        wounded_enemy = any(
            int(getattr(monster, "current_hp", 0) or 0)
            < max(1, int(getattr(monster, "max_hp", 0) or 0))
            for monster in monsters
        )
        continuing_healer_focus = self.focus_key == _monster_key(healer)
        if not (support_intent or wounded_enemy or continuing_healer_focus):
            return None

        by_key = {_monster_key(monster): monster for monster in monsters}
        primary = by_key.get(self.focus_key)
        if primary is None or primary is healer:
            primary = max(companions, key=self._target_priority)

        playable = [
            card
            for card in getattr(game, "hand", []) or []
            if getattr(card, "is_playable", False)
            and combat_predictor.card_energy_cost(game, card)
            <= max(0, int(getattr(game.player, "energy", 0) or 0))
        ]
        player_hp = max(
            1, int(getattr(getattr(game, "player", None), "current_hp", 1) or 1)
        )
        turn_outcome = combat_predictor.projected_turn_outcome(game)
        if turn_outcome.total_hp_loss >= player_hp:
            attackers = [
                monster
                for monster in monsters
                if getattr(monster, "intent", None) is not None
                and getattr(monster, "intent", None).is_attack()
            ]
            if attackers:
                primary_attacker = max(
                    attackers,
                    key=lambda monster: (
                        combat_predictor.monster_threat(monster),
                        -int(getattr(monster, "current_hp", 0) or 0),
                    ),
                )
                if (
                    combat_predictor.monster_threat(primary_attacker) > 0
                    and self._current_single_card_true_kill(
                        game, playable, primary_attacker
                    )
                    and combat_predictor.projected_turn_outcome(
                        game, excluded_monsters=[primary_attacker]
                    ).total_hp_loss
                    < turn_outcome.total_hp_loss
                ):
                    return primary_attacker
            return healer
        if self._current_single_card_true_kill(game, playable, primary):
            return primary
        return healer

    def _restore_immediate_reptomancer_attack_targets(
        self, game, playable, active
    ):
        """Restore only passively-doomed daggers whose current move is lethal.

        ``active_monsters`` intentionally removes enemies that are guaranteed
        to die by the end of the turn.  Noxious Fumes is part of that longer
        projection, but its Poison is applied after the enemy's currently
        displayed move.  In a lethal Reptomancer turn, keep an excluded
        attacker targetable only when the current hand has an affordable,
        deterministic one-card kill.  Existing Poison that kills before the
        move remains excluded, and an immediate boss kill remains superior.
        """

        living = combat_predictor.living_monsters(game)
        reptomancer = next(
            (
                monster
                for monster in living
                if _token(getattr(monster, "monster_id", ""))
                == "reptomancer"
            ),
            None,
        )
        if reptomancer is None:
            return active
        if self._current_single_card_true_kill(
            game, playable, reptomancer
        ):
            return active
        player_hp = max(
            1,
            int(
                getattr(
                    getattr(game, "player", None), "current_hp", 1
                )
                or 1
            ),
        )
        baseline_loss = (
            combat_predictor.projected_turn_outcome(game).total_hp_loss
        )
        if baseline_loss < player_hp:
            return active

        active_ids = {id(monster) for monster in active}
        restored = [
            monster
            for monster in combat_predictor.attack_monsters(game)
            if id(monster) not in active_ids
            and _token(getattr(monster, "monster_id", "")) == "dagger"
            and combat_predictor.monster_threat(monster) > 0
            and self._current_single_card_true_kill(
                game, playable, monster
            )
            and combat_predictor.projected_turn_outcome(
                game, excluded_monsters=[monster]
            ).total_hp_loss
            < player_hp
        ]
        if not restored:
            return active
        return list(active) + restored

    def _encounter_priority_target(self, game, monsters):
        ids = {
            _token(getattr(monster, "monster_id", "")): monster
            for monster in monsters
        }
        if "reptomancer" in ids:
            daggers = [
                monster for monster in monsters
                if _token(getattr(monster, "monster_id", "")) == "dagger"
            ]
            urgent = [
                monster for monster in daggers
                if combat_predictor.monster_threat(monster) > 0
            ]
            if urgent:
                return max(
                    urgent,
                    key=lambda monster: (
                        combat_predictor.monster_threat(monster),
                        -int(getattr(monster, "current_hp", 0) or 0),
                    ),
                )
        awakened = ids.get("awakenedone")
        if awakened is not None and combat_predictor.has_power(
            awakened,
            "Curiosity",
            "CuriosityPower",
            "Unawakened",
            "UnawakenedPower",
        ):
            cultists = [
                monster for monster in monsters
                if _token(getattr(monster, "monster_id", "")) == "cultist"
            ]
            if cultists:
                return max(
                    cultists,
                    key=lambda monster: (
                        combat_predictor.monster_threat(monster),
                        -int(getattr(monster, "current_hp", 0) or 0),
                    ),
                )
        return self._healer_support_priority_target(game, monsters)

    def _should_update_damage_focus(self, candidate):
        """Keep one-turn control from overwriting the persistent kill target."""

        target = getattr(candidate, "target", None)
        if target is None or int(getattr(candidate, "damage", 0) or 0) <= 0:
            return False
        target_key = _monster_key(target)
        if self.focus_key is None or self.focus_key == target_key:
            return True
        # Neutralize and similar attacks may correctly point at the monster
        # whose current attack must be weakened.  That is a control target,
        # not evidence that damage already invested elsewhere should be
        # abandoned on the next authoritative frame.
        if int(getattr(candidate, "intrinsic_mitigation", 0) or 0) > 0:
            return False
        return True

    def _target_priority(self, monster):
        effective_hp = max(
            1,
            int(getattr(monster, "current_hp", 0) or 0)
            + int(getattr(monster, "block", 0) or 0),
        )
        threat = combat_predictor.monster_threat(monster)
        enemy_id = _token(getattr(monster, "monster_id", ""))
        bonus = self.SCALING_ENEMY_BONUS.get(enemy_id, 0)
        return threat * 0.45 + bonus + max(0, 24 - effective_hp) * 0.18

    def _persistent_debuff_pressure(self, game, monster):
        """Price recurring permanent stat loss against the live deck.

        The returned value is an output-at-risk estimate, not fabricated HP
        loss.  It is used to compare taking an affordable hit now with losing
        more damage and Block on every later cycle.  Exact terminal survival
        checks still reject lethal or reserve-breaking lines.
        """

        if game is None:
            return 0.0
        enemy_id = _token(getattr(monster, "monster_id", ""))
        profile = self.PERSISTENT_DEBUFF_PROFILES.get(enemy_id)
        if not profile:
            return 0.0

        cards = self._known_combat_cards(game)
        if not cards:
            return 0.0
        deck_size = max(1, len(cards))
        draw_fraction = min(1.0, 5.0 / deck_size)
        attack_hits = 0.0
        block_applications = 0.0
        for card in cards:
            if getattr(card, "type", None) == CardType.ATTACK:
                raw_damage, hits = self._future_attack_profile(game, card)
                if raw_damage > 0 and hits > 0:
                    attack_hits += max(1, hits) * draw_fraction
            if max(
                0,
                int(
                    getattr(card, "block", 0)
                    or getattr(card, "base_block", 0)
                    or 0
                ),
            ) > 0:
                block_applications += draw_fraction

        exposure_turns = max(
            1,
            int(profile.get("attack_turns_per_cycle", 1) or 1),
        )
        energy = max(
            1,
            int(getattr(getattr(game, "player", None), "energy", 0) or 3),
        )
        # A five-card hand cannot normally spend more positive-cost cards than
        # its Energy.  Keep zero-cost/multi-hit decks distinguishable without
        # letting a large master deck manufacture unbounded exposure.
        attack_exposure = min(
            5.0 * exposure_turns,
            attack_hits * exposure_turns,
            (energy + 2.0) * exposure_turns,
        )
        block_exposure = min(
            5.0 * exposure_turns,
            block_applications * exposure_turns,
            (energy + 2.0) * exposure_turns,
        )
        future_loss = (
            max(0, int(profile.get("strength_loss", 0) or 0))
            * attack_exposure
            + max(0, int(profile.get("dexterity_loss", 0) or 0))
            * block_exposure
        )

        player = getattr(game, "player", None)
        strength_debt = max(
            0,
            -combat_predictor.signed_power_amount(player, "Strength"),
        )
        dexterity_debt = max(
            0,
            -combat_predictor.signed_power_amount(player, "Dexterity"),
        )
        accumulated_loss = (
            strength_debt * min(5.0, attack_hits)
            + dexterity_debt * min(5.0, block_applications)
        )
        return min(30.0, future_loss + accumulated_loss)

    def combat_threat_profile(self, game, active=None):
        """Share encounter urgency without changing exact damage forecasts.

        Special encounter clocks remain under their dedicated combat rules:
        Time Eater must not receive a generic per-card growth allowance, while
        Transient has a finite survival clock rather than a damage race.
        Potion timing still needs to recognize both as nontrivial threats.
        """
        active = (
            combat_predictor.active_monsters(game)
            if active is None else active
        )
        enemies = []
        for monster in active:
            if int(getattr(monster, "current_hp", 0) or 0) <= 1:
                continue
            enemy_id = _token(getattr(monster, "monster_id", ""))
            special = {
                "timeeater": "time_warp_and_haste",
                "transient": "finite_survival_clock",
            }.get(enemy_id)
            pressure = self._monster_scaling_pressure(monster, game)
            if special or pressure > 0:
                enemies.append({
                    "enemy_id": enemy_id,
                    "monster_index": getattr(monster, "monster_index", None),
                    "model": special or "shared_scaling_pressure",
                    "pressure": round(pressure, 3),
                })
        return {
            "has_scaling_threat": bool(enemies),
            "enemies": enemies,
        }

    def _monster_scaling_pressure(self, monster, game=None):
        """Return bounded multi-turn urgency for an enemy left alive.

        The value combines known snowballing encounters with authoritative
        growth powers. It is intentionally shared by all player classes and
        only relaxes current-turn risk while the player still survives.
        """

        enemy_id = _token(getattr(monster, "monster_id", ""))
        # Time Eater's pressure is governed by the exact 12-card counter and
        # Haste transition below. Treating it as generic per-turn growth can
        # make the planner spend card twelve for no tactical benefit.
        if enemy_id == "timeeater":
            return 0.0
        pressure = self.SCALING_ENEMY_BONUS.get(enemy_id, 0) * 0.55
        pressure += self._persistent_debuff_pressure(game, monster)
        pressure += combat_predictor.power_amount(monster, "Ritual") * 2.5
        for power in getattr(monster, "powers", []) or []:
            power_id = _token(
                getattr(power, "power_id", "") or getattr(power, "name", "")
            )
            if "ritual" in power_id:
                continue
            if any(marker in power_id for marker in (
                "curiosity", "enrage", "angry", "strengthup", "growth",
            )):
                pressure += max(1, int(getattr(power, "amount", 0) or 0)) * 2.0
        return min(30.0, pressure)

    @staticmethod
    def _known_combat_cards(game):
        """Return one de-duplicated view of cards which can cycle this fight."""

        def collect(zones):
            cards = []
            seen = set()
            for zone in zones:
                for card in getattr(game, zone, []) or []:
                    key = getattr(card, "uuid", None) or id(card)
                    if key in seen:
                        continue
                    seen.add(key)
                    cards.append(card)
            return cards

        # CommunicationMod's persistent ``deck`` copies are not combat
        # objects: their dynamic damage/block fields can remain -1 while the
        # same UUID in hand/draw/discard contains the resolved values.  They
        # also retain Powers and cards which have already exhausted this
        # fight.  Use only the cycling combat zones whenever they are
        # available, both to retain resolved attack profiles and to avoid
        # pricing cards which can no longer be drawn.  The master deck is a
        # fallback for partial/offline fixtures which expose no combat zone.
        combat_zones = ("hand", "draw_pile", "discard_pile")
        available_combat_zones = tuple(
            zone for zone in combat_zones if hasattr(game, zone)
        )
        if available_combat_zones:
            return collect(available_combat_zones)
        return collect(("deck",))

    @staticmethod
    def _future_attack_profile(game, card, *, energy_override=None):
        """Return a conservative profile for a card outside the live hand.

        CommunicationMod resolves ``damage`` only for cards in hand.  Draw and
        discard pile attacks therefore legitimately arrive with ``damage=-1``
        while retaining their authoritative upgraded ``base_damage``.  Exact
        current-turn simulation must continue to trust the dynamic value, but
        lifecycle/kill-clock estimates must not interpret the sentinel as a
        deck containing no attacks.
        """

        serialized_damage = int(getattr(card, "damage", 0) or 0)
        base_damage = int(getattr(card, "base_damage", 0) or 0)
        profile_card = card
        if (
            getattr(card, "type", None) == CardType.ATTACK
            and serialized_damage < 0
            and base_damage > 0
        ):
            profile_card = copy.copy(card)
            profile_card.damage = base_damage
        return combat_predictor.card_attack_profile(
            game,
            profile_card,
            energy_override=energy_override,
        )

    @staticmethod
    def _has_usable_potion(game, potion_id):
        wanted = _token(potion_id)
        getter = getattr(game, "get_real_potions", None)
        try:
            potions = list(getter()) if callable(getter) else []
        except (AttributeError, TypeError):
            potions = []
        if not potions:
            potions = list(getattr(game, "potions", []) or [])
        return any(
            _token(getattr(potion, "potion_id", "")) == wanted
            and bool(getattr(potion, "can_use", True))
            for potion in potions
        )

    def _combat_progress_profile(self, game):
        """Estimate a bounded kill clock from live HP and the cycling deck.

        This is deliberately a discounted strategic horizon, not a second
        combat simulator.  It exists to price effects whose downside repeats
        after the exact one-turn beam has ended.
        """

        monsters = combat_predictor.active_monsters(game)
        if not monsters:
            monsters = combat_predictor.living_monsters(game)
        effective_hp = sum(
            max(0, int(getattr(monster, "current_hp", 0) or 0))
            + max(0, int(getattr(monster, "block", 0) or 0))
            for monster in monsters
        )
        # Phase-one Awakened One is not the end of the encounter.  Rebirth
        # restores a full second health bar, so pricing a persistent setup
        # from only the currently visible HP makes every long-horizon Power
        # look least useful immediately before the phase transition.  Keep
        # this horizon estimate conservative and phase-local: the exact
        # one-turn branch still owns the knockdown/rebirth transition.
        effective_hp += sum(
            max(0, int(getattr(monster, "max_hp", 0) or 0))
            for monster in monsters
            if self._is_unawakened_phase(monster)
        )
        if effective_hp <= 0:
            return {"turns": 0, "damage_per_turn": 0.0, "passive_damage_per_turn": 0.0,
                    "effective_enemy_hp": 0}

        cards = self._known_combat_cards(game)
        # Use the recurring energy budget, not the energy left after this
        # hand. The same future attack profile already prices persistent setup.
        offense = self._future_attack_damage_per_turn(game)
        source_poison = sum(
            max(0, int(getattr(card, "magic_number", 0) or 0)) * 0.55
            for card in cards
            if _token(getattr(card, "card_id", "")) in self.POISON_CARDS
        )
        offense += source_poison * min(5.0, float(len(cards))) / max(1, len(cards))

        act = max(1, int(getattr(game, "act", 1) or 1))
        fallback_dpt = {1: 13.0, 2: 19.0, 3: 26.0, 4: 30.0}.get(
            act, 20.0
        )
        offense_known = bool(cards) or any(
            hasattr(game, zone) for zone in ("hand", "draw_pile", "discard_pile")
        )
        if offense_known:
            # A visible low-output (or exhausted) deck is not missing data.
            # Do not invent six damage, or an Act-based fallback attack.
            damage_per_turn = max(0.0, min(fallback_dpt * 1.5, offense * 0.72))
        else:
            damage_per_turn = fallback_dpt

        # Already-running damage needs no future draw or energy. These are
        # aggregate strategic values, never exact random-target kills.
        player = getattr(game, "player", None)
        orbs = self._occupied_orbs(game)
        lightning = sum(self._orb_passive(orb) for orb in orbs
                        if self._orb_id(orb) == "lightning")
        if orbs and self._orb_id(orbs[0]) == "lightning":
            lightning += self._orb_passive(orbs[0]) * (
                combat_predictor.power_amount(player, "Loop")
                + int(self._has_relic(game, "Cables"))
            )
        if combat_predictor.has_power(player, "Electrodynamics"):
            lightning *= len(monsters)
        passive_damage = lightning + (
            combat_predictor.power_amount(player, "Combust") * len(monsters)
        )
        # Frost is mitigation and Dark only stores damage until evoked.
        damage_per_turn += passive_damage
        poison = sum(
            combat_predictor.power_amount(monster, "Poison")
            for monster in monsters
        )
        damage_per_turn += min(18.0, poison * 0.45)
        unbounded_turns = (
            int(math.ceil(effective_hp / damage_per_turn))
            if damage_per_turn > 0 else None
        )
        turns = unbounded_turns if unbounded_turns is not None else 14
        room_type = str(getattr(game, "room_type", "") or "")
        if room_type == "MonsterRoomBoss":
            turns = max(2, min(14, turns))
        elif room_type == "MonsterRoomElite":
            turns = max(1, min(10, turns))
        else:
            turns = max(1, min(8, turns))
        return {
            "turns": turns,
            "damage_per_turn": round(damage_per_turn, 3),
            "passive_damage_per_turn": round(passive_damage, 3),
            "effective_enemy_hp": effective_hp,
            "unbounded_turns": unbounded_turns,
            "horizon_truncated": unbounded_turns is None or turns < unbounded_turns,
            "offense_source_known": offense_known,
            "semantics": "bounded_expected_progress_not_survival_proof",
        }

    def _expected_remaining_turns(self, game):
        return self._combat_progress_profile(game)["turns"]

    def delayed_damage_projection(self, game, target, amount):
        """Bound marginal Poison damage by decay and a shared fight horizon."""
        active = combat_predictor.active_monsters(game)
        if target not in active or combat_predictor.power_amount(target, "Artifact") > 0:
            return {"ticks": 0, "marginal_damage": 0.0, "reason": "blocked_or_doomed"}
        profile = self._combat_progress_profile(game)
        hp = max(0, int(getattr(target, "current_hp", 0) or 0))
        damage_share = profile["damage_per_turn"] / max(1, len(active))
        target_turns = max(1, int(math.ceil(hp / max(1.0, damage_share))))
        ticks = min(4, profile["turns"], target_turns)
        poison = combat_predictor.power_amount(target, "Poison")
        baseline = sum(max(0, poison - turn) for turn in range(ticks))
        enhanced = sum(max(0, poison + amount - turn) for turn in range(ticks))
        return {
            "ticks": ticks,
            "marginal_damage": max(0.0, min(float(hp), enhanced) - min(float(hp), baseline)),
            "fight_horizon": profile,
            "reason": "discounted_future_damage_not_current_turn_rescue",
        }

    def _future_strength_units_per_turn(self, game):
        """Return discounted attack-hit units which consume future Strength.

        Demon Form has no same-turn consumer.  Its payoff starts on the next
        player turn and depends much more on attack density/multi-hit cards
        than on a flat Power priority.  One ordinary attack hit is one unit;
        Heavy Blade contributes its real Strength multiplier.  Scaling the
        cycling deck to its recurring draw count and payable Attack energy
        keeps this a bounded horizon rather than simulating future hands.
        """

        cards = self._recurring_combat_cards(game)
        if not cards:
            return 0.0
        energy_budget = self._future_energy_budget(game)
        units = 0.0
        for known_card in cards:
            if (
                getattr(known_card, "type", None) != CardType.ATTACK
                or bool(getattr(known_card, "exhausts", False))
            ):
                continue
            raw_damage, hits = self._future_attack_profile(
                game, known_card, energy_override=energy_budget
            )
            if raw_damage <= 0 or hits <= 0:
                continue
            multiplier = 1
            if _token(getattr(known_card, "card_id", "")) == "heavyblade":
                multiplier = max(
                    3,
                    int(getattr(known_card, "magic_number", 0) or 0),
                )
            units += max(0, int(hits or 0)) * multiplier
        cards_seen_per_turn = self._future_cards_seen_per_turn(game, cards)
        seen_ratio = cards_seen_per_turn / len(cards)
        expected_units = units * seen_ratio
        expected_attack_energy = sum(
            self._future_card_energy_cost(game, known_card, energy_budget)
            for known_card in cards
            if (
                getattr(known_card, "type", None) == CardType.ATTACK
                and not bool(getattr(known_card, "exhausts", False))
            )
        ) * seen_ratio
        if expected_attack_energy > float(energy_budget):
            expected_units *= float(energy_budget) / expected_attack_energy
        return min(6.0, expected_units)

    def _future_energy_budget(self, game):
        """Conservative recurring energy available to future attack cards."""

        permanent_energy_relics = {
            "Busted Crown", "Coffee Dripper", "Cursed Key", "Ectoplasm",
            "Fusion Hammer", "Mark of Pain", "Philosopher's Stone",
            "Runic Dome", "Sozu", "Velvet Choker",
        }
        budget = 3 + sum(
            int(self._has_relic(game, relic_id))
            for relic_id in permanent_energy_relics
        )
        if (
            getattr(game, "room_type", "")
            in {"MonsterRoomBoss", "MonsterRoomElite"}
            and self._has_relic(game, "Slaver's Collar")
        ):
            budget += 1
        if self._has_relic(game, "Nuclear Battery"):
            budget += 1
        return max(1, min(6, budget))

    def _recurring_combat_cards(self, game):
        """Cards which can actually return in later draw cycles."""

        return [
            card for card in self._known_combat_cards(game)
            if getattr(card, "type", None) != CardType.POWER
            and not bool(getattr(card, "exhausts", False))
        ]

    def _future_cards_seen_per_turn(self, game, cards=None):
        """Estimate recurring draw without treating the current hand as fate."""

        cards = list(cards if cards is not None else self._known_combat_cards(game))
        if not cards:
            return 0.0
        draw = 5
        if self._has_relic(game, "Snecko Eye"):
            draw += 2
        draw -= max(
            0,
            combat_predictor.power_amount(
                getattr(game, "player", None),
                "Draw Reduction", "DrawReductionPower",
            ),
        )
        return min(float(len(cards)), float(max(1, draw)))

    def _future_card_energy_cost(self, game, card, energy_budget=None):
        """Return an energy-realizable recurring cost for a cycling card."""

        energy_budget = (
            self._future_energy_budget(game)
            if energy_budget is None else max(1, int(energy_budget))
        )
        raw_cost = int(getattr(card, "cost", 0) or 0)
        if raw_cost < 0:
            return float(energy_budget)
        # Confusion rerolls each drawn card uniformly from zero through three.
        # Current-hand costs are authoritative for the exact beam, but using
        # one lucky roll as the recurring cost systematically distorts the
        # strategic horizon.
        if (
            self._has_relic(game, "Snecko Eye")
            or combat_predictor.has_power(
                getattr(game, "player", None), "Confusion"
            )
        ):
            return 1.5
        return float(max(0, raw_cost))

    def _future_attack_damage_per_turn(self, game):
        """Energy-feasible recurring Attack damage for DF's kill clock."""

        cards = self._recurring_combat_cards(game)
        if not cards:
            return 0.0
        energy_budget = self._future_energy_budget(game)
        seen_ratio = self._future_cards_seen_per_turn(game, cards) / len(cards)
        damage = 0.0
        energy = 0.0
        for known_card in cards:
            if getattr(known_card, "type", None) != CardType.ATTACK:
                continue
            raw_damage, _ = self._future_attack_profile(
                game, known_card, energy_override=energy_budget,
            )
            damage += max(0, raw_damage)
            energy += self._future_card_energy_cost(
                game, known_card, energy_budget
            )
        damage *= seen_ratio
        energy *= seen_ratio
        if energy > float(energy_budget):
            damage *= float(energy_budget) / energy
        return max(0.0, damage)

    def _recurring_enemy_attack_growth(self, game):
        """Return a stable lower-bound for recurring enemy attack growth.

        Ritual/Growth powers increase Strength, but the current intent's hit
        count is not a forecast of future moves.  Multiplying by that count
        made the same long fight look harmless on a BUFF frame and lethal on
        a multi-hit frame.  Charge one future attack packet per active enemy;
        this is deliberately conservative and, importantly, frame invariant.
        """

        growth = 0.0
        for monster in combat_predictor.active_monsters(game):
            per_hit = 0
            for power in getattr(monster, "powers", []) or []:
                power_id = _token(
                    getattr(power, "power_id", "")
                    or getattr(power, "name", "")
                )
                if any(marker in power_id for marker in (
                    "ritual", "strengthup", "growth",
                )):
                    per_hit += max(
                        1, int(getattr(power, "amount", 0) or 0)
                    )
            if per_hit <= 0:
                continue
            growth += per_hit
        return growth

    def _demon_form_growth_risk(self, game, triggers, growth):
        """Discount Demon Form against known recurring enemy scaling.

        This is a strategic cost, not a fabricated future-intent simulator.
        If an enemy gains ``growth`` Strength every turn, the extra pressure
        over ``triggers`` future attacks grows triangularly.  Persisted Block
        may absorb that debt when Barricade is already active; subtract only
        Block that survives the authoritative current attack.  Exact current
        survival and all card ordering remain owned by the terminal beam.
        """

        triggers = max(0, int(triggers))
        growth = max(0.0, float(growth))
        raw_debt = growth * triggers * (triggers + 1) / 2.0
        player = getattr(game, "player", None)
        persistent_block_credit = 0.0
        if player is not None and combat_predictor.has_power(
            player, "Barricade", "BarricadePower"
        ):
            current_block = max(
                0, int(getattr(player, "block", 0) or 0)
            )
            current_attack = max(0, combat_predictor.incoming_damage(game))
            persistent_block_credit = max(0.0, current_block - current_attack)
        unmitigated_debt = max(0.0, raw_debt - persistent_block_credit)
        return {
            "raw_debt": raw_debt,
            "persistent_block_credit": persistent_block_credit,
            # Future intents are unknown, so price only a discounted share.
            # The exact search still decides whether the present setup turn
            # is survivable; this term prevents known growth from being free.
            "cost": unmitigated_debt * 0.35,
        }

    def _demon_form_trigger_horizon(self, game, card, fallback_turns):
        """Discount Demon Form's future without pretending to simulate it.

        Future intents and draw order are not authoritative.  A previous
        pseudo-simulator spent the same energy on both attacks and Block,
        revived Exhaust cards every turn, and changed its answer solely with
        the current intent.  Instead cap the strategic window by room class,
        and use one energy-feasible Attack budget for both baseline damage and
        the extra damage produced by future Strength.  Survival beyond the
        exact beam is deliberately not invented: current intent does not
        describe future turns, while Barricade/Intangible can make a generic
        growth-to-HP clock equally wrong in the other direction.
        """

        raw_triggers = max(0, int(fallback_turns) - 1)
        room_type = str(getattr(game, "room_type", "") or "")
        room_cap = (
            6 if room_type == "MonsterRoomBoss"
            else 5 if room_type == "MonsterRoomElite"
            else 4
        )
        baseline_damage = self._future_attack_damage_per_turn(game)
        strength_units = self._future_strength_units_per_turn(game)
        strength_per_trigger = max(
            2 + int(getattr(card, "upgrades", 0) or 0),
            int(getattr(card, "magic_number", 0) or 0),
        )
        monsters = combat_predictor.active_monsters(game)
        if not monsters:
            monsters = combat_predictor.living_monsters(game)
        effective_hp = sum(
            max(0, int(getattr(monster, "current_hp", 0) or 0))
            + max(0, int(getattr(monster, "block", 0) or 0))
            for monster in monsters
        ) + sum(
            max(0, int(getattr(monster, "max_hp", 0) or 0))
            for monster in monsters
            if self._is_unawakened_phase(monster)
        )
        kill_clock_cap = min(raw_triggers, room_cap)
        if baseline_damage > 0 and strength_units > 0 and effective_hp > 0:
            cumulative_damage = 0.0
            for trigger in range(1, min(raw_triggers, room_cap) + 1):
                cumulative_damage += (
                    baseline_damage
                    + strength_per_trigger * trigger * strength_units
                )
                if cumulative_damage >= effective_hp:
                    kill_clock_cap = trigger
                    break
        capped = min(raw_triggers, room_cap, kill_clock_cap)
        growth = self._recurring_enemy_attack_growth(game)
        triggers = max(0, capped)
        return {
            "turns": triggers + 1,
            "triggers": triggers,
            "raw_trigger_horizon": raw_triggers,
            "room_trigger_cap": room_cap,
            "kill_clock_trigger_cap": kill_clock_cap,
            "future_attack_damage_per_turn": round(baseline_damage, 3),
            "enemy_attack_growth_per_turn": round(growth, 3),
        }

    def _future_block_plays_per_turn(self, game):
        cards = self._known_combat_cards(game)
        if not cards:
            return 1.0
        block_cards = sum(
            1
            for card in cards
            if max(0, int(getattr(card, "block", 0) or 0)) > 0
        )
        # A partial authoritative fixture may expose only the current hand.
        # Do not interpret the absence of a visible block card as a deck that
        # will never block again after Wraith Form expires.
        return max(
            0.75,
            min(2.5, 5.0 * block_cards / max(1, len(cards))),
        )

    def _forced_exhaust_resource_value(self, game, card):
        """Bound the lost future use of premium non-power resources.

        Ordinary attacks/blocks and status cleanup have no premium here.
        Exact current-turn damage, exhaust triggers and survival tiers remain
        owned by the simulator; this estimate is never counted as mitigation.
        """
        if getattr(card, "type", None) not in {CardType.ATTACK, CardType.SKILL}:
            return 0.0
        if getattr(card, "ethereal", False):
            return 0.0
        turns = max(0, self._expected_remaining_turns(game) - 1)
        if not turns:
            return 0.0
        known = self._known_combat_cards(game)
        seen = self._future_cards_seen_per_turn(game, known)
        access = min(1.0, turns * seen / max(1, len(known)))
        card_id = _token(getattr(card, "card_id", ""))
        raw_cost = int(getattr(card, "cost", 1) or 0)
        cost = 3 if raw_cost == -1 else max(1, raw_cost)
        block = max(0, int(getattr(card, "base_block", 0) or 0))
        if block <= 0 and getattr(card, "base_block", None) is None:
            block = max(0, int(getattr(card, "block", 0) or 0))
        damage, _hits = self._future_attack_profile(game, card, energy_override=3)
        # Value above a basic energy-equivalent replacement, capped at one
        # future use. Do not multiply big decks into infinite future resources.
        premium = max(0.0, block - 5.0 * cost)
        premium += 0.5 * max(0.0, damage - 6.0 * cost)
        premium += 3.0 * max(0, self.DRAW_COUNTS.get(card_id, 0))
        premium += 3.0 * max(0, self.ENERGY_VALUES.get(card_id, 0))
        premium -= max(0, self.INTRINSIC_SELF_DAMAGE.get(card_id, 0))
        return min(35.0, max(0.0, premium)) * access

    def _lifecycle_evaluation(
        self, game, card, total_loss, *, draw_pile_size=None,
        intangible_turns=None, orb_slots=None, orbs=None, artifact=None,
        resolution_copies=1,
    ):
        """Price repeated benefits and liabilities beyond the exact turn."""

        card_id = _token(getattr(card, "card_id", ""))
        if card_id not in {
            "brutality", "combust", "demonform", "echoform", "evolve",
            "wraithform", "wraithformv2", "ghostly", "apparition",
            "biasedcognition", "capacitor",
        }:
            return None

        turns = self._expected_remaining_turns(game)
        player = getattr(game, "player", None)
        hp = max(1, int(getattr(player, "current_hp", 1) or 1))
        max_hp = max(hp, int(getattr(player, "max_hp", hp) or hp))
        hp_ratio = hp / max_hp
        reserve = max(8, int(max_hp * 0.20 + 0.999))
        boss = str(getattr(game, "room_type", "") or "") == "MonsterRoomBoss"
        hp_weight = 0.55 + (1.0 - hp_ratio) * 1.4 + (0.10 if boss else 0.0)
        gambler = self._has_usable_potion(game, "GamblersBrew")
        details = {
            "kind": card_id,
            "turns": turns,
            "triggers": 0,
            "benefit": 0.0,
            "cost": 0.0,
            "adjustment": 0.0,
            "intangible_turns": 0,
            "post_intangible_turns": 0,
            "gambler_alternative": gambler,
        }

        if card_id in {"ghostly", "apparition"}:
            existing = (
                combat_predictor.power_amount(player, "Intangible", "IntangiblePlayer")
                if intangible_turns is None else intangible_turns
            )
            # Additional stacks survive the enemy-turn decrement. Exact
            # mitigation owns this turn; only credit newly covered later turns.
            covered_before = max(0, min(turns - 1, existing - 1))
            covered_after = max(0, min(turns - 1, existing + resolution_copies - 1))
            benefit = (covered_after - covered_before) * 8.0
            details.update(benefit=benefit, adjustment=benefit,
                           intangible_turns=existing + resolution_copies,
                           triggers=resolution_copies)
            return details

        if card_id == "biasedcognition":
            gain = 4 + int(getattr(card, "upgrades", 0) or 0)
            channels = self._occupied_orbs(game) if orbs is None else orbs
            affected = sum(
                _token(getattr(orb, "orb_id", "")) in {"frost", "lightning", "dark"}
                for orb in channels
            )
            # Existing orbs are certain; a real channel source gives only
            # bounded credit when the queue has not yet been established.
            affected = max(1, affected)
            artifact_layers = (
                combat_predictor.power_amount(player, "Artifact")
                if artifact is None else artifact
            )
            unprotected_copies = max(0, resolution_copies - artifact_layers)
            if unprotected_copies == 0:
                benefit, debt = 6.0, 0.0
            else:
                # Existing Bias also affects the no-play alternative. Charge
                # only the extra decay introduced by this resolution.
                changes = [
                    gain * resolution_copies - turn * unprotected_copies
                    for turn in range(1, turns)
                ]
                benefit = sum(max(0, value) for value in changes) * affected * 0.25
                debt = sum(max(0, -value) for value in changes) * affected * 0.85
            details.update(benefit=benefit, cost=debt,
                           adjustment=benefit - debt, triggers=max(0, turns - 1))
            return details

        if card_id == "capacitor":
            occupied = list(self._occupied_orbs(game) if orbs is None else orbs)
            if orb_slots is None:
                orb_slots = max(len(getattr(player, "orbs", []) or []),
                                int(getattr(player, "max_orbs", 0) or 0), 3)
            empty = max(0, orb_slots - len(occupied))
            added = max(0, min(10 - orb_slots, 2 + int(getattr(card, "upgrades", 0) or 0)))
            recurring = self._recurring_combat_cards(game)
            seen = min(1.0, self._future_cards_seen_per_turn(game, recurring) / max(1, len(recurring)))
            channels = sum(len(self._card_channels(game, known, 1)) for known in recurring)
            removals = sum(_token(getattr(known, "card_id", "")) in {"dualcast", "multicast"} for known in recurring)
            window = min(3, max(0, turns - 1))
            natural_growth = 0
            for relic in getattr(game, "relics", []) or []:
                if _token(getattr(relic, "relic_id", "")) == "inserter":
                    natural_growth += (window + max(0, int(getattr(relic, "counter", 0) or 0))) // 2
            storm_channels = max(0, combat_predictor.power_amount(player, "Storm", "StormPower"))
            needed = max(0.0, storm_channels + window * max(0, channels - removals) * seen - empty - natural_growth)
            useful = min(added, needed)
            focus = combat_predictor.signed_power_amount(player, "Focus")
            # Capacity has value only if we can fill it before drawing it
            # again. Existing empty slots and Inserter are already paid for.
            value = useful * max(1.0, 2.0 + focus) * 1.5 - (6.0 if useful < 0.5 else 0.0)
            details.update(benefit=value, adjustment=value, triggers=window,
                           empty_slots=empty, natural_growth=natural_growth,
                           useful_new_slots=round(useful, 3))
            return details

        if card_id == "evolve":
            # Deck construction already treats Evolve as a Slime Boss/status
            # answer, but the combat planner historically priced it as a
            # generic six-point Power.  That made an upgraded copy lose to a
            # second Strike even after three authoritative Slimed cards were
            # already in the discard pile (attempt 8fc20a4a, turn 2), and it
            # was still unplayed when nine Slimed cards filled the draw pile.
            #
            # Price only Status cards that already exist in future draw zones.
            # A Status currently in hand has already missed Evolve's trigger,
            # while exhausted cards cannot return.  The bounded kill-clock
            # horizon supplies the same discounted exposure used by the other
            # lifecycle powers; no future enemy move or generated card is
            # invented here.
            authoritative_draw = list(getattr(game, "draw_pile", []) or [])
            if (
                getattr(game, "draw_pile_order_known", True) is True
                and type(draw_pile_size) is int
                and 0 <= draw_pile_size <= len(authoritative_draw)
            ):
                # CommunicationMod exposes the draw-pile top at the end.
                # A deterministic earlier draw in this beam branch shrinks
                # the live prefix, so statuses already drawn before Evolve
                # resolves must not retain future-trigger value.
                authoritative_draw = authoritative_draw[:draw_pile_size]
            future_statuses = []
            seen_statuses = set()
            for known_card in (
                authoritative_draw
                + list(getattr(game, "discard_pile", []) or [])
            ):
                if getattr(known_card, "type", None) != CardType.STATUS:
                    continue
                key = getattr(known_card, "uuid", None) or id(known_card)
                if key in seen_statuses:
                    continue
                seen_statuses.add(key)
                future_statuses.append(known_card)
            cycling_cards = self._known_combat_cards(game)
            cards_seen = self._future_cards_seen_per_turn(
                game, cycling_cards
            )
            future_draw_capacity = max(0.0, (turns - 1) * cards_seen)
            exposure = min(
                1.0,
                future_draw_capacity / max(1.0, float(len(cycling_cards))),
            )
            triggers = min(
                len(future_statuses),
                int(len(future_statuses) * exposure + 1e-9),
            )
            serialized_magic = max(
                0, int(getattr(card, "magic_number", 0) or 0)
            )
            draws_per_trigger = max(
                1,
                serialized_magic
                if serialized_magic > 0
                else 1 + int(getattr(card, "upgrades", 0) or 0),
            )
            setup_value = 6.0
            future_draw_value = triggers * draws_per_trigger * 2.2 * 0.72
            benefit = setup_value + future_draw_value
            details.update({
                "triggers": triggers,
                "benefit": benefit,
                "cost": 0.0,
                "adjustment": benefit,
                "known_future_statuses": len(future_statuses),
                "draws_per_trigger": draws_per_trigger,
                "future_draw_capacity": round(future_draw_capacity, 3),
                "status_exposure": round(exposure, 3),
            })
            return details

        if card_id == "brutality":
            triggers = max(1, turns)
            # The exact terminal owns the first next-turn HP event.  Price
            # only later ticks here so it is not charged twice.
            future_ticks = max(0, triggers - 1)
            per_tick = 0 if self._has_relic(game, "Tungsten Rod") else 1
            future_hp_cost = future_ticks * per_tick
            draw_value = triggers * 1.0
            if triggers <= 3:
                draw_value += 0.65
            reserve_shortfall = max(
                0,
                reserve - (hp - triggers * per_tick),
            )
            strategic_cost = (
                future_hp_cost * hp_weight
                + reserve_shortfall * 1.75
                + (3.0 if gambler and total_loss < hp else 0.0)
            )
            if combat_predictor.has_power(player, "Rupture"):
                draw_value += future_ticks * 0.35
            details.update({
                "triggers": triggers,
                "benefit": draw_value,
                "cost": strategic_cost,
                "adjustment": draw_value - strategic_cost,
            })
            return details

        if card_id == "combust":
            triggers = max(1, turns)
            future_ticks = max(0, triggers - 1)
            per_tick = 0 if self._has_relic(game, "Tungsten Rod") else 1
            active_count = max(1, len(combat_predictor.active_monsters(game)))
            damage = max(5, int(getattr(card, "magic_number", 0) or 0))
            discounted_damage = future_ticks * damage * active_count * 0.40
            future_hp_cost = future_ticks * per_tick
            reserve_shortfall = max(
                0,
                reserve - (hp - triggers * per_tick),
            )
            strategic_cost = (
                future_hp_cost * hp_weight + reserve_shortfall * 1.75
            )
            if combat_predictor.has_power(player, "Rupture"):
                discounted_damage += future_ticks * 0.35
            details.update({
                "triggers": triggers,
                "benefit": discounted_damage,
                "cost": strategic_cost,
                "adjustment": discounted_damage - strategic_cost,
            })
            return details

        if card_id == "demonform":
            # The card is played during the current player turn, therefore
            # its first Strength trigger is the *next* player-turn start.
            # Never feed this value into the exact current-turn Strength
            # state; this is a discounted lifecycle-only payoff.
            horizon = self._demon_form_trigger_horizon(game, card, turns)
            turns = int(horizon["turns"])
            triggers = int(horizon["triggers"])
            details["turns"] = turns
            strength_per_trigger = max(
                2 + int(getattr(card, "upgrades", 0) or 0),
                int(getattr(card, "magic_number", 0) or 0),
            )
            strength_units = self._future_strength_units_per_turn(game)
            cumulative_strength = (
                strength_per_trigger * triggers * (triggers + 1) / 2.0
            )
            # Future damage is less certain than damage in the exact beam,
            # but a ten-turn Boss clock must comfortably beat one ordinary
            # attack turn.  The cap prevents a speculative fourteen-turn
            # horizon from overwhelming survival and phase constraints.
            future_damage = cumulative_strength * strength_units
            lifecycle_discount = 0.55 if boss else 0.36
            benefit = min(180.0, future_damage * lifecycle_discount)

            # Curiosity grants Awakened One permanent Strength for the Power.
            # The exact branch already owns its current attack increase; this
            # is only the discounted later-turn debt.  Do not apply the old
            # flat Power ban on top of this lifecycle-specific cost.
            curiosity = max(
                (
                    combat_predictor.power_amount(
                        monster, "Curiosity", "CuriosityPower"
                    )
                    for monster in combat_predictor.active_monsters(game)
                    if _token(getattr(monster, "monster_id", ""))
                    == "awakenedone"
                ),
                default=0,
            )
            curiosity_cost = max(0, curiosity) * triggers * 1.2
            no_payoff_cost = (
                6.0 if triggers <= 0 or strength_units <= 0 else 0.0
            )
            growth_risk = self._demon_form_growth_risk(
                game,
                triggers,
                horizon["enemy_attack_growth_per_turn"],
            )
            growth_cost = float(growth_risk["cost"])
            strategic_cost = curiosity_cost + no_payoff_cost + growth_cost
            details.update({
                "triggers": triggers,
                "benefit": benefit,
                "cost": strategic_cost,
                "adjustment": benefit - strategic_cost,
                "strength_per_trigger": strength_per_trigger,
                "strength_units_per_turn": strength_units,
                "curiosity_cost": curiosity_cost,
                "enemy_growth_cost": growth_cost,
                "enemy_growth_raw_debt": growth_risk["raw_debt"],
                "persistent_block_credit": growth_risk[
                    "persistent_block_credit"
                ],
                "raw_trigger_horizon": horizon["raw_trigger_horizon"],
                "room_trigger_cap": horizon["room_trigger_cap"],
                "kill_clock_trigger_cap": horizon[
                    "kill_clock_trigger_cap"
                ],
                "enemy_attack_growth_per_turn": horizon[
                    "enemy_attack_growth_per_turn"
                ],
                "future_attack_damage_per_turn": horizon[
                    "future_attack_damage_per_turn"
                ],
            })
            return details

        if card_id == "echoform":
            # Echo Form has no same-turn payoff. Its value is the first
            # playable card copied on each later player turn, which the exact
            # one-turn beam cannot observe. Estimate that copy from the
            # authoritative cycling deck, discount it by draw exposure, and
            # cap the horizon by room class.
            triggers = max(0, turns - 1)
            room_type = str(getattr(game, "room_type", "") or "")
            room_cap = (
                6 if room_type == "MonsterRoomBoss"
                else 5 if room_type == "MonsterRoomElite"
                else 4
            )
            triggers = min(triggers, room_cap)
            recurring_cards = self._recurring_combat_cards(game)
            energy_budget = self._future_energy_budget(game)
            copy_payloads = []
            for known_card in recurring_cards:
                known_type = getattr(known_card, "type", None)
                if known_type in {CardType.STATUS, CardType.CURSE}:
                    continue
                known_cost = self._future_card_energy_cost(
                    game, known_card, energy_budget
                )
                if known_cost > energy_budget:
                    continue
                known_id = _token(getattr(known_card, "card_id", ""))
                payload = 0.0
                if known_type == CardType.ATTACK:
                    raw_damage, _ = self._future_attack_profile(
                        game, known_card, energy_override=energy_budget
                    )
                    payload += max(0, raw_damage)
                payload += max(
                    0,
                    int(getattr(known_card, "block", 0) or 0),
                    int(getattr(known_card, "base_block", 0) or 0),
                )
                if known_id in self.POISON_CARDS:
                    payload += max(
                        0, int(getattr(known_card, "magic_number", 0) or 0)
                    ) * 0.55
                payload += self._card_draw_count(known_card, game) * 1.6
                # Duplicated channel cards create another orb, not merely
                # another six-point attack. Include its discounted lifetime.
                for orb_id in self._card_channels(game, known_card, 1):
                    focus = combat_predictor.signed_power_amount(player, "Focus")
                    if orb_id in {"lightning", "frost", "dark"}:
                        payload += max(0, (2 if orb_id == "frost" else 3) + focus) * 2.0
                if payload > 0:
                    copy_payloads.append(min(18.0, payload))

            expected_copy_payload = 0.0
            draw_exposure = 0.0
            if copy_payloads and recurring_cards:
                draw_exposure = min(
                    1.0,
                    self._future_cards_seen_per_turn(game, recurring_cards)
                    / max(1.0, float(len(recurring_cards))),
                )
                # Lead with the best *drawn* payload. Averaging over all
                # cards incorrectly assumes Echo must copy a random card.
                unseen_probability = 1.0
                for payload in sorted(copy_payloads, reverse=True):
                    expected_copy_payload += payload * draw_exposure * unseen_probability
                    unseen_probability *= 1.0 - draw_exposure

            lifecycle_discount = (
                0.55 if boss
                else 0.50 if room_type == "MonsterRoomElite"
                else 0.45
            )
            setup_value = (
                6.0
                if triggers > 0 and expected_copy_payload > 0
                else 0.0
            )
            benefit = min(
                120.0,
                setup_value
                + triggers * expected_copy_payload * lifecycle_discount,
            )
            curiosity = max(
                (
                    combat_predictor.power_amount(
                        monster, "Curiosity", "CuriosityPower"
                    )
                    for monster in combat_predictor.active_monsters(game)
                    if _token(getattr(monster, "monster_id", ""))
                    == "awakenedone"
                ),
                default=0,
            )
            curiosity_cost = max(0, curiosity) * triggers * 1.2
            no_payoff_cost = (
                6.0
                if triggers <= 0 or expected_copy_payload <= 0
                else 0.0
            )
            growth_risk = self._demon_form_growth_risk(
                game, triggers, self._recurring_enemy_attack_growth(game)
            )
            growth_cost = float(growth_risk["cost"])
            strategic_cost = curiosity_cost + no_payoff_cost + growth_cost
            details.update({
                "turns": triggers + 1,
                "triggers": triggers,
                "benefit": benefit,
                "cost": strategic_cost,
                "adjustment": benefit - strategic_cost,
                "expected_copy_payload": round(expected_copy_payload, 3),
                "copy_payload_candidates": len(copy_payloads),
                "draw_exposure": round(draw_exposure, 3),
                "setup_value": setup_value,
                "room_trigger_cap": room_cap,
                "curiosity_cost": curiosity_cost,
                "enemy_growth_cost": growth_cost,
                "enemy_growth_raw_debt": growth_risk["raw_debt"],
                "persistent_block_credit": growth_risk[
                    "persistent_block_credit"
                ],
            })
            return details

        intangible_turns = max(
            1,
            int(getattr(card, "magic_number", 0) or 0)
            or (3 if card_id == "wraithformv2" else 2),
        )
        post_turns = max(0, turns - intangible_turns)
        future_covered = max(0, min(turns - 1, intangible_turns - 1))
        current_incoming = combat_predictor.incoming_damage(game)
        future_intangible_value = (
            future_covered * min(12.0, max(3.0, current_incoming * 0.55)) * 0.32
        )
        block_plays = self._future_block_plays_per_turn(game)
        average_dex_debt = min(
            6.0,
            intangible_turns + max(0, post_turns - 1) * 0.5,
        )
        dexterity_cost = post_turns * block_plays * average_dex_debt * 0.42
        wasted_current_window = 4.0 if total_loss <= 2 else 0.0
        kill_window_bonus = 12.0 if turns <= intangible_turns else 0.0
        emergency_bonus = 12.0 if total_loss >= hp else 0.0
        gambler_cost = (
            2.0
            if gambler and total_loss < hp and post_turns > 0
            else 0.0
        )
        benefit = future_intangible_value + kill_window_bonus + emergency_bonus
        cost = dexterity_cost + wasted_current_window + gambler_cost
        details.update({
            "triggers": intangible_turns,
            "benefit": benefit,
            "cost": cost,
            "adjustment": benefit - cost,
            "intangible_turns": intangible_turns,
            "post_intangible_turns": post_turns,
        })
        return details

    def _priority_bonus(self, card):
        rank = self.priorities.PLAY_PRIORITIES.get(getattr(card, "card_id", ""))
        if rank is None:
            return 0.0
        return max(0.0, 4.5 - rank / 18.0)

    @staticmethod
    def _lifecycle_trace(candidate):
        if candidate is None or not getattr(candidate, "lifecycle_kind", ""):
            return None
        return {
            "kind": candidate.lifecycle_kind,
            "expected_remaining_turns": candidate.expected_remaining_turns,
            "expected_trigger_count": candidate.expected_trigger_count,
            "expected_benefit": round(
                candidate.expected_lifecycle_benefit, 3
            ),
            "expected_cost": round(candidate.expected_lifecycle_cost, 3),
            "adjustment": round(candidate.lifecycle_adjustment, 3),
            "intangible_turns": candidate.intangible_turns,
            "post_intangible_turns": candidate.post_intangible_turns,
            "gambler_alternative": candidate.gambler_alternative,
        }

    def _fallback_respects_lifecycle(self, candidate):
        """Do not resurrect a losing long-term setup just for immediate Block.

        The ordered search owns beneficial combinations. A marginal defensive
        fallback may override negative lifecycle value only with a concrete
        combat end or an END-death -> surviving-turn transition.
        """
        entry = getattr(self, "_last_single_card_search", {}).get(id(candidate))
        terminal = entry[0] if entry is not None else {}
        liability = float(terminal.get(
            "lifecycle_liability",
            max(0.0, candidate.expected_lifecycle_cost
                - candidate.expected_lifecycle_benefit),
        ) or 0.0)
        if liability <= 0:
            return True
        initial = getattr(self, "_last_initial_search", {}) or {}
        if terminal.get("true_combat_end"):
            return True
        if initial.get("tier") == 0 and terminal.get("tier", 0) > 0:
            return True
        rejected = getattr(self, "_last_lifecycle_fallback_rejections", None)
        if rejected is not None:
            rejected[id(candidate)] = {
                "card_id": getattr(candidate.card, "card_id", None),
                "lifecycle_liability": round(liability, 3),
                "initial_tier": initial.get("tier"),
                "candidate_tier": terminal.get("tier"),
                "reason": "immediate_mitigation_does_not_override_lifecycle",
            }
        return False

    @staticmethod
    def _has_relic(game, relic_id):
        wanted = _token(relic_id)
        wanted_ids = {wanted}
        if wanted in {"papercrane", "paperkrane"}:
            wanted_ids.update({"papercrane", "paperkrane"})
        if wanted in {"snakeskull", "sneckoskull"}:
            # The base-game runtime id is ``Snake Skull`` while card/relic
            # strategy tables historically used the display name Snecko
            # Skull.  Treat both as the same poison trigger.
            wanted_ids.update({"snakeskull", "sneckoskull"})
        return any(
            _token(getattr(relic, "relic_id", "")) in wanted_ids
            for relic in getattr(game, "relics", []) or []
        )

    @classmethod
    def _self_forming_clay_value(cls, game, hp_loss_events):
        """Return bounded next-turn value from exact HP-loss events."""

        if not cls._has_relic(game, "Self Forming Clay"):
            return 0, 0, 0.0
        events = max(0, int(hp_loss_events or 0))
        future_block = 3 * events
        credit = min(9, future_block) * 0.45
        return events, future_block, credit

    def _velvet_choker_remaining(
        self, game, cards_played=None, *, branch_cards_played=None
    ):
        """Return the number of card plays still legal this turn.

        Velvet Choker is a hard action constraint, not merely a draw penalty:
        the sixth card may resolve, but no seventh card can be played.  The
        authoritative count is the number of cards already confirmed by the
        bridge; search branches add their own transitions on top of it.
        """

        choker = next(
            (
                relic
                for relic in getattr(game, "relics", []) or []
                if _token(getattr(relic, "relic_id", ""))
                == "velvetchoker"
            ),
            None,
        )
        if choker is None:
            return None
        relic_counter = int(getattr(choker, "counter", -1) or 0)
        if relic_counter >= 0:
            used = relic_counter + max(
                0, int(branch_cards_played or 0)
            )
        else:
            if cards_played is None:
                cards_played = self._confirmed_card_resolutions
            used = max(0, int(cards_played or 0))
        return max(0, 6 - used)

    @staticmethod
    def _panache_trigger_count(game, cards_before, cards_after):
        """Count visible Panache countdown crossings in one search branch.

        CommunicationMod serializes Panache's remaining-card countdown, not
        its 10/14 damage payload.  Callers may therefore score these crossings
        conservatively but must not turn them into concrete HP or lethal.
        """

        player = getattr(game, "player", None)
        remaining = combat_predictor.power_amount(
            player, "Panache", "PanachePower"
        )
        before = max(0, int(cards_before or 0))
        after = max(before, int(cards_after or 0))
        if remaining < 1 or remaining > 5 or after <= before:
            return 0
        return sum(
            1
            for card_number in range(before + 1, after + 1)
            if card_number >= remaining
            and (card_number - remaining) % 5 == 0
        )

    @staticmethod
    def _reactive_damage_events(game, target, hits):
        """Return ordered damage events caused by one Attack card.

        Thorns triggers on every hit. Guardian's Sharp Hide triggers once
        after the Attack card, regardless of hit count. Player Intangible
        caps each event before Buffer/Tungsten resolve it.
        """

        thorns = combat_predictor.power_amount(target, "Thorns")
        sharp_hide = combat_predictor.power_amount(
            target, "Sharp Hide", "SharpHidePower"
        )
        thorns_events = []
        if thorns > 0:
            thorns_events.extend(
                thorns for _ in range(max(0, int(hits or 0)))
            )
        sharp_hide_events = (sharp_hide,) if sharp_hide > 0 else ()
        return tuple(thorns_events), sharp_hide_events

    @classmethod
    def _card_reactive_damage_events(
        cls, game, card, target, hits, active_targets=None
    ):
        """Return conservative reactive events for one complete Attack card.

        Targeted cards trigger only their bound enemy. AOE cards trigger every
        living target. Sword Boomerang is random when several enemies live, so
        it cannot claim deterministic damage; for survival it must still price
        every hit against the largest currently reachable Thorns stack.
        """

        if getattr(card, "type", None) != CardType.ATTACK:
            return (), ()
        if target is not None:
            return cls._reactive_damage_events(game, target, hits)

        targets = list(
            active_targets
            if active_targets is not None
            else combat_predictor.active_monsters(game)
        )
        targets = [
            monster for monster in targets
            if int(getattr(monster, "current_hp", 0) or 0) > 0
            and not getattr(monster, "is_gone", False)
            and not getattr(monster, "half_dead", False)
        ]
        if not targets:
            return (), ()

        card_id = _token(getattr(card, "card_id", ""))
        if card_id in {"swordboomerang", "ripandtear"}:
            highest_thorns = max(
                combat_predictor.power_amount(monster, "Thorns")
                for monster in targets
            )
            highest_sharp_hide = max(
                combat_predictor.power_amount(
                    monster, "Sharp Hide", "SharpHidePower"
                )
                for monster in targets
            )
            return (
                tuple(
                    highest_thorns
                    for _ in range(max(0, int(hits or 0)))
                    if highest_thorns > 0
                ),
                (highest_sharp_hide,) if highest_sharp_hide > 0 else (),
            )

        thorns_events = []
        sharp_hide_events = []
        for monster in targets:
            thorns, sharp_hide = cls._reactive_damage_events(
                game, monster, hits
            )
            thorns_events.extend(thorns)
            sharp_hide_events.extend(sharp_hide)
        return tuple(thorns_events), tuple(sharp_hide_events)

    @staticmethod
    def _card_self_damage_events(game, card):
        card_id = _token(getattr(card, "card_id", ""))
        pain_events = tuple(
            1
            for hand_card in getattr(game, "hand", []) or []
            if hand_card is not card
            and _token(getattr(hand_card, "card_id", "")) == "pain"
        )
        blue_candle_events = (
            (1,)
            if getattr(card, "type", None) == CardType.CURSE
            and any(
                _token(getattr(relic, "relic_id", "")) == "bluecandle"
                for relic in getattr(game, "relics", []) or []
            )
            else ()
        )
        card_damage = FastCombatPlanner.INTRINSIC_SELF_DAMAGE.get(card_id, 0)
        if card_damage <= 0:
            return pain_events + blue_candle_events
        return pain_events + blue_candle_events + (card_damage,)

    @staticmethod
    def _reactive_attrition_weight(game, player_hp):
        max_hp = max(
            1,
            int(
                getattr(getattr(game, "player", None), "max_hp", player_hp)
                or player_hp
            ),
        )
        return 2.0 if player_hp <= max(24, int(max_hp * 0.5)) else 1.0

    def _low_efficiency_self_damage_penalty(
        self,
        game,
        card,
        candidate,
        *,
        hp_damage,
        self_hp_cost,
        killed,
        neutralized,
        generated_block,
        hand_additions,
        energy_gain,
        secondary_damage,
        targets,
    ):
        """Conservatively demote an optional poor HP-for-HP exchange.

        This is a dominance *gate*, not a branch prune.  A later card may make
        block stripped by this attack useful, so the ordered state remains
        expandable; the penalty merely prevents the low-efficiency attack by
        itself from beating END.  Broad tactical and engine exceptions keep
        kills, intent cancellation, mitigation, permanent effects, and
        reliable self-damage/attack triggers available.
        """

        if (
            getattr(card, "type", None) != CardType.ATTACK
            or self_hp_cost <= 0
            or hp_damage > self_hp_cost
        ):
            return 0.0
        if (
            killed
            or neutralized
            or candidate.kills
            or candidate.intrinsic_mitigation > 0
            or generated_block > 0
            or hand_additions > 0
            or energy_gain > 0
            or candidate.healing_gain > 0
            or secondary_damage > 0
        ):
            return 0.0

        card_id = _token(getattr(card, "card_id", ""))
        if card_id in (
            self.WEAK_CARDS
            | self.STRENGTH_DOWN_CARDS
            | self.VULNERABLE_CARDS
            | self.POISON_CARDS
            | {"choke", "corpseexplosion"}
        ):
            return 0.0
        if any(
            self._monster_scaling_pressure(target, game) > 0
            for target in targets
            if target is not None
        ):
            return 0.0

        player = getattr(game, "player", None)
        if combat_predictor.has_power(
            player, "Rupture", "Rage", "After Image", "AfterImagePower"
        ):
            return 0.0
        # These either reward losing HP or make playing an Attack/card advance
        # a reliable persistent counter.  Skipping the gate is deliberately
        # conservative; their exact marginal value remains in normal scoring.
        synergy_relics = {
            "centennial puzzle",
            "ink bottle",
            "kunai",
            "nunchaku",
            "ornamental fan",
            "pen nib",
            "red skull",
            "runic cube",
            "shuriken",
        }
        if any(
            _token(getattr(relic, "relic_id", "")) in synergy_relics
            for relic in getattr(game, "relics", []) or []
        ):
            return 0.0

        return 0.5 + max(0, self_hp_cost - hp_damage)

    def _non_orichalcum_end_turn_block(self, game, orbs=None, generated=0):
        player = getattr(game, "player", None)
        existing = max(0, int(getattr(player, "block", 0) or 0))
        if not combat_predictor.can_gain_block(player):
            return existing
        occupied = self._occupied_orbs(game) if orbs is None else orbs
        return (
            existing
            + combat_predictor.power_amount(player, "Metallicize")
            + combat_predictor.power_amount(
                player, "Plated Armor", "PlatedArmor"
            )
            + sum(
                self._orb_passive(orb)
                for orb in occupied
                if self._orb_id(orb) == "frost"
            )
            + max(0, int(generated or 0))
        )

    @staticmethod
    def _x_effect(
        game, card, upgraded_bonus=False, energy_override=None
    ):
        return combat_predictor.x_cost_effect(
            game,
            card,
            energy_override=energy_override,
            upgraded_bonus=upgraded_bonus,
        )

    @staticmethod
    def _occupied_orbs(game):
        return [
            orb
            for orb in getattr(getattr(game, "player", None), "orbs", []) or []
            if _token(getattr(orb, "orb_id", "")) not in {"", "empty"}
        ]

    @staticmethod
    def _orb_id(orb):
        return _token(getattr(orb, "orb_id", ""))

    @staticmethod
    def _orb_evoke(orb):
        return max(0, int(getattr(orb, "evoke_amount", 0) or 0))

    @staticmethod
    def _orb_passive(orb):
        return max(0, int(getattr(orb, "passive_amount", 0) or 0))

    @staticmethod
    def _adjust_orb_focus(orbs, focus_delta):
        """Apply a same-turn Focus change to already-channelled orbs."""

        delta = int(focus_delta or 0)
        if delta == 0:
            return tuple(orbs)
        adjusted = []
        for orb in orbs:
            orb_id = _token(getattr(orb, "orb_id", ""))
            passive = max(0, int(getattr(orb, "passive_amount", 0) or 0))
            evoke = max(0, int(getattr(orb, "evoke_amount", 0) or 0))
            if orb_id != "plasma":
                passive = max(0, passive + delta)
                evoke = max(0, evoke + delta)
            adjusted.append(_ProjectedOrb(orb_id, passive, evoke))
        return tuple(adjusted)

    def _projected_orb(self, game, orb_id, *, focus_override=None):
        focus = (
            combat_predictor.signed_power_amount(
                getattr(game, "player", None), "Focus"
            )
            if focus_override is None
            else int(focus_override or 0)
        )
        base = {
            "lightning": (3, 8),
            "frost": (2, 5),
            "dark": (6, 6),
            "plasma": (1, 2),
        }.get(orb_id, (0, 0))
        passive, evoke = base
        if orb_id != "plasma":
            passive = max(0, passive + focus)
            evoke = max(0, evoke + focus)
        return _ProjectedOrb(orb_id, passive, evoke)

    def _card_channels(
        self,
        game,
        card,
        living_count,
        *,
        resolution_copies=1,
    ):
        card_id = _token(getattr(card, "card_id", card))
        copies = max(1, int(resolution_copies or 1))
        if card_id == "rainbow":
            return ("lightning", "frost", "dark") * copies
        if card_id == "electrodynamics":
            count = 2 + int(getattr(card, "upgrades", 0) or 0)
            return ("lightning",) * count * copies
        orb_id, count = {
            "zap": ("lightning", 1),
            "balllightning": ("lightning", 1),
            "coldsnap": ("frost", 1),
            "coolheaded": ("frost", 1),
            "glacier": ("frost", 2),
            "chill": ("frost", max(0, living_count)),
            "doomandgloom": ("dark", 1),
            "fusion": ("plasma", 1),
        }.get(card_id, (None, 0))
        return (orb_id,) * count * copies if orb_id else ()

    def _advance_channelled_orbs(
        self, game, orbs, orb_slots, card, living_count,
        *, focus_override=None, resolution_copies=1, extra_orb_ids=()
    ):
        """Return the final queue and every orb evoked by channel overflow.

        Evocation effects are intentionally resolved by the ordered turn
        transition, where enemy HP/Block, player Block, and branch energy are
        available together.  Returning the concrete evoked orbs keeps all
        channel cards on that one path instead of special-casing only Frost.
        """

        orb_ids = self._card_channels(
            game,
            card,
            living_count,
            resolution_copies=resolution_copies,
        ) + tuple(extra_orb_ids or ())
        return self._advance_orb_channels(
            game,
            orbs,
            orb_slots,
            orb_ids,
            focus_override=focus_override,
        )

    def _advance_orb_channels(
        self, game, orbs, orb_slots, orb_ids, *, focus_override=None
    ):
        """Advance explicit orb ids through the shared overflow queue.

        Card-native channels, Storm, and future deterministic channel
        triggers must all use this primitive so Frost/Plasma resources and
        Lightning/Dark damage are resolved from one ordered queue.
        """

        advanced = list(orbs)
        evoked_orbs = []
        for orb_id in tuple(orb_ids or ()):
            new_orb = self._projected_orb(
                game, orb_id, focus_override=focus_override
            )
            if len(advanced) >= max(1, orb_slots):
                evoked_orbs.append(advanced.pop(0))
            advanced.append(new_orb)
        return tuple(advanced), tuple(evoked_orbs)

    @staticmethod
    def _static_discharge_layers(game):
        return combat_predictor.power_amount(
            getattr(game, "player", None),
            "StaticDischarge",
            "Static Discharge",
            "StaticDischargePower",
        )

    @staticmethod
    def _writhing_mass_has_compulsive(monster):
        return (
            _token(getattr(monster, "monster_id", "")) == "writhingmass"
            and combat_predictor.has_power(
                monster,
                "Compulsive",
                "Reactive",
                "ReactivePower",
            )
        )

    @staticmethod
    def _writhing_mass_risk_packet(game, monster):
        """Return a worst-case envelope, never a guessed rerolled intent."""

        ascension = max(
            0,
            int(
                getattr(
                    game,
                    "ascension_level",
                    getattr(game, "ascension", 0),
                )
                or 0
            ),
        )
        # Base-game bytecode defines BIG_HIT as 32 below A2 and 38 at A2+;
        # it is the largest possible immediate HP packet after Compulsive.
        # Existing Strength is state, not a guess about the rerolled move.
        amount = (38 if ascension >= 2 else 32) + max(
            0,
            combat_predictor.signed_power_amount(
                monster, "Strength", "StrengthPower"
            ),
        )
        return combat_predictor.DamagePacket(
            "reactive_intent_envelope",
            amount,
            1,
            True,
            True,
        )

    def _writhing_mass_fallback_attack_is_safe(
        self, game, card, target=None, *, extra_block=0,
    ):
        """Fail closed when an unsearched fallback can reroll a lethal move.

        The ordered beam models Compulsive and re-plans after the real frame
        refreshes.  Several narrow progress/resource fallbacks deliberately
        select a card outside that winning beam, though, so their cached
        search describes END or a different card.  Before any such Attack is
        admitted, replace every surviving Writhing Mass move with the same
        conservative encounter envelope used by the beam.  Exact lethal and
        zero-HP-damage attacks do not trigger Compulsive and remain legal.
        """

        if getattr(card, "type", None) != CardType.ATTACK:
            return True
        active = list(combat_predictor.active_monsters(game))
        if target is not None:
            affected = [target]
        else:
            # Targetless attacks are AOE or have hidden/random targets.  A
            # fallback cannot prove that a living Mass is missed, so include
            # every active one in the conservative envelope.
            affected = active
        reactive = []
        for monster in affected:
            if not self._writhing_mass_has_compulsive(monster):
                continue
            dealt = combat_predictor.card_attack_hp_loss(
                game, card, monster, target=target,
            )
            if dealt <= 0:
                continue
            remaining_hp = max(
                0,
                int(getattr(monster, "current_hp", 0) or 0) - int(dealt),
            )
            if self._is_fatal_kill(game, monster, remaining_hp):
                continue
            reactive.append(monster)
        if not reactive:
            return True

        outcome = combat_predictor.projected_turn_outcome(
            game,
            extra_block=max(0, int(extra_block or 0)),
            damage_packet_overrides={
                id(monster): (self._writhing_mass_risk_packet(game, monster),)
                for monster in reactive
            },
        )
        player_hp = max(
            1, int(getattr(getattr(game, "player", None), "current_hp", 1) or 1)
        )
        final_hp = outcome.final_player_hp
        if final_hp is None:
            final_hp = max(0, player_hp - int(outcome.total_hp_loss or 0))
        return bool(
            outcome.player_survives is not False
            and not outcome.fairy_revive_consumed
            and int(final_hp) > 0
            and int(final_hp) >= self._reactive_safety_reserve(game, player_hp)
        )

    def _projected_turn_outcome_with_attack_reactions(
        self,
        game,
        monsters,
        hp,
        enemy_block,
        corpse,
        damage_value,
        mode_shift_remaining,
        neutralized,
        orbs,
        orb_slots,
        focus_bonus,
        electrodynamics,
        static_discharge_layers,
        thorns_layers,
        flame_barrier_layers,
        lock_on,
        turn_outcome_kwargs,
        *,
        extra_combust_hp_loss=0,
        extra_brutality_amount=0,
    ):
        """Resolve enemy healing and player reactions between Attack hits.

        ``combat_predictor`` remains the owner of Intangible -> Block ->
        Buffer -> Torii -> Tungsten ordering and decides which declared hits
        trigger Bronze Scales/Thorns.  This wrapper advances their concrete
        enemy damage plus Static Discharge's branch-local channel/evoke state,
        so a true reaction kill can stop later DamageActions and telemetry is
        derived from the same state used to rank the action.
        """

        local_hp = list(hp)
        local_block = list(enemy_block)
        local_corpse = list(corpse)
        local_damage_value = list(damage_value)
        local_mode_shift = list(mode_shift_remaining)
        local_neutralized = set(neutralized)
        local_orbs = list(orbs)
        layers = max(0, int(static_discharge_layers or 0))
        player_thorns = max(0, int(thorns_layers or 0))
        player_flame_barrier = max(
            0, int(flame_barrier_layers or 0)
        )
        painful_stabs_present = any(
            combat_predictor.has_power(
                monster,
                "Painful Stabs",
                "PainfulStabs",
                "PainfulStabsPower",
            )
            for monster in monsters
        )
        attack_healing_present = any(
            combat_predictor.monster_attack_healing(monster, 1) > 0
            for monster in monsters
        )
        trace = {
            "static_discharge_layers": int(layers),
            "static_discharge_triggered_hits": 0,
            "static_discharge_channels": 0,
            "static_discharge_overflow_evokes": 0,
            "static_discharge_frost_block": 0,
            "static_discharge_plasma_energy_during_enemy_turn": 0,
            "static_discharge_evoke_damage_value": 0.0,
            "static_discharge_random_target_damage_value": 0.0,
            "static_discharge_random_target_evokes": 0,
            "static_discharge_zero_hp_loss_triggers": 0,
            "static_discharge_stopped_attacker_indexes": [],
            "enemy_reaction_damage_events": 0,
            "enemy_reaction_direct_damage": 0,
            "enemy_reaction_total_damage": 0,
            "enemy_attack_healing_events": 0,
            "enemy_attack_healing_requested": 0,
            "enemy_attack_healing": 0,
            "bronze_scales_reaction_damage": 0,
            "player_thorns_reaction_damage": 0,
            "enemy_reaction_stopped_attacker_indexes": [],
            "painful_stabs_wounds": 0,
            "post_death_reactions_suppressed": 0,
            "player_end_turn_healing_before_attacks": 0,
        }
        if (
            layers <= 0
            and player_thorns <= 0
            and player_flame_barrier <= 0
            and not painful_stabs_present
            and not attack_healing_present
        ):
            outcome = combat_predictor.projected_turn_outcome(
                game,
                **turn_outcome_kwargs,
                extra_combust_hp_loss=extra_combust_hp_loss,
                extra_brutality_amount=extra_brutality_amount,
            )
            trace["player_end_turn_healing_before_attacks"] = outcome.end_turn_healing
            return (
                outcome,
                tuple(local_hp),
                tuple(local_block),
                tuple(local_corpse),
                tuple(local_damage_value),
                tuple(local_mode_shift),
                frozenset(local_neutralized),
                tuple(local_orbs),
                trace,
            )

        index_by_id = {
            id(monster): index for index, monster in enumerate(monsters)
        }
        focus = (
            combat_predictor.signed_power_amount(game.player, "Focus")
            + int(focus_bonus or 0)
        )
        can_gain_block = combat_predictor.can_gain_block(game)
        stopped = set()
        reflection_stopped = set()
        # The predictor owns the chronological health ledger, including Fairy
        # death replacement. Reactions observe it rather than recomputing HP.
        player_hp_remaining = 0

        def observe_player_health(hp):
            nonlocal player_hp_remaining
            player_hp_remaining = hp

        def attacker_is_active(monster):
            index = index_by_id.get(id(monster))
            return index is None or local_hp[index] > 0

        def on_enemy_attack_healing(monster, requested_healing):
            """Advance Suck healing on the planner's branch-local HP."""

            index = index_by_id.get(id(monster))
            requested = max(0, int(requested_healing or 0))
            if index is None or requested <= 0 or local_hp[index] <= 0:
                return 0
            trace["enemy_attack_healing_requested"] += requested
            monster_max_hp = max(
                local_hp[index],
                int(
                    getattr(monster, "max_hp", local_hp[index])
                    or local_hp[index]
                ),
            )
            applied = min(
                requested,
                max(0, monster_max_hp - local_hp[index]),
            )
            if applied > 0:
                local_hp[index] += applied
                trace["enemy_attack_healing_events"] += 1
                trace["enemy_attack_healing"] += applied
            return applied

        def on_enemy_reaction(monster, amount, source):
            nonlocal local_hp, local_block, local_damage_value

            if player_hp_remaining <= 0:
                trace["post_death_reactions_suppressed"] += 1
                return combat_predictor.EnemyReactionOutcome(False)
            index = index_by_id.get(id(monster))
            if index is None:
                return combat_predictor.EnemyReactionOutcome(False)
            if local_hp[index] <= 0:
                return combat_predictor.EnemyReactionOutcome(True)
            trace["enemy_reaction_damage_events"] += 1
            hp_before = tuple(local_hp)
            dealt = self._apply_enemy_damage_packet(
                monster,
                index,
                local_hp,
                local_block,
                local_damage_value,
                local_mode_shift,
                local_neutralized,
                amount,
                # THORNS damage ignores Strength/Weak/Vulnerable, but it is
                # still ordinary damage for the defender's current Block.
                blockable=True,
            )
            trace["enemy_reaction_direct_damage"] += dealt
            source_key = (
                "bronze_scales_reaction_damage"
                if source == "bronze_scales"
                else "player_thorns_reaction_damage"
            )
            trace[source_key] += dealt
            local_hp, local_block, local_damage_value = (
                self._resolve_state_deaths(
                    game,
                    monsters,
                    local_hp,
                    local_block,
                    local_corpse,
                    local_damage_value,
                    mode_shift_remaining=local_mode_shift,
                    neutralized=local_neutralized,
                )
            )
            local_hp = list(local_hp)
            local_block = list(local_block)
            local_damage_value = list(local_damage_value)
            trace["enemy_reaction_total_damage"] += sum(
                max(0, int(before or 0) - int(after or 0))
                for before, after in zip(hp_before, local_hp)
            )
            stop_attacker = local_hp[index] <= 0
            if stop_attacker:
                reflection_stopped.add(index)
            newly_killed = tuple(
                candidate
                for candidate_index, candidate in enumerate(monsters)
                if int(hp_before[candidate_index] or 0) > 0
                and self._is_true_death_at_hp(
                    candidate,
                    local_hp[candidate_index],
                    monsters,
                    local_hp,
                )
            )
            return combat_predictor.EnemyReactionOutcome(
                stop_attacker=stop_attacker,
                killed_monsters=newly_killed,
            )

        def on_enemy_pre_attack_block(monster, amount):
            index = index_by_id.get(id(monster))
            if index is None or local_hp[index] <= 0:
                return
            local_block[index] += max(0, int(amount or 0))

        def on_attack_hit(monster, reaction_damage, final_hp_damage):
            nonlocal local_hp, local_block, local_damage_value
            if player_hp_remaining <= 0:
                trace["post_death_reactions_suppressed"] += 1
                return combat_predictor.AttackHitReaction()

            if (
                final_hp_damage > 0
                and combat_predictor.has_power(
                    monster,
                    "Painful Stabs",
                    "PainfulStabs",
                    "PainfulStabsPower",
                )
            ):
                trace["painful_stabs_wounds"] += 1
            if layers > 0:
                trace["static_discharge_triggered_hits"] += 1
                if final_hp_damage <= 0:
                    trace[
                        "static_discharge_zero_hp_loss_triggers"
                    ] += 1
            gained_block = 0
            hp_before_reaction = tuple(local_hp)
            for _ in range(layers):
                trace["static_discharge_channels"] += 1
                evoked = None
                if len(local_orbs) >= max(1, int(orb_slots or 0)):
                    evoked = local_orbs.pop(0)
                    trace["static_discharge_overflow_evokes"] += 1
                local_orbs.append(
                    self._projected_orb(
                        game, "lightning", focus_override=focus
                    )
                )
                if evoked is None:
                    continue
                evoked_id = self._orb_id(evoked)
                if evoked_id == "frost":
                    if can_gain_block:
                        amount = self._orb_evoke(evoked)
                        gained_block += amount
                        trace["static_discharge_frost_block"] += amount
                    continue
                if evoked_id == "plasma":
                    # Energy is real but arrives during the enemy turn and is
                    # reset before the next player decision.  Keep it only as
                    # an audit value; never spend it in the current beam.
                    trace[
                        "static_discharge_plasma_energy_during_enemy_turn"
                    ] += self._orb_evoke(evoked)
                    continue
                if evoked_id not in {"lightning", "dark"}:
                    continue
                alive_before = [
                    index
                    for index, value in enumerate(local_hp)
                    if value > 0
                ]
                if (
                    evoked_id == "lightning"
                    and len(alive_before) > 1
                    and not electrodynamics
                ):
                    trace["static_discharge_random_target_evokes"] += 1
                    random_target = True
                else:
                    random_target = False
                (
                    local_hp,
                    local_block,
                    local_damage_value,
                    evoke_value,
                    _,
                ) = self._resolve_damage_orb_evokes(
                    game,
                    monsters,
                    local_hp,
                    local_block,
                    local_corpse,
                    local_damage_value,
                    local_mode_shift,
                    local_neutralized,
                    evoked,
                    1,
                    electrodynamics=electrodynamics,
                    lock_on=lock_on,
                )
                trace["static_discharge_evoke_damage_value"] += evoke_value
                if random_target:
                    trace[
                        "static_discharge_random_target_damage_value"
                    ] += evoke_value

            index = index_by_id.get(id(monster))
            stop_attacker = index is not None and local_hp[index] <= 0
            if stop_attacker:
                stopped.add(index)
            newly_killed = tuple(
                candidate
                for candidate_index, candidate in enumerate(monsters)
                if int(hp_before_reaction[candidate_index] or 0) > 0
                and self._is_true_death_at_hp(
                    candidate,
                    local_hp[candidate_index],
                    monsters,
                    local_hp,
                )
            )
            return combat_predictor.AttackHitReaction(
                block_gain=gained_block,
                stop_attacker=stop_attacker,
                killed_monsters=newly_killed,
            )

        outcome = combat_predictor.projected_turn_outcome(
            game,
            **turn_outcome_kwargs,
            extra_combust_hp_loss=extra_combust_hp_loss,
            extra_brutality_amount=extra_brutality_amount,
            enemy_attack_healing=(
                on_enemy_attack_healing
                if attack_healing_present
                else None
            ),
            attack_hit_reaction=(
                on_attack_hit
                if (
                    layers > 0
                    or player_thorns > 0
                    or player_flame_barrier > 0
                    or painful_stabs_present
                )
                else None
            ),
            attacker_is_active=attacker_is_active,
            enemy_reaction_damage=on_enemy_reaction,
            enemy_pre_attack_block_gain=on_enemy_pre_attack_block,
            player_thorns_override=player_thorns,
            player_flame_barrier_override=player_flame_barrier,
            combat_ended_after_attacks=lambda: self._all_truly_dead(monsters, local_hp),
            player_health_observer=observe_player_health,
        )
        trace["player_end_turn_healing_before_attacks"] = outcome.end_turn_healing
        trace["static_discharge_stopped_attacker_indexes"] = sorted(stopped)
        trace["enemy_reaction_stopped_attacker_indexes"] = sorted(
            reflection_stopped
        )
        trace["static_discharge_evoke_damage_value"] = round(
            float(trace["static_discharge_evoke_damage_value"]), 3
        )
        trace["static_discharge_random_target_damage_value"] = round(
            float(
                trace["static_discharge_random_target_damage_value"]
            ),
            3,
        )
        return (
            outcome,
            tuple(local_hp),
            tuple(local_block),
            tuple(local_corpse),
            tuple(local_damage_value),
            tuple(local_mode_shift),
            frozenset(local_neutralized),
            tuple(local_orbs),
            trace,
        )

    def _initial_channel_plasma_energy(self, game, card):
        """Energy guaranteed by a turn-start channel overflow.

        CommunicationMod marks expensive follow-ups unplayable at the current
        frame.  This narrow preview admits them to the beam when a playable
        channel card will certainly evoke a front Plasma; the branch-local
        transition remains authoritative and rejects any line that cannot
        actually pay the later cost.
        """

        serialized = list(
            getattr(getattr(game, "player", None), "orbs", []) or []
        )
        occupied = tuple(self._occupied_orbs(game))
        if not occupied:
            return 0
        _, evoked = self._advance_channelled_orbs(
            game,
            occupied,
            max(3, len(serialized), len(occupied)),
            card,
            len(combat_predictor.living_monsters(game)),
        )
        return sum(
            self._orb_evoke(orb)
            for orb in evoked
            if self._orb_id(orb) == "plasma"
        )

    def _card_draw_count(
        self, card, game=None, *, orbs=None, hand_size_before_play=None
    ):
        """Return the deterministic draw count at this exact branch state.

        Static per-card tables are insufficient for Compile Driver and
        Expertise, while Reboot's upgrade adds two cards rather than one.
        This helper deliberately returns only deterministic card-effect draw;
        conditional Dropkick/Heel Hook draw is handled beside their target
        predicate and relic/power draws are advanced by the transition.
        """

        card_id = _token(getattr(card, "card_id", ""))
        upgrades = max(0, int(getattr(card, "upgrades", 0) or 0))
        if card_id == "fission":
            if orbs is None and game is None:
                return 0
            occupied = tuple(
                self._occupied_orbs(game) if orbs is None else orbs
            )
            return len(occupied)
        if card_id == "compiledriver":
            if orbs is None and game is None:
                return 0
            occupied = tuple(
                self._occupied_orbs(game) if orbs is None else orbs
            )
            return len({
                self._orb_id(orb)
                for orb in occupied
                if self._orb_id(orb)
            })
        if card_id == "expertise":
            if hand_size_before_play is None:
                hand_size_before_play = len(
                    getattr(game, "hand", []) or []
                ) if game is not None else 0
            target_size = 7 if upgrades > 0 else 6
            after_play = max(0, int(hand_size_before_play or 0) - 1)
            return max(0, target_size - after_play)
        if card_id == "reboot":
            return 6 if upgrades > 0 else 4
        value = max(0, int(self.DRAW_COUNTS.get(card_id, 0) or 0))
        if upgrades > 0:
            value += int(self.UPGRADED_DRAW_BONUSES.get(card_id, 0) or 0)
        return max(0, value)

    @staticmethod
    def _heatsinks_layers(game):
        player = getattr(game, "player", None)
        amount = combat_predictor.power_amount(
            player,
            "Heatsink",
            "Heatsinks",
            "HeatsinkPower",
            "HeatsinksPower",
        )
        if amount <= 0 and combat_predictor.has_power(
            player,
            "Heatsink",
            "Heatsinks",
            "HeatsinkPower",
            "HeatsinksPower",
        ):
            amount = 1
        return max(0, int(amount or 0))

    def _heatsinks_draw_count(
        self, game, card, *, layers=None, draw_pile_size=None
    ):
        """Known draw caused by already-active Heat Sinks layers."""

        if getattr(card, "type", None) != CardType.POWER:
            return 0
        if layers is None:
            layers = self._heatsinks_layers(game)
        layers = max(0, int(layers or 0))
        if layers <= 0:
            return 0
        if draw_pile_size is None:
            draw_pile_size = len(
                getattr(game, "draw_pile", []) or []
            )
        draw_pile_size = int(draw_pile_size or 0)
        if draw_pile_size <= 0:
            # A discard-pile reshuffle is legal in game, but the next cards
            # are not known by this bounded search.  Fail closed and let the
            # authoritative post-action frame expose the result.
            return 0
        return min(layers, draw_pile_size)

    @staticmethod
    def _conditional_branch_resources(
        card_id, target_index, vulnerable, weak, discarded_this_turn
    ):
        """Return (draw, energy) whose predicate is already true.

        These effects inspect state *before* their own card resolves.  In
        particular, merely planning a later debuff or merely holding a
        discard card cannot unlock the resource.
        """

        if (
            card_id == "dropkick"
            and target_index is not None
            and int(vulnerable[target_index] or 0) > 0
        ):
            return 1, 1
        if (
            card_id == "heelhook"
            and target_index is not None
            and int(weak[target_index] or 0) > 0
        ):
            return 1, 1
        if card_id == "sneakystrike" and discarded_this_turn:
            return 0, 2
        return 0, 0

    def _authoritative_conditional_resources(self, game, card, target):
        card_id = _token(getattr(card, "card_id", ""))
        if card_id == "dropkick" and target is not None:
            return (
                (1, 1)
                if combat_predictor.power_amount(target, "Vulnerable") > 0
                else (0, 0)
            )
        if card_id == "heelhook" and target is not None:
            return (
                (1, 1)
                if combat_predictor.power_amount(target, "Weak") > 0
                else (0, 0)
            )
        if card_id == "sneakystrike":
            return (
                (0, 2)
                if int(
                    getattr(game, "cards_discarded_this_turn", 0) or 0
                ) > 0
                else (0, 0)
            )
        return 0, 0

    def _energy_gain(
        self,
        card,
        game=None,
        orbs=None,
        *,
        energy_override=None,
        draw_pile_size=None,
    ):
        card_id = _token(getattr(card, "card_id", ""))
        if card_id == "doubleenergy":
            if energy_override is None:
                energy_override = int(
                    getattr(getattr(game, "player", None), "energy", 0) or 0
                )
            card_cost = max(0, int(getattr(card, "cost", 0) or 0))
            # The card spends its cost before ``use`` reads the current
            # EnergyManager value and gains that same amount.
            return max(0, int(energy_override or 0) - card_cost)
        if card_id == "aggregate":
            if draw_pile_size is None:
                if game is None:
                    return 0
                draw_pile_size = len(
                    getattr(game, "draw_pile", []) or []
                )
            draw_pile_size = int(draw_pile_size or 0)
            if draw_pile_size < 0:
                return 0
            divisor = (
                3
                if int(getattr(card, "upgrades", 0) or 0) > 0
                else 4
            )
            return max(0, draw_pile_size // divisor)
        if card_id == "fission":
            if orbs is None and game is None:
                return 0
            occupied = tuple(
                self._occupied_orbs(game) if orbs is None else orbs
            )
            value = len(occupied)
            if int(getattr(card, "upgrades", 0) or 0) > 0:
                value += sum(
                    self._orb_evoke(orb)
                    for orb in occupied
                    if self._orb_id(orb) == "plasma"
                )
            return value
        if card_id in {
            "consume", "dualcast", "multicast", "recursion", "redo",
        }:
            if orbs is None and game is None:
                return 0
            occupied = tuple(
                self._occupied_orbs(game) if orbs is None else orbs
            )
            if not occupied or self._orb_id(occupied[0]) != "plasma":
                return 0
            if card_id == "dualcast":
                evoke_count = 2
            elif card_id == "multicast":
                evoke_count = self._x_effect(
                    game,
                    card,
                    upgraded_bonus=True,
                    energy_override=energy_override,
                )
            else:
                evoke_count = 1
            return self._orb_evoke(occupied[0]) * max(
                0, int(evoke_count or 0)
            )
        value = self.ENERGY_VALUES.get(card_id, 0)
        if card_id in {
            "adrenaline", "bloodletting",
            "tactician", "turbo",
        }:
            value += int(getattr(card, "upgrades", 0) or 0)
        return max(0, value)

    def _next_turn_energy_gain(self, card):
        """Return energy granted at the next turn's start.

        The live bridge serializes these cards just like ordinary Skills, so
        treating their effect as current-turn energy creates impossible
        branches (and makes a useful setup card appear to be a free combo
        enabler).  Only Outmaneuver's upgrade increases delayed energy;
        Flying Knee and Conserve Battery upgrade damage/block instead.
        """

        card_id = _token(getattr(card, "card_id", ""))
        value = self.NEXT_TURN_ENERGY_VALUES.get(card_id, 0)
        if value <= 0:
            return 0
        upgrade_bonus = (
            int(getattr(card, "upgrades", 0) or 0)
            if card_id == "outmaneuver"
            else 0
        )
        return max(0, value + upgrade_bonus)

    @staticmethod
    def _deterministic_discard_count(card, hand_size_before_play):
        card_id = _token(getattr(card, "card_id", ""))
        if card_id == "prepared":
            return 1 + int(getattr(card, "upgrades", 0) or 0)
        if card_id == "concentrate":
            return (
                2
                if int(getattr(card, "upgrades", 0) or 0) > 0
                else 3
            )
        if card_id in {"calculatedgamble", "stormofsteel"}:
            return max(0, int(hand_size_before_play or 0) - 1)
        if card_id == "alloutattack":
            return min(1, max(0, int(hand_size_before_play or 0) - 1))
        return {
            "acrobatics": 1,
            "daggerthrow": 1,
            "survivor": 1,
        }.get(card_id, 0)

    def _base_fission_opportunity_cost(self, orbs):
        """Value deterministic orb output destroyed by unupgraded Fission."""

        cost = 0.0
        for orb in orbs:
            orb_id = self._orb_id(orb)
            passive = self._orb_passive(orb)
            evoke = self._orb_evoke(orb)
            if orb_id == "dark":
                # Dark's accumulated damage lives in evoke_amount. Looking at
                # passive growth alone made a charged 50-damage orb appear
                # cheaper than one card and one energy.
                cost += max(passive * 0.5, evoke * 0.8)
            elif orb_id == "lightning":
                cost += passive * 0.8 + evoke * 0.2
            elif orb_id == "plasma":
                cost += passive * 1.6 + evoke * 0.5
            elif orb_id == "frost":
                # Immediate lost Frost block is handled by
                # _frost_evoke_mitigation; retain a small future-value cost.
                cost += passive * 0.35
        return cost

    def _weak_mitigation(self, target):
        if target is None or not getattr(target, "intent", None).is_attack():
            return 0
        if combat_predictor.power_amount(target, "Artifact") > 0:
            return 0
        if combat_predictor.power_amount(target, "Weak", "Weakened") > 0:
            return 0
        return max(1, combat_predictor.monster_threat(target) // 4)

    def _future_turn_weak_value(self, game, card, target):
        """Value Weak which protects a later turn after this one is covered.

        ``mitigation`` is intentionally based on damage that can be prevented
        during the current turn.  That is correct while the player is still
        taking damage, but it made a multi-turn debuff such as Leg Sweep look
        worthless when an earlier card had already supplied enough block.  In
        that state the card can still extend an existing Weak stack (or apply
        the first stack for the next enemy attack), which is exactly the
        buffer needed to avoid ending with energy and a hand full of future
        defense.  Keep this estimate small and only apply it to an attacker;
        ordinary Defend cards therefore remain zero-value once the current
        turn is safe.
        """

        card_id = _token(getattr(card, "card_id", ""))
        if card_id not in self.WEAK_CARDS:
            return 0.0

        targets = []
        if target is not None:
            targets = [target]
        elif card_id in self.AOE_WEAK_CARDS:
            targets = [
                monster
                for monster in combat_predictor.active_monsters(game)
                if (
                    getattr(monster, "intent", None).is_attack()
                    or _token(getattr(monster, "monster_id", ""))
                    in self.FUTURE_ATTACK_PROFILES
                )
            ]

        value = 0.0
        duration = max(1, int(getattr(card, "magic_number", 0) or 0))
        for monster in targets:
            if (
                monster is None
                or combat_predictor.power_amount(monster, "Artifact") > 0
            ):
                continue
            intent = getattr(monster, "intent", None)
            if not intent.is_attack():
                profile = self.FUTURE_ATTACK_PROFILES.get(
                    _token(getattr(monster, "monster_id", ""))
                )
                if not profile:
                    continue
                existing_weak = combat_predictor.power_amount(
                    monster, "Weak", "Weakened"
                )
                # Debuffs tick down at the end of this non-attack enemy turn.
                # Only the remaining stack can cover following attack packets.
                protected_attacks = max(0, existing_weak + duration - 1)
                for damage_per_hit, hits in profile[:protected_attacks]:
                    weakened = max(0, int(damage_per_hit * 0.75))
                    value += max(0, damage_per_hit - weakened) * max(1, hits)
                continue
            threat = max(0, combat_predictor.monster_threat(monster))
            if threat <= 0:
                continue
            existing_weak = combat_predictor.power_amount(
                monster, "Weak", "Weakened"
            )
            # A first Weak packet buys one future attack.  If Weak is already
            # present, the new duration is still useful: it prevents the
            # common one-turn gap immediately after the current block expires.
            multiplier = 0.85 if existing_weak <= 0 else min(
                1.45, 0.65 + duration * 0.20
            )
            value += max(1.0, threat / 4.0) * multiplier

        return min(18.0, value)

    def _future_turn_block_value(self, game, card):
        """Value persistent Block over protocol-known future attack turns."""

        if _token(getattr(card, "card_id", "")) != "metallicize":
            return 0.0
        block_per_turn = max(0, int(getattr(card, "magic_number", 0) or 0))
        if block_per_turn <= 0:
            return 0.0
        attack_turns = max(
            (
                len(self.FUTURE_ATTACK_PROFILES.get(
                    _token(getattr(monster, "monster_id", "")), ()
                ))
                for monster in combat_predictor.active_monsters(game)
                if not getattr(monster, "intent", None).is_attack()
            ),
            default=0,
        )
        return min(16.0, float(block_per_turn * attack_turns))

    def _strength_down_mitigation(self, game, card, target):
        card_id = _token(getattr(card, "card_id", ""))
        amount = (
            self._x_effect(game, card, upgraded_bonus=True)
            if card_id == "malaise"
            else max(0, int(getattr(card, "magic_number", 0) or 0))
        )
        if amount <= 0:
            return 0
        if _token(getattr(card, "card_id", "")) == "piercingwail":
            return sum(
                self._strength_reduction_per_hit(game, monster, amount)
                * max(1, int(getattr(monster, "move_hits", 0) or 0))
                for monster in combat_predictor.active_monsters(game)
                if getattr(monster, "intent", None).is_attack()
                and combat_predictor.power_amount(monster, "Artifact") <= 0
            )
        if (
            target is not None
            and getattr(target, "intent", None).is_attack()
            and combat_predictor.power_amount(target, "Artifact") <= 0
        ):
            return (
                self._strength_reduction_per_hit(game, target, amount)
                * max(1, int(getattr(target, "move_hits", 0) or 0))
            )
        return 0

    def _strength_reduction_per_hit(self, game, target, amount):
        per_hit = max(0, int(getattr(target, "move_adjusted_damage", 0) or 0))
        if per_hit <= 0:
            return 0
        multiplier = 1.0
        if combat_predictor.power_amount(target, "Weak", "Weakened") > 0:
            multiplier = 0.6 if self._has_relic(game, "Paper Krane") else 0.75
        # move_adjusted_damage already includes Weak. Strength is removed
        # before that multiplier, so a full point per stack is unsafe here.
        return min(per_hit, max(0, int(amount * multiplier)))

    def _malaise_mitigation(
        self, game, card, target, energy_override=None
    ):
        if target is None or not getattr(target, "intent", None).is_attack():
            return 0
        amount = self._x_effect(
            game,
            card,
            upgraded_bonus=True,
            energy_override=energy_override,
        )
        if amount <= 0:
            return 0
        hits = max(1, int(getattr(target, "move_hits", 0) or 0))
        strength_value = self._strength_reduction_per_hit(game, target, amount) * hits
        weak_value = (
            0
            if combat_predictor.power_amount(target, "Weak", "Weakened") > 0
            else max(1, combat_predictor.monster_threat(target) // 4)
        )
        artifact = combat_predictor.power_amount(target, "Artifact")
        if artifact <= 0:
            return strength_value + weak_value
        if artifact == 1:
            # Malaise applies two separate debuffs. One Artifact charge can
            # stop only one; count the smaller guaranteed surviving effect.
            return min(strength_value, weak_value)
        return 0

    def _poison_value(self, game, card, target):
        card_id = _token(getattr(card, "card_id", ""))
        poison_application_bonus = int(
            self._has_relic(game, "Snecko Skull")
        )
        if card_id == "catalyst":
            if target is None:
                return 0.0
            poison = combat_predictor.power_amount(target, "Poison")
            if poison <= 0:
                return -18.0
            if combat_predictor.power_amount(target, "Artifact") > 0:
                # Catalyst applies another instance of Poison rather than
                # mutating the existing stack in place.  Artifact consumes
                # that application, so spending an exhausting payoff here is
                # not a poison burst.  A prior poison/debuff card can remove
                # Artifact and make Catalyst valuable later in the same
                # ordered branch.
                return -18.0
            multiplier = self._catalyst_multiplier(card)
            if self._is_champ(target) and self._champ_is_before_half(target):
                added_poison = (
                    poison * (multiplier - 1) + poison_application_bonus
                )
                max_hp = max(1, int(getattr(target, "max_hp", 0) or 0))
                meaningful_burst = max(self.CHAMP_CATALYST_MIN_BURST, (max_hp + 9) // 10)
                boosted_tick = poison * multiplier + poison_application_bonus
                # Poison is cleansed at the phase change.  Above half health,
                # consume Catalyst only when its *additional* immediate tick is
                # material, or when the boosted tick is already lethal.
                if added_poison < meaningful_burst and boosted_tick < int(getattr(target, "current_hp", 0) or 0):
                    return -18.0
            return (
                poison * (multiplier - 1) + poison_application_bonus
            ) * 0.9
        if card_id not in self.POISON_CARDS:
            return 0.0

        amount = (
            max(1, int(getattr(card, "magic_number", 0) or 0))
            + poison_application_bonus
        )
        if card_id == "bouncingflask":
            # Each bounce is a separate three-Poison application.  The
            # upgrade adds a fourth application, which matters whenever
            # Artifact consumes only the first one or two packets.
            amount = (
                (3 + poison_application_bonus)
                * self._bouncing_flask_applications(card)
            )
        targets = [target] if target is not None else combat_predictor.active_monsters(game)
        if not targets:
            return 0.0
        if card_id != "corpseexplosion":
            # Poison ticks at the end of the turn.  A target whose current
            # Poison already guarantees that tick's death has no marginal
            # value for another Poison/Catalyst application; spending the
            # card there only hides the live target that still needs damage.
            doomed_ids = {
                id(monster)
                for monster in combat_predictor.projected_doomed_monsters(game)
            }
            if target is not None and id(target) in doomed_ids:
                return 0.0
            targets = [monster for monster in targets if id(monster) not in doomed_ids]
            if not targets:
                return 0.0

        if card_id == "bouncingflask" and len(targets) == 1:
            only_target = targets[0]
            applications = self._bouncing_flask_applications(card)
            absorbed = min(
                applications,
                max(
                    0,
                    combat_predictor.power_amount(
                        only_target, "Artifact"
                    ),
                ),
            )
            landed_poison = (
                (3 + poison_application_bonus)
                * max(0, applications - absorbed)
            )
            room_multiplier = (
                1.05
                if getattr(game, "room_type", "")
                in {"MonsterRoomBoss", "MonsterRoomElite"}
                else 0.72
            )
            return (
                min(
                    landed_poison,
                    max(1, int(getattr(only_target, "current_hp", 0) or 0)),
                )
                * room_multiplier
                + absorbed * 0.75
            )
        if (
            target is not None
            and combat_predictor.power_amount(target, "Artifact") > 0
            and card_id != "corpseexplosion"
        ):
            # Spending a poison card to strip Artifact can be useful when it
            # is the only target, but it must lose decisively to applying the
            # full poison stack to another otherwise equivalent monster.
            return -4.0
        # Poison is slower than direct damage, but becomes more valuable in
        # elite/boss rooms and when it sets up Catalyst or Corpse Explosion.
        room_multiplier = 1.05 if getattr(game, "room_type", "") in {"MonsterRoomBoss", "MonsterRoomElite"} else 0.72
        unblocked_targets = [
            monster for monster in targets
            if combat_predictor.power_amount(monster, "Artifact") <= 0
        ]
        artifact_break_value = 0.75 * (len(targets) - len(unblocked_targets))
        value = (
            min(
                amount,
                sum(max(1, int(monster.current_hp)) for monster in unblocked_targets),
            )
            * room_multiplier
            + artifact_break_value
        )
        if card_id == "corpseexplosion" and len(combat_predictor.living_monsters(game)) > 1:
            value += 10
            if target is not None:
                doomed_ids = {
                    id(monster)
                    for monster in combat_predictor.projected_doomed_monsters(game)
                }
                if id(target) in doomed_ids:
                    # Applying Corpse Explosion to an enemy already reserved
                    # to die guarantees the explosion this turn.  Estimate
                    # only damage visible from the current frame and cap the
                    # bonus so it cannot crowd out lethal defense.
                    explosion_damage = max(0, int(getattr(target, "max_hp", 0) or 0))
                    other_damage = sum(
                        min(
                            int(getattr(monster, "current_hp", 0) or 0),
                            combat_predictor.attack_hp_loss(
                                monster,
                                explosion_damage,
                                vulnerable_eligible=False,
                            ),
                        )
                        for monster in combat_predictor.living_monsters(game)
                        if monster is not target
                    )
                    value += min(36.0, 8.0 + other_damage * 0.55)
        return value

    @staticmethod
    def _catalyst_multiplier(card):
        """Return Catalyst's real multiplier from incomplete live card data.

        CommunicationMod can expose ``magic_number=-1`` for Catalyst.  The
        upgrade count is authoritative in that frame: base Catalyst doubles
        poison and Catalyst+ triples it.
        """

        magic_number = int(getattr(card, "magic_number", 0) or 0)
        if magic_number >= 2:
            return magic_number
        return 3 if int(getattr(card, "upgrades", 0) or 0) > 0 else 2

    @staticmethod
    def _bouncing_flask_applications(card):
        """Return the number of independent three-Poison applications."""

        magic_number = int(getattr(card, "magic_number", 0) or 0)
        if magic_number >= 3:
            return magic_number
        return 3 + min(1, max(0, int(getattr(card, "upgrades", 0) or 0)))

    @staticmethod
    def _is_champ(monster):
        return _token(getattr(monster, "monster_id", "")) in {"champ", "thechamp"}

    @staticmethod
    def _champ_is_before_half(champ):
        max_hp = max(1, int(getattr(champ, "max_hp", 0) or 0))
        return int(getattr(champ, "current_hp", 0) or 0) > max_hp // 2

    def _poison_after_card(self, game, card=None, target=None):
        # Retain the historical two-argument helper for offline fixtures;
        # live/static callers pass ``game`` so Snecko Skull is observable.
        if target is None:
            target = card
            card = game
            game = None
        poison = combat_predictor.power_amount(target, "Poison")
        card_id = _token(getattr(card, "card_id", ""))
        snecko_skull = int(self._has_relic(game, "Snecko Skull"))
        if card_id == "catalyst":
            if poison <= 0 or combat_predictor.power_amount(
                target, "Artifact"
            ) > 0:
                return poison
            return (
                poison * self._catalyst_multiplier(card) + snecko_skull
            )
        if card_id not in self.POISON_CARDS or card_id == "noxiousfumes":
            return poison
        if card_id == "bouncingflask":
            applications = self._bouncing_flask_applications(card)
            artifact = max(
                0, combat_predictor.power_amount(target, "Artifact")
            )
            landed = max(0, applications - min(applications, artifact))
            return poison + (3 + snecko_skull) * landed
        if combat_predictor.power_amount(target, "Artifact") > 0:
            return poison

        amount = max(0, int(getattr(card, "magic_number", 0) or 0))
        return poison + amount + snecko_skull

    def _champ_execute_estimate(self, game, champ):
        """Conservative estimate for the post-cleanse two-hit Execute."""

        strength = combat_predictor.power_amount(champ, "Strength")
        # At A0 the phase change adds six Strength.  Starting from ten per hit
        # also remains conservative for the low-ascension goal in this repo.
        per_hit = 10 + strength + 6
        # Anger removes every debuff at the phase transition, so Weak on the
        # pre-split frame cannot reduce the following Execute.

        player = getattr(game, "player", None)
        if combat_predictor.power_amount(player, "Vulnerable") >= 2:
            per_hit = (per_hit * 3 + 1) // 2
        if combat_predictor.power_amount(player, "Intangible", "IntangiblePlayer") >= 2:
            per_hit = 1

        loss = per_hit * 2
        if combat_predictor.projected_buffer_remaining(game) > 0:
            loss -= per_hit
        return max(0, loss)

    def _champ_transition_projection(self, game, card, target, direct_damage):
        """Return whether this play newly crosses half HP and if it is safe."""

        active = combat_predictor.active_monsters(game)
        champ = target if target is not None and self._is_champ(target) else None
        if champ is None and target is None and len(active) == 1 and self._is_champ(active[0]):
            champ = active[0]
        if champ is None or not self._champ_is_before_half(champ):
            return False, True

        hp = int(getattr(champ, "current_hp", 0) or 0)
        max_hp = max(1, int(getattr(champ, "max_hp", 0) or 0))
        threshold = max_hp // 2
        current_poison = combat_predictor.power_amount(champ, "Poison")

        # If the already-present poison crosses the line, declining this card
        # cannot prevent the transition and should not distort card ordering.
        if hp - current_poison <= threshold:
            return False, True

        poison_after = self._poison_after_card(game, card, champ)
        projected_hp = hp - max(0, int(direct_damage or 0)) - poison_after
        if projected_hp > threshold:
            return False, True

        if projected_hp <= 0 or projected_hp <= max(24, max_hp // 12):
            return True, True
        player_hp = int(getattr(getattr(game, "player", None), "current_hp", 0) or 0)
        hp_after_current_turn = player_hp - combat_predictor.projected_turn_hp_loss(game)
        ready = hp_after_current_turn > self._champ_execute_estimate(game, champ) + 4
        return True, ready

    @classmethod
    def _is_split_monster(cls, monster):
        return (
            monster is not None
            and _token(getattr(monster, "monster_id", ""))
            in cls.SPLIT_MONSTER_IDS
        )

    @staticmethod
    def _is_slime_family(monster):
        enemy_id = _token(getattr(monster, "monster_id", ""))
        return enemy_id.startswith("acidslime") or enemy_id.startswith(
            "spikeslime"
        ) or enemy_id == "slimeboss"

    def _slime_generated_shiv_finish_window(
        self, game, state, monsters, hp, block,
    ):
        """Value one observable next-turn Shiv without predicting its draw.

        The enemy turn and Thorns reactions have already resolved in ``hp``.
        In an all-slime fight, an active Infinite Blades stack is therefore a
        concrete next-turn attack source.  Keep the value bounded by the
        known next-attack envelope of a target and allocate each generated
        Shiv once; hidden move rolls remain unknown.
        """

        empty = {
            "source": None,
            "generated_cards": 0,
            "damage_per_card": 0,
            "candidate_indexes": [],
            "selected_indexes": [],
            "attack_upper_bound_removed": 0,
            "potential_credit": 0.0,
        }
        living_indexes = [
            index for index, value in enumerate(hp) if int(value or 0) > 0
        ]
        if len(living_indexes) < 2 or not all(
            self._is_slime_family(monsters[index])
            for index in living_indexes
        ):
            return empty

        generated_cards = max(
            0,
            combat_predictor.power_amount(
                game.player,
                "Infinite Blades",
                "InfiniteBlades",
                "InfiniteBladesPower",
            ),
        )
        if generated_cards <= 0:
            return empty
        # Infinite Blades adds its Shiv after the normal draw step.  Runic
        # Pyramid makes the future retained/drawn hand size order-dependent;
        # fail closed for that relic instead of promising a card with no slot.
        if self._has_relic(game, "Runic Pyramid"):
            return empty

        damage_per_card = 4 + max(0, int(state.player_accuracy or 0))
        if self._has_relic(game, "Wrist Blade"):
            damage_per_card += 4
        # Positive Strength is intentionally omitted from this lower bound.
        # Negative Strength must remain, or the window can claim a kill the
        # generated Shiv cannot actually reach.
        damage_per_card += min(
            0,
            combat_predictor.signed_power_amount(
                game.player, "Strength", "StrengthPower"
            ),
        )
        damage_per_card += min(
            0, int(getattr(state, "player_strength_bonus", 0) or 0)
        )

        # Weak already at two or more stacks survives this end step.  An Acid
        # Slime currently using Lick also applies Weak before the next Shiv;
        # counting it even through possible Artifact is conservative.
        future_weak = combat_predictor.power_amount(
            game.player, "Weak", "WeakPower"
        ) > 1
        future_weak = future_weak or any(
            _token(getattr(monsters[index], "monster_id", ""))
            == "acidslimem"
            and _token(
                getattr(
                    getattr(monsters[index], "intent", None),
                    "name",
                    getattr(monsters[index], "intent", None),
                )
            ) == "debuff"
            for index in living_indexes
        )
        if future_weak:
            damage_per_card = int(damage_per_card * 0.75)
        if damage_per_card <= 0:
            return empty

        candidates = []
        for index in living_indexes:
            monster = monsters[index]
            if self._is_split_monster(monster):
                continue
            if max(0, int(block[index] or 0)) > 0:
                continue
            final_hp = max(0, int(hp[index] or 0))
            if final_hp > damage_per_card:
                continue
            attack_upper_bound = (
                combat_predictor.next_turn_attack_upper_bound(game, monster)
            )
            if attack_upper_bound <= 0:
                continue
            candidates.append((
                attack_upper_bound,
                combat_predictor.monster_threat(monster),
                -final_hp,
                index,
            ))

        candidates.sort(reverse=True)
        selected = candidates[:generated_cards]
        selected_indexes = [item[3] for item in selected]
        removed = sum(item[0] for item in selected)
        return {
            "source": "infinite_blades",
            "generated_cards": int(generated_cards),
            "damage_per_card": int(damage_per_card),
            "candidate_indexes": [item[3] for item in candidates],
            "selected_indexes": selected_indexes,
            "attack_upper_bound_removed": int(removed),
            # One future card must not outweigh arbitrary current-turn value.
            # Its bounded attack envelope is sufficient to break the stale
            # focus tie in the observed low-HP Slime Boss state.
            "potential_credit": float(
                sum(min(12, item[0]) for item in selected)
            ),
        }

    @staticmethod
    def _split_threshold(monster):
        return max(1, int(getattr(monster, "max_hp", 0) or 0)) // 2

    @classmethod
    def _split_pending_at_hp(cls, monster, hp):
        return (
            cls._is_split_monster(monster)
            and 0 < int(hp or 0) <= cls._split_threshold(monster)
        )

    @staticmethod
    def _is_unawakened_phase(monster):
        return (
            _token(getattr(monster, "monster_id", "")) == "awakenedone"
            and combat_predictor.has_power(
                monster,
                "Unawakened",
                "UnawakenedPower",
                "Curiosity",
                "CuriosityPower",
            )
        )

    @staticmethod
    def _is_darkling(monster):
        return _token(getattr(monster, "monster_id", "")) == "darkling"

    @classmethod
    def _is_true_death_at_hp(
        cls, monster, hp, monsters=None, hp_values=None
    ):
        """Whether zero HP is a permanent kill rather than a revival state."""

        if int(hp or 0) > 0 or cls._is_unawakened_phase(monster):
            return False
        if cls._is_darkling(monster):
            if monsters is None or hp_values is None:
                return False
            darkling_indexes = [
                index
                for index, candidate in enumerate(monsters)
                if cls._is_darkling(candidate)
            ]
            return bool(darkling_indexes) and all(
                int(hp_values[index] or 0) <= 0
                for index in darkling_indexes
            )
        return True

    @classmethod
    def _is_fatal_kill(cls, game, target, final_hp):
        """Whether a card's Fatal/on-kill reward fires at this transition."""

        if int(final_hp or 0) > 0 or cls._is_unawakened_phase(target):
            return False
        if not cls._is_darkling(target):
            return True
        return not any(
            monster is not target
            and cls._is_darkling(monster)
            and not bool(getattr(monster, "half_dead", False))
            and int(getattr(monster, "current_hp", 0) or 0) > 0
            for monster in getattr(game, "monsters", []) or []
        )

    @classmethod
    def _sunder_refund_is_true_kill(
        cls, game, target, final_hp, monsters=None, hp_values=None
    ):
        """Whether Sunder's direct hit earns its three-energy refund.

        Crossing a Large Slime's split threshold while it remains above zero
        is not a kill; phase-one Awakened One or a non-final Darkling at zero
        enters a revival state.  Reusing a coarse phase-transition shortcut
        for these encounters fabricates same-turn energy.
        """

        if target is None or int(final_hp or 0) > 0:
            return False
        if monsters is not None and hp_values is not None:
            return cls._is_true_death_at_hp(
                target, final_hp, monsters, hp_values
            )
        return cls._is_fatal_kill(game, target, final_hp)

    @classmethod
    def _all_truly_dead(cls, monsters, hp):
        return bool(monsters) and all(
            cls._is_true_death_at_hp(
                monster, hp[index], monsters, hp
            )
            for index, monster in enumerate(monsters)
        )

    @classmethod
    def _slime_transition_projection(cls, target, direct_damage):
        if target is None or not cls._is_split_monster(target):
            return False
        hp = int(getattr(target, "current_hp", 0) or 0)
        threshold = cls._split_threshold(target)
        return hp > threshold and hp - max(0, int(direct_damage or 0)) <= threshold

    @classmethod
    def _transition_suppresses_action(
        cls, monster, final_hp, cumulative_hp_damage=0
    ):
        """Whether current-turn damage replaced this monster's old intent.

        This is deliberately based on the simulated branch, not a one-card
        candidate.  It therefore catches cumulative attacks, Choke triggers,
        deterministic orb damage and poison resolved at END.
        """

        final_hp = int(final_hp or 0)
        if final_hp <= 0:
            # A first-phase Awakened One is not a true death, but Rebirth still
            # replaces its old attack for this enemy turn.
            return True
        if (
            cls._is_split_monster(monster)
            and 0 < final_hp <= cls._split_threshold(monster)
        ):
            return True
        mode_shift = combat_predictor.power_amount(
            monster, "Mode Shift", "ModeShift", "ModeShiftPower"
        )
        if mode_shift > 0 and int(cumulative_hp_damage or 0) >= mode_shift:
            return True
        # Champ chooses Anger in getMove for his next intent. Crossing half
        # health (by a card, orb, or Poison) never interrupts the current hit.
        # Keep phase-two preparation scoring separate from exact mitigation.
        return False

    def _is_champ_preparation_candidate(self, candidate):
        """Whether a non-crossing play materially prepares for Execute."""

        if candidate.champ_transition or candidate.base_score <= 0.75:
            return False
        card = candidate.card
        card_id = _token(getattr(card, "card_id", ""))
        if getattr(card, "type", None) == CardType.POWER:
            return True
        if (
            card_id in self.DRAW_COUNTS
            or card_id in self.ENERGY_VALUES
            or card_id in self.NEXT_TURN_ENERGY_VALUES
        ):
            return True
        return False

    def _orb_value(self, game, card_id, orbs=None, *, focus_override=None):
        # This is bounded future utility, not a reward for playing an orb card.
        # Exact evocations and this turn's mitigation are resolved separately.
        channel_values = {
            "zap": ("lightning", 6.0), "balllightning": ("lightning", 5.5),
            "coldsnap": ("frost", 4.0), "glacier": ("frost", 7.0),
            "coolheaded": ("frost", 3.5), "doomandgloom": ("dark", 8.0),
        }
        if card_id in channel_values:
            orb_id, value = channel_values[card_id]
            projected = self._projected_orb(
                game, orb_id, focus_override=focus_override,
            )
            baseline = self._projected_orb(game, orb_id, focus_override=0)
            access = min(1.0, (
                self._orb_passive(projected) + self._orb_evoke(projected)
            ) / max(1, self._orb_passive(baseline) + self._orb_evoke(baseline)))
            return value * access
        if card_id == "dualcast":
            occupied = list(self._occupied_orbs(game) if orbs is None else orbs)
            return float(self._orb_evoke(occupied[0])) * 2.0 if occupied else 0.0
        return 0.0

    def _frost_channel_mitigation(self, game, card_id):
        channel_count = {
            "coldsnap": 1,
            "coolheaded": 1,
            "glacier": 2,
            "rainbow": 1,
        }.get(card_id, 0)
        if card_id == "chill":
            channel_count = len(combat_predictor.active_monsters(game))
        if channel_count <= 0:
            return 0

        player = getattr(game, "player", None)
        orbs = list(getattr(player, "orbs", []) or [])
        before = sum(
            max(0, int(getattr(orb, "passive_amount", 0) or 0))
            for orb in orbs
            if _token(getattr(orb, "orb_id", "")) == "frost"
        )
        immediate_evoke_block = 0
        # CommunicationMod serializes empty slots as Empty orbs.  Preserve the
        # slot count and simulate the deterministic left-to-right channeling
        # order only far enough to project block before the enemy attack.
        slots = [
            {
                "id": _token(getattr(orb, "orb_id", "")),
                "passive": max(0, int(getattr(orb, "passive_amount", 0) or 0)),
                "evoke": max(0, int(getattr(orb, "evoke_amount", 0) or 0)),
            }
            for orb in orbs
        ]
        if not slots:
            slots = [{"id": "empty", "passive": 0, "evoke": 0} for _ in range(3)]
        focus = next(
            (
                int(getattr(power, "amount", 0) or 0)
                for power in getattr(player, "powers", []) or []
                if _token(getattr(power, "power_id", "")) == "focus"
                or _token(getattr(power, "power_name", "")) == "focus"
            ),
            0,
        )
        new_frost = {"id": "frost", "passive": max(0, 2 + focus), "evoke": max(0, 5 + focus)}
        for _ in range(channel_count):
            empty_index = next((index for index, orb in enumerate(slots) if orb["id"] in {"", "empty"}), None)
            if empty_index is not None:
                slots[empty_index] = dict(new_frost)
            else:
                evoked = slots.pop(0)
                if evoked["id"] == "frost":
                    immediate_evoke_block += evoked["evoke"]
                slots.append(dict(new_frost))
        after = sum(orb["passive"] for orb in slots if orb["id"] == "frost")
        return max(0, after - before + immediate_evoke_block)

    def _frost_evoke_mitigation(
        self, game, card, orbs=None, energy_override=None
    ):
        """Return (net attack mitigation, immediate block gained) from evokes."""

        card_id = _token(getattr(card, "card_id", ""))
        occupied = list(
            self._occupied_orbs(game) if orbs is None else orbs
        )
        if not occupied:
            return 0, 0

        if card_id in {"dualcast", "multicast", "recursion", "redo"}:
            orb = occupied[0]
            if self._orb_id(orb) != "frost":
                return 0, 0
            evoke = self._orb_evoke(orb)
            passive = self._orb_passive(orb)
            if card_id == "dualcast":
                evokes, keeps_orb = 2, False
            elif card_id == "multicast":
                evokes = self._x_effect(
                    game,
                    card,
                    upgraded_bonus=True,
                    energy_override=energy_override,
                )
                keeps_orb = False
            else:
                evokes, keeps_orb = 1, True
            immediate = evoke * evokes
            net = immediate if keeps_orb else max(0, immediate - passive)
            return net, immediate

        if card_id == "fission" and int(getattr(card, "upgrades", 0) or 0) > 0:
            immediate = sum(
                self._orb_evoke(orb)
                for orb in occupied
                if self._orb_id(orb) == "frost"
            )
            lost_passive = sum(
                self._orb_passive(orb)
                for orb in occupied
                if self._orb_id(orb) == "frost"
            )
            return immediate - lost_passive, immediate
        if card_id == "fission":
            lost_passive = sum(
                self._orb_passive(orb)
                for orb in occupied
                if self._orb_id(orb) == "frost"
            )
            return -lost_passive, 0
        return 0, 0

    def _heart_block_sequence_survives(self, game, heart, required_card):
        """Whether a block-card sequence beginning here survives Heart.

        Candidate scoring normally evaluates one card at a time. Beat of
        Death is the important exception: two individually insufficient
        Defends can be a safe sequence. This bounded search models only
        deterministic block cards, so a positive result is a real survival
        line rather than an optimistic draw assumption.
        """

        player = getattr(game, "player", None)
        beat = combat_predictor.power_amount(
            heart, "Beat of Death", "BeatOfDeathPower"
        )
        if (
            player is None
            or beat <= 0
            or not combat_predictor.can_gain_block(player)
        ):
            return False
        playable = [
            card
            for card in getattr(game, "hand", []) or []
            if getattr(card, "is_playable", False)
        ]
        if required_card not in playable:
            return False

        def direct_block(card, energy_before, block_before, used=frozenset()):
            card_id = _token(getattr(card, "card_id", ""))
            value = max(0, int(getattr(card, "block", 0) or 0))
            if card_id == "autoshields" and block_before > 0:
                return 0
            if card_id == "reinforcedbody":
                return value * self._x_effect(
                    game, card, energy_override=energy_before
                )
            if card_id == "secondwind":
                serialized_base = int(
                    getattr(card, "base_block", 0) or 0
                )
                per_card = (
                    value
                    if value > 0 or serialized_base > 0
                    else 5 + 2 * int(getattr(card, "upgrades", 0) or 0)
                )
                exhaustible = sum(
                    1
                    for other in getattr(game, "hand", []) or []
                    if other is not card
                    and id(other) not in used
                    and getattr(other, "type", None) != CardType.ATTACK
                )
                return per_card * exhaustible
            if card_id == "entrench":
                return block_before
            return value

        block_cards = [
            card
            for card in playable
            if direct_block(
                card,
                max(0, int(getattr(player, "energy", 0) or 0)),
                max(0, int(getattr(player, "block", 0) or 0)),
            )
            > 0
        ]
        if required_card not in block_cards:
            return False

        intangible = combat_predictor.has_power(
            player, "Intangible", "IntangiblePlayer"
        )
        after_image = combat_predictor.power_amount(
            player, "After Image", "AfterImagePower"
        )
        future_block = (
            combat_predictor.power_amount(player, "Metallicize")
            + combat_predictor.power_amount(
                player, "Plated Armor", "PlatedArmor"
            )
            + sum(
                max(0, int(getattr(orb, "passive_amount", 0) or 0))
                for orb in getattr(player, "orbs", []) or []
                if _token(getattr(orb, "orb_id", "")) == "frost"
            )
        )
        def play(state, card):
            hp, block, energy, buffer, state_intangible, used = state
            raw_cost = int(getattr(card, "cost", 0) or 0)
            cost = energy if raw_cost == -1 else max(0, raw_cost)
            if cost > energy:
                return None
            card_id = _token(getattr(card, "card_id", ""))
            self_outcome = combat_predictor.resolve_player_damage_events(
                game,
                tuple(
                    combat_predictor.PlayerDamageEvent(
                        "card_self", amount, blockable=False
                    )
                    for amount in self._card_self_damage_events(game, card)
                ),
                block=block,
                buffer_layers=buffer,
                force_intangible=state_intangible,
            )
            hp -= self_outcome.hp_loss
            block = self_outcome.block
            buffer = self_outcome.buffer
            if hp <= 0:
                return None

            gained = direct_block(card, energy, block, used)
            block += gained + after_image
            if getattr(card, "type", None) == CardType.ATTACK:
                block += combat_predictor.power_amount(
                    player, "Rage", "RagePower"
                )
                fan = next(
                    (
                        relic
                        for relic in getattr(game, "relics", []) or []
                        if _token(getattr(relic, "relic_id", ""))
                        == "ornamentalfan"
                    ),
                    None,
                )
                if fan is not None:
                    prior_attacks = sum(
                        1
                        for other in playable
                        if id(other) in used
                        and getattr(other, "type", None) == CardType.ATTACK
                    )
                    if (
                        max(0, int(getattr(fan, "counter", 0) or 0))
                        + prior_attacks
                        + 1
                    ) % 3 == 0:
                        block += 4
            if card_id in self.INTANGIBLE_CARDS:
                state_intangible = True
            if card_id == "buffer":
                buffer += 1 + int(getattr(card, "upgrades", 0) or 0)
            beat_outcome = combat_predictor.resolve_player_damage_events(
                game,
                (
                    combat_predictor.PlayerDamageEvent(
                        "beat_of_death", beat, blockable=True
                    ),
                ),
                block=block,
                buffer_layers=buffer,
                force_intangible=state_intangible,
            )
            hp -= beat_outcome.hp_loss
            block = beat_outcome.block
            buffer = beat_outcome.buffer
            if hp <= 0:
                return None
            newly_used = {id(card)}
            if card_id == "secondwind":
                # Second Wind exhausts every other non-attack in hand. Those
                # cards both determine its block and become unavailable to
                # the remainder of this simulated sequence.
                newly_used.update(
                    id(other)
                    for other in getattr(game, "hand", []) or []
                    if id(other) not in used
                    and other is not card
                    and getattr(other, "type", None) != CardType.ATTACK
                )
            return (
                hp,
                block,
                energy - cost,
                buffer,
                state_intangible,
                used | newly_used,
            )

        initial = (
            max(0, int(getattr(player, "current_hp", 0) or 0)),
            max(0, int(getattr(player, "block", 0) or 0)),
            max(0, int(getattr(player, "energy", 0) or 0)),
            combat_predictor.power_amount(player, "Buffer"),
            intangible,
            frozenset(),
        )
        first_state = play(initial, required_card)
        if first_state is None:
            return False

        seen = set()

        def search(state):
            hp, block, energy, buffer, state_intangible, used = state
            remaining_hand = [
                card
                for card in getattr(game, "hand", []) or []
                if id(card) not in used
            ]
            turn_outcome = combat_predictor.projected_turn_outcome(
                game,
                block_override=block,
                buffer_override=buffer,
                passive_block_override=future_block,
                hand_override=remaining_hand,
                hand_size_override=len(remaining_hand),
                force_intangible=state_intangible,
                player_hp_override=hp,
            )
            if turn_outcome.total_hp_loss < hp:
                return True
            key = (
                hp,
                block,
                energy,
                buffer,
                state_intangible,
                tuple(sorted(used)),
            )
            if key in seen:
                return False
            seen.add(key)
            for card in block_cards:
                if id(card) in used:
                    continue
                next_state = play(state, card)
                if next_state is not None and search(next_state):
                    return True
            return False

        return search(first_state)

    def _candidate(self, game, card, target, preferred_target, incoming, attack_loss, total_loss):
        card_id = _token(getattr(card, "card_id", ""))
        damage = 0
        kills = False
        base_score = 0.0
        base_block = getattr(card, "base_block", None)
        mitigation = max(0, int(getattr(card, "block", 0) or 0)) if base_block is None or base_block >= 0 else 0
        player = getattr(game, "player", None)
        if card_id == "autoshields" and int(getattr(player, "block", 0) or 0) > 0:
            mitigation = 0
        elif card_id == "reinforcedbody":
            mitigation = max(0, int(getattr(card, "block", 0) or 0)) * self._x_effect(game, card)
        block_mitigation = mitigation
        block_gain = mitigation
        neutralized_attackers = []
        neutralization_mitigation = 0
        thorns_damage_events = []
        sharp_hide_damage_events = []
        new_buffer_layers = 0
        buffer_mitigation = 0
        intangible_mitigation = 0

        raw_damage, hits = combat_predictor.card_attack_profile(
            game, card, target=target
        )
        random_multi_target_attack = _is_random_multi_target_attack(
            game, card, target
        )
        if card.type == CardType.ATTACK and raw_damage > 0:
            if target is not None:
                damage = combat_predictor.card_attack_hp_loss(
                    game, card, target, target=target,
                )
                target_hp = max(0, int(getattr(target, "current_hp", 0) or 0))
                effective_damage = min(damage, target_hp)
                # Regenerate resolves after the enemy's next material turn;
                # value only the net HP reduction that survives that boundary.
                post_attack_hp = max(0, target_hp - effective_damage)
                effective_damage = max(
                    0,
                    effective_damage
                    - combat_predictor.projected_monster_end_turn_healing(
                        target, post_attack_hp
                    ),
                )
                reaches_zero = (
                    damage >= int(getattr(target, "current_hp", 0) or 0)
                    and not combat_predictor.has_unresolved_damage_cap(target)
                )
                kills = reaches_zero and self._is_fatal_kill(
                    game, target, 0
                )
                base_score += effective_damage
                if kills:
                    base_score += 8 + combat_predictor.monster_threat(target) * 0.35
                    neutralized_attackers.append(target)
                mode_shift = combat_predictor.power_amount(
                    target, "Mode Shift", "ModeShift"
                )
                if (
                    mode_shift > 0
                    and damage >= mode_shift
                    and getattr(target, "intent", None).is_attack()
                ):
                    neutralized_attackers.append(target)
                if (
                    getattr(target, "intent", None).is_attack()
                    and self._transition_suppresses_action(
                        target,
                        max(
                            0,
                            int(getattr(target, "current_hp", 0) or 0)
                            - damage,
                        ),
                        damage,
                    )
                    and all(
                        item is not target for item in neutralized_attackers
                    )
                ):
                    neutralized_attackers.append(target)
                thorns_events, sharp_hide_events = (
                    self._reactive_damage_events(game, target, hits)
                )
                thorns_damage_events.extend(thorns_events)
                sharp_hide_damage_events.extend(sharp_hide_events)
            else:
                aoe_targets = (
                    combat_predictor.living_monsters(game)
                    if card_id == "reaper"
                    else combat_predictor.active_monsters(game)
                )
                if random_multi_target_attack:
                    # Rip and Tear has two random packets.  For scoring, use a
                    # deterministic conservative witness (one packet on each
                    # of the first two live enemies) so damage is never
                    # multiplied by the number of enemies.  The hidden target
                    # remains unbound and therefore cannot prove a kill.
                    per_hit, remainder = divmod(
                        max(0, int(raw_damage or 0)),
                        max(1, int(hits or 1)),
                    )
                    for hit_index, monster in enumerate(
                        aoe_targets[: max(1, int(hits or 1))]
                    ):
                        packet = per_hit + int(hit_index < remainder)
                        dealt = combat_predictor.attack_hp_loss(
                            monster,
                            packet,
                            hits=1,
                            **combat_predictor.attack_relic_modifiers(game),
                        )
                        monster_hp = max(
                            0, int(getattr(monster, "current_hp", 0) or 0)
                        )
                        dealt_hp = min(dealt, monster_hp)
                        post_attack_hp = max(0, monster_hp - dealt_hp)
                        damage += max(
                            0,
                            dealt_hp
                            - combat_predictor.projected_monster_end_turn_healing(
                                monster, post_attack_hp
                            ),
                        )
                    thorns_events, sharp_hide_events = (
                        self._card_reactive_damage_events(
                            game,
                            card,
                            None,
                            hits,
                            active_targets=aoe_targets,
                        )
                    )
                    thorns_damage_events.extend(thorns_events)
                    sharp_hide_damage_events.extend(sharp_hide_events)
                else:
                    for monster in aoe_targets:
                        dealt = combat_predictor.card_attack_hp_loss(
                            game, card, monster,
                        )
                        monster_hp = max(
                            0, int(getattr(monster, "current_hp", 0) or 0)
                        )
                        dealt_hp = min(dealt, monster_hp)
                        post_attack_hp = max(0, monster_hp - dealt_hp)
                        damage += max(
                            0,
                            dealt_hp
                            - combat_predictor.projected_monster_end_turn_healing(
                                monster, post_attack_hp
                            ),
                        )
                        if (
                            dealt >= int(getattr(monster, "current_hp", 0) or 0)
                            and not combat_predictor.has_unresolved_damage_cap(monster)
                            and self._is_fatal_kill(game, monster, 0)
                        ):
                            base_score += 7 + combat_predictor.monster_threat(monster) * 0.25
                            neutralized_attackers.append(monster)
                        mode_shift = combat_predictor.power_amount(
                            monster, "Mode Shift", "ModeShift"
                        )
                        if (
                            mode_shift > 0
                            and dealt >= mode_shift
                            and getattr(monster, "intent", None).is_attack()
                        ):
                            neutralized_attackers.append(monster)
                        if (
                            getattr(monster, "intent", None).is_attack()
                            and self._transition_suppresses_action(
                                monster,
                                max(
                                    0,
                                    int(getattr(monster, "current_hp", 0) or 0)
                                    - dealt,
                                ),
                                dealt,
                            )
                            and all(
                                item is not monster
                                for item in neutralized_attackers
                            )
                        ):
                            neutralized_attackers.append(monster)
                        thorns_events, sharp_hide_events = (
                            self._reactive_damage_events(game, monster, hits)
                        )
                        thorns_damage_events.extend(thorns_events)
                        sharp_hide_damage_events.extend(sharp_hide_events)
                base_score += damage
        elif card.type == CardType.ATTACK:
            # A random multi-enemy attack has no guaranteed direct target, but
            # its serialized hits still trigger reactive damage. A genuine
            # zero-hit attack keeps only Sharp Hide's once-per-card event.
            thorns_events, sharp_hide_events = (
                self._card_reactive_damage_events(
                    game, card, target, hits
                )
            )
            thorns_damage_events.extend(thorns_events)
            sharp_hide_damage_events.extend(sharp_hide_events)

        if card.type == CardType.ATTACK:
            base_score += self._envenom_attack_utility(game, card, target)
        elif card.type == CardType.POWER:
            base_score += self._mummified_hand_power_utility(game, card)

        reactive_damage_events = (
            thorns_damage_events + sharp_hide_damage_events
        )

        if neutralized_attackers:
            loss_after_play = combat_predictor.projected_attack_hp_loss(
                game,
                excluded_monsters=neutralized_attackers,
            )
            neutralization_mitigation = max(0, attack_loss - loss_after_play)
            mitigation += neutralization_mitigation

        if card_id == "secondwind":
            serialized_block = max(0, int(getattr(card, "block", 0) or 0))
            serialized_base = int(getattr(card, "base_block", 0) or 0)
            per_card = (
                serialized_block
                if serialized_block > 0 or serialized_base > 0
                else 5 + 2 * int(getattr(card, "upgrades", 0) or 0)
            )
            exhaustible = sum(
                1
                for hand_card in getattr(game, "hand", []) or []
                if hand_card is not card
                and getattr(hand_card, "type", None) != CardType.ATTACK
            )
            gained = per_card * exhaustible
            # The serialized block field already contains Second Wind's
            # per-card amount, not block granted before the exhaust loop.
            mitigation = gained
            block_mitigation = gained
            block_gain = gained
        elif card_id == "entrench":
            gained = max(
                0,
                int(getattr(getattr(game, "player", None), "block", 0) or 0),
            )
            mitigation += gained
            block_mitigation += gained
            block_gain += gained

        frost_evoke_mitigation, frost_evoke_block = self._frost_evoke_mitigation(
            game, card
        )
        mitigation += frost_evoke_mitigation
        block_mitigation += frost_evoke_mitigation
        block_gain += frost_evoke_block

        if card_id in self.WEAK_CARDS:
            if target is None and card_id in self.AOE_WEAK_CARDS:
                mitigation += sum(
                    self._weak_mitigation(monster)
                    for monster in combat_predictor.active_monsters(game)
                )
            else:
                mitigation += self._weak_mitigation(target)
        if card_id in self.STRENGTH_DOWN_CARDS:
            mitigation += self._strength_down_mitigation(game, card, target)
        if card_id == "malaise":
            mitigation += self._malaise_mitigation(game, card, target)
        if card_id == "buffer":
            new_buffer_layers = 1 + int(getattr(card, "upgrades", 0) or 0)
            attack_buffer_layers = new_buffer_layers
            heart = next(
                (
                    monster for monster in combat_predictor.active_monsters(game)
                    if _token(getattr(monster, "monster_id", "")) == "corruptheart"
                ),
                None,
            )
            if heart is not None:
                beat = combat_predictor.power_amount(
                    heart, "Beat of Death", "BeatOfDeathPower"
                )
                after_image = combat_predictor.power_amount(
                    player, "After Image", "AfterImagePower"
                )
                if beat > int(getattr(player, "block", 0) or 0) + after_image:
                    # Beat triggers on the Buffer card before the enemy acts.
                    # If it reaches HP, it consumes one newly-created stack.
                    attack_buffer_layers = max(0, attack_buffer_layers - 1)
            loss_with_buffer = combat_predictor.projected_attack_hp_loss(
                game, extra_buffer=attack_buffer_layers
            )
            buffer_mitigation = max(0, attack_loss - loss_with_buffer)
            mitigation += buffer_mitigation
        if card_id in self.INTANGIBLE_CARDS:
            # Wraith Form/Apparition apply Intangible immediately. Serialized
            # card has no block value, so without this projection a lethal
            # turn can incorrectly prefer a small Defend or an attack.
            loss_with_intangible = combat_predictor.projected_attack_hp_loss(
                game, force_intangible=True
            )
            intangible_mitigation = max(0, attack_loss - loss_with_intangible)
            mitigation += intangible_mitigation
        frost_channel_block = self._frost_channel_mitigation(game, card_id)
        mitigation += frost_channel_block
        block_mitigation += frost_channel_block
        after_image_block = combat_predictor.power_amount(
            getattr(game, "player", None), "After Image", "AfterImagePower"
        )
        mitigation += after_image_block
        block_mitigation += after_image_block

        if not combat_predictor.can_gain_block(game):
            # NoBlockPower preserves block already on the player but makes
            # every new gainBlock source a no-op.  Keep Weak/Strength-down,
            # Buffer and Intangible mitigation while removing only the block
            # component from this static ordering score.
            mitigation = max(0, mitigation - block_mitigation)
            block_mitigation = 0
            block_gain = 0
            after_image_block = 0

        orichalcum_block_gain = 0
        orichalcum_single_mitigation = 0
        # Do not replace Orichalcum in this static score. Its check happens
        # before Metallicize, Plated Armor and Frost passives, while immediate
        # card/After Image/Frost-evoke block can be spent again by reactive
        # events before END. The ordered beam terminal has the actual current
        # block at that point and performs the single authoritative check.

        intrinsic_mitigation = mitigation
        state_block_mitigation = (
            orichalcum_single_mitigation
            if orichalcum_block_gain > 0
            else block_mitigation
        )

        draw = self._card_draw_count(
            card,
            game,
            hand_size_before_play=len(getattr(game, "hand", []) or []),
        )
        draw += self._heatsinks_draw_count(game, card)
        conditional_draw, conditional_energy = (
            self._authoritative_conditional_resources(
                game, card, target
            )
        )
        draw += conditional_draw
        energy_gain = self._energy_gain(card, game) + conditional_energy
        authoritative_sunder_refund = (
            card_id == "sunder"
            and target is not None
            and damage >= max(
                1, int(getattr(target, "current_hp", 0) or 0)
            )
            and not combat_predictor.has_unresolved_damage_cap(target)
            and self._sunder_refund_is_true_kill(game, target, 0)
        )
        if authoritative_sunder_refund:
            energy_gain += 3
        created_cards = {
            "bladedance": max(
                3,
                int(getattr(card, "magic_number", 0) or 0),
            ),
            "cloakanddagger": max(
                1,
                int(getattr(card, "magic_number", 0) or 0),
            ),
            "discovery": 1,
            "distraction": 1,
            "foreigninfluence": 1,
            "infernalblade": 1,
            "jackofalltrades": 1,
            "transmutation": self._x_effect(game, card),
            "whitenoise": 1,
        }.get(card_id, 0)
        # End-turn hand effects are regenerated from the simulated remaining
        # hand. Drawn/generated cards are not candidates in this bounded
        # search, but they still count toward Regret's queued amount.
        hand_additions = max(0, draw) + max(0, created_cards)
        end_turn_relief = 0
        # Current energy is a constraint, not a payoff. Its value comes from
        # the continuation it enables (or Ice Cream retention at END).
        non_block_utility = draw * 2.2
        healing_gain = 0
        non_block_utility += self._poison_value(game, card, target)
        non_block_utility += self._orb_value(game, card_id)

        future_turn_weak_value = 0.0
        future_turn_block_value = 0.0
        future_turn_energy_value = 0.0
        if total_loss <= 0:
            future_turn_weak_value = self._future_turn_weak_value(
                game, card, target
            )
            non_block_utility += future_turn_weak_value
            future_turn_block_value = self._future_turn_block_value(
                game, card
            )
            non_block_utility += future_turn_block_value
            next_turn_energy = self._next_turn_energy_gain(card)
            if next_turn_energy > 0:
                # Delayed energy is a real setup payoff, but it must never
                # inflate the current branch's energy or make an impossible
                # same-turn combo look legal.
                future_turn_energy_value = next_turn_energy * 3.8
                non_block_utility += future_turn_energy_value

        # When the enemy is not threatening this turn, a pure block card has
        # no immediate mitigation to buy.  A Vulnerable attack such as Bash
        # can instead front-load the next turn, provided Artifact will not
        # consume the debuff.  Give that real setup value a modest score bump;
        # it is deliberately gated by current loss and target state so it
        # cannot override urgent defense or waste Bash into Artifact.
        if (
            card_id in self.VULNERABLE_CARDS
            and target is not None
            and total_loss <= 0
            and combat_predictor.power_amount(target, "Artifact") <= 0
            and combat_predictor.power_amount(target, "Vulnerable") <= 0
        ):
            non_block_utility += 10.0

        if card_id == "fission":
            occupied = self._occupied_orbs(game)
            if not occupied:
                # No occupied orb means no draw, energy, evoke, or useful
                # removal. Do not play Fission merely to empty the hand.
                non_block_utility -= 30.0
            elif int(getattr(card, "upgrades", 0) or 0) <= 0:
                non_block_utility -= self._base_fission_opportunity_cost(
                    occupied
                )

        if card_id == "bladedance":
            non_block_utility += max(3, int(getattr(card, "magic_number", 0) or 0)) * 3.2
        elif card_id == "cloakanddagger":
            non_block_utility += max(1, int(getattr(card, "magic_number", 0) or 0)) * 2.8
        elif card_id == "dodgeroll":
            non_block_utility += max(0, int(getattr(card, "block", 0) or 0)) * 0.32
        elif card_id == "blur":
            non_block_utility += min(12, combat_predictor.projected_player_block(game)) * 0.35
        elif card_id == "reaper":
            player = getattr(game, "player", None)
            missing_hp = max(
                0,
                int(getattr(player, "max_hp", 0) or 0)
                - int(getattr(player, "current_hp", 0) or 0),
            )
            healing_gain = min(missing_hp, max(0, damage))
            non_block_utility += healing_gain * 1.4
        elif card_id == "bite":
            player = getattr(game, "player", None)
            missing_hp = max(
                0,
                int(getattr(player, "max_hp", 0) or 0)
                - int(getattr(player, "current_hp", 0) or 0),
            )
            heal = max(2, int(getattr(card, "magic_number", 0) or 0))
            healing_gain = min(missing_hp, heal, max(0, damage))
            non_block_utility += healing_gain * 1.5
        elif card_id in {"bandageup", "selfrepair"}:
            player = getattr(game, "player", None)
            missing_hp = max(
                0,
                int(getattr(player, "max_hp", 0) or 0)
                - int(getattr(player, "current_hp", 0) or 0),
            )
            default_heal = 4 if card_id == "bandageup" else 7
            heal = max(default_heal, int(getattr(card, "magic_number", 0) or 0))
            # Bandage Up heals during this card's own effects. Self Repair is
            # delayed until combat victory and cannot rescue the current turn.
            healing_gain = (
                min(missing_hp, heal) if card_id == "bandageup" else 0
            )
            non_block_utility += min(missing_hp, heal) * 1.5
        elif card_id == "alchemize":
            potions_full = getattr(game, "are_potions_full", lambda: True)()
            if not potions_full:
                non_block_utility += 13
        elif card_id == "geneticalgorithm":
            # Playing it permanently improves future copies even when its
            # current block is not needed on the final turn.
            non_block_utility += 7

        self_damage_events = self._card_self_damage_events(game, card)
        static_outcome = combat_predictor.resolve_player_damage_events(
            game,
            tuple(
                combat_predictor.PlayerDamageEvent(
                    "card_self", amount, blockable=False
                )
                for amount in self_damage_events
            ),
            block=max(0, int(getattr(player, "block", 0) or 0)),
            buffer_layers=combat_predictor.power_amount(player, "Buffer"),
        )
        static_block = static_outcome.block + max(0, block_gain)
        static_buffer = static_outcome.buffer + new_buffer_layers
        static_intangible = (
            card_id in self.INTANGIBLE_CARDS
            or combat_predictor.has_power(
                player, "Intangible", "IntangiblePlayer"
            )
        )
        thorns_outcome = combat_predictor.resolve_player_damage_events(
            game,
            tuple(
                combat_predictor.PlayerDamageEvent(
                    "thorns", amount, blockable=True
                )
                for amount in thorns_damage_events
            ),
            block=static_block,
            buffer_layers=static_buffer,
            force_intangible=static_intangible,
        )
        static_block = thorns_outcome.block + after_image_block
        static_buffer = thorns_outcome.buffer
        sharp_hide_outcome = combat_predictor.resolve_player_damage_events(
            game,
            tuple(
                combat_predictor.PlayerDamageEvent(
                    "sharp_hide", amount, blockable=True
                )
                for amount in sharp_hide_damage_events
            ),
            block=static_block,
            buffer_layers=static_buffer,
            force_intangible=static_intangible,
        )
        static_block = sharp_hide_outcome.block
        static_buffer = sharp_hide_outcome.buffer
        heart_for_events = next(
            (
                monster
                for monster in combat_predictor.active_monsters(game)
                if combat_predictor.power_amount(
                    monster, "Beat of Death", "BeatOfDeathPower"
                ) > 0
            ),
            None,
        )
        beat_outcome = None
        if heart_for_events is not None:
            beat_outcome = combat_predictor.resolve_player_damage_events(
                game,
                (
                    combat_predictor.PlayerDamageEvent(
                        "beat_of_death",
                        combat_predictor.power_amount(
                            heart_for_events,
                            "Beat of Death",
                            "BeatOfDeathPower",
                        ),
                        blockable=True,
                    ),
                ),
                block=static_block,
                buffer_layers=static_buffer,
                force_intangible=static_intangible,
            )
            static_block = beat_outcome.block
            static_buffer = beat_outcome.buffer
        static_clay_hp_loss_events = sum(
            len(outcome.hp_loss_events)
            for outcome in (
                static_outcome,
                thorns_outcome,
                sharp_hide_outcome,
                beat_outcome,
            )
            if outcome is not None
        )
        (
            static_clay_hp_loss_events,
            static_clay_future_block,
            static_clay_credit,
        ) = self._self_forming_clay_value(
            game, static_clay_hp_loss_events
        )
        reactive_hp_cost = (
            thorns_outcome.hp_loss
            + sharp_hide_outcome.hp_loss
            + (beat_outcome.hp_loss if beat_outcome is not None else 0)
        )
        self_damage = static_outcome.hp_loss + reactive_hp_cost
        runic_cube_draws = combat_predictor.runic_cube_draw(
            game,
            sum(
                int(getattr(outcome, "hp_loss", 0) or 0) > 0
                for outcome in (
                    static_outcome,
                    thorns_outcome,
                    sharp_hide_outcome,
                    beat_outcome,
                )
                if outcome is not None
            ),
        )
        if runic_cube_draws > 0:
            hand_additions += runic_cube_draws
            non_block_utility += runic_cube_draws * 2.2
        if self_damage:
            player_hp = int(getattr(getattr(game, "player", None), "current_hp", 0) or 0)
            non_block_utility -= self_damage
            if reactive_hp_cost:
                # Sharp Hide/Thorns is optional attrition, unlike deliberate
                # Ironclad self-damage.  Charge it again (and more at low HP)
                # unless the attack's real kill value outweighs the cost.
                reactive_weight = self._reactive_attrition_weight(
                    game, player_hp
                )
                non_block_utility -= reactive_hp_cost * reactive_weight
            if player_hp <= self_damage:
                non_block_utility -= 1000

        # Spot Weakness grants the same persistent Strength resource as an
        # Inflame when its bound target is attacking.  The ordered transition
        # already prices that Strength for attacks later in this turn, but it
        # also persists across future turns.  Power cards receive a generic
        # lifecycle value below; without an equivalent conditional value the
        # static card ordering can prefer an upgraded Inflame (+3) over an
        # upgraded Spot Weakness (+4) even in the exact same two-card line.
        if card_id == "spotweakness":
            persistent_strength = self._immediate_strength_gain(
                game, card, target, 0
            )
            if persistent_strength > 0:
                non_block_utility += 6.0 + persistent_strength * 3.0

        lifecycle = self._lifecycle_evaluation(
            game, card, total_loss
        ) if card.type == CardType.POWER or card_id in {"ghostly", "apparition"} else None
        if card.type == CardType.POWER or card_id in {"ghostly", "apparition"}:
            if lifecycle is None:
                non_block_utility += 6 + self.POWER_VALUES.get(card_id, 0)
            else:
                # Downside powers do not receive the unconditional generic
                # Power bonus. Their repeated draw/damage/Intangible value is
                # compared directly with the full post-turn liability.
                non_block_utility += lifecycle["adjustment"]

        if getattr(card, "exhausts", False):
            player = getattr(game, "player", None)
            if (
                (
                    combat_predictor.can_gain_block(player)
                    and combat_predictor.has_power(player, "Feel No Pain")
                )
                or combat_predictor.has_power(
                    player, "Dark Embrace", "Corruption"
                )
            ):
                non_block_utility += 3

        priority_bonus = self._priority_bonus(card)
        if (
            non_block_utility <= 0 and card.type == CardType.SKILL
            and (energy_gain > 0 or card_id in {
                "zap", "dualcast", "glacier", "coolheaded",
            })
        ):
            # Ranking preferences must not manufacture resource-only value.
            # Immediate triggers are still evaluated in each branch.
            priority_bonus = 0.0
        if card.type in {CardType.ATTACK, CardType.POWER} or non_block_utility > 0:
            non_block_utility += priority_bonus
        elif not self.priorities.is_card_defensive(card):
            non_block_utility += priority_bonus * 0.55

        base_score += non_block_utility

        # Target preference may separate already-worthwhile actions, but it
        # must never manufacture value for a losing play.  In particular,
        # E2E seq 91937's Hemokinesis had -2.12 intrinsic value yet became
        # positive solely because the only enemy was also the preferred
        # target.  Delay applying this bonus until every real card cost has
        # been charged below.
        target_tiebreak = 0.0
        if target is not None:
            target_tiebreak += self._target_priority(target) * 0.16
            if target is preferred_target:
                target_tiebreak += 3.5

        active_ids = {_token(getattr(monster, "monster_id", "")) for monster in combat_predictor.active_monsters(game)}
        if "gremlinnob" in active_ids and int(getattr(game, "turn", 0) or 0) > 1 and card.type == CardType.SKILL:
            nob = next(
                monster for monster in combat_predictor.active_monsters(game)
                if _token(getattr(monster, "monster_id", "")) == "gremlinnob"
            )
            enrage = max(2, combat_predictor.power_amount(nob, "Enrage", "EnragePower"))
            hits = max(0, int(getattr(nob, "move_hits", 0) or 0)) if getattr(nob, "intent", None).is_attack() else 0
            # Enrage is a real marginal cost, but it is not a flat ban on all
            # skills.  Current-turn added damage plus a small future reserve
            # lets Defend/Leg Sweep win when they prevent more HP than they
            # create and lets damage-generating skills finish the fight.
            base_score -= enrage * hits + (0 if kills else min(2.0, enrage * 0.75))
        if "awakenedone" in active_ids and card.type == CardType.POWER:
            awakened = next(
                monster for monster in combat_predictor.active_monsters(game)
                if _token(getattr(monster, "monster_id", "")) == "awakenedone"
            )
            if (
                combat_predictor.has_power(
                    awakened, "Curiosity", "CuriosityPower"
                )
                and card_id not in {"demonform", "echoform"}
            ):
                base_score -= 16
        # Beat of Death, including block/Buffer consumption and cards which
        # apply Intangible, is resolved in the ordered beam transition. Do not
        # run a second one-card lethal model here: its old ordering could apply
        # a -1000 penalty to the first card of a genuinely surviving line.

        cost = combat_predictor.card_energy_cost(game, card)
        base_score -= cost * 0.12
        positive_before_clay = base_score > 0
        if positive_before_clay:
            # Preserve target focus for useful attacks (including phase
            # knockdowns and setup attacks such as Bash) while keeping it a
            # conditional preference rather than tactical output of its own.
            base_score += target_tiebreak
            target_tiebreak = 0.0
        # Apply Clay only after the intrinsic-positive gate above.  Its
        # bounded 1.35-per-event continuation value must not unlock the much
        # larger preferred-target bonus for an otherwise losing action.
        base_score += static_clay_credit
        effective_mitigation = min(max(0, attack_loss), mitigation)
        order_score = (
            base_score
            + effective_mitigation
            * (
                2.5
                if total_loss >= getattr(game.player, "current_hp", 1)
                else 1.1
            )
            + target_tiebreak
        )
        if kills:
            order_score += 12
        if cost == 0 and positive_before_clay:
            order_score += 1

        champ_transition, champ_transition_ready = self._champ_transition_projection(
            game, card, target, damage
        )
        slime_target = target
        slime_damage = damage
        if target is None and card.type == CardType.ATTACK and raw_damage > 0:
            # AOE cards are serialized without a target.  Project their
            # damage against Slime Boss directly instead of using the summed
            # AOE damage, otherwise Cleave/Whirlwind/Dagger Spray can cross
            # the split before cheaper non-crossing plays are spent.
            slime_target = next(
                (
                    monster
                    for monster in combat_predictor.active_monsters(game)
                    if _token(getattr(monster, "monster_id", "")) == "slimeboss"
                ),
                None,
            )
            if slime_target is not None:
                slime_damage = combat_predictor.attack_hp_loss(
                    slime_target,
                    raw_damage,
                    hits=hits,
                    **combat_predictor.attack_relic_modifiers(game),
                )
        slime_transition = self._slime_transition_projection(slime_target, slime_damage)
        end_turn_damage_events = ()
        end_turn_aoe_damage = (
            max(5, int(getattr(card, "magic_number", 0) or 0))
            if card_id == "combust"
            else 0
        )
        return _Candidate(
            card,
            target,
            base_score,
            mitigation,
            intrinsic_mitigation,
            damage,
            kills,
            order_score,
            block_gain=block_gain,
            orichalcum_block_gain=orichalcum_block_gain,
            orichalcum_single_mitigation=orichalcum_single_mitigation,
            end_turn_relief=end_turn_relief,
            self_hp_cost=self_damage,
            reactive_hp_cost=reactive_hp_cost,
            post_reactive_block=max(0, int(static_block or 0)),
            self_damage_events=tuple(self_damage_events),
            reactive_damage_events=tuple(reactive_damage_events),
            thorns_damage_events=tuple(thorns_damage_events),
            sharp_hide_damage_events=tuple(sharp_hide_damage_events),
            buffer_gain=new_buffer_layers,
            buffer_mitigation=buffer_mitigation,
            state_block_mitigation=state_block_mitigation,
            intangible_mitigation=intangible_mitigation,
            end_turn_damage_events=end_turn_damage_events,
            end_turn_aoe_damage=end_turn_aoe_damage,
            extra_combust_hp_loss=(1 if card_id == "combust" else 0),
            lifecycle_kind=(lifecycle or {}).get("kind", ""),
            expected_remaining_turns=int((lifecycle or {}).get("turns", 0)),
            expected_trigger_count=int((lifecycle or {}).get("triggers", 0)),
            expected_lifecycle_benefit=float(
                (lifecycle or {}).get("benefit", 0.0)
            ),
            expected_lifecycle_cost=float(
                (lifecycle or {}).get("cost", 0.0)
            ),
            lifecycle_adjustment=float(
                (lifecycle or {}).get("adjustment", 0.0)
            ),
            intangible_turns=int(
                (lifecycle or {}).get("intangible_turns", 0)
            ),
            post_intangible_turns=int(
                (lifecycle or {}).get("post_intangible_turns", 0)
            ),
            gambler_alternative=bool(
                (lifecycle or {}).get("gambler_alternative", False)
            ),
            hand_additions=hand_additions,
            runic_cube_draws=runic_cube_draws,
            self_forming_clay_hp_loss_events=(
                static_clay_hp_loss_events
            ),
            self_forming_clay_future_block=static_clay_future_block,
            self_forming_clay_credit=static_clay_credit,
            healing_gain=healing_gain,
            non_block_utility=float(non_block_utility),
            # This is an audit-only value used to explain why a Weak card can
            # be worth playing even when the current turn is already fully
            # blocked.
            future_turn_weak_value=future_turn_weak_value,
            future_turn_block_value=future_turn_block_value,
            future_turn_energy_value=future_turn_energy_value,
            champ_transition=champ_transition,
            champ_transition_ready=champ_transition_ready,
            slime_transition=slime_transition,
            stateful_mitigation=mitigation - neutralization_mitigation,
        )

    def _can_claim_doomed_kill(self, game, card, monster):
        if combat_predictor.has_unresolved_damage_cap(monster):
            return False
        raw_damage, hits = combat_predictor.card_attack_profile(
            game, card, target=monster
        )
        return combat_predictor.attack_hp_loss(
            monster,
            raw_damage,
            hits=hits,
            **combat_predictor.attack_relic_modifiers(game),
        ) >= int(getattr(monster, "current_hp", 0) or 0)

    def _envenom_attack_utility(self, game, card, target):
        """Conservatively value poison revealed after the selected Attack.

        The controller refreshes the authoritative frame after every action,
        so later choices will see the real poison stacks. This estimate is used
        only to rank the first Attack and never mutates HP, proves a kill, or
        assumes every hit of a multi-hit/random attack lands.
        """

        envenom = combat_predictor.power_amount(
            game.player, "Envenom", "EnvenomPower"
        )
        if (
            envenom <= 0
            or getattr(card, "type", None) != CardType.ATTACK
            or _is_random_multi_target_attack(game, card, target)
        ):
            return 0.0
        poison_per_trigger = envenom + int(
            self._has_relic(game, "Snecko Skull")
        )
        targets = (
            [target]
            if target is not None
            else combat_predictor.active_monsters(game)
        )
        value = 0.0
        for monster in targets:
            if (
                monster is None
                or combat_predictor.power_amount(monster, "Artifact") > 0
            ):
                continue
            dealt = combat_predictor.card_attack_hp_loss(
                game, card, monster, target=monster
            )
            hp_after = max(
                0,
                int(getattr(monster, "current_hp", 0) or 0) - int(dealt or 0),
            )
            if dealt <= 0 or hp_after <= 0:
                continue
            existing_poison = combat_predictor.power_amount(monster, "Poison")
            marginal = min(
                poison_per_trigger,
                max(0, hp_after - existing_poison),
            )
            value += marginal * 0.75
        return min(8.0, value)

    def _mummified_hand_power_utility(self, game, card):
        """Value the bounded random discount without inventing its target."""

        if not self._has_relic(game, "Mummified Hand"):
            return 0.0
        eligible_costs = []
        card_uuid = getattr(card, "uuid", None)
        for hand_card in getattr(game, "hand", []) or []:
            if (
                hand_card is card
                or (
                    card_uuid is not None
                    and getattr(hand_card, "uuid", None) == card_uuid
                )
            ):
                continue
            cost = int(getattr(hand_card, "cost", 0) or 0)
            if cost > 0:
                eligible_costs.append(min(3, cost))
        if not eligible_costs:
            return 0.0
        # The target is random, so use expected bounded energy saved only for
        # ranking this Power. The next authoritative frame reveals the real
        # zero-cost card before the planner chooses another action.
        expected_saved = sum(eligible_costs) / len(eligible_costs)
        return min(4.5, expected_saved * 1.5)

    def _target_pool_for_card(self, game, card, active_targets):
        """Extend active targets only when this exact card gains real value."""

        card_id = _token(getattr(card, "card_id", ""))
        if card_id == "spotweakness":
            # Spot Weakness is legal against every living monster, but its
            # effect resolves only when the selected target currently has an
            # attacking intent. Keeping non-attack targets in the beam lets
            # generic setup/tie-break score beat END even though the branch
            # correctly gains zero Strength. Filter those no-op plays at the
            # target boundary (live seed 1127161324318756364, F31/F46).
            return [
                monster for monster in active_targets
                if (
                    getattr(monster, "intent", None) is not None
                    and getattr(monster, "intent", None).is_attack()
                )
            ]
        if card_id in (self.POISON_CARDS | {"catalyst"}) - {"corpseexplosion"}:
            # A poison application has no marginal value on a monster whose
            # current poison/passive effects already guarantee its death at
            # the end of this turn.  Filter this at target-pool construction,
            # not only in _poison_value: when all enemies are passively doomed
            # the planner temporarily falls back to the full living set so it
            # can spend a genuinely profitable final-turn card (for example
            # Corpse Explosion, Bite, or Reaper).  Without this filter a
            # positive target tiebreak could still select Deadly Poison on the
            # doomed target, exactly as seen in the live Gremlin Leader trace.
            doomed_ids = {
                id(monster)
                for monster in combat_predictor.projected_doomed_monsters(game)
            }
            active_targets = [
                monster
                for monster in active_targets
                if id(monster) not in doomed_ids
            ]
        if card_id not in self.DOOMED_TARGET_BENEFIT_CARDS:
            return active_targets
        living = combat_predictor.living_monsters(game)
        active_ids = {id(monster) for monster in active_targets}
        doomed = [monster for monster in living if id(monster) not in active_ids]
        if card_id == "corpseexplosion":
            extras = doomed
        elif card_id in self.ON_KILL_BENEFIT_CARDS:
            extras = [
                monster
                for monster in doomed
                if self._can_claim_doomed_kill(game, card, monster)
            ]
        elif card_id == "bite":
            player = getattr(game, "player", None)
            missing_hp = max(
                0,
                int(getattr(player, "max_hp", 0) or 0)
                - int(getattr(player, "current_hp", 0) or 0),
            )
            raw_damage, hits = combat_predictor.card_attack_profile(game, card)
            extras = [
                monster
                for monster in doomed
                if missing_hp > 0
                and combat_predictor.attack_hp_loss(
                    monster,
                    raw_damage,
                    hits=hits,
                    **combat_predictor.attack_relic_modifiers(game),
                ) > 0
            ]
        else:
            extras = []
        return list(active_targets) + extras

    def _build_candidates(self, game, playable_cards, monsters, preferred_target, incoming, attack_loss, total_loss):
        groups = []
        for card in playable_cards:
            if getattr(card, "has_target", False):
                card_id = _token(getattr(card, "card_id", ""))
                target_pool = self._target_pool_for_card(game, card, monsters)
                candidates = [
                    self._candidate(game, card, target, preferred_target, incoming, attack_loss, total_loss)
                    for target in target_pool
                ]
            else:
                candidates = [self._candidate(game, card, None, preferred_target, incoming, attack_loss, total_loss)]
            groups.append(candidates)

        # If a useful setup card is in hand, play it before an unsafe phase
        # crossing.  The game state is authoritative after that play, so the
        # next planner call sees the setup card gone and may cross normally;
        # this avoids both premature Execute and an indefinite combat stall.
        flat_candidates = [candidate for group in groups for candidate in group]
        preparation_available = any(
            self._is_champ_preparation_candidate(candidate)
            for candidate in flat_candidates
        )
        if preparation_available:
            for candidate in flat_candidates:
                if candidate.champ_transition and not candidate.champ_transition_ready:
                    candidate.base_score -= self.CHAMP_TRANSITION_PENALTY
                    candidate.order_score -= self.CHAMP_TRANSITION_PENALTY
                    candidate.champ_transition_penalized = True

        poison_setup_available = any(
            _token(getattr(candidate.card, "card_id", ""))
            in (self.POISON_CARDS - {"noxiousfumes"})
            and _token(getattr(candidate.card, "card_id", "")) != "catalyst"
            and candidate.base_score > 0.75
            for candidate in flat_candidates
        )
        if poison_setup_available:
            for candidate in flat_candidates:
                if _token(getattr(candidate.card, "card_id", "")) == "catalyst":
                    candidate.order_score -= 18

        followup_attack_available = any(
            getattr(candidate.card, "type", None) == CardType.ATTACK
            and _token(getattr(candidate.card, "card_id", "")) not in self.VULNERABLE_CARDS
            and candidate.damage > 0
            for candidate in flat_candidates
        )
        if followup_attack_available:
            for candidate in flat_candidates:
                card_id = _token(getattr(candidate.card, "card_id", ""))
                if card_id not in self.VULNERABLE_CARDS:
                    continue
                if candidate.target is not None and combat_predictor.power_amount(candidate.target, "Vulnerable") > 0:
                    continue
                candidate.order_score += 9
        return groups

    def _multi_enemy_flask_setup_override(self, game, groups, plan):
        """Return Bouncing Flask when its random Poison must be observed.

        Against several enemies the beam deliberately cannot invent which
        targets receive Bouncing Flask's three random applications.  If it
        executes Catalyst first, however, the planner permanently loses the
        chance to amplify the authoritative Flask result.  Play only Flask,
        then let the next CommunicationMod frame choose Catalyst's real
        target.  Hard card-play limits and a deterministic survival kill keep
        Catalyst authoritative when the second action would not be available
        or delaying it would be unsafe.
        """

        if not plan or _token(
            getattr(plan[0].card, "card_id", "")
        ) != "catalyst":
            return None
        if len(combat_predictor.active_monsters(game)) <= 1:
            return None

        catalyst = plan[0]
        energy = max(0, int(getattr(game.player, "energy", 0) or 0))
        catalyst_cost = combat_predictor.card_energy_cost(
            game, catalyst.card
        )
        flask_candidates = [
            candidate
            for group in groups
            for candidate in group
            if _token(getattr(candidate.card, "card_id", ""))
            == "bouncingflask"
            and candidate.base_score > 0.75
            and catalyst_cost
            + combat_predictor.card_energy_cost(game, candidate.card)
            <= energy
        ]
        if not flask_candidates:
            return None

        # Both cards must remain legally playable.  Otherwise a Time Warp,
        # Velvet Choker, or Normality boundary can turn "setup first" into an
        # immediate forced END before Catalyst.
        time_warp_remaining = self._time_warp_remaining(game)
        if time_warp_remaining is not None and time_warp_remaining < 2:
            return None
        choker_remaining = self._velvet_choker_remaining(game)
        if choker_remaining is not None and choker_remaining < 2:
            return None
        if (
            int(self._confirmed_card_resolutions or 0) >= 2
            and any(
                _token(getattr(card, "card_id", "")) == "normality"
                for card in getattr(game, "hand", []) or []
            )
        ):
            return None

        single_search = getattr(self, "_last_single_card_search", {})
        original_details = dict(getattr(self, "_last_search", {}) or {})
        catalyst_details = single_search.get(id(catalyst), ({}, 0.0))[0]
        if not catalyst_details:
            return None

        safe_flasks = []
        for flask in flask_candidates:
            flask_details = single_search.get(id(flask), ({}, 0.0))[0]
            if not flask_details:
                continue
            catalyst_loss = int(
                catalyst_details.get("actual_loss", 0) or 0
            )
            flask_loss = int(flask_details.get("actual_loss", 0) or 0)
            catalyst_kills = int(
                catalyst_details.get("enemies_dead", 0) or 0
            )
            flask_kills = int(flask_details.get("enemies_dead", 0) or 0)
            deterministic_finish = bool(
                catalyst_details.get("true_combat_end")
                and not flask_details.get("true_combat_end")
            )
            survival_kill = (
                catalyst_kills > flask_kills
                and catalyst_loss < flask_loss
            )
            original_finish = bool(
                original_details.get("true_combat_end")
                and not flask_details.get("true_combat_end")
            )
            original_tier = float(original_details.get("tier", 0) or 0)
            flask_tier = float(flask_details.get("tier", 0) or 0)
            original_loss = int(
                original_details.get("actual_loss", 0) or 0
            )
            original_kills = int(
                original_details.get("enemies_dead", 0) or 0
            )
            if (
                deterministic_finish
                or survival_kill
                or original_finish
                or original_tier > flask_tier
                or original_loss < flask_loss
                or original_kills > flask_kills
            ):
                continue
            safe_flasks.append(flask)

        if not safe_flasks:
            return None
        return max(
            safe_flasks,
            key=lambda candidate: (
                candidate.base_score,
                -combat_predictor.card_energy_cost(game, candidate.card),
            ),
        )

    def _profitable_doomed_cards(self, game, playable_cards, living):
        """Return final-turn cards whose cross-combat value is deterministic."""

        doomed = combat_predictor.projected_doomed_monsters(game)
        if not doomed:
            return []
        player = getattr(game, "player", None)
        missing_hp = max(
            0,
            int(getattr(player, "max_hp", 0) or 0)
            - int(getattr(player, "current_hp", 0) or 0),
        )
        profitable = []
        for card in playable_cards:
            card_id = _token(getattr(card, "card_id", ""))
            if card_id not in self.DOOMED_RESOURCE_CARDS:
                continue
            if card_id in self.ON_KILL_BENEFIT_CARDS:
                useful = any(
                    self._can_claim_doomed_kill(game, card, monster)
                    for monster in doomed
                )
            elif card_id == "bite":
                raw_damage, hits = combat_predictor.card_attack_profile(game, card)
                useful = missing_hp > 0 and any(
                    combat_predictor.attack_hp_loss(
                        monster,
                        raw_damage,
                        hits=hits,
                        **combat_predictor.attack_relic_modifiers(game),
                    ) > 0
                    for monster in doomed
                )
            elif card_id == "reaper":
                raw_damage, hits = combat_predictor.card_attack_profile(game, card)
                useful = missing_hp > 0 and any(
                    combat_predictor.attack_hp_loss(
                        monster,
                        raw_damage,
                        hits=hits,
                        **combat_predictor.attack_relic_modifiers(game),
                    ) > 0
                    for monster in living
                )
            elif card_id in {"bandageup", "selfrepair"}:
                useful = missing_hp > 0
            elif card_id == "alchemize":
                useful = not getattr(game, "are_potions_full", lambda: True)()
            elif card_id == "geneticalgorithm":
                useful = True
            else:
                useful = False
            if useful:
                profitable.append(card)
        return profitable

    def _current_single_card_true_kill(self, game, playable_cards, target):
        """Return whether an ordinary affordable Attack can end this combat."""

        if target is None or combat_predictor.has_unresolved_damage_cap(target):
            return False
        target_hp = max(0, int(getattr(target, "current_hp", 0) or 0))
        if target_hp <= 0:
            return False
        energy = max(0, int(getattr(game.player, "energy", 0) or 0))
        for card in playable_cards:
            card_id = _token(getattr(card, "card_id", ""))
            if (
                card_id in self.ON_KILL_BENEFIT_CARDS
                or getattr(card, "type", None) != CardType.ATTACK
                or combat_predictor.card_energy_cost(game, card) > energy
            ):
                continue
            raw_damage, hits = combat_predictor.card_attack_profile(
                game, card, target=target
            )
            if raw_damage <= 0 or hits <= 0:
                continue
            dealt = combat_predictor.attack_hp_loss(
                target,
                raw_damage,
                hits=hits,
                **combat_predictor.attack_relic_modifiers(game),
            )
            if dealt >= target_hp and self._is_fatal_kill(game, target, 0):
                return True
        return False

    def _safe_feed_fatal_wait(
        self,
        game,
        playable_cards,
        active,
        turn_outcome,
        lethal_combo,
    ):
        """Return a top-deck Feed when one zero-loss wait is deterministic.

        This is deliberately narrower than a general multi-turn stall.  It
        waits only for the very next draw, against one enemy which cannot act
        or change state materially this turn.  That keeps Feed farming from
        trading HP, Buffer, debuffs, statuses, summons, healing, or scaling
        uncertainty for three max HP.
        """

        living = combat_predictor.living_monsters(game)
        if len(living) != 1 or len(active) != 1:
            return None
        target = active[0]
        if target is not living[0]:
            return None
        if int(getattr(game, "act", 0) or 0) >= 4:
            # The Heart is the terminal fight; permanent max HP has no later
            # encounter in which to repay even a nominal opportunity cost.
            return None
        if _token(getattr(target, "monster_id", "")) in {
            "corruptheart", "theheart", "heart",
        }:
            return None
        if getattr(target, "intent", None) not in {
            Intent.NONE, Intent.SLEEP, Intent.STUN,
        }:
            return None
        if int(getattr(turn_outcome, "total_hp_loss", 0) or 0) != 0:
            return None
        if target in combat_predictor.projected_doomed_monsters(game):
            return None
        if int(getattr(target, "block", 0) or 0) > 0:
            return None
        if combat_predictor.has_power(
            target,
            "Regeneration", "RegenerationPower",
            "Metallicize", "MetallicizePower",
            "Plated Armor", "PlatedArmor", "PlatedArmorPower",
            "Malleable", "MalleablePower",
            "Mode Shift", "ModeShift",
            "Flight", "FlightPower",
            "Intangible", "IntangiblePower",
            "Barricade", "Curl Up", "CurlUpPower",
        ):
            return None
        if combat_predictor.has_power(
            game.player,
            "No Draw", "NoDrawPower", "Confusion", "ConfusionPower",
        ):
            return None

        # Do not replace a permanent reward which is already claimable now.
        for card in playable_cards:
            if (
                _token(getattr(card, "card_id", ""))
                in self.ON_KILL_BENEFIT_CARDS
                and self._can_claim_doomed_kill(game, card, target)
                and self._is_fatal_kill(game, target, 0)
            ):
                return None

        current_lethal = bool(lethal_combo) or self._current_single_card_true_kill(
            game, playable_cards, target
        )
        if not current_lethal:
            return None

        draw_pile = list(getattr(game, "draw_pile", []) or [])
        if not draw_pile:
            return None
        # CommunicationMod serializes the top of the draw pile at the end.
        if getattr(game, "draw_pile_order_known", True) is not True:
            return None
        feed = draw_pile[-1]
        if _token(getattr(feed, "card_id", "")) != "feed":
            return None
        # Even a retained hand must have a real slot for the next draw.
        if len(getattr(game, "hand", []) or []) >= 10:
            return None
        feed_cost = combat_predictor.card_energy_cost(game, feed)
        if feed_cost < 0 or feed_cost > 3:
            return None
        if not self._can_claim_doomed_kill(game, feed, target):
            return None
        if not self._is_fatal_kill(game, target, 0):
            return None
        return feed

    def _guaranteed_attack_combo(self, game, playable_cards, active, total_loss, end_turn_loss):
        """Find a direct-damage combination that deterministically removes a threat.

        The bounded value DP intentionally does not mutate each monster's HP.
        Without this narrow exact-lethal pass, two modest attacks can each
        lose to a block card even though together they end a lethal combat.
        """

        attacks = [
            card for card in playable_cards
            if getattr(card, "type", None) == CardType.ATTACK
            and combat_predictor.card_attack_profile(game, card)[0] > 0
            and int(getattr(card, "cost", 0) or 0) != -1
        ]
        if not attacks or not active:
            return None
        if max(0, int(total_loss or 0)) > 0 and len(active) > 1 and any(
            getattr(card, "type", None) == CardType.ATTACK
            and not getattr(card, "has_target", False)
            and combat_predictor.card_attack_profile(game, card)[0] > 0
            for card in playable_cards
        ):
            # The aggregate shortcut proves damage only against its selected
            # target.  It cannot compare that kill with a global attack which
            # removes other attackers and therefore lowers end-turn HP loss.
            # Leave multi-enemy allocation to the ordered beam, which resolves
            # every target and can still choose the same single-target combo
            # when it is genuinely the better line.
            return None
        if (
            len(attacks) >= 2
            and combat_predictor.pen_nib_ready(game)
        ):
            # Pen Nib is a one-shot modifier, but CommunicationMod may expose
            # its ready power by pre-doubling every Attack in the visible
            # hand.  This aggregate shortcut cannot safely distinguish that
            # serialization from the relic-counter-only form: summing the
            # visible damage can therefore spend Pen Nib once per card.  The
            # ordered beam already consumes the bonus after the first Attack,
            # so fail closed here and let that exact sequential model decide.
            return None
        energy = max(0, int(getattr(game.player, "energy", 0) or 0))
        player_hp = max(1, int(getattr(game.player, "current_hp", 0) or 0))
        can_gain_block = combat_predictor.can_gain_block(game)

        max_cards = len(attacks)
        choker_remaining = self._velvet_choker_remaining(game)
        if choker_remaining is not None:
            max_cards = min(max_cards, choker_remaining)
        time_eater = next(
            (
                monster for monster in active
                if _token(getattr(monster, "monster_id", "")) == "timeeater"
            ),
            None,
        )
        if time_eater is not None:
            played = combat_predictor.power_amount(
                time_eater, "Time Warp", "TimeWarpPower"
            )
            max_cards = min(max_cards, 12 - played if 0 <= played < 12 else 12)

        solutions = []
        for target in active:
            if combat_predictor.has_unresolved_damage_cap(target):
                continue
            if self._writhing_mass_has_compulsive(target):
                # A multi-card "guaranteed" combo is not guaranteed once the
                # first positive nonlethal hit rerolls this monster's intent.
                # Leave it to the ordered branch, which executes one action
                # and then requires an authoritative refresh.
                continue
            target_id = _token(getattr(target, "monster_id", ""))
            reactive_barrier = combat_predictor.has_power(
                target,
                "Curl Up",
                "CurlUpPower",
                "Malleable",
                "MalleablePower",
                "Mode Shift",
                "ModeShift",
                "Flight",
                "FlightPower",
            )
            target_hp = int(getattr(target, "current_hp", 0) or 0)
            for mask in range(1, 1 << len(attacks)):
                chosen = [
                    attacks[index] for index in range(len(attacks))
                    if mask & (1 << index)
                ]
                if any(
                    _token(getattr(card, "card_id", ""))
                    in self.ON_KILL_BENEFIT_CARDS
                    for card in chosen
                ):
                    # Aggregate combo damage cannot prove which card delivers
                    # Fatal.  Let the ordered beam preserve the reward and
                    # model every hit instead of choosing Feed as a nonfatal
                    # first action merely because its raw damage is high.
                    continue
                # Single-card kills already go through the ordinary candidate
                # path. This branch exists only for cumulative lethal and must
                # not aggregate across barriers that trigger after each card.
                if len(chosen) < 2 or reactive_barrier:
                    continue
                if len(chosen) > max_cards:
                    continue
                fiend_fires = [
                    card for card in chosen
                    if _token(getattr(card, "card_id", "")) == "fiendfire"
                ]
                # The first Fiend Fire exhausts every other card in hand, so
                # two copies can never both belong to one guaranteed combo.
                if len(fiend_fires) > 1:
                    continue
                chosen = sorted(
                    chosen,
                    key=lambda item: (
                        _token(getattr(item, "card_id", "")) == "fiendfire",
                        _token(getattr(item, "card_id", ""))
                        not in self.VULNERABLE_CARDS,
                    ),
                )
                cost = sum(combat_predictor.card_energy_cost(game, card) for card in chosen)
                if cost > energy:
                    continue
                attack_modifiers = combat_predictor.attack_relic_modifiers(
                    game
                )
                if int(attack_modifiers.get("minimum_damage", 0) or 0) > 0:
                    # The Boot is applied after each hit consumes Block.  This
                    # aggregate shortcut does not preserve that packet order;
                    # the ordered beam below does.
                    continue
                if combat_predictor.has_power(target, "Slow", "SlowPower"):
                    # Slow changes after every card play, and the eventual
                    # first action can be reordered for debuff application.
                    # Keep that stateful multiplier in the ordered beam.
                    continue
                adjusted_attack_damage = 0
                combo_vulnerable = combat_predictor.has_power(
                    target, "Vulnerable"
                )
                self_cost = 0
                reactive_cost = 0
                buffer_layers = combat_predictor.power_amount(
                    game.player, "Buffer"
                )
                player_block = max(
                    0, int(getattr(game.player, "block", 0) or 0)
                )
                player_intangible = combat_predictor.has_power(
                    game.player, "Intangible", "IntangiblePlayer"
                )
                beat_source = next(
                    (
                        monster
                        for monster in active
                        if combat_predictor.power_amount(
                            monster,
                            "Beat of Death",
                            "BeatOfDeathPower",
                        )
                        > 0
                    ),
                    None,
                )
                combo_fan = next(
                    (
                        relic
                        for relic in getattr(game, "relics", []) or []
                        if _token(getattr(relic, "relic_id", ""))
                        == "ornamentalfan"
                    ),
                    None,
                )
                combo_fan_counter = (
                    max(0, int(getattr(combo_fan, "counter", 0) or 0))
                    if combo_fan is not None
                    else 0
                )
                for combo_index, card in enumerate(chosen):
                    card_id = _token(getattr(card, "card_id", ""))
                    if card_id == "fiendfire":
                        # Every other selected attack must be played first.
                        # Those cards have left the hand when Fiend Fire
                        # counts/exhausts its remaining targets.
                        hits = max(
                            0,
                            len(getattr(game, "hand", []) or []) - len(chosen),
                        )
                        raw = (
                            max(0, int(getattr(card, "damage", 0) or 0))
                            * hits
                        )
                    else:
                        raw, hits = combat_predictor.card_attack_profile(
                            game, card, target=target
                        )
                    weak_target_per_hit = self._weak_target_per_hit_damage(
                        game,
                        card,
                        target,
                        raw,
                        max(1, hits),
                        vulnerable_override=combo_vulnerable,
                    )
                    if weak_target_per_hit is not None:
                        if combat_predictor.is_intangible(target):
                            adjusted = (
                                max(1, hits)
                                if weak_target_per_hit > 0
                                else 0
                            )
                        else:
                            adjusted = weak_target_per_hit * max(1, hits)
                    else:
                        adjusted = (
                            combat_predictor.attack_damage_after_target_modifiers(
                                target,
                                raw,
                                hits=max(1, hits),
                                vulnerable_override=combo_vulnerable,
                                **attack_modifiers,
                            )
                        )
                    adjusted_attack_damage += adjusted
                    # Deliberately do not credit Vulnerable newly applied by
                    # this combo.  Ignoring that later benefit is conservative
                    # and still proves already-sufficient Bash+Strike lines;
                    # stateful debuff benefits remain in the ordered beam.
                    self_outcome = combat_predictor.resolve_player_damage_events(
                        game,
                        tuple(
                            combat_predictor.PlayerDamageEvent(
                                "card_self", amount, blockable=False
                            )
                            for amount in self._card_self_damage_events(
                                game, card
                            )
                        ),
                        block=player_block,
                        buffer_layers=buffer_layers,
                        force_intangible=player_intangible,
                    )
                    self_cost += self_outcome.hp_loss
                    player_block = self_outcome.block
                    buffer_layers = self_outcome.buffer
                    serialized_base_block = getattr(
                        card, "base_block", None
                    )
                    if can_gain_block and (
                        serialized_base_block is None
                        or int(serialized_base_block or 0) >= 0
                    ):
                        player_block += max(
                            0, int(getattr(card, "block", 0) or 0)
                        )
                    thorns_events, sharp_hide_events = (
                        self._card_reactive_damage_events(
                            game,
                            card,
                            target if getattr(card, "has_target", False) else None,
                            hits,
                            active_targets=active,
                        )
                    )
                    thorns_outcome = combat_predictor.resolve_player_damage_events(
                        game,
                        tuple(
                            combat_predictor.PlayerDamageEvent(
                                "thorns", amount, blockable=True
                            )
                            for amount in thorns_events
                        ),
                        block=player_block,
                        buffer_layers=buffer_layers,
                        force_intangible=player_intangible,
                    )
                    player_block = thorns_outcome.block
                    if can_gain_block:
                        player_block += (
                            combat_predictor.power_amount(
                                game.player, "After Image", "AfterImagePower"
                            )
                            + combat_predictor.power_amount(
                                game.player, "Rage", "RagePower"
                            )
                            + (
                                4
                                if combo_fan is not None
                                and (
                                    combo_fan_counter + combo_index + 1
                                )
                                % 3
                                == 0
                                else 0
                            )
                        )
                    buffer_layers = thorns_outcome.buffer
                    sharp_outcome = combat_predictor.resolve_player_damage_events(
                        game,
                        tuple(
                            combat_predictor.PlayerDamageEvent(
                                "sharp_hide", amount, blockable=True
                            )
                            for amount in sharp_hide_events
                        ),
                        block=player_block,
                        buffer_layers=buffer_layers,
                        force_intangible=player_intangible,
                    )
                    card_reactive_cost = (
                        thorns_outcome.hp_loss + sharp_outcome.hp_loss
                    )
                    player_block = sharp_outcome.block
                    buffer_layers = sharp_outcome.buffer
                    reactive_cost += card_reactive_cost
                    self_cost += card_reactive_cost
                    if beat_source is not None:
                        beat_outcome = (
                            combat_predictor.resolve_player_damage_events(
                                game,
                                (
                                    combat_predictor.PlayerDamageEvent(
                                        "beat_of_death",
                                        combat_predictor.power_amount(
                                            beat_source,
                                            "Beat of Death",
                                            "BeatOfDeathPower",
                                        ),
                                        blockable=True,
                                    ),
                                ),
                                block=player_block,
                                buffer_layers=buffer_layers,
                                force_intangible=player_intangible,
                            )
                        )
                        self_cost += beat_outcome.hp_loss
                        reactive_cost += beat_outcome.hp_loss
                        player_block = beat_outcome.block
                        buffer_layers = beat_outcome.buffer
                if any(
                    combat_predictor.attack_hp_loss(
                        target,
                        *combat_predictor.card_attack_profile(
                            game, card, target=target
                        ),
                        **combat_predictor.attack_relic_modifiers(game),
                    ) >= target_hp
                    for card in chosen
                ):
                    # Ordinary one-card lethal scoring already handles this.
                    # Requiring an unnecessary second attack would hijack the
                    # turn ordering and falsely label the pair a combo.
                    continue
                # Vulnerable, Intangible, and per-hit rounding apply inside
                # each card, while existing Block is shared across the whole
                # sequence.  Aggregating raw damage/hit counts first changes
                # Pummel+Strike from 40+16 into a false 58-point lethal.
                dealt = max(
                    0,
                    adjusted_attack_damage
                    - max(0, int(getattr(target, "block", 0) or 0)),
                )
                if dealt < target_hp or self_cost >= player_hp:
                    continue

                if self._has_affordable_combo_block_line(
                    game, chosen, reactive_cost
                ):
                    # Do not let the exact-lethal shortcut bypass a legal
                    # block-first line.  The ordinary ordered beam can then
                    # compare Defend/Flame Barrier against the same attacks
                    # and account for every reaction packet in sequence.
                    continue

                ends_combat = (
                    len(combat_predictor.living_monsters(game)) == 1
                    and not (
                        target_id == "awakenedone"
                        and combat_predictor.has_power(
                            target,
                            "Unawakened",
                            "UnawakenedPower",
                            "Curiosity",
                            "CuriosityPower",
                        )
                    )
                )
                if any(
                    _token(getattr(card, "card_id", "")) == "fiendfire"
                    for card in chosen
                ):
                    remaining_hand = []
                else:
                    chosen_ids = {id(card) for card in chosen}
                    remaining_hand = [
                        card
                        for card in getattr(game, "hand", []) or []
                        if id(card) not in chosen_ids
                    ]
                combo_forced_end = bool(
                    time_eater is not None
                    and len(chosen) >= max_cards
                )
                combo_attack_bonus = (
                    {id(time_eater): 2}
                    if combo_forced_end and time_eater is not target
                    else {}
                )
                combo_outcome = combat_predictor.projected_turn_outcome(
                    game,
                    excluded_monsters=[target],
                    active_monsters_override=[
                        monster for monster in active if monster is not target
                    ],
                    block_override=player_block,
                    buffer_override=buffer_layers,
                    hand_override=remaining_hand,
                    hand_size_override=len(remaining_hand),
                    per_hit_damage_bonus=combo_attack_bonus,
                    force_intangible=player_intangible,
                    combat_ends_before_next_turn=ends_combat,
                    player_hp_override=max(0, player_hp - self_cost),
                )
                loss_without_target = combo_outcome.total_hp_loss
                retained_hp = player_hp - self_cost
                if not ends_combat:
                    retained_hp -= loss_without_target
                if (
                    reactive_cost > 0
                    and retained_hp
                    < self._reactive_safety_reserve(game, player_hp)
                ):
                    # The exact-lethal shortcut runs before the bounded turn
                    # search, so it must enforce the same low-HP reserve as
                    # terminal Spiker/Sharp Hide scoring. Include damage from
                    # every enemy that survives the combo, not only Thorns.
                    continue
                if (
                    not ends_combat
                    and self_cost + loss_without_target >= player_hp
                ):
                    continue
                solutions.append((
                    -combat_predictor.monster_threat(target),
                    len(chosen),
                    cost,
                    dealt - target_hp,
                    target,
                    chosen,
                    {
                        "projected_loss": int(
                            0 if ends_combat else loss_without_target
                        ),
                        "actual_loss": int(
                            self_cost
                            + (0 if ends_combat else loss_without_target)
                        ),
                        # This shortcut runs before the ordinary beam search,
                        # but consumers still need the same authoritative
                        # terminal proof.  In particular, potion policy must
                        # not treat a guaranteed full-combat lethal as another
                        # turn in which delayed Regeneration can pay out.
                        "true_combat_end": bool(ends_combat),
                        "enemies_dead": 1,
                        "tier": 2,
                        "forced_end": combo_forced_end,
                        "player_buffer": int(buffer_layers),
                        "final_player_block": int(player_block),
                        "reactive_hp_cost": int(reactive_cost),
                        "combo_card_ids": [
                            getattr(item, "card_id", None) for item in chosen
                        ],
                        "combo_card_uuids": [
                            getattr(item, "uuid", None) for item in chosen
                        ],
                    },
                ))

        if not solutions:
            return None
        _, _, cost, _, target, chosen, combo_details = min(
            solutions, key=lambda item: item[:4]
        )
        self._last_combo_search = combo_details
        first_candidates = [
            card for card in chosen
            if _token(getattr(card, "card_id", "")) != "fiendfire"
        ] or chosen
        first = max(
            first_candidates,
            key=lambda card: (
                _token(getattr(card, "card_id", "")) in self.VULNERABLE_CARDS,
                -combat_predictor.card_energy_cost(game, card),
                combat_predictor.card_attack_profile(game, card)[0],
            ),
        )
        return first, target if getattr(first, "has_target", False) else None, cost, len(chosen)

    @staticmethod
    def _orichalcum_candidate_components(candidate):
        raw_block = max(
            0, int(getattr(candidate, "orichalcum_block_gain", 0) or 0)
        )
        if raw_block <= 0:
            return candidate.mitigation, 0
        single_block_mitigation = int(
            getattr(candidate, "orichalcum_single_mitigation", 0) or 0
        )
        return candidate.mitigation - single_block_mitigation, raw_block

    @staticmethod
    def _orichalcum_block_effect(game, attack_loss, raw_block):
        if raw_block <= 0:
            return 0
        loss_with_new_block = combat_predictor.projected_attack_hp_loss(
            game,
            block_override=raw_block,
        )
        return attack_loss - loss_with_new_block

    def _plan_attack_mitigation(self, game, plan, attack_loss):
        other_mitigation = 0
        raw_orichalcum_block = 0
        for candidate in plan:
            candidate_other, candidate_block = (
                self._orichalcum_candidate_components(candidate)
            )
            other_mitigation += candidate_other
            raw_orichalcum_block += candidate_block
        total = other_mitigation + self._orichalcum_block_effect(
            game,
            attack_loss,
            raw_orichalcum_block,
        )
        return min(attack_loss, max(0, total))

    def _initial_direct_score(self, game, candidate):
        """Direct-damage part already embedded in a candidate's static score."""

        card = candidate.card
        raw_damage, hits = combat_predictor.card_attack_profile(game, card)
        if getattr(card, "type", None) != CardType.ATTACK or raw_damage <= 0:
            return 0.0
        targets = (
            [candidate.target]
            if candidate.target is not None
            else combat_predictor.active_monsters(game)
        )
        value = 0.0
        for monster in targets:
            dealt = combat_predictor.attack_hp_loss(
                monster,
                raw_damage,
                hits=hits,
                **combat_predictor.attack_relic_modifiers(game),
            )
            hp = max(0, int(getattr(monster, "current_hp", 0) or 0))
            value += min(hp, dealt)
            if dealt >= hp and not combat_predictor.has_unresolved_damage_cap(monster):
                if candidate.target is not None:
                    value += 8 + combat_predictor.monster_threat(monster) * 0.35
                else:
                    value += 7 + combat_predictor.monster_threat(monster) * 0.25
        return value

    @staticmethod
    def _state_card_cost(
        card, energy, corruption=False, bullet_time=False,
    ):
        if bullet_time:
            return 0
        if corruption and getattr(card, "type", None) == CardType.SKILL:
            return 0
        cost = int(getattr(card, "cost", 0) or 0)
        return energy if cost == -1 else max(0, cost)

    @staticmethod
    def _immediate_strength_gain(game, card, target, current_bonus):
        """Return deterministic same-turn Strength granted by ``card``.

        Delayed powers such as Demon Form deliberately do not appear here.
        The game remains authoritative after every action; this only lets the
        bounded whole-turn search value the order of immediate setup cards.
        """

        card_id = _token(getattr(card, "card_id", ""))
        amount = max(0, int(getattr(card, "magic_number", 0) or 0))
        if card_id in {"flex", "inflame", "jax"}:
            defaults = {"flex": 2, "inflame": 2, "jax": 2}
            return max(defaults[card_id], amount)
        if card_id == "spotweakness":
            intent = getattr(target, "intent", None)
            if (
                target is None
                or intent is None
                or not intent.is_attack()
            ):
                return 0
            return max(3, amount)
        if card_id == "limitbreak":
            current_strength = combat_predictor.signed_power_amount(
                getattr(game, "player", None), "Strength"
            )
            return max(0, current_strength + int(current_bonus or 0))
        return 0

    @staticmethod
    def _reactive_enemy_strength_gain(monster, card, monster_hp):
        """Strength gained when this exact card is played.

        Curiosity reacts to a Power only while its owner is alive.  This
        distinction matters for Awakened One: an attack which drops phase one
        to zero before a later Power prevents that Power from strengthening
        the boss, while moving the Power ahead of the attack does not.

        Gremlin Nob's Anger/Enrage is the analogous Skill reaction.  It must
        live in the ordered branch rather than only in a static card penalty:
        every Defend makes the current attack and all later turns stronger.
        """

        if max(0, int(monster_hp or 0)) <= 0:
            return 0
        card_type = getattr(card, "type", None)
        if card_type == CardType.POWER:
            return combat_predictor.power_amount(
                monster, "Curiosity", "CuriosityPower"
            )
        if (
            card_type == CardType.SKILL
            and _token(getattr(monster, "monster_id", "")) == "gremlinnob"
        ):
            return combat_predictor.power_amount(
                monster, "Anger", "Enrage", "EnragePower"
            )
        return 0

    @staticmethod
    def _curiosity_strength_gain(monster, card, monster_hp):
        """Compatibility helper for the narrower Curiosity transition."""

        if (
            max(0, int(monster_hp or 0)) <= 0
            or getattr(card, "type", None) != CardType.POWER
        ):
            return 0
        return combat_predictor.power_amount(
            monster, "Curiosity", "CuriosityPower"
        )

    def _strength_damage_bonus_per_hit(self, game, monster, strength_gain):
        """Conservative displayed-damage delta for new enemy Strength.

        Serialized intent damage already contains the enemy's current Weak
        and the player's current Vulnerable multiplier.  Curiosity Strength
        is applied before those multipliers, so adding its raw amount after
        the serialized value can underestimate damage against a Vulnerable
        player.  The ceiling is the safe marginal value when game rounding
        could make the exact delta one point smaller.
        """

        numerator = max(0, int(strength_gain or 0))
        denominator = 1
        if numerator <= 0:
            return 0
        if combat_predictor.has_power(monster, "Weak", "Weakened"):
            numerator *= 3
            denominator *= 5 if self._has_relic(game, "Paper Krane") else 4
        if combat_predictor.has_power(
            getattr(game, "player", None), "Vulnerable"
        ):
            numerator *= 5 if self._has_relic(game, "Odd Mushroom") else 3
            denominator *= 4 if self._has_relic(game, "Odd Mushroom") else 2
        return (numerator + denominator - 1) // denominator

    def _branch_enemy_attack_damage_delta(
        self,
        game,
        monster,
        *,
        strength_gain=0,
        strength_reduction=0,
        weak_amount=0,
        player_vulnerable=False,
    ):
        """Signed per-hit delta from exact branch enemy debuffs/buffs.

        CommunicationMod's displayed damage is authoritative for the current
        frame. Recompute only the marginal change from branch Strength/Weak
        state so unknown encounter modifiers remain in that baseline. Weak
        is floored per hit before Block and therefore correctly controls
        Static Discharge, Painful Stabs, and Suck.
        """

        strength_gain = max(0, int(strength_gain or 0))
        strength_reduction = max(0, int(strength_reduction or 0))
        authoritative_weak = combat_predictor.has_power(
            monster, "Weak", "Weakened"
        )
        branch_weak = max(0, int(weak_amount or 0)) > 0
        authoritative_player_vulnerable = combat_predictor.has_power(
            getattr(game, "player", None), "Vulnerable"
        )
        branch_player_vulnerable = bool(
            authoritative_player_vulnerable or player_vulnerable
        )
        if (
            strength_gain <= 0
            and strength_reduction <= 0
            and branch_weak == authoritative_weak
            and branch_player_vulnerable
            == authoritative_player_vulnerable
        ):
            return 0

        serialized = max(
            0, int(getattr(monster, "move_adjusted_damage", 0) or 0)
        )
        authoritative_strength = combat_predictor.signed_power_amount(
            monster, "Strength", "StrengthPower"
        )
        player_intangible = combat_predictor.has_power(
            getattr(game, "player", None),
            "Intangible",
            "IntangiblePlayer",
        )
        base_damage = int(
            getattr(monster, "move_base_damage", 0) or 0
        )
        if base_damage <= 0:
            # Offline fixtures often omit move_base_damage. Invert the known
            # authoritative Weak/Vulnerable multipliers (rounding upward) to
            # recover a conservative pre-multiplier value: displayed seven
            # under Weak, for example, came from raw ten rather than seven.
            multiplier_numerator = 1
            multiplier_denominator = 1
            if authoritative_weak:
                multiplier_numerator *= 3
                multiplier_denominator *= (
                    5 if self._has_relic(game, "Paper Krane") else 4
                )
            if authoritative_player_vulnerable:
                multiplier_numerator *= (
                    5 if self._has_relic(game, "Odd Mushroom") else 3
                )
                multiplier_denominator *= (
                    4 if self._has_relic(game, "Odd Mushroom") else 2
                )
            raw_with_strength = (
                serialized * multiplier_denominator
                + multiplier_numerator
                - 1
            ) // multiplier_numerator
            base_damage = max(
                0, raw_with_strength - authoritative_strength
            )

        def adjusted(raw_damage, weak_active, vulnerable_active):
            raw_damage = max(0, int(raw_damage or 0))
            numerator = raw_damage
            denominator = 1
            if weak_active:
                numerator *= 3
                denominator *= (
                    5 if self._has_relic(game, "Paper Krane") else 4
                )
            if vulnerable_active:
                numerator *= (
                    5 if self._has_relic(game, "Odd Mushroom") else 3
                )
                denominator *= (
                    4 if self._has_relic(game, "Odd Mushroom") else 2
                )
            value = numerator // denominator
            if player_intangible and value > 0:
                value = 1
            return value

        authoritative_raw = max(
            0, base_damage + authoritative_strength
        )
        branch_raw = max(
            0,
            authoritative_raw + strength_gain - strength_reduction,
        )
        modeled_before = adjusted(
            authoritative_raw,
            authoritative_weak,
            authoritative_player_vulnerable,
        )
        modeled_after = adjusted(
            branch_raw,
            branch_weak,
            branch_player_vulnerable,
        )
        # Apply only the modeled marginal delta to the serialized value. When
        # an unknown encounter modifier made the authoritative number differ
        # from our baseline, scale only where retaining the raw delta would
        # become unsafe: amplify positive deltas under an unknown multiplier,
        # and shrink negative deltas toward zero under an unknown reducer.
        delta = modeled_after - modeled_before
        if modeled_before > 0 and serialized != modeled_before:
            if delta > 0 and serialized > modeled_before:
                delta = (
                    delta * serialized + modeled_before - 1
                ) // modeled_before
            elif delta < 0 and serialized < modeled_before:
                delta = -(
                    abs(delta) * serialized // modeled_before
                )
        return delta

    @staticmethod
    def _same_turn_strength_damage_bonus(game, card, strength_bonus, hits):
        """Conservatively project damage from Strength gained in this plan."""

        strength_bonus = max(0, int(strength_bonus or 0))
        hits = max(0, int(hits or 0))
        if strength_bonus <= 0 or hits <= 0:
            return 0
        card_id = _token(getattr(card, "card_id", ""))
        multiplier = 1
        if card_id == "heavyblade":
            multiplier = max(
                3, int(getattr(card, "magic_number", 0) or 0)
            )
        per_hit = strength_bonus * multiplier
        if combat_predictor.has_power(
            getattr(game, "player", None), "Weak", "Weakened"
        ):
            # Weak rounds each hit down independently.  Existing serialized
            # damage already contains the old Strength contribution, so only
            # add the conservative marginal part here.
            per_hit = int(per_hit * 0.75)
        return max(0, per_hit) * hits

    @staticmethod
    def _weak_target_per_hit_damage(
        game, card, monster, serialized_damage, hits, *,
        vulnerable_override=None, extra_slow_cards=0,
    ):
        """Recover exact per-hit damage across Weak and target multipliers.

        CommunicationMod's ``card.damage`` is already floored after the
        player's Weak modifier.  Reapplying target Vulnerable to that rounded
        value is not equivalent to the game's one full multiplier chain:
        Base damage plus ordinary Strength is evaluated before the one full
        Weak/Vulnerable multiplier chain.  For example, base nine with one
        Strength under Weak and Vulnerable is
        ``floor((9 + 1) * .75 * 1.5) == 11``, not
        ``floor(floor((9 + 1) * .75) * 1.5) == 10``.  Use this reconstruction
        only when the authoritative base/dynamic pair proves that no other
        player-side damage modifier is hidden; every other case stays on the
        existing conservative serialized path.
        """

        if getattr(card, "type", None) != CardType.ATTACK:
            return None
        return combat_predictor.weak_attack_per_hit_after_target_modifiers(
            game,
            card,
            monster,
            serialized_damage,
            hits,
            vulnerable_override=vulnerable_override,
            extra_slow_cards=extra_slow_cards,
        )

    def _current_attack_hp_loss(self, game, card, monster, raw_damage, hits):
        """Return one current-card HP packet from the shared exact helper."""

        profile_raw, profile_hits = combat_predictor.card_attack_profile(
            game, card, target=monster
        )
        if (
            max(0, int(raw_damage or 0))
            == max(0, int(profile_raw or 0))
            and max(1, int(hits or 1))
            == max(1, int(profile_hits or 1))
        ):
            hp_loss = combat_predictor.card_attack_hp_loss(
                game, card, monster, target=monster,
            )
        else:
            hp_loss = combat_predictor.attack_hp_loss(
                monster,
                raw_damage,
                hits=hits,
                **combat_predictor.attack_relic_modifiers(game),
            )
        # This helper is consumed as an observable HP delta, not as an
        # uncapped damage packet. Overkill cannot remove more HP than the
        # target currently has.
        return min(
            max(0, int(hp_loss or 0)),
            max(0, int(getattr(monster, "current_hp", 0) or 0)),
        )

    @staticmethod
    def _random_attack_hp_loss(game, card, monsters):
        """Conservatively total hidden random Attack packets.

        The returned value is aggregate HP loss for the card, not damage to a
        bound target.  A deterministic witness assigns each packet to a
        different live enemy; it is used only for first-action accounting and
        never establishes a terminal kill.
        """

        targets = list(monsters or [])
        if not targets:
            return 0
        raw_damage, hits = combat_predictor.card_attack_profile(
            game, card
        )
        hits = max(1, int(hits or 1))
        per_hit, remainder = divmod(max(0, int(raw_damage or 0)), hits)
        return sum(
            min(
                combat_predictor.attack_hp_loss(
                    monster,
                    per_hit + int(hit_index < remainder),
                    hits=1,
                    **combat_predictor.attack_relic_modifiers(game),
                ),
                max(0, int(getattr(monster, "current_hp", 0) or 0)),
            )
            for hit_index, monster in enumerate(targets[:hits])
        )

    @staticmethod
    def _branch_dexterity_block_gain(game, raw_gain, extra_dexterity):
        """Return a branch-local Dexterity delta after player debuffs."""

        return combat_predictor.block_after_extra_dexterity(
            game,
            raw_gain,
            extra_dexterity,
        )

    def _apply_enemy_damage_packet(
        self,
        monster,
        index,
        hp,
        block,
        damage_value,
        mode_shift_remaining,
        neutralized,
        amount,
        *,
        blockable,
        minimum_unblocked_damage=0,
    ):
        """Apply one ordered enemy HP-loss packet and immediate transitions.

        Every source uses this path so Guardian's Mode Shift can add its 20
        Block between attacks, orb hits, Choke, poison, Combust, and Corpse
        Explosion packets.  ``blockable=False`` represents direct HP loss
        such as Choke/poison; those packets still reduce the Mode Shift
        counter because the power is based on HP lost.
        """

        amount = max(0, int(amount or 0))
        if hp[index] <= 0 or amount <= 0:
            return 0
        # Intangible caps both ordinary damage and direct HP loss (Poison,
        # Choke) per event.  Block is consulted only for blockable packets.
        if combat_predictor.is_intangible(monster):
            amount = min(amount, 1)
        if blockable:
            absorbed = min(block[index], amount)
            block[index] -= absorbed
            unblocked = max(0, amount - absorbed)
            # The Boot floors each positive post-Block Attack packet, not
            # the raw packet.  This distinction was observed directly in the
            # live trace (12 into 11 Block dealt five), and must stay inside
            # the ordered hit path so multi-hit cards consume Block first.
            if minimum_unblocked_damage > 0 and unblocked > 0:
                unblocked = max(
                    unblocked, int(minimum_unblocked_damage)
                )
            dealt = min(hp[index], unblocked)
        else:
            dealt = min(hp[index], amount)
        if combat_predictor.has_unresolved_damage_cap(monster):
            dealt = min(dealt, max(0, hp[index] - 1))
        if dealt <= 0:
            return 0

        hp[index] -= dealt
        damage_value[index] += dealt
        if mode_shift_remaining[index] > 0:
            mode_shift_remaining[index] = max(
                0, mode_shift_remaining[index] - dealt
            )
            if mode_shift_remaining[index] == 0:
                neutralized.add(index)
                if hp[index] > 0:
                    block[index] += 20
        if self._transition_suppresses_action(
            monster, hp[index], damage_value[index]
        ):
            neutralized.add(index)
        return dealt

    def _resolve_state_deaths(
        self,
        game,
        monsters,
        hp,
        block,
        corpse,
        damage_value,
        *,
        mode_shift_remaining=None,
        neutralized=None,
    ):
        """Resolve deterministic Corpse Explosion cascades after one card."""

        hp = list(hp)
        block = list(block)
        damage_value = list(damage_value)
        mode_shift_remaining = (
            mode_shift_remaining
            if mode_shift_remaining is not None
            else [0 for _ in monsters]
        )
        neutralized = neutralized if neutralized is not None else set()
        exploded = set()
        while True:
            sources = [
                index for index, value in enumerate(hp)
                # Rebirth/revival prevents Fatal and combat completion, but
                # the zero-HP event still fires on-death powers such as
                # Corpse Explosion.
                if value <= 0
                and corpse[index] > 0
                and index not in exploded
            ]
            if not sources:
                break
            for source in sources:
                exploded.add(source)
                explosion_layers = max(0, int(corpse[source] or 0))
                # Consume the debuff in the branch state.  Terminal scoring
                # invokes death resolution after several ordered end-turn
                # phases; leaving it nonzero would detonate the same corpse
                # again after Lightning, Combust, and poison.
                if isinstance(corpse, list):
                    corpse[source] = 0
                raw = (
                    max(
                        0,
                        int(
                            getattr(monsters[source], "max_hp", 0) or 0
                        ),
                    )
                    * explosion_layers
                )
                for index, monster in enumerate(monsters):
                    if index == source or hp[index] <= 0:
                        continue
                    self._apply_enemy_damage_packet(
                        monster,
                        index,
                        hp,
                        block,
                        damage_value,
                        mode_shift_remaining,
                        neutralized,
                        raw,
                        blockable=True,
                    )
        return tuple(hp), tuple(block), tuple(damage_value)

    @staticmethod
    def _state_orb_hit(
        monster, hp, block, amount, *, lock_on_amount=0
    ):
        """Apply one deterministic orb hit to simulated monster state.

        Orb damage is ordinary damage: Intangible caps it before block.  The
        protocol does not expose Invincible's remaining per-turn allowance, so
        an orb may damage such a target but must never claim a guaranteed kill.
        """

        amount = combat_predictor.orb_damage_after_lock_on(
            monster,
            amount,
            lock_on_override=lock_on_amount,
        )
        if combat_predictor.is_intangible(monster) and amount > 0:
            amount = 1
        absorbed = min(max(0, int(block or 0)), amount)
        remaining_block = max(0, int(block or 0)) - absorbed
        dealt = min(max(0, int(hp or 0)), max(0, amount - absorbed))
        if combat_predictor.has_unresolved_damage_cap(monster):
            dealt = min(dealt, max(0, int(hp or 0) - 1))
        return dealt, remaining_block

    def _resolve_damage_orb_evokes(
        self,
        game,
        monsters,
        hp,
        block,
        corpse,
        damage_value,
        mode_shift_remaining,
        neutralized,
        orb,
        evoke_count,
        *,
        electrodynamics=False,
        amount_override=None,
        lock_on=None,
        uncertain_damage=None,
    ):
        """Resolve deterministic Lightning/Dark evocations into branch state.

        Ordinary multi-enemy Lightning is random, so it contributes expected
        score without claiming a target or kill.  Electrodynamics makes that
        same packet deterministic against every living enemy and can safely
        advance HP, phase transitions, and Corpse Explosion cascades.
        """

        hp = list(hp)
        block = list(block)
        damage_value = list(damage_value)
        orb_id = self._orb_id(orb)
        amount = (
            self._orb_evoke(orb)
            if amount_override is None
            else max(0, int(amount_override or 0))
        )
        total_value = 0.0
        killed = False
        if orb_id not in {"lightning", "dark"} or amount <= 0:
            return hp, block, damage_value, total_value, killed

        for _ in range(max(0, int(evoke_count or 0))):
            alive = [index for index, value in enumerate(hp) if value > 0]
            if not alive:
                break
            if orb_id == "dark":
                targets = [min(alive, key=lambda item: (hp[item], item))]
            elif electrodynamics:
                targets = list(alive)
            elif len(alive) == 1:
                targets = [alive[0]]
            else:
                # Without Electrodynamics no concrete target is guaranteed.
                # Preserve expected damage for ranking, but leave the exact
                # enemy state untouched so the branch cannot invent a kill.
                possible = [
                    self._state_orb_hit(
                        monsters[index],
                        hp[index],
                        block[index],
                        amount,
                        lock_on_amount=(
                            lock_on[index]
                            if lock_on is not None
                            and index < len(lock_on)
                            else combat_predictor.lock_on_amount(
                                monsters[index]
                            )
                        ),
                    )[0]
                    for index in alive
                ]
                expected_damage = sum(possible) / max(1, len(possible))
                total_value += expected_damage
                if uncertain_damage is not None:
                    # The concrete target is intentionally left untouched,
                    # but the aggregate expected loss is still real signal
                    # for first-action resource comparisons and telemetry.
                    uncertain_damage[0] += expected_damage
                continue

            hp_before = tuple(hp)
            for index in targets:
                if hp[index] <= 0:
                    continue
                target_amount = combat_predictor.orb_damage_after_lock_on(
                    monsters[index],
                    amount,
                    lock_on_override=(
                        lock_on[index]
                        if lock_on is not None and index < len(lock_on)
                        else None
                    ),
                )
                total_value += self._apply_enemy_damage_packet(
                    monsters[index],
                    index,
                    hp,
                    block,
                    damage_value,
                    mode_shift_remaining,
                    neutralized,
                    target_amount,
                    blockable=True,
                )
            killed = killed or any(
                before > 0 and hp[index] <= 0
                for index, before in enumerate(hp_before)
            )
            hp, block, damage_value = self._resolve_state_deaths(
                game,
                monsters,
                hp,
                block,
                corpse,
                damage_value,
                mode_shift_remaining=mode_shift_remaining,
                neutralized=neutralized,
            )
            hp = list(hp)
            block = list(block)
            damage_value = list(damage_value)

        return hp, block, damage_value, total_value, killed

    @staticmethod
    def _orb_consumes_front(card_id):
        return card_id in {"consume", "dualcast", "multicast"}

    @staticmethod
    def _orb_requires_occupied(card_id):
        return card_id in {
            "barrage", "consume", "dualcast", "fission", "multicast", "recursion",
            "redo",
        }

    @staticmethod
    def _reactive_safety_reserve(game, player_hp):
        max_player_hp = max(
            player_hp,
            int(getattr(game.player, "max_hp", player_hp) or player_hp),
        )
        max_hp_reserve = max(1, (max_player_hp * 15 + 99) // 100)
        injured_reserve = max(2, (player_hp + 3) // 4)
        return min(
            max(0, player_hp - 1),
            max_hp_reserve,
            injured_reserve,
        )

    @staticmethod
    def _static_card_block_gain(game, card):
        """Return a conservative immediate Block estimate for a card.

        This helper is intentionally separate from the full candidate model:
        it is used before the ordered beam exists to decide whether an attack
        into Thorns/Sharp Hide has an affordable block-first alternative.
        Serialized attack cards occasionally carry a stale positive ``block``
        field; ``base_block`` is the capability guard that rejects those.
        """

        base_block = getattr(card, "base_block", None)
        if base_block is not None and int(base_block or 0) < 0:
            return 0
        card_id = _token(getattr(card, "card_id", ""))
        if card_id == "entrench":
            return max(
                0,
                int(getattr(getattr(game, "player", None), "block", 0) or 0),
            )
        if card_id == "reinforcedbody":
            return max(0, int(getattr(card, "block", 0) or 0)) * max(
                1, int(getattr(getattr(game, "player", None), "energy", 0) or 0)
            )
        if card_id == "secondwind":
            per_card = max(
                0,
                int(getattr(card, "block", 0) or 0),
            ) or 5 + 2 * int(getattr(card, "upgrades", 0) or 0)
            return per_card * sum(
                1
                for hand_card in getattr(game, "hand", []) or []
                if hand_card is not card
                and getattr(hand_card, "type", None) != CardType.ATTACK
            )
        return max(0, int(getattr(card, "block", 0) or 0))

    def _branch_has_pre_reactive_block(self, state):
        """Whether this exact ordered branch already blocks before reaction."""

        generated_block = 0
        same_turn_after_image = 0
        same_turn_rage = 0
        for candidate in state.plan:
            card_id = _token(getattr(candidate.card, "card_id", ""))
            if candidate.reactive_damage_events and (
                generated_block > 0
                or same_turn_after_image > 0
                or same_turn_rage > 0
            ):
                return True
            generated_block += max(0, int(getattr(candidate, "block_gain", 0) or 0))
            if card_id == "afterimage":
                same_turn_after_image += max(
                    1, int(getattr(candidate.card, "magic_number", 0) or 0)
                )
            elif card_id == "rage":
                same_turn_rage += max(
                    3, int(getattr(candidate.card, "magic_number", 0) or 0)
                )
        return False

    def _has_affordable_reactive_block_line(self, game, state):
        """Check for an unused block card that can precede this branch.

        The result is deliberately conservative: any legal positive Block
        source with spare energy counts.  The terminal simulator still prices
        the exact block and reaction packets; this predicate only prevents a
        direct lethal line from receiving the special ``finish before Thorns``
        bonus when the hand can safely play Defend/Flame Barrier first.
        """

        if not state.reactive_hp_cost or self._branch_has_pre_reactive_block(state):
            return False
        if not any(candidate.reactive_damage_events for candidate in state.plan):
            return False
        for index in state.remaining_hand_indexes:
            if index < 0 or index >= len(getattr(game, "hand", []) or []):
                continue
            card = game.hand[index]
            if not getattr(card, "is_playable", False):
                continue
            cost = self._state_card_cost(
                card,
                state.energy,
                corruption=state.player_corruption,
                bullet_time=state.player_bullet_time,
            )
            if cost > state.energy:
                continue
            if self._static_card_block_gain(game, card) <= 0:
                continue
            return True
        return False

    def _has_affordable_combo_block_line(self, game, chosen, reactive_cost):
        """Whether a direct attack combo can be preceded by a Block card."""

        if reactive_cost <= 0 or not combat_predictor.can_gain_block(game):
            return False
        chosen_ids = {id(card) for card in chosen}
        energy_left = max(0, int(getattr(game.player, "energy", 0) or 0)) - sum(
            combat_predictor.card_energy_cost(game, card) for card in chosen
        )
        if energy_left <= 0:
            return False
        for card in getattr(game, "hand", []) or []:
            if id(card) in chosen_ids or not getattr(card, "is_playable", False):
                continue
            if combat_predictor.card_energy_cost(game, card) > energy_left:
                continue
            if self._static_card_block_gain(game, card) > 0:
                return True
        return False

    def _echo_form_duplicates_first_card(self, game, state=None):
        """Return whether Echo Form repeats the card resolving now.

        CommunicationMod exposes Echo Form as a player power rather than as a
        second action.  Its repeat applies only to the first manually played
        card of the turn, so a whole-turn branch must charge reactive damage
        twice on its first transition but never on later transitions.
        """
        # The serialized power amount is the number of remaining card
        # resolutions in the authoritative frame.  Only speculative cards
        # added inside this branch consume that counter; confirmed cards have
        # already been reflected by CommunicationMod.
        # ``cards_played`` counts every complete useCard resolution because
        # Time Warp/Beat/Ink/other hooks also see an autoplayed copy.  Echo's
        # remaining-card counter is different: it qualifies the first one or
        # two cards selected from hand, so use the concrete branch plan.
        branch_cards_played = (
            int(self._confirmed_cards_played or 0)
            + len(getattr(state, "plan", ()) or ())
            if state is not None
            else int(self._confirmed_cards_played or 0)
        )
        echo_layers = combat_predictor.power_amount(
            getattr(game, "player", None), "Echo Form", "EchoFormPower"
        )
        # Echo Form+ repeats each of the first two manually played cards; it
        # does not resolve the first card three times.  The power amount is a
        # remaining-card counter, while ``branch_cards_played`` is local
        # progress from the authoritative frame.
        return branch_cards_played < max(0, int(echo_layers or 0))

    def _play_turn_candidate(
        self,
        game,
        state,
        group_index,
        candidate,
        monsters,
        target_indexes,
        active_indexes,
        time_warp_remaining,
        total_loss,
    ):
        """Advance one search branch using serialized, deterministic effects."""

        if state.forced_end or group_index in state.used:
            return None
        choker_remaining = self._velvet_choker_remaining(
            game,
            self._confirmed_card_resolutions + state.cards_played,
            branch_cards_played=state.cards_played,
        )
        if choker_remaining is not None and choker_remaining <= 0:
            return None
        card = candidate.card
        card_id = _token(getattr(card, "card_id", ""))
        if choker_remaining == 1:
            # The sixth card is the last legal play.  A draw/energy-only card
            # such as Offering or Battle Trance cannot convert its resource
            # into another action and should not be selected just because its
            # nominal priority is high.  Keep cards with an immediate effect
            # (damage, block, mitigation, healing, or Feel No Pain exhaust
            # value) eligible.
            resource_only = (
                card_id in self.DRAW_COUNTS
                or card_id in {"seek", "bullettime"}
                or self._energy_gain(card, game) > 0
            )
            exhaust_block = (
                bool(getattr(card, "exhausts", False))
                and combat_predictor.can_gain_block(game)
                and combat_predictor.has_power(
                    game.player, "Feel No Pain", "FeelNoPainPower"
                )
            )
            has_immediate_effect = (
                candidate.damage > 0
                or candidate.mitigation > 0
                or candidate.block_gain > 0
                or candidate.buffer_gain > 0
                or candidate.intrinsic_mitigation > 0
                or candidate.end_turn_relief > 0
                or candidate.healing_gain > 0
                or exhaust_block
            )
            if resource_only and not has_immediate_effect:
                return None
        cost = self._state_card_cost(
            card,
            state.energy,
            corruption=state.player_corruption,
            bullet_time=state.player_bullet_time,
        )
        if cost > state.energy:
            return None
        seek_target_value = 0.0
        if card_id == "seek":
            seek_target_value = self._best_seek_target_value(
                game,
                energy=max(0, state.energy - cost),
                corruption=state.player_corruption,
                bullet_time=state.player_bullet_time,
            )
            if seek_target_value <= 0.0:
                # A tutor with no executable payoff only burns a card play
                # and can force a bad Grid selection. Re-plan from the
                # authoritative frame instead of spending Seek late.
                return None
        raw_cost = int(getattr(card, "cost", 0) or 0)
        if raw_cost == -1 and state.energy <= 0:
            # X cards are technically playable at zero energy, but most of
            # them resolve zero times.  Treating the relic-side utility of a
            # zero-effect Multi-Cast/Whirlwind as positive caused the Defect
            # to spend its turn on an empty attack while a live threat was
            # still present.  Chemical X and upgraded X cards are retained:
            # their non-zero X effect is calculated with the branch-local
            # energy, not the stale turn-start value.
            x_effect = self._x_effect(
                game,
                card,
                upgraded_bonus=True,
                energy_override=state.energy,
            )
            immediate_effect = any(
                value > 0
                for value in (
                    candidate.damage,
                    candidate.mitigation,
                    candidate.block_gain,
                    candidate.buffer_gain,
                    candidate.intrinsic_mitigation,
                    candidate.end_turn_relief,
                    candidate.hand_additions,
                    self._energy_gain(card, game, orbs=state.orbs),
                    candidate.healing_gain,
                    candidate.future_turn_weak_value,
                    candidate.future_turn_block_value,
                    candidate.future_turn_energy_value,
                )
            )
            if x_effect <= 0 or (
                not immediate_effect
                and self._orb_requires_occupied(card_id)
                and not state.orbs
            ):
                return None
        target_index = target_indexes.get(id(candidate.target)) if candidate.target is not None else None
        if target_index is not None and state.hp[target_index] <= 0:
            return None

        hp = list(state.hp)
        hp_before_card = tuple(hp)
        block = list(state.block)
        poison = list(state.poison)
        artifact = list(state.artifact)
        vulnerable = list(state.vulnerable)
        weak = list(state.weak)
        lock_on = list(
            state.lock_on or tuple(0 for _ in monsters)
        )
        corpse = list(state.corpse_explosion)
        damage_value = list(state.damage_value)
        poison_value = list(state.poison_value)
        choke = list(
            state.choke or tuple(0 for _ in monsters)
        )
        mode_shift_remaining = list(
            state.mode_shift_remaining or tuple(0 for _ in monsters)
        )
        curl_up_block = list(
            state.curl_up_block or tuple(0 for _ in monsters)
        )
        malleable_next_block = list(
            state.malleable_next_block or tuple(0 for _ in monsters)
        )
        pending_malleable_triggers = [0 for _ in monsters]
        def settle_pending_malleable():
            for malleable_index, trigger_count in enumerate(
                pending_malleable_triggers
            ):
                if hp[malleable_index] <= 0:
                    pending_malleable_triggers[malleable_index] = 0
                    continue
                for _ in range(trigger_count):
                    block[malleable_index] += malleable_next_block[
                        malleable_index
                    ]
                    malleable_next_block[malleable_index] += 1
                pending_malleable_triggers[malleable_index] = 0
        flight_stacks = list(
            state.flight_stacks or tuple(0 for _ in monsters)
        )
        orbs = tuple(state.orbs)
        player_artifact = state.player_artifact
        player_buffer = state.player_buffer
        player_block = state.player_block
        player_no_block = state.player_no_block
        player_intangible = state.player_intangible
        player_vulnerable = state.player_vulnerable
        player_hp_state = state.player_hp
        player_rage = state.player_rage
        player_after_image = state.player_after_image
        player_accuracy = state.player_accuracy
        player_thousand_cuts = state.player_thousand_cuts
        player_sadistic_nature = state.player_sadistic_nature
        player_feel_no_pain = state.player_feel_no_pain
        player_dark_embrace = state.player_dark_embrace
        player_corruption = state.player_corruption
        player_bullet_time = state.player_bullet_time
        player_heatsinks = state.player_heatsinks
        player_storm = state.player_storm
        player_double_tap = state.player_double_tap
        player_burst = state.player_burst
        player_duplication = state.player_duplication
        player_pen_nib = state.player_pen_nib
        player_akabeko_ready = state.player_akabeko_ready
        player_dexterity_bonus = state.player_dexterity_bonus
        player_focus_bonus = state.player_focus_bonus
        player_electrodynamics = state.player_electrodynamics
        player_static_discharge = state.player_static_discharge
        player_thorns = state.player_thorns
        player_flame_barrier = state.player_flame_barrier
        player_rupture = state.player_rupture
        player_hex = state.player_hex
        rupture_trigger_count = 0
        rupture_strength_gain = 0
        player_strength_bonus = state.player_strength_bonus
        orb_slots = state.orb_slots
        requested_resolution_copies = (
            1
            + int(self._echo_form_duplicates_first_card(game, state))
            + int(
                getattr(card, "type", None) == CardType.ATTACK
                and player_double_tap > 0
            )
            + int(
                getattr(card, "type", None) == CardType.SKILL
                and player_burst > 0
            )
            + int(
                getattr(card, "type", None)
                in {CardType.ATTACK, CardType.SKILL, CardType.POWER}
                and player_duplication > 0
            )
        )
        # Autoplayed copies are complete useCard resolutions and fire the
        # normal player/relic/monster hooks.  Before an autoplayed copy enters
        # useCard it still passes card.canUse: Time Warp clears the queue at
        # twelve, and Velvet Choker/Normality reject a copy after the original
        # reaches their sixth/third-card boundary.
        resolution_copies = requested_resolution_copies
        if time_warp_remaining is not None:
            use_events_until_warp = max(
                0, int(time_warp_remaining) - int(state.cards_played)
            )
            if use_events_until_warp <= 0:
                return None
            resolution_copies = min(
                resolution_copies, use_events_until_warp
            )
        if choker_remaining is not None:
            resolution_copies = min(
                resolution_copies, max(0, int(choker_remaining))
            )
        normality_in_hand = any(
            _token(getattr(game.hand[index], "card_id", ""))
            == "normality"
            for index in state.remaining_hand_indexes
        )
        if normality_in_hand:
            resolution_copies = min(
                resolution_copies,
                max(0, 3 - int(state.normality_cards_played)),
            )
        if resolution_copies <= 0:
            return None
        if (
            resolution_copies > 1
            and getattr(card, "type", None) == CardType.ATTACK
            and card_id in {
                "rampage",
                "claw",
                "glassknife",
                "bodyslam",
                "finisher",
            }
        ):
            # These attacks derive their damage from mutable state which can
            # change between complete useCard resolutions.  Until that state
            # is represented in the ordered branch, treating the serialized
            # first-use damage as reusable would fabricate an exact copy.
            return None
        if (
            resolution_copies > 1
            and getattr(card, "type", None) == CardType.ATTACK
            and target_index is not None
            and not combat_predictor.has_unresolved_damage_cap(
                monsters[target_index]
            )
        ):
            # A queued targeted copy calls canUse again.  If the first use is
            # guaranteed to leave its target dying, the copy is rejected and
            # must not receive body effects, hooks, or card-play counters.
            # Keep this preflight deliberately narrow (single-hit, no
            # changing reactive defense) so uncertain deaths remain in the
            # exact loop rather than truncating a legal copy.
            pre_raw, pre_hits = combat_predictor.card_attack_profile(
                game,
                card,
                energy_override=state.energy,
                target=candidate.target,
                poisoned_override=(
                    poison[target_index] > 0
                    if card_id == "bane"
                    else None
                ),
            )
            pre_raw += self._same_turn_strength_damage_bonus(
                game, card, player_strength_bonus, pre_hits
            )
            if vulnerable[target_index] > 0:
                pre_raw = int(
                    pre_raw
                    * combat_predictor.vulnerable_damage_multiplier(game)
                )
            stable_single_hit = (
                int(pre_hits or 0) == 1
                and combat_predictor.power_amount(
                    monsters[target_index], "Flight", "FlightPower"
                ) <= 0
                and combat_predictor.power_amount(
                    monsters[target_index], "Malleable", "MalleablePower"
                ) <= 0
                and combat_predictor.power_amount(
                    monsters[target_index], "Curl Up", "CurlUpPower"
                ) <= 0
            )
            if (
                stable_single_hit
                and max(0, pre_raw - block[target_index])
                >= hp[target_index]
            ):
                resolution_copies = 1
        # Most deterministic resources repeat once per full resolution;
        # cards which consume a whole mutable zone must instead be advanced
        # explicitly below.
        planned_resolution_copies = resolution_copies
        draw_resolution_copies = (
            1
            if card_id in {"battletrance", "fission", "reboot"}
            else resolution_copies
        )
        energy_resolution_copies = (
            1
            if card_id in {"concentrate", "fission"}
            else resolution_copies
        )
        # Every successful autoplay copy executes the normal useCard hooks.
        # A copied Storm/Heat Sinks sees the stack installed by its preceding
        # resolution, although the first resolution never triggers the new
        # stack it is only about to create.
        if getattr(card, "type", None) == CardType.POWER:
            storm_layers = max(0, int(player_storm or 0))
            storm_lightning_channels = (
                storm_layers * resolution_copies
                + (
                    resolution_copies * (resolution_copies - 1) // 2
                    if card_id == "storm"
                    else 0
                )
            )
            heatsinks_layers = max(0, int(player_heatsinks or 0))
            heatsinks_stack_gain = (
                max(
                    0,
                    int(getattr(card, "magic_number", 0) or 0),
                )
                if card_id == "heatsinks"
                else 0
            )
            if card_id == "heatsinks" and heatsinks_stack_gain <= 0:
                heatsinks_stack_gain = 1 + int(
                    getattr(card, "upgrades", 0) or 0
                )
            heatsinks_hook_draw = (
                heatsinks_layers * resolution_copies
                + (
                    heatsinks_stack_gain
                    * resolution_copies
                    * (resolution_copies - 1)
                    // 2
                    if card_id == "heatsinks"
                    else 0
                )
            )
            if int(state.draw_pile_size or 0) >= 0:
                heatsinks_hook_draw = min(
                    heatsinks_hook_draw,
                    max(0, int(state.draw_pile_size or 0)),
                )
        else:
            storm_lightning_channels = 0
            heatsinks_hook_draw = 0
        hex_dazed_count = (
            max(0, int(player_hex or 0)) * resolution_copies
            if getattr(card, "type", None) != CardType.ATTACK
            else 0
        )
        # Electrodynamics applies before its Channel actions.  This matters
        # when the newly channelled Lightning overflows a full queue and
        # immediately evokes an existing Lightning orb.
        if card_id == "electrodynamics":
            player_electrodynamics = True
        if card_id == "staticdischarge":
            player_static_discharge += max(
                1, int(getattr(card, "magic_number", 0) or 0)
            ) * resolution_copies
        if card_id == "caltrops":
            player_thorns += max(
                3 + 2 * int(getattr(card, "upgrades", 0) or 0),
                int(getattr(card, "magic_number", 0) or 0),
            ) * resolution_copies
        elif card_id == "flamebarrier":
            player_flame_barrier += max(
                4 + 2 * int(getattr(card, "upgrades", 0) or 0),
                int(getattr(card, "magic_number", 0) or 0),
            ) * resolution_copies
        unconsumed_temporary_strength = (
            state.unconsumed_temporary_strength
        )
        enemy_strength_bonus = list(
            state.enemy_strength_bonus
            or tuple(0 for _ in monsters)
        )
        enemy_strength_reduction = list(
            state.enemy_strength_reduction
            or tuple(0 for _ in monsters)
        )
        neutralized = set(state.neutralized)
        neutralized_before_card = frozenset(neutralized)
        writhing_mass_intent_unknown = set(
            state.writhing_mass_intent_unknown
        )
        if card_id == "limitbreak":
            effective_strength = (
                combat_predictor.signed_power_amount(
                    getattr(game, "player", None), "Strength"
                )
                + int(player_strength_bonus or 0)
            )
            will_exhaust = bool(getattr(card, "exhausts", False)) or (
                getattr(card, "type", None) == CardType.SKILL
                and player_corruption
            )
            exhaust_has_value = will_exhaust and (
                player_feel_no_pain > 0
                or player_dark_embrace > 0
                or self._has_relic(game, "Dead Branch")
                or self._has_relic(game, "Charon's Ashes")
            )
            if effective_strength <= 0 and not exhaust_has_value:
                # The static play-priority bonus used to make Limit Break a
                # positive action even when it doubled zero. Reject only the
                # exact no-effect transition: a prior Flex/Inflame/Spot
                # Weakness state, existing Strength, or exhaust engine still
                # makes the card legal and valuable.
                return None
        reactive_enemy_strength_per_resolution = tuple(
            self._reactive_enemy_strength_gain(
                monster, card, hp[index]
            )
            for index, monster in enumerate(monsters)
        )
        for index, strength_per_resolution in enumerate(
            reactive_enemy_strength_per_resolution
        ):
            enemy_strength_bonus[index] += (
                strength_per_resolution * resolution_copies
            )
        can_gain_block = (
            combat_predictor.can_gain_block(game) and not player_no_block
        )
        remaining_hand_indexes = list(state.remaining_hand_indexes)
        played_hand_index = next(
            (
                index
                for index in remaining_hand_indexes
                if getattr(game, "hand", [])[index] is card
            ),
            None,
        )
        if played_hand_index is None:
            return None
        remaining_hand_indexes.remove(played_hand_index)
        removed_hand_count = 1
        medical_kit = self._has_relic(game, "Medical Kit")
        blue_candle = self._has_relic(game, "Blue Candle")
        self_exhaust_count = int(
            bool(getattr(card, "exhausts", False))
            or (medical_kit and getattr(card, "type", None) == CardType.STATUS)
            or (blue_candle and getattr(card, "type", None) == CardType.CURSE)
            or (
                player_corruption
                and getattr(card, "type", None) == CardType.SKILL
            )
        )
        played_card_enters_discard = bool(
            self_exhaust_count == 0
            and getattr(card, "type", None)
            in {CardType.ATTACK, CardType.SKILL}
        )
        if card_id == "secondwind":
            other_exhaust_indexes = [
                index
                for index in remaining_hand_indexes
                if getattr(game.hand[index], "type", None) != CardType.ATTACK
            ]
            other_exhaust_count = len(other_exhaust_indexes)
        elif card_id == "fiendfire":
            other_exhaust_indexes = list(remaining_hand_indexes)
            other_exhaust_count = max(0, state.hand_size - 1)
        elif (
            card_id in {"truegrit", "burningpact"}
            and len(remaining_hand_indexes) == 1
        ):
            # True Grit and Burning Pact open a selection screen only when
            # there is a real choice. With exactly one other card in hand,
            # that card is deterministically exhausted before another card
            # can resolve. Keeping it in the beam created impossible
            # True Grit -> Thunderclap and Burning Pact -> Whirlwind lines.
            other_exhaust_indexes = list(remaining_hand_indexes)
            other_exhaust_count = 1
        else:
            other_exhaust_indexes = []
            other_exhaust_count = 0
        feel_no_pain = player_feel_no_pain if can_gain_block else 0
        dark_embrace = player_dark_embrace
        dead_branch = self._has_relic(game, "Dead Branch")
        strange_spoon = self._has_relic(game, "Strange Spoon")
        guaranteed_self_exhaust_count = (
            0 if strange_spoon else self_exhaust_count
        )
        ink_bottle = next(
            (
                relic
                for relic in getattr(game, "relics", []) or []
                if _token(getattr(relic, "relic_id", "")) == "inkbottle"
            ),
            None,
        )
        discard_count = self._deterministic_discard_count(
            card, state.hand_size
        )
        hovering_kite_energy = int(
            discard_count > 0
            and not state.discarded_this_turn
            and self._has_relic(game, "Hovering Kite")
        )
        # Unload deterministically discards every remaining non-Attack.  A
        # hand-size-only approximation leaves those concrete cards available
        # to later beam transitions and creates impossible plans such as
        # ``Unload -> Accuracy`` or ``Unload -> A Thousand Cuts``.
        unload_discard_indexes = []
        if card_id == "unload":
            unload_discard_indexes = [
                index
                for index in remaining_hand_indexes
                if getattr(game.hand[index], "type", None) != CardType.ATTACK
            ]
            discard_count = len(unload_discard_indexes)
        random_discard_unknown = (
            resolution_copies > 1
            and card_id in {
                "acrobatics", "alloutattack", "calculatedgamble",
                "concentrate", "discovery", "distraction",
                "foreigninfluence", "infernalblade", "jackofalltrades",
                "prepared", "reboot", "secrettechnique", "secretweapon",
                "stormofsteel", "survivor", "transmutation", "truegrit",
                "unload", "whitenoise",
            }
        )
        if card_id == "alloutattack" and discard_count > 0:
            possible_after_play = max(0, state.hand_size - 1)
            final_hand_size = max(0, possible_after_play - discard_count)
            random_discard_unknown = possible_after_play > 1

            # Concrete cards can outnumber the exact final hand only when at
            # least one of them must have been discarded.  Retain the most
            # harmful end-turn cards as a conservative representative of an
            # unknown random result; when just one card remains, remove that
            # exact card and the state is deterministic.
            known_to_remove = max(
                0, len(remaining_hand_indexes) - final_hand_size
            )

            def discard_hazard(hand_index):
                remaining_id = _token(
                    getattr(game.hand[hand_index], "card_id", "")
                )
                if remaining_id == "regret":
                    return max(1, final_hand_size)
                if remaining_id == "burn":
                    return (
                        4
                        if int(
                            getattr(game.hand[hand_index], "upgrades", 0)
                            or 0
                        )
                        > 0
                        else 2
                    )
                if remaining_id == "decay":
                    return 2
                return 0

            for hand_index in sorted(
                remaining_hand_indexes,
                key=lambda index: (discard_hazard(index), index),
            )[:known_to_remove]:
                remaining_hand_indexes.remove(hand_index)
        tough_bandages_gain = (
            discard_count * 3
            if can_gain_block and self._has_relic(game, "Tough Bandages")
            else 0
        )
        ornamental_fan = next(
            (
                relic
                for relic in getattr(game, "relics", []) or []
                if _token(getattr(relic, "relic_id", ""))
                == "ornamentalfan"
            ),
            None,
        )
        fan_gain = 0
        # Attack relics advance inside the per-resolution useCard loop below;
        # a copied Attack can cross the counter and use the gained Block or
        # Strength before the following copy.
        if self._orb_requires_occupied(card_id) and not orbs:
            # The card remains legally playable, but this bounded search has
            # no useful zero-orb effect to claim. Most importantly, a second
            # Fission/Dualcast cannot reuse orbs consumed earlier in the plan.
            return None

        # Duplicated Power body effects are advanced beside their per-use
        # Storm channels below.  Aggregating Focus/slots first would make the
        # first copy's overflow use the second copy's state.
        orbs_before_card = orbs
        conditional_draw, conditional_energy = (
            self._conditional_branch_resources(
                card_id,
                target_index,
                vulnerable,
                weak,
                state.discarded_this_turn,
            )
        )
        authoritative_conditional_draw, authoritative_conditional_energy = (
            self._authoritative_conditional_resources(
                game, card, candidate.target
            )
        )
        native_energy_gain = self._energy_gain(
            card,
            game,
            orbs=orbs_before_card,
            energy_override=state.energy,
            draw_pile_size=state.draw_pile_size,
        )
        if card_id == "doubleenergy":
            # Energy is spent before the first use.  Every repeated use then
            # doubles the energy produced by the prior one.
            post_cost_energy = max(0, state.energy - cost)
            final_energy = post_cost_energy
            for _ in range(resolution_copies):
                final_energy *= 2
            energy_gain = max(0, final_energy - post_cost_energy)
        elif card_id in {
            "consume", "dualcast", "multicast", "recursion", "redo",
        }:
            # Plasma energy is resolved against the evolving orb queue below.
            energy_gain = conditional_energy * resolution_copies
        else:
            energy_gain = (
                native_energy_gain * energy_resolution_copies
                + conditional_energy * resolution_copies
            )
        original_orbs = tuple(self._occupied_orbs(game))
        original_energy = self._energy_gain(
            card,
            game,
            orbs=original_orbs,
            energy_override=state.energy,
        ) + authoritative_conditional_energy
        original_draw = self._card_draw_count(
            card,
            game,
            orbs=original_orbs,
            hand_size_before_play=len(getattr(game, "hand", []) or []),
        ) + authoritative_conditional_draw + self._heatsinks_draw_count(
            game, card
        )
        native_branch_draw = self._card_draw_count(
            card,
            game,
            orbs=orbs_before_card,
            hand_size_before_play=state.hand_size,
        )
        branch_draw = (
            native_branch_draw * draw_resolution_copies
            + conditional_draw * resolution_copies
            + heatsinks_hook_draw
        )
        # Candidate construction is authoritative-frame static.  Correct its
        # draw/energy utility when an earlier branch action changed a
        # predicate, orb set, hand size, or draw-pile size.
        draw_utility_delta = branch_draw - original_draw
        if card_id == "expertise":
            # The exact later hand size still uses branch_draw below.  Do not
            # reward spending an otherwise-useless card merely because that
            # makes Expertise refill one additional slot: the consumed card
            # and replacement draw are a neutral hand-size exchange.
            draw_utility_delta = 0
        orb_utility_correction = draw_utility_delta * 2.2
        # The triggering Power's static candidate score cannot contain
        # Lightning created by an already-active Storm.  Price each concrete
        # channel with the same bounded value as Zap; ordered overflow damage,
        # Block, and Plasma energy are still added below from the shared queue.
        branch_focus = (
            combat_predictor.signed_power_amount(game.player, "Focus")
            + state.player_focus_bonus
        )
        orb_utility_correction += (
            storm_lightning_channels * self._orb_value(
                game, "zap", focus_override=branch_focus,
            )
        )
        if card_id in {
            "zap", "balllightning", "coldsnap", "glacier", "coolheaded",
            "doomandgloom", "dualcast",
        }:
            original_orb_value = self._orb_value(game, card_id, orbs=original_orbs)
            resolved_orb_value = self._orb_value(
                game, card_id, orbs=orbs_before_card, focus_override=branch_focus,
            )
            orb_utility_correction += resolved_orb_value - original_orb_value
            if (
                original_orb_value > 0 and resolved_orb_value <= 0
                and card.type == CardType.SKILL
                and candidate.non_block_utility
                <= original_orb_value + self._priority_bonus(card) + 1e-9
            ):
                orb_utility_correction -= self._priority_bonus(card)
        if card_id == "fission":
            if int(getattr(card, "upgrades", 0) or 0) <= 0:
                orb_utility_correction += (
                    self._base_fission_opportunity_cost(original_orbs)
                    - self._base_fission_opportunity_cost(orbs_before_card)
                )
        duplicated_created_cards = 0
        duplicated_created_utility = 0.0
        if resolution_copies > 1 and card_id in {
            "bladedance", "cloakanddagger",
        }:
            created_per_resolution = max(
                3 if card_id == "bladedance" else 1,
                int(getattr(card, "magic_number", 0) or 0),
            )
            duplicated_created_cards = (
                created_per_resolution * (resolution_copies - 1)
            )
            duplicated_created_utility = duplicated_created_cards * (
                3.2 if card_id == "bladedance" else 2.8
            )
        dynamic_hand_additions = (
            max(0, candidate.hand_additions) + duplicated_created_cards
        )
        dynamic_hand_additions = max(
            0,
            dynamic_hand_additions + branch_draw - original_draw,
        )
        if card_id == "transmutation":
            # The static candidate was built at turn-start energy.  Generated
            # cards follow the X value at this exact branch instead.
            dynamic_hand_additions = max(
                0,
                dynamic_hand_additions
                + self._x_effect(
                    game, card, energy_override=state.energy
                )
                - self._x_effect(game, card),
            )
        draw_timing_adjustment = 0.0
        effective_draw = 0
        choker_card_lock = (
            choker_remaining is not None and choker_remaining <= 1
        )
        if branch_draw > 0:
            # Reboot shuffles the whole old hand away before drawing, so its
            # capacity is ten rather than the pre-play hand's remaining room.
            capacity = (
                10
                if card_id == "reboot"
                else max(0, 10 - max(0, state.hand_size - 1))
            )
            normality_draw_lock = (
                state.normality_cards_played + resolution_copies >= 3
                and any(
                    _token(getattr(game.hand[index], "card_id", ""))
                    == "normality"
                    for index in remaining_hand_indexes
                )
            )
            effective_draw = (
                0
                if state.no_draw or normality_draw_lock
                else min(branch_draw, capacity)
            )
            # Choker still lets the sixth card draw into the hand, but those
            # cards cannot be played this turn.  Preserve the hand-size/end-
            # turn consequences while removing their immediate draw value.
            scored_draw = 0 if choker_card_lock else effective_draw
            lost_draw = max(0, branch_draw - effective_draw)
            dynamic_hand_additions = max(
                0, dynamic_hand_additions - lost_draw
            )
            active_nob = next((
                monster
                for index, monster in enumerate(monsters)
                if hp[index] > 0
                and _token(getattr(monster, "monster_id", ""))
                == "gremlinnob"
                and combat_predictor.power_amount(
                    monster, "Anger", "Enrage", "EnragePower"
                ) > 0
            ), None)
            remaining_attack_costs = []
            if active_nob is not None and card_id in {"battletrance", "warcry"}:
                for hand_index in remaining_hand_indexes:
                    remaining_card = game.hand[hand_index]
                    if getattr(remaining_card, "type", None) != CardType.ATTACK:
                        continue
                    remaining_damage, _ = combat_predictor.card_attack_profile(
                        game,
                        remaining_card,
                        energy_override=state.energy - cost,
                    )
                    if remaining_damage <= 0:
                        continue
                    remaining_cost = self._state_card_cost(
                        remaining_card,
                        state.energy - cost,
                        corruption=player_corruption,
                        bullet_time=player_bullet_time,
                    )
                    if remaining_cost == state.energy - cost:
                        remaining_attack_costs = [remaining_cost]
                        break
                    if 0 < remaining_cost <= state.energy - cost:
                        remaining_attack_costs.append(remaining_cost)
            attack_energy_reachable = {0}
            for remaining_cost in remaining_attack_costs:
                attack_energy_reachable |= {
                    spent + remaining_cost
                    for spent in tuple(attack_energy_reachable)
                    if spent + remaining_cost <= state.energy - cost
                }
            known_attacks_fill_energy = (
                state.energy - cost > 0
                and state.energy - cost in attack_energy_reachable
            )
            # Candidate construction prices each nominal draw at 2.2. Remove
            # draws prevented by No Draw or hand capacity, then reward draw
            # timing by the energy still available to use the new cards.
            # This is an ordering term only; it does not fabricate concrete
            # cards or categorical lethal from an unknown draw.
            draw_timing_adjustment -= lost_draw * 2.2
            if known_attacks_fill_energy:
                # A pure draw Skill after Nob's Enrage is active has no
                # proven energy-enabling value when attacks already in hand
                # can spend the whole turn.  Do not let optimistic unknown
                # draws outweigh the immediate and permanent Strength gain.
                # The draw retains its normal value when the hand cannot use
                # the available energy, where it may be the only frontload.
                draw_timing_adjustment -= scored_draw * 2.2
            timing_weight = (
                0.0
                if known_attacks_fill_energy
                else 0.75
                if card_id == "battletrance"
                else 0.0
                if card_id == "fission"
                else 0.25
            )
            draw_timing_adjustment += (
                scored_draw
                * min(
                    3,
                    max(0, state.energy - cost + energy_gain),
                )
                * timing_weight
            )
            if choker_card_lock:
                # At the cap draws cannot fund another play. Current energy
                # has no nominal utility to charge back.
                draw_timing_adjustment -= max(0, int(branch_draw or 0)) * 2.2
        original_orb_mitigation = self._frost_evoke_mitigation(
            game,
            card,
            orbs=original_orbs,
            energy_override=state.energy,
        )[0]
        current_orb_mitigation = self._frost_evoke_mitigation(
            game,
            card,
            orbs=orbs_before_card,
            energy_override=state.energy,
        )[0]
        orb_mitigation_correction = (
            current_orb_mitigation - original_orb_mitigation
        )

        # Card-body Block is resolution-local.  Supported Attack+Block cards
        # (Iron Wave and Dash) gain Block before their DamageAction, so a
        # duplicated card must expose each copy's Block to that copy's
        # immediate Thorns/Sharp Hide reaction rather than pooling all Block
        # before all reactions.
        serialized_base_block = getattr(card, "base_block", None)
        serialized_block = (
            max(0, int(getattr(card, "block", 0) or 0))
            if serialized_base_block is None
            or int(serialized_base_block or 0) >= 0
            else 0
        )
        intrinsic_block_gain_total = 0
        intrinsic_block_resolutions_applied = 0
        can_gain_this_resolution = can_gain_block

        def apply_intrinsic_block_resolution(resolution_index):
            nonlocal player_block, player_no_block
            nonlocal can_gain_this_resolution
            nonlocal intrinsic_block_gain_total
            nonlocal intrinsic_block_resolutions_applied

            if not can_gain_this_resolution:
                resolution_block_gain = 0
            elif card_id == "autoshields":
                resolution_block_gain = (
                    serialized_block if player_block <= 0 else 0
                )
            elif card_id == "reinforcedbody":
                resolution_block_gain = serialized_block * self._x_effect(
                    game, card, energy_override=state.energy
                )
            elif card_id == "entrench":
                resolution_block_gain = player_block
            elif card_id == "secondwind":
                serialized_base = int(getattr(card, "base_block", 0) or 0)
                per_card = (
                    serialized_block
                    if serialized_block > 0 or serialized_base > 0
                    else 5 + 2 * int(getattr(card, "upgrades", 0) or 0)
                )
                resolution_block_gain = (
                    per_card * other_exhaust_count
                    if resolution_index == 0
                    else 0
                )
            else:
                resolution_block_gain = serialized_block

            if (
                player_dexterity_bonus > 0
                and resolution_block_gain > 0
                and card_id not in {"entrench", "secondwind"}
            ):
                raw_block_gain = (
                    int(serialized_base_block)
                    if serialized_base_block is not None
                    and int(serialized_base_block or 0) >= 0
                    else None
                )
                resolution_block_gain += self._branch_dexterity_block_gain(
                    game, raw_block_gain, player_dexterity_bonus
                )
            resolution_block_gain = max(0, resolution_block_gain)
            player_block += resolution_block_gain
            intrinsic_block_gain_total += resolution_block_gain
            intrinsic_block_resolutions_applied += 1
            if card_id == "panicbutton":
                can_gain_this_resolution = False
                player_no_block = True
            return resolution_block_gain

        raw_damage, hits = combat_predictor.card_attack_profile(
            game,
            card,
            energy_override=state.energy,
            target=candidate.target,
            poisoned_override=(
                poison[target_index] > 0
                if card_id == "bane" and target_index is not None
                else None
            ),
        )
        if card_id == "barrage":
            hits = len(orbs_before_card)
            raw_damage = max(
                0, int(getattr(card, "damage", 0) or 0)
            ) * hits
        elif card_id == "fiendfire":
            hits = other_exhaust_count
            raw_damage = max(
                0, int(getattr(card, "damage", 0) or 0)
            ) * hits
        akabeko_card_bonus = 0
        if (
            getattr(card, "type", None) == CardType.ATTACK
            and player_akabeko_ready
        ):
            akabeko_card_bonus = combat_predictor.akabeko_bonus(game)
            raw_damage += akabeko_card_bonus
        serialized_pen_nib = combat_predictor.has_power(
            game.player, "Pen Nib", "PenNibPower"
        )
        authoritative_pen_nib = (
            serialized_pen_nib or combat_predictor.pen_nib_ready(game)
        )
        if (
            getattr(card, "type", None) == CardType.ATTACK
            and authoritative_pen_nib
            and not player_pen_nib
        ):
            # Every serialized Attack still carries the authoritative Pen Nib
            # modifier even after an earlier branch card consumed it.
            if serialized_pen_nib:
                raw_damage //= 2
        strength_damage_bonus = self._same_turn_strength_damage_bonus(
            game, card, player_strength_bonus, hits
        )
        if authoritative_pen_nib and player_pen_nib:
            # A relic-counter-only frame has an ordinary serialized card;
            # normalize it to the same doubled first resolution represented
            # by a ready PenNibPower frame.  The latter already carries the
            # bonus in card.damage and must not be doubled again.
            if (
                serialized_pen_nib is False
                and getattr(card, "type", None) == CardType.ATTACK
                and raw_damage > 0
            ):
                raw_damage *= 2
            strength_damage_bonus *= 2
        raw_damage += strength_damage_bonus
        residual_attack_loss = combat_predictor.projected_attack_hp_loss(
            game,
            active_monsters_override=[
                monsters[index]
                for index in sorted(active_indexes)
                if hp[index] > 0 and index not in neutralized
            ],
            block_override=player_block,
            buffer_override=player_buffer,
            force_intangible=player_intangible,
        )
        zero_x_defense_is_useful = (
            residual_attack_loss > 0
            and (player_rage > 0 or player_after_image > 0)
        )
        zero_x_damage_trigger = (
            player_thousand_cuts > 0
            or any(int(amount or 0) > 0 for amount in choke)
        )
        if (
            getattr(card, "type", None) == CardType.ATTACK
            and int(getattr(card, "cost", 0) or 0) == -1
            and raw_damage <= 0
            and not zero_x_defense_is_useful
            and not zero_x_damage_trigger
        ):
            # An X-cost attack at zero energy has no hit to resolve.  Do not
            # spend a card-play action on a zero-packet Whirlwind/Skewer after
            # ordinary cards consumed the energy. Rage/After Image only count
            # when their extra block prevents real remaining attack damage;
            # already-covered incoming damage is not a reason to waste the
            # card. Thousand Cuts and Choke remain genuine damage triggers.
            return None
        if card_id == "shiv" and hits > 0:
            authoritative_accuracy = combat_predictor.power_amount(
                game.player, "Accuracy", "AccuracyPower"
            )
            raw_damage += max(
                0, int(player_accuracy or 0) - authoritative_accuracy
            ) * hits
        if getattr(card, "type", None) == CardType.ATTACK and hits > 0:
            unconsumed_temporary_strength = 0

        def branch_weak_debuff_spec():
            if card_id not in self.WEAK_CARDS or card_id == "waveofthehand":
                return (), 0
            if (
                card_id == "gofortheeyes"
                and (
                    target_index is None
                    or not (
                        getattr(monsters[target_index], "intent", None)
                        and getattr(
                            monsters[target_index], "intent", None
                        ).is_attack()
                    )
                )
            ):
                return (), 0
            upgrades = max(0, int(getattr(card, "upgrades", 0) or 0))
            serialized_weak = int(getattr(card, "magic_number", 0) or 0)
            fallback = {
                "clothesline": 2 + min(1, upgrades),
                "cripplingpoison": 2 + min(1, upgrades),
                "gofortheeyes": 1 + min(1, upgrades),
                "legsweep": 2 + min(1, upgrades),
                "neutralize": 1 + min(1, upgrades),
                "shockwave": 3 + 2 * min(1, upgrades),
                "suckerpunch": 1 + min(1, upgrades),
                "uppercut": 1 + min(1, upgrades),
            }
            amount = (
                serialized_weak
                if serialized_weak > 0 and card_id != "cripplingpoison"
                else fallback.get(card_id, 1)
            )
            targets = (
                sorted(active_indexes)
                if card_id in self.AOE_WEAK_CARDS
                else [target_index]
            )
            return tuple(targets), amount

        weak_debuff_targets, weak_debuff_amount = branch_weak_debuff_spec()

        def apply_branch_weak_debuff_once(targets=None, amount=None):
            selected = weak_debuff_targets if targets is None else targets
            applied_amount = (
                weak_debuff_amount if amount is None else max(0, int(amount))
            )
            if applied_amount <= 0:
                return set()
            landed = set()
            for index in selected:
                if index is None or hp[index] <= 0:
                    continue
                if artifact[index] > 0:
                    artifact[index] -= 1
                else:
                    weak[index] += applied_amount
                    landed.add(index)
                    trigger_sadistic_nature(index)
            return landed

        def apply_branch_vulnerable_debuff_once():
            if card_id not in self.VULNERABLE_CARDS:
                return set()
            serialized = int(getattr(card, "magic_number", 0) or 0)
            upgrades = max(0, int(getattr(card, "upgrades", 0) or 0))
            fallback = {
                "bash": 2 + min(1, upgrades),
                "beamcell": 2 + min(1, upgrades),
                "shockwave": 3 + 2 * min(1, upgrades),
                "terror": 99,
                "thunderclap": 1,
                "trip": 2,
                "uppercut": 1 + min(1, upgrades),
            }
            amount = serialized if serialized > 0 else fallback.get(card_id, 1)
            targets = (
                [target_index]
                if target_index is not None
                else sorted(active_indexes)
            )
            landed = set()
            for index in targets:
                if index is None or hp[index] <= 0:
                    continue
                if artifact[index] > 0:
                    artifact[index] -= 1
                    continue
                vulnerable[index] += amount
                landed.add(index)
                trigger_sadistic_nature(index)
            if landed and self._has_relic(game, "Champion Belt"):
                # Champion Belt queues a separate one-Weak ApplyPowerAction
                # only after Vulnerable actually lands.  It therefore owns
                # its own Artifact check and never triggers for a blocked
                # Vulnerable application.
                apply_branch_weak_debuff_once(tuple(sorted(landed)), 1)
            return landed

        def apply_branch_lock_on_once():
            if (
                card_id != "lockon"
                or target_index is None
                or hp[target_index] <= 0
            ):
                return
            serialized = max(0, int(getattr(card, "magic_number", 0) or 0))
            amount = (
                serialized
                if serialized > 0
                else 2 + int(getattr(card, "upgrades", 0) or 0)
            )
            if artifact[target_index] > 0:
                artifact[target_index] -= 1
            else:
                lock_on[target_index] += amount
                trigger_sadistic_nature(target_index)

        dynamic_direct = 0.0
        # Random multi-enemy Lightning does not mutate a concrete HP branch,
        # so keep its aggregate expected loss out-of-band for the first
        # action's resource/telemetry fields.
        uncertain_orb_hp_loss = [0.0]

        def trigger_sadistic_nature(index):
            """Resolve one landed debuff's immediate THORNS packet."""

            nonlocal dynamic_direct
            if index is None or hp[index] <= 0:
                return 0
            amount = max(0, int(player_sadistic_nature or 0))
            if amount <= 0:
                return 0
            dealt = self._apply_enemy_damage_packet(
                monsters[index],
                index,
                hp,
                block,
                damage_value,
                mode_shift_remaining,
                neutralized,
                amount,
                # Sadistic Nature uses DamageType.THORNS.  It ignores
                # Strength/Vulnerable but consumes the defender's Block.
                blockable=True,
            )
            dynamic_direct += dealt
            return dealt
        dynamic_fatal_utility = 0.0
        direct_attack_hp_damage = 0
        direct_attack_kill = False
        sunder_refund = False
        authoritative_sunder_refund = (
            card_id == "sunder"
            and candidate.target is not None
            and candidate.damage >= max(
                1,
                int(
                    getattr(candidate.target, "current_hp", 0) or 0
                ),
            )
            and not combat_predictor.has_unresolved_damage_cap(
                candidate.target
            )
            and self._sunder_refund_is_true_kill(
                game, candidate.target, 0
            )
        )
        direct_attack_target_indexes = set()
        # A pre-existing Choke triggers after this card's own effects.  Keep a
        # snapshot because a Choke applied by this card does not trigger on
        # itself, and because ordering matters when an earlier hit activates
        # Guardian's immediate 20 Block.
        choke_before_card = tuple(choke)
        thousand_cuts_before_card = max(
            0, int(player_thousand_cuts or 0)
        )
        beat_at_card_start = tuple(
            combat_predictor.power_amount(
                monster, "Beat of Death", "BeatOfDeathPower"
            )
            for index, monster in enumerate(monsters)
            if hp[index] > 0
        )

        attack_resolution_copies = (
            1 if card_id == "fiendfire" else resolution_copies
        )
        reaction_resolution_copies = (
            attack_resolution_copies
            if getattr(card, "type", None) == CardType.ATTACK
            else resolution_copies
        )
        dynamic_attack_healing_actual = 0
        dynamic_thorns_cost = 0
        dynamic_sharp_hide_cost = 0
        dynamic_beat_cost = 0
        dynamic_clay_hp_loss_events = 0
        attack_reactions_resolved = (
            getattr(card, "type", None) == CardType.ATTACK
        )
        if getattr(card, "type", None) == CardType.ATTACK:
            strength_bonus_at_attack_start = player_strength_bonus
            pen_nib_active_at_attack_start = player_pen_nib
            pen_nib_relic_counter = combat_predictor.relic_counter(
                game, "Pen Nib", default=-1
            )
            base_damage_targets = (
                [target_index]
                if target_index is not None
                else sorted(active_indexes)
            )
            random_multi_target_attack = (
                card_id in _RANDOM_MULTI_TARGET_ATTACKS
                and target_index is None
                and len(base_damage_targets) > 1
            )
            if random_multi_target_attack:
                # Keep the branch deterministic for ranking while retaining
                # the real target uncertainty: two Rip and Tear packets are
                # represented by at most two concrete witnesses, never one
                # packet per living enemy.
                base_damage_targets = base_damage_targets[
                    : max(1, int(hits or 1))
                ]
            # Each duplicate is a complete ``card.use`` call.  Finish one
            # attack (including its debuffs and deaths) before the next copy
            # so Bash/Beam Cell/Uppercut can modify the repeated damage.  A
            # duplicated Fiend Fire has no remaining hand to exhaust after
            # its first resolution and therefore cannot replay the old hits.
            attack_channels_orbs = bool(
                self._card_channels(
                    game,
                    card,
                    sum(1 for value in hp if value > 0),
                    resolution_copies=1,
                )
            )
            if (
                attack_resolution_copies > 1
                and attack_channels_orbs
                and any(
                    malleable_next_block[index] > 0
                    for index in base_damage_targets
                    if index is not None and hp[index] > 0
                )
            ):
                # The current aggregate orb queue cannot interleave copied
                # Attack channel/overflow actions with per-copy Malleable
                # gains. Refuse that rare speculative branch.
                return None
            for resolution_index in range(attack_resolution_copies):
                if (
                    target_index is not None
                    and hp[target_index] <= 0
                ):
                    # Targeted autoplay repeats card.canUse. A target made
                    # dying by the prior use cancels the queued copy before
                    # any body or on-use hook can fire.
                    resolution_copies = resolution_index
                    attack_resolution_copies = resolution_index
                    break
                beat_snapshots = beat_at_card_start
                if card_id in {"ironwave", "dash"}:
                    apply_intrinsic_block_resolution(resolution_index)
                dealt_for_resolution = 0
                resolution_healing_requested = 0
                resolution_attacked_any = False
                resolution_thorns_events = []
                resolution_sharp_hide_events = []
                random_attack_reactions = (
                    card_id in {"swordboomerang", "ripandtear"}
                    and target_index is None
                    and len(base_damage_targets) > 1
                )
                for random_hit_index, index in enumerate(base_damage_targets):
                    if index is None or hp[index] <= 0:
                        continue
                    # Even a zero-output Attack still counts as a card use
                    # for Sharp Hide and still resolves its own debuffs.  A
                    # Thorns packet, however, belongs to each concrete damage
                    # hit below rather than to the card in the abstract.
                    resolution_attacked_any = True
                    direct_attack_target_indexes.add(index)
                    monster = monsters[index]
                    hp_before_direct_damage = hp[index]
                    if (
                        card_id == "melter"
                        and target_index is not None
                        and index == target_index
                    ):
                        # RemoveAllBlockAction is queued ahead of Melter's
                        # DamageAction on every complete card resolution.  A
                        # copied Melter must therefore remove Block restored
                        # by Malleable/Curl Up after the preceding copy too.
                        block[index] = 0
                    hit_count = (
                        1
                        if random_multi_target_attack
                        else max(1, int(hits or 1))
                    )
                    resolution_raw_damage = raw_damage
                    if random_multi_target_attack:
                        packet_count = max(1, int(hits or 1))
                        packet_base, packet_remainder = divmod(
                            max(0, int(raw_damage or 0)), packet_count
                        )
                        resolution_raw_damage = (
                            packet_base
                            + int(random_hit_index < packet_remainder)
                        )
                    if akabeko_card_bonus > 0 and resolution_index > 0:
                    # Echo Form/Double Tap repeats the card after Akabeko's
                    # one-shot trigger has been consumed.
                        resolution_raw_damage = max(
                            0, resolution_raw_damage - akabeko_card_bonus
                        )
                    if (
                        pen_nib_active_at_attack_start
                        and resolution_index > 0
                    ):
                    # The serialized/raw first hit includes a PenNibPower
                    # which its first use removes before the copy.
                        resolution_raw_damage //= 2
                    if player_pen_nib and resolution_index > 0:
                        # A relic counter crossing 8 -> 9 on the preceding
                        # use installs PenNibPower for this copied resolution.
                        resolution_raw_damage *= 2
                    strength_delta_from_prior_copy = (
                        player_strength_bonus
                        - strength_bonus_at_attack_start
                    )
                    if strength_delta_from_prior_copy > 0:
                        resolution_raw_damage += (
                            self._same_turn_strength_damage_bonus(
                                game,
                                card,
                                strength_delta_from_prior_copy,
                                hits,
                            )
                        )
                    base_per_hit, remainder = divmod(
                        max(0, int(resolution_raw_damage or 0)), hit_count
                    )
                    weak_target_damage = (
                        self._weak_target_per_hit_damage(
                            game, card, monster, resolution_raw_damage,
                            hit_count,
                            vulnerable_override=vulnerable[index],
                            extra_slow_cards=(
                                state.cards_played + resolution_index
                            ),
                        )
                        if (
                            resolution_raw_damage == raw_damage
                            and strength_delta_from_prior_copy == 0
                            and akabeko_card_bonus == 0
                            and not pen_nib_active_at_attack_start
                            and not player_pen_nib
                        )
                        else None
                    )
                    dealt_for_card = 0
                    curl_up_triggered = False
                    flight_active_for_resolution = flight_stacks[index] > 0
                    executed_hit_count = 0
                    for hit_index in range(hit_count):
                        if hp[index] <= 0:
                            break
                        executed_hit_count += 1
                        adjusted = base_per_hit + int(hit_index < remainder)
                    # Card.damage contains player-side modifiers only. Apply
                    # the target's branch-local Vulnerable to every hit,
                    # whether it existed in the authoritative frame or was
                    # applied by an earlier card in this simulated sequence.
                        slow_multiplier = combat_predictor.slow_damage_multiplier(
                            monster,
                            extra_cards=(
                                state.cards_played + resolution_index
                            ),
                        )
                        if weak_target_damage is not None:
                            adjusted = weak_target_damage
                        else:
                            target_multiplier = slow_multiplier
                            if vulnerable[index] > 0:
                                target_multiplier *= (
                                    combat_predictor.vulnerable_damage_multiplier(game)
                                )
                            if adjusted > 0 and target_multiplier != 1.0:
                                adjusted = combat_predictor.floor_damage_product(
                                    adjusted, target_multiplier
                                )
                    # Giant Head's Slow is target-local and grows with every
                    # card already played this turn.  The authoritative card
                    # damage cannot contain this enemy-specific multiplier;
                    # apply it before target Block for each simulated hit.
                        if flight_active_for_resolution:
                            adjusted //= 2
                        dealt = self._apply_enemy_damage_packet(
                            monster,
                            index,
                            hp,
                            block,
                            damage_value,
                            mode_shift_remaining,
                            neutralized,
                            adjusted,
                            blockable=True,
                            minimum_unblocked_damage=(
                                combat_predictor.boot_minimum_damage(game)
                            ),
                        )
                        dealt_for_card += dealt
                        dealt_for_resolution += dealt
                        dynamic_direct += dealt
                        if dealt > 0 and hp[index] > 0:
                            curl_up_triggered = (
                                curl_up_triggered
                                or curl_up_block[index] > 0
                            )
                            if malleable_next_block[index] > 0:
                                # Malleable queues GainBlockAction behind the
                                # card's already-created actions. In
                                # particular, Cold Snap's channel/overflow
                                # evoke damages the target before the final
                                # hit's Block arrives. Earlier HP-damaging
                                # hits in a multi-hit action settle before the
                                # next hit and can prevent a later trigger.
                                if hit_index + 1 < hit_count:
                                    block[index] += malleable_next_block[index]
                                    malleable_next_block[index] += 1
                                else:
                                    pending_malleable_triggers[index] += 1
                            if flight_stacks[index] > 0:
                                flight_stacks[index] -= 1

                    if (
                        flight_active_for_resolution
                        and flight_stacks[index] == 0
                        and hp[index] > 0
                        and _token(getattr(monster, "monster_id", ""))
                        == "byrd"
                    ):
                        # The hit which removes Byrd's last Flight stack makes
                        # it fall and replaces its queued move with STUN.  The
                        # whole Attack card still uses the pre-resolution
                        # Flight multiplier, but the enemy cannot execute the
                        # old attack (or die to Thorns from that invented
                        # attack) at end of turn.
                        neutralized.add(index)

                    # Reactions are resolution-local.  A duplicated Attack
                    # does not replay the first copy's dead targets, and a
                    # lethal early hit does not invent reactions for later
                    # hits which never execute.  Sharp Hide is one on-use
                    # packet for the attacked Guardian; Thorns is per actual
                    # positive attack hit.
                    if not random_attack_reactions:
                        thorns_amount = combat_predictor.power_amount(
                            monster, "Thorns"
                        )
                        if thorns_amount > 0:
                            resolution_thorns_events.extend(
                                thorns_amount
                                for _ in range(executed_hit_count)
                            )
                        sharp_hide_amount = combat_predictor.power_amount(
                            monster, "Sharp Hide", "SharpHidePower"
                        )
                        if sharp_hide_amount > 0:
                            resolution_sharp_hide_events.append(
                                sharp_hide_amount
                            )

                # Curl Up queues its one-time Block after the whole attack
                # card resolves, so a multi-hit card is not interrupted
                # between hits. Echo Form copies are separate resolutions and
                # therefore observe the new Block on the repeated copy.
                    if curl_up_triggered and hp[index] > 0:
                        block[index] += curl_up_block[index]
                        curl_up_block[index] = 0

                    if (
                        dealt_for_card > 0
                        and hp[index] > 0
                        and self._writhing_mass_has_compulsive(monster)
                    ):
                    # ReactivePower (protocol ID ``Compulsive``) rolls a new
                    # intent only after positive nonlethal Attack damage.  Do
                    # not guess that result or keep treating the displayed
                    # pre-hit move as authoritative.
                        writhing_mass_intent_unknown.add(index)

                    direct_attack_hp_damage += dealt_for_card
                    if (
                        not random_multi_target_attack
                        and hp_before_direct_damage > 0
                        and hp[index] <= 0
                    ):
                        direct_attack_kill = True
                        if (
                            card_id == "sunder"
                            and index == target_index
                            and self._sunder_refund_is_true_kill(
                                game,
                                monster,
                                hp[index],
                                monsters,
                                hp,
                            )
                        ):
                            sunder_refund = True

                    scaling_pressure = self._monster_scaling_pressure(
                        monster, game
                    )
                    dynamic_direct += dealt_for_card * min(
                        0.75, scaling_pressure / 20.0
                    )
                    # Recurring permanent Strength/Dexterity loss creates a
                    # real damage race. Reward concrete HP progress enough to
                    # admit a safe extra hit instead of repeatedly buying a
                    # little Block while the whole deck decays every cycle.
                    persistent_pressure = self._persistent_debuff_pressure(
                        game, monster
                    )
                    persistent_stat_debt = (
                        max(
                            0,
                            -combat_predictor.signed_power_amount(
                                game.player, "Strength"
                            ),
                        )
                        + max(
                            0,
                            -combat_predictor.signed_power_amount(
                                game.player, "Dexterity"
                            ),
                        )
                    )
                    if persistent_stat_debt > 0:
                        dynamic_direct += dealt_for_card * min(
                            2.0, persistent_pressure / 5.0
                        )
                    if (
                        index == target_index
                        and card_id in self.ON_KILL_BENEFIT_CARDS
                        and hp_before_direct_damage > 0
                    ):
                        if hp[index] <= 0 and self._is_true_death_at_hp(
                            monster, hp[index], monsters, hp
                        ):
                            dynamic_fatal_utility += self.ON_KILL_BONUS.get(
                                card_id, 0
                            )
                        elif bool(getattr(card, "exhausts", False)):
                        # A nonlethal use, a phase-one Awakened One, or a
                        # non-final Darkling consumes the permanent-reward
                        # card without firing Fatal. Preserve it unless this
                        # branch improves the higher-priority survival tier.
                            dynamic_fatal_utility -= (
                                self.NONFATAL_EXHAUST_PENALTY.get(card_id, 5)
                            )
                    if hp[index] <= 0 and not random_multi_target_attack:
                    # Reaching zero in Awakened One phase one is not a true
                    # combat kill, but it is still valuable phase progress
                    # and removes Curiosity before later Powers this turn.
                        dynamic_direct += (
                            (8 if target_index is not None else 7)
                            + combat_predictor.monster_threat(monster)
                            * (0.35 if target_index is not None else 0.25)
                            + scaling_pressure * 0.75
                        )

                # Attack-applied powers belong to this resolution, not to a
                # post-loop aggregate.  Weak precedes Vulnerable for
                # Uppercut, matching its two separate ApplyPower actions.
                apply_branch_weak_debuff_once()
                apply_branch_vulnerable_debuff_once()
                apply_branch_lock_on_once()
                if (
                    card_id == "poisonedstab"
                    and target_index is not None
                    and hp[target_index] > 0
                ):
                    poison_amount = max(
                        1,
                        int(getattr(card, "magic_number", 0) or 0),
                    ) + int(self._has_relic(game, "Snecko Skull"))
                    if artifact[target_index] > 0:
                        artifact[target_index] -= 1
                    else:
                        poison[target_index] += poison_amount
                        trigger_sadistic_nature(target_index)

                if random_attack_reactions and resolution_attacked_any:
                    # Sword Boomerang's concrete targets are hidden.  Preserve
                    # the candidate's conservative highest reachable Thorns
                    # stack instead of pretending every living enemy was hit.
                    resolution_thorns_events.extend(
                        candidate.thorns_damage_events
                    )
                    resolution_sharp_hide_events.extend(
                        candidate.sharp_hide_damage_events
                    )

                if card_id == "reaper":
                    resolution_healing_requested = dealt_for_resolution
                elif card_id == "bite":
                    bite_heal = max(
                        2, int(getattr(card, "magic_number", 0) or 0)
                    )
                    resolution_healing_requested = min(
                        bite_heal, dealt_for_resolution
                    )

                damage_before_resolution_deaths = sum(damage_value)
                hp, block, damage_value = self._resolve_state_deaths(
                    game,
                    monsters,
                    hp,
                    block,
                    corpse,
                    damage_value,
                    mode_shift_remaining=mode_shift_remaining,
                    neutralized=neutralized,
                )
                hp = list(hp)
                block = list(block)
                damage_value = list(damage_value)
                dynamic_direct += max(
                    0.0,
                    sum(damage_value) - damage_before_resolution_deaths,
                )

                if (
                    card_id == "wallop"
                    and can_gain_this_resolution
                    and dealt_for_resolution > 0
                ):
                    # Wallop's custom action records this resolution's actual
                    # HP damage, then queues GainBlockAction to the top above
                    # the target's queued Thorns reaction.
                    player_block += dealt_for_resolution
                    intrinsic_block_gain_total += dealt_for_resolution

                if resolution_attacked_any:
                    thorns_outcome = combat_predictor.resolve_player_damage_events(
                        game,
                        tuple(
                            combat_predictor.PlayerDamageEvent(
                                "thorns", amount, blockable=True
                            )
                            for amount in resolution_thorns_events
                        ),
                        block=player_block,
                        buffer_layers=player_buffer,
                        force_intangible=player_intangible,
                    )
                    dynamic_clay_hp_loss_events += len(
                        thorns_outcome.hp_loss_events
                    )
                    dynamic_thorns_cost += thorns_outcome.hp_loss
                    player_hp_state -= thorns_outcome.hp_loss
                    player_block = thorns_outcome.block
                    player_buffer = thorns_outcome.buffer
                    if player_hp_state <= 0:
                        return None

                    if card_id in {"reaper", "bite"}:
                        max_player_hp = max(
                            1,
                            int(getattr(game.player, "max_hp", 1) or 1),
                        )
                        before_heal = player_hp_state
                        player_hp_state = min(
                            max_player_hp,
                            player_hp_state
                            + resolution_healing_requested,
                        )
                        dynamic_attack_healing_actual += max(
                            0, player_hp_state - before_heal
                        )

                    # A duplicated Attack is another complete useCard.  Its
                    # player on-use Block hooks resolve after the card body's
                    # damage/Thorns actions and before the enemy's Sharp Hide
                    # on-use damage.  They therefore cannot be pooled across
                    # copies either.
                    for raw_gain in (
                        player_after_image,
                        player_rage,
                    ):
                        if not can_gain_this_resolution or raw_gain <= 0:
                            continue
                        player_block += raw_gain
                        if player_dexterity_bonus > 0:
                            player_block += (
                                self._branch_dexterity_block_gain(
                                    game,
                                    raw_gain,
                                    player_dexterity_bonus,
                                )
                            )

                    # Other player/relic on-use hooks also advance once per
                    # successful autoplay resolution.  Their actions resolve
                    # before enemy Sharp Hide and Beat, and their resulting
                    # Strength/Dexterity is visible to the next copy.
                    attack_ordinal = (
                        state.attack_resolutions_played
                        + resolution_index
                        + 1
                    )
                    if ornamental_fan is not None:
                        fan_counter = max(
                            0,
                            int(
                                getattr(ornamental_fan, "counter", 0)
                                or 0
                            ),
                        )
                        if (fan_counter + attack_ordinal) % 3 == 0:
                            raw_fan_gain = 4
                            fan_gain += raw_fan_gain
                            if can_gain_this_resolution:
                                player_block += raw_fan_gain
                                if player_dexterity_bonus > 0:
                                    player_block += (
                                        self._branch_dexterity_block_gain(
                                            game,
                                            raw_fan_gain,
                                            player_dexterity_bonus,
                                        )
                                    )
                    if self._has_relic(game, "Shuriken"):
                        shuriken_counter = combat_predictor.relic_counter(
                            game, "Shuriken", default=0
                        )
                        if (shuriken_counter + attack_ordinal) % 3 == 0:
                            player_strength_bonus += 1
                    if self._has_relic(game, "Kunai"):
                        kunai_counter = combat_predictor.relic_counter(
                            game, "Kunai", default=0
                        )
                        if (kunai_counter + attack_ordinal) % 3 == 0:
                            player_dexterity_bonus += 1
                    if self._has_relic(game, "Nunchaku"):
                        nunchaku_counter = combat_predictor.relic_counter(
                            game, "Nunchaku", default=0
                        )
                        if (nunchaku_counter + attack_ordinal) % 10 == 0:
                            energy_gain += 1
                    if pen_nib_active_at_attack_start and resolution_index == 0:
                        player_pen_nib = False
                    elif resolution_index > 0 and player_pen_nib:
                        player_pen_nib = False
                    if pen_nib_relic_counter >= 0:
                        pen_nib_progress = (
                            pen_nib_relic_counter + attack_ordinal
                        ) % 10
                        if pen_nib_progress == 9:
                            player_pen_nib = True
                        elif pen_nib_progress == 0:
                            player_pen_nib = False

                    # Existing A Thousand Cuts and Choke stacks fire for each
                    # successful copied play.  Resolve them before the enemy
                    # use-card reactions so a hook kill can invalidate the
                    # next targeted copy at its canUse boundary.
                    if thousand_cuts_before_card > 0:
                        for hook_index, hook_monster in enumerate(monsters):
                            if hp[hook_index] <= 0:
                                continue
                            dynamic_direct += self._apply_enemy_damage_packet(
                                hook_monster,
                                hook_index,
                                hp,
                                block,
                                damage_value,
                                mode_shift_remaining,
                                neutralized,
                                thousand_cuts_before_card,
                                blockable=True,
                            )
                    for hook_index, hook_amount in enumerate(
                        choke_before_card
                    ):
                        if hp[hook_index] <= 0 or hook_amount <= 0:
                            continue
                        dynamic_direct += self._apply_enemy_damage_packet(
                            monsters[hook_index],
                            hook_index,
                            hp,
                            block,
                            damage_value,
                            mode_shift_remaining,
                            neutralized,
                            hook_amount,
                            blockable=False,
                        )

                    sharp_hide_outcome = (
                        combat_predictor.resolve_player_damage_events(
                            game,
                            tuple(
                                combat_predictor.PlayerDamageEvent(
                                    "sharp_hide", amount, blockable=True
                                )
                                for amount in resolution_sharp_hide_events
                            ),
                            block=player_block,
                            buffer_layers=player_buffer,
                            force_intangible=player_intangible,
                        )
                    )
                    dynamic_clay_hp_loss_events += len(
                        sharp_hide_outcome.hp_loss_events
                    )
                    dynamic_sharp_hide_cost += sharp_hide_outcome.hp_loss
                    player_hp_state -= sharp_hide_outcome.hp_loss
                    player_block = sharp_hide_outcome.block
                    player_buffer = sharp_hide_outcome.buffer
                    if player_hp_state <= 0:
                        return None

                    for beat_damage in beat_snapshots:
                        if beat_damage <= 0:
                            continue
                        beat_outcome = (
                            combat_predictor.resolve_player_damage_events(
                                game,
                                (
                                    combat_predictor.PlayerDamageEvent(
                                        "beat_of_death",
                                        beat_damage,
                                        blockable=True,
                                    ),
                                ),
                                block=player_block,
                                buffer_layers=player_buffer,
                                force_intangible=player_intangible,
                            )
                        )
                        dynamic_clay_hp_loss_events += len(
                            beat_outcome.hp_loss_events
                        )
                        dynamic_beat_cost += beat_outcome.hp_loss
                        player_hp_state -= beat_outcome.hp_loss
                        player_block = beat_outcome.block
                        player_buffer = beat_outcome.buffer
                        if player_hp_state <= 0:
                            return None

                if not attack_channels_orbs:
                    # Melter copies and ordinary repeated attacks observe the
                    # Block restored by the preceding complete card use.
                    settle_pending_malleable()

        if sunder_refund:
            # The refund resolves immediately after Sunder's own fatal hit
            # and is available to later cards in this same ordered branch.
            energy_gain += 3

        # Orb-consuming cards must see the queue left by the preceding copy.
        # In particular Echo Dualcast consumes two distinct front orbs, and
        # repeated Consume applies Focus before each forced slot eviction.
        channel_frost_evoke_block = 0
        orb_consumer_ids = {
            "consume", "dualcast", "multicast", "recursion", "redo",
        }
        if card_id in orb_consumer_ids:
            evolving_orbs = tuple(orbs_before_card)
            for _ in range(resolution_copies):
                if card_id == "consume":
                    focus_gain = 2 + int(getattr(card, "upgrades", 0) or 0)
                    player_focus_bonus += focus_gain
                    evolving_orbs = self._adjust_orb_focus(
                        evolving_orbs, focus_gain
                    )
                    new_slots = max(1, orb_slots - 1)
                    must_evoke = len(evolving_orbs) > new_slots
                    orb_slots = new_slots
                    if not must_evoke:
                        continue
                if not evolving_orbs:
                    break
                evoked_orb = evolving_orbs[0]
                evolving_orbs = evolving_orbs[1:]
                orb_id = self._orb_id(evoked_orb)
                if card_id == "dualcast":
                    evoke_count = 2
                elif card_id == "multicast":
                    evoke_count = self._x_effect(
                        game,
                        card,
                        upgraded_bonus=True,
                        energy_override=state.energy,
                    )
                else:
                    evoke_count = 1
                evoke_count = max(0, int(evoke_count or 0))
                if orb_id == "frost":
                    channel_frost_evoke_block += (
                        self._orb_evoke(evoked_orb) * evoke_count
                    )
                elif orb_id == "plasma":
                    plasma_energy = self._orb_evoke(evoked_orb) * evoke_count
                    energy_gain += plasma_energy
                elif orb_id in {"lightning", "dark"} and evoke_count > 0:
                    (
                        hp,
                        block,
                        damage_value,
                        evoke_value,
                        evoke_kill,
                    ) = self._resolve_damage_orb_evokes(
                        game,
                        monsters,
                        hp,
                        block,
                        corpse,
                        damage_value,
                        mode_shift_remaining,
                        neutralized,
                        evoked_orb,
                        evoke_count,
                        electrodynamics=player_electrodynamics,
                        lock_on=lock_on,
                        uncertain_damage=uncertain_orb_hp_loss,
                    )
                    dynamic_direct += evoke_value
                    direct_attack_kill = direct_attack_kill or evoke_kill
                if card_id in {"recursion", "redo"}:
                    evolving_orbs = evolving_orbs + (
                        self._projected_orb(
                            game,
                            orb_id,
                            focus_override=(
                                combat_predictor.signed_power_amount(
                                    game.player, "Focus"
                                )
                                + player_focus_bonus
                            ),
                        ),
                    )
            orbs = evolving_orbs

        if card_id == "fission" and int(getattr(card, "upgrades", 0) or 0) > 0:
            # Evokes resolve immediately, before end-of-turn poison.  A
            # monster classified as passively doomed is therefore still a
            # legal Lightning target and, for Dark, can still be the required
            # lowest-HP target.  Restricting this set to ``active_indexes``
            # let Fission redirect damage away from a poisoned-but-living
            # enemy and invent a kill on the remaining attacker.
            for orb in orbs_before_card:
                orb_id = self._orb_id(orb)
                if orb_id not in {"lightning", "dark"}:
                    continue
                alive_before = {
                    index for index, value in enumerate(hp) if value > 0
                }
                (
                    hp,
                    block,
                    damage_value,
                    evoke_value,
                    evoke_kill,
                ) = self._resolve_damage_orb_evokes(
                    game,
                    monsters,
                    hp,
                    block,
                    corpse,
                    damage_value,
                    mode_shift_remaining,
                    neutralized,
                    orb,
                    1,
                    electrodynamics=player_electrodynamics,
                    lock_on=lock_on,
                    uncertain_damage=uncertain_orb_hp_loss,
                )
                # Keep Fission's deliberately conservative random-Lightning
                # discount, but exact single-target/Electrodynamics packets
                # receive their full transition value.
                if (
                    orb_id == "lightning"
                    and len(alive_before) > 1
                    and not player_electrodynamics
                ):
                    evoke_value *= 0.5
                dynamic_direct += evoke_value
                direct_attack_kill = direct_attack_kill or evoke_kill
                if evoke_kill:
                    killed_indexes = [
                        index
                        for index in alive_before
                        if hp[index] <= 0
                    ]
                    dynamic_direct += sum(
                        7
                        + combat_predictor.monster_threat(monsters[index])
                        * 0.25
                        for index in killed_indexes
                    )

        # Advance the authoritative deterministic orb state. This is also the
        # state used by end-turn passives, so consuming Fission cannot leave a
        # ghost Lightning kill and newly channelled orbs are not omitted.
        def resolve_overflow_evokes(overflow_evokes):
            nonlocal hp, block, damage_value
            nonlocal channel_frost_evoke_block, energy_gain
            nonlocal orb_utility_correction, dynamic_direct
            nonlocal direct_attack_kill
            for evoked_orb in overflow_evokes:
                evoked_id = self._orb_id(evoked_orb)
                if evoked_id == "frost":
                    channel_frost_evoke_block += self._orb_evoke(
                        evoked_orb
                    )
                elif evoked_id == "plasma":
                    plasma_energy = self._orb_evoke(evoked_orb)
                    energy_gain += plasma_energy
                elif evoked_id in {"lightning", "dark"}:
                    (
                        hp,
                        block,
                        damage_value,
                        evoke_value,
                        evoke_kill,
                    ) = self._resolve_damage_orb_evokes(
                        game,
                        monsters,
                        hp,
                        block,
                        corpse,
                        damage_value,
                        mode_shift_remaining,
                        neutralized,
                        evoked_orb,
                        1,
                        electrodynamics=player_electrodynamics,
                        lock_on=lock_on,
                        uncertain_damage=uncertain_orb_hp_loss,
                    )
                    dynamic_direct += evoke_value
                    direct_attack_kill = (
                        direct_attack_kill or evoke_kill
                    )

        if card_id == "fission":
            orbs = ()
        elif card_id in orb_consumer_ids:
            pass
        elif getattr(card, "type", None) == CardType.POWER:
            evolving_orbs = tuple(orbs_before_card)
            for _ in range(resolution_copies):
                if card_id in {"defragment", "biasedcognition"}:
                    focus_gain = (
                        1 + int(getattr(card, "upgrades", 0) or 0)
                        if card_id == "defragment"
                        else 4
                        + int(getattr(card, "upgrades", 0) or 0)
                    )
                    player_focus_bonus += focus_gain
                    evolving_orbs = self._adjust_orb_focus(
                        evolving_orbs, focus_gain
                    )
                if card_id == "capacitor":
                    serialized_slots = max(
                        0,
                        int(getattr(card, "magic_number", 0) or 0),
                    )
                    orb_slots = min(10, orb_slots + max(
                        serialized_slots,
                        2 + int(getattr(card, "upgrades", 0) or 0),
                    ))
                evolving_orbs, overflow_evokes = (
                    self._advance_channelled_orbs(
                        game,
                        evolving_orbs,
                        orb_slots,
                        card,
                        sum(1 for value in hp if value > 0),
                        focus_override=(
                            combat_predictor.signed_power_amount(
                                game.player, "Focus"
                            )
                            + player_focus_bonus
                        ),
                        resolution_copies=1,
                        extra_orb_ids=(
                            ("lightning",) * max(0, player_storm)
                        ),
                    )
                )
                resolve_overflow_evokes(overflow_evokes)
                if card_id == "storm":
                    player_storm += 1
            orbs = evolving_orbs
        else:
            orbs, overflow_evokes = self._advance_channelled_orbs(
                game,
                orbs_before_card,
                orb_slots,
                card,
                sum(1 for value in hp if value > 0),
                focus_override=(
                    combat_predictor.signed_power_amount(
                        game.player, "Focus"
                    )
                    + player_focus_bonus
                ),
                resolution_copies=resolution_copies,
                extra_orb_ids=(
                    ("lightning",) * storm_lightning_channels
                ),
            )
            resolve_overflow_evokes(overflow_evokes)
        settle_pending_malleable()
        if not can_gain_block:
            channel_frost_evoke_block = 0

        # Poison/Catalyst are advanced against the state produced by earlier
        # cards, so Deadly Poison -> Catalyst is visible even when Catalyst
        # appeared first in the serialized hand.
        poison_static = 0.0
        poison_dynamic = 0.0
        exact_poison = card_id == "catalyst" or (
            card_id in self.POISON_CARDS
            and card_id not in {"noxiousfumes", "poisonedstab"}
            and not (card_id == "bouncingflask" and len(active_indexes) > 1)
        )
        if exact_poison:
            poison_static = self._poison_value(game, card, candidate.target)
            poison_application_bonus = int(
                self._has_relic(game, "Snecko Skull")
            )
            passively_doomed_ids = (
                {
                    id(monster)
                    for monster in combat_predictor.projected_doomed_monsters(game)
                }
                if card_id != "corpseexplosion"
                else set()
            )
            poison_targets = (
                [target_index]
                if target_index is not None
                else sorted(active_indexes)
            )
            room_multiplier = 1.05 if getattr(game, "room_type", "") in {"MonsterRoomBoss", "MonsterRoomElite"} else 0.72
            for index in poison_targets:
                if index is None or hp[index] <= 0:
                    continue
                passively_doomed = id(monsters[index]) in passively_doomed_ids
                # Echo Form repeats the whole card resolution. Replay each
                # debuff action against the Artifact state left by the prior
                # copy; grouping all Poison and then all Weak actions gives a
                # different result for Crippling Cloud.
                for poison_resolution_index in range(resolution_copies):
                    if hp[index] <= 0:
                        if target_index is not None:
                            resolution_copies = min(
                                resolution_copies,
                                poison_resolution_index,
                            )
                        break
                    if card_id == "catalyst":
                        if poison[index] <= 0:
                            # Playing an exhausting Catalyst with no Poison has no
                            # card effect.  Keep its negative static value in the
                            # exact branch instead of cancelling it during the
                            # static-to-dynamic score replacement.
                            value = -18.0
                        elif artifact[index] > 0:
                            # Artifact blocks Catalyst's single Poison
                            # application even when an older Poison stack already
                            # exists (the Corrupt Heart can create this state).
                            artifact[index] -= 1
                            value = -18.0
                        else:
                            added = poison[index] * (
                                self._catalyst_multiplier(card) - 1
                            ) + poison_application_bonus
                            poison[index] += added
                            trigger_sadistic_nature(index)
                            value = 0.0 if passively_doomed else added * 0.9
                    else:
                        if card_id == "bouncingflask":
                            absorbed = 0
                            landed_poison = 0
                            for _ in range(
                                self._bouncing_flask_applications(card)
                            ):
                                if artifact[index] > 0:
                                    artifact[index] -= 1
                                    absorbed += 1
                                else:
                                    applied = 3 + poison_application_bonus
                                    poison[index] += applied
                                    landed_poison += applied
                                    trigger_sadistic_nature(index)
                            value = (
                                min(landed_poison, max(1, hp[index]))
                                * room_multiplier
                                + absorbed * 0.75
                            )
                            if passively_doomed:
                                value = 0.0
                        else:
                            amount = max(
                                1,
                                int(getattr(card, "magic_number", 0) or 0),
                            ) + poison_application_bonus
                            if artifact[index] > 0:
                                artifact[index] -= 1
                                value = 0.0 if passively_doomed else 0.75
                            else:
                                poison[index] += amount
                                trigger_sadistic_nature(index)
                                value = (
                                    min(amount, max(1, hp[index]))
                                    * room_multiplier
                                )
                                if passively_doomed:
                                    value = 0.0
                        if card_id == "corpseexplosion":
                            if artifact[index] > 0:
                                artifact[index] -= 1
                            else:
                                corpse[index] += 1
                                trigger_sadistic_nature(index)
                                if len(monsters) > 1:
                                    value += 10
                    poison_value[index] += value
                    poison_dynamic += value
                    if card_id == "cripplingpoison":
                        # Base-game action order is Poison -> Weak inside
                        # each full resolution, not all Poison copies first.
                        apply_branch_weak_debuff_once((index,))
                    if hp[index] <= 0 and target_index is not None:
                        resolution_copies = min(
                            resolution_copies,
                            poison_resolution_index + 1,
                        )
                        break

            if (
                poison_static < 0
                and candidate.target is not None
                and self._is_champ(candidate.target)
                and combat_predictor.power_amount(candidate.target, "Poison") > 0
            ):
                # Encounter-specific "save Catalyst" signals are policy
                # constraints, not stale poison estimates to be replaced.
                poison_static = 0.0
                poison_dynamic = 0.0

        if card_id == "choke" and target_index is not None and hp[target_index] > 0:
            serialized_choke = int(getattr(card, "magic_number", 0) or 0)
            choke_amount = (
                serialized_choke
                if serialized_choke > 0
                else 3 + 2 * int(getattr(card, "upgrades", 0) or 0)
            )
            for _ in range(resolution_copies):
                if artifact[target_index] > 0:
                    artifact[target_index] -= 1
                else:
                    choke[target_index] += choke_amount
                    trigger_sadistic_nature(target_index)

        # Weak and Vulnerable are separate ApplyPower actions. A duplicated
        # Shockwave/Uppercut performs Weak -> Vulnerable for copy one, then
        # repeats that order for copy two, so Artifact depleted by the first
        # resolution cannot incorrectly protect the second one.
        if getattr(card, "type", None) != CardType.ATTACK:
            for debuff_resolution_index in range(resolution_copies):
                if card_id != "cripplingpoison":
                    apply_branch_weak_debuff_once()
                apply_branch_vulnerable_debuff_once()
                if (
                    target_index is not None
                    and hp[target_index] <= 0
                ):
                    resolution_copies = min(
                        resolution_copies,
                        debuff_resolution_index + 1,
                    )
                    break

        if card_id in self.STRENGTH_DOWN_CARDS:
            strength_down_amount = max(
                0, int(getattr(card, "magic_number", 0) or 0)
            )
            strength_down_targets = (
                sorted(active_indexes)
                if card_id == "piercingwail"
                else [target_index]
            )
            for strength_resolution_index in range(resolution_copies):
                for index in strength_down_targets:
                    if (
                        index is None
                        or hp[index] <= 0
                        or strength_down_amount <= 0
                    ):
                        continue
                    if artifact[index] > 0:
                        artifact[index] -= 1
                    else:
                        enemy_strength_reduction[index] += strength_down_amount
                        trigger_sadistic_nature(index)
                if (
                    target_index is not None
                    and hp[target_index] <= 0
                ):
                    resolution_copies = min(
                        resolution_copies,
                        strength_resolution_index + 1,
                    )
                    break

        # Malaise applies Strength-down and Weak as two separate debuffs.
        # Advance both Artifact checks in their real action order and retain
        # both effects in branch-local state.
        if card_id == "malaise" and target_index is not None:
            malaise_amount = self._x_effect(
                game,
                card,
                upgraded_bonus=True,
                energy_override=state.energy,
            )
            if malaise_amount > 0 and hp[target_index] > 0:
                for malaise_resolution_index in range(resolution_copies):
                    if artifact[target_index] > 0:
                        artifact[target_index] -= 1
                    else:
                        enemy_strength_reduction[
                            target_index
                        ] += malaise_amount
                        trigger_sadistic_nature(target_index)
                    if artifact[target_index] > 0:
                        artifact[target_index] -= 1
                    else:
                        weak[target_index] += malaise_amount
                        trigger_sadistic_nature(target_index)
                    if hp[target_index] <= 0:
                        resolution_copies = min(
                            resolution_copies,
                            malaise_resolution_index + 1,
                        )
                        break

        if resolution_copies < planned_resolution_copies:
            cancelled_resolutions = (
                planned_resolution_copies - resolution_copies
            )
            for index, strength_per_resolution in enumerate(
                reactive_enemy_strength_per_resolution
            ):
                enemy_strength_bonus[index] -= (
                    strength_per_resolution * cancelled_resolutions
                )
            hex_dazed_count = (
                max(0, int(player_hex or 0)) * resolution_copies
                if getattr(card, "type", None) != CardType.ATTACK
                else 0
            )

        # Non-Attack player/enemy use-card damage hooks still belong to each
        # full resolution.  Preserve Thousand Cuts -> Choke ordering and stop
        # a targeted autoplay when either hook leaves its target dying.
        if getattr(card, "type", None) != CardType.ATTACK:
            for hook_resolution_index in range(resolution_copies):
                if thousand_cuts_before_card > 0:
                    for index, monster in enumerate(monsters):
                        if hp[index] <= 0:
                            continue
                        dynamic_direct += self._apply_enemy_damage_packet(
                            monster,
                            index,
                            hp,
                            block,
                            damage_value,
                            mode_shift_remaining,
                            neutralized,
                            thousand_cuts_before_card,
                            blockable=True,
                        )
                for index, amount in enumerate(choke_before_card):
                    if hp[index] <= 0 or int(amount or 0) <= 0:
                        continue
                    dynamic_direct += self._apply_enemy_damage_packet(
                        monsters[index],
                        index,
                        hp,
                        block,
                        damage_value,
                        mode_shift_remaining,
                        neutralized,
                        amount,
                        blockable=False,
                    )
                if (
                    target_index is not None
                    and hp[target_index] <= 0
                ):
                    resolution_copies = min(
                        resolution_copies,
                        hook_resolution_index + 1,
                    )
                    break

        planned_skills = sum(
            1
            for planned in state.plan
            if getattr(planned.card, "type", None) == CardType.SKILL
        )
        letter_opener_packet = (
            combat_predictor.letter_opener_damage(game, planned_skills)
            if getattr(card, "type", None) == CardType.SKILL
            else 0
        )
        if letter_opener_packet > 0:
            # Letter Opener is non-Attack relic damage, but the game still
            # resolves it through enemy Block.  It does not create extra
            # Attack-hit reactions.
            for index, monster in enumerate(monsters):
                if hp[index] <= 0:
                    continue
                dealt = self._apply_enemy_damage_packet(
                    monster,
                    index,
                    hp,
                    block,
                    damage_value,
                    mode_shift_remaining,
                    neutralized,
                    letter_opener_packet,
                    blockable=True,
                )
                dynamic_direct += dealt

        damage_before_explosions = sum(damage_value)
        hp, block, damage_value = self._resolve_state_deaths(
            game,
            monsters,
            hp,
            block,
            corpse,
            damage_value,
            mode_shift_remaining=mode_shift_remaining,
            neutralized=neutralized,
        )
        # Death resolution returns immutable snapshots.  This branch keeps
        # simulating same-card follow-ups (for example Charon's Ashes after an
        # exhausting attack), so restore its mutable working state first.
        hp = list(hp)
        block = list(block)
        damage_value = list(damage_value)
        dynamic_direct += max(0.0, sum(damage_value) - damage_before_explosions)
        for index, monster in enumerate(monsters):
            if self._transition_suppresses_action(
                monster, hp[index], damage_value[index]
            ):
                neutralized.add(index)
        dynamic_base = (
            candidate.base_score
            - self._initial_direct_score(game, candidate)
            - poison_static
            + dynamic_direct
            + dynamic_fatal_utility
            + poison_dynamic
            + orb_utility_correction
            + draw_timing_adjustment
            + duplicated_created_utility
            + seek_target_value
        )
        if (
            card_id == "evolve"
            and not state.future_draw_pile_unknown
            and type(state.draw_pile_size) is int
            and 0 <= state.draw_pile_size
            <= len(getattr(game, "draw_pile", []) or [])
        ):
            # Candidate construction happens once for the authoritative
            # frame, but a branch may draw known top cards before Evolve is
            # played.  Reprice only that lifecycle component at resolution
            # time so Battle Trance/other deterministic draws cannot leave
            # already-consumed Status cards in Evolve's future benefit.
            resolved_lifecycle = self._lifecycle_evaluation(
                game, card, 0, draw_pile_size=state.draw_pile_size
            )
            dynamic_base += (
                float(resolved_lifecycle.get("adjustment", 0.0) or 0.0)
                - float(candidate.lifecycle_adjustment or 0.0)
            )
        lifecycle_liability = max(0.0, candidate.expected_lifecycle_cost - candidate.expected_lifecycle_benefit)
        if card_id in {"ghostly", "apparition", "capacitor", "biasedcognition"}:
            resolved_lifecycle = self._lifecycle_evaluation(
                game, card, 0, intangible_turns=state.player_intangible_turns,
                orb_slots=state.orb_slots, orbs=state.orbs, artifact=state.player_artifact,
                resolution_copies=resolution_copies,
            )
            dynamic_base += resolved_lifecycle["adjustment"] - candidate.lifecycle_adjustment
            lifecycle_liability = max(0.0, resolved_lifecycle["cost"] - resolved_lifecycle["benefit"])
        # Hex inserts Dazed at a random draw-pile position.  The exact count
        # is known but the next concrete draw is not, so charge a bounded
        # future-deck cost and mark the pile for authoritative re-planning.
        dynamic_base -= hex_dazed_count * 1.5
        if card_id in {"panacea", "coresurge"}:
            player_artifact += resolution_copies * max(
                1, int(getattr(card, "magic_number", 0) or 0)
            )
        elif card_id == "biasedcognition":
            for _ in range(resolution_copies):
                if player_artifact > 0:
                    player_artifact -= 1
                    # Artifact must exist before Biased Cognition; playing a
                    # source later cannot remove the already-applied Bias.
                    dynamic_base += 24.0
                # Unprotected decay is priced across the lifecycle above.
        immediate_strength_gain = self._immediate_strength_gain(
            game, card, candidate.target, player_strength_bonus
        )
        if card_id == "limitbreak" and immediate_strength_gain > 0:
            starting_strength = immediate_strength_gain
            immediate_strength_gain = (
                starting_strength * (2 ** resolution_copies - 1)
            )
        else:
            immediate_strength_gain *= resolution_copies
        player_strength_bonus += immediate_strength_gain
        if card_id == "flex" and immediate_strength_gain > 0:
            if player_artifact > 0:
                # Artifact consumes Flex's delayed Strength-down debuff, so
                # the gain is no longer temporary even without an attack.
                player_artifact -= 1
            else:
                unconsumed_temporary_strength += immediate_strength_gain
        elif card_id == "limitbreak" and unconsumed_temporary_strength > 0:
            # Flex 2 -> Limit Break 4 -> end-turn -2 leaves two permanent
            # Strength. The setup therefore has a real downstream consumer.
            unconsumed_temporary_strength = 0
        candidate_other = (
            candidate.stateful_mitigation
            - candidate.state_block_mitigation
            - candidate.buffer_mitigation
            - candidate.intangible_mitigation
        )
        if (
            card_id in self.WEAK_CARDS
            or card_id in self.STRENGTH_DOWN_CARDS
            or card_id == "malaise"
        ):
            # These effects now live in branch Weak/Strength state and are
            # applied per hit before Block. Keeping their old scalar HP
            # mitigation would double count them and corrupt on-hit powers.
            candidate_other = 0
        candidate_block = 0

        static_self_cost = max(0, candidate.self_hp_cost)
        static_reactive_cost = max(0, candidate.reactive_hp_cost)
        self_outcome = combat_predictor.resolve_player_damage_events(
            game,
            tuple(
                combat_predictor.PlayerDamageEvent(
                    "card_self", amount, blockable=False
                )
                for amount in candidate.self_damage_events * resolution_copies
            ),
            block=player_block,
            buffer_layers=player_buffer,
            force_intangible=player_intangible,
        )
        dynamic_clay_hp_loss_events += len(self_outcome.hp_loss_events)
        dynamic_nonreactive_cost = self_outcome.hp_loss
        rupture_trigger_count = (
            len(self_outcome.hp_loss_events)
            if player_rupture > 0
            else 0
        )
        rupture_strength_gain = (
            rupture_trigger_count * max(0, int(player_rupture or 0))
        )
        if rupture_strength_gain > 0:
            # Rupture's ApplyPowerAction is queued to the top by wasHPLost,
            # so the Strength is authoritative before the next hand card.
            # It does not retroactively change the card whose LoseHPAction
            # caused the trigger because that card's damage already resolved.
            player_strength_bonus += rupture_strength_gain
            dynamic_base += rupture_strength_gain * 2.4
        player_hp_state -= dynamic_nonreactive_cost
        if player_hp_state <= 0:
            return None
        player_block = self_outcome.block
        player_buffer = self_outcome.buffer
        if card_id in self.INTANGIBLE_CARDS:
            player_intangible = True
        intangible_gain = (
            (1 if card_id in {"ghostly", "apparition"} else
             2 + int(getattr(card, "upgrades", 0) or 0))
            * resolution_copies if card_id in self.INTANGIBLE_CARDS else 0
        )
        dynamic_nonattack_healing_actual = 0
        nonattack_use_hooks_resolved = False
        if getattr(card, "type", None) == CardType.ATTACK:
            player_buffer += (
                max(0, candidate.buffer_gain) * resolution_copies
            )

        after_image_gain = (
            player_after_image * resolution_copies
            + (
                max(1, int(getattr(card, "magic_number", 0) or 0))
                * resolution_copies
                * (resolution_copies - 1)
                // 2
                if card_id == "afterimage"
                else 0
            )
            if can_gain_block
            else 0
        )
        rage_gain = (
            player_rage * resolution_copies
            if can_gain_block
            and getattr(card, "type", None) == CardType.ATTACK
            else 0
        )
        # CommunicationMod applies Dexterity/Frail to the serialized ``block``
        # field even for cards that do not grant block.  ``base_block`` is the
        # authoritative capability flag; without this guard an attack can be
        # simulated as replacing Orichalcum with a few points of fake block.
        if getattr(card, "type", None) != CardType.ATTACK:
            if (
                resolution_copies > 1
                and any(amount > 0 for amount in beat_at_card_start)
                and (
                    channel_frost_evoke_block > 0
                    or tough_bandages_gain > 0
                    or guaranteed_self_exhaust_count * feel_no_pain > 0
                )
            ):
                # These body-generated Block sources are currently stored as
                # aggregates, so their exact placement between copied Beat
                # packets is unknowable.  Refuse this speculative branch
                # instead of pooling the Block into a false Heart survival.
                return None
            after_image_stack_gain = (
                max(1, int(getattr(card, "magic_number", 0) or 0))
                if card_id == "afterimage"
                else 0
            )
            for resolution_index in range(resolution_copies):
                player_buffer += max(0, candidate.buffer_gain)
                apply_intrinsic_block_resolution(resolution_index)
                if candidate.healing_gain > 0:
                    before_heal = player_hp_state
                    player_hp_state = min(
                        max(
                            1,
                            int(
                                getattr(game.player, "max_hp", 1) or 1
                            ),
                        ),
                        player_hp_state
                        + max(0, candidate.healing_gain),
                    )
                    dynamic_nonattack_healing_actual += max(
                        0, player_hp_state - before_heal
                    )
                active_after_image = (
                    player_after_image
                    + resolution_index * after_image_stack_gain
                )
                if can_gain_this_resolution and active_after_image > 0:
                    player_block += active_after_image
                    if player_dexterity_bonus > 0:
                        player_block += self._branch_dexterity_block_gain(
                            game,
                            active_after_image,
                            player_dexterity_bonus,
                        )
                for beat_damage in beat_at_card_start:
                    if beat_damage <= 0:
                        continue
                    beat_outcome = (
                        combat_predictor.resolve_player_damage_events(
                            game,
                            (
                                combat_predictor.PlayerDamageEvent(
                                    "beat_of_death",
                                    beat_damage,
                                    blockable=True,
                                ),
                            ),
                            block=player_block,
                            buffer_layers=player_buffer,
                            force_intangible=player_intangible,
                        )
                    )
                    dynamic_clay_hp_loss_events += len(
                        beat_outcome.hp_loss_events
                    )
                    dynamic_beat_cost += beat_outcome.hp_loss
                    player_hp_state -= beat_outcome.hp_loss
                    player_block = beat_outcome.block
                    player_buffer = beat_outcome.buffer
                    if player_hp_state <= 0:
                        return None
            nonattack_use_hooks_resolved = True
        # Supported Attack+Block cards were advanced inside each attack
        # resolution.  Unknown Attack block ordering fails closed at zero;
        # Wallop's actual-damage Block was handled before its reaction.
        immediate_block_gain = intrinsic_block_gain_total
        extra_immediate_block = 0
        if card_id not in orb_consumer_ids:
            extra_immediate_block += self._frost_evoke_mitigation(
                game,
                card,
                orbs=orbs_before_card,
                energy_override=state.energy,
            )[1]
        extra_immediate_block += other_exhaust_count * feel_no_pain
        if not can_gain_block:
            extra_immediate_block = 0
        player_block += extra_immediate_block
        immediate_block_gain += extra_immediate_block
        generated_block_gain = 0
        if not attack_reactions_resolved:
            # Attack reactions were already interleaved with every complete
            # card resolution above.  This fallback remains for future
            # non-Attack reactive sources and must never replay those events.
            thorns_outcome = combat_predictor.resolve_player_damage_events(
                game,
                tuple(
                    combat_predictor.PlayerDamageEvent(
                        "thorns", amount, blockable=True
                    )
                    for amount in candidate.thorns_damage_events
                    * reaction_resolution_copies
                ),
                block=player_block,
                buffer_layers=player_buffer,
                force_intangible=player_intangible,
            )
            dynamic_clay_hp_loss_events += len(
                thorns_outcome.hp_loss_events
            )
            dynamic_thorns_cost += thorns_outcome.hp_loss
            player_hp_state -= thorns_outcome.hp_loss
            if player_hp_state <= 0:
                return None
            player_block = thorns_outcome.block
            player_buffer = thorns_outcome.buffer
        deferred_after_image_gain = (
            0
            if attack_reactions_resolved or nonattack_use_hooks_resolved
            else after_image_gain
        )
        deferred_rage_gain = (
            0 if attack_reactions_resolved else rage_gain
        )
        deferred_fan_gain = (
            0 if attack_reactions_resolved else fan_gain
        )
        player_block += (
            max(0, channel_frost_evoke_block)
            + deferred_after_image_gain
            + deferred_rage_gain
            + deferred_fan_gain
            + tough_bandages_gain
        )
        if player_dexterity_bonus > 0:
            # These are separate gainBlock calls in the game, so a Kunai /
            # Footwork delta also passes through Frail independently for each
            # source.  Adding the raw Dexterity amount here overstates Block
            # whenever the player is Frail.
            for raw_gain in (
                channel_frost_evoke_block,
                deferred_after_image_gain,
                deferred_rage_gain,
                deferred_fan_gain,
                tough_bandages_gain,
            ):
                if raw_gain > 0:
                    player_block += self._branch_dexterity_block_gain(
                        game, raw_gain, player_dexterity_bonus
                    )
        if card_id in {"reaper", "bite"}:
            # Lifesteal was capped and applied after each full Attack copy,
            # after that copy's reactive damage.  Reusing the aggregate
            # request here would heal the player twice and would also ignore
            # intervening max-HP caps.
            actual_healing = dynamic_attack_healing_actual
        elif nonattack_use_hooks_resolved:
            actual_healing = dynamic_nonattack_healing_actual
        else:
            healing_before_card = player_hp_state
            requested_healing = (
                max(0, candidate.healing_gain) * resolution_copies
            )
            actual_healing = max(0, player_hp_state - healing_before_card)
            if requested_healing > 0:
                player_hp_state = min(
                    max(1, int(getattr(game.player, "max_hp", 1) or 1)),
                    player_hp_state + requested_healing,
                )
                actual_healing = max(
                    0, player_hp_state - healing_before_card
                )
        if actual_healing > 0:
            static_healing = min(
                max(0, candidate.healing_gain), actual_healing
            )
            dynamic_base += max(0, actual_healing - static_healing) * (
                1.4 if card_id == "reaper" else 1.5
            )
        sharp_hide_events = (
            ()
            if attack_reactions_resolved
            else tuple(
                candidate.sharp_hide_damage_events
                * reaction_resolution_copies
            )
        )
        sharp_hide_outcome = combat_predictor.resolve_player_damage_events(
            game,
            tuple(
                combat_predictor.PlayerDamageEvent(
                    "sharp_hide", amount, blockable=True
                )
                for amount in sharp_hide_events
            ),
            block=player_block,
            buffer_layers=player_buffer,
            force_intangible=player_intangible,
        )
        dynamic_clay_hp_loss_events += len(
            sharp_hide_outcome.hp_loss_events
        )
        dynamic_sharp_hide_cost += sharp_hide_outcome.hp_loss
        player_hp_state -= sharp_hide_outcome.hp_loss
        if player_hp_state <= 0:
            return None
        player_block = sharp_hide_outcome.block
        player_buffer = sharp_hide_outcome.buffer
        if not attack_reactions_resolved and not nonattack_use_hooks_resolved:
            for _ in range(resolution_copies):
                for beat_damage in beat_at_card_start:
                    if beat_damage <= 0:
                        continue
                    beat_outcome = (
                        combat_predictor.resolve_player_damage_events(
                            game,
                            (
                                combat_predictor.PlayerDamageEvent(
                                    "beat_of_death",
                                    beat_damage,
                                    blockable=True,
                                ),
                            ),
                            block=player_block,
                            buffer_layers=player_buffer,
                            force_intangible=player_intangible,
                        )
                    )
                    dynamic_clay_hp_loss_events += len(
                        beat_outcome.hp_loss_events
                    )
                    dynamic_beat_cost += beat_outcome.hp_loss
                    player_hp_state -= beat_outcome.hp_loss
                    if player_hp_state <= 0:
                        return None
                    player_block = beat_outcome.block
                    player_buffer = beat_outcome.buffer
        player_block += guaranteed_self_exhaust_count * feel_no_pain
        if player_dexterity_bonus > 0 and feel_no_pain > 0:
            player_block += guaranteed_self_exhaust_count * (
                self._branch_dexterity_block_gain(
                    game, feel_no_pain, player_dexterity_bonus
                )
            )
        dynamic_reactive_cost = (
            dynamic_thorns_cost
            + dynamic_sharp_hide_cost
            + dynamic_beat_cost
        )
        dynamic_self_cost = (
            dynamic_nonreactive_cost
            + dynamic_reactive_cost
        )
        _, _, clay_credit_before = self._self_forming_clay_value(
            game, state.self_forming_clay_hp_loss_events
        )
        (
            total_clay_hp_loss_events,
            _total_clay_future_block,
            total_clay_credit,
        ) = self._self_forming_clay_value(
            game,
            state.self_forming_clay_hp_loss_events
            + dynamic_clay_hp_loss_events,
        )
        # Candidate.base_score already includes its static Clay estimate.
        # Replace only that estimate with the exact branch-local marginal
        # credit; the resulting value remains wholly outside mitigation.
        dynamic_base += (
            total_clay_credit
            - clay_credit_before
            - candidate.self_forming_clay_credit
        )
        player_hp = max(
            1, int(getattr(game.player, "current_hp", 1) or 1)
        )
        reactive_weight = self._reactive_attrition_weight(game, player_hp)
        dynamic_base += static_self_cost - dynamic_self_cost
        dynamic_base += (
            static_reactive_cost - dynamic_reactive_cost
        ) * reactive_weight
        if static_self_cost >= player_hp:
            dynamic_base += 1000
        if dynamic_self_cost >= player_hp:
            dynamic_base -= 1000
        forced_exhaust_lifecycle_cost = 0.0
        forced_exhaust_lifecycle_cards = []
        for index in other_exhaust_indexes:
            exhausted_card = game.hand[index]
            lifecycle = self._lifecycle_evaluation(
                game, exhausted_card, total_loss
            )
            adjustment = max(
                float((lifecycle or {}).get("adjustment", 0.0) or 0.0),
                self._forced_exhaust_resource_value(game, exhausted_card),
            )
            if adjustment <= 0:
                continue
            forced_exhaust_lifecycle_cost += adjustment
            forced_exhaust_lifecycle_cards.append((
                getattr(exhausted_card, "card_id", ""),
                getattr(exhausted_card, "uuid", None),
                int((lifecycle or {}).get("triggers", 0) or 0),
                round(adjustment, 3),
            ))
        for index in other_exhaust_indexes:
            if index in remaining_hand_indexes:
                remaining_hand_indexes.remove(index)
        removed_hand_count += other_exhaust_count
        for index in unload_discard_indexes:
            if index in remaining_hand_indexes:
                remaining_hand_indexes.remove(index)
        exhaust_count = guaranteed_self_exhaust_count + other_exhaust_count
        charons_packet = (
            combat_predictor.charons_ashes_damage(game) * exhaust_count
        )
        if charons_packet > 0:
            # Charon's Ashes deals normal damage to every enemy, so existing
            # Block absorbs it before HP.  It does not trigger attack-only
            # reactions, but that is separate from blockability.
            # Apply the aggregate packet after this card's exhaust choices;
            # the authoritative next frame will re-plan any remaining hand.
            for index, monster in enumerate(monsters):
                if hp[index] <= 0:
                    continue
                dealt = self._apply_enemy_damage_packet(
                    monster,
                    index,
                    hp,
                    block,
                    damage_value,
                    mode_shift_remaining,
                    neutralized,
                    charons_packet,
                    blockable=True,
                )
                dynamic_direct += dealt
        dark_embrace_draws = (
            0
            if state.no_draw
            else exhaust_count * int(dark_embrace)
        )
        dynamic_hand_additions += (
            dark_embrace_draws + exhaust_count * int(dead_branch)
        )
        # Newly established Dark Embrace is absent from the candidate's
        # authoritative-frame exhaust bonus. Value its actual draw continuation,
        # rather than depending on an unrelated nominal energy bonus.
        new_embrace_layers = max(
            0, int(dark_embrace)
            - combat_predictor.power_amount(game.player, "Dark Embrace", "DarkEmbracePower"),
        )
        if not state.no_draw:
            dynamic_base += min(
                dark_embrace_draws,
                exhaust_count * new_embrace_layers,
                max(0, 10 - max(0, state.hand_size - 1)),
            ) * 2.2
        ink_bottle_draw = 0
        if ink_bottle is not None:
            ink_counter = max(
                0, int(getattr(ink_bottle, "counter", 0) or 0)
            )
            if not state.no_draw:
                ink_bottle_draw = sum(
                    1
                    for resolution_offset in range(
                        1, resolution_copies + 1
                    )
                    if (
                        ink_counter
                        + state.cards_played
                        + resolution_offset
                    )
                    % 10
                    == 0
                )
                dynamic_hand_additions += ink_bottle_draw
        generated_block_for_card = max(
            0,
            immediate_block_gain
            + channel_frost_evoke_block
            + after_image_gain
            + rage_gain
            + fan_gain
            + tough_bandages_gain
            + guaranteed_self_exhaust_count * feel_no_pain,
        )
        dynamic_base -= self._low_efficiency_self_damage_penalty(
            game,
            card,
            candidate,
            hp_damage=direct_attack_hp_damage,
            self_hp_cost=dynamic_nonreactive_cost,
            killed=direct_attack_kill,
            neutralized=bool(neutralized - neutralized_before_card),
            generated_block=generated_block_for_card,
            hand_additions=dynamic_hand_additions,
            energy_gain=energy_gain,
            secondary_damage=max(
                0.0, dynamic_direct - direct_attack_hp_damage
            ),
            targets=[
                monsters[index]
                for index in sorted(direct_attack_target_indexes)
            ],
        )
        # A discard choice cannot preserve every named old-hand card when the
        # card discarded more than it actually drew.  In that case stop after
        # this action and retain only unplayable end-turn hazards as a
        # conservative terminal representative.  If enough new unknown cards
        # were drawn, discarding those is a realizable branch and every old
        # concrete card really can remain; terminal-plan binding is still
        # disabled so the authoritative choice result is re-planned.
        choice_discard_unknown = (
            discard_count > max(0, int(effective_draw or 0))
            and card_id in {
                "acrobatics",
                "concentrate",
                "daggerthrow",
                "prepared",
                "survivor",
            }
        )
        redraw_hand_unknown = (
            card_id in {"calculatedgamble", "stormofsteel"}
            and discard_count > 0
        )
        reboot_hand_unknown = card_id == "reboot"
        if choice_discard_unknown:
            abstract_final_size = max(
                0,
                state.hand_size
                - removed_hand_count
                + dynamic_hand_additions
                - discard_count,
            )
            end_turn_hazard_ids = {
                "burn", "decay", "normality", "regret",
            }
            remaining_hand_indexes = [
                index
                for index in remaining_hand_indexes
                if _token(getattr(game.hand[index], "card_id", ""))
                in end_turn_hazard_ids
            ][:abstract_final_size]
            random_discard_unknown = True
        elif redraw_hand_unknown:
            remaining_hand_indexes = []
            random_discard_unknown = True
        elif reboot_hand_unknown:
            # Reboot shuffles every old card, including all cards which were
            # concrete beam candidates.  Its replacement draw is intentionally
            # left abstract and forces an authoritative re-plan.
            remaining_hand_indexes = []
            removed_hand_count = state.hand_size
            random_discard_unknown = True

        next_hand_size = max(
            len(remaining_hand_indexes),
            state.hand_size
            - removed_hand_count
            + dynamic_hand_additions
            - discard_count,
        )
        next_hand_size = min(10, next_hand_size)

        # Advance the exact draw-pile count for deterministic draws.  Reboot
        # first shuffles several zones together, and drawing past the known
        # pile triggers a reshuffle; both cases make the next size unknown and
        # therefore make a later Aggregate conservatively grant zero here.
        next_draw_pile_size = int(state.draw_pile_size or 0)
        deterministic_draws = (
            max(0, int(effective_draw or 0))
            + max(0, int(dark_embrace_draws or 0))
            + max(0, int(ink_bottle_draw or 0))
        )
        sundial_draws = deterministic_draws
        if card_id == "calculatedgamble":
            # Calculated Gamble empties the old hand before drawing its
            # replacement cards, so the ordinary pre-draw hand-cap clamp is
            # not the number of DrawCardAction resolutions which can cross
            # the pile boundary.
            sundial_draws = (
                max(0, int(branch_draw or 0))
                + max(0, int(dark_embrace_draws or 0))
                + max(0, int(ink_bottle_draw or 0))
            )
        sundial_draw_pile_size = max(
            0, int(state.sundial_draw_pile_size or 0)
        )
        sundial_discard_pile_size = max(
            0, int(state.sundial_discard_pile_size or 0)
        )
        sundial_shuffle_events = 0
        pre_draw_discards = (
            discard_count if card_id == "calculatedgamble" else 0
        )
        sundial_discard_pile_size += pre_draw_discards
        if card_id == "reboot":
            reboot_pool_size = (
                sundial_draw_pile_size
                + sundial_discard_pile_size
                + max(0, int(state.hand_size or 0) - 1)
            )
            if reboot_pool_size > 0:
                sundial_shuffle_events = resolution_copies
            sundial_draw_pile_size = max(
                0, reboot_pool_size - max(0, int(sundial_draws or 0))
            )
            sundial_discard_pile_size = 0
        elif card_id == "deepbreath":
            if sundial_discard_pile_size > 0:
                sundial_shuffle_events = resolution_copies
                sundial_draw_pile_size += sundial_discard_pile_size
                sundial_discard_pile_size = 0
            sundial_draw_pile_size = max(
                0,
                sundial_draw_pile_size
                - min(sundial_draw_pile_size, sundial_draws),
            )
        else:
            if (
                sundial_draws > sundial_draw_pile_size
                and sundial_discard_pile_size > 0
            ):
                sundial_shuffle_events = 1
                sundial_draw_pile_size = max(
                    0,
                    sundial_draw_pile_size
                    + sundial_discard_pile_size
                    - sundial_draws,
                )
                sundial_discard_pile_size = 0
            else:
                sundial_draw_pile_size = max(
                    0,
                    sundial_draw_pile_size
                    - min(sundial_draw_pile_size, sundial_draws),
                )
        if card_id != "reboot":
            sundial_discard_pile_size += max(
                0, discard_count - pre_draw_discards
            )
            sundial_discard_pile_size += int(played_card_enters_discard)
        generated_voids = resolution_copies if card_id == "turbo" else 0
        sundial_discard_pile_size += generated_voids

        sundial_counter = max(0, int(state.sundial_counter or 0)) % 3
        sundial_energy_gain = 0
        if self._has_relic(game, "Sundial"):
            for _ in range(max(0, int(sundial_shuffle_events or 0))):
                sundial_counter = (sundial_counter + 1) % 3
                if sundial_counter == 0:
                    sundial_energy_gain += 2
        drawn_voids = 0
        draw_energy_unknown = False
        if deterministic_draws > 0:
            authoritative_draw_pile = list(
                getattr(game, "draw_pile", []) or []
            )
            exact_known_order = (
                not state.future_draw_pile_unknown
                and int(state.draw_pile_size or 0) >= 0
                and int(state.draw_pile_size or 0)
                <= len(authoritative_draw_pile)
                and card_id not in {"reboot", "deepbreath"}
                and hex_dazed_count <= 0
            )
            known_drawn_cards = []
            if exact_known_order:
                known_size = int(state.draw_pile_size or 0)
                known_draw_count = min(deterministic_draws, known_size)
                if known_draw_count > 0:
                    # CommunicationMod serializes the draw-pile top at the
                    # list's end.  A prior exact draw only shrinks the active
                    # prefix, so this also remains correct later in a branch.
                    known_drawn_cards = authoritative_draw_pile[
                        known_size - known_draw_count:known_size
                    ]
                drawn_voids = sum(
                    1
                    for drawn_card in known_drawn_cards
                    if _token(getattr(drawn_card, "card_id", "")) == "void"
                )
                if deterministic_draws > known_size:
                    # The remainder comes from a reshuffle whose exact order
                    # is outside this bounded state.  Do not promise energy
                    # when a Void is present in a contributing zone.
                    draw_energy_unknown = any(
                        _token(getattr(zone_card, "card_id", "")) == "void"
                        for zone_card in (
                            list(getattr(game, "discard_pile", []) or [])
                            + list(getattr(game, "exhaust_pile", []) or [])
                        )
                    )
                if (
                    combat_predictor.has_power(
                        game.player, "Evolve", "EvolvePower"
                    )
                    and any(
                        getattr(drawn_card, "type", None)
                        == CardType.STATUS
                        for drawn_card in known_drawn_cards
                    )
                ):
                    # Evolve queues further draws after the known card.  The
                    # scalar draw state does not own that expanding sequence.
                    draw_energy_unknown = True
            else:
                draw_energy_unknown = any(
                    _token(getattr(zone_card, "card_id", "")) == "void"
                    for zone_card in (
                        authoritative_draw_pile
                        + list(getattr(game, "discard_pile", []) or [])
                    )
                )
        if (
            deterministic_draws > 0
            and state.generated_voids + generated_voids > 0
            and (
                not exact_known_order
                or deterministic_draws > int(state.draw_pile_size or 0)
            )
        ):
            # A simulated Turbo can contribute Void to a later reshuffle even
            # though it is absent from the authoritative discard snapshot.
            draw_energy_unknown = True
        if card_id in {"reboot", "deepbreath"}:
            next_draw_pile_size = -1
        elif hex_dazed_count > 0:
            # MakeTempCardInDrawPileAction uses a random insertion point.
            # Size remains knowable in the game, but this scalar state cannot
            # preserve the concrete ordering needed by Aggregate or draws.
            next_draw_pile_size = -1
        elif deterministic_draws > 0:
            if (
                next_draw_pile_size < 0
                or deterministic_draws > next_draw_pile_size
            ):
                next_draw_pile_size = -1
            else:
                next_draw_pile_size -= deterministic_draws
        if int(getattr(card, "cost", 0) or 0) == -1:
            initial_energy = max(
                0, int(getattr(game.player, "energy", 0) or 0)
            )
            # Card-specific effects above use the shared branch X value.
            # Keep only the coarse value adjustment for unsupported/random
            # outputs such as Doppelganger and Transmutation; scaling all
            # mitigation by energy would also scale away Chemical X.
            dynamic_base += (state.energy - initial_energy) * 3.0

        # Powers become available only after the setup card itself resolves.
        # In particular, a newly played After Image must not grant block for
        # its own card-play event, while an already-active copy still does.
        if card_id == "rupture":
            serialized_magic = max(
                0, int(getattr(card, "magic_number", 0) or 0)
            )
            player_rupture += (
                serialized_magic
                if serialized_magic > 0
                else 1 + int(getattr(card, "upgrades", 0) or 0)
            ) * resolution_copies
        elif card_id == "rage":
            serialized_magic = int(
                getattr(card, "magic_number", 0) or 0
            )
            rage_amount = (
                serialized_magic
                if serialized_magic > 0
                else 3 + 2 * int(getattr(card, "upgrades", 0) or 0)
            )
            player_rage += max(0, rage_amount) * resolution_copies
        elif card_id == "berserk":
            # Berserk's Vulnerable is immediate, not a next-turn-only power.
            player_vulnerable = True
        elif card_id == "footwork":
            serialized_magic = int(
                getattr(card, "magic_number", 0) or 0
            )
            dexterity_gain = (
                serialized_magic
                if serialized_magic > 0
                else 2 + int(getattr(card, "upgrades", 0) or 0)
            )
            player_dexterity_bonus += (
                max(0, dexterity_gain) * resolution_copies
            )
        elif card_id == "afterimage":
            player_after_image += max(
                1, int(getattr(card, "magic_number", 0) or 0)
            ) * resolution_copies
        elif card_id == "accuracy":
            serialized_magic = int(
                getattr(card, "magic_number", 0) or 0
            )
            player_accuracy += resolution_copies * (
                serialized_magic
                if serialized_magic > 0
                else 4 + 2 * int(getattr(card, "upgrades", 0) or 0)
            )
        elif card_id == "athousandcuts":
            serialized_magic = int(
                getattr(card, "magic_number", 0) or 0
            )
            player_thousand_cuts += resolution_copies * (
                serialized_magic
                if serialized_magic > 0
                else 1 + int(getattr(card, "upgrades", 0) or 0)
            )
        elif card_id == "sadisticnature":
            player_sadistic_nature += resolution_copies * max(
                5 + 2 * int(getattr(card, "upgrades", 0) or 0),
                int(getattr(card, "magic_number", 0) or 0),
            )
        elif card_id == "feelnopain":
            serialized_magic = int(
                getattr(card, "magic_number", 0) or 0
            )
            feel_no_pain_amount = (
                serialized_magic
                if serialized_magic > 0
                else 3 + int(getattr(card, "upgrades", 0) or 0)
            )
            player_feel_no_pain += (
                max(0, feel_no_pain_amount) * resolution_copies
            )
        elif card_id == "darkembrace":
            player_dark_embrace += resolution_copies
        elif card_id == "corruption":
            player_corruption = True
        elif card_id == "bullettime":
            player_bullet_time = True
        elif card_id == "heatsinks":
            serialized_magic = max(
                0, int(getattr(card, "magic_number", 0) or 0)
            )
            player_heatsinks += resolution_copies * (
                serialized_magic
                if serialized_magic > 0
                else 1 + int(getattr(card, "upgrades", 0) or 0)
            )
        elif card_id == "storm":
            # Per-resolution body application and existing-stack channels
            # were interleaved with the evolving orb queue above.
            pass
        elif card_id == "doubletap":
            player_double_tap += resolution_copies * max(
                1, int(getattr(card, "magic_number", 0) or 0)
            )
        elif card_id == "burst":
            burst_gain = max(
                1 + int(getattr(card, "upgrades", 0) or 0),
                int(getattr(card, "magic_number", 0) or 0),
            )
            player_burst += burst_gain * resolution_copies
        if (
            getattr(card, "type", None) == CardType.ATTACK
            and state.player_double_tap > 0
        ):
            player_double_tap = max(0, player_double_tap - 1)
        if (
            getattr(card, "type", None) == CardType.SKILL
            and state.player_burst > 0
        ):
            player_burst = max(0, player_burst - 1)
        if (
            getattr(card, "type", None)
            in {CardType.ATTACK, CardType.SKILL, CardType.POWER}
            and state.player_duplication > 0
        ):
            # DuplicationPower consumes when the original UseCardAction is
            # constructed, before the queued copy reaches its own canUse
            # boundary.  It is therefore spent even when Time Warp, Choker,
            # Normality, or a dead target later rejects that copy.
            player_duplication = max(0, player_duplication - 1)
        if getattr(card, "type", None) == CardType.ATTACK:
            if akabeko_card_bonus > 0:
                player_akabeko_ready = False
        if discard_count > 0 and self._has_relic(game, "Tingsha"):
            alive_indexes = [
                index for index, value in enumerate(hp) if value > 0
            ]
            if len(alive_indexes) == 1:
                for _ in range(discard_count):
                    index = alive_indexes[0]
                    if hp[index] <= 0:
                        break
                    hp_before_tingsha = sum(max(0, value) for value in hp)
                    self._apply_enemy_damage_packet(
                        monsters[index],
                        index,
                        hp,
                        block,
                        damage_value,
                        mode_shift_remaining,
                        neutralized,
                        3,
                        blockable=True,
                    )
                    hp, block, damage_value = (
                        list(values)
                        for values in self._resolve_state_deaths(
                            game,
                            monsters,
                            hp,
                            block,
                            corpse,
                            damage_value,
                            mode_shift_remaining=mode_shift_remaining,
                            neutralized=neutralized,
                        )
                    )
                    dynamic_direct += max(
                        0,
                        hp_before_tingsha
                        - sum(max(0, value) for value in hp),
                    )
            elif alive_indexes:
                # Random targeting cannot establish a concrete kill. Preserve
                # the branch HP and add only expected aggregate HP loss to
                # score/telemetry; the next authoritative frame resolves the
                # actual target.
                possible_losses = []
                for index in alive_indexes:
                    amount = (
                        1
                        if combat_predictor.is_intangible(monsters[index])
                        else 3
                    )
                    unblocked = max(0, amount - block[index])
                    possible_losses.append(min(hp[index], unblocked))
                expected_tingsha = (
                    discard_count
                    * sum(possible_losses)
                    / max(1, len(possible_losses))
                )
                uncertain_orb_hp_loss[0] += expected_tingsha
                dynamic_direct += expected_tingsha
        cards_played = state.cards_played + resolution_copies
        panache_triggers = self._panache_trigger_count(
            game, state.cards_played, cards_played
        )
        if panache_triggers:
            living_targets = sum(1 for value in hp if value > 0)
            # Ten is the lower (unupgraded) payload. Discount it because this
            # heuristic deliberately does not reconstruct Block, Intangible,
            # Flight or the upgraded value, and never mutates concrete HP.
            dynamic_base += panache_triggers * living_targets * 10 * 0.45
        normality_cards_played = (
            state.normality_cards_played + resolution_copies
        )
        normality_still_in_hand = any(
            _token(getattr(game.hand[index], "card_id", ""))
            == "normality"
            for index in remaining_hand_indexes
        )
        forced_end = (
            (
                time_warp_remaining is not None
                and cards_played >= time_warp_remaining
            )
            or (
                normality_still_in_hand
                and normality_cards_played >= 3
            )
        )
        post_draw_energy = max(
            0,
            state.energy - cost + energy_gain
            + hovering_kite_energy + sundial_energy_gain - drawn_voids,
        )
        if draw_energy_unknown:
            # Unknown draw order plus a reachable Void cannot establish that
            # any positive remainder is spendable.  The next authoritative
            # frame will expose the real debt and reopen legal actions.
            post_draw_energy = 0
        exact_first_action_loss = sum(
            max(0, int(before_hp or 0) - int(after_hp or 0))
            for before_hp, after_hp in zip(hp_before_card, hp)
        )
        expected_first_action_loss = exact_first_action_loss + int(
            round(max(0.0, uncertain_orb_hp_loss[0]))
        )
        return _TurnState(
            used=state.used | {group_index},
            energy=post_draw_energy,
            cards_played=cards_played,
            normality_cards_played=normality_cards_played,
            score=state.score + dynamic_base,
            plan=state.plan + (candidate,),
            hp=tuple(hp),
            block=tuple(block),
            poison=tuple(poison),
            artifact=tuple(artifact),
            vulnerable=tuple(vulnerable),
            weak=tuple(weak),
            lock_on=tuple(lock_on),
            corpse_explosion=tuple(corpse),
            damage_value=tuple(damage_value),
            poison_value=tuple(poison_value),
            choke=tuple(choke),
            mode_shift_remaining=tuple(mode_shift_remaining),
            curl_up_block=tuple(curl_up_block),
            malleable_next_block=tuple(malleable_next_block),
            flight_stacks=tuple(flight_stacks),
            orbs=tuple(orbs),
            orb_slots=orb_slots,
            player_artifact=player_artifact,
            player_buffer=player_buffer,
            player_block=player_block,
            player_no_block=player_no_block,
            player_intangible=player_intangible,
            player_intangible_turns=state.player_intangible_turns + intangible_gain,
            player_vulnerable=player_vulnerable,
            player_hp=player_hp_state,
            player_rage=player_rage,
            player_after_image=player_after_image,
            player_accuracy=player_accuracy,
            player_thousand_cuts=player_thousand_cuts,
            player_sadistic_nature=player_sadistic_nature,
            player_feel_no_pain=player_feel_no_pain,
            player_dark_embrace=player_dark_embrace,
            player_corruption=player_corruption,
            player_bullet_time=player_bullet_time,
            player_heatsinks=player_heatsinks,
            player_storm=player_storm,
            player_double_tap=player_double_tap,
            player_burst=player_burst,
            player_duplication=player_duplication,
            attack_resolutions_played=(
                state.attack_resolutions_played
                + (
                    resolution_copies
                    if getattr(card, "type", None) == CardType.ATTACK
                    else 0
                )
            ),
            first_action_resolution_count=(
                state.first_action_resolution_count
                if state.first_action_resolution_count > 0
                else resolution_copies
            ),
            player_pen_nib=player_pen_nib,
            player_akabeko_ready=player_akabeko_ready,
            player_dexterity_bonus=player_dexterity_bonus,
            player_focus_bonus=player_focus_bonus,
            player_electrodynamics=player_electrodynamics,
            player_static_discharge=player_static_discharge,
            player_thorns=player_thorns,
            player_flame_barrier=player_flame_barrier,
            player_rupture=player_rupture,
            player_hex=player_hex,
            rupture_triggers=(
                state.rupture_triggers + rupture_trigger_count
            ),
            rupture_strength_gained=(
                state.rupture_strength_gained + rupture_strength_gain
            ),
            player_strength_bonus=player_strength_bonus,
            unconsumed_temporary_strength=unconsumed_temporary_strength,
            enemy_strength_bonus=tuple(enemy_strength_bonus),
            enemy_strength_reduction=tuple(enemy_strength_reduction),
            remaining_hand_indexes=tuple(remaining_hand_indexes),
            hand_size=next_hand_size,
            draw_pile_size=next_draw_pile_size,
            sundial_draw_pile_size=sundial_draw_pile_size,
            sundial_discard_pile_size=sundial_discard_pile_size,
            sundial_counter=sundial_counter,
            sundial_shuffle_count=(
                state.sundial_shuffle_count + sundial_shuffle_events
            ),
            sundial_energy_gained=(
                state.sundial_energy_gained + sundial_energy_gain
            ),
            drawn_void_count=(
                state.drawn_void_count + drawn_voids
            ),
            draw_energy_loss=(
                state.draw_energy_loss + drawn_voids
            ),
            future_draw_pile_unknown=(
                state.future_draw_pile_unknown
                or hex_dazed_count > 0
            ),
            generated_dazed=(
                state.generated_dazed + hex_dazed_count
            ),
            generated_voids=state.generated_voids + generated_voids,
            discarded_this_turn=(
                state.discarded_this_turn or discard_count > 0
            ),
            hovering_kite_energy_gained=(
                state.hovering_kite_energy_gained
                + hovering_kite_energy
            ),
            next_turn_energy=(
                state.next_turn_energy
                + self._next_turn_energy_gain(card) * resolution_copies
            ),
            no_draw=(
                state.no_draw
                or card_id in {"battletrance", "bullettime"}
            ),
            neutralized=frozenset(neutralized),
            mitigation=state.mitigation + candidate_other,
            end_turn_relief=state.end_turn_relief + max(0, candidate.end_turn_relief),
            self_hp_cost=state.self_hp_cost + dynamic_self_cost,
            voluntary_self_hp_cost=(
                state.voluntary_self_hp_cost + dynamic_nonreactive_cost
            ),
            reactive_hp_cost=(
                state.reactive_hp_cost
                + dynamic_reactive_cost
            ),
            self_forming_clay_hp_loss_events=(
                total_clay_hp_loss_events
            ),
            raw_orichalcum_block=state.raw_orichalcum_block + candidate_block,
            generated_block=state.generated_block + generated_block_gain,
            end_turn_damage_events=(
                state.end_turn_damage_events
                + tuple(candidate.end_turn_damage_events)
            ),
            end_turn_aoe_damage=(
                state.end_turn_aoe_damage
                + max(0, candidate.end_turn_aoe_damage)
            ),
            extra_combust_hp_loss=(
                state.extra_combust_hp_loss
                + max(0, candidate.extra_combust_hp_loss)
            ),
            extra_brutality_amount=(
                state.extra_brutality_amount
                + (1 if card_id == "brutality" else 0)
            ),
            lifecycle_liability=(
                state.lifecycle_liability
                + lifecycle_liability
            ),
            forced_exhaust_lifecycle_cost=(
                state.forced_exhaust_lifecycle_cost
                + forced_exhaust_lifecycle_cost
            ),
            forced_exhaust_lifecycle_cards=(
                state.forced_exhaust_lifecycle_cards
                + tuple(forced_exhaust_lifecycle_cards)
            ),
            random_hand_unknown=(
                state.random_hand_unknown or random_discard_unknown
            ),
            writhing_mass_intent_unknown=frozenset(
                writhing_mass_intent_unknown
            ),
            first_action_enemy_hp_loss=(
                state.first_action_enemy_hp_loss
                if state.first_action_enemy_hp_loss >= 0
                else exact_first_action_loss
            ),
            first_action_expected_enemy_hp_loss=(
                state.first_action_expected_enemy_hp_loss
                if state.first_action_expected_enemy_hp_loss >= 0
                else expected_first_action_loss
            ),
            forced_end=forced_end,
        )

    def _terminal_turn_score(
        self, game, state, monsters, attack_loss, total_loss, risk_budget,
        time_warp_remaining,
    ):
        hp = list(state.hp)
        block = list(state.block)
        branch_poison = list(state.poison)
        damage_value = list(state.damage_value)
        corpse = list(state.corpse_explosion)
        mode_shift_remaining = list(
            state.mode_shift_remaining or tuple(0 for _ in monsters)
        )
        neutralized = set(state.neutralized)
        time_eater_index = next(
            (
                index
                for index, monster in enumerate(monsters)
                if _token(getattr(monster, "monster_id", "")) == "timeeater"
            ),
            None,
        )
        haste_triggered = False
        haste_healed = 0
        # Time Eater's twelfth-card trigger resolves immediately after the
        # card, before end-of-turn orbs/statuses and before the enemy acts.
        if (
            time_eater_index is not None
            and time_warp_remaining is not None
            and state.cards_played >= time_warp_remaining
            and hp[time_eater_index] > 0
        ):
            eater = monsters[time_eater_index]
            haste_triggered = True
            half_hp = max(1, int(getattr(eater, "max_hp", 0) or 0)) // 2
            if hp[time_eater_index] < half_hp:
                haste_healed = half_hp - hp[time_eater_index]
                hp[time_eater_index] = half_hp
            # Haste clears poison/debuff state before the enemy turn.
            branch_poison[time_eater_index] = 0
            corpse[time_eater_index] = 0
        random_target_uncertain = any(
            candidate.target is None
            and _token(getattr(candidate.card, "card_id", ""))
            in _RANDOM_MULTI_TARGET_ATTACKS
            and sum(
                1
                for monster in monsters
                if int(getattr(monster, "current_hp", 0) or 0) > 0
            ) > 1
            for candidate in (state.plan or ())
        )
        combat_ended_before_end_turn = (
            self._all_truly_dead(monsters, hp)
            and not random_target_uncertain
        )
        final_orbs = list(state.orbs)
        if (
            not combat_ended_before_end_turn
            and self._has_relic(game, "Frozen Core")
            and len(final_orbs) < max(1, state.orb_slots)
        ):
            final_orbs.append(
                self._projected_orb(
                    game,
                    "frost",
                    focus_override=(
                        combat_predictor.signed_power_amount(
                            game.player, "Focus"
                        )
                        + state.player_focus_bonus
                    ),
                )
            )

        # Use the simulated final orb state rather than the authoritative
        # frame: Fission removes old orbs and channel cards add new ones.
        # Ordinary Lightning is deterministic only with one living target;
        # Electrodynamics makes every passive hit deterministic against all.
        hp_before_passive_damage = tuple(hp)
        living_indexes = [
            index for index, value in enumerate(hp) if value > 0
        ]
        if (
            not combat_ended_before_end_turn
            and living_indexes
            and (
                len(living_indexes) == 1
                or state.player_electrodynamics
            )
        ):
            lightning_amounts = [
                self._orb_passive(orb)
                for orb in final_orbs
                if self._orb_id(orb) == "lightning"
            ]
            if (
                final_orbs
                and self._has_relic(game, "Cables")
                and self._orb_id(final_orbs[0]) == "lightning"
            ):
                lightning_amounts.append(self._orb_passive(final_orbs[0]))
            for amount in lightning_amounts:
                if not any(value > 0 for value in hp):
                    break
                passive_orb = _ProjectedOrb(
                    "lightning", amount, amount
                )
                (
                    hp,
                    block,
                    damage_value,
                    _,
                    _,
                ) = self._resolve_damage_orb_evokes(
                    game,
                    monsters,
                    hp,
                    block,
                    corpse,
                    damage_value,
                    mode_shift_remaining,
                    neutralized,
                    passive_orb,
                    1,
                    electrodynamics=state.player_electrodynamics,
                    amount_override=amount,
                    lock_on=state.lock_on,
                )

        combat_ended_before_player_events = self._all_truly_dead(
            monsters, hp
        )

        combust_aoe_damage = (
            combat_predictor.power_amount(game.player, "Combust")
            + max(0, state.end_turn_aoe_damage)
            + combat_predictor.stone_calendar_damage(game)
        )
        if not combat_ended_before_player_events and combust_aoe_damage > 0:
            for index, monster in enumerate(monsters):
                if hp[index] <= 0:
                    continue
                self._apply_enemy_damage_packet(
                    monster,
                    index,
                    hp,
                    block,
                    damage_value,
                    mode_shift_remaining,
                    neutralized,
                    combust_aoe_damage,
                    blockable=True,
                )
            hp, block, damage_value = self._resolve_state_deaths(
                game,
                monsters,
                hp,
                block,
                corpse,
                damage_value,
                mode_shift_remaining=mode_shift_remaining,
                neutralized=neutralized,
            )
            hp = list(hp)
            block = list(block)
            damage_value = list(damage_value)
        # Credit only HP actually lost to the ordered blockable END effects.
        # The search subtracts the same value for bare END, so stripping Block
        # receives only the extra passive damage it enables. Direct card
        # damage, Poison utility and later enemy reactions are priced elsewhere.
        passive_hp_damage = sum(
            max(0, before - max(0, after))
            for before, after in zip(hp_before_passive_damage, hp)
        )
        # Poison acts before the enemy's material action and can start a
        # Corpse Explosion cascade. Invincible remains conservatively alive.
        if not combat_ended_before_player_events:
            for index, monster in enumerate(monsters):
                if hp[index] <= 0 or combat_predictor.has_unresolved_damage_cap(monster):
                    continue
                self._apply_enemy_damage_packet(
                    monster,
                    index,
                    hp,
                    block,
                    damage_value,
                    mode_shift_remaining,
                    neutralized,
                    branch_poison[index],
                    blockable=False,
                )
        hp, block, damage_value = self._resolve_state_deaths(
            game,
            monsters,
            hp,
            block,
            corpse,
            damage_value,
            mode_shift_remaining=mode_shift_remaining,
            neutralized=neutralized,
        )
        dead = {
            index
            for index, monster in enumerate(monsters)
            if self._is_true_death_at_hp(
                monster, hp[index], monsters, hp
            )
        }
        reviving = {
            index
            for index, monster in enumerate(monsters)
            if hp[index] <= 0
            and not self._is_true_death_at_hp(
                monster, hp[index], monsters, hp
            )
        }
        transitioned = {
            index
            for index, monster in enumerate(monsters)
            if self._transition_suppresses_action(
                monster,
                hp[index],
                max(
                    damage_value[index],
                    int(getattr(monster, "current_hp", 0) or 0)
                    - hp[index],
                ),
            )
        }
        excluded_indexes = (
            dead | reviving | transitioned | neutralized
        )
        excluded = [monsters[index] for index in excluded_indexes]
        remaining_attackers = [
            monster
            for index, monster in enumerate(monsters)
            if index not in excluded_indexes
        ]
        writhing_mass_replan_indexes = {
            index
            for index in state.writhing_mass_intent_unknown
            if 0 <= index < len(monsters)
        }
        writhing_mass_unknown_indexes = {
            index
            for index in writhing_mass_replan_indexes
            if index not in excluded_indexes
            and hp[index] > 0
        }
        # Compulsive has already invalidated these serialized moves.  Replace
        # each stale intent with a worst-case encounter envelope solely for
        # bounded risk ranking; the next real action is always selected from
        # the refreshed authoritative frame, and no particular reroll is
        # claimed in the trace.
        reactive_intent_envelopes = {
            id(monsters[index]): (
                self._writhing_mass_risk_packet(game, monsters[index]),
            )
            for index in writhing_mass_unknown_indexes
        }
        reactive_intent_envelope_damage = {
            index: self._writhing_mass_risk_packet(
                game, monsters[index]
            ).damage_per_hit
            for index in writhing_mass_replan_indexes
        }
        branch_player_vulnerable = (
            state.player_vulnerable
            or combat_predictor.projected_deaths_apply_player_vulnerable(
                game,
                [monsters[index] for index in dead],
                artifact_layers=state.player_artifact,
            )
        )
        authoritative_player_vulnerable = combat_predictor.has_power(
            getattr(game, "player", None), "Vulnerable"
        )
        # Build one signed, target-local per-hit delta from the branch's final
        # Weak and Strength state. It enters the common attack pipeline before
        # Block/Buffer and therefore owns every downstream trigger correctly.
        forced_attack_bonus = {}
        per_hit_damage_includes_player_vulnerable = set()
        branch_enemy_attack_damage_per_hit = []
        shifting_strength_loss = []
        for index, monster in enumerate(monsters):
            serialized_damage = max(
                0,
                int(
                    getattr(monster, "move_adjusted_damage", 0) or 0
                ),
            )
            strength_gain = (
                int(state.enemy_strength_bonus[index] or 0)
                if index < len(state.enemy_strength_bonus)
                else 0
            )
            if (
                state.forced_end
                and time_eater_index == index
                and index not in excluded_indexes
            ):
                strength_gain += 2
            strength_reduction = (
                int(state.enemy_strength_reduction[index] or 0)
                if index < len(state.enemy_strength_reduction)
                else 0
            )
            # Shifting reacts to actual HP lost, including card packets
            # and the pre-attack orb/Combust/Poison events resolved above.
            # Start at this authoritative frame's HP: earlier Shifting loss
            # is already present in its displayed intent/Strength. Enemy
            # block is excluded, as are later Thorns/Discharge reactions.
            shifting_loss = (
                max(0, int(getattr(monster, "current_hp", 0) or 0) - hp[index])
                if combat_predictor.has_power(monster, "Shifting", "ShiftingPower")
                else 0
            )
            shifting_strength_loss.append(shifting_loss)
            strength_reduction += shifting_loss
            weak_amount = (
                int(state.weak[index] or 0)
                if index < len(state.weak)
                else 0
            )
            if index in writhing_mass_unknown_indexes:
                # Compulsive invalidated the concrete move. Preserve only
                # positive Strength in its conservative risk envelope; never
                # claim Weak/Strength-down mitigation against an unknown roll.
                delta = self._strength_damage_bonus_per_hit(
                    game, monster, strength_gain
                )
            else:
                delta = self._branch_enemy_attack_damage_delta(
                    game,
                    monster,
                    strength_gain=strength_gain,
                    strength_reduction=strength_reduction,
                    weak_amount=weak_amount,
                    player_vulnerable=branch_player_vulnerable,
                )
                if (
                    branch_player_vulnerable
                    and not authoritative_player_vulnerable
                ):
                    per_hit_damage_includes_player_vulnerable.add(
                        id(monster)
                    )
            if index not in excluded_indexes and delta != 0:
                forced_attack_bonus[id(monster)] = delta
            branch_enemy_attack_damage_per_hit.append(
                max(0, serialized_damage + delta)
            )
        all_dead = (
            self._all_truly_dead(monsters, hp)
            and not random_target_uncertain
        )
        frost_passive_block = sum(
            self._orb_passive(orb)
            for orb in final_orbs
            if self._orb_id(orb) == "frost"
        )
        if (
            final_orbs
            and self._has_relic(game, "Cables")
            and self._orb_id(final_orbs[0]) == "frost"
        ):
            frost_passive_block += self._orb_passive(final_orbs[0])
        can_gain_block = combat_predictor.can_gain_block(game)
        future_passive_block = (
            combat_predictor.power_amount(game.player, "Metallicize")
            + combat_predictor.power_amount(
                game.player, "Plated Armor", "PlatedArmor"
            )
            + frost_passive_block
            + max(0, state.generated_block)
            if can_gain_block
            else 0
        )
        orichalcum_active = (
            can_gain_block
            and state.player_block == 0
            and self._has_relic(game, "Orichalcum")
        )
        final_player_block = (
            state.player_block
            + (6 if orichalcum_active else 0)
            + future_passive_block
        )
        # Compare against the authoritative block at the start of the turn,
        # not the branch's post-card block.  The latter would make every
        # branch appear to have zero Calipers progress by definition.
        authoritative_player_block = max(
            0, int(getattr(game.player, "block", 0) or 0)
        )
        calipers_retained_block_before = combat_predictor.calipers_retained_block(
            game, authoritative_player_block
        )
        calipers_retained_block_after = combat_predictor.calipers_retained_block(
            game, final_player_block
        )
        calipers_retained_block_gain = max(
            0,
            int(calipers_retained_block_after)
            - int(calipers_retained_block_before),
        )
        delayed_voluntary_hp_cost = 0
        static_discharge_trace = {
            "static_discharge_layers": int(
                state.player_static_discharge
            ),
            "static_discharge_triggered_hits": 0,
            "static_discharge_channels": 0,
            "static_discharge_overflow_evokes": 0,
            "static_discharge_frost_block": 0,
            "static_discharge_plasma_energy_during_enemy_turn": 0,
            "static_discharge_evoke_damage_value": 0.0,
            "static_discharge_random_target_damage_value": 0.0,
            "static_discharge_random_target_evokes": 0,
            "static_discharge_zero_hp_loss_triggers": 0,
            "static_discharge_stopped_attacker_indexes": [],
            "enemy_reaction_damage_events": 0,
            "enemy_reaction_direct_damage": 0,
            "enemy_reaction_total_damage": 0,
            "enemy_attack_healing_events": 0,
            "enemy_attack_healing_requested": 0,
            "enemy_attack_healing": 0,
            "bronze_scales_reaction_damage": 0,
            "player_thorns_reaction_damage": 0,
            "enemy_reaction_stopped_attacker_indexes": [],
            "painful_stabs_wounds": 0,
            "post_death_reactions_suppressed": 0,
            "player_end_turn_healing_before_attacks": 0,
        }
        unattributed_attack_mitigation = max(
            0,
            int(state.mitigation or 0),
        )
        turn_outcome = None
        delayed_rupture_triggers = 0
        delayed_rupture_strength = 0
        if combat_ended_before_player_events:
            projected_loss = 0
        else:
            turn_outcome_kwargs = {
                "excluded_monsters": excluded,
                "active_monsters_override": remaining_attackers,
                "per_hit_damage_bonus": forced_attack_bonus,
                "per_hit_damage_includes_player_vulnerable": (
                    per_hit_damage_includes_player_vulnerable
                ),
                "block_override": state.player_block,
                "buffer_override": state.player_buffer,
                "passive_block_override": future_passive_block,
                "extra_end_turn_events": state.end_turn_damage_events,
                "hand_override": [
                    game.hand[index]
                    for index in state.remaining_hand_indexes
                ],
                "hand_size_override": state.hand_size,
                "force_intangible": state.player_intangible,
                "force_player_vulnerable": branch_player_vulnerable,
                "combat_ends_before_next_turn": all_dead,
                "player_hp_override": state.player_hp,
                "attack_hp_loss_reduction": (
                    unattributed_attack_mitigation
                ),
                "damage_packet_overrides": reactive_intent_envelopes,
            }
            pre_reaction_hp = tuple(hp)
            pre_reaction_block = tuple(block)
            pre_reaction_corpse = tuple(corpse)
            pre_reaction_damage_value = tuple(damage_value)
            pre_reaction_mode_shift = tuple(mode_shift_remaining)
            pre_reaction_neutralized = frozenset(neutralized)
            pre_reaction_orbs = tuple(final_orbs)
            (
                turn_outcome,
                hp,
                block,
                corpse,
                damage_value,
                mode_shift_remaining,
                neutralized,
                final_orbs,
                static_discharge_trace,
            ) = self._projected_turn_outcome_with_attack_reactions(
                game,
                monsters,
                pre_reaction_hp,
                pre_reaction_block,
                pre_reaction_corpse,
                pre_reaction_damage_value,
                pre_reaction_mode_shift,
                pre_reaction_neutralized,
                pre_reaction_orbs,
                state.orb_slots,
                state.player_focus_bonus,
                state.player_electrodynamics,
                state.player_static_discharge,
                state.player_thorns,
                state.player_flame_barrier,
                state.lock_on,
                turn_outcome_kwargs,
                extra_combust_hp_loss=state.extra_combust_hp_loss,
                extra_brutality_amount=state.extra_brutality_amount,
            )
            hp = list(hp)
            block = list(block)
            corpse = list(corpse)
            damage_value = list(damage_value)
            mode_shift_remaining = list(mode_shift_remaining)
            neutralized = set(neutralized)
            final_orbs = list(final_orbs)
            projected_loss = turn_outcome.total_hp_loss
            if state.extra_combust_hp_loss > 0:
                without_combust = self._projected_turn_outcome_with_attack_reactions(
                    game,
                    monsters,
                    pre_reaction_hp,
                    pre_reaction_block,
                    pre_reaction_corpse,
                    pre_reaction_damage_value,
                    pre_reaction_mode_shift,
                    pre_reaction_neutralized,
                    pre_reaction_orbs,
                    state.orb_slots,
                    state.player_focus_bonus,
                    state.player_electrodynamics,
                    state.player_static_discharge,
                    state.player_thorns,
                    state.player_flame_barrier,
                    state.lock_on,
                    turn_outcome_kwargs,
                    extra_combust_hp_loss=0,
                    extra_brutality_amount=state.extra_brutality_amount,
                )[0]
                # Only the HP loss in Combust's own end-turn phase is an
                # intentional cost.  Its event may consume Buffer and expose
                # a later enemy attack; that downstream attack must remain in
                # the ordinary risk budget rather than being mislabelled as
                # voluntary self-damage.
                delayed_voluntary_hp_cost += max(
                    0,
                    turn_outcome.end_turn_hp_loss
                    - without_combust.end_turn_hp_loss,
                )
            if state.extra_brutality_amount > 0:
                without_brutality = self._projected_turn_outcome_with_attack_reactions(
                    game,
                    monsters,
                    pre_reaction_hp,
                    pre_reaction_block,
                    pre_reaction_corpse,
                    pre_reaction_damage_value,
                    pre_reaction_mode_shift,
                    pre_reaction_neutralized,
                    pre_reaction_orbs,
                    state.orb_slots,
                    state.player_focus_bonus,
                    state.player_electrodynamics,
                    state.player_static_discharge,
                    state.player_thorns,
                    state.player_flame_barrier,
                    state.lock_on,
                    turn_outcome_kwargs,
                    extra_combust_hp_loss=state.extra_combust_hp_loss,
                    extra_brutality_amount=0,
                )[0]
                # Brutality is the final event before the next decision, so
                # compare only that phase.  Existing stacks remain in both
                # branches and the predictor merges the new stack into their
                # single Buffer/Tungsten-aware event.
                delayed_voluntary_hp_cost += max(
                    0,
                    turn_outcome.next_turn_start_hp_loss
                    - without_brutality.next_turn_start_hp_loss,
                )

        if turn_outcome is not None and state.player_rupture > 0:
            self_owned_sources = {
                "burn", "combust", "decay", "regret", "brutality",
            }
            delayed_rupture_triggers = sum(
                1
                for event in (
                    tuple(turn_outcome.end_turn_hp_loss_events)
                    + tuple(turn_outcome.next_turn_start_hp_loss_events)
                )
                if _token(getattr(event, "source", ""))
                in self_owned_sources
            )
            delayed_rupture_strength = (
                delayed_rupture_triggers
                * max(0, int(state.player_rupture or 0))
            )

        # Static Discharge can kill its source, a later attacker through
        # Electrodynamics, or a whole group through Corpse Explosion while
        # the enemy actions are resolving.  Refresh every death-derived
        # terminal field from that post-reaction state instead of retaining
        # the pre-attack classification used to build the attack queue.
        dead = {
            index
            for index, monster in enumerate(monsters)
            if self._is_true_death_at_hp(
                monster, hp[index], monsters, hp
            )
        }
        reviving = {
            index
            for index, monster in enumerate(monsters)
            if hp[index] <= 0
            and not self._is_true_death_at_hp(
                monster, hp[index], monsters, hp
            )
        }
        transitioned = {
            index
            for index, monster in enumerate(monsters)
            if self._transition_suppresses_action(
                monster,
                hp[index],
                max(
                    damage_value[index],
                    int(getattr(monster, "current_hp", 0) or 0)
                    - hp[index],
                ),
            )
        }
        excluded_indexes = dead | reviving | transitioned | neutralized
        all_dead = (
            self._all_truly_dead(monsters, hp)
            and not random_target_uncertain
        )

        regeneration_debt = sum(
            combat_predictor.projected_monster_end_turn_healing(
                monster, hp[index]
            )
            for index, monster in enumerate(monsters)
            if hp[index] > 0
        )
        # Regenerate is a future-state effect, not an immediate damage
        # modifier. Penalize only the healing that will actually occur after
        # this turn's poison/death boundary so kill lines are not discounted.
        static_evoke_value = float(
            static_discharge_trace.get(
                "static_discharge_evoke_damage_value", 0.0
            )
            or 0.0
        )
        static_random_value = float(
            static_discharge_trace.get(
                "static_discharge_random_target_damage_value", 0.0
            )
            or 0.0
        )
        enemy_reaction_value = float(
            static_discharge_trace.get(
                "enemy_reaction_total_damage", 0.0
            )
            or 0.0
        )
        enemy_attack_healing = float(
            static_discharge_trace.get(
                "enemy_attack_healing", 0.0
            )
            or 0.0
        )
        painful_stabs_wounds = max(
            0,
            int(
                static_discharge_trace.get("painful_stabs_wounds", 0)
                or 0
            ),
        )
        dazed_penalty = (
            0.0
            if all_dead
            else max(0, int(state.generated_dazed or 0)) * 1.5
        )
        wound_penalty = 0.0 if all_dead else painful_stabs_wounds * 2.0
        # Bounded heuristic for a dead draw plus future energy loss; this
        # does not assert that Void will be drawn on the very next turn.
        void_penalty = 0.0 if all_dead else state.generated_voids * 2.5
        status_deck_penalty = dazed_penalty + wound_penalty + void_penalty
        (
            clay_hp_loss_events,
            clay_future_block,
            clay_credit,
        ) = self._self_forming_clay_value(
            game, state.self_forming_clay_hp_loss_events
        )
        # Exact single-target/Electrodynamics evocations receive full combat
        # value.  Random multi-target Lightning is useful in expectation but
        # is discounted and, more importantly, never changes concrete HP or
        # claims a kill in the branch state.
        score = (
            state.score
            + passive_hp_damage
            - clay_credit
            + (0.0 if all_dead else clay_credit)
            + max(0.0, static_evoke_value - static_random_value)
            + max(0.0, static_random_value) * 0.5
            + max(0.0, enemy_reaction_value)
            - max(0.0, enemy_attack_healing)
            + delayed_rupture_strength * 2.4
            - wound_penalty
            - void_penalty
            - regeneration_debt * 0.85
        )
        # Calipers is persistent defensive value, not ordinary transient
        # Block.  Give a bounded credit for a positive retained-block delta
        # so a covered attack does not make every additional Defend look
        # worthless, while avoiding an incentive to hoard shields forever.
        if calipers_retained_block_gain > 0 and not all_dead:
            score += min(12, calipers_retained_block_gain) * 0.6
        cards_played_this_turn = (
            max(0, int(self._confirmed_card_resolutions or 0))
            + max(0, int(state.cards_played or 0))
        )
        pocketwatch_preserved = bool(
            not all_dead
            and self._has_relic(game, "Pocketwatch")
            and cards_played_this_turn <= 3
        )
        if pocketwatch_preserved:
            # The next hand is unknown, but drawing three extra cards is a
            # deterministic resource.  A bounded continuation value forces a
            # fourth low-impact card to justify giving up the trigger without
            # overwhelming lethal or survival tiers.
            score += 7.5
        ice_cream_energy_retained = (
            max(0, int(state.energy or 0))
            if not all_dead and self._has_relic(game, "Ice Cream")
            else 0
        )
        if ice_cream_energy_retained > 0:
            # Retained Energy is spendable next turn but its concrete cards
            # are unknown.  Credit less than immediate Energy while ensuring
            # a safe END is not treated as throwing the resource away.
            score += min(5, ice_cream_energy_retained) * 1.6
        attacks_played_this_turn = (
            max(0, int(self._confirmed_attack_resolutions or 0))
            + max(0, int(state.attack_resolutions_played or 0))
        )
        art_of_war_preserved = bool(
            not all_dead
            and self._has_relic(game, "Art of War")
            and attacks_played_this_turn == 0
        )
        if art_of_war_preserved:
            # Price the full next-turn Energy above a negligible current
            # attack so the search does not throw away the trigger for one
            # or two damage.
            score += 5.0
        # Damage absorbed by ordinary enemy Block is normally transient: the
        # Block disappears at the start of that monster's next turn, so it is
        # intentionally absent from direct-damage score.  Barricade and
        # Plated Armor are the narrow exceptions.  Their Block/durability
        # survives between turns and is part of the monster's real remaining
        # durability.  Without valuing this exact terminal delta, the
        # planner can defend forever against Spheric Guardian or end against
        # Shelled Parasite while mathematically useful attacks are scored as
        # zero HP damage.  Compare the authoritative starting Block with the
        # final branch once, rather than pricing individual packets, so
        # Malleable/Curl Up and multi-hit cards cannot double-count progress.
        persistent_enemy_block_progress_by_monster = [
            (
                max(
                    0,
                    max(0, int(getattr(monster, "block", 0) or 0))
                    - max(0, int(block[index] or 0)),
                )
                if hp[index] > 0
                and (
                    combat_predictor.has_power(
                        monster, "Barricade", "BarricadePower"
                    )
                    or combat_predictor.has_power(
                        monster, "Plated Armor", "PlatedArmor",
                        "PlatedArmorPower",
                    )
                )
                else 0
            )
            for index, monster in enumerate(monsters)
        ]
        persistent_enemy_block_progress = sum(
            persistent_enemy_block_progress_by_monster
        )
        score += persistent_enemy_block_progress
        # A safe setup window is itself a consumable turn resource.  Without
        # pricing the alternative, several draw/block cards can accumulate
        # generic card utility while spending the Energy that an affordable
        # Demon Form needed, even when they prevent no HP loss and make no
        # enemy progress.  Charge the *evaluated* lifecycle value of that
        # foregone setup rather than assigning either card a fixed bonus.
        #
        # Keep this deliberately evidence-bounded: the initial turn must be
        # lossless, the setup must still be in the authoritative hand, its
        # lifecycle model must predict at least two triggers, and the branch
        # must neither end combat nor reduce enemy HP/persistent durability.
        # Lines which can still afford the setup are left to normal beam
        # expansion, and uncertain/random hands are not judged here.
        initial_energy = max(
            0, int(getattr(getattr(game, "player", None), "energy", 0) or 0)
        )
        enemy_hp_progress = sum(
            max(
                0,
                int(getattr(monster, "current_hp", 0) or 0)
                - max(0, int(hp[index] or 0)),
            )
            for index, monster in enumerate(monsters)
        )
        foregone_scaling_setup_cost = 0.0
        foregone_scaling_setup_cards = []
        if (
            not all_dead
            and attack_loss == 0
            and total_loss == 0
            and enemy_hp_progress == 0
            and not state.random_hand_unknown
        ):
            for index in state.remaining_hand_indexes:
                if index < 0 or index >= len(getattr(game, "hand", []) or []):
                    continue
                setup_card = game.hand[index]
                if _token(getattr(setup_card, "card_id", "")) not in {
                    "demonform", "echoform",
                }:
                    continue
                setup_cost = combat_predictor.card_energy_cost(
                    game, setup_card
                )
                if setup_cost > initial_energy or setup_cost <= state.energy:
                    continue
                lifecycle = self._lifecycle_evaluation(
                    game, setup_card, total_loss
                )
                if (
                    not isinstance(lifecycle, dict)
                    or int(lifecycle.get("triggers", 0) or 0) < 2
                    or float(lifecycle.get("adjustment", 0.0) or 0.0) <= 0
                ):
                    continue
                adjustment = float(lifecycle["adjustment"])
                # Removing a persistent enemy Block stack is only a partial
                # payoff: Barricade/Plated Armor will rebuild it while the
                # long-fight setup compounds across future turns. Charge a
                # bounded fraction of that temporary progress as additional
                # opportunity cost when the branch still deals no HP damage.
                persistent_block_penalty = 0.0
                if persistent_enemy_block_progress > 0 and adjustment > 0:
                    persistent_block_penalty = min(
                        float(persistent_enemy_block_progress),
                        adjustment,
                    ) * 0.5
                    adjustment += persistent_block_penalty
                if adjustment > foregone_scaling_setup_cost:
                    foregone_scaling_setup_cost = adjustment
                foregone_scaling_setup_cards.append({
                    "card_id": getattr(setup_card, "card_id", ""),
                    "cost": int(setup_cost),
                    "triggers": int(lifecycle["triggers"]),
                    "adjustment": round(adjustment, 3),
                    "persistent_block_penalty": round(
                        persistent_block_penalty, 3
                    ),
                })
        score -= foregone_scaling_setup_cost
        forced_exhaust_lifecycle_cost = (
            0.0 if all_dead else state.forced_exhaust_lifecycle_cost
        )
        score -= forced_exhaust_lifecycle_cost
        # Bronze Orb returns its Stasis card to the hand immediately when the
        # orb dies.  The controller replans from the refreshed authoritative
        # frame, so the beam does not need to speculate about the returned
        # card itself, but it must reserve enough energy to play valuable
        # setup before that card is discarded at end of turn.  Otherwise a
        # harmless, nearly-dead orb becomes an attractive seven-HP target and
        # a boss-defining power such as Demon Form is released only after the
        # branch has already spent all available energy.
        premature_stasis_release_cost = 0.0
        premature_stasis_release_cards = []
        playable_stasis_release_credit = 0.0
        playable_stasis_release_cards = []
        retains_returned_cards = (
            self._has_relic(game, "Runic Pyramid")
            or combat_predictor.power_amount(
                game.player,
                "Retain Cards",
                "RetainCards",
                "Equilibrium",
                "EquilibriumPower",
            ) > 0
        )
        if not all_dead:
            for index, monster in enumerate(monsters):
                if (
                    int(getattr(monster, "current_hp", 0) or 0) <= 0
                    or int(hp[index] or 0) > 0
                ):
                    continue
                stasis_card = next(
                    (
                        getattr(power, "card", None)
                        for power in getattr(monster, "powers", []) or []
                        if _token(
                            getattr(power, "power_id", "")
                            or getattr(power, "power_name", "")
                        ) == "stasis"
                        and getattr(power, "card", None) is not None
                    ),
                    None,
                )
                if (
                    stasis_card is None
                    or getattr(stasis_card, "type", None) != CardType.POWER
                ):
                    continue
                setup_cost = combat_predictor.card_energy_cost(
                    game, stasis_card
                )
                if setup_cost <= 0:
                    continue
                lifecycle = self._lifecycle_evaluation(
                    game, stasis_card, total_loss
                )
                if (
                    not isinstance(lifecycle, dict)
                    or int(lifecycle.get("triggers", 0) or 0) < 2
                    or float(lifecycle.get("adjustment", 0.0) or 0.0) <= 0
                ):
                    continue
                adjustment = min(
                    60.0,
                    float(lifecycle.get("adjustment", 0.0) or 0.0),
                )
                release_details = {
                    "monster_index": int(index),
                    "card_id": getattr(stasis_card, "card_id", ""),
                    "cost": int(setup_cost),
                    "remaining_energy": int(state.energy),
                    "triggers": int(lifecycle["triggers"]),
                    "adjustment": round(adjustment, 3),
                }
                if setup_cost <= state.energy:
                    # This is the value the next authoritative replan can
                    # actually realize.  Crediting only branches which retain
                    # the full cost makes the beam choose "block, break orb,
                    # play setup" instead of spending the reserved energy on
                    # marginal over-block first.
                    playable_stasis_release_credit += adjustment
                    playable_stasis_release_cards.append(release_details)
                    continue
                intent = getattr(monster, "intent", None)
                if (
                    retains_returned_cards
                    or (intent is not None and intent.is_attack())
                ):
                    # Killing an attacking orb is immediate mitigation.  Let
                    # the ordinary survival tiers decide that trade instead
                    # of protecting a setup card at the player's expense.
                    continue
                premature_stasis_release_cost += adjustment
                premature_stasis_release_cards.append(release_details)
        score += (
            playable_stasis_release_credit
            - premature_stasis_release_cost
        )
        split_pending = {
            index
            for index, monster in enumerate(monsters)
            if self._split_pending_at_hp(monster, hp[index])
        }
        # Once Split is queued, two children inherit the parent's final HP.
        # Damage below the threshold therefore removes one additional future
        # HP for every point dealt.  This makes a deeper one-turn burst beat a
        # shallow 70-HP split without inventing a fixed preferred card order.
        inherited_hp_reduction = sum(
            max(
                0,
                min(
                    int(getattr(monsters[index], "current_hp", 0) or 0),
                    self._split_threshold(monsters[index]),
                )
                - hp[index],
            )
            for index in split_pending
        )
        score += inherited_hp_reduction
        future_spawn_hp = sum(2 * max(0, hp[index]) for index in split_pending)
        # Temporary Strength that reached END unused provided no value.  The
        # penalty removes the generic skill/card-play score while leaving
        # Flex-before-attack, Artifact+Flex, and Flex+Limit Break untouched.
        score -= 3.0 * max(0, state.unconsumed_temporary_strength)
        # Double Tap is a one-shot setup whose value is realized by the next
        # Attack.  A terminal branch that leaves the power pending while a
        # directly executable attack can still cross the current enemy Block
        # is not a neutral hold: it consumed a card/energy slot and forfeited
        # the promised doubled hit.  Price only this observable lower-bound
        # case; if attacks are unavailable, blocked, or the encounter carries
        # a known attack-reaction hazard, preserving Double Tap for the next
        # authoritative frame remains valid.
        pending_double_tap = max(0, int(state.player_double_tap or 0))
        double_tap_played_this_branch = any(
            _token(getattr(candidate.card, "card_id", "")) == "doubletap"
            for candidate in state.plan
        )
        if pending_double_tap and double_tap_played_this_branch:
            living_indexes = [
                index for index, value in enumerate(hp) if value > 0
            ]
            reaction_hazard = any(
                combat_predictor.has_power(
                    monsters[index],
                    "Thorns", "ThornsPower", "Sharp Hide", "SharpHidePower",
                )
                or _token(getattr(monsters[index], "monster_id", ""))
                in {"timeeater", "gremlinnob", "corruptheart"}
                for index in living_indexes
            )
            target_block = (
                min(max(0, int(block[index] or 0)) for index in living_indexes)
                if living_indexes else 0
            )
            executable_double_tap_attack = any(
                getattr(game.hand[index], "type", None) == CardType.ATTACK
                and max(
                    0,
                    int(
                        getattr(game.hand[index], "damage", 0)
                        or getattr(game.hand[index], "base_damage", 0)
                        or 0
                    ),
                ) > target_block
                and self._state_card_cost(
                    game.hand[index],
                    state.energy,
                    corruption=state.player_corruption,
                    bullet_time=state.player_bullet_time,
                ) <= state.energy
                for index in state.remaining_hand_indexes
                if 0 <= index < len(getattr(game, "hand", []) or [])
            )
            if executable_double_tap_attack and not reaction_hazard:
                score -= 18.0 * pending_double_tap
        time_warp_carry_debt = 0
        valuable_time_warp_reset = any(
            (
                getattr(game.hand[index], "type", None) == CardType.POWER
                or max(
                    0, int(getattr(game.hand[index], "block", 0) or 0)
                ) > 0
            )
            and self._state_card_cost(
                game.hand[index],
                state.energy,
                corruption=state.player_corruption,
                bullet_time=state.player_bullet_time,
            ) <= state.energy
            for index in state.remaining_hand_indexes
        )
        if time_eater_index is not None and time_eater_index not in dead:
            if haste_triggered:
                # Damage/poison invested before Haste is erased from the
                # future state. Charge that loss once, while retaining any
                # post-reset damage from the current terminal phases.
                score -= min(
                    state.damage_value[time_eater_index], haste_healed
                )
                score -= state.poison_value[time_eater_index]
            if state.forced_end:
                score -= 11.0
            elif time_warp_remaining is not None and state.cards_played > 0:
                remaining_after = time_warp_remaining - state.cards_played
                score -= 5.0 if remaining_after == 1 else 1.0 if remaining_after == 2 else 0.0
            elif (
                time_warp_remaining == 1
                and not haste_triggered
                and (attack_loss > 0 or valuable_time_warp_reset)
            ):
                # Ending while the counter is already eleven merely moves
                # the forced end to the first card of the next turn. Price
                # that one-card-turn debt even on a buff/debuff turn: it is
                # usually the safest window to play a useful setup/block card
                # and reset before the next attack. Do not burn an otherwise
                # low-value Strike solely for the counter. Ordinary card value
                # chooses among real reset candidates; Haste-triggering and
                # lethal resets keep their separate penalties below.
                time_warp_carry_debt = 3
                score -= 12.0

        remaining_persistent_debuff_pressure = sum(
            self._persistent_debuff_pressure(game, monster)
            * min(
                1.0,
                hp[index]
                / max(1, int(getattr(monster, "max_hp", 1) or 1)),
            )
            for index, monster in enumerate(monsters)
            if hp[index] > 0
        )
        remaining_scaling_pressure = sum(
            self._monster_scaling_pressure(monster, game)
            * min(1.0, hp[index] / max(1, int(getattr(monster, "max_hp", 1) or 1)))
            for index, monster in enumerate(monsters)
            if hp[index] > 0
        )
        score -= remaining_scaling_pressure * 0.55
        persistent_enemy_strength_debt = sum(
            max(0, int(state.enemy_strength_bonus[index] or 0))
            for index, monster in enumerate(monsters)
            if hp[index] > 0
            and _token(getattr(monster, "monster_id", ""))
            in {"gremlinnob", "awakenedone"}
        )
        persistent_nob_strength_debt = sum(
            max(0, int(state.enemy_strength_bonus[index] or 0))
            for index, monster in enumerate(monsters)
            if hp[index] > 0
            and _token(getattr(monster, "monster_id", ""))
            == "gremlinnob"
        )
        # The current attack already receives this Strength through
        # forced_attack_bonus.  Charge a separate, bounded next-turn debt so
        # several superficially efficient Defends cannot permanently scale
        # Nob while a viable frontload line is available.
        score -= (
            persistent_enemy_strength_debt * 2.0
            + persistent_nob_strength_debt
        )

        player_hp = max(1, int(getattr(game.player, "current_hp", 1) or 1))
        remaining_player_hp = max(0, int(state.player_hp or 0))
        initial_scaling_pressure = sum(
            self._monster_scaling_pressure(monster, game)
            for monster in monsters
        )
        # Recognizing boss growth is not evidence that paying extra HP now
        # will shorten the fight. Keep these newly modeled clocks in target
        # and terminal value, but require a future survival model before they
        # can widen the existing attrition budget.
        race_budget_pressure = max(0.0, initial_scaling_pressure - sum(
            self._monster_scaling_pressure(monster, game)
            for monster in monsters
            if _token(getattr(monster, "monster_id", ""))
            in {"champ", "deca", "donu", "thecollector"}
        ))
        initial_persistent_debuff_pressure = sum(
            self._persistent_debuff_pressure(game, monster)
            for monster in monsters
        )
        player_persistent_stat_debt = (
            max(
                0,
                -combat_predictor.signed_power_amount(
                    game.player, "Strength"
                ),
            )
            + max(
                0,
                -combat_predictor.signed_power_amount(
                    game.player, "Dexterity"
                ),
            )
        )
        affordable_attack_remains = any(
            getattr(game.hand[index], "type", None) == CardType.ATTACK
            and self._state_card_cost(
                game.hand[index],
                state.energy,
                corruption=state.player_corruption,
                bullet_time=state.player_bullet_time,
            ) <= state.energy
            for index in state.remaining_hand_indexes
            if 0 <= index < len(getattr(game, "hand", []) or [])
        )
        defensive_attack_prefix_progress = (
            max(0, int(total_loss - projected_loss))
            if affordable_attack_remains
            else 0
        )
        scaling_allowance = min(
            max(0, (player_hp - 1) // 4),
            int(race_budget_pressure * 0.6),
            # Extra attrition is justified only by actually racing the
            # scaler.  Setup-only lines used to receive the full allowance,
            # which let Self Repair and Loop consume energy before an X-cost
            # Reinforced Body against the double Orb Walker event.
            max(
                0,
                int(enemy_hp_progress),
                defensive_attack_prefix_progress,
            ),
        )
        if (
            initial_persistent_debuff_pressure > 0
            and player_persistent_stat_debt > 0
        ):
            persistent_reserve = self._reactive_safety_reserve(
                game, player_hp
            )
            persistent_race_allowance = min(
                max(0, player_hp - persistent_reserve - risk_budget),
                int(initial_persistent_debuff_pressure * 1.5),
                max(
                    0,
                    int(enemy_hp_progress),
                    defensive_attack_prefix_progress,
                ),
            )
            scaling_allowance = max(
                scaling_allowance, persistent_race_allowance
            )
        effective_risk_budget = min(player_hp - 1, risk_budget + scaling_allowance)
        actual_loss = max(
            0,
            player_hp - remaining_player_hp + projected_loss
            - (turn_outcome.end_turn_healing if turn_outcome is not None else 0),
        )
        fairy_revive_consumed = bool(
            turn_outcome is not None
            and turn_outcome.fairy_revive_consumed
        )
        fairy_revive_healing = (
            int(turn_outcome.fairy_revive_healing or 0)
            if turn_outcome is not None else 0
        )
        projected_player_hp_after_turn = (
            turn_outcome.final_player_hp
            if turn_outcome is not None else None
        )
        projected_player_hp_delta = (
            turn_outcome.player_hp_delta
            if turn_outcome is not None else None
        )
        boundary_player_hp = (
            int(projected_player_hp_after_turn)
            if projected_player_hp_after_turn is not None
            else max(0, remaining_player_hp - projected_loss)
        )
        terminal_risk_budget = effective_risk_budget
        max_player_hp = max(
            player_hp,
            int(getattr(game.player, "max_hp", player_hp) or player_hp),
        )
        safety_reserve = self._reactive_safety_reserve(game, player_hp)
        reactive_block_alternative = self._has_affordable_reactive_block_line(
            game, state
        )
        terminal_reactive_commit = (
            all_dead
            and state.reactive_hp_cost > 0
            and not reactive_block_alternative
            and boundary_player_hp > 0
            and boundary_player_hp >= safety_reserve
        )
        nonreactive_self_cost = max(0, state.voluntary_self_hp_cost)
        voluntary_hp_cost = (
            nonreactive_self_cost + delayed_voluntary_hp_cost
        )
        safe_self_damage_commit = (
            voluntary_hp_cost > 0
            and boundary_player_hp > 0
            and boundary_player_hp >= safety_reserve
        )
        if terminal_reactive_commit:
            # Once a reactive enemy can be finished while retaining a
            # max-HP-relative safety reserve, waiting only lets Thorns/Sharp
            # Hide grow. Admit the real cost instead of comparing it to an
            # arbitrary fixed cap. The low-HP/lethal guards remain below.
            terminal_risk_budget = max(terminal_risk_budget, actual_loss)
        elif safe_self_damage_commit:
            # Voluntary HP costs are already charged in candidate score. Do
            # not turn low current HP into a flat ban when the complete
            # attack/end-turn/next-turn lifecycle still leaves the dynamic
            # reserve. This only admits the real loss into the normal score
            # comparison: the low-efficiency gate still rejects attacks whose
            # HP damage does not beat their self cost, while lethal or
            # reserve-breaking branches retain the stricter loss tier.
            terminal_risk_budget = min(
                player_hp - 1,
                terminal_risk_budget + voluntary_hp_cost,
            )
        if total_loss >= player_hp:
            mitigation_weight = 5.0
        elif total_loss > effective_risk_budget:
            mitigation_weight = 1.8
        else:
            mitigation_weight = 0.45
        score += (total_loss - projected_loss) * mitigation_weight
        if fairy_revive_consumed:
            # The bottle replaced a lethal boundary with real HP, but the
            # guaranteed future revive is now gone. Price that consumed
            # resource in the same HP units it restored so a legal block or
            # kill line which preserves it is not scored as worse merely
            # because the stable post-revive HP delta is positive.
            score -= fairy_revive_healing
        if terminal_reactive_commit:
            reactive_weight = (
                2.0
                if player_hp <= max(24, int(max_player_hp * 0.5))
                else 1.0
            )
            # Candidate scoring charges optional reactive damage once as HP
            # loss and again as avoidable attrition. On a safe terminal line,
            # offset those policy penalties so the planner cannot wait until
            # the same hit becomes unsafe; the actual-loss tier still protects
            # the safety reserve.
            score += state.reactive_hp_cost * (1.0 + reactive_weight)
        elif reactive_block_alternative:
            # A direct reactive kill is not the same as an unavoidable
            # reactive kill.  When an unused Block card fits before the attack,
            # retain the real HP loss but remove the old terminal incentive
            # and add a bounded dominance penalty so ``Defend -> Attack`` wins
            # whenever it is legal, without banning progress when no such
            # line exists.
            reactive_weight = (
                2.0
                if player_hp <= max(24, int(max_player_hp * 0.5))
                else 1.0
            )
            score -= state.reactive_hp_cost * (2.5 + reactive_weight)
        terminal_player_survives = (
            turn_outcome.player_survives
            if (
                turn_outcome is not None
                and turn_outcome.player_survives is not None
            )
            else projected_loss < remaining_player_hp
        )
        next_turn_finish_window = self._slime_generated_shiv_finish_window(
            game, state, monsters, hp, block,
        )
        projected_hp_after = boundary_player_hp
        next_turn_finish_window_eligible = bool(
            terminal_player_survives
            and projected_hp_after < safety_reserve
            and next_turn_finish_window["selected_indexes"]
        )
        next_turn_finish_window_credit = (
            float(next_turn_finish_window["potential_credit"])
            if next_turn_finish_window_eligible
            else 0.0
        )
        # A long-fight setup may spend a *bounded* amount of healthy HP.
        # Without this allowance a 9-damage Guardian setup is always ranked
        # below zero-loss stalling, regardless of Demon Form's future value.
        setup_risk_allowance = 0
        setup_candidates = [
            candidate for candidate in state.plan
            if candidate.lifecycle_kind in {"demonform", "echoform"}
            and candidate.expected_trigger_count >= 3
            and candidate.lifecycle_adjustment >= max(24.0, actual_loss * 3.0)
        ]
        if (
            setup_candidates and not all_dead and terminal_player_survives
            and state.lifecycle_liability <= 0
            and projected_hp_after >= max(24, int(max_player_hp * 0.40))
            and not any(_token(getattr(monster, "monster_id", "")) in {
                "awakenedone", "corruptheart", "gremlinnob",
            } for monster in monsters)
        ):
            setup_risk_allowance = min(12, int(max_player_hp * 0.15))
            terminal_risk_budget = max(terminal_risk_budget, setup_risk_allowance)
        if not terminal_player_survives:
            score -= 10000 + actual_loss
            tier = 0
        elif actual_loss > terminal_risk_budget:
            score -= (actual_loss - terminal_risk_budget) * 1.5
            tier = 1
        else:
            tier = 2
        if tier == 2 and (
            state.lifecycle_liability >= 4.0
            or persistent_enemy_strength_debt >= 4
            or fairy_revive_consumed
        ):
            # Strategic debt is not fabricated current HP loss, so keep it
            # out of projected_loss.  It does, however, invalidate the claim
            # that this line is categorically safer than every tier-1 setup
            # line when the exact beam ends before the downside begins.  The
            # same applies to permanent Strength just granted to a surviving
            # Nob/Awakened One: a low-loss current turn is not categorically
            # safe when it makes every later attack materially stronger.
            tier = 1
        if all_dead:
            # A terminal kill deserves a score bonus, but it must not bypass
            # the same HP-loss tier used by every non-terminal line.  The old
            # unconditional tier 3 made a seven-damage Spiker kill beat a
            # zero-loss wait even though self_hp_cost was already known.
            score += 20
        return score, {
            "passive_hp_damage": passive_hp_damage,
            "setup_risk_allowance": setup_risk_allowance,
            "projected_loss": int(projected_loss),
            "actual_loss": int(actual_loss),
            "projected_attack_hp_loss": int(
                turn_outcome.attack_hp_loss
                if turn_outcome is not None else 0
            ),
            "projected_end_turn_hp_loss": int(
                turn_outcome.end_turn_hp_loss
                if turn_outcome is not None else 0
            ),
            "projected_next_turn_start_hp_loss": int(
                turn_outcome.next_turn_start_hp_loss
                if turn_outcome is not None else 0
            ),
            "projected_hp_loss": int(projected_loss),
            "fairy_revive_consumed": fairy_revive_consumed,
            "fairy_revive_healing": int(fairy_revive_healing),
            "projected_player_hp_after_turn": (
                int(projected_player_hp_after_turn)
                if projected_player_hp_after_turn is not None else None
            ),
            "projected_player_hp_delta": (
                int(projected_player_hp_delta)
                if projected_player_hp_delta is not None else None
            ),
            "tier": tier,
            "forced_end": state.forced_end,
            "haste_triggered": haste_triggered,
            "haste_healed": int(haste_healed),
            "haste_erased_poison": int(
                state.poison_value[time_eater_index]
                if haste_triggered and time_eater_index is not None
                else 0
            ),
            "time_warp_carry_debt": int(time_warp_carry_debt),
            "enemies_dead": len(dead),
            "true_combat_end": bool(all_dead),
            "reviving_enemy_indexes": sorted(reviving),
            "action_suppressed_enemy_indexes": sorted(excluded_indexes - dead),
            "split_pending_enemy_indexes": sorted(split_pending),
            "future_spawn_hp": int(future_spawn_hp),
            "inherited_hp_reduction": int(inherited_hp_reduction),
            "final_enemy_hp": [max(0, int(value or 0)) for value in hp],
            "final_enemy_block": [max(0, int(value or 0)) for value in block],
            "persistent_enemy_block_progress": int(
                persistent_enemy_block_progress
            ),
            "persistent_enemy_block_progress_by_monster": [
                int(value)
                for value in persistent_enemy_block_progress_by_monster
            ],
            "enemy_hp_progress": int(enemy_hp_progress),
            "foregone_scaling_setup_cost": round(
                foregone_scaling_setup_cost, 3
            ),
            "foregone_scaling_setup_cards": foregone_scaling_setup_cards,
            "forced_exhaust_lifecycle_cost": round(
                forced_exhaust_lifecycle_cost, 3
            ),
            "forced_exhaust_lifecycle_cards": [
                {
                    "card_id": card_id,
                    "card_uuid": card_uuid,
                    "triggers": triggers,
                    "adjustment": adjustment,
                }
                for card_id, card_uuid, triggers, adjustment
                in state.forced_exhaust_lifecycle_cards
            ],
            "premature_stasis_release_cost": round(
                premature_stasis_release_cost, 3
            ),
            "premature_stasis_release_cards": (
                premature_stasis_release_cards
            ),
            "playable_stasis_release_credit": round(
                playable_stasis_release_credit, 3
            ),
            "playable_stasis_release_cards": (
                playable_stasis_release_cards
            ),
            "final_enemy_vulnerable": [
                max(0, int(value or 0)) for value in state.vulnerable
            ],
            "final_enemy_weak": [
                max(0, int(value or 0)) for value in state.weak
            ],
            "final_enemy_strength_reduction": [
                max(0, int(value or 0))
                for value in state.enemy_strength_reduction
            ],
            "branch_enemy_attack_damage_per_hit": list(
                branch_enemy_attack_damage_per_hit
            ),
            "shifting_strength_loss": shifting_strength_loss,
            "unattributed_attack_hp_loss_reduction": int(
                unattributed_attack_mitigation
            ),
            "scaling_pressure": round(remaining_scaling_pressure, 3),
            "scaling_risk_allowance": int(scaling_allowance),
            "race_budget_pressure": round(race_budget_pressure, 3),
            "defensive_attack_prefix_progress": int(
                defensive_attack_prefix_progress
            ),
            "persistent_debuff_pressure": round(
                remaining_persistent_debuff_pressure, 3
            ),
            "regeneration_debt": int(regeneration_debt),
            "effective_risk_budget": int(effective_risk_budget),
            "terminal_risk_budget": int(terminal_risk_budget),
            "reactive_hp_cost": int(state.reactive_hp_cost),
            "voluntary_self_hp_cost": int(
                state.voluntary_self_hp_cost
            ),
            "self_forming_clay_hp_loss_events": int(
                clay_hp_loss_events
            ),
            "self_forming_clay_future_block": int(clay_future_block),
            "self_forming_clay_credit": round(clay_credit, 3),
            "self_forming_clay_applied_credit": round(
                0.0 if all_dead else clay_credit, 3
            ),
            "self_forming_clay_current_turn_mitigation": 0,
            "self_forming_clay_credit_authority": (
                "planner_dynamic_hp_loss_events"
            ),
            "delayed_voluntary_hp_cost": int(
                delayed_voluntary_hp_cost
            ),
            "extra_brutality_amount": int(
                state.extra_brutality_amount
            ),
            "lifecycle_liability": round(
                state.lifecycle_liability, 3
            ),
            "terminal_reactive_commit": terminal_reactive_commit,
            "reactive_block_alternative": reactive_block_alternative,
            "safe_self_damage_commit": safe_self_damage_commit,
            "safety_reserve": int(safety_reserve),
            "next_turn_finish_window": {
                **next_turn_finish_window,
                "eligible": next_turn_finish_window_eligible,
                "credit": round(next_turn_finish_window_credit, 3),
                "future_intent_authority": (
                    "known_move_restriction_upper_bound"
                    if next_turn_finish_window["source"] is not None
                    else None
                ),
            },
            "final_non_orichalcum_block": int(
                state.player_block + future_passive_block
            ),
            "orichalcum_reactivated": orichalcum_active,
            "orichalcum_active": orichalcum_active,
            "final_player_block": int(final_player_block),
            "calipers_retained_block_before": int(
                calipers_retained_block_before
            ),
            "calipers_retained_block_after": int(
                calipers_retained_block_after
            ),
            "calipers_retained_block_gain": int(
                calipers_retained_block_gain
            ),
            "pocketwatch_cards_played": int(cards_played_this_turn),
            "pocketwatch_preserved": pocketwatch_preserved,
            "ice_cream_energy_retained": int(
                ice_cream_energy_retained
            ),
            "art_of_war_attacks_played": int(attacks_played_this_turn),
            "art_of_war_preserved": art_of_war_preserved,
            "stone_calendar_damage": int(
                combat_predictor.stone_calendar_damage(game)
            ),
            "player_buffer": int(state.player_buffer),
            "player_hp_after_cards": int(remaining_player_hp),
            "player_vulnerable": bool(state.player_vulnerable),
            "player_rage": int(state.player_rage),
            "player_after_image": int(state.player_after_image),
            "player_accuracy": int(state.player_accuracy),
            "player_thousand_cuts": int(state.player_thousand_cuts),
            "player_strength_bonus": int(state.player_strength_bonus),
            "player_focus_bonus": int(state.player_focus_bonus),
            "player_electrodynamics": bool(
                state.player_electrodynamics
            ),
            "player_static_discharge": int(
                state.player_static_discharge
            ),
            # Preserve the historical aggregate audit field while exposing
            # the packet boundary used by the exact reaction resolver.
            "player_thorns": int(
                state.player_thorns + state.player_flame_barrier
            ),
            "player_thorns_power": int(state.player_thorns),
            "player_flame_barrier": int(
                state.player_flame_barrier
            ),
            "player_rupture": int(state.player_rupture),
            "rupture_triggers": int(
                state.rupture_triggers + delayed_rupture_triggers
            ),
            "rupture_strength_gained": int(
                state.rupture_strength_gained
                + delayed_rupture_strength
            ),
            "delayed_rupture_triggers": int(
                delayed_rupture_triggers
            ),
            "delayed_rupture_strength": int(
                delayed_rupture_strength
            ),
            "final_enemy_lock_on": [
                max(0, int(value or 0)) for value in state.lock_on
            ],
            "player_feel_no_pain": int(state.player_feel_no_pain),
            "player_dark_embrace": int(state.player_dark_embrace),
            "player_corruption": bool(state.player_corruption),
            "player_heatsinks": int(state.player_heatsinks),
            "player_storm": int(state.player_storm),
            "player_double_tap": int(state.player_double_tap),
            "player_burst": int(state.player_burst),
            "player_duplication": int(state.player_duplication),
            "player_pen_nib": bool(state.player_pen_nib),
            "remaining_hand_size": int(state.hand_size),
            "remaining_draw_pile_size": int(state.draw_pile_size),
            "sundial_draw_pile_size": int(
                state.sundial_draw_pile_size
            ),
            "sundial_discard_pile_size": int(
                state.sundial_discard_pile_size
            ),
            "sundial_counter": int(state.sundial_counter),
            "sundial_shuffle_count": int(
                state.sundial_shuffle_count
            ),
            "sundial_energy_gained": int(
                state.sundial_energy_gained
            ),
            "drawn_void_count": int(state.drawn_void_count),
            "draw_energy_loss": int(state.draw_energy_loss),
            "future_draw_pile_unknown": bool(
                state.future_draw_pile_unknown
            ),
            "generated_dazed": int(state.generated_dazed),
            "generated_voids": int(state.generated_voids),
            "generated_void_penalty": round(void_penalty, 3),
            "generated_wounds": int(painful_stabs_wounds),
            "status_deck_penalty": round(status_deck_penalty, 3),
            "discarded_this_turn": bool(state.discarded_this_turn),
            "hovering_kite_energy_gained": int(
                state.hovering_kite_energy_gained
            ),
            "next_turn_energy": int(state.next_turn_energy),
            "remaining_energy": int(state.energy),
            "random_hand_unknown": bool(state.random_hand_unknown),
            "writhing_mass_replan_required": bool(
                writhing_mass_replan_indexes
            ),
            "writhing_mass_first_hit_change_risk": bool(
                writhing_mass_replan_indexes
            ),
            "writhing_mass_old_intent_ignored": bool(
                writhing_mass_replan_indexes
            ),
            "writhing_mass_intent_unknown_indexes": sorted(
                writhing_mass_replan_indexes
            ),
            "writhing_mass_risk_envelopes": [
                {
                    "monster_index": int(index),
                    "max_raw_attack": int(
                        reactive_intent_envelope_damage[index]
                    ),
                    "policy": "worst_case_not_predicted_intent",
                }
                for index in sorted(writhing_mass_replan_indexes)
            ],
            "final_orb_ids": [
                self._orb_id(orb) for orb in final_orbs
            ],
            "normality_cards_played": int(
                state.normality_cards_played
            ),
            "card_resolutions_played": int(state.cards_played),
            "attack_resolutions_played": int(
                state.attack_resolutions_played
            ),
            "first_action_resolution_count": int(
                state.first_action_resolution_count
            ),
            "first_action_enemy_hp_loss": (
                None
                if state.first_action_enemy_hp_loss < 0
                else int(state.first_action_enemy_hp_loss)
            ),
            "first_action_expected_enemy_hp_loss": (
                None
                if state.first_action_expected_enemy_hp_loss < 0
                else int(state.first_action_expected_enemy_hp_loss)
            ),
            "first_action_enemy_hp_loss_is_expected": bool(
                state.first_action_expected_enemy_hp_loss
                > state.first_action_enemy_hp_loss
                >= 0
            ),
            "enemy_strength_bonus": [
                int(amount or 0)
                for amount in state.enemy_strength_bonus
            ],
            "unconsumed_temporary_strength": int(
                state.unconsumed_temporary_strength
            ),
            **static_discharge_trace,
        }

    def _best_plan(
        self,
        game,
        groups,
        attack_loss,
        total_loss,
        risk_budget,
        active_monsters_override=None,
    ):
        """Beam-search ordered whole-turn states, including END at each node."""

        monsters = list(combat_predictor.living_monsters(game))
        target_indexes = {id(monster): index for index, monster in enumerate(monsters)}
        search_active = (
            combat_predictor.active_monsters(game)
            if active_monsters_override is None
            else list(active_monsters_override)
        )
        active_ids = {id(monster) for monster in search_active}
        active_indexes = {
            index for index, monster in enumerate(monsters) if id(monster) in active_ids
        }
        initial_vulnerable = tuple(
            combat_predictor.power_amount(monster, "Vulnerable") for monster in monsters
        )
        initial_weak = tuple(
            combat_predictor.power_amount(monster, "Weak", "Weakened")
            for monster in monsters
        )
        serialized_orbs = list(
            getattr(getattr(game, "player", None), "orbs", []) or []
        )
        occupied_orbs = tuple(self._occupied_orbs(game))
        authoritative_orb_slots = getattr(
            getattr(game, "player", None), "max_orbs", None
        )
        orb_slots = (
            max(
                len(occupied_orbs),
                max(0, int(authoritative_orb_slots or 0)),
            )
            if authoritative_orb_slots is not None
            else max(3, len(serialized_orbs), len(occupied_orbs))
        )
        dark_embrace_layers = combat_predictor.signed_power_amount(
            game.player, "Dark Embrace", "DarkEmbracePower"
        )
        if dark_embrace_layers <= 0 and combat_predictor.has_power(
            game.player, "Dark Embrace", "DarkEmbracePower"
        ):
            dark_embrace_layers = 1
        initial_thorns = max(
            0,
            combat_predictor.power_amount(
                game.player, "Thorns", "ThornsPower"
            ),
        )
        if initial_thorns <= 0:
            # Compatibility fallback for frames which expose Bronze Scales
            # only as a relic. Caltrops played in the branch adds to this same
            # stack instead of creating a duplicate reaction packet.
            initial_thorns = combat_predictor.bronze_scales_damage(game)
        initial = _TurnState(
            used=frozenset(),
            energy=max(0, int(getattr(game.player, "energy", 0) or 0)),
            # Serialized Time Warp/Slow/Ink counters already include every
            # confirmed card. Only new speculative plays belong here.
            cards_played=0,
            normality_cards_played=max(
                0, int(self._confirmed_card_resolutions or 0)
            ),
            score=0.0,
            plan=(),
            hp=tuple(max(0, int(getattr(monster, "current_hp", 0) or 0)) for monster in monsters),
            block=tuple(max(0, int(getattr(monster, "block", 0) or 0)) for monster in monsters),
            poison=tuple(combat_predictor.power_amount(monster, "Poison") for monster in monsters),
            artifact=tuple(combat_predictor.power_amount(monster, "Artifact") for monster in monsters),
            vulnerable=initial_vulnerable,
            weak=initial_weak,
            lock_on=tuple(
                combat_predictor.lock_on_amount(monster)
                for monster in monsters
            ),
            corpse_explosion=tuple(combat_predictor.power_amount(monster, "Corpse Explosion", "CorpseExplosionPower") for monster in monsters),
            damage_value=tuple(0.0 for _ in monsters),
            poison_value=tuple(0.0 for _ in monsters),
            choke=tuple(
                combat_predictor.power_amount(
                    monster, "Choke", "ChokePower", "Choked",
                    "ChokedPower",
                )
                for monster in monsters
            ),
            mode_shift_remaining=tuple(
                combat_predictor.power_amount(
                    monster,
                    "Mode Shift",
                    "ModeShift",
                    "ModeShiftPower",
                )
                for monster in monsters
            ),
            curl_up_block=tuple(
                combat_predictor.power_amount(
                    monster, "Curl Up", "CurlUpPower"
                )
                for monster in monsters
            ),
            malleable_next_block=tuple(
                combat_predictor.power_amount(
                    monster, "Malleable", "MalleablePower"
                )
                for monster in monsters
            ),
            flight_stacks=tuple(
                combat_predictor.power_amount(
                    monster, "Flight", "FlightPower"
                )
                for monster in monsters
            ),
            orbs=occupied_orbs,
            orb_slots=orb_slots,
            player_artifact=combat_predictor.power_amount(game.player, "Artifact"),
            player_buffer=combat_predictor.power_amount(game.player, "Buffer"),
            player_block=max(0, int(getattr(game.player, "block", 0) or 0)),
            player_no_block=not combat_predictor.can_gain_block(game),
            player_intangible=combat_predictor.has_power(
                game.player, "Intangible", "IntangiblePlayer"
            ),
            player_intangible_turns=combat_predictor.power_amount(
                game.player, "Intangible", "IntangiblePlayer"
            ),
            player_vulnerable=combat_predictor.has_power(
                game.player, "Vulnerable"
            ),
            player_hp=max(
                1, int(getattr(game.player, "current_hp", 1) or 1)
            ),
            player_rage=combat_predictor.power_amount(
                game.player, "Rage", "RagePower"
            ),
            player_after_image=combat_predictor.power_amount(
                game.player, "After Image", "AfterImagePower"
            ),
            player_accuracy=combat_predictor.power_amount(
                game.player, "Accuracy", "AccuracyPower"
            ),
            player_thousand_cuts=combat_predictor.power_amount(
                game.player, "Thousand Cuts", "ThousandCutsPower"
            ),
            player_sadistic_nature=(
                max(
                    5,
                    combat_predictor.power_amount(
                        game.player,
                        "Sadistic",
                        "Sadistic Nature",
                        "SadisticNaturePower",
                    ),
                )
                if combat_predictor.has_power(
                    game.player,
                    "Sadistic",
                    "Sadistic Nature",
                    "SadisticNaturePower",
                )
                else 0
            ),
            player_feel_no_pain=combat_predictor.power_amount(
                game.player, "Feel No Pain", "FeelNoPainPower"
            ),
            player_dark_embrace=dark_embrace_layers,
            player_corruption=combat_predictor.has_power(
                game.player, "Corruption"
            ),
            player_heatsinks=self._heatsinks_layers(game),
            player_storm=(
                max(
                    1,
                    combat_predictor.power_amount(
                        game.player, "Storm", "StormPower"
                    ),
                )
                if combat_predictor.has_power(
                    game.player, "Storm", "StormPower"
                )
                else 0
            ),
            player_double_tap=(
                max(
                    0,
                    combat_predictor.power_amount(
                        game.player, "Double Tap", "DoubleTapPower"
                    ),
                )
            ),
            player_burst=max(
                0,
                combat_predictor.power_amount(
                    game.player, "Burst", "BurstPower"
                ),
            ),
            player_duplication=max(
                0,
                combat_predictor.power_amount(
                    game.player, "Duplication", "DuplicationPower"
                ),
            ),
            attack_resolutions_played=0,
            first_action_resolution_count=0,
            player_pen_nib=combat_predictor.pen_nib_ready(game),
            player_akabeko_ready=combat_predictor.akabeko_ready(game),
            player_dexterity_bonus=0,
            player_focus_bonus=0,
            player_electrodynamics=combat_predictor.has_power(
                game.player,
                "Electrodynamics",
                "ElectrodynamicsPower",
                "Electro",
            ),
            player_static_discharge=self._static_discharge_layers(game),
            player_thorns=initial_thorns,
            player_flame_barrier=max(
                0,
                combat_predictor.power_amount(
                    game.player,
                    "Flame Barrier",
                    "FlameBarrierPower",
                ),
            ),
            player_rupture=combat_predictor.power_amount(
                game.player, "Rupture", "RupturePower"
            ),
            player_hex=(
                max(
                    1,
                    combat_predictor.power_amount(
                        game.player, "Hex", "HexPower"
                    ),
                )
                if combat_predictor.has_power(
                    game.player, "Hex", "HexPower"
                )
                else 0
            ),
            rupture_triggers=0,
            rupture_strength_gained=0,
            player_strength_bonus=0,
            unconsumed_temporary_strength=0,
            enemy_strength_bonus=tuple(0 for _ in monsters),
            enemy_strength_reduction=tuple(0 for _ in monsters),
            remaining_hand_indexes=tuple(
                range(len(getattr(game, "hand", []) or []))
            ),
            hand_size=len(getattr(game, "hand", []) or []),
            draw_pile_size=len(getattr(game, "draw_pile", []) or []),
            sundial_draw_pile_size=len(
                getattr(game, "draw_pile", []) or []
            ),
            sundial_discard_pile_size=len(
                getattr(game, "discard_pile", []) or []
            ),
            sundial_counter=(
                max(
                    0,
                    combat_predictor.relic_counter(
                        game, "Sundial", default=0
                    ),
                ) % 3
            ),
            sundial_shuffle_count=0,
            sundial_energy_gained=0,
            drawn_void_count=0,
            draw_energy_loss=0,
            future_draw_pile_unknown=(
                getattr(game, "draw_pile_order_known", True) is not True
            ),
            generated_dazed=0,
            generated_voids=0,
            discarded_this_turn=(
                int(
                    getattr(game, "cards_discarded_this_turn", 0) or 0
                ) > 0
            ),
            hovering_kite_energy_gained=0,
            next_turn_energy=0,
            player_bullet_time=False,
            no_draw=combat_predictor.has_power(
                game.player, "No Draw", "NoDrawPower"
            ),
            neutralized=frozenset(),
            random_hand_unknown=False,
            writhing_mass_intent_unknown=frozenset(),
        )
        time_warp_remaining = self._time_warp_remaining(game)
        choker_remaining = self._velvet_choker_remaining(
            game,
            self._confirmed_card_resolutions + initial.cards_played,
            branch_cards_played=initial.cards_played,
        )
        player_hp = max(
            1, int(getattr(game.player, "current_hp", 1) or 1)
        )
        safety_reserve = self._reactive_safety_reserve(game, player_hp)
        dangerous = (
            getattr(game, "room_type", "") in {"MonsterRoomElite", "MonsterRoomBoss"}
            or total_loss >= player_hp
        )
        survival_priority = (
            dangerous
            or player_hp - max(0, int(total_loss or 0)) < safety_reserve
        )

        def terminal_rank(result):
            """Keep unsafe-but-live plans from trading away extra HP for score.

            Tier 2 already fits the run's explicit risk budget, so ordinary
            damage/scaling value remains authoritative there.  In a dangerous
            room, or when the baseline end turn breaks the dynamic safety
            reserve, every tier-1 line exceeds that budget.  Prefer the line
            with less actual loss (and then more HP before end-of-turn damage)
            before considering its ordinary score.  This also keeps a
            draw-triggered re-plan from replacing an already-known safer line
            merely because the newly drawn card has attractive damage value.
            """

            score, details = result
            tier = int(details.get("tier", 0) or 0)
            if survival_priority and tier == 1:
                lifecycle_loss = int(
                    float(details.get("lifecycle_liability", 0.0) or 0.0)
                    * 0.5
                    + 0.999
                )
                time_warp_rank_debt = int(
                    details.get("time_warp_carry_debt", 0) or 0
                )
                reactive_strength_rank_debt = sum(
                    max(0, int(value or 0))
                    * (
                        3
                        if index < len(monsters)
                        and _token(
                            getattr(monsters[index], "monster_id", "")
                        ) == "gremlinnob"
                        else 2
                    )
                    for index, value in enumerate(
                        details.get("enemy_strength_bonus", [])
                    )
                )
                if (
                    details.get("forced_end")
                    and details.get("haste_triggered")
                    and (
                        int(details.get("haste_healed", 0) or 0) > 0
                        or int(details.get("haste_erased_poison", 0) or 0) > 0
                    )
                ):
                    # Resetting by crossing Haste heals Time Eater and clears
                    # invested debuffs. Do not let the generic reset preference
                    # force that boss transition.
                    time_warp_rank_debt += 4
                return (
                    tier,
                    -(
                        int(details.get("actual_loss", 0) or 0)
                        + lifecycle_loss
                        + time_warp_rank_debt
                        + reactive_strength_rank_debt
                    ),
                    int(details.get("player_hp_after_cards", 0) or 0),
                    score,
                )
            return (tier, 0, 0, score)

        def plan_card_signature(state):
            return tuple(sorted(
                (
                    str(getattr(candidate.card, "uuid", None) or ""),
                    _token(getattr(candidate.card, "card_id", "")),
                )
                for candidate in state.plan
            ))

        def target_allocation_equivalent(left_item, right_item):
            """Whether two terminals differ only in pure damage placement."""

            (left_result, left_state) = left_item
            (right_result, right_state) = right_item
            left_details = left_result[1]
            right_details = right_result[1]
            if plan_card_signature(left_state) != plan_card_signature(
                right_state
            ):
                return False

            # Every branch field except damage distribution and the score
            # must be identical.  This excludes target-local Weak, Poison,
            # Vulnerable, Artifact, phase counters, orb state, hand changes,
            # and player-resource differences from the tie-break.
            ignored_state_fields = {
                "score", "plan", "hp", "block", "damage_value",
                # Beam deduplication may preserve a different safe order for
                # the same cards/terminal state.  These two audit-only fields
                # describe that first card's damage; they do not change the
                # authoritative end-of-turn outcome compared below.
                "first_action_enemy_hp_loss",
                "first_action_expected_enemy_hp_loss",
            }
            left_state_values = {
                key: value
                for key, value in vars(left_state).items()
                if key not in ignored_state_fields
            }
            right_state_values = {
                key: value
                for key, value in vars(right_state).items()
                if key not in ignored_state_fields
            }
            if left_state_values != right_state_values:
                return False

            equal_detail_fields = (
                "tier", "actual_loss", "projected_loss",
                "player_hp_after_cards", "final_player_block",
                "remaining_energy", "enemies_dead", "true_combat_end",
                "fairy_revive_consumed", "forced_end", "haste_triggered",
                "future_spawn_hp", "inherited_hp_reduction",
                "enemy_reaction_total_damage", "remaining_hand_size",
                "remaining_draw_pile_size", "next_turn_energy",
            )
            if any(
                left_details.get(field) != right_details.get(field)
                for field in equal_detail_fields
            ):
                return False
            equal_index_fields = (
                "reviving_enemy_indexes",
                "action_suppressed_enemy_indexes",
                "split_pending_enemy_indexes",
            )
            if any(
                tuple(left_details.get(field, []) or [])
                != tuple(right_details.get(field, []) or [])
                for field in equal_index_fields
            ):
                return False

            left_hp = tuple(left_details.get("final_enemy_hp", []) or [])
            right_hp = tuple(right_details.get("final_enemy_hp", []) or [])
            left_block = tuple(
                left_details.get("final_enemy_block", []) or []
            )
            right_block = tuple(
                right_details.get("final_enemy_block", []) or []
            )
            if tuple(value <= 0 for value in left_hp) != tuple(
                value <= 0 for value in right_hp
            ):
                return False
            return sum(left_hp) + sum(left_block) == (
                sum(right_hp) + sum(right_block)
            )

        def self_damage_summary(item):
            (item_score, item_details), item_state = item
            return {
                "score": round(float(item_score), 3),
                "tier": int(item_details.get("tier", 0) or 0),
                "actual_loss": int(
                    item_details.get("actual_loss", 0) or 0
                ),
                "projected_loss": int(
                    item_details.get("projected_loss", 0) or 0
                ),
                "voluntary_self_hp_cost": int(
                    item_details.get("voluntary_self_hp_cost", 0) or 0
                ),
                "true_combat_end": bool(
                    item_details.get("true_combat_end")
                ),
                "enemy_effective_hp": int(
                    sum(item_details.get("final_enemy_hp", []) or [])
                    + sum(item_details.get("final_enemy_block", []) or [])
                ),
                "plan": [
                    _token(getattr(candidate.card, "card_id", ""))
                    for candidate in item_state.plan
                ],
            }

        def persistent_plan_ids(state):
            persistent_ids = {
                "alchemize", "apotheosis", "feed", "geneticalgorithm",
                "handofgreed", "jax", "lessonlearned", "ritualdagger",
                "selfrepair",
            }
            return {
                _token(getattr(candidate.card, "card_id", ""))
                for candidate in state.plan
                if (
                    getattr(candidate.card, "type", None) == CardType.POWER
                    or _token(getattr(candidate.card, "card_id", ""))
                    in persistent_ids
                    or float(
                        getattr(candidate, "future_turn_energy_value", 0.0)
                        or 0.0
                    ) > 0
                    or float(
                        getattr(candidate, "future_turn_weak_value", 0.0)
                        or 0.0
                    ) > 0
                    or float(
                        getattr(candidate, "future_turn_block_value", 0.0)
                        or 0.0
                    ) > 0
                    or float(
                        getattr(candidate, "lifecycle_adjustment", 0.0)
                        or 0.0
                    ) > 0
                )
            }

        def self_damage_material_advantage(selected_item, alternative_item):
            (_, selected_details), selected_state = selected_item
            (_, alternative_details), alternative_state = alternative_item
            selected_end = bool(selected_details.get("true_combat_end"))
            alternative_end = bool(alternative_details.get("true_combat_end"))
            if selected_end and not alternative_end:
                return True, "unique_combat_end"
            if selected_end and alternative_end:
                run_reward_ids = {
                    "alchemize", "feed", "geneticalgorithm",
                    "handofgreed", "lessonlearned", "ritualdagger",
                    "selfrepair",
                }
                selected_rewards = {
                    _token(getattr(candidate.card, "card_id", ""))
                    for candidate in selected_state.plan
                    if _token(getattr(candidate.card, "card_id", ""))
                    in run_reward_ids
                }
                alternative_rewards = {
                    _token(getattr(candidate.card, "card_id", ""))
                    for candidate in alternative_state.plan
                    if _token(getattr(candidate.card, "card_id", ""))
                    in run_reward_ids
                }
                if selected_rewards - alternative_rewards:
                    return True, "run_persistent_terminal_reward"
                return False, "equivalent_combat_end"

            selected_setups = persistent_plan_ids(selected_state)
            alternative_setups = persistent_plan_ids(alternative_state)
            if selected_setups - alternative_setups:
                return True, "persistent_setup_or_reward"

            scalar_fields = (
                "player_strength_bonus", "player_focus_bonus",
                "player_rage", "player_after_image", "player_accuracy",
                "player_thousand_cuts", "player_feel_no_pain",
                "player_dark_embrace", "player_thorns",
                "player_rupture", "next_turn_energy",
                "calipers_retained_block_after",
            )
            if any(
                float(selected_details.get(field, 0) or 0)
                > float(alternative_details.get(field, 0) or 0)
                for field in scalar_fields
            ):
                return True, "persistent_state_gain"

            selected_enemy_benefits = (
                selected_state.poison,
                selected_state.vulnerable,
                selected_state.weak,
                selected_state.lock_on,
                selected_state.corpse_explosion,
                selected_state.choke,
                selected_state.enemy_strength_reduction,
            )
            alternative_enemy_benefits = (
                alternative_state.poison,
                alternative_state.vulnerable,
                alternative_state.weak,
                alternative_state.lock_on,
                alternative_state.corpse_explosion,
                alternative_state.choke,
                alternative_state.enemy_strength_reduction,
            )
            if any(
                any(
                    int(selected or 0) > int(alternative or 0)
                    for selected, alternative in zip(
                        selected_values, alternative_values
                    )
                )
                for selected_values, alternative_values in zip(
                    selected_enemy_benefits, alternative_enemy_benefits
                )
            ):
                return True, "persistent_enemy_debuff"
            if selected_state.orbs != alternative_state.orbs:
                return True, "orb_state_change"
            if int(selected_details.get("projected_loss", 0) or 0) < int(
                alternative_details.get("projected_loss", 0) or 0
            ):
                return True, "current_turn_mitigation"

            selected_effective_hp = (
                sum(selected_details.get("final_enemy_hp", []) or [])
                + sum(selected_details.get("final_enemy_block", []) or [])
            )
            alternative_effective_hp = (
                sum(alternative_details.get("final_enemy_hp", []) or [])
                + sum(alternative_details.get("final_enemy_block", []) or [])
            )
            progress_gain = max(
                0, int(alternative_effective_hp - selected_effective_hp)
            )
            extra_self_cost = max(
                1,
                int(selected_details.get("voluntary_self_hp_cost", 0) or 0)
                - int(
                    alternative_details.get("voluntary_self_hp_cost", 0)
                    or 0
                ),
            )
            # Optional self damage must buy enough progress to justify the
            # *avoidable* HP loss, not merely clear a small fixed damage
            # threshold.  The old fixed threshold admitted lines such as
            # ``Defend -> Defend -> Hemokinesis`` against an attack: the
            # attack was fully blockable by the remaining Defend, but the
            # extra 22 damage was treated as material despite costing six
            # otherwise avoidable HP (four from the hit plus two intrinsic).
            # Keep the old floor for covered/near-zero-loss lines while
            # requiring a 4:1 progress-to-avoidable-loss conversion when a
            # safer lower-cost line materially reduces current HP loss.
            avoidable_loss_delta = max(
                0,
                int(selected_details.get("actual_loss", 0) or 0)
                - int(alternative_details.get("actual_loss", 0) or 0),
            )
            progress_threshold = max(
                6,
                extra_self_cost * 2,
                avoidable_loss_delta * 4,
            )
            if progress_gain > progress_threshold:
                return True, "efficient_damage_conversion"
            return False, "no_material_advantage"

        def lower_self_damage_plan_dominates(selected_item, alternative_item):
            (_, selected_details), selected_state = selected_item
            (_, alternative_details), alternative_state = alternative_item
            selected_cost = int(
                selected_details.get("voluntary_self_hp_cost", 0) or 0
            )
            alternative_cost = int(
                alternative_details.get("voluntary_self_hp_cost", 0) or 0
            )
            if alternative_cost >= selected_cost:
                return False, "not_lower_self_cost"
            if int(alternative_details.get("tier", 0) or 0) < int(
                selected_details.get("tier", 0) or 0
            ):
                return False, "lower_survival_tier"
            if int(alternative_details.get("actual_loss", 0) or 0) >= int(
                selected_details.get("actual_loss", 0) or 0
            ):
                return False, "no_actual_loss_reduction"

            material, material_reason = self_damage_material_advantage(
                selected_item, alternative_item
            )
            if material:
                return False, material_reason
            if (
                bool(selected_details.get("true_combat_end"))
                and bool(alternative_details.get("true_combat_end"))
            ):
                return True, "same_combat_end_less_hp_loss"

            selected_enemy = (
                tuple(selected_details.get("final_enemy_hp", []) or []),
                tuple(selected_details.get("final_enemy_block", []) or []),
            )
            alternative_enemy = (
                tuple(alternative_details.get("final_enemy_hp", []) or []),
                tuple(alternative_details.get("final_enemy_block", []) or []),
            )
            if selected_enemy == alternative_enemy:
                return True, "same_enemy_state_less_hp_loss"
            return True, "low_efficiency_optional_self_damage"

        def beam_rank(result):
            # Frontier states are prefixes rather than executable plans.  A
            # temporarily unsafe attack/setup prefix may still complete a
            # kill or defensive combo on a later expansion.
            score, details = result
            return (int(details.get("tier", 0) or 0), score)

        def deferred_setup_prefix(state):
            """Keep setup-first prefixes alive until their consumer is seen.

            A setup card can have zero immediate score even though it changes
            every later transition (Rage/After Image/Accuracy/A Thousand
            Cuts are the common examples).  The ordinary beam ranks only the
            prefix and could therefore discard Rage before it ever sees the
            following attacks.  Preserve a small, bounded set of these
            prefixes; terminal selection remains unchanged and still prices
            the exact final line.
            """

            if not state.plan:
                return False
            first = state.plan[0]
            first_id = _token(getattr(first.card, "card_id", ""))
            setup_consumers = {
                "rage": lambda card: (
                    getattr(card, "type", None) == CardType.ATTACK
                ),
                "accuracy": lambda card: (
                    _token(getattr(card, "card_id", "")) == "shiv"
                ),
                "athousandcuts": lambda card: True,
                "afterimage": lambda card: True,
                "inflame": lambda card: (
                    getattr(card, "type", None) == CardType.ATTACK
                ),
                "spotweakness": lambda card: (
                    getattr(card, "type", None) == CardType.ATTACK
                ),
                "flex": lambda card: (
                    getattr(card, "type", None) == CardType.ATTACK
                    or _token(getattr(card, "card_id", ""))
                    == "limitbreak"
                ),
                "feelnopain": lambda card: bool(
                    getattr(card, "exhausts", False)
                ),
                "darkembrace": lambda card: bool(
                    getattr(card, "exhausts", False)
                ),
                "storm": lambda card: (
                    getattr(card, "type", None) == CardType.POWER
                ),
            }
            consumer = setup_consumers.get(first_id)
            if consumer is None:
                return False
            if any(consumer(candidate.card) for candidate in state.plan[1:]):
                return False
            return any(
                consumer(game.hand[index])
                for index in state.remaining_hand_indexes
                if 0 <= index < len(getattr(game, "hand", []) or [])
            )

        beam_width = 160 if dangerous else 72
        frontier = [initial]
        terminals = [initial]
        expanded = 0
        max_depth = min(len(groups), time_warp_remaining or len(groups))
        if choker_remaining is not None:
            max_depth = min(max_depth, choker_remaining)

        for _ in range(max_depth):
            proposals = []
            for state in frontier:
                if state.forced_end or state.random_hand_unknown:
                    continue
                for group_index, candidates in enumerate(groups):
                    if group_index in state.used:
                        continue
                    for candidate in candidates:
                        next_state = self._play_turn_candidate(
                            game, state, group_index, candidate, monsters,
                            target_indexes, active_indexes,
                            time_warp_remaining, total_loss,
                        )
                        if next_state is not None:
                            proposals.append(next_state)
                            expanded += 1
            if not proposals:
                break
            deduped = {}
            for state in proposals:
                key = (
                    state.used, state.energy, state.cards_played,
                    state.normality_cards_played, state.hp,
                    state.block, state.poison, state.artifact, state.vulnerable,
                    state.weak, state.lock_on,
                    state.corpse_explosion, state.damage_value,
                    state.poison_value, state.choke,
                    state.mode_shift_remaining,
                    state.curl_up_block,
                    state.malleable_next_block,
                    state.flight_stacks,
                    state.orbs, state.orb_slots,
                    state.player_artifact,
                     state.player_buffer, state.player_block,
                     state.player_no_block,
                     state.player_intangible, state.player_intangible_turns, state.player_vulnerable,
                     state.player_hp, state.player_rage,
                    state.player_after_image,
                    state.player_accuracy,
                    state.player_thousand_cuts,
                    state.player_sadistic_nature,
                    state.player_feel_no_pain,
                    state.player_dark_embrace,
                    state.player_corruption,
                    state.player_heatsinks,
                    state.player_storm,
                    state.player_double_tap,
                    state.player_burst,
                    state.player_duplication,
                    state.attack_resolutions_played,
                    state.first_action_resolution_count,
                     state.player_pen_nib,
                     state.player_focus_bonus,
                     state.player_electrodynamics,
                     state.player_static_discharge,
                     state.player_thorns,
                     state.player_flame_barrier,
                     state.player_rupture,
                     state.player_hex,
                     state.rupture_triggers,
                     state.rupture_strength_gained,
                     state.player_strength_bonus,
                    state.enemy_strength_bonus,
                    state.enemy_strength_reduction,
                    state.remaining_hand_indexes,
                    state.hand_size,
                    state.draw_pile_size,
                    state.sundial_draw_pile_size,
                    state.sundial_discard_pile_size,
                    state.sundial_counter,
                    state.sundial_shuffle_count,
                    state.sundial_energy_gained,
                    state.drawn_void_count,
                    state.draw_energy_loss,
                    state.future_draw_pile_unknown,
                    state.generated_dazed,
                    state.generated_voids,
                    state.discarded_this_turn,
                    state.next_turn_energy,
                    state.no_draw,
                    state.mitigation,
                    state.neutralized,
                    state.end_turn_relief, state.self_hp_cost,
                    state.voluntary_self_hp_cost,
                    state.reactive_hp_cost,
                    state.raw_orichalcum_block,
                    state.generated_block,
                    state.end_turn_damage_events,
                    state.end_turn_aoe_damage,
                    state.extra_combust_hp_loss,
                    state.extra_brutality_amount,
                    round(state.lifecycle_liability, 3),
                    round(state.forced_exhaust_lifecycle_cost, 3),
                    state.random_hand_unknown,
                    state.writhing_mass_intent_unknown,
                    state.forced_end,
                )
                if key not in deduped or state.score > deduped[key].score:
                    deduped[key] = state
            ranked = sorted(
                deduped.values(),
                # A frontier state is only a prefix.  Keep ordinary score as
                # the beam heuristic so a temporarily unsafe attack/setup
                # prefix can survive long enough to complete a kill or a
                # defensive combo.  The strict same-tier HP ordering belongs
                # only to the final, executable plan selection below.
                key=lambda state: beam_rank(
                    self._terminal_turn_score(
                        game, state, monsters, attack_loss, total_loss,
                        risk_budget, time_warp_remaining,
                    )
                ),
                reverse=True,
            )
            frontier = ranked[:beam_width]
            # Preserve at most eight setup prefixes in addition to the normal
            # width. This avoids widening every combat while preventing a
            # zero-cost setup (for example Rage) from being pruned before its
            # downstream attack is searched.
            setup_prefixes = [
                state for state in ranked
                if deferred_setup_prefix(state)
            ][:8]
            for state in setup_prefixes:
                if state not in frontier:
                    frontier.append(state)
            terminals.extend(frontier)

        scored = [
            (
                self._terminal_turn_score(
                    game, state, monsters, attack_loss, total_loss, risk_budget,
                    time_warp_remaining,
                ),
                state,
            )
            for state in terminals
        ]
        initial_result = self._terminal_turn_score(
            game, initial, monsters, attack_loss, total_loss, risk_budget,
            time_warp_remaining,
        )
        initial_score = initial_result[0]
        self._last_initial_search = dict(initial_result[1])
        self._last_single_card_search = {}
        for result, state in scored:
            if len(state.plan) != 1:
                continue
            candidate = state.plan[0]
            previous = self._last_single_card_search.get(id(candidate))
            value = (dict(result[1]), result[0])
            if previous is None or (
                value[0]["tier"], value[1]
            ) > (
                previous[0]["tier"], previous[1]
            ):
                self._last_single_card_search[id(candidate)] = value
        verified_fallbacks = [
            (
                initial_result[1]["actual_loss"] - result[1]["actual_loss"],
                result[1]["tier"],
                result[0],
                state.plan[0],
                dict(result[1]),
            )
            for result, state in scored
            if len(state.plan) == 1
            and (
                result[1]["tier"] > 0
                or not any(
                    combat_predictor.power_amount(
                        monster, "Beat of Death", "BeatOfDeathPower"
                    ) > 0
                    for monster in combat_predictor.active_monsters(game)
                )
            )
            and result[1]["actual_loss"]
            < initial_result[1]["actual_loss"]
            and self._fallback_respects_lifecycle(state.plan[0])
        ]
        if verified_fallbacks:
            selected_fallback = max(
                verified_fallbacks, key=lambda item: item[:3]
            )
            self._last_verified_fallback = selected_fallback[-2]
            self._last_verified_fallback_search = selected_fallback[-1]
        else:
            self._last_verified_fallback = None
            self._last_verified_fallback_search = {}
        selected_item = max(
            scored,
            key=lambda item: terminal_rank(item[0]),
        )
        # Only break ties between the same ordered actions and targets.
        # An upgraded unrelated card is not a reason to change the whole plan.
        signature = lambda item: tuple(
            (_token(c.card.card_id), id(c.target), c.card.cost) for c in item[1].plan
        )
        equivalents = [
            item for item in scored
            if terminal_rank(item[0]) == terminal_rank(selected_item[0])
            and signature(item) == signature(selected_item)
        ]
        selected_item = max(equivalents, key=lambda item: tuple(
            int(getattr(c.card, "upgrades", 0) or 0) for c in item[1].plan
        ))
        target_allocation_tiebreak = None
        target_allocation_selected_state = None
        selected_score_override = None
        selected_tier = int(selected_item[0][1].get("tier", 0) or 0)
        if survival_priority and selected_tier == 1:
            equivalent_items = [
                item for item in scored
                if target_allocation_equivalent(selected_item, item)
            ]
            if equivalent_items:
                baseline_credit = float(
                    (
                        selected_item[0][1].get(
                            "next_turn_finish_window", {}
                        ) or {}
                    ).get("credit", 0.0)
                    or 0.0
                )
                preferred_item = max(
                    equivalent_items,
                    key=lambda item: (
                        float(
                            (
                                item[0][1].get(
                                    "next_turn_finish_window", {}
                                ) or {}
                            ).get("credit", 0.0)
                            or 0.0
                        ),
                        terminal_rank(item[0]),
                    ),
                )
                preferred_credit = float(
                    (
                        preferred_item[0][1].get(
                            "next_turn_finish_window", {}
                        ) or {}
                    ).get("credit", 0.0)
                    or 0.0
                )
                if preferred_credit > baseline_credit:
                    target_allocation_tiebreak = {
                        "applied": True,
                        "equivalent_terminal_count": len(equivalent_items),
                        "baseline_credit": round(baseline_credit, 3),
                        "selected_credit": round(preferred_credit, 3),
                        "scope": "same_cards_same_loss_same_total_progress",
                    }
                    selected_score_override = max(
                        float(selected_item[0][0]),
                        float(preferred_item[0][0]),
                    )
                    selected_item = preferred_item
                    target_allocation_selected_state = preferred_item[1]
        (score, details), best = selected_item
        if selected_score_override is not None:
            score = selected_score_override
        self_damage_comparison = None
        selected_self_cost = int(
            details.get("voluntary_self_hp_cost", 0) or 0
        )
        if selected_self_cost > 0:
            lower_cost_items = [
                item for item in scored
                if int(
                    item[0][1].get("voluntary_self_hp_cost", 0) or 0
                ) < selected_self_cost
            ]
            lower_cost_items.sort(
                key=lambda item: terminal_rank(item[0]), reverse=True
            )
            dominating = None
            dominance_reason = None
            best_lower = lower_cost_items[0] if lower_cost_items else None
            for alternative_item in lower_cost_items:
                dominates, reason = lower_self_damage_plan_dominates(
                    selected_item, alternative_item
                )
                if dominates:
                    dominating = alternative_item
                    dominance_reason = reason
                    break
            if dominating is not None:
                self_damage_comparison = {
                    "evaluated": True,
                    "dominated": True,
                    "status": "rejected_dominated_plan",
                    "reason": dominance_reason,
                    "rejected_plan": self_damage_summary(selected_item),
                    "chosen_plan": self_damage_summary(dominating),
                }
                selected_item = dominating
                (score, details), best = selected_item
            else:
                material_reason = "no_lower_cost_terminal"
                if best_lower is not None:
                    _, material_reason = self_damage_material_advantage(
                        selected_item, best_lower
                    )
                self_damage_comparison = {
                    "evaluated": True,
                    "dominated": False,
                    "status": "selected_with_material_advantage",
                    "reason": material_reason,
                    "selected_plan": self_damage_summary(selected_item),
                    "best_lower_cost_plan": (
                        self_damage_summary(best_lower)
                        if best_lower is not None else None
                    ),
                }
        if (
            details["tier"] == 0
            and initial_result[1]["tier"] == 0
            and any(
                combat_predictor.power_amount(
                    monster, "Beat of Death", "BeatOfDeathPower"
                )
                > 0
                for monster in combat_predictor.active_monsters(game)
            )
        ):
            # On Heart, every extra card creates another ordered damage event.
            # If the unified terminal says every searched line is lethal, do
            # not choose a card merely because its ordinary score is higher.
            score, details = initial_result
            best = initial
        if (
            target_allocation_tiebreak is not None
            and best is not target_allocation_selected_state
        ):
            # A later self-damage dominance or Heart fail-safe replaced the
            # target-only choice; do not claim the discarded tie-break ran.
            target_allocation_tiebreak = None
        score -= initial_score
        self._last_search = {
            "expanded_states": expanded,
            "beam_width": beam_width,
            "terminal_states": len(terminals),
            "combat_threat": self.combat_threat_profile(game, monsters),
            **details,
        }
        if target_allocation_tiebreak is not None:
            self._last_search["target_allocation_tiebreak"] = (
                target_allocation_tiebreak
            )
        if self_damage_comparison is not None:
            self._last_search[
                "voluntary_self_damage_comparison"
            ] = self_damage_comparison
        return score, list(best.plan)

    def project_end_turn(self, game):
        """Return the same exact END projection used by ordered search.

        The shared predictor cannot model enemy-attack reactions that mutate
        combat state between hits, notably Static Discharge orb channels and
        their overflow evokes. Running the empty plan through the terminal
        scorer keeps END logging, potion decisions, and plan ranking on one
        authoritative mechanics path.
        """

        self._sync_confirmed_card_plays(game)
        turn_outcome = combat_predictor.projected_turn_outcome(game)
        attack_loss = turn_outcome.attack_hp_loss
        total_loss = turn_outcome.total_hp_loss
        act_budget = {1: 8, 2: 4, 3: 2, 4: 0}.get(
            int(getattr(game, "act", 0) or 0), 2
        )
        risk_budget = min(
            act_budget,
            max(
                0,
                int(getattr(game.player, "current_hp", 0) or 0) // 8,
            ),
        )
        self._best_plan(
            game, [], attack_loss, total_loss, risk_budget
        )
        return dict(getattr(self, "_last_search", {}) or {})

    @staticmethod
    def _time_warp_remaining(game):
        time_eater = next(
            (
                monster for monster in combat_predictor.active_monsters(game)
                if _token(getattr(monster, "monster_id", "")) == "timeeater"
            ),
            None,
        )
        if time_eater is None:
            return None
        played = combat_predictor.power_amount(
            time_eater,
            "Time Warp",
            "TimeWarpPower",
        )
        return 12 - played if 0 <= played < 12 else 12

    def _time_eater_reset_progress_candidate(self, game, groups, total_loss):
        """Use a safe legal card to trigger Time Eater's final reset.

        With eleven cards already played, END can look harmless when the
        current attack is already covered or the intent is a defend/debuff.
        The next turn may then spend its first card only to trigger Time Warp,
        wasting the rest of that turn.  Prefer a useful block card at low HP;
        otherwise a cheap persistent/future-turn setup is also a valid reset.
        Pure damage is still left to the normal search, so this fallback does
        not burn a low-value Strike solely to advance the counter.
        """

        if self._time_warp_remaining(game) != 1:
            return None
        if max(0, int(total_loss or 0)) > 0:
            return None
        choker_remaining = self._velvet_choker_remaining(game)
        if choker_remaining is not None and choker_remaining <= 0:
            return None
        energy = max(0, int(getattr(game.player, "energy", 0) or 0))
        hp = max(1, int(getattr(game.player, "current_hp", 1) or 1))
        max_hp = max(hp, int(getattr(game.player, "max_hp", hp) or hp))
        safety_reserve = self._reactive_safety_reserve(game, hp)
        choices = []
        for group in groups:
            for candidate in group:
                card = candidate.card
                # Some cards enter the beam only because an earlier energy
                # generator could make them reachable.  This fallback issues
                # the card immediately, so current-frame legality is required.
                if not getattr(card, "is_playable", False):
                    continue
                if getattr(card, "type", None) in {
                    CardType.CURSE,
                    CardType.STATUS,
                }:
                    continue
                cost = combat_predictor.card_energy_cost(game, card)
                if cost > energy:
                    continue
                if (
                    int(getattr(candidate, "self_hp_cost", 0) or 0) > 0
                    or int(getattr(candidate, "reactive_hp_cost", 0) or 0) > 0
                ):
                    continue
                block = max(0, int(getattr(candidate, "block_gain", 0) or 0))
                damage = max(0, int(getattr(candidate, "damage", 0) or 0))
                mitigation = max(
                    0, int(getattr(candidate, "intrinsic_mitigation", 0) or 0)
                )
                # The one-card terminal search is authoritative when present;
                # reject a candidate whose forced reset would be lethal or
                # leave less than the dynamic safety reserve.
                entry = getattr(self, "_last_single_card_search", {}).get(
                    id(candidate)
                )
                if entry is not None:
                    details = entry[0]
                    if int(details.get("tier", 0) or 0) <= 0:
                        continue
                    if int(details.get("actual_loss", 0) or 0) >= hp:
                        continue
                    if (
                        hp - int(details.get("actual_loss", 0) or 0)
                        < safety_reserve
                    ):
                        continue
                    if (
                        bool(details.get("haste_triggered"))
                        and (
                            int(details.get("haste_healed", 0) or 0) > 0
                            or int(
                                details.get("haste_erased_poison", 0) or 0
                            ) > 0
                        )
                        and not bool(details.get("true_combat_end"))
                    ):
                        continue
                future_value = (
                    max(
                        0.0,
                        float(
                            getattr(candidate, "future_turn_weak_value", 0.0)
                            or 0.0
                        ),
                    )
                    + max(
                        0.0,
                        float(
                            getattr(candidate, "future_turn_block_value", 0.0)
                            or 0.0
                        ),
                    )
                    + max(
                        0.0,
                        float(
                            getattr(candidate, "future_turn_energy_value", 0.0)
                            or 0.0
                        ),
                    )
                    + max(
                        0.0,
                        float(
                            getattr(candidate, "lifecycle_adjustment", 0.0)
                            or 0.0
                        ),
                    )
                )
                persistent_value = 0.0
                if getattr(card, "type", None) == CardType.POWER:
                    persistent_value = max(
                        0.0,
                        float(getattr(candidate, "base_score", 0.0) or 0.0),
                    )
                setup_value = persistent_value + future_value
                defensive_reset = block > 0 or mitigation > 0
                # A non-defensive setup is admitted only when the exact
                # one-card terminal was searched and its benefit survives the
                # forced end.  Bullet Time and other same-turn-only effects do
                # not qualify; Powers, future Weak/energy, and positive
                # lifecycle setup do.
                if not defensive_reset and (
                    entry is None or setup_value <= 0.75
                ):
                    continue
                # At low HP a block card is the safest twelfth card because
                # its block survives the forced reset into the next enemy
                # intent.  At healthy HP prefer durable setup, using cost and
                # immediate safety only as tie-breaks.
                low_hp = hp * 2 <= max_hp
                actual_loss = (
                    int(entry[0].get("actual_loss", 0) or 0)
                    if entry is not None
                    else 0
                )
                key = (
                    1 if block > 0 and low_hp else 0,
                    setup_value,
                    -actual_loss,
                    block,
                    mitigation,
                    float(getattr(candidate, "order_score", 0.0) or 0.0),
                    -cost,
                    damage,
                )
                choices.append((key, candidate))
        if not choices:
            return None
        return max(choices, key=lambda item: item[0])[1]

    def _safe_corrupted_block_cycle_candidate(self, game, groups):
        """Cycle a free pure block Skill into an attack-only draw source.

        The normal terminal search evaluates the visible Skill but stops at
        Dark Embrace's newly drawn card.  That makes a Fairy-revival-neutral
        block card appear to have no positive marginal value even though
        Corruption exhausts it for free and the next authoritative frame can
        play the attack it draws.  Admit only the fail-closed case where every
        possible draw is an Attack and at least one remains affordable.
        """

        player = getattr(game, "player", None)
        if player is None or not combat_predictor.has_power(
            player, "Corruption", "CorruptionPower"
        ) or not combat_predictor.has_power(
            player, "Dark Embrace", "DarkEmbracePower"
        ):
            return None
        if combat_predictor.has_power(
            player,
            "No Draw", "NoDrawPower",
            "Confusion", "ConfusionPower",
            "Hex", "HexPower",
        ):
            return None
        if any(
            _token(getattr(card, "card_id", "")) in {"normality", "pain"}
            for card in getattr(game, "hand", []) or []
        ):
            return None
        if (
            self._has_relic(game, "Strange Spoon")
            or self._has_relic(game, "Velvet Choker")
            or self._has_relic(game, "Pocketwatch")
            or self._time_warp_remaining(game) is not None
            or any(
                combat_predictor.has_power(
                    monster,
                    "Beat of Death", "BeatOfDeathPower",
                    "Choke", "ChokePower",
                    "Enrage", "EnragePower",
                )
                for monster in combat_predictor.active_monsters(game)
            )
        ):
            return None

        future_cards = list(getattr(game, "draw_pile", []) or [])
        draw_source = "draw"
        if not future_cards:
            future_cards = list(getattr(game, "discard_pile", []) or [])
            draw_source = "discard_shuffle"
        if not future_cards or any(
            getattr(card, "type", None) != CardType.ATTACK
            for card in future_cards
        ):
            return None

        energy = max(0, int(getattr(player, "energy", 0) or 0))
        affordable_attack_ids = [
            getattr(card, "card_id", None)
            for card in future_cards
            if (
                bool(getattr(card, "is_playable", True))
                and combat_predictor.card_energy_cost(game, card) <= energy
                and max(
                    int(getattr(card, "damage", 0) or 0),
                    int(getattr(card, "base_damage", 0) or 0),
                ) > 0
            )
        ]
        if not affordable_attack_ids:
            return None

        initial_actual_loss = int(
            getattr(self, "_last_initial_search", {}).get("actual_loss", 0)
        )
        choices = []
        for group in groups:
            for candidate in group:
                card = candidate.card
                if (
                    getattr(card, "type", None) != CardType.SKILL
                    or _token(getattr(card, "card_id", ""))
                    not in self.SAFE_CORRUPTED_BLOCK_CYCLE_CARDS
                    or combat_predictor.card_energy_cost(game, card) != 0
                    or int(getattr(candidate, "block_gain", 0) or 0) <= 0
                    or int(getattr(candidate, "self_hp_cost", 0) or 0) > 0
                    or int(getattr(candidate, "reactive_hp_cost", 0) or 0) > 0
                    or float(getattr(candidate, "base_score", 0.0) or 0.0)
                    <= -1.0
                ):
                    continue
                terminal_entry = getattr(
                    self, "_last_single_card_search", {}
                ).get(id(candidate))
                if terminal_entry is None:
                    continue
                terminal_details = terminal_entry[0]
                if (
                    terminal_details.get("tier", 0) <= 0
                    or int(terminal_details.get("actual_loss", 0))
                    > initial_actual_loss
                ):
                    continue
                choices.append((
                    int(getattr(candidate, "block_gain", 0) or 0),
                    float(getattr(candidate, "base_score", 0.0) or 0.0),
                    float(getattr(candidate, "order_score", 0.0) or 0.0),
                    candidate,
                ))
        if not choices:
            return None
        chosen = max(choices, key=lambda item: item[:3])[-1]
        return chosen, {
            "draw_source": draw_source,
            "future_attack_count": len(future_cards),
            "affordable_attack_ids": affordable_attack_ids,
        }

    def _status_cleanup_candidate(self, game, groups):
        """Use otherwise wasted energy to remove a playable dead status.

        This is deliberately a fallback after the normal multi-card plan and
        avoidable-damage mitigation. Encounter penalties already included in
        ``base_score`` prevent cleanup into Beat of Death or Time Warp when
        playing another card would be materially harmful.
        """

        time_warp_remaining = self._time_warp_remaining(game)
        if time_warp_remaining is not None or any(
            combat_predictor.power_amount(
                monster, "Beat of Death", "BeatOfDeathPower"
            )
            > 0
            for monster in combat_predictor.active_monsters(game)
        ):
            return None
        energy = max(0, int(getattr(game.player, "energy", 0) or 0))
        candidates = []
        initial_actual_loss = int(
            getattr(self, "_last_initial_search", {}).get(
                "actual_loss", 0
            )
        )
        for group in groups:
            for candidate in group:
                card = candidate.card
                if _token(getattr(card, "card_id", "")) not in self.DEAD_STATUS_CARDS:
                    continue
                if getattr(card, "type", None) != CardType.STATUS:
                    continue
                cost = combat_predictor.card_energy_cost(game, card)
                if cost > energy:
                    continue
                terminal_entry = getattr(
                    self, "_last_single_card_search", {}
                ).get(id(candidate))
                if terminal_entry is None:
                    continue
                terminal_details = terminal_entry[0]
                if (
                    terminal_details.get("tier", 0) <= 0
                    or int(terminal_details.get("actual_loss", 0))
                    > initial_actual_loss
                ):
                    continue
                # A plain Slimed card is slightly negative only because it
                # spends energy. Larger penalties signal an encounter where
                # playing a harmless card has a real downside.
                if candidate.base_score <= -1.0:
                    continue
                candidates.append((-cost, candidate.base_score, candidate.order_score, candidate))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[:3])[-1]

    def _combat_turn_key(self, game):
        return (*self._current_combat_key(game), int(getattr(game, "turn", 0) or 0))

    def _sync_confirmed_card_plays(self, game):
        """Confirm selected cards only after the authoritative hand changes."""

        turn_key = self._combat_turn_key(game)
        if turn_key != self._card_play_turn_key:
            self._card_play_turn_key = turn_key
            self._confirmed_cards_played = 0
            self._confirmed_card_resolutions = 0
            self._confirmed_attack_resolutions = 0
            self._card_play_pending_uuid = None
            self._card_play_pending_resolutions = 1
            self._card_play_pending_is_attack = False
            self._clear_terminal_plan()
            return
        pending_uuid = self._card_play_pending_uuid
        if pending_uuid is None:
            return
        if all(
            getattr(card, "uuid", None) != pending_uuid
            for card in getattr(game, "hand", []) or []
        ):
            self._confirmed_cards_played += 1
            self._confirmed_card_resolutions += max(
                1, int(self._card_play_pending_resolutions or 1)
            )
            if self._card_play_pending_is_attack:
                self._confirmed_attack_resolutions += max(
                    1, int(self._card_play_pending_resolutions or 1)
                )
            self._card_play_pending_uuid = None
            self._card_play_pending_resolutions = 1
            self._card_play_pending_is_attack = False

    def _play_card_action(self, game, card, target=None):
        # Every card-selection path (terminal continuation, verified
        # fallback, reactive rescue, and ordinary beam output) eventually
        # passes through this constructor.  Keep the protocol action itself
        # fail-closed for an X-cost attack with no energy: a few legacy
        # fallback paths can otherwise bypass the search-time guard and emit
        # a zero-hit Whirlwind/Skewer.  Chemical X or another effect that
        # gives the card a real X result remains eligible because the
        # branch-local effect is positive.
        raw_cost = int(getattr(card, "cost", 0) or 0)
        card_type = getattr(card, "type", None)
        if (
            raw_cost == -1
            and card_type == CardType.ATTACK
            and max(0, int(getattr(game.player, "energy", 0) or 0)) <= 0
            and self._x_effect(
                game,
                card,
                upgraded_bonus=True,
                energy_override=0,
            ) <= 0
        ):
            self._clear_terminal_plan()
            self.last_decision = {
                "reason": "zero_energy_x_attack_no_effect",
                "card_id": getattr(card, "card_id", None),
                "card_uuid": getattr(card, "uuid", None),
                "energy": 0,
            }
            return EndTurnAction()
        self._card_play_pending_uuid = getattr(card, "uuid", None)
        self._card_play_pending_resolutions = max(
            1,
            int(
                getattr(self, "last_decision", {})
                .get("search", {})
                .get("first_action_resolution_count", 1)
                or 1
            ),
        )
        self._card_play_pending_is_attack = (
            getattr(card, "type", None) == CardType.ATTACK
        )
        if target is not None:
            return PlayCardAction(card=card, target_monster=target)
        return PlayCardAction(card=card)

    def _reactive_progress_used_this_turn(self, game):
        pending = self._reactive_progress_pending
        if pending is None:
            return False
        turn_key, card_uuid = pending
        if turn_key != self._combat_turn_key(game):
            self._reactive_progress_pending = None
            return False
        if card_uuid is None:
            return True
        # The controller may reject an action without advancing the state.  In
        # that case the selected UUID remains in hand and the safe retry must
        # not be mistaken for a second progress attack.
        return all(
            getattr(card, "uuid", None) != card_uuid
            for card in getattr(game, "hand", []) or []
        )

    def _remember_reactive_progress(self, game, candidate):
        self._reactive_progress_pending = (
            self._combat_turn_key(game),
            getattr(candidate.card, "uuid", None),
        )

    @staticmethod
    def _panic_button_is_emergency(game, total_loss):
        """Reserve Panic Button unless the current turn is genuinely severe.

        Its block also suppresses subsequent card block while No Block lasts.
        Use this gate on the residual loss of an ordinary defensive plan when
        one is available, rather than treating raw incoming damage as need.
        Lethal loss always clears it; otherwise the required loss scales from
        12 to 20 with current HP.
        """

        hp = max(1, int(getattr(game.player, "current_hp", 1) or 1))
        loss = max(0, int(total_loss or 0))
        if loss >= hp:
            return True
        severe_threshold = max(12, min(20, (hp + 2) // 3))
        return loss >= severe_threshold

    @staticmethod
    def _passive_wait_mitigation_candidate(candidate):
        """Whether a non-target card can plausibly reduce player-end loss."""

        card = candidate.card
        if getattr(card, "has_target", False):
            return False
        if getattr(card, "type", None) == CardType.ATTACK:
            return False
        card_id = _token(getattr(card, "card_id", ""))
        return (
            candidate.block_gain > 0
            or candidate.buffer_gain > 0
            or candidate.intrinsic_mitigation > 0
            or card_id in FastCombatPlanner.INTANGIBLE_CARDS
            or card_id in FastCombatPlanner.DEAD_STATUS_CARDS
        )

    def _orichalcum_cumulative_block_rescue(self, game, plan):
        """Admit a searched pure-block line that first beats Ori at the end.

        Orichalcum makes the first few small Block cards look non-positive:
        one or more cards can merely replace its six Block before the final
        card crosses that floor.  The ordered beam already evaluates the
        complete line exactly; this narrow gate prevents the later marginal
        score cutoff from discarding that proof while still refusing to spend
        one or two cards which never improve on Orichalcum.
        """

        if not plan or len(plan) < 2:
            return None
        player = getattr(game, "player", None)
        if (
            player is None
            or not self._has_relic(game, "Orichalcum")
            or not combat_predictor.can_gain_block(player)
            or max(0, int(getattr(player, "block", 0) or 0)) != 0
            or self._non_orichalcum_end_turn_block(game) != 0
            or self._time_warp_remaining(game) is not None
        ):
            return None

        initial = dict(getattr(self, "_last_initial_search", {}) or {})
        final = dict(getattr(self, "_last_search", {}) or {})
        initial_loss = max(0, int(initial.get("actual_loss", 0) or 0))
        final_loss = max(0, int(final.get("actual_loss", initial_loss) or 0))
        if (
            initial_loss <= 0
            or final_loss != 0
            or final_loss >= initial_loss
            or final.get("forced_end")
            or final.get("random_hand_unknown")
        ):
            return None

        cumulative_block = 0
        prefix_blocks = []
        for candidate in plan:
            card = candidate.card
            block_gain = max(
                0, int(getattr(candidate, "block_gain", 0) or 0)
            )
            if (
                getattr(card, "type", None) != CardType.SKILL
                or getattr(candidate, "target", None) is not None
                or block_gain <= 0
                or self._static_card_block_gain(game, card) <= 0
                or max(0, int(getattr(candidate, "damage", 0) or 0)) > 0
                or max(
                    0,
                    int(getattr(candidate, "intrinsic_mitigation", 0) or 0),
                ) != block_gain
                or max(0, int(getattr(candidate, "buffer_gain", 0) or 0)) > 0
                or max(0, int(getattr(candidate, "end_turn_relief", 0) or 0)) > 0
                or max(0, int(getattr(candidate, "self_hp_cost", 0) or 0)) > 0
                or max(0, int(getattr(candidate, "reactive_hp_cost", 0) or 0)) > 0
            ):
                return None
            cumulative_block += block_gain
            prefix_blocks.append(cumulative_block)

        orichalcum_floor = 6
        if (
            cumulative_block <= orichalcum_floor
            or any(
                value > orichalcum_floor for value in prefix_blocks[:-1]
            )
            or int(final.get("final_non_orichalcum_block", 0) or 0)
            < cumulative_block
        ):
            return None
        return {
            "candidate": plan[0],
            "initial_loss": initial_loss,
            "final_loss": final_loss,
            "orichalcum_floor": orichalcum_floor,
            "prefix_blocks": prefix_blocks,
            "cumulative_block": cumulative_block,
        }

    def _calculated_gamble_rescue(self, game, playable, total_loss):
        """Prove one no-shuffle Calculated Gamble survival continuation.

        This is deliberately an emergency-only exact lookahead.  It consumes
        the visible top cards only when the draw pile contains the complete
        redraw, then asks an isolated planner whether that authoritative hand
        has a nonlethal continuation.  Any shuffle or encounter trigger which
        would make the post-Gamble state uncertain rejects the shortcut.
        """

        if self._disable_calculated_gamble_rescue:
            return None
        player = getattr(game, "player", None)
        hp = max(1, int(getattr(player, "current_hp", 1) or 1))
        if max(0, int(total_loss or 0)) < hp:
            return None
        gambles = [
            card
            for card in playable
            if _token(getattr(card, "card_id", "")) == "calculatedgamble"
        ]
        if not gambles:
            return None
        if combat_predictor.has_power(
            player, "No Draw", "NoDrawPower", "Confusion"
        ):
            return None
        if combat_predictor.has_power(player, "No Block", "NoBlockPower"):
            # The redraw proof relies on the continuation planner's block
            # model.  Panic Button's No Block state invalidates every
            # defensive redraw until that central model explicitly proves the
            # whole turn, so this emergency shortcut must fail closed.
            return None
        if combat_predictor.has_power(player, "Choked", "ChokedPower"):
            return None
        if self._time_warp_remaining(game) is not None:
            return None
        active = combat_predictor.active_monsters(game)
        if any(
            combat_predictor.power_amount(
                monster, "Beat of Death", "BeatOfDeathPower"
            )
            > 0
            for monster in active
        ):
            return None
        if any(
            _token(getattr(monster, "monster_id", "")) == "gremlinnob"
            for monster in active
        ) and int(getattr(game, "turn", 0) or 0) > 1:
            return None
        hand = list(getattr(game, "hand", []) or [])
        if any(
            _token(getattr(card, "card_id", ""))
            in {"normality", "pain", "reflex", "tactician"}
            for card in hand
        ):
            return None
        if any(
            self._has_relic(game, relic_id)
            for relic_id in {"Unceasing Top", "Velvet Choker"}
        ):
            return None
        if self._has_relic(game, "Ink Bottle"):
            # Calculated Gamble itself advances the counter; any starting
            # value can make a later continuation cross ten and draw another
            # card which this single-redraw proof does not simulate.
            return None
        if self._has_relic(game, "Strange Spoon"):
            # Whether the base Gamble exhausts is random with Strange Spoon.
            # That changes exhaust triggers (and therefore block/draw/card
            # generation), so the result cannot honestly be called exact.
            return None

        draw_pile = list(getattr(game, "draw_pile", []) or [])
        current_energy = max(0, int(getattr(player, "energy", 0) or 0))
        choices = []
        for gamble in gambles:
            cost = combat_predictor.card_energy_cost(game, gamble)
            if cost > current_energy:
                continue
            # Calculated Gamble exhausts until upgraded.  Exhaust-triggered
            # draws/card generation are deliberately outside this one-card
            # redraw model, so reject those states rather than labelling a
            # partial simulation exact.  Deriving the base-card behaviour
            # from upgrades also protects fixtures/older payloads whose
            # ``exhausts`` field is missing or stale.
            gamble_exhausts = bool(getattr(gamble, "exhausts", False)) or (
                int(getattr(gamble, "upgrades", 0) or 0) <= 0
            )
            if gamble_exhausts and (
                combat_predictor.has_power(
                    player, "Dark Embrace", "DarkEmbracePower"
                )
                or self._has_relic(game, "Dead Branch")
            ):
                continue
            draw_count = self._deterministic_discard_count(
                gamble, len(hand)
            )
            if draw_count <= 0 or len(draw_pile) < draw_count:
                # Drawing past the visible pile would shuffle the discard pile
                # at a random order, so no deterministic rescue can be claimed.
                continue
            # CardGroup stores its top at the end; repeated draws therefore
            # expose the visible suffix in reverse order.
            if getattr(game, "draw_pile_order_known", True) is not True:
                continue
            drawn_source = list(reversed(draw_pile[-draw_count:]))
            # These cards have hand-dependent canUse rules whose serialized
            # draw-pile flag can change after the redraw.  Refuse to turn that
            # stale flag into an alleged exact survival line.
            conditional_ids = {
                "clash", "concentrate", "grandfinale", "reflex",
                "normality", "signaturemove", "stormofsteel", "tactician",
                "void",
            }
            if any(
                _token(getattr(card, "card_id", "")) in conditional_ids
                for card in drawn_source
            ):
                continue
            if combat_predictor.has_power(
                player, "Evolve", "EvolvePower"
            ) and any(
                getattr(card, "type", None) == CardType.STATUS
                for card in drawn_source
            ):
                # Every drawn Status causes another draw, changing both the
                # hand and the remaining draw pile from this hypothetical.
                continue

            hypothetical = copy.copy(game)
            hypothetical.player = copy.copy(player)
            hypothetical.player.energy = current_energy - cost
            generated_block = 0
            if combat_predictor.can_gain_block(game):
                generated_block = combat_predictor.power_amount(
                    player, "After Image", "AfterImagePower"
                )
                if self._has_relic(game, "Tough Bandages"):
                    generated_block += draw_count * 3
                if (
                    gamble_exhausts
                    and not self._has_relic(game, "Strange Spoon")
                ):
                    generated_block += combat_predictor.power_amount(
                        player, "Feel No Pain", "FeelNoPainPower"
                    )
            hypothetical.player.block = max(
                0, int(getattr(player, "block", 0) or 0)
            ) + generated_block
            hypothetical.hand = [copy.copy(card) for card in drawn_source]
            hypothetical.draw_pile = list(draw_pile[:-draw_count])
            discarded = [card for card in hand if card is not gamble]
            hypothetical.discard_pile = list(
                getattr(game, "discard_pile", []) or []
            ) + discarded
            hypothetical.cards_discarded_this_turn = (
                int(getattr(game, "cards_discarded_this_turn", 0) or 0)
                + len(discarded)
            )

            planner = FastCombatPlanner(self.priorities)
            planner._disable_calculated_gamble_rescue = True
            continuation = planner.choose_card_action(hypothetical)
            decision = dict(planner.last_decision or {})
            if isinstance(continuation, PlayCardAction):
                continuation_card = continuation.card
                continuation_id = _token(
                    getattr(continuation_card, "card_id", "")
                )
                pure_non_target_mitigation = (
                    getattr(continuation, "target_monster", None) is None
                    and not getattr(continuation_card, "has_target", False)
                    and getattr(continuation_card, "type", None)
                    != CardType.ATTACK
                    and (
                        int(getattr(continuation_card, "block", 0) or 0) > 0
                        or continuation_id in self.INTANGIBLE_CARDS
                        or continuation_id == "buffer"
                    )
                )
                if not pure_non_target_mitigation:
                    # A fresh planner has no outer focus/reactive state.  Do
                    # not prove survival with a target-dependent kill or a
                    # multi-step setup which the real next frame is not bound
                    # to reproduce.
                    continue
            elif not isinstance(continuation, EndTurnAction):
                continue
            search = decision.get("search")
            candidate_loss = None
            if isinstance(search, dict):
                value = search.get("actual_loss")
                if isinstance(value, int) and not isinstance(value, bool):
                    candidate_loss = max(0, value)
            if candidate_loss is None and isinstance(
                continuation, EndTurnAction
            ):
                value = decision.get("projected_hp_loss")
                if isinstance(value, int) and not isinstance(value, bool):
                    candidate_loss = max(0, value)
            if candidate_loss is None or candidate_loss >= hp:
                continue
            if candidate_loss >= max(0, int(total_loss or 0)):
                continue
            choices.append((
                -candidate_loss,
                draw_count,
                gamble,
                {
                    "actual_loss": candidate_loss,
                    "baseline_loss": max(0, int(total_loss or 0)),
                    "redraw_count": draw_count,
                    "drawn_card_ids": [
                        getattr(card, "card_id", None) for card in drawn_source
                    ],
                    "continuation_reason": decision.get("reason"),
                    "continuation_card_id": getattr(
                        getattr(continuation, "card", None), "card_id", None
                    ),
                    "continuation_search": dict(search or {}),
                },
            ))
        if not choices:
            return None
        selected = max(choices, key=lambda item: item[:2])
        return selected[2], selected[3]

    def _resource_generation_rescue(
        self, game, playable, total_loss, risk_budget
    ):
        """Use a visible energy/draw card before accepting avoidable damage.

        The ordered beam normally discovers Offering/Bloodletting/Adrenaline
        lines.  A narrow failure mode remains when the line starts at zero
        energy and the only downstream card is currently marked unplayable:
        the first defensive card can consume the last energy, after which the
        resource card is left in hand and the planner returns END.  When the
        current loss is already above the risk budget, spend one *safe* resource
        card if it demonstrably unlocks another card or a deterministic draw.
        This is intentionally a one-card rescue; the authoritative next frame
        replans the newly drawn hand, so no speculative multi-action sequence is
        issued here.
        """

        loss = max(0, int(total_loss or 0))
        if loss <= max(0, int(risk_budget or 0)):
            return None
        player = getattr(game, "player", None)
        hp = max(1, int(getattr(player, "current_hp", 1) or 1))
        reserve = self._reactive_safety_reserve(game, hp)
        if combat_predictor.has_power(
            player, "No Draw", "NoDrawPower", "Confusion"
        ):
            # A draw card can still grant an effect, but its card/energy
            # continuation is not deterministic under these powers.
            allow_draw = False
        else:
            allow_draw = True
        if any(
            combat_predictor.power_amount(
                monster, "Beat of Death", "BeatOfDeathPower"
            ) > 0
            for monster in combat_predictor.active_monsters(game)
        ):
            return None
        # A resource-only card is not a valid rescue when the next play would
        # consume Time Eater's final safe slot and force the heal/reset before
        # the newly generated resource can be used.
        time_warp_remaining = self._time_warp_remaining(game)
        if time_warp_remaining is not None and time_warp_remaining <= 1:
            return None
        choker_remaining = self._velvet_choker_remaining(game)
        if choker_remaining is not None and choker_remaining <= 1:
            return None

        current_energy = max(0, int(getattr(player, "energy", 0) or 0))
        hand = list(getattr(game, "hand", []) or [])
        active = combat_predictor.active_monsters(game)
        choices = []
        for card in playable:
            card_id = _token(getattr(card, "card_id", ""))
            energy_gain = max(0, int(self._energy_gain(card, game) or 0))
            draw_count = self._card_draw_count(
                card,
                game,
                hand_size_before_play=len(hand),
            )
            if energy_gain <= 0 and draw_count <= 0:
                continue
            if draw_count > 0 and not allow_draw:
                # Keep a pure energy card eligible under No Draw, but do not
                # pretend a draw-only card can rescue a deterministic line.
                if energy_gain <= 0:
                    continue
                draw_count = 0
            cost = combat_predictor.card_energy_cost(game, card)
            if cost > current_energy:
                continue
            target = None
            if getattr(card, "has_target", False):
                if not active:
                    continue
                target = self._prepare_focus(game, active)
                if target is None:
                    continue
            if not self._writhing_mass_fallback_attack_is_safe(
                game, card, target,
            ):
                continue
            self_events = self._card_self_damage_events(game, card)
            self_outcome = combat_predictor.resolve_player_damage_events(
                game,
                tuple(
                    combat_predictor.PlayerDamageEvent(
                        "card_self", amount, blockable=False
                    )
                    for amount in self_events
                ),
                block=max(0, int(getattr(player, "block", 0) or 0)),
                buffer_layers=combat_predictor.power_amount(
                    player, "Buffer"
                ),
            )
            _raw_damage, hits = combat_predictor.card_attack_profile(
                game, card, target=target
            )
            thorns_events, sharp_hide_events = (
                self._card_reactive_damage_events(
                    game, card, target, hits, active_targets=active
                )
            )
            reactive_outcome = combat_predictor.resolve_player_damage_events(
                game,
                tuple(
                    combat_predictor.PlayerDamageEvent(
                        "thorns", amount, blockable=True
                    )
                    for amount in thorns_events
                ) + tuple(
                    combat_predictor.PlayerDamageEvent(
                        "sharp_hide", amount, blockable=True
                    )
                    for amount in sharp_hide_events
                ),
                block=self_outcome.block,
                buffer_layers=self_outcome.buffer,
            )
            self_cost = self_outcome.hp_loss + reactive_outcome.hp_loss
            if hp - self_cost < reserve:
                continue
            after_energy = max(0, current_energy - cost + energy_gain)
            remaining = [candidate for candidate in hand if candidate is not card]
            unlocks_card = any(
                candidate is not None
                and getattr(candidate, "type", None)
                not in {CardType.CURSE, CardType.STATUS}
                and combat_predictor.card_energy_cost(game, candidate)
                <= after_energy
                for candidate in remaining
            )
            deterministic_draw = draw_count > 0 and bool(
                getattr(game, "draw_pile", []) or []
            )
            if not unlocks_card and not deterministic_draw:
                continue
            if not deterministic_draw:
                initial = getattr(self, "_last_initial_search", {}) or {}
                selected = getattr(self, "_last_search", {}) or {}
                if (
                    initial
                    and selected
                    and selected.get("actual_loss", loss) >= initial.get("actual_loss", loss)
                    and sum(selected.get("final_enemy_hp", []))
                    >= sum(initial.get("final_enemy_hp", []))
                ):
                    # The ordered search already evaluated the known hand.
                    # Mere affordability cannot overrule its finding that
                    # energy converts into neither safer END nor damage.
                    continue
            # Prefer cards which buy more immediate energy, then guaranteed
            # draw, while charging the real HP cost as a small tie-break.
            value = energy_gain * 5 + draw_count * 2 - self_cost * 0.5
            choices.append((
                value, energy_gain, draw_count, -self_cost,
                card, target, self_cost,
            ))
        if not choices:
            return None
        selected = max(choices, key=lambda item: item[:4])
        card, target, self_cost = selected[4:]
        # A draw/energy rescue is still a real card play.  Targeted draw
        # cards (most notably Pommel Strike) must carry the same authoritative
        # enemy object as an ordinary attack; returning only the card silently
        # drops the target when the controller serializes the next action.
        return card, target, self_cost

    def _doomed_turn_draw_replan(self, game, groups):
        """Expose a new hand when END and every searched line are lethal.

        A terminal forecast is not a proof that an unseen draw cannot rescue
        the turn. Only issue one surviving draw resolution, with energy and a
        play slot left to use its result; replan from the authoritative frame.
        Do not replace any known surviving line; bound added loss to the
        immediate cost of the draw resolution.
        """
        initial = getattr(self, "_last_initial_search", {}) or {}
        selected = getattr(self, "_last_search", {}) or {}
        if initial.get("tier") != 0 or selected.get("tier") != 0:
            return None
        if combat_predictor.has_power(
            game.player, "No Draw", "NoDrawPower", "Confusion"
        ):
            return None
        if not (getattr(game, "draw_pile", []) or getattr(game, "discard_pile", [])):
            return None
        for remaining in (
            self._time_warp_remaining(game),
            self._velvet_choker_remaining(game),
        ):
            if remaining is not None and remaining <= 1:
                return None
        choices = []
        for group in groups:
            for candidate in group:
                draw_count = self._card_draw_count(
                    candidate.card, game,
                    hand_size_before_play=len(game.hand),
                )
                if draw_count <= 0:
                    continue
                entry = getattr(self, "_last_single_card_search", {}).get(
                    id(candidate)
                )
                if entry is None:
                    continue
                details, score = entry
                if (
                    int(details.get("player_hp_after_cards", 0)) <= 0
                    or int(details.get("remaining_energy", 0)) <= 0
                    or details.get("forced_end")
                    or int(details.get("actual_loss", 0))
                    > int(initial.get("actual_loss", 0)) + max(
                        0, int(game.player.current_hp)
                        - int(details.get("player_hp_after_cards", 0))
                    )
                    or not self._writhing_mass_fallback_attack_is_safe(
                        game, candidate.card, candidate.target,
                    )
                ):
                    continue
                choices.append((
                    -int(details.get("actual_loss", 0)),
                    int(details.get("player_hp_after_cards", 0)),
                    draw_count, score, candidate, details,
                ))
        if not choices:
            return None
        best = max(choices, key=lambda item: item[:4])
        return best[4], dict(best[5])

    def _reactive_progress_fallback(self, game, groups, total_loss):
        """Choose one safe, efficient attack when strict HP ranking stalls.

        Dangerous-room tier-1 plans deliberately sort by exact current-turn
        HP loss before ordinary score.  Against Sharp Hide/Thorns this can
        make END permanently dominate every nonlethal attack, even while the
        enemy keeps attacking.  This fallback admits one meaningful attack,
        with the same exact terminal simulation and dynamic safety reserve,
        without weakening the global survival ordering.
        """

        if self._reactive_progress_used_this_turn(game):
            return None
        active = combat_predictor.active_monsters(game)
        scaling_pressure = sum(
            self._monster_scaling_pressure(monster, game)
            for monster in active
        )
        unavoidable_card_tax = any(
            combat_predictor.power_amount(
                monster, "Beat of Death", "BeatOfDeathPower"
            ) > 0
            for monster in active
        )
        if (
            max(0, int(total_loss or 0)) <= 0
            and scaling_pressure <= 0
            and not unavoidable_card_tax
        ):
            return None

        initial = dict(getattr(self, "_last_initial_search", {}) or {})
        initial_loss = max(0, int(initial.get("actual_loss", total_loss) or 0))
        player_hp = max(1, int(getattr(game.player, "current_hp", 1) or 1))
        reserve = self._reactive_safety_reserve(game, player_hp)
        doomed_ids = {
            id(monster)
            for monster in combat_predictor.projected_doomed_monsters(game)
        }
        choices = []
        for group in groups:
            for candidate in group:
                card = candidate.card
                if getattr(card, "type", None) != CardType.ATTACK:
                    continue
                if (
                    not (candidate.reactive_damage_events or unavoidable_card_tax)
                    or candidate.damage <= 0
                ):
                    continue
                if candidate.self_damage_events:
                    # Keep this escape hatch about reactive progress.  Cards
                    # with an additional voluntary HP cost need the ordinary
                    # full-plan policy rather than silently expanding risk.
                    continue
                if candidate.target is not None and id(candidate.target) in doomed_ids:
                    continue
                entry = getattr(self, "_last_single_card_search", {}).get(
                    id(candidate)
                )
                if entry is None:
                    continue
                details, terminal_score = entry
                details = dict(details)
                if int(details.get("tier", 0) or 0) <= 0:
                    continue
                final_hp = (
                    int(details.get("player_hp_after_cards", player_hp) or 0)
                    - int(details.get("projected_loss", total_loss) or 0)
                )
                if final_hp < reserve:
                    continue
                nominal_reactive = sum(
                    max(0, int(amount or 0))
                    for amount in candidate.reactive_damage_events
                ) + sum(
                    max(0, combat_predictor.power_amount(
                        monster, "Beat of Death", "BeatOfDeathPower"
                    ))
                    for monster in active
                ) * max(1, int(details.get("first_action_resolution_count", 1)))
                if nominal_reactive <= 0:
                    continue
                actual_loss = max(
                    0, int(details.get("actual_loss", initial_loss) or 0)
                )
                added_loss = max(0, actual_loss - initial_loss)
                # This also covers reactive damage which first consumes block
                # and only later lets the enemy attack reach HP.  Do not admit
                # unrelated extra loss larger than the known reaction events.
                if added_loss > nominal_reactive:
                    continue
                progress = max(0, int(candidate.damage or 0))
                transition_gain = max(
                    0,
                    int(initial.get("projected_loss", total_loss) or 0)
                    - int(details.get("projected_loss", total_loss) or 0),
                )
                new_deaths = max(
                    0,
                    int(details.get("enemies_dead", 0) or 0)
                    - int(initial.get("enemies_dead", 0) or 0),
                )
                if (
                    progress < max(6, nominal_reactive * 2)
                    and transition_gain <= 0
                    and new_deaths <= 0
                ):
                    continue
                efficiency = progress / max(1, nominal_reactive)
                choices.append((
                    new_deaths,
                    transition_gain,
                    efficiency,
                    progress,
                    -added_loss,
                    float(terminal_score),
                    candidate,
                    details,
                    nominal_reactive,
                    added_loss,
                ))
        if not choices:
            return None
        selected = max(choices, key=lambda item: item[:6])
        return selected[6], selected[7], selected[8], selected[9]

    def _apply_true_grit_damage_reserve_guard(
        self, game, groups, total_loss,
    ):
        """Keep forced exhaust from deleting the deck's last damage route.

        The constraint is based on the current combat piles, not character or
        starter-deck assumptions.  It applies only when every possible True
        Grit target in the current hand is an Attack, the current turn is not
        already lethal, and exhausting one would leave an inadequate damage
        reserve.  Two sources are retained against recurring enemy defense;
        elsewhere only the final source is protected.
        """

        true_grit_candidates = [
            candidate
            for group in groups
            for candidate in group
            if _token(getattr(candidate.card, "card_id", "")) == "truegrit"
        ]
        if not true_grit_candidates:
            return groups, None

        hand = list(getattr(game, "hand", []) or [])
        true_grit_cards = {
            id(candidate.card) for candidate in true_grit_candidates
        }
        forced_targets = [
            card for card in hand if id(card) not in true_grit_cards
        ]
        if not forced_targets or any(
            getattr(card, "type", None) != CardType.ATTACK
            for card in forced_targets
        ):
            return groups, None

        hp = max(1, int(getattr(game.player, "current_hp", 1) or 1))
        if max(0, int(total_loss or 0)) >= hp:
            # A lethal turn may require accepting long-fight damage loss in
            # order to survive the immediate attack.
            return groups, None

        attack_sources = {}
        for pile_name in ("hand", "draw_pile", "discard_pile", "limbo"):
            for card in getattr(game, pile_name, []) or []:
                if getattr(card, "type", None) != CardType.ATTACK:
                    continue
                identity = getattr(card, "uuid", None) or id(card)
                attack_sources[identity] = card

        living = combat_predictor.living_monsters(game)
        persistent_defenders = []
        for monster in living:
            plated = combat_predictor.power_amount(
                monster, "Plated Armor", "PlatedArmorPower"
            )
            regenerate = combat_predictor.power_amount(
                monster, "Regenerate", "Regeneration", "RegeneratePower"
            )
            barricade = combat_predictor.has_power(
                monster, "Barricade", "BarricadePower"
            )
            if plated > 0 or regenerate > 0 or barricade:
                persistent_defenders.append({
                    "monster_id": getattr(monster, "monster_id", None),
                    "plated_armor": max(0, int(plated or 0)),
                    "regenerate": max(0, int(regenerate or 0)),
                    "barricade": bool(barricade),
                    "block": max(0, int(getattr(monster, "block", 0) or 0)),
                })

        minimum_reserve = 2 if persistent_defenders else 1
        if len(attack_sources) > minimum_reserve:
            return groups, None

        filtered = [
            [
                candidate
                for candidate in group
                if _token(getattr(candidate.card, "card_id", ""))
                != "truegrit"
            ]
            for group in groups
        ]
        return filtered, {
            "engaged": True,
            "reason": (
                "preserve_two_damage_sources_against_recurring_defense"
                if persistent_defenders
                else "preserve_last_damage_source"
            ),
            "attack_source_count": len(attack_sources),
            "minimum_attack_source_reserve": minimum_reserve,
            "forced_target_card_ids": [
                getattr(card, "card_id", None) for card in forced_targets
            ],
            "persistent_defenders": persistent_defenders,
            "baseline_projected_hp_loss": max(0, int(total_loss or 0)),
            "player_hp": hp,
        }

    def choose_card_action(self, game):
        self._sync_confirmed_card_plays(game)
        choker_remaining = self._velvet_choker_remaining(game)
        if choker_remaining == 0:
            self._clear_terminal_plan()
            self.last_decision = {
                "reason": "velvet_choker_card_limit",
                "confirmed_cards_played": int(
                    self._confirmed_cards_played or 0
                ),
                "projected_hp_loss": int(
                    combat_predictor.projected_turn_outcome(game).total_hp_loss
                ),
            }
            return EndTurnAction()
        continuation = self._continue_terminal_plan(game)
        if continuation is not None:
            return continuation
        all_living = combat_predictor.living_monsters(game)
        active = combat_predictor.active_monsters(game)
        hand = list(getattr(game, "hand", []) or [])
        playable = [
            card for card in hand if getattr(card, "is_playable", False)
        ]
        # CommunicationMod marks a card unaffordable in the current frame even
        # when a legal Turbo/Seeing Red/Adrenaline line makes it affordable.
        # Admit only that narrow class of currently-unplayable cards; the
        # state search still cannot choose one before generating the energy.
        current_energy = max(0, int(getattr(game.player, "energy", 0) or 0))
        discarded_authoritatively = (
            int(getattr(game, "cards_discarded_this_turn", 0) or 0) > 0
        )
        reachable_energy = current_energy + sum(
            self._energy_gain(card, game)
            + self._initial_channel_plasma_energy(game, card)
            # This is only an admission upper bound.  The ordered transition
            # still checks the exact target debuff before granting the refund.
            + (
                1
                if _token(getattr(card, "card_id", ""))
                in {"dropkick", "heelhook"}
                else 2
                if (
                    _token(getattr(card, "card_id", ""))
                    == "sneakystrike"
                    and discarded_authoritatively
                )
                else 0
            )
            for card in playable
        )
        if reachable_energy > current_energy:
            playable_ids = {id(card) for card in playable}
            playable.extend(
                card for card in hand
                if id(card) not in playable_ids
                and int(getattr(card, "cost", -1) or 0) > current_energy
                and int(getattr(card, "cost", -1) or 0) <= reachable_energy
                and getattr(card, "type", None) not in {CardType.CURSE, CardType.STATUS}
            )
        # Corruption can make a currently-unaffordable Skill cost zero later
        # in this same branch.  Admit only cards whose raw cost is the reason
        # visible in this frame: other conditional illegality remains outside
        # the beam and is left to the next authoritative replan.
        corruption_reachable = combat_predictor.has_power(
            game.player, "Corruption"
        ) or any(
            _token(getattr(card, "card_id", "")) == "corruption"
            for card in playable
        )
        if corruption_reachable:
            playable_ids = {id(card) for card in playable}
            playable.extend(
                card
                for card in hand
                if id(card) not in playable_ids
                and getattr(card, "type", None) == CardType.SKILL
                and int(getattr(card, "cost", -1) or 0) > current_energy
            )
        # Entangled is an authoritative player debuff that makes Attacks
        # illegal for this turn.  Most frames already expose
        # ``is_playable=False``; keep a protocol-level guard so a stale or
        # older bridge cannot make the planner issue an invalid Attack.
        if combat_predictor.has_power(game.player, "Entangled"):
            playable = [
                card
                for card in playable
                if getattr(card, "type", None) != CardType.ATTACK
            ]
        base_active_ids = {id(monster) for monster in active}
        active = self._restore_immediate_reptomancer_attack_targets(
            game, playable, active
        )
        search_active_override = (
            list(active)
            if any(id(monster) not in base_active_ids for monster in active)
            else None
        )
        profitable_doomed_cards = self._profitable_doomed_cards(
            game, playable, all_living
        )
        safe_passive_wait = combat_predictor.safe_to_wait_for_passive_kills(game)
        passive_wait_only = False
        if (
            not active
            and all_living
            and safe_passive_wait
            and not profitable_doomed_cards
        ):
            turn_outcome = combat_predictor.projected_turn_outcome(
                game, combat_ends_before_next_turn=True
            )
            attack_loss = turn_outcome.attack_hp_loss
            end_turn_loss = turn_outcome.end_turn_hp_loss
            next_turn_start_loss = turn_outcome.next_turn_start_hp_loss
            if end_turn_loss <= 0:
                self.last_decision = {
                    "reason": "all_enemies_passively_doomed",
                    "projected_attack_hp_loss": attack_loss,
                    "projected_end_turn_hp_loss": end_turn_loss,
                    "projected_next_turn_start_hp_loss": next_turn_start_loss,
                    "projected_hp_loss": turn_outcome.total_hp_loss,
                    "search": {
                        "true_combat_end": True,
                        "final_enemy_hp": [0 for _ in all_living],
                        "stone_calendar_damage": int(
                            combat_predictor.stone_calendar_damage(game)
                        ),
                    },
                }
                return EndTurnAction()
            # Poison/Combust kills happen after Burn, Decay and Constricted.
            # Give the exact terminal search only non-target, non-attack cards
            # so it may prevent that avoidable loss without spending damage on
            # an enemy which is already guaranteed to die.
            passive_wait_only = True
            active = all_living
            playable = [
                card
                for card in playable
                if not getattr(card, "has_target", False)
                and getattr(card, "type", None) != CardType.ATTACK
            ]
        if not active:
            active = all_living
            if safe_passive_wait and profitable_doomed_cards:
                playable = profitable_doomed_cards
        if not playable or not active:
            turn_outcome = combat_predictor.projected_turn_outcome(
                game,
                combat_ends_before_next_turn=(
                    bool(passive_wait_only) or not all_living
                ),
            )
            attack_loss = turn_outcome.attack_hp_loss
            end_turn_loss = turn_outcome.end_turn_hp_loss
            next_turn_start_loss = turn_outcome.next_turn_start_hp_loss
            self.last_decision = {
                "reason": (
                    "all_enemies_passively_doomed"
                    if passive_wait_only
                    else "no_playable_card_or_target"
                ),
                "projected_attack_hp_loss": attack_loss,
                "projected_end_turn_hp_loss": end_turn_loss,
                "projected_next_turn_start_hp_loss": next_turn_start_loss,
                "projected_hp_loss": turn_outcome.total_hp_loss,
                "search": ({
                    "true_combat_end": True,
                    "final_enemy_hp": [0 for _ in all_living],
                    "stone_calendar_damage": int(
                        combat_predictor.stone_calendar_damage(game)
                    ),
                } if passive_wait_only else {}),
            }
            return EndTurnAction()

        preferred_target = self._prepare_focus(game, active)
        incoming = combat_predictor.incoming_damage(game)
        turn_outcome = combat_predictor.projected_turn_outcome(game)
        attack_loss = turn_outcome.attack_hp_loss
        end_turn_loss = turn_outcome.end_turn_hp_loss
        next_turn_start_loss = turn_outcome.next_turn_start_hp_loss
        total_loss = turn_outcome.total_hp_loss
        lethal_combo = None if passive_wait_only else self._guaranteed_attack_combo(
            game, playable, active, total_loss, end_turn_loss
        )
        feed_wait = None if passive_wait_only else self._safe_feed_fatal_wait(
            game, playable, active, turn_outcome, lethal_combo
        )
        if feed_wait is not None:
            target = active[0]
            self.last_decision = {
                "reason": "wait_for_feed_fatal",
                "feed_card_id": getattr(feed_wait, "card_id", None),
                "feed_card_uuid": getattr(feed_wait, "uuid", None),
                "target_key": list(_monster_key(target)),
                "target_hp": int(getattr(target, "current_hp", 0) or 0),
                "projected_attack_hp_loss": attack_loss,
                "projected_end_turn_hp_loss": end_turn_loss,
                "projected_next_turn_start_hp_loss": next_turn_start_loss,
                "projected_hp_loss": total_loss,
                "wait_turns": 1,
            }
            return EndTurnAction()
        if lethal_combo is not None:
            card, target, cost, card_count = lethal_combo
            combo_search = dict(getattr(self, "_last_combo_search", {}))
            rage_setup = max(
                (
                    setup for setup in playable
                    if _token(getattr(setup, "card_id", "")) == "rage"
                    and combat_predictor.card_energy_cost(game, setup) == 0
                ),
                key=lambda setup: max(
                    3, int(getattr(setup, "magic_number", 0) or 0)
                ),
                default=None,
            )
            after_image_setup = max(
                (
                    setup for setup in playable
                    if _token(getattr(setup, "card_id", "")) == "afterimage"
                    and (
                        combat_predictor.card_energy_cost(game, setup) + cost
                        <= max(
                            0, int(getattr(game.player, "energy", 0) or 0)
                        )
                    )
                ),
                key=lambda setup: max(
                    1, int(getattr(setup, "magic_number", 0) or 0)
                ),
                default=None,
            )
            beat_source = any(
                combat_predictor.power_amount(
                    monster, "Beat of Death", "BeatOfDeathPower"
                ) > 0
                for monster in active
            )
            nob_hazard = any(
                monster is not target
                and _token(getattr(monster, "monster_id", ""))
                == "gremlinnob"
                and combat_predictor.power_amount(
                    monster, "Enrage", "EnragePower"
                ) > 0
                for monster in active
            )
            time_eater = next((
                monster for monster in active
                if _token(getattr(monster, "monster_id", ""))
                == "timeeater"
            ), None)
            time_eater_room = True
            if time_eater is not None:
                played = combat_predictor.power_amount(
                    time_eater, "Time Warp", "TimeWarpPower"
                )
                remaining = 12 - played if 0 <= played < 12 else 12
                time_eater_room = card_count + 1 <= remaining
            choker_room = True
            choker_remaining = self._velvet_choker_remaining(game)
            if choker_remaining is not None:
                choker_room = card_count + 1 <= choker_remaining
            combo_actual_loss = int(
                combo_search.get("actual_loss", total_loss) or 0
            )
            setup_options = []
            if (
                rage_setup is not None
                and combo_actual_loss > 0
                and combat_predictor.can_gain_block(game)
                and not beat_source
                and not nob_hazard
                and time_eater_room
                and choker_room
            ):
                rage_amount = max(
                    3, int(getattr(rage_setup, "magic_number", 0) or 0)
                )
                setup_options.append(
                    (rage_amount * card_count, "rage", rage_setup)
                )
            if (
                after_image_setup is not None
                and combo_actual_loss > 0
                and combat_predictor.can_gain_block(game)
                and not beat_source
                and time_eater_room
                and choker_room
            ):
                after_image_amount = max(
                    1,
                    int(
                        getattr(after_image_setup, "magic_number", 0) or 0
                    ),
                )
                curiosity_cost = 0
                for monster in active:
                    if monster is target:
                        continue
                    curiosity = combat_predictor.power_amount(
                        monster, "Curiosity", "CuriosityPower"
                    )
                    intent = getattr(monster, "intent", None)
                    if curiosity <= 0 or not (
                        intent is not None and intent.is_attack()
                    ):
                        continue
                    hits = max(
                        1, int(getattr(monster, "move_hits", 0) or 0)
                    )
                    curiosity_cost += (
                        self._strength_damage_bonus_per_hit(
                            game, monster, curiosity
                        )
                        * hits
                    )
                net_block = after_image_amount * card_count - curiosity_cost
                if net_block > 0:
                    setup_options.append(
                        (net_block, "after_image", after_image_setup)
                    )

            # The exact-lethal shortcut used to return the first attack of a
            # proven multi-attack kill before considering Flex/Inflame.  That
            # is a real ordering bug: both cards strengthen every later hit,
            # and the missed damage can change a boss phase or leave a lethal
            # enemy alive.  Add them only when the setup plus the proven combo
            # fits energy/card-count limits and there is a downstream attack
            # to consume the strength.
            strength_setups = []
            combo_ids = [
                _token(card_id)
                for card_id in combo_search.get("combo_card_ids", [])
                if card_id
            ]
            if (
                combo_ids
                and not beat_source
                and time_eater_room
                and choker_room
            ):
                energy_now = max(
                    0, int(getattr(game.player, "energy", 0) or 0)
                )
                for setup in playable:
                    setup_id = _token(getattr(setup, "card_id", ""))
                    if setup_id not in {"flex", "inflame"}:
                        continue
                    setup_cost = combat_predictor.card_energy_cost(game, setup)
                    if setup_cost + max(0, int(cost or 0)) > energy_now:
                        continue
                    strength_gain = self._immediate_strength_gain(
                        game, setup, target, 0
                    )
                    if strength_gain <= 0:
                        continue
                    affected_hits = 0
                    extra_damage = 0
                    remaining_ids = list(combo_ids)
                    for candidate_card in playable:
                        candidate_id = _token(
                            getattr(candidate_card, "card_id", "")
                        )
                        if candidate_id not in remaining_ids:
                            continue
                        remaining_ids.remove(candidate_id)
                        raw, hits = combat_predictor.card_attack_profile(
                            game, candidate_card
                        )
                        if raw <= 0 or hits <= 0:
                            continue
                        affected_hits += hits
                        baseline = combat_predictor.attack_hp_loss(
                            target,
                            raw,
                            hits=hits,
                            **combat_predictor.attack_relic_modifiers(game),
                        )
                        strengthened = combat_predictor.attack_hp_loss(
                            target,
                            raw + strength_gain,
                            hits=hits,
                            **combat_predictor.attack_relic_modifiers(game),
                        )
                        extra_damage += max(0, strengthened - baseline)
                    if affected_hits and extra_damage:
                        # Inflame is permanent while Flex is temporary; the
                        # small tie-break keeps permanent setup preferred when
                        # both produce the same immediate damage.
                        value = extra_damage + (
                            1 if setup_id == "inflame" else 0
                        )
                        strength_setups.append(
                            (value, setup_id, setup)
                        )
            setup_options.extend(strength_setups)

            # A Thousand Cuts and Accuracy are same-turn trigger powers.  The
            # exact lethal shortcut historically returned the first attack of
            # a proven combo before considering either power, which produced
            # traces such as Shiv -> Endless Agony -> A Thousand Cuts.  Put a
            # safe, affordable trigger setup ahead of that combo while there
            # is still room for the planned attacks.  Beat of Death and Time
            # Warp remain hard hazards, matching the existing Rage/After
            # Image setup guards above.
            trigger_setups = []
            if not beat_source and time_eater_room and choker_room:
                energy_now = max(
                    0, int(getattr(game.player, "energy", 0) or 0)
                )
                combo_cost = max(0, int(cost or 0))
                for setup in playable:
                    setup_id = _token(getattr(setup, "card_id", ""))
                    setup_cost = combat_predictor.card_energy_cost(game, setup)
                    if setup_cost + combo_cost > energy_now:
                        continue
                    if setup_id == "athousandcuts":
                        amount = max(
                            1, int(getattr(setup, "magic_number", 0) or 0)
                        )
                        trigger_setups.append(
                            (amount * max(1, card_count), "athousand_cuts", setup)
                        )
                    elif setup_id == "accuracy":
                        shiv_count = sum(
                            1
                            for candidate_card in playable
                            if _token(
                                getattr(candidate_card, "card_id", "")
                            ) == "shiv"
                        )
                        if shiv_count > 0:
                            amount = max(
                                1,
                                int(getattr(setup, "magic_number", 0) or 0),
                            )
                            trigger_setups.append(
                                (
                                    amount * shiv_count,
                                    "accuracy",
                                    setup,
                                )
                            )
            setup_options.extend(trigger_setups)
            setup_choice = max(
                setup_options,
                key=lambda item: (item[0], item[1] == "after_image"),
                default=None,
            )
            first_action_enemy_hp_loss = 0
            first_action_card = (
                setup_choice[2] if setup_choice is not None else card
            )
            first_action_target = None if setup_choice is not None else target
            first_action_projection = self._current_action_search_projection(
                game, first_action_card, first_action_target
            )
            projected_first_loss = (
                first_action_projection.get("first_action_enemy_hp_loss")
                if isinstance(first_action_projection, dict) else None
            )
            if type(projected_first_loss) is int:
                first_action_enemy_hp_loss = projected_first_loss
            elif getattr(first_action_card, "type", None) == CardType.ATTACK:
                raw_first, hits_first = combat_predictor.card_attack_profile(
                    game, first_action_card
                )
                first_targets = (
                    [first_action_target]
                    if first_action_target is not None
                    else list(active)
                )
                if _is_random_multi_target_attack(
                    game, first_action_card, first_action_target
                ):
                    first_action_enemy_hp_loss = self._random_attack_hp_loss(
                        game, first_action_card, first_targets
                    )
                else:
                    first_action_enemy_hp_loss = sum(
                        self._current_attack_hp_loss(
                            game, first_action_card, enemy,
                            raw_first, hits_first,
                        )
                        for enemy in first_targets
                        if enemy is not None
                    )
            if setup_choice is not None:
                setup_value, setup_kind, setup_card = setup_choice
                # The exact lethal shortcut intentionally looks only at
                # attacks.  A setup which preserves the proven combo and
                # prevents more damage than it creates must resolve first;
                # otherwise the shortcut can place After Image/Rage after the
                # very cards which provide their same-turn payoff.
                self.last_decision = {
                    "reason": "guaranteed_combo_setup_before_attacks",
                    "card_id": getattr(setup_card, "card_id", None),
                    "setup_kind": setup_kind,
                    "setup_value": int(setup_value),
                    "target_key": None,
                    "combo_first_card_id": getattr(card, "card_id", None),
                    "combo_target_key": (
                        list(_monster_key(target))
                        if target is not None else None
                    ),
                    "combo_cards": card_count,
                    "combo_energy": cost,
                    "first_action_enemy_hp_loss": int(
                        first_action_enemy_hp_loss
                    ),
                    "projected_attack_hp_loss": attack_loss,
                    "projected_end_turn_hp_loss": end_turn_loss,
                    "projected_next_turn_start_hp_loss": (
                        next_turn_start_loss
                    ),
                    "projected_hp_loss": total_loss,
                    "search": combo_search,
                }
                return self._play_card_action(game, setup_card)
            if target is not None:
                self.focus_key = _monster_key(target)
            self.last_decision = {
                "reason": "guaranteed_attack_combo_lethal",
                "card_id": getattr(card, "card_id", None),
                "target_key": list(_monster_key(target)) if target is not None else None,
                "focus_key": list(self.focus_key) if self.focus_key is not None else None,
                "combo_cards": card_count,
                "combo_energy": cost,
                "first_action_enemy_hp_loss": int(
                    first_action_enemy_hp_loss
                ),
                "projected_attack_hp_loss": attack_loss,
                "projected_end_turn_hp_loss": end_turn_loss,
                "projected_next_turn_start_hp_loss": next_turn_start_loss,
                "projected_hp_loss": total_loss,
                "search": combo_search,
            }
            if target is not None:
                return self._play_card_action(game, card, target)
            return self._play_card_action(game, card)
        act_budget = {1: 8, 2: 4, 3: 2, 4: 0}.get(int(getattr(game, "act", 0) or 0), 2)
        risk_budget = min(act_budget, max(0, int(getattr(game.player, "current_hp", 0) or 0) // 8))

        self._last_lifecycle_fallback_rejections = {}
        groups = self._build_candidates(
            game, playable, active, preferred_target, incoming, attack_loss, total_loss
        )
        lifecycle_candidates = [
            candidate
            for group in groups
            for candidate in group
            if candidate.lifecycle_kind
        ]
        lifecycle_context = self._lifecycle_trace(
            max(
                lifecycle_candidates,
                key=lambda candidate: candidate.base_score,
            )
            if lifecycle_candidates
            else None
        )
        if passive_wait_only:
            groups = [
                [
                    candidate
                    for candidate in group
                    if self._passive_wait_mitigation_candidate(candidate)
                ]
                for group in groups
            ]
        elif not self._panic_button_is_emergency(game, total_loss):
            groups = [
                [
                    candidate
                    for candidate in group
                    if _token(getattr(candidate.card, "card_id", ""))
                    != "panicbutton"
                ]
                for group in groups
            ]
        true_grit_damage_reserve_guard = None
        if not passive_wait_only:
            groups, true_grit_damage_reserve_guard = (
                self._apply_true_grit_damage_reserve_guard(
                    game, groups, total_loss
                )
            )
        search_kwargs = {}
        if search_active_override is not None and not passive_wait_only:
            search_kwargs["active_monsters_override"] = (
                search_active_override
            )
        score, plan = self._best_plan(
            game,
            groups,
            attack_loss,
            total_loss,
            risk_budget,
            **search_kwargs,
        )
        if (
            any(_token(candidate.card.card_id) == "panicbutton" for candidate in plan)
            and not self._last_search.get("true_combat_end")
            and not combat_predictor.has_power(game.player, "Artifact")
        ):
            # Compare complete ordered plans: raw incoming damage ignores
            # ordinary block, energy relics and card retrieval. Preserve next
            # turn's card defense unless Panic Button buys material survival
            # or removes an additional enemy.
            ordinary_groups = [
                [candidate for candidate in group
                 if _token(candidate.card.card_id) != "panicbutton"]
                for group in groups
            ]
            search_fields = (
                "_last_search", "_last_initial_search",
                "_last_single_card_search", "_last_verified_fallback",
                "_last_verified_fallback_search",
            )
            selected_search = {
                field: getattr(self, field, None) for field in search_fields
            }
            selected_details = dict(self._last_search)
            ordinary_score, ordinary_plan = self._best_plan(
                game, ordinary_groups, attack_loss, total_loss, risk_budget,
                **search_kwargs,
            )
            ordinary_details = dict(self._last_search)
            preserve_defense = (
                bool(ordinary_plan)
                and int(ordinary_details.get("tier", 0)) > 0
                and not self._panic_button_is_emergency(
                    game, ordinary_details.get("actual_loss", total_loss)
                )
                and int(ordinary_details.get("enemies_dead", 0))
                >= int(selected_details.get("enemies_dead", 0))
                and float(ordinary_details.get("lifecycle_liability", 0))
                <= float(selected_details.get("lifecycle_liability", 0))
            )
            if preserve_defense:
                groups = ordinary_groups
                score, plan = ordinary_score, ordinary_plan
            else:
                for field, value in selected_search.items():
                    setattr(self, field, value)
            self._last_search["panic_button_comparison"] = {
                "preserved_future_card_block": preserve_defense,
                "selected_with_panic_loss": selected_details.get("actual_loss"),
                "ordinary_plan_loss": ordinary_details.get("actual_loss"),
                "ordinary_plan_tier": ordinary_details.get("tier"),
                "ordinary_plan": [
                    getattr(candidate.card, "card_id", None)
                    for candidate in ordinary_plan
                ],
            }
        # The last Choker slot is a hard opportunity boundary.  A positive
        # setup score (for example Demon Form or Seeing Red) must not consume
        # that final slot when a directly playable attack can cross the
        # target's current Block and there is no immediate reaction hazard or
        # survival tradeoff.  The next authoritative frame cannot recover a
        # skipped sixth play, so keep this rescue deliberately narrower than
        # ordinary attack scoring.
        if (
            choker_remaining == 1
            and not passive_wait_only
            and total_loss <= 0
        ):
            choker_hazard = any(
                combat_predictor.power_amount(
                    monster, "Beat of Death", "BeatOfDeathPower"
                ) > 0
                or combat_predictor.has_power(
                    monster,
                    "Thorns", "ThornsPower", "Sharp Hide", "SharpHidePower",
                )
                or _token(getattr(monster, "monster_id", ""))
                in {"timeeater", "gremlinnob", "corruptheart"}
                for monster in active
            )
            choker_progress = []
            if not choker_hazard:
                current_energy = max(
                    0, int(getattr(game.player, "energy", 0) or 0)
                )
                for group in groups:
                    for candidate in group:
                        if getattr(candidate.card, "type", None) != CardType.ATTACK:
                            continue
                        raw_damage, hp_damage, block_progress = (
                            _candidate_attack_progress(game, candidate)
                        )
                        if not _candidate_has_authoritative_target(game, candidate) or (
                            hp_damage <= 0 and block_progress <= 0
                        ):
                            continue
                        if combat_predictor.card_energy_cost(
                            game, candidate.card
                        ) > current_energy:
                            continue
                        if raw_damage <= 0:
                            continue
                        if not self._writhing_mass_fallback_attack_is_safe(
                            game,
                            candidate.card,
                            candidate.target,
                            extra_block=candidate.block_gain,
                        ):
                            continue
                        choker_progress.append(candidate)
            if choker_progress:
                chosen = max(
                    choker_progress,
                    key=lambda candidate: (
                        _candidate_attack_progress(game, candidate)[2],
                        _candidate_attack_progress(game, candidate)[1],
                        candidate.base_score,
                        -combat_predictor.card_energy_cost(
                            game, candidate.card
                        ),
                    ),
                )
                chosen_search = dict(getattr(self, "_last_search", {}))
                # This fallback may select a different target from the beam's
                # winning terminal.  Bind the first-action shadow damage to
                # the selected candidate, or a blocked Neutralize can inherit
                # another target's four HP damage in the audit trace.
                chosen_search.update({
                    "first_action_enemy_hp_loss": int(
                        getattr(chosen, "damage", 0) or 0
                    ),
                    "first_action_expected_enemy_hp_loss": int(
                        getattr(chosen, "damage", 0) or 0
                    ),
                    "first_action_enemy_hp_loss_is_expected": False,
                })
                self._clear_terminal_plan()
                self.focus_key = _monster_key(chosen.target)
                self.last_decision = {
                    "reason": "velvet_choker_progress_fallback",
                    "plan_score": round(score, 3),
                    "card_id": getattr(chosen.card, "card_id", None),
                    "target_key": list(self.focus_key),
                    "incoming": incoming,
                    "projected_attack_hp_loss": attack_loss,
                    "projected_end_turn_hp_loss": end_turn_loss,
                    "projected_hp_loss": total_loss,
                    "card_damage": chosen.damage,
                    "raw_attack_progress": _candidate_attack_progress(
                        game, chosen
                    )[0],
                    "blocked_attack_progress": _candidate_attack_progress(
                        game, chosen
                    )[2],
                    "choker_remaining": choker_remaining,
                    "search": chosen_search,
                }
                return self._play_card_action(
                    game, chosen.card, chosen.target
                )
        reset_progress = self._time_eater_reset_progress_candidate(
            game, groups, total_loss
        )
        if reset_progress is not None and (not plan or score <= 0.75):
            target = reset_progress.target
            reset_search = dict(
                getattr(self, "_last_single_card_search", {}).get(
                    id(reset_progress),
                    (getattr(self, "_last_search", {}), 0.0),
                )[0]
            )
            self.last_decision = {
                "reason": "time_eater_reset_progress",
                "plan_score": round(score, 3),
                "card_id": getattr(reset_progress.card, "card_id", None),
                "target_key": (
                    list(_monster_key(target)) if target is not None else None
                ),
                "incoming": incoming,
                "projected_attack_hp_loss": attack_loss,
                "projected_end_turn_hp_loss": end_turn_loss,
                "projected_hp_loss": total_loss,
                "card_mitigation": reset_progress.mitigation,
                "card_block_gain": reset_progress.block_gain,
                "card_damage": reset_progress.damage,
                "search": reset_search,
            }
            if target is not None:
                return self._play_card_action(game, reset_progress.card, target)
            return self._play_card_action(game, reset_progress.card)
        if passive_wait_only:
            initial_loss = int(
                getattr(self, "_last_initial_search", {}).get(
                    "actual_loss", total_loss
                )
                or 0
            )
            planned_loss = int(
                getattr(self, "_last_search", {}).get(
                    "actual_loss", initial_loss
                )
                or 0
            )
            if plan and planned_loss < initial_loss:
                chosen = plan[0]
                self.last_decision = {
                    "reason": "prevent_passive_kill_end_turn_loss",
                    "plan_score": round(score, 3),
                    "card_id": getattr(chosen.card, "card_id", None),
                    "incoming": incoming,
                    "projected_attack_hp_loss": attack_loss,
                    "projected_end_turn_hp_loss": end_turn_loss,
                    "projected_hp_loss": total_loss,
                    "card_mitigation": chosen.mitigation,
                    "card_intrinsic_mitigation": chosen.intrinsic_mitigation,
                    "card_block_gain": chosen.block_gain,
                    "planned_sequence": [
                        {
                            "card_id": getattr(candidate.card, "card_id", None),
                            "card_uuid": getattr(candidate.card, "uuid", None),
                            "target_key": None,
                        }
                        for candidate in plan
                    ],
                    "search": dict(getattr(self, "_last_search", {})),
                }
                return self._play_card_action(game, chosen.card)
            self.last_decision = {
                "reason": "all_enemies_passively_doomed",
                "projected_attack_hp_loss": attack_loss,
                "projected_end_turn_hp_loss": end_turn_loss,
                "projected_hp_loss": total_loss,
                "search": dict(getattr(self, "_last_initial_search", {})),
            }
            return EndTurnAction()
        if not plan or score <= 0.75:
            # The bounded search normally returns a positive plan for a
            # block card, but a narrow tier/risk cutoff can still collapse to
            # END while a cheap Defend/Dodge and Roll removes all current
            # incoming damage.  Make that safety invariant explicit instead
            # of relying on the score threshold (which is allowed to trade a
            # few HP in ordinary fights).
            beat_source = any(
                combat_predictor.power_amount(
                    monster, "Beat of Death", "BeatOfDeathPower"
                ) > 0
                for monster in combat_predictor.active_monsters(game)
            )
            progress_hazard = beat_source or any(
                combat_predictor.has_power(
                    monster,
                    "Thorns", "ThornsPower", "Sharp Hide", "SharpHidePower",
                )
                or combat_predictor.has_power(
                    monster,
                    "Regenerate", "Regeneration", "RegeneratePower",
                )
                or _token(getattr(monster, "monster_id", ""))
                in {"timeeater", "gremlinnob", "corruptheart"}
                for monster in combat_predictor.active_monsters(game)
            )
            player_block_reaction_hazard = (
                any(
                    _token(getattr(relic, "relic_id", ""))
                    in {"toughbandages", "ornamentalfan"}
                    for relic in getattr(game, "relics", []) or []
                )
                or combat_predictor.power_amount(
                    game.player, "Rage", "RagePower"
                ) > 0
            ) and any(
                _token(getattr(relic, "relic_id", "")) == "orichalcum"
                for relic in getattr(game, "relics", []) or []
            )
            progress_hazard = progress_hazard or player_block_reaction_hazard
            if total_loss <= 0 and not progress_hazard:
                progress_candidates = []
                for group in groups:
                    for candidate in group:
                        card = candidate.card
                        if getattr(card, "type", None) != CardType.ATTACK:
                            continue
                        raw_damage, hp_damage, block_progress = (
                            _candidate_attack_progress(game, candidate)
                        )
                        if not _candidate_has_authoritative_target(game, candidate) or (
                            hp_damage <= 0 and block_progress <= 0
                        ):
                            continue
                        if combat_predictor.card_energy_cost(game, card) > max(
                            0, int(getattr(game.player, "energy", 0) or 0)
                        ):
                            continue
                        if raw_damage <= 0:
                            continue
                        if not self._writhing_mass_fallback_attack_is_safe(
                            game,
                            candidate.card,
                            candidate.target,
                            extra_block=candidate.block_gain,
                        ):
                            continue
                        progress_candidates.append(candidate)
                if progress_candidates:
                    # If Double Tap is already active, spend it on a proven
                    # attack rather than ending with the one-shot power
                    # unused.  If the card is still in hand, take the setup
                    # now; the next authoritative frame will take the attack
                    # through the first branch above.
                    active_double_tap = combat_predictor.power_amount(
                        game.player, "Double Tap", "DoubleTapPower"
                    ) > 0
                    double_tap_card = next((
                        card for card in getattr(game, "hand", []) or []
                        if _token(getattr(card, "card_id", ""))
                        == "doubletap"
                        and combat_predictor.card_energy_cost(game, card)
                        <= max(0, int(getattr(game.player, "energy", 0) or 0))
                    ), None)
                    if active_double_tap:
                        offensive = max(
                            progress_candidates,
                            key=lambda candidate: (
                                _candidate_attack_progress(game, candidate)[2],
                                _candidate_attack_progress(game, candidate)[1],
                                candidate.base_score,
                                -combat_predictor.card_energy_cost(
                                    game, candidate.card
                                ),
                            ),
                        )
                        self.focus_key = _monster_key(offensive.target)
                        self.last_decision = {
                            "reason": "double_tap_attack_progress_fallback",
                            "plan_score": round(score, 3),
                            "card_id": getattr(
                                offensive.card, "card_id", None
                            ),
                            "target_key": list(self.focus_key),
                            "incoming": incoming,
                            "projected_attack_hp_loss": attack_loss,
                            "projected_end_turn_hp_loss": end_turn_loss,
                            "projected_hp_loss": total_loss,
                            "card_damage": offensive.damage,
                            "raw_attack_progress": _candidate_attack_progress(
                                game, offensive
                            )[0],
                            "blocked_attack_progress": _candidate_attack_progress(
                                game, offensive
                            )[2],
                            "search": dict(
                                getattr(self, "_last_search", {})
                            ),
                        }
                        return self._play_card_action(
                            game, offensive.card, offensive.target
                        )
                    if double_tap_card is not None:
                        self.last_decision = {
                            "reason": "double_tap_progress_setup",
                            "plan_score": round(score, 3),
                            "card_id": getattr(
                                double_tap_card, "card_id", None
                            ),
                            "incoming": incoming,
                            "projected_attack_hp_loss": attack_loss,
                            "projected_end_turn_hp_loss": end_turn_loss,
                            "projected_hp_loss": total_loss,
                            "follow_up_attack_ids": [
                                getattr(candidate.card, "card_id", None)
                                for candidate in progress_candidates
                            ],
                            "search": dict(
                                getattr(self, "_last_search", {})
                            ),
                        }
                        return self._play_card_action(game, double_tap_card)
                    # A zero-loss turn is not automatically a reason to END.
                    # The old fallback only rescued an active/pending Double
                    # Tap, so an ordinary attack that crossed the target's
                    # current Block could still be stranded behind
                    # ``no_positive_marginal_action`` (notably Writhing Mass
                    # at F39).  Preserve the conservative hazard gate above,
                    # and only take a normal attack when it produces actual
                    # HP progress; attacks that merely chip temporary Block
                    # remain planner-owned unless a durable armor rule makes
                    # that progress authoritative.
                    attack_progress = [
                        candidate
                        for candidate in progress_candidates
                        if (
                            _candidate_attack_progress(game, candidate)[1] > 0
                            and int(getattr(candidate, "self_hp_cost", 0) or 0)
                            <= 0
                            and int(
                                getattr(candidate, "reactive_hp_cost", 0) or 0
                            ) <= 0
                        )
                    ]
                    # When the exact search deliberately chose END to keep
                    # Art of War live, do not let this generic progress
                    # fallback override that relic-aware comparison.  A
                    # materially valuable attack would already have won the
                    # search and appeared in ``plan``.
                    preserves_art_of_war = bool(
                        getattr(self, "_last_initial_search", {}).get(
                            "art_of_war_preserved", False
                        )
                    )
                    if attack_progress and not preserves_art_of_war:
                        offensive = max(
                            attack_progress,
                            key=lambda candidate: (
                                _candidate_attack_progress(game, candidate)[1],
                                _candidate_attack_progress(game, candidate)[2],
                                candidate.base_score,
                                -combat_predictor.card_energy_cost(
                                    game, candidate.card
                                ),
                            ),
                        )
                        self.focus_key = _monster_key(offensive.target)
                        self.last_decision = {
                            "reason": "attack_progress_fallback",
                            "plan_score": round(score, 3),
                            "card_id": getattr(
                                offensive.card, "card_id", None
                            ),
                            "target_key": list(self.focus_key),
                            "incoming": incoming,
                            "projected_attack_hp_loss": attack_loss,
                            "projected_end_turn_hp_loss": end_turn_loss,
                            "projected_hp_loss": total_loss,
                            "card_damage": offensive.damage,
                            "raw_attack_progress": _candidate_attack_progress(
                                game, offensive
                            )[0],
                            "blocked_attack_progress": _candidate_attack_progress(
                                game, offensive
                            )[2],
                            "search": dict(getattr(self, "_last_search", {})),
                        }
                        return self._play_card_action(
                            game, offensive.card, offensive.target
                        )
            orichalcum_rescue = (
                None
                if beat_source
                else self._orichalcum_cumulative_block_rescue(game, plan)
            )
            if orichalcum_rescue is not None:
                defensive = orichalcum_rescue["candidate"]
                self.last_decision = {
                    "reason": "orichalcum_cumulative_block_threshold",
                    "plan_score": round(score, 3),
                    "card_id": getattr(defensive.card, "card_id", None),
                    "incoming": incoming,
                    "projected_attack_hp_loss": attack_loss,
                    "projected_end_turn_hp_loss": end_turn_loss,
                    "projected_hp_loss": total_loss,
                    "card_mitigation": defensive.mitigation,
                    "card_intrinsic_mitigation": (
                        defensive.intrinsic_mitigation
                    ),
                    "card_block_gain": defensive.block_gain,
                    "orichalcum_floor": orichalcum_rescue[
                        "orichalcum_floor"
                    ],
                    "cumulative_block": orichalcum_rescue[
                        "cumulative_block"
                    ],
                    "prefix_blocks": orichalcum_rescue["prefix_blocks"],
                    "baseline_loss": orichalcum_rescue["initial_loss"],
                    "planned_loss": orichalcum_rescue["final_loss"],
                    "planned_sequence": [
                        {
                            "card_id": getattr(candidate.card, "card_id", None),
                            "card_uuid": getattr(candidate.card, "uuid", None),
                            "target_key": None,
                        }
                        for candidate in plan
                    ],
                    "search": dict(getattr(self, "_last_search", {})),
                }
                return self._play_card_action(game, defensive.card)
            if total_loss > 0:
                # Re-read the authoritative frame at the fallback boundary.
                # The planner can be called after a previous card has already
                # changed player Block; carrying the outer turn's loss here
                # makes a basic Defend look useful even when current Block
                # already covers the entire incoming hit (and can trigger
                # Chosen's Hex for no survival gain).
                current_outcome = combat_predictor.projected_turn_outcome(
                    game
                )
                current_total_loss = int(
                    current_outcome.total_hp_loss or 0
                )
                energy_now = max(
                    0, int(getattr(game.player, "energy", 0) or 0)
                )
                baseline_loss = current_total_loss
                avoidable = []
                seen_cards = set()
                for group in groups:
                    for candidate in group:
                        card = candidate.card
                        if id(card) in seen_cards:
                            continue
                        seen_cards.add(id(card))
                        cost = combat_predictor.card_energy_cost(game, card)
                        extra_block = max(
                            0, int(getattr(candidate, "block_gain", 0) or 0)
                        )
                        reactive_hp_cost = max(
                            0,
                            int(getattr(candidate, "reactive_hp_cost", 0) or 0),
                        )
                        voluntary_hp_cost = max(
                            0,
                            int(getattr(candidate, "self_hp_cost", 0) or 0)
                            - reactive_hp_cost,
                        )
                        current_block = max(
                            0, int(getattr(game.player, "block", 0) or 0)
                        )
                        post_reactive_block = max(
                            0,
                            int(
                                getattr(
                                    candidate, "post_reactive_block", 0
                                ) or 0
                            ),
                        )
                        net_extra_block = max(
                            0, post_reactive_block - current_block
                        )
                        if (
                            cost > energy_now
                            or extra_block <= 0
                            or net_extra_block <= 0
                            or voluntary_hp_cost > 0
                            or reactive_hp_cost >= max(
                                1,
                                int(
                                    getattr(game.player, "current_hp", 1) or 1
                                ),
                            )
                            or not self._writhing_mass_fallback_attack_is_safe(
                                game,
                                card,
                                candidate.target,
                                extra_block=net_extra_block,
                            )
                        ):
                            continue
                        projected = combat_predictor.projected_turn_outcome(
                            game, extra_block=net_extra_block
                        ).total_hp_loss
                        projected_total = int(projected) + reactive_hp_cost
                        reduction = baseline_loss - projected_total
                        current_hp = max(
                            1,
                            int(
                                getattr(game.player, "current_hp", 1) or 1
                            ),
                        )
                        continuation_value = bool(
                            int(getattr(candidate, "hand_additions", 0) or 0)
                            > 0
                            or int(getattr(candidate, "healing_gain", 0) or 0)
                            > 0
                        )
                        if (
                            beat_source
                            and projected_total >= current_hp
                            and not continuation_value
                        ):
                            # Preserve the established Heart boundary for a
                            # pure block card which still cannot survive. A
                            # draw/heal card is different: its net mitigation
                            # buys a real continuation that must be resolved on
                            # the next authoritative frame.
                            continue
                        if reduction > 0 and self._fallback_respects_lifecycle(candidate):
                            avoidable.append(
                                (
                                    reduction,
                                    -cost,
                                    net_extra_block,
                                    candidate,
                                    projected_total,
                                )
                            )
                if avoidable:
                    (
                        loss_reduction,
                        _,
                        _,
                        defensive,
                        net_projected_loss,
                    ) = max(avoidable, key=lambda item: item[:3])
                    self.last_decision = {
                        "reason": "prevent_avoidable_hp_loss",
                        "plan_score": round(score, 3),
                        "card_id": getattr(defensive.card, "card_id", None),
                        "target_key": (
                            list(_monster_key(defensive.target))
                            if defensive.target is not None else None
                        ),
                        "incoming": incoming,
                        "projected_attack_hp_loss": attack_loss,
                        "projected_end_turn_hp_loss": end_turn_loss,
                        "projected_hp_loss": total_loss,
                        "card_mitigation": defensive.mitigation,
                        "card_intrinsic_mitigation": defensive.intrinsic_mitigation,
                        "card_block_gain": defensive.block_gain,
                        "card_self_hp_cost": defensive.self_hp_cost,
                        "card_reactive_hp_cost": defensive.reactive_hp_cost,
                        "card_post_reactive_block": (
                            defensive.post_reactive_block
                        ),
                        "net_projected_hp_loss": net_projected_loss,
                        "net_loss_reduction": loss_reduction,
                        "lifecycle": self._lifecycle_trace(defensive),
                        "search": dict(getattr(self, "_last_single_card_search", {}).get(
                            id(defensive), (getattr(self, "_last_search", {}), 0.0),
                        )[0]),
                    }
                    if defensive.target is not None:
                        return self._play_card_action(
                            game, defensive.card, defensive.target
                        )
                    return self._play_card_action(game, defensive.card)
            fallback = getattr(self, "_last_verified_fallback", None)
            if fallback is not None:
                current_outcome = combat_predictor.projected_turn_outcome(
                    game
                )
                current_total_loss = int(
                    current_outcome.total_hp_loss or 0
                )
                fallback_id = _token(getattr(fallback.card, "card_id", ""))
                pure_block_fallback = fallback_id in {
                    "defend", "defendg", "defendb", "defendr",
                    "deflect", "survivor",
                }
                if pure_block_fallback:
                    fallback_loss = int(
                        combat_predictor.projected_turn_outcome(
                            game,
                            extra_block=max(
                                0,
                                int(getattr(fallback, "block_gain", 0) or 0),
                            ),
                        ).total_hp_loss
                        or 0
                    )
                    # A pure block card is not a positive fallback when the
                    # live frame is already safe.  In particular this prevents
                    # an extra Defend from firing Hex after Deflect has
                    # covered Chosen's attack.
                    if current_total_loss <= 0 or fallback_loss >= current_total_loss:
                        fallback = None
            if fallback is not None:
                if fallback.target is not None and (fallback.damage > 0 or fallback.base_score > 2):
                    self.focus_key = _monster_key(fallback.target)
                self.last_decision = {
                    "reason": "prevent_avoidable_hp_loss",
                    "plan_score": round(score, 3),
                    "card_id": getattr(fallback.card, "card_id", None),
                    "target_key": list(_monster_key(fallback.target)) if fallback.target is not None else None,
                    "focus_key": list(self.focus_key) if self.focus_key is not None else None,
                    "incoming": incoming,
                    "projected_attack_hp_loss": attack_loss,
                    "projected_end_turn_hp_loss": end_turn_loss,
                    "projected_hp_loss": total_loss,
                    "card_mitigation": fallback.mitigation,
                    "card_intrinsic_mitigation": fallback.intrinsic_mitigation,
                    "card_end_turn_relief": fallback.end_turn_relief,
                    "card_self_hp_cost": fallback.self_hp_cost,
                    "card_damage": fallback.damage,
                    "search": dict(
                        getattr(
                            self, "_last_verified_fallback_search", {}
                        )
                    ),
                }
                if fallback.target is not None:
                    return self._play_card_action(
                        game, fallback.card, fallback.target
                    )
                return self._play_card_action(game, fallback.card)
            redraw_rescue = self._calculated_gamble_rescue(
                game, playable, total_loss
            )
            if redraw_rescue is not None:
                redraw_card, redraw_analysis = redraw_rescue
                redraw_search = dict(
                    redraw_analysis.get("continuation_search", {}) or {}
                )
                redraw_search["actual_loss"] = int(
                    redraw_analysis["actual_loss"]
                )
                self.last_decision = {
                    "reason": "emergency_redraw_survival",
                    "plan_score": round(score, 3),
                    "card_id": getattr(redraw_card, "card_id", None),
                    "incoming": incoming,
                    "projected_attack_hp_loss": attack_loss,
                    "projected_end_turn_hp_loss": end_turn_loss,
                    "projected_hp_loss": total_loss,
                    "risk_budget": risk_budget,
                    "redraw_count": redraw_analysis["redraw_count"],
                    "redraw_card_ids": redraw_analysis["drawn_card_ids"],
                    "redraw_baseline_loss": redraw_analysis["baseline_loss"],
                    "redraw_candidate_loss": redraw_analysis["actual_loss"],
                    "redraw_continuation_reason": redraw_analysis[
                        "continuation_reason"
                    ],
                    "redraw_continuation_card_id": redraw_analysis[
                        "continuation_card_id"
                    ],
                    "planned_sequence": [{
                        "card_id": getattr(redraw_card, "card_id", None),
                        "card_uuid": getattr(redraw_card, "uuid", None),
                        "target_key": None,
                    }],
                    "search": redraw_search,
                }
                return self._play_card_action(game, redraw_card)
            draw_replan = self._doomed_turn_draw_replan(game, groups)
            if draw_replan is not None:
                draw_candidate, draw_search = draw_replan
                self.last_decision = {
                    "reason": "doomed_turn_draw_replan",
                    "card_id": getattr(draw_candidate.card, "card_id", None),
                    "target_key": list(_monster_key(draw_candidate.target))
                    if draw_candidate.target is not None else None,
                    "projected_hp_loss": total_loss,
                    "draw_continuation_unproven": True,
                    "search": draw_search,
                }
                return self._play_card_action(
                    game, draw_candidate.card, draw_candidate.target
                )
            resource_rescue = self._resource_generation_rescue(
                game, playable, total_loss, risk_budget
            )
            if resource_rescue is not None:
                resource_card, resource_target, resource_hp_cost = (
                    resource_rescue
                )
                self.last_decision = {
                    "reason": "emergency_resource_rescue",
                    "plan_score": round(score, 3),
                    "card_id": getattr(resource_card, "card_id", None),
                    "target_key": (
                        list(_monster_key(resource_target))
                        if resource_target is not None else None
                    ),
                    "resource_kind": (
                        "energy"
                        if self._energy_gain(resource_card, game) > 0
                        else "draw"
                    ),
                    "energy_gain": int(
                        self._energy_gain(resource_card, game) or 0
                    ),
                    "draw_count": int(
                        self._card_draw_count(
                            resource_card,
                            game,
                            hand_size_before_play=len(hand),
                        )
                    ),
                    "card_self_hp_cost": int(resource_hp_cost),
                    "incoming": incoming,
                    "projected_attack_hp_loss": attack_loss,
                    "projected_end_turn_hp_loss": end_turn_loss,
                    "projected_hp_loss": total_loss,
                    "risk_budget": risk_budget,
                    "search": dict(getattr(self, "_last_search", {})),
                }
                return self._play_card_action(
                    game, resource_card, resource_target
                )
            reactive_progress = self._reactive_progress_fallback(
                game, groups, total_loss
            )
            if reactive_progress is not None:
                reactive, reactive_details, nominal_reactive, added_loss = (
                    reactive_progress
                )
                if reactive.target is not None:
                    self.focus_key = _monster_key(reactive.target)
                reactive_search = dict(getattr(self, "_last_search", {}))
                reactive_search.update(reactive_details)
                self.last_decision = {
                    "reason": "reactive_progress_fallback",
                    "plan_score": round(score, 3),
                    "card_id": getattr(reactive.card, "card_id", None),
                    "target_key": list(_monster_key(reactive.target))
                    if reactive.target is not None else None,
                    "focus_key": list(self.focus_key)
                    if self.focus_key is not None else None,
                    "incoming": incoming,
                    "projected_attack_hp_loss": attack_loss,
                    "projected_end_turn_hp_loss": end_turn_loss,
                    "projected_hp_loss": total_loss,
                    "risk_budget": risk_budget,
                    "card_mitigation": reactive.mitigation,
                    "card_intrinsic_mitigation": reactive.intrinsic_mitigation,
                    "card_block_gain": reactive.block_gain,
                    "card_self_hp_cost": reactive.self_hp_cost,
                    "card_damage": reactive.damage,
                    "reactive_nominal_damage": nominal_reactive,
                    "reactive_added_hp_loss": added_loss,
                    "planned_sequence": [{
                        "card_id": getattr(reactive.card, "card_id", None),
                        "card_uuid": getattr(reactive.card, "uuid", None),
                        "target_key": list(_monster_key(reactive.target))
                        if reactive.target is not None else None,
                    }],
                    "search": reactive_search,
                }
                self._remember_reactive_progress(game, reactive)
                if reactive.target is not None:
                    return self._play_card_action(
                        game, reactive.card, reactive.target
                    )
                return self._play_card_action(game, reactive.card)
            corrupted_cycle = self._safe_corrupted_block_cycle_candidate(
                game, groups
            )
            if corrupted_cycle is not None:
                cycle, cycle_details = corrupted_cycle
                self.last_decision = {
                    "reason": "safe_corrupted_block_cycle",
                    "plan_score": round(score, 3),
                    "card_id": getattr(cycle.card, "card_id", None),
                    "incoming": incoming,
                    "projected_attack_hp_loss": attack_loss,
                    "projected_end_turn_hp_loss": end_turn_loss,
                    "projected_hp_loss": total_loss,
                    "corrupted_cycle": cycle_details,
                    "search": dict(
                        getattr(self, "_last_single_card_search", {}).get(
                            id(cycle),
                            ({}, 0.0),
                        )[0]
                    ),
                }
                return self._play_card_action(game, cycle.card)
            cleanup = self._status_cleanup_candidate(game, groups)
            if cleanup is not None:
                self.last_decision = {
                    "reason": "exhaust_dead_status",
                    "plan_score": round(score, 3),
                    "card_id": getattr(cleanup.card, "card_id", None),
                    "incoming": incoming,
                    "projected_attack_hp_loss": attack_loss,
                    "projected_end_turn_hp_loss": end_turn_loss,
                    "projected_hp_loss": total_loss,
                    "search": dict(
                        getattr(self, "_last_single_card_search", {}).get(
                            id(cleanup),
                            ({}, 0.0),
                        )[0]
                    ),
                }
                return self._play_card_action(game, cleanup.card)
            self.last_decision = {
                "reason": "no_positive_marginal_action",
                "plan_score": round(score, 3),
                "incoming": incoming,
                "projected_attack_hp_loss": attack_loss,
                "projected_end_turn_hp_loss": end_turn_loss,
                "projected_hp_loss": total_loss,
                "rejected_lifecycle": lifecycle_context,
                "lifecycle_fallback_rejections": list(
                    self._last_lifecycle_fallback_rejections.values()
                ),
                "true_grit_damage_reserve_guard": (
                    true_grit_damage_reserve_guard
                ),
                "reason_detail": (
                    "all_affordable_candidates_failed_the_survival_or_risk_budget"
                ),
                # Preserve the no-play branch's complete terminal forecast so
                # trace auditing can account for deterministic orb/poison/
                # Combust packets that resolve after END.
                "search": dict(getattr(self, "_last_initial_search", {})),
            }
            return EndTurnAction()

        stochastic_poison_setup = self._multi_enemy_flask_setup_override(
            game, groups, plan
        )
        stochastic_poison_search = None
        if stochastic_poison_setup is not None:
            # Do not bind Catalyst behind a stochastic Flask result.  The
            # next authoritative frame contains the real random poison
            # distribution and will select the correct target (or save the
            # exhausting payoff if no target became worthwhile).
            plan = (stochastic_poison_setup,)
            stochastic_poison_search = dict(
                getattr(self, "_last_single_card_search", {}).get(
                    id(stochastic_poison_setup),
                    ({}, 0.0),
                )[0]
            )

        choker_draw_accelerator = None
        choker_deferred_sequence = []
        if (
            stochastic_poison_setup is None
            and choker_remaining is not None
            and len(plan) >= max(2, choker_remaining)
        ):
            # The bounded beam accounts for draw as expected utility, but it
            # cannot insert the concrete cards which the live game will draw
            # into the current branch.  With Velvet Choker that abstraction
            # created a destructive ordering: low-value attacks consumed
            # early slots, Adrenaline was played fifth, and its newly exposed
            # Catalyst package could no longer fit under the six-card cap.
            # If the selected full-cap line already contains a lossless
            # draw+energy accelerator, resolve it before non-draw cards and
            # re-plan from the authoritative post-draw frame.
            for index, candidate in enumerate(plan[1:], start=1):
                card_id = _token(getattr(candidate.card, "card_id", ""))
                if card_id != "adrenaline":
                    continue
                draw_count = int(
                    self._card_draw_count(
                        candidate.card,
                        game,
                        hand_size_before_play=len(hand),
                    )
                    or 0
                )
                energy_gain = int(
                    self._energy_gain(candidate.card, game) or 0
                )
                free_hand_slots_after_play = max(0, 11 - len(hand))
                earlier_are_non_draw = all(
                    int(
                        self._card_draw_count(
                            earlier.card,
                            game,
                            hand_size_before_play=len(hand),
                        )
                        or 0
                    ) <= 0
                    and int(getattr(earlier, "hand_additions", 0) or 0) <= 0
                    for earlier in plan[:index]
                )
                if (
                    draw_count <= 0
                    or energy_gain <= 0
                    or draw_count > free_hand_slots_after_play
                    or int(getattr(candidate, "self_hp_cost", 0) or 0) > 0
                    or int(getattr(candidate, "reactive_hp_cost", 0) or 0) > 0
                    or not earlier_are_non_draw
                ):
                    continue
                choker_draw_accelerator = candidate
                choker_deferred_sequence = [
                    getattr(planned.card, "card_id", None)
                    for planned in plan
                ]
                plan = (candidate,)
                self._clear_terminal_plan()
                break

        armaments_upgrade_setup = None
        armaments_deferred_sequence = []
        if choker_draw_accelerator is None and len(plan) >= 2:
            for index, candidate in enumerate(plan[1:], start=1):
                if (
                    _token(getattr(candidate.card, "card_id", ""))
                    != "armaments"
                    or int(getattr(candidate.card, "upgrades", 0) or 0) <= 0
                ):
                    continue
                earlier = plan[:index]
                if any(
                    self._card_draw_count(
                        item.card,
                        game,
                        hand_size_before_play=len(hand),
                    ) > 0
                    or int(getattr(item, "hand_additions", 0) or 0) > 0
                    or _token(getattr(item.card, "card_id", ""))
                    == "apotheosis"
                    for item in earlier
                ):
                    # Burning Pact/Battle Trance first expands the set which
                    # Armaments+ upgrades.  Re-plan after that draw instead.
                    continue
                upgrades_a_downstream_card = any(
                    int(getattr(item.card, "upgrades", 0) or 0) <= 0
                    and getattr(item.card, "type", None)
                    not in {CardType.CURSE, CardType.STATUS}
                    for item in earlier
                )
                if (
                    not upgrades_a_downstream_card
                    or int(getattr(candidate, "self_hp_cost", 0) or 0) > 0
                    or int(getattr(candidate, "reactive_hp_cost", 0) or 0) > 0
                ):
                    continue
                armaments_upgrade_setup = candidate
                armaments_deferred_sequence = [
                    getattr(planned.card, "card_id", None)
                    for planned in plan
                ]
                # Card numbers change authoritatively after Armaments+.
                # Execute only the setup, then score the upgraded hand.
                plan = (candidate,)
                self._clear_terminal_plan()
                break

        planned_attack_mitigation = self._plan_attack_mitigation(
            game,
            plan,
            attack_loss,
        )
        planned_end_relief = min(
            end_turn_loss,
            sum(max(0, candidate.end_turn_relief) for candidate in plan),
        )
        planned_mitigation = planned_attack_mitigation + planned_end_relief
        exact_projected_loss = int(
            (
                stochastic_poison_search
                or getattr(self, "_last_search", {})
                or {}
            ).get(
                "projected_loss", total_loss
            )
            or 0
        )
        planned_mitigation = max(
            planned_mitigation,
            max(0, int(total_loss or 0) - exact_projected_loss),
        )
        planned_orichalcum_block = sum(
            max(
                0,
                int(
                    getattr(candidate, "orichalcum_block_gain", 0) or 0
                ),
            )
            for candidate in plan
        )
        mitigation_needed = max(0, total_loss - risk_budget)
        # The search evaluated this exact order. Executing a later high-score
        # card here recreated the old Catalyst-before-poison and attack-before-
        # vulnerable bugs, so the first transition is authoritative.
        chosen = plan[0]
        setup_override = None
        zero_loss_progress_override = False
        authoritative_current_loss = int(
            combat_predictor.projected_turn_outcome(game).total_hp_loss
        )
        if authoritative_current_loss <= 0:
            # Do not spend the first action on a pure block card when an
            # affordable Vulnerable attack can establish next-turn damage.
            # The ordered terminal may still rank ``Shrug -> Bash`` above
            # ``Bash -> END`` because the former carries unused block; this
            # narrow prefix correction makes the tactical intent explicit and
            # lets the authoritative next frame re-plan the remainder.
            block_only_ids = {
                "defend", "defendr", "defendg", "defendb", "shrugitoff",
                "dodgeandroll", "backflip", "survivor", "deflect",
                "legssweep",
            }
            chosen_id = _token(getattr(chosen.card, "card_id", ""))
            chosen_is_pure_block = (
                chosen_id in block_only_ids
                and int(getattr(chosen, "damage", 0) or 0) <= 0
                and int(getattr(chosen, "block_gain", 0) or 0) > 0
            )
            if chosen_is_pure_block:
                current_block = int(getattr(game.player, "block", 0) or 0)
                calipers_gain = 0
                if self._has_relic(game, "Calipers"):
                    calipers_gain = max(
                        0,
                        int(
                            combat_predictor.calipers_retained_block(
                                game,
                                current_block
                                + int(getattr(chosen, "block_gain", 0) or 0),
                            )
                        )
                        - int(
                            combat_predictor.calipers_retained_block(
                                game, current_block
                            )
                        ),
                    )
                non_block_value = (
                    float(getattr(chosen, "non_block_utility", 0.0) or 0.0)
                    + float(getattr(chosen, "damage", 0) or 0)
                    + float(getattr(chosen, "end_turn_relief", 0) or 0)
                    + float(getattr(chosen, "future_turn_weak_value", 0.0) or 0.0)
                    + float(getattr(chosen, "future_turn_block_value", 0.0) or 0.0)
                    + float(getattr(chosen, "future_turn_energy_value", 0.0) or 0.0)
                    + float(getattr(chosen, "healing_gain", 0) or 0)
                    + float(getattr(chosen, "hand_additions", 0) or 0)
                )
                search_snapshot = dict(getattr(self, "_last_search", {}))
                expected_first_action_loss = search_snapshot.get(
                    "first_action_expected_enemy_hp_loss"
                )
                if expected_first_action_loss is None:
                    expected_first_action_loss = search_snapshot.get(
                        "first_action_enemy_hp_loss", 0
                    )
                first_action_enemy_hp_loss = max(
                    0,
                    int(expected_first_action_loss or 0),
                )
                # A base Defend can still be an offensive action through
                # A Thousand Cuts or Letter Opener.  It can also be the
                # necessary prefix that absorbs a later Thorns/Sharp Hide
                # packet or powers Body Slam.  These are real non-block
                # consumers and must survive the redundant-block guard.
                reactive_followup_requires_block = any(
                    int(getattr(later, "reactive_hp_cost", 0) or 0) > 0
                    for later in plan[1:]
                )
                block_enables_followup_damage = any(
                    _token(getattr(later.card, "card_id", ""))
                    == "bodyslam"
                    for later in plan[1:]
                )
                non_block_value += first_action_enemy_hp_loss
                if (
                    reactive_followup_requires_block
                    or block_enables_followup_damage
                ):
                    non_block_value += 1.0
                if non_block_value <= 0 and calipers_gain <= 0:
                    current_energy = max(
                        0, int(getattr(game.player, "energy", 0) or 0)
                    )
                    safe_downstream_attack = next((
                        later for later in plan[1:]
                        if getattr(later.card, "type", None)
                        == CardType.ATTACK
                        and int(getattr(later, "damage", 0) or 0) > 0
                        and int(getattr(later, "self_hp_cost", 0) or 0) <= 0
                        and int(
                            getattr(later, "reactive_hp_cost", 0) or 0
                        ) <= 0
                        and combat_predictor.card_energy_cost(
                            game, later.card
                        ) <= current_energy
                        and self._writhing_mass_fallback_attack_is_safe(
                            game,
                            later.card,
                            later.target,
                            extra_block=later.block_gain,
                        )
                    ), None)
                    if safe_downstream_attack is not None:
                        setup_override = safe_downstream_attack
                        zero_loss_progress_override = True
                        chosen = setup_override
                        plan = (chosen,)
                    else:
                        self._clear_terminal_plan()
                        self.last_decision = {
                            "reason": "no_positive_marginal_action",
                            "plan_score": round(score, 3),
                            "incoming": incoming,
                            "projected_attack_hp_loss": attack_loss,
                            "projected_end_turn_hp_loss": end_turn_loss,
                            "projected_hp_loss": total_loss,
                            "authoritative_current_hp_loss": (
                                authoritative_current_loss
                            ),
                            "card_id": getattr(
                                chosen.card, "card_id", None
                            ),
                            "card_block_gain": int(
                                getattr(chosen, "block_gain", 0) or 0
                            ),
                            "calipers_retained_block_gain": int(
                                calipers_gain
                            ),
                            "search": search_snapshot,
                        }
                        return EndTurnAction()
                affordable_setup = []
                current_energy = max(
                    0, int(getattr(game.player, "energy", 0) or 0)
                )
                for group in groups:
                    for candidate in group:
                        card_id = _token(
                            getattr(candidate.card, "card_id", "")
                        )
                        if card_id not in self.VULNERABLE_CARDS:
                            continue
                        if candidate.target is None or candidate.damage <= 0:
                            continue
                        if candidate.self_hp_cost > 0:
                            continue
                        if (
                            combat_predictor.power_amount(
                                candidate.target, "Artifact"
                            ) > 0
                            or combat_predictor.power_amount(
                                candidate.target, "Vulnerable"
                            ) > 0
                        ):
                            continue
                        if combat_predictor.card_energy_cost(
                            game, candidate.card
                        ) > current_energy:
                            continue
                        if not self._writhing_mass_fallback_attack_is_safe(
                            game,
                            candidate.card,
                            candidate.target,
                            extra_block=candidate.block_gain,
                        ):
                            continue
                        affordable_setup.append(candidate)
                if affordable_setup and not zero_loss_progress_override:
                    setup_override = max(
                        affordable_setup,
                        key=lambda candidate: (
                            candidate.damage,
                            candidate.base_score,
                            -combat_predictor.card_energy_cost(
                                game, candidate.card
                            ),
                        ),
                    )
                    chosen = setup_override
                    plan = (chosen,)
        reported_self_hp_cost = chosen.self_hp_cost
        extra_resolution_copies = (
            int(self._echo_form_duplicates_first_card(game))
            + int(
                getattr(chosen.card, "type", None) == CardType.ATTACK
                and combat_predictor.power_amount(
                    game.player, "Double Tap", "DoubleTapPower"
                ) > 0
            )
            + int(
                getattr(chosen.card, "type", None) == CardType.SKILL
                and combat_predictor.power_amount(
                    game.player, "Burst", "BurstPower"
                ) > 0
            )
            + int(
                getattr(chosen.card, "type", None)
                in {CardType.ATTACK, CardType.SKILL, CardType.POWER}
                and combat_predictor.power_amount(
                    game.player, "Duplication", "DuplicationPower"
                ) > 0
            )
        )
        if extra_resolution_copies > 0:
            # Candidate construction prices one resolution.  The exact beam
            # transition charges the duplicate's event sequence above; expose
            # a conservative per-action cost in the audit trace as well.  The
            # repeated events may consume block/Buffer, so their raw sum is a
            # safe upper bound for the duplicate rather than a stale copy of
            # the first-resolution HP loss.
            reported_self_hp_cost += extra_resolution_copies * sum(
                max(0, int(amount or 0))
                for amount in (
                    tuple(chosen.self_damage_events)
                    + tuple(chosen.thorns_damage_events)
                    + tuple(chosen.sharp_hide_damage_events)
                )
            )

        if self._should_update_damage_focus(chosen):
            self.focus_key = _monster_key(chosen.target)
        future_turn_weak_value = float(
            getattr(chosen, "future_turn_weak_value", 0.0) or 0.0
        )
        future_turn_block_value = float(
            getattr(chosen, "future_turn_block_value", 0.0) or 0.0
        )
        future_turn_energy_value = float(
            getattr(chosen, "future_turn_energy_value", 0.0) or 0.0
        )
        chosen_token = _token(getattr(chosen.card, "card_id", ""))
        chosen_target_key = (
            _monster_key(chosen.target) if chosen.target is not None else None
        )
        vulnerable_consumers = 0
        if chosen_token in self.VULNERABLE_CARDS:
            for later in plan[1:]:
                if (
                    getattr(later.card, "type", None) != CardType.ATTACK
                    or int(getattr(later, "damage", 0) or 0) <= 0
                ):
                    continue
                if (
                    chosen_target_key is None
                    or (
                        later.target is not None
                        and _monster_key(later.target) == chosen_target_key
                    )
                ):
                    vulnerable_consumers += 1
        self.last_decision = {
            "reason": (
                "armaments_handwide_upgrade_first"
                if armaments_upgrade_setup is not None
                else "velvet_choker_draw_accelerator_first"
                if choker_draw_accelerator is not None
                else "future_turn_energy_setup"
                if future_turn_energy_value > 0 and total_loss <= 0
                else "future_turn_defense_setup"
                if (
                    future_turn_weak_value > 0
                    or future_turn_block_value > 0
                ) and total_loss <= 0
                else (
                    "stochastic_poison_setup_before_catalyst"
                    if stochastic_poison_setup is not None
                    else (
                        "no_incoming_vulnerable_setup"
                        if setup_override is not None
                        and not zero_loss_progress_override
                        else "zero_loss_attack_progress"
                        if zero_loss_progress_override
                        else "ordered_turn_search"
                    )
                )
            ),
            "plan_score": round(score, 3),
            "card_id": getattr(chosen.card, "card_id", None),
            "target_key": list(_monster_key(chosen.target)) if chosen.target is not None else None,
            "focus_key": list(self.focus_key) if self.focus_key is not None else None,
            "incoming": incoming,
            "projected_attack_hp_loss": attack_loss,
            "projected_end_turn_hp_loss": end_turn_loss,
            "projected_hp_loss": total_loss,
            "risk_budget": risk_budget,
            "planned_mitigation": planned_mitigation,
            "card_mitigation": chosen.mitigation,
            "card_intrinsic_mitigation": chosen.intrinsic_mitigation,
            "card_block_gain": chosen.block_gain,
            "card_end_turn_relief": chosen.end_turn_relief,
            "card_self_hp_cost": reported_self_hp_cost,
            "card_damage": chosen.damage,
            "future_turn_weak_value": round(future_turn_weak_value, 3),
            "future_turn_block_value": round(future_turn_block_value, 3),
            "future_turn_energy_value": round(future_turn_energy_value, 3),
            "vulnerable_opener": bool(vulnerable_consumers),
            "vulnerable_source": (
                getattr(chosen.card, "card_id", None)
                if vulnerable_consumers
                else None
            ),
            "vulnerable_consumers": vulnerable_consumers,
            "choker_remaining": choker_remaining,
            "choker_deferred_sequence": choker_deferred_sequence,
            "armaments_deferred_sequence": armaments_deferred_sequence,
            "lifecycle": self._lifecycle_trace(chosen),
            "true_grit_damage_reserve_guard": (
                true_grit_damage_reserve_guard
            ),
            "champ_transition": chosen.champ_transition,
            "champ_transition_ready": chosen.champ_transition_ready,
            "champ_transition_penalized": chosen.champ_transition_penalized,
            "slime_transition": chosen.slime_transition,
            "slime_transition_penalized": chosen.slime_transition_penalized,
            "planned_sequence": [
                {
                    "card_id": getattr(candidate.card, "card_id", None),
                    "card_uuid": getattr(candidate.card, "uuid", None),
                    "target_key": list(_monster_key(candidate.target))
                    if candidate.target is not None else None,
                }
                for candidate in plan
            ],
            "search": dict(
                stochastic_poison_search
                or getattr(self, "_last_search", {})
            ),
        }
        self._arm_terminal_plan(
            game,
            plan,
            self.last_decision.get("search") or {},
        )
        if self._terminal_plan:
            self._terminal_plan_selected_uuid = getattr(
                chosen.card, "uuid", None
            )
        if chosen.target is not None:
            return self._play_card_action(game, chosen.card, chosen.target)
        return self._play_card_action(game, chosen.card)
