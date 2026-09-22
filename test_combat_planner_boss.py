import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.communication.action import PlayCardAction
from spirecomm.spire.card import Card, CardRarity, CardType
from spirecomm.spire.character import Intent, Monster, Player
from spirecomm.spire.power import Power


class PrioritiesStub:
    PLAY_PRIORITIES = {}

    @staticmethod
    def is_card_defensive(card):
        return int(getattr(card, "block", 0) or 0) > 0


class GameStub:
    def __init__(self, target, hand, hp=70, energy=1):
        self.act = 2
        self.floor = 33
        self.room_type = "MonsterRoomBoss"
        self.turn = 6
        self.monsters = [target]
        self.player = Player(70, hp, block=0, energy=energy)
        self.hand = list(hand)
        self.relics = []


def make_monster(monster_id, current_hp, max_hp=None, poison=0):
    target = Monster(
        name=monster_id,
        monster_id=monster_id,
        max_hp=max_hp or current_hp,
        current_hp=current_hp,
        block=0,
        intent=Intent.BUFF,
        half_dead=False,
        is_gone=False,
        move_adjusted_damage=0,
        move_hits=0,
    )
    if poison:
        target.powers = [Power("Poison", "Poison", poison)]
    return target


def catalyst(upgrades=1, magic_number=-1):
    return Card(
        "Catalyst",
        "Catalyst+" if upgrades else "Catalyst",
        CardType.SKILL,
        CardRarity.UNCOMMON,
        upgrades=upgrades,
        has_target=True,
        cost=1,
        uuid="catalyst",
        is_playable=True,
        exhausts=True,
        magic_number=magic_number,
    )


def deadly_poison(amount=5):
    return Card(
        "Deadly Poison", "Deadly Poison", CardType.SKILL, CardRarity.COMMON,
        cost=1, uuid="deadly-poison", is_playable=True, has_target=True,
        magic_number=amount,
    )


def strike(damage=6):
    return Card(
        "Strike_G",
        "Strike",
        CardType.ATTACK,
        CardRarity.BASIC,
        has_target=True,
        cost=1,
        uuid="strike",
        is_playable=True,
        damage=damage,
        base_damage=damage,
    )


def neutralize(damage=4):
    return Card(
        "Neutralize", "Neutralize", CardType.ATTACK, CardRarity.BASIC,
        has_target=True, cost=0, uuid="neutralize", is_playable=True,
        damage=damage, base_damage=damage, magic_number=1,
    )


def cleave(damage=6):
    return Card(
        "Cleave", "Cleave", CardType.ATTACK, CardRarity.COMMON,
        has_target=False, cost=1, uuid="cleave", is_playable=True,
        damage=damage, base_damage=damage,
    )


def thousand_cuts():
    return Card(
        "A Thousand Cuts",
        "A Thousand Cuts",
        CardType.POWER,
        CardRarity.RARE,
        cost=1,
        uuid="thousand-cuts",
        is_playable=True,
        magic_number=1,
    )


def footwork():
    return Card(
        "Footwork",
        "Footwork",
        CardType.POWER,
        CardRarity.UNCOMMON,
        cost=1,
        uuid="footwork",
        is_playable=True,
        magic_number=2,
    )


def wraith_form():
    return Card(
        "Wraith Form v2",
        "Wraith Form",
        CardType.POWER,
        CardRarity.RARE,
        cost=3,
        uuid="wraith-form",
        is_playable=True,
        magic_number=2,
    )


def demon_form():
    return Card(
        "Demon Form", "Demon Form+", CardType.POWER, CardRarity.RARE,
        upgrades=1, cost=3, uuid="demon-form", is_playable=True,
        magic_number=3,
    )


class BossAwareCombatPlannerTests(unittest.TestCase):
    def setUp(self):
        self.planner = FastCombatPlanner(PrioritiesStub())

    def test_upgraded_catalyst_uses_upgrade_when_magic_number_is_minus_one(self):
        catalyst_plus = catalyst(upgrades=1, magic_number=-1)
        basic_attack = strike(damage=9)
        target = make_monster("Cultist", current_hp=100, poison=7)
        game = GameStub(target, [catalyst_plus, basic_attack], energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        # Triple poison is worth 12.6 here and beats the nine-damage attack;
        # treating magic_number=-1 as a 2x Catalyst would choose the attack.
        self.assertIs(action.card, catalyst_plus)

    def test_poison_source_is_played_before_catalyst_in_same_hand(self):
        target = make_monster("Cultist", current_hp=100, poison=5)
        poison = deadly_poison(5)
        catalyst_plus = catalyst()
        game = GameStub(target, [catalyst_plus, poison], energy=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, poison)

    def test_thousand_cuts_is_played_before_guaranteed_attack_combo(self):
        target = make_monster("Cultist", current_hp=10)
        power = thousand_cuts()
        first = strike(damage=6)
        second = strike(damage=6)
        second.uuid = "strike-2"
        game = GameStub(target, [first, second, power], energy=3)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, power)
        self.assertEqual(
            "guaranteed_combo_setup_before_attacks",
            self.planner.last_decision.get("reason"),
        )

    def test_wraith_form_projects_immediate_intangible_on_lethal_turn(self):
        attacker = make_monster("Champ", current_hp=300, max_hp=420)
        attacker.intent = Intent.ATTACK
        attacker.move_adjusted_damage = 40
        attacker.move_hits = 1
        intangible = wraith_form()
        basic_attack = strike(damage=9)
        game = GameStub(attacker, [intangible, basic_attack], hp=20, energy=3)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, intangible)

    def test_stasis_orb_is_not_broken_before_demon_form_can_be_played(self):
        sealed_demon_form = demon_form()
        stasis_orb = make_monster("BronzeOrb", current_hp=7, max_hp=54)
        stasis_orb.intent = Intent.DEFEND
        stasis_orb.powers = [
            Power("Minion", "Minion", -1),
            Power("Stasis", "Stasis", -1, card=sealed_demon_form)
        ]
        automaton = make_monster("BronzeAutomaton", current_hp=241, max_hp=300)
        automaton.intent = Intent.DEFEND_BUFF
        automaton.block = 12
        automaton.powers = [
            Power("Artifact", "Artifact", 3),
            Power("Strength", "Strength", 3),
        ]
        attacking_orb = make_monster("BronzeOrb", current_hp=52, max_hp=52)
        attacking_orb.intent = Intent.ATTACK
        attacking_orb.move_adjusted_damage = 8
        attacking_orb.move_hits = 1
        attacking_orb.powers = [
            Power("Minion", "Minion", -1),
        ]
        for index, monster in enumerate(
            (stasis_orb, automaton, attacking_orb)
        ):
            monster.monster_index = index
        twin_strike = Card(
            "Twin Strike", "Twin Strike+", CardType.ATTACK,
            CardRarity.COMMON, has_target=True, cost=1,
            uuid="twin-strike", is_playable=True, damage=8,
            base_damage=5, magic_number=-1,
        )
        defend = Card(
            "Defend_R", "Defend+", CardType.SKILL, CardRarity.BASIC,
            upgrades=1, cost=1, uuid="defend", is_playable=True, block=8,
        )
        shrugs = [
            Card(
                "Shrug It Off", "Shrug It Off", CardType.SKILL,
                CardRarity.COMMON, cost=1, uuid=f"shrug-{index}",
                is_playable=True, block=8,
            )
            for index in range(3)
        ]
        game = GameStub(
            automaton,
            [shrugs[0], twin_strike, shrugs[1], shrugs[2], defend],
            hp=80,
            energy=3,
        )
        game.turn = 5
        game.monsters = [stasis_orb, automaton, attacking_orb]
        game.player.powers = [Power("Strength", "Strength", 3)]
        game.draw_pile = [strike(damage=9) for _ in range(9)]
        game.discard_pile = []
        game.exhaust_pile = []
        game.limbo = []
        self.planner.combat_key = self.planner._current_combat_key(game)
        self.planner.focus_key = ("bronzeorb", 0)

        self.planner.choose_card_action(game)

        decision = self.planner.last_decision
        self.assertGreater(decision["search"]["final_enemy_hp"][0], 0)
        self.assertEqual(
            0.0, decision["search"]["premature_stasis_release_cost"]
        )

        # With two extra energy, one Shrug can cover the live attack, Twin
        # Strike can break Stasis, and three energy remain for Demon Form.
        game.player.energy = 5
        self.planner.focus_key = ("bronzeorb", 0)
        self.planner.choose_card_action(game)
        release = self.planner.last_decision["search"]
        self.assertEqual(0, release["final_enemy_hp"][0])
        self.assertGreaterEqual(release["remaining_energy"], 3)
        self.assertEqual(0.0, release["premature_stasis_release_cost"])

        # The bridge refresh exposes the returned card; the normal boss
        # lifecycle model must then play it immediately.
        stasis_orb.current_hp = 0
        stasis_orb.is_gone = True
        attacking_orb.intent = Intent.DEFEND
        attacking_orb.move_adjusted_damage = 0
        game.hand = [sealed_demon_form]
        game.player.energy = 3
        action = self.planner.choose_card_action(game)
        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, sealed_demon_form)

    def test_low_poison_catalyst_is_saved_before_champ_half_health(self):
        catalyst_plus = catalyst()
        basic_attack = strike()
        champ = make_monster("Champ", current_hp=340, max_hp=420, poison=9)
        game = GameStub(champ, [catalyst_plus, basic_attack], hp=34, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, basic_attack)

    def test_meaningful_catalyst_burst_is_not_blanket_banned_before_half(self):
        catalyst_plus = catalyst()
        basic_attack = strike()
        champ = make_monster("TheChamp", current_hp=250, max_hp=420, poison=30)
        game = GameStub(champ, [catalyst_plus, basic_attack], hp=34, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, catalyst_plus)

    def test_champ_execute_estimate_does_not_keep_pre_transition_weak(self):
        champ = make_monster("Champ", current_hp=214, max_hp=420)
        champ.powers = [Power("Weak", "Weak", 3)]
        game = GameStub(champ, [strike()], hp=40, energy=1)

        # Anger cleanses Weak before the 2x16 Execute at A0.
        self.assertEqual(32, self.planner._champ_execute_estimate(game, champ))

    def test_champ_setup_is_played_before_unsafe_half_health_crossing(self):
        setup = footwork()
        basic_attack = strike()
        champ = make_monster("Champ", current_hp=214, max_hp=420)
        game = GameStub(champ, [basic_attack, setup], hp=25, energy=2)

        first_action = self.planner.choose_card_action(game)

        self.assertIsInstance(first_action, PlayCardAction)
        self.assertIs(first_action.card, setup)

        # Once the available preparation has been played, replanning permits
        # the necessary transition instead of stalling the combat forever.
        game.hand = [basic_attack]
        game.player.energy = 1
        second_action = self.planner.choose_card_action(game)
        self.assertIsInstance(second_action, PlayCardAction)
        self.assertIs(second_action.card, basic_attack)
        self.assertTrue(self.planner.last_decision["champ_transition"])
        self.assertFalse(self.planner.last_decision["champ_transition_ready"])
        self.assertFalse(self.planner.last_decision["champ_transition_penalized"])

    def test_slime_boss_spends_all_damage_before_split_action(self):
        slime = make_monster("SlimeBoss", current_hp=75, max_hp=140)
        crossing_attack = strike(damage=6)
        setup_damage = neutralize(damage=4)
        game = GameStub(slime, [crossing_attack, setup_damage], hp=50, energy=2)

        self.planner.choose_card_action(game)

        decision = self.planner.last_decision
        self.assertCountEqual(
            ["Strike_G", "Neutralize"],
            [entry["card_id"] for entry in decision["planned_sequence"]],
        )
        self.assertEqual([65], decision["search"]["final_enemy_hp"])
        self.assertEqual([0], decision["search"]["split_pending_enemy_indexes"])
        self.assertEqual(5, decision["search"]["inherited_hp_reduction"])

    def test_slime_boss_aoe_and_targeted_damage_both_lower_spawn_hp(self):
        slime = make_monster("SlimeBoss", current_hp=75, max_hp=140)
        crossing_aoe = cleave(damage=6)
        setup_damage = neutralize(damage=4)
        game = GameStub(slime, [crossing_aoe, setup_damage], hp=50, energy=2)

        self.planner.choose_card_action(game)

        decision = self.planner.last_decision
        self.assertCountEqual(
            ["Cleave", "Neutralize"],
            [entry["card_id"] for entry in decision["planned_sequence"]],
        )
        self.assertEqual([65], decision["search"]["final_enemy_hp"])
        self.assertEqual(130, decision["search"]["future_spawn_hp"])


if __name__ == "__main__":
    unittest.main()
