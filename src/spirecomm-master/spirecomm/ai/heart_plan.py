"""Deadline-aware Act 4 key plan shared by macro decisions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HeartPlan:
    active: bool
    act: int
    floor_in_act: int
    ruby_missing: bool
    emerald_missing: bool
    sapphire_missing: bool

    @classmethod
    def from_game(cls, game, goal_mode):
        active = str(goal_mode or "").upper() == "HEART"
        act = max(1, int(getattr(game, "act", 1) or 1))
        floor = max(0, int(getattr(game, "floor", 0) or 0))
        return cls(
            active=active,
            act=act,
            floor_in_act=floor % 17,
            ruby_missing=active and not bool(getattr(game, "has_ruby_key", False)),
            emerald_missing=active and not bool(getattr(game, "has_emerald_key", False)),
            sapphire_missing=active and not bool(getattr(game, "has_sapphire_key", False)),
        )

    def emerald_bonus(self, *, burning, healthy, ready, floor_in_act=None):
        if not self.emerald_missing or not burning:
            return 0.0
        projected_floor = (
            self.floor_in_act
            if floor_in_act is None
            else max(0, int(floor_in_act))
        )
        if self.act == 1:
            # A0 Act 1 is the preferred Emerald window.  Keep a substantial
            # *soft* route value for a healthy, still-developing deck: an
            # early fork can otherwise abandon the only burning elite before
            # one more card reward makes the deck ready.  This is deliberately
            # not a hard constraint, and unhealthy routes retain the small
            # postpone value.  Row 12 is floor 13 in the map protocol and is
            # still a normal Act 1 elite location.
            if healthy and 5 <= projected_floor <= 13:
                return 260.0 if ready else 125.0
            return 24.0
        if self.act == 2:
            # Act 2 is the route-preservation deadline.  A healthy developing
            # deck should still prefer the key branch after the survival gate
            # admits it; readiness changes confidence, not the objective.
            if healthy:
                return 245.0 if ready else 120.0
            return 35.0
        return 520.0

    def must_preserve_emerald_route(self):
        # Act 3 is too late to begin preserving reachability: an early fork
        # can permanently abandon the only burning elite before the planner's
        # former hard deadline activates.  Act 1 remains a health/readiness-
        # gated preference; Act 2 is the first route-preservation deadline.
        return self.emerald_missing and self.act >= 2

    def should_take_sapphire(self, linked_relic_score):
        if not self.sapphire_missing:
            return False
        if self.act >= 3:
            return True
        return (
            float(linked_relic_score)
            <= self.sapphire_opportunity_value()
        )

    def sapphire_opportunity_value(self):
        """Comparable key value before Sapphire becomes mandatory.

        Act 2 is the target window when its linked relic is ordinary because
        only the guaranteed Act 3 chest remains.  In Act 1 two guaranteed
        chests remain, so only an explicitly low-value relic should be traded
        for the key.  Act 3 is represented separately as a hard deadline.
        """

        if not self.sapphire_missing or self.act >= 3:
            return None
        return 30.0 if self.act == 2 else 4.0

    def should_recall(
        self,
        hp_ratio,
        best_alternative,
        can_recover,
        recovery_ratio=0.0,
        survival_ready=True,
    ):
        """Commit Ruby at the first low-cost window, not the last fire.

        ``recall_is_mandatory`` remains the literal deadline.  This method is
        the earlier scheduling policy: Act 2 is preferred when the deck is
        healthy and would waste most of a normal Rest, while Act 3 takes the
        first survivable window even if a later Rest may be useful.  A truly
        exceptional campfire action (for example removing Pain) may still be
        taken before the deadline.
        """

        if not self.ruby_missing:
            return False
        hp_ratio = float(hp_ratio)
        best_alternative = float(best_alternative)
        recovery_ratio = max(0.0, float(recovery_ratio))
        if self.recall_is_mandatory(hp_ratio=hp_ratio):
            return True
        if not survival_ready or best_alternative >= 30.0:
            return False
        if self.act == 2:
            return (
                hp_ratio >= 0.88
                and (not can_recover or recovery_ratio <= 0.12)
            )
        if self.act >= 3:
            return hp_ratio >= 0.68
        return False

    def act_one_recall_is_candidate(
        self,
        hp_ratio,
        can_recover,
        recovery_ratio=0.0,
        survival_ready=True,
    ):
        """Expose only genuinely low-cost Act 1 Recall windows to advice.

        Act 1 Recall remains optional rather than a deterministic commitment:
        its upgrade cost compounds for the rest of the run.  The model may
        compare it with the exact deck only when HP is nearly full and a Rest
        would recover almost nothing (or Rest is unavailable).
        """

        return (
            self.ruby_missing
            and self.act == 1
            and bool(survival_ready)
            and float(hp_ratio) >= 0.92
            and (
                not can_recover
                or max(0.0, float(recovery_ratio)) <= 0.08
            )
        )

    def recall_is_mandatory(self, floor_in_act=None, hp_ratio=None):
        """Whether a safe current window should be treated as the deadline.

        The literal last chance is the Act 3 final fire.  A healthy Act 2
        final fire is also made mandatory because postponing all Ruby cost to
        Act 3 repeatedly converted otherwise viable runs into no-Heart runs.
        Low-HP decks may still heal for the Act 2 boss and use Act 3.
        """

        projected_floor = (
            self.floor_in_act
            if floor_in_act is None
            else max(0, int(floor_in_act))
        )
        if not self.ruby_missing or projected_floor < 15:
            return False
        if self.act >= 3:
            return True
        return (
            self.act == 2
            and hp_ratio is not None
            and float(hp_ratio) >= 0.80
        )

    def recall_opportunity_value(self, floor_in_act=None, hp_ratio=None):
        """Comparable value for an optional Recall before its hard deadline."""

        if not self.ruby_missing:
            return -1000.0
        if self.act == 1:
            # A soft score keeps a safe Act 1 candidate within the model's
            # bounded regret window only when the competing action is modest.
            return 7.0
        projected_floor = (
            self.floor_in_act
            if floor_in_act is None
            else max(0, int(floor_in_act))
        )
        if self.recall_is_mandatory(projected_floor, hp_ratio=hp_ratio):
            return 1000.0
        if self.act == 2:
            return 9.0
        return 20.0 if projected_floor >= 9 else 11.0

    def snapshot(self):
        sapphire_mandatory = self.sapphire_missing and self.act >= 3
        return {
            "active": self.active,
            "act": self.act,
            "floor_in_act": self.floor_in_act,
            "missing": {
                "ruby": self.ruby_missing,
                "emerald": self.emerald_missing,
                "sapphire": self.sapphire_missing,
            },
            "emerald_deadline": 2 if self.emerald_missing else None,
            "sapphire_target_act": 2 if self.sapphire_missing and self.act <= 2 else 3,
            "sapphire_opportunity_value": self.sapphire_opportunity_value(),
            "sapphire_mandatory": sapphire_mandatory,
            "ruby_deadline_floor_in_act": 15 if self.ruby_missing else None,
        }
