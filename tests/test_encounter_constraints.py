import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.communication.action import EndTurnAction, PlayCardAction
from spirecomm.spire.card import Card, CardRarity, CardType
from spirecomm.spire.character import Intent, Monster, Orb, Player
from spirecomm.spire.power import Power


class PrioritiesStub:
    PLAY_PRIORITIES = {}

    @staticmethod
    def is_card_defensive(card):
        return int(getattr(card, "block", 0) or 0) > 0


def monster(monster_id, hp=100, intent=Intent.BUFF, damage=0, hits=0, powers=None, index=0):
    value = Monster(
        monster_id, monster_id, hp, hp, 0, intent, False, False,
        move_adjusted_damage=damage, move_hits=hits,
    )
    value.monster_index = index
    value.powers = list(powers or [])
    return value


def attack(uuid="strike"):
    return Card(
        "Strike_G", "Strike", CardType.ATTACK, CardRarity.BASIC,
        cost=1, uuid=uuid, has_target=True, is_playable=True,
        damage=6, base_damage=6,
    )


class GameStub:
    def __init__(self, monsters, hand, hp=70, energy=3, act=3):
        self.monsters = monsters
        self.hand = hand
        self.player = Player(70, hp, block=0, energy=energy)
        self.player.powers = []
        self.player.orbs = []
        self.act = act
        self.floor = 50 if act == 3 else 55
        self.turn = 3
        self.room_type = "MonsterRoomBoss"
        self.relics = []


class EncounterConstraintTests(unittest.TestCase):
    def setUp(self):
        self.planner = FastCombatPlanner(PrioritiesStub())

    def test_time_eater_does_not_spend_twelfth_card_without_a_reason(self):
        eater = monster(
            "TimeEater",
            powers=[Power("Time Warp", "Time Warp", 11)],
        )
        game = GameStub([eater], [attack()], energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_heart_beat_of_death_rejects_nonlethal_card_that_kills_player(self):
        heart = monster(
            "CorruptHeart",
            hp=300,
            powers=[
                Power("BeatOfDeathPower", "Beat of Death", 2),
                Power("InvinciblePower", "Invincible", 200),
            ],
        )
        game = GameStub([heart], [attack()], hp=2, energy=1, act=4)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_heart_invincible_cap_prevents_false_lethal_bypass(self):
        heart = monster(
            "CorruptHeart",
            hp=5,
            powers=[
                Power("BeatOfDeathPower", "Beat of Death", 2),
                Power("InvinciblePower", "Invincible", 200),
            ],
        )
        game = GameStub([heart], [attack()], hp=2, energy=1, act=4)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_corpse_explosion_can_target_a_reserved_death_for_cascade(self):
        doomed = monster(
            "doomed",
            hp=5,
            powers=[Power("Poison", "Poison", 5)],
            index=0,
        )
        doomed.max_hp = 30
        active = monster("active", hp=25, index=1)
        corpse_explosion = Card(
            "Corpse Explosion", "Corpse Explosion", CardType.SKILL, CardRarity.RARE,
            cost=2, uuid="corpse-explosion", is_playable=True, has_target=True,
            magic_number=6,
        )
        game = GameStub([doomed, active], [corpse_explosion], hp=50, energy=2, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, doomed)

    def test_feed_can_claim_a_reserved_death_when_it_is_a_real_kill(self):
        doomed = monster(
            "doomed",
            hp=6,
            powers=[Power("Poison", "Poison", 6)],
        )
        feed = Card(
            "Feed", "Feed", CardType.ATTACK, CardRarity.RARE,
            cost=1, uuid="feed", is_playable=True, has_target=True,
            damage=10, base_damage=10,
        )
        game = GameStub([doomed], [feed], hp=50, energy=1, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, feed)
        self.assertIs(action.target_monster, doomed)

    def test_feed_cannot_target_reserved_death_when_damage_is_insufficient(self):
        doomed = monster(
            "doomed",
            hp=20,
            powers=[Power("Poison", "Poison", 20)],
            index=0,
        )
        active = monster("active", hp=40, index=1)
        feed = Card(
            "Feed", "Feed", CardType.ATTACK, CardRarity.RARE,
            cost=1, uuid="feed", is_playable=True, has_target=True,
            damage=10, base_damage=10,
        )
        game = GameStub([doomed, active], [feed], hp=50, energy=1, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, active)

    def test_ironclad_reaper_heals_before_all_reserved_deaths(self):
        doomed = monster(
            "doomed",
            hp=5,
            powers=[Power("Poison", "Poison", 5)],
        )
        reaper = Card(
            "Reaper", "Reaper", CardType.ATTACK, CardRarity.RARE,
            cost=2, uuid="reaper", is_playable=True, has_target=False,
            damage=4, base_damage=4,
        )
        game = GameStub([doomed], [reaper], hp=40, energy=2, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, reaper)

    def test_silent_alchemize_uses_empty_potion_slot_before_reserved_death(self):
        doomed = monster(
            "doomed",
            hp=5,
            powers=[Power("Poison", "Poison", 5)],
        )
        alchemize = Card(
            "Alchemize", "Alchemize", CardType.SKILL, CardRarity.RARE,
            cost=1, uuid="alchemize", is_playable=True, has_target=False,
        )
        game = GameStub([doomed], [alchemize], hp=50, energy=1, act=2)
        game.are_potions_full = lambda: False

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, alchemize)

    def test_defect_self_repair_plays_before_reserved_death(self):
        doomed = monster(
            "doomed",
            hp=5,
            powers=[Power("Poison", "Poison", 5)],
        )
        self_repair = Card(
            "Self Repair", "Self Repair", CardType.POWER, CardRarity.UNCOMMON,
            cost=1, uuid="self-repair", is_playable=True, has_target=False,
            magic_number=7,
        )
        game = GameStub([doomed], [self_repair], hp=40, energy=1, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, self_repair)

    def test_act_four_focuses_spear_before_equally_healthy_shield(self):
        shield = monster("SpireShield", hp=60, index=0)
        spear = monster("SpireSpear", hp=60, index=1)
        game = GameStub([shield, spear], [attack()], energy=1, act=4)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, spear)

    def test_defect_frost_channel_counts_as_current_turn_mitigation(self):
        attacker = monster("Cultist", hp=40, intent=Intent.ATTACK, damage=10, hits=1)
        glacier = Card(
            "Glacier", "Glacier", CardType.SKILL, CardRarity.UNCOMMON,
            cost=2, uuid="glacier", is_playable=True,
        )
        game = GameStub([attacker], [glacier, attack()], hp=5, energy=2, act=2)
        game.player.orbs = [
            Orb("Empty", "Empty", 0, 0),
            Orb("Empty", "Empty", 0, 0),
            Orb("Empty", "Empty", 0, 0),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, glacier)
        self.assertEqual(4, self.planner.last_decision["card_intrinsic_mitigation"])

    def test_vulnerable_setup_is_ordered_before_followup_attack(self):
        target = monster("Cultist", hp=40)
        trip = Card(
            "Trip", "Trip", CardType.SKILL, CardRarity.UNCOMMON,
            cost=0, uuid="trip", is_playable=True, has_target=True, magic_number=2,
        )
        followup = attack()
        game = GameStub([target], [followup, trip], hp=50, energy=1, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, trip)


if __name__ == "__main__":
    unittest.main()
