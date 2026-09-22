import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai import combat_predictor
from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.ai.priorities import DefectPowerPriority, IroncladPriority, SilentPriority
from spirecomm.communication.action import EndTurnAction, PlayCardAction
from spirecomm.spire.card import Card, CardRarity, CardType
from spirecomm.spire.character import Intent, Monster, Orb, Player
from spirecomm.spire.power import Power
from spirecomm.spire.potion import Potion
from spirecomm.spire.relic import Relic


class PrioritiesStub:
    PLAY_PRIORITIES = {}

    @staticmethod
    def is_card_defensive(card):
        return int(getattr(card, "block", 0) or 0) > 0


def monster(monster_id, hp, *, intent=Intent.BUFF, damage=0, hits=0, powers=None):
    value = Monster(
        monster_id,
        monster_id,
        hp,
        hp,
        0,
        intent,
        False,
        False,
        move_adjusted_damage=damage,
        move_hits=hits,
    )
    value.powers = list(powers or [])
    value.monster_index = 0
    return value


def card(
    card_id,
    card_type,
    *,
    cost=1,
    damage=0,
    block=0,
    target=False,
    playable=True,
    magic=0,
    upgrades=0,
):
    return Card(
        card_id,
        card_id,
        card_type,
        CardRarity.COMMON,
        upgrades=upgrades,
        cost=cost,
        uuid=card_id,
        has_target=target,
        is_playable=playable,
        damage=damage,
        base_damage=damage,
        block=block,
        base_block=block,
        magic_number=magic,
    )


class GameStub:
    def __init__(self, enemies, hand, *, hp=20, block=0, energy=1, act=3):
        self.monsters = list(enemies)
        for index, enemy in enumerate(self.monsters):
            enemy.monster_index = index
        self.hand = list(hand)
        self.player = Player(70, hp, block=block, energy=energy)
        self.player.powers = []
        self.player.orbs = []
        self.act = act
        self.floor = 40
        self.turn = 3
        self.room_type = "MonsterRoom"
        self.relics = []


class CombatSurvivalRegressionTests(unittest.TestCase):
    def setUp(self):
        self.planner = FastCombatPlanner(PrioritiesStub())

    def test_corruption_dark_embrace_cycles_good_instincts_before_end(self):
        """Replay 499175: free block must expose the attack-only discard."""

        collector = monster(
            "TheCollector", 209,
            intent=Intent.ATTACK, damage=37, hits=1,
        )
        good_instincts = card(
            "Good Instincts", CardType.SKILL, cost=0, block=4,
        )
        game = GameStub(
            [collector], [good_instincts], hp=2, energy=1, act=2,
        )
        game.player.max_hp = 95
        game.player.powers = [
            Power("Metallicize", "Metallicize", 4),
            Power("Corruption", "Corruption", -1),
            Power("DarkEmbracePower", "Dark Embrace", 1),
            Power("Frail", "Frail", 1),
        ]
        game.potions = [Potion(
            "FairyPotion", "Fairy in a Bottle",
            can_use=False, can_discard=True, requires_target=False,
        )]
        game.draw_pile = []
        game.discard_pile = [
            card("Bash", CardType.ATTACK, cost=2, damage=8, target=True),
            card("Strike_R", CardType.ATTACK, cost=1, damage=6, target=True),
            card("Anger", CardType.ATTACK, cost=0, damage=6, target=True),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, good_instincts)
        self.assertEqual(
            "safe_corrupted_block_cycle", self.planner.last_decision["reason"]
        )
        cycle = self.planner.last_decision["corrupted_cycle"]
        self.assertEqual("discard_shuffle", cycle["draw_source"])
        self.assertEqual(3, cycle["future_attack_count"])
        self.assertEqual(
            ["Strike_R", "Anger"], cycle["affordable_attack_ids"]
        )

    def test_corrupted_cycle_does_not_use_hand_mutating_block_skill(self):
        enemy = monster("Cultist", 100)
        true_grit = card(
            "True Grit", CardType.SKILL, cost=1, block=7,
        )
        game = GameStub([enemy], [true_grit], hp=20, energy=1, act=2)
        game.player.powers = [
            Power("Corruption", "Corruption", -1),
            Power("DarkEmbracePower", "Dark Embrace", 1),
        ]
        game.draw_pile = []
        game.discard_pile = [
            card("Strike_R", CardType.ATTACK, cost=1, damage=6, target=True)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "no_positive_marginal_action", self.planner.last_decision["reason"]
        )

    def test_whole_turn_counts_stacked_brutality_after_survived_attack(self):
        enemy = monster(
            "Cultist", 50, intent=Intent.ATTACK, damage=12, hits=1
        )
        bash = card(
            "Bash", CardType.ATTACK, cost=2, damage=8, target=True
        )
        game = GameStub([enemy], [bash], hp=5, block=8, energy=2)
        game.player.powers = [Power("Brutality", "Brutality", 3)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, bash)
        self.assertEqual(7, self.planner.last_decision["search"]["projected_loss"])
        self.assertEqual(7, self.planner.last_decision["search"]["actual_loss"])
        self.assertEqual(0, self.planner.last_decision["search"]["tier"])

    def test_true_combat_end_suppresses_next_turn_brutality(self):
        enemy = monster(
            "Cultist", 6, intent=Intent.ATTACK, damage=30, hits=1
        )
        strike = card(
            "Strike_R", CardType.ATTACK, damage=6, target=True
        )
        game = GameStub([enemy], [strike], hp=1, energy=1)
        game.player.powers = [Power("Brutality", "Brutality", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertTrue(self.planner.last_decision["search"]["true_combat_end"])
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])

    def test_mercury_hourglass_is_not_replayed_at_end_of_turn(self):
        enemy = monster(
            "Cultist", 3, intent=Intent.ATTACK, damage=10, hits=1
        )
        poke = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=1, target=True
        )
        game = GameStub([enemy], [poke], hp=20, energy=1)
        game.relics = [Relic("Mercury Hourglass", "Mercury Hourglass")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertFalse(
            self.planner.last_decision["search"]["true_combat_end"]
        )
        self.assertEqual(
            [2], self.planner.last_decision["search"]["final_enemy_hp"]
        )
        self.assertEqual(
            10, self.planner.last_decision["search"]["actual_loss"]
        )

    def test_first_action_damage_is_capped_by_remaining_enemy_hp(self):
        enemy = monster("Cultist", 1)
        strike = card(
            "Strike_R", CardType.ATTACK, damage=6, target=True
        )
        game = GameStub([enemy], [strike], hp=20, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(
            1,
            self.planner.last_decision["search"][
                "first_action_enemy_hp_loss"
            ],
        )

    def test_serialized_sadistic_alias_counts_thunderclap_trigger(self):
        enemy = monster("Cultist", 100)
        thunderclap = card(
            "Thunderclap", CardType.ATTACK,
            cost=1, damage=4, target=False,
        )
        game = GameStub([enemy], [thunderclap], hp=20, energy=1)
        game.player.powers = [Power("Sadistic", "Sadistic", 5)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, thunderclap)
        self.assertEqual(
            9,
            self.planner.last_decision["search"][
                "first_action_enemy_hp_loss"
            ],
        )

    def test_guaranteed_combo_first_damage_uses_reactive_projection(self):
        enemy = monster(
            "Hexaghost", 34, intent=Intent.ATTACK_DEBUFF,
            damage=8, hits=1,
        )
        bash = card(
            "Bash", CardType.ATTACK,
            cost=2, damage=10, magic=3, target=True, upgrades=1,
        )
        pommel = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=10, magic=2, target=True, upgrades=1,
        )
        angers = [
            card(
                f"Anger-{index}", CardType.ATTACK,
                cost=0, damage=6, target=True,
            )
            for index in range(3)
        ]
        for anger in angers:
            anger.card_id = "Anger"
        game = GameStub(
            [enemy], [bash, angers[0], pommel, *angers[1:]],
            hp=13, energy=3, act=1,
        )
        game.player.powers = [Power("Sadistic", "Sadistic", 5)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, bash)
        self.assertEqual(
            "guaranteed_attack_combo_lethal",
            self.planner.last_decision["reason"],
        )
        self.assertEqual(
            15, self.planner.last_decision["first_action_enemy_hp_loss"]
        )

    def test_rip_and_tear_multi_enemy_damage_is_two_packets(self):
        enemies = [
            monster("AcidSlime_S", 30),
            monster("SpikeSlime_S", 30),
            monster("SpikeSlime_S", 30),
        ]
        rip = card(
            "Rip and Tear",
            CardType.ATTACK,
            cost=1,
            damage=6,
            target=False,
        )
        game = GameStub(enemies, [rip], hp=70, energy=1)

        candidate = self.planner._candidate(
            game,
            rip,
            None,
            None,
            0,
            0,
            0,
        )

        # Rip and Tear resolves two random six-damage packets.  The old
        # targetless loop applied one packet to every enemy (18 here), which
        # made the planner disagree with the authoritative 12 total.
        self.assertEqual(12, candidate.damage)
        self.assertFalse(candidate.kills)

    def test_defect_plays_zero_cost_attack_against_orb_walker(self):
        """A live Orb Walker must not make a playable zero-cost Attack disappear."""

        walker = monster(
            "OrbWalker",
            30,
            intent=Intent.ATTACK,
            damage=15,
            hits=1,
        )
        claw = card(
            "Claw",
            CardType.ATTACK,
            cost=0,
            damage=3,
            target=True,
        )
        game = GameStub([walker], [claw], hp=50, energy=1, act=3)
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, claw)
        self.assertEqual("Claw", planner.last_decision["card_id"])

    def test_collector_minion_one_above_poison_is_finished_before_end(self):
        """A zero-loss turn still removes a minion not proven passively dead."""

        torch = monster(
            "TorchHead", 5, intent=Intent.ATTACK, damage=6, hits=1,
            powers=[Power("Poison", "Poison", 4)],
        )
        collector = monster("Collector", 229, intent=Intent.STRONG_DEBUFF)
        collector.block = 11
        strike = card(
            "Strike_G", CardType.ATTACK, cost=1, damage=6, target=True
        )
        defend = card("Defend_G", CardType.SKILL, cost=1, block=10)
        game = GameStub(
            [torch, collector], [strike, defend],
            hp=63, block=10, energy=2, act=2,
        )
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertIs(action.target_monster, torch)

    def test_reptomancer_lethal_incoming_uses_affordable_block_before_end(self):
        conserve = card(
            "Conserve Battery", CardType.SKILL, cost=1, block=11
        )
        reprogram = card("Reprogram", CardType.SKILL, cost=1)
        impatience = card("Impatience", CardType.SKILL, cost=1)
        game = GameStub(
            [
                monster(
                    "Reptomancer", 180, intent=Intent.ATTACK,
                    damage=34, hits=1,
                ),
                monster(
                    "Dagger", 25, intent=Intent.ATTACK,
                    damage=25, hits=1,
                ),
                monster(
                    "Dagger", 25, intent=Intent.ATTACK,
                    damage=30, hits=1,
                ),
            ],
            [conserve, reprogram, impatience],
            hp=46, block=20, energy=1, act=3,
        )
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, conserve)

    def test_upgraded_bouncing_flask_has_four_three_poison_applications(self):
        """The upgrade adds a fourth independent three-Poison packet."""

        guardian = monster(
            "SphericGuardian",
            17,
            intent=Intent.ATTACK,
            damage=10,
            hits=1,
            powers=[Power("Poison", "Poison", 2)],
        )
        flask = card(
            "Bouncing Flask",
            CardType.SKILL,
            cost=1,
            target=True,
            magic=4,
            upgrades=1,
        )
        game = GameStub([guardian], [flask], hp=68, energy=1, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, flask)
        search = self.planner.last_decision["search"]
        self.assertEqual([3], search["final_enemy_hp"])
        self.assertFalse(search["true_combat_end"])

    def test_upgraded_bouncing_flask_transition_counts_four_packets(self):
        guardian = monster(
            "SphericGuardian",
            40,
            powers=[Power("Poison", "Poison", 2)],
        )
        flask = card(
            "Bouncing Flask",
            CardType.SKILL,
            cost=1,
            target=True,
            magic=4,
            upgrades=1,
        )

        self.assertEqual(14, self.planner._poison_after_card(flask, guardian))

    def test_battle_trance_precedes_energy_consuming_attacks(self):
        """Replay floor 21: zero-cost draw must not be saved for zero energy."""

        enemy = monster(
            "Shelled Parasite",
            61,
            intent=Intent.ATTACK,
            damage=4,
            hits=2,
        )
        battle_trance = card(
            "Battle Trance", CardType.SKILL, cost=0, magic=3
        )
        rage = card("Rage", CardType.SKILL, cost=0, magic=3)
        perfected = card(
            "Perfected Strike",
            CardType.ATTACK,
            cost=2,
            damage=24,
            target=True,
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        defend = card("Defend_R", CardType.SKILL, cost=1, block=5)
        game = GameStub(
            [enemy],
            [battle_trance, strike, defend, perfected, rage],
            hp=24,
            energy=3,
            act=2,
        )
        game.deck = list(game.hand)
        planner = FastCombatPlanner(IroncladPriority())

        planner.choose_card_action(game)

        sequence = [
            step["card_id"] for step in planner.last_decision["planned_sequence"]
        ]
        self.assertLess(
            sequence.index("Battle Trance"),
            sequence.index("Perfected Strike"),
        )
        self.assertLess(
            sequence.index("Battle Trance"),
            sequence.index("Strike_R"),
        )

    def test_acrobatics_precedes_champ_defense_to_preserve_drawn_card_energy(self):
        """Replay floor 33: draw at four energy before spending on defense."""

        champ = monster(
            "Champ",
            237,
            intent=Intent.ATTACK_DEBUFF,
            damage=12,
            hits=1,
            powers=[
                Power("Poison", "Poison", 22),
                Power("Strength", "Strength", -4),
                Power("Shackled", "Shackled", 6),
            ],
        )
        champ.max_hp = 420
        defend = card("Defend_G", CardType.SKILL, cost=1, block=8)
        spray = card(
            "Dagger Spray", CardType.ATTACK, cost=1, damage=6
        )
        wail = card("PiercingWail", CardType.SKILL, cost=1, magic=6)
        wail.exhausts = True
        acrobatics = card(
            "Acrobatics", CardType.SKILL, cost=1, magic=3
        )
        poison = card(
            "Deadly Poison", CardType.SKILL, cost=1, target=True, magic=5
        )
        game = GameStub(
            [champ],
            [defend, spray, wail, acrobatics, poison],
            hp=39,
            energy=4,
            act=2,
        )
        game.room_type = "MonsterRoomBoss"
        game.floor = 33
        game.turn = 7
        game.player.max_hp = 77
        game.player.powers = [
            Power("Thorns", "Thorns", 3),
            Power("Strength", "Strength", 2),
            Power("Dexterity", "Dexterity", 6),
            Power("Vulnerable", "Vulnerable", 2),
            Power("Noxious Fumes", "Noxious Fumes", 3),
            Power("Frail", "Frail", 1),
        ]
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, acrobatics)
        sequence = [
            step["card_id"] for step in planner.last_decision["planned_sequence"]
        ]
        self.assertLess(
            sequence.index("Acrobatics"),
            sequence.index("PiercingWail"),
        )

    def test_offering_is_not_used_as_the_sixth_velvet_choker_card(self):
        """Offering's draw/energy are worthless after the Choker cap."""

        enemy = monster("Cultist", 50, intent=Intent.BUFF)
        offering = card("Offering", CardType.SKILL, cost=0, magic=3)
        offering.exhausts = True
        bash = card("Bash", CardType.ATTACK, cost=2, damage=8, target=True)
        game = GameStub([enemy], [offering, bash], hp=30, energy=0, act=1)
        game.relics = [
            Relic("Velvet Choker", "Velvet Choker", counter=5)
        ]
        planner = FastCombatPlanner(IroncladPriority())
        planner._card_play_turn_key = planner._combat_turn_key(game)
        planner._confirmed_cards_played = 5

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(30, game.player.current_hp)

    def test_skill_potion_offering_search_beats_disarm_and_survives(self):
        """Replay 75d seq242269-277 with exact potion draw order."""

        centurion = monster(
            "Centurion", 77, intent=Intent.ATTACK, damage=18, hits=1
        )
        thunderclap = card(
            "Thunderclap", CardType.ATTACK,
            cost=1, damage=4, target=False,
        )
        anger_one = card(
            "Anger", CardType.ATTACK, cost=0, damage=6, target=True
        )
        anger_one.uuid = "anger-hand-one"
        anger_two = card(
            "Anger", CardType.ATTACK, cost=0, damage=6, target=True
        )
        anger_two.uuid = "anger-hand-two"
        pommel = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=9, target=True,
        )
        entrench = card("Entrench", CardType.SKILL, cost=1)
        game = GameStub(
            [centurion],
            [thunderclap, entrench, anger_one, anger_two, pommel],
            hp=12,
            energy=3,
            act=2,
        )
        game.current_action = "DiscoveryAction"
        game.player.powers = [Power("Frail", "Frail", 1)]
        drawn_anger_three = card(
            "Anger", CardType.ATTACK,
            cost=0, damage=6, target=True, playable=False,
        )
        drawn_anger_three.uuid = "anger-draw-three"
        flame_barrier = card(
            "Flame Barrier", CardType.SKILL,
            cost=2, block=9, playable=False,
        )
        flame_barrier.base_block = 12
        drawn_anger_four = card(
            "Anger", CardType.ATTACK,
            cost=0, damage=6, target=True, playable=False,
        )
        drawn_anger_four.uuid = "anger-draw-four"
        game.draw_pile = [
            drawn_anger_three, flame_barrier, drawn_anger_four
        ]

        offering = card("Offering", CardType.SKILL, cost=0, magic=3)
        offering.exhausts = True
        disarm = card(
            "Disarm", CardType.SKILL,
            cost=1, target=True, magic=2, playable=False,
        )
        true_grit = card(
            "True Grit", CardType.SKILL,
            cost=1, block=7, playable=False,
        )

        offering_score = self.planner.score_temporary_card(game, offering)
        offering_trace = dict(self.planner._last_temporary_card_search)
        disarm_score = self.planner.score_temporary_card(game, disarm)
        true_grit_score = self.planner.score_temporary_card(game, true_grit)

        self.assertGreater(offering_score, disarm_score)
        self.assertGreater(offering_score, true_grit_score)
        self.assertGreater(offering_score, 0)
        self.assertEqual(6, offering_trace["prefix_hp_loss"])
        self.assertEqual(
            ["Anger", "Flame Barrier", "Anger"],
            offering_trace["offering_drawn_card_ids"],
        )
        self.assertIn(
            "Flame Barrier", offering_trace["continuation_card_ids"]
        )
        self.assertIn("Entrench", offering_trace["continuation_card_ids"])
        self.assertLess(
            offering_trace["candidate"]["actual_loss"], 6
        )

    def test_attack_potion_e0_vetoes_skewer_for_executable_twelve_damage(self):
        """Replay e1 seq240885-887: visible zero cost is not X effect."""

        book = monster("BookOfStabbing", 78, intent=Intent.BUFF)
        slice_card = card(
            "Slice", CardType.ATTACK,
            cost=0, damage=3, target=True,
        )
        game = GameStub(
            [book], [slice_card], hp=4, block=5, energy=0, act=2
        )
        game.current_action = "DiscoveryAction"
        game.draw_pile = []

        skewer = card(
            "Skewer", CardType.ATTACK,
            cost=-1, damage=7, target=True,
        )
        underhanded = card(
            "Underhanded Strike", CardType.ATTACK,
            cost=2, damage=0, target=True, playable=False,
        )
        underhanded.damage = -1
        underhanded.base_damage = 12

        skewer_score = self.planner.score_temporary_card(game, skewer)
        underhanded_score = self.planner.score_temporary_card(
            game, underhanded
        )
        search = dict(self.planner._last_temporary_card_search)

        self.assertLess(skewer_score, 0)
        self.assertGreater(underhanded_score, 0)
        self.assertGreater(underhanded_score, skewer_score)
        self.assertEqual("authoritative_one_turn_search", search["mode"])
        self.assertLessEqual(
            sum(search["candidate"]["final_enemy_hp"]), 63
        )

    def test_non_discovery_combat_reward_keeps_printed_card_cost(self):
        enemy = monster("Cultist", 40)
        game = GameStub([enemy], [], hp=20, energy=0, act=1)
        game.current_action = "SomeOtherRewardAction"
        expensive = card(
            "Carnage", CardType.ATTACK,
            cost=2, damage=20, target=True, playable=False,
        )

        self.planner.score_temporary_card(game, expensive)

        self.assertEqual(
            "static_fallback",
            self.planner._last_temporary_card_search["mode"],
        )
        self.assertEqual(2, expensive.cost)

    def test_offering_temporary_search_fails_closed_under_no_draw(self):
        enemy = monster(
            "Centurion", 40, intent=Intent.ATTACK, damage=18, hits=1
        )
        game = GameStub([enemy], [], hp=12, energy=3, act=2)
        game.current_action = "DiscoveryAction"
        game.player.powers = [Power("NoDrawPower", "No Draw", 1)]
        game.draw_pile = [
            card("Anger", CardType.ATTACK, cost=0, damage=6, target=True)
            for _ in range(3)
        ]
        offering = card("Offering", CardType.SKILL, cost=0, magic=3)

        score = self.planner.score_temporary_card(game, offering)

        self.assertLess(score, 0)
        self.assertEqual(
            "fail_closed", self.planner._last_temporary_card_search["mode"]
        )

    def test_temporary_search_inherits_fifth_choker_resolution(self):
        """A generated sixth card cannot reopen four zero-cost Angers."""

        enemy = monster("Cultist", 100, intent=Intent.BUFF)
        angers = []
        for index in range(4):
            anger = card(
                "Anger", CardType.ATTACK,
                cost=0, damage=6, target=True,
            )
            anger.uuid = f"late-choker-anger-{index}"
            angers.append(anger)
        game = GameStub([enemy], angers, hp=40, energy=3, act=2)
        game.current_action = "DiscoveryAction"
        # counter=-1 forces the planner-owned confirmed progress path.
        game.relics = [Relic("Velvet Choker", "Velvet Choker", counter=-1)]
        carnage = card(
            "Carnage", CardType.ATTACK,
            cost=2, damage=20, target=True, playable=False,
        )
        planner = FastCombatPlanner(IroncladPriority())
        planner._card_play_turn_key = planner._combat_turn_key(game)
        planner._confirmed_cards_played = 5
        planner._confirmed_card_resolutions = 5

        first_score = planner.score_temporary_card(game, carnage)
        first_trace = dict(planner._last_temporary_card_search)
        second_score = planner.score_temporary_card(game, carnage)
        second_trace = dict(planner._last_temporary_card_search)

        self.assertGreater(first_score, 0)
        self.assertEqual(first_score, second_score)
        self.assertEqual(5, first_trace["source_confirmed_cards_played"])
        self.assertEqual(
            5, first_trace["source_confirmed_card_resolutions"]
        )
        self.assertLessEqual(
            len(first_trace["continuation_card_ids"]), 1
        )
        self.assertEqual(
            first_trace["continuation_card_ids"],
            second_trace["continuation_card_ids"],
        )
        self.assertEqual(5, planner._confirmed_cards_played)
        self.assertEqual(5, planner._confirmed_card_resolutions)

    def test_temporary_search_inherits_normality_third_card_boundary(self):
        """Confirmed two resolutions leave exactly one legal generated play."""

        enemy = monster("Cultist", 100, intent=Intent.BUFF)
        normality = card(
            "Normality", CardType.CURSE,
            cost=-2, playable=False,
        )
        angers = []
        for index in range(3):
            anger = card(
                "Anger", CardType.ATTACK,
                cost=0, damage=6, target=True,
            )
            anger.uuid = f"late-normality-anger-{index}"
            angers.append(anger)
        game = GameStub(
            [enemy], [normality, *angers], hp=40, energy=3, act=2
        )
        game.current_action = "DiscoveryAction"
        carnage = card(
            "Carnage", CardType.ATTACK,
            cost=2, damage=20, target=True, playable=False,
        )
        planner = FastCombatPlanner(IroncladPriority())
        planner._card_play_turn_key = planner._combat_turn_key(game)
        planner._confirmed_cards_played = 2
        planner._confirmed_card_resolutions = 2

        planner.score_temporary_card(game, carnage)
        trace = dict(planner._last_temporary_card_search)

        self.assertEqual(2, trace["source_confirmed_cards_played"])
        self.assertEqual(2, trace["source_confirmed_card_resolutions"])
        self.assertLessEqual(len(trace["continuation_card_ids"]), 1)
        self.assertEqual(2, planner._confirmed_cards_played)
        self.assertEqual(2, planner._confirmed_card_resolutions)

    def test_offering_is_allowed_before_choker_cap_when_it_enables_bash(self):
        enemy = monster("Cultist", 50, intent=Intent.BUFF)
        offering = card("Offering", CardType.SKILL, cost=0, magic=3)
        offering.exhausts = True
        bash = card("Bash", CardType.ATTACK, cost=2, damage=8, target=True)
        game = GameStub([enemy], [offering, bash], hp=30, energy=0, act=1)
        game.relics = [
            Relic("Velvet Choker", "Velvet Choker", counter=4)
        ]
        planner = FastCombatPlanner(IroncladPriority())
        planner._card_play_turn_key = planner._combat_turn_key(game)
        planner._confirmed_cards_played = 4

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, offering)
        self.assertEqual(
            ["Offering", "Bash"],
            [step["card_id"] for step in planner.last_decision["planned_sequence"]],
        )

    def test_offering_is_rejected_when_plain_attack_has_same_lethal(self):
        """Do not pay six HP for draw/energy when Strike already ends combat."""

        enemy = monster(
            "Cultist", 3, intent=Intent.ATTACK, damage=15, hits=1
        )
        offering = card(
            "Offering", CardType.SKILL, cost=0, magic=5, upgrades=1
        )
        offering.exhausts = True
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub(
            [enemy], [offering, strike], hp=50, energy=2, act=1
        )
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        comparison = planner.last_decision["search"][
            "voluntary_self_damage_comparison"
        ]
        self.assertTrue(comparison["evaluated"])
        self.assertTrue(comparison["dominated"])
        self.assertEqual("rejected_dominated_plan", comparison["status"])
        self.assertEqual(
            "same_combat_end_less_hp_loss", comparison["reason"]
        )
        self.assertEqual(
            6, comparison["rejected_plan"]["voluntary_self_hp_cost"]
        )
        self.assertEqual(
            0, comparison["chosen_plan"]["voluntary_self_hp_cost"]
        )

    def test_velvet_choker_cap_blocks_fallback_seventh_card(self):
        enemy = monster("Cultist", 50, intent=Intent.ATTACK, damage=5)
        strike = card("Strike_R", CardType.ATTACK, cost=1, damage=6, target=True)
        game = GameStub([enemy], [strike], hp=30, energy=1, act=1)
        game.relics = [
            Relic("Velvet Choker", "Velvet Choker", counter=6)
        ]
        planner = FastCombatPlanner(IroncladPriority())
        planner._card_play_turn_key = planner._combat_turn_key(game)
        planner._confirmed_cards_played = 6

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual("velvet_choker_card_limit", planner.last_decision["reason"])

    def test_velvet_choker_plays_adrenaline_before_low_value_cap_fillers(self):
        """Replay run 5 T1: draw the poison finish before filling the cap."""

        enemy = monster("Donu", 250, intent=Intent.BUFF)
        bite = card(
            "Bite", CardType.ATTACK, cost=1, damage=7, target=True
        )
        bane = card(
            "Bane", CardType.ATTACK, cost=1, damage=7, target=True
        )
        survivor = card("Survivor", CardType.SKILL, cost=1, block=11)
        defends = [
            card(f"Defend_G_{index}", CardType.SKILL, cost=1, block=5)
            for index in range(3)
        ]
        adrenaline = card(
            "Adrenaline", CardType.SKILL, cost=0, magic=2, upgrades=1
        )
        game = GameStub(
            [enemy],
            [bite, survivor, *defends, bane, adrenaline],
            hp=29,
            energy=3,
            act=3,
        )
        game.relics = [Relic("Velvet Choker", "Velvet Choker", counter=2)]
        game.draw_pile = [
            card("Crippling Cloud", CardType.SKILL, cost=2, upgrades=1),
            card("Catalyst", CardType.SKILL, cost=1, upgrades=1),
        ]
        planner = FastCombatPlanner(SilentPriority())

        def stale_expected_draw_plan(_game, groups, *_args, **_kwargs):
            by_id = {
                getattr(candidate.card, "card_id", None): candidate
                for group in groups
                for candidate in group
            }
            return 20.0, [
                by_id["Bite"],
                by_id["Bane"],
                by_id["Adrenaline"],
                by_id["Survivor"],
            ]

        with patch.object(
            planner, "_best_plan", side_effect=stale_expected_draw_plan
        ):
            action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, adrenaline)
        self.assertEqual(
            "velvet_choker_draw_accelerator_first",
            planner.last_decision["reason"],
        )
        self.assertEqual(4, planner.last_decision["choker_remaining"])
        self.assertIn(
            "Adrenaline", planner.last_decision["choker_deferred_sequence"]
        )

    def test_armaments_plus_precedes_unupgraded_planned_cards(self):
        armaments = card(
            "Armaments", CardType.SKILL, cost=1, block=5, upgrades=1
        )
        thunderclap = card(
            "Thunderclap", CardType.ATTACK,
            cost=1, damage=4, target=False,
        )
        game = GameStub(
            [monster("Cultist", 50)],
            [thunderclap, armaments],
            hp=50,
            energy=2,
            act=2,
        )
        planner = FastCombatPlanner(IroncladPriority())

        def stale_upgrade_plan(_game, groups, *_args, **_kwargs):
            by_id = {
                getattr(candidate.card, "card_id", None): candidate
                for group in groups
                for candidate in group
            }
            return 10.0, [by_id["Thunderclap"], by_id["Armaments"]]

        with patch.object(
            planner, "_best_plan", side_effect=stale_upgrade_plan
        ):
            action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, armaments)
        self.assertEqual(
            "armaments_handwide_upgrade_first",
            planner.last_decision["reason"],
        )
        self.assertEqual(
            ["Thunderclap", "Armaments"],
            planner.last_decision["armaments_deferred_sequence"],
        )

    def test_draw_source_stays_before_armaments_plus(self):
        pact = card("Burning Pact", CardType.SKILL, cost=1, magic=2)
        armaments = card(
            "Armaments", CardType.SKILL, cost=1, block=5, upgrades=1
        )
        thunderclap = card(
            "Thunderclap", CardType.ATTACK,
            cost=1, damage=4, target=False,
        )
        game = GameStub(
            [monster("Cultist", 50)],
            [pact, thunderclap, armaments],
            hp=50,
            energy=3,
            act=2,
        )
        planner = FastCombatPlanner(IroncladPriority())

        def draw_then_upgrade_plan(_game, groups, *_args, **_kwargs):
            by_id = {
                getattr(candidate.card, "card_id", None): candidate
                for group in groups
                for candidate in group
            }
            return 12.0, [
                by_id["Burning Pact"],
                by_id["Thunderclap"],
                by_id["Armaments"],
            ]

        with patch.object(
            planner, "_best_plan", side_effect=draw_then_upgrade_plan
        ):
            action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, pact)
        self.assertNotEqual(
            "armaments_handwide_upgrade_first",
            planner.last_decision["reason"],
        )

    def test_last_velvet_choker_slot_prefers_attack_progress_over_setup(self):
        """Do not spend the sixth play on setup when attack HP progress is free."""

        enemy = monster(
            "WrithingMass", 40, intent=Intent.BUFF,
        )
        enemy.block = 5
        setup = card("Demon Form", CardType.POWER, cost=1, magic=3)
        strike = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=12, target=True,
        )
        game = GameStub(
            [enemy], [setup, strike], hp=30, energy=1, act=3,
        )
        game.relics = [
            Relic("Velvet Choker", "Velvet Choker", counter=5),
        ]
        planner = FastCombatPlanner(IroncladPriority())

        # Force the ordinary beam to reject the branch.  The narrow Choker
        # boundary must still preserve the executable attack opportunity.
        with patch.object(planner, "_best_plan", return_value=(0.0, [])):
            action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertIs(action.target_monster, enemy)
        self.assertEqual(
            "velvet_choker_progress_fallback",
            planner.last_decision["reason"],
        )

    def test_writhing_mass_f39_sixth_slot_cannot_be_demon_form(self):
        """Replay F39 T2: a Choker slot with a crossing Attack beats setup."""

        enemy = monster(
            "WrithingMass", 160,
            intent=Intent.ATTACK_DEBUFF, damage=15, hits=1,
            powers=[
                Power("Malleable", "Malleable", 3),
                Power("Vulnerable", "Vulnerable", 3),
                Power("Compulsive", "Compulsive", -1),
            ],
        )
        enemy.block = 5
        demon_form = card("Demon Form", CardType.POWER, cost=3, magic=2)
        clothesline = card(
            "Clothesline", CardType.ATTACK,
            cost=2, damage=12, target=True,
        )
        twin_strike = card(
            "Twin Strike", CardType.ATTACK,
            cost=1, damage=5, target=True,
        )
        dropkick = card(
            "Dropkick", CardType.ATTACK,
            cost=1, damage=5, target=True,
        )
        seeing_red = card("Seeing Red", CardType.SKILL, cost=0)
        game = GameStub(
            [enemy],
            [demon_form, clothesline, twin_strike, dropkick, seeing_red],
            hp=73, block=19, energy=6, act=3,
        )
        game.player.max_hp = 85
        game.player.powers = [
            Power("Corruption", "Corruption", -1),
            Power("Metallicize", "Metallicize", 3),
            Power("DemonForm", "Demon Form", 2),
            Power("Strength", "Strength", 6),
        ]
        game.relics = [Relic("Velvet Choker", "Velvet Choker", counter=5)]

        planner = FastCombatPlanner(IroncladPriority())
        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, clothesline)
        self.assertIs(action.target_monster, enemy)
        self.assertEqual(
            "velvet_choker_progress_fallback",
            planner.last_decision["reason"],
        )

    def test_last_velvet_choker_slot_counts_attack_into_current_block(self):
        """A blocked hit still consumes persistent/last-slot progress."""

        enemy = monster("WrithingMass", 90, intent=Intent.BUFF)
        enemy.block = 15
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub([enemy], [strike], hp=30, energy=1, act=3)
        game.relics = [Relic("Velvet Choker", "Velvet Choker", counter=5)]
        planner = FastCombatPlanner(IroncladPriority())

        with patch.object(planner, "_best_plan", return_value=(0.0, [])):
            action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertIs(action.target_monster, enemy)
        self.assertEqual(
            6, planner.last_decision["blocked_attack_progress"]
        )

    def test_no_incoming_damage_prefers_bash_setup_over_unused_block(self):
        enemy = monster("Cultist", 50, intent=Intent.BUFF)
        bash = card("Bash", CardType.ATTACK, cost=2, damage=8, target=True)
        shrug = card("Shrug It Off", CardType.SKILL, cost=1, block=8)
        game = GameStub([enemy], [shrug, bash], hp=30, energy=3, act=1)
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, bash)

    def test_offering_is_not_third_card_with_normality_in_hand(self):
        """Replay seq 116289: drawn cards cannot be used after card three."""

        enemy = monster("OrbWalker", 55)
        normality = card(
            "Normality", CardType.CURSE, cost=-2, playable=False
        )
        offering = card("Offering", CardType.SKILL, cost=0, magic=3)
        offering.exhausts = True
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        defend = card("Defend_R", CardType.SKILL, cost=1, block=5)
        game = GameStub(
            [enemy], [normality, offering, strike, defend],
            hp=58, block=21, energy=3, act=3,
        )
        game.turn = 2
        self.planner._card_play_turn_key = self.planner._combat_turn_key(game)
        self.planner._confirmed_cards_played = 2
        self.planner._confirmed_card_resolutions = 2

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIsNot(action.card, offering)
        self.assertNotIn(
            "Offering",
            [
                item["card_id"]
                for item in self.planner.last_decision.get(
                    "planned_sequence", []
                )
            ],
        )

    def test_card_play_progress_counts_only_authoritative_confirmation(self):
        enemy = monster("Cultist", 40)
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub([enemy], [strike], hp=20, energy=1)
        game.turn = 2

        self.planner._sync_confirmed_card_plays(game)
        self.planner._play_card_action(game, strike, enemy)
        self.planner._sync_confirmed_card_plays(game)
        self.assertEqual(0, self.planner._confirmed_cards_played)

        game.hand = []
        self.planner._sync_confirmed_card_plays(game)
        self.assertEqual(1, self.planner._confirmed_cards_played)
        self.assertEqual(1, self.planner._confirmed_card_resolutions)

        game.turn = 3
        self.planner._sync_confirmed_card_plays(game)
        self.assertEqual(0, self.planner._confirmed_cards_played)
        self.assertEqual(0, self.planner._confirmed_card_resolutions)

    def test_other_draw_precedes_battle_trance_no_draw(self):
        enemy = monster("Cultist", 80, intent=Intent.BUFF)
        pommel = card(
            "Pommel Strike",
            CardType.ATTACK,
            cost=1,
            damage=9,
            target=True,
            magic=1,
        )
        battle_trance = card(
            "Battle Trance", CardType.SKILL, cost=0, magic=3
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub(
            [enemy], [battle_trance, pommel, strike], energy=2, act=2
        )
        planner = FastCombatPlanner(IroncladPriority())

        planner.choose_card_action(game)

        sequence = [
            step["card_id"] for step in planner.last_decision["planned_sequence"]
        ]
        self.assertLess(
            sequence.index("Pommel Strike"),
            sequence.index("Battle Trance"),
        )

    def test_guardian_mode_shift_attack_cancels_lethal_intent(self):
        guardian = monster(
            "TheGuardian",
            150,
            intent=Intent.ATTACK,
            damage=24,
            hits=1,
            powers=[Power("Mode Shift", "Mode Shift", 6)],
        )
        strike = card("Strike_R", CardType.ATTACK, damage=6, target=True)
        defend = card("Defend_R", CardType.SKILL, block=5)
        game = GameStub([guardian], [strike, defend], hp=10)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)

    def test_guardian_cumulative_damage_triggers_mode_shift(self):
        guardian = monster(
            "TheGuardian",
            100,
            intent=Intent.ATTACK,
            damage=35,
            hits=1,
            powers=[Power("Mode Shift", "Mode Shift", 10)],
        )
        attacks = [
            card(f"Strike-{index}", CardType.ATTACK, damage=6, target=True)
            for index in range(2)
        ]
        defend = card("Defend", CardType.SKILL, block=5)
        game = GameStub([guardian], [*attacks, defend], hp=20, energy=2)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["actual_loss"])
        self.assertEqual([88], search["final_enemy_hp"])
        self.assertEqual([20], search["final_enemy_block"])
        self.assertEqual([0], search["action_suppressed_enemy_indexes"])

    def test_guardian_plan_does_not_reuse_turn_start_energy_for_x_attacks(self):
        """Replay E2E seq 91991: Bash cannot power either later Whirlwind."""

        guardian = monster(
            "TheGuardian",
            50,
            intent=Intent.ATTACK,
            damage=3,
            hits=4,
            powers=[
                Power("Mode Shift", "Mode Shift", 60),
                Power("Weakened", "Weak", 1),
            ],
        )
        bash = card(
            "Bash", CardType.ATTACK, cost=2, damage=8, target=True, magic=2
        )
        first_whirlwind = card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=5, target=False
        )
        first_whirlwind.uuid = "whirlwind-1"
        second_whirlwind = card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=5, target=False
        )
        second_whirlwind.uuid = "whirlwind-2"
        game = GameStub(
            [guardian],
            [bash, first_whirlwind, second_whirlwind],
            hp=5,
            block=8,
            energy=2,
            act=1,
        )
        game.floor = 16
        game.turn = 16
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, bash)
        self.assertEqual(
            ["Bash"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(
            [42], self.planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_zero_energy_x_attack_is_blocked_at_action_boundary(self):
        """A legacy fallback must not emit a zero-hit X-cost attack."""

        enemy = monster(
            "Collector", 80, intent=Intent.ATTACK, damage=10, hits=1
        )
        whirlwind = card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=5, target=False
        )
        game = GameStub([enemy], [whirlwind], hp=50, energy=0)

        action = self.planner._play_card_action(game, whirlwind)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "zero_energy_x_attack_no_effect",
            self.planner.last_decision["reason"],
        )

    def test_seq_91937_does_not_spend_hp_for_a_nonpositive_focus_attack(self):
        """Replay E2E seq 91937 with target-local Vulnerable included."""

        guardian = monster(
            "TheGuardian",
            208,
            intent=Intent.BUFF,
            powers=[Power("Vulnerable", "Vulnerable", 1)],
        )
        guardian.max_hp = 240
        guardian.block = 20
        hemokinesis = card(
            "Hemokinesis",
            CardType.ATTACK,
            cost=1,
            damage=15,
            target=True,
        )
        cleave = card(
            "Cleave", CardType.ATTACK, cost=1, damage=8, target=False
        )
        defends = [
            card(f"Defend-{index}", CardType.SKILL, cost=1, block=5)
            for index in range(2)
        ]
        game = GameStub(
            [guardian],
            [cleave, *defends, hemokinesis],
            hp=73,
            block=13,
            energy=1,
            act=1,
        )
        game.player.max_hp = 77
        game.floor = 16
        game.turn = 2
        game.room_type = "MonsterRoomBoss"

        preferred = self.planner._prepare_focus(game, [guardian])
        candidate = self.planner._candidate(
            game,
            hemokinesis,
            guardian,
            preferred,
            incoming=0,
            attack_loss=0,
            total_loss=0,
        )
        action = self.planner.choose_card_action(game)

        # Fifteen becomes 22 under Vulnerable, crosses 20 Block, and deals
        # two real HP. Trading the player's persistent two HP for exactly two
        # boss HP has negative intrinsic value after energy cost; preferred
        # target status may order targets but cannot resurrect the action.
        self.assertEqual(2, candidate.damage)
        self.assertLess(candidate.base_score, 0)
        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "no_positive_marginal_action",
            self.planner.last_decision["reason"],
        )

    def test_weak_setup_is_used_when_current_block_already_covers_attack(self):
        """Do not END with energy when Weak protects the next attack."""

        book = monster(
            "BookOfStabbing",
            100,
            intent=Intent.ATTACK,
            damage=4,
            hits=4,
            powers=[Power("Weakened", "Weakened", 1)],
        )
        leg_sweep = card(
            "Leg Sweep",
            CardType.SKILL,
            cost=2,
            block=11,
            target=True,
            magic=2,
        )
        game = GameStub([book], [leg_sweep], hp=12, block=20, energy=2, act=2)
        game.room_type = "MonsterRoomElite"

        action = self.planner.choose_card_action(game)

        # The existing 20 block prevents all 16 current damage, but it will
        # be discarded at END.  Extending Weak is the only deterministic
        # value in the hand and should be selected before the next 30-ish hit.
        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, leg_sweep)
        self.assertEqual(
            "future_turn_defense_setup",
            self.planner.last_decision["reason"],
        )
        self.assertGreater(
            self.planner.last_decision["future_turn_weak_value"], 0
        )

    def test_bash_vulnerable_applies_only_to_later_attack_in_plan(self):
        target = monster("Cultist", 100)
        bash = card(
            "Bash",
            CardType.ATTACK,
            cost=2,
            damage=8,
            target=True,
            magic=2,
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub([target], [strike, bash], hp=30, energy=3, act=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, bash)
        self.assertEqual(
            ["Bash", "Strike_R"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        # Bash itself deals eight; only the later Strike is multiplied to
        # floor(6 * 1.5) = 9.
        self.assertEqual(
            [83], self.planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_hexaghost_opens_with_thunderclap_before_flash_and_bash(self):
        """The AOE attack is the Vulnerable setup; Bash-first loses damage."""

        target = monster("Hexaghost", 250)
        bash = card(
            "Bash",
            CardType.ATTACK,
            cost=2,
            damage=10,
            target=True,
            magic=3,
            upgrades=1,
        )
        thunderclap = card(
            "Thunderclap", CardType.ATTACK, cost=1, damage=4
        )
        flash = card(
            "Flash of Steel", CardType.ATTACK, cost=0, damage=3, target=True
        )
        game = GameStub(
            [target], [bash, card("Defend_R", CardType.SKILL),
                       card("Defend_R", CardType.SKILL), thunderclap, flash],
            hp=57, energy=3, act=1,
        )
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, thunderclap)
        self.assertEqual(
            ["Thunderclap", "Flash of Steel", "Bash"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(
            [227], planner.last_decision["search"]["final_enemy_hp"]
        )
        self.assertEqual(
            [4], planner.last_decision["search"]["final_enemy_vulnerable"]
        )
        self.assertTrue(planner.last_decision["vulnerable_opener"])
        self.assertEqual(
            "Thunderclap", planner.last_decision["vulnerable_source"]
        )
        self.assertEqual(2, planner.last_decision["vulnerable_consumers"])

    def test_low_efficiency_self_damage_gate_keeps_kill_exception(self):
        target = monster(
            "Cultist",
            2,
            powers=[Power("Vulnerable", "Vulnerable", 1)],
        )
        target.block = 20
        hemokinesis = card(
            "Hemokinesis",
            CardType.ATTACK,
            cost=1,
            damage=15,
            target=True,
        )
        game = GameStub([target], [hemokinesis], hp=20, energy=1, act=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, hemokinesis)

    def test_optional_self_damage_yields_to_fully_blockable_attack(self):
        """Avoid Hemokinesis when the remaining Defend prevents all damage."""

        hexaghost = monster(
            "Hexaghost", 49, intent=Intent.ATTACK, damage=14, hits=1,
        )
        defend = card("Defend_R", CardType.SKILL, block=5)
        hemokinesis = card(
            "Hemokinesis", CardType.ATTACK, cost=1, damage=22, target=True,
        )
        game = GameStub(
            [hexaghost], [defend, hemokinesis],
            hp=22, block=10, energy=1, act=1,
        )
        game.floor = 16
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)
        search = self.planner.last_decision["search"]
        comparison = search["voluntary_self_damage_comparison"]
        self.assertTrue(comparison["dominated"])
        self.assertEqual("low_efficiency_optional_self_damage", comparison["reason"])
        self.assertEqual(0, comparison["chosen_plan"]["voluntary_self_hp_cost"])
        self.assertEqual(0, search["actual_loss"])

    def test_runic_cube_values_a_self_damage_draw_event(self):
        enemy = monster("Cultist", 50, intent=Intent.BUFF)
        hemokinesis = card(
            "Hemokinesis", CardType.ATTACK, cost=1, damage=15, target=True
        )
        game = GameStub([enemy], [hemokinesis], hp=30, energy=1, act=1)
        game.relics = [Relic("Runic Cube", "Runic Cube")]
        planner = FastCombatPlanner(IroncladPriority())

        candidate = planner._candidate(
            game,
            hemokinesis,
            enemy,
            enemy,
            incoming=0,
            attack_loss=0,
            total_loss=0,
        )

        self.assertEqual(1, candidate.runic_cube_draws)
        self.assertGreaterEqual(candidate.hand_additions, 1)

    def test_seq_92690_uses_safe_high_value_self_damage(self):
        """Covered incoming damage must not make low HP a flat attack ban."""

        automaton = monster(
            "BronzeAutomaton",
            149,
            intent=Intent.ATTACK,
            damage=13,
            hits=2,
            powers=[Power("Strength", "Strength", 6)],
        )
        automaton.max_hp = 300
        hemokinesis = card(
            "Hemokinesis",
            CardType.ATTACK,
            cost=1,
            damage=22,
            target=True,
        )
        wounds = [
            card("Wound", CardType.STATUS, cost=-2, playable=False)
            for _ in range(3)
        ]
        for index, wound in enumerate(wounds):
            wound.uuid = f"wound-{index}"
        game = GameStub(
            [automaton],
            [wounds[0], hemokinesis, *wounds[1:]],
            hp=5,
            block=23,
            energy=1,
            act=2,
        )
        game.player.max_hp = 85
        game.player.powers = [Power("Metallicize", "Metallicize", 4)]
        game.floor = 33
        game.turn = 8
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, hemokinesis)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["projected_loss"])
        self.assertEqual(2, search["actual_loss"])
        self.assertEqual(3, search["player_hp_after_cards"])
        self.assertEqual(2, search["tier"])
        self.assertTrue(search["safe_self_damage_commit"])

    def test_self_damage_stays_rejected_when_combined_loss_breaks_reserve(self):
        attacker = monster(
            "ScalingEnemy",
            100,
            intent=Intent.ATTACK,
            damage=26,
            hits=1,
        )
        hemokinesis = card(
            "Hemokinesis",
            CardType.ATTACK,
            cost=1,
            damage=22,
            target=True,
        )
        game = GameStub(
            [attacker], [hemokinesis], hp=5, block=20, energy=1
        )
        game.player.max_hp = 85
        game.player.powers = [Power("Metallicize", "Metallicize", 4)]
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        # END loses two HP and retains three. Hemokinesis would pay two more
        # before that attack, leaving only one and violating the dynamic
        # reserve, so its attractive damage must not outrank the safer line.
        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "no_positive_marginal_action",
            self.planner.last_decision["reason"],
        )

    def test_self_damage_stays_rejected_when_card_cost_is_lethal(self):
        target = monster("Cultist", 100)
        hemokinesis = card(
            "Hemokinesis",
            CardType.ATTACK,
            cost=1,
            damage=22,
            target=True,
        )
        game = GameStub([target], [hemokinesis], hp=2, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "no_positive_marginal_action",
            self.planner.last_decision["reason"],
        )

    def test_new_brutality_is_rejected_when_next_turn_start_loss_is_lethal(self):
        target = monster("Boss", 100)
        brutality = card("Brutality", CardType.POWER, cost=0)
        game = GameStub([target], [brutality], hp=1, energy=0, act=2)
        game.player.max_hp = 85
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "no_positive_marginal_action",
            self.planner.last_decision["reason"],
        )

    def test_new_brutality_tracks_safe_short_fight_cost(self):
        target = monster("Boss", 25)
        brutality = card("Brutality", CardType.POWER, cost=0)
        game = GameStub([target], [brutality], hp=40, energy=0, act=2)
        game.player.max_hp = 85
        game.room_type = "MonsterRoomBoss"

        # A short fight needs an actual recurring damage source. The old
        # fixture contained only Brutality and passed on invented Act damage.
        game.draw_pile = [
            card("Strike_R", CardType.ATTACK, cost=1, damage=9),
            card("Strike_R", CardType.ATTACK, cost=1, damage=9),
        ]

        for index, attack in enumerate(game.draw_pile):
            attack.uuid = f"short-fight-attack-{index}"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, brutality)
        search = self.planner.last_decision["search"]
        self.assertEqual(1, search["projected_loss"])
        self.assertEqual(1, search["actual_loss"])
        self.assertEqual(1, search["extra_brutality_amount"])
        self.assertEqual(1, search["delayed_voluntary_hp_cost"])
        lifecycle = self.planner.last_decision["lifecycle"]
        self.assertEqual("brutality", lifecycle["kind"])
        self.assertEqual(2, lifecycle["expected_trigger_count"])

    def test_brutality_is_deferred_in_long_boss_with_gamblers_brew(self):
        guardian = monster(
            "TheGuardian", 195, intent=Intent.ATTACK, damage=9, hits=1
        )
        brutality = card("Brutality", CardType.POWER, cost=0)
        game = GameStub(
            [guardian], [brutality], hp=71, block=5, energy=0, act=1
        )
        game.player.max_hp = 80
        game.room_type = "MonsterRoomBoss"
        game.turn = 3
        game.potions = [
            SimpleNamespace(potion_id="GamblersBrew", can_use=True)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        lifecycle = self.planner.last_decision["rejected_lifecycle"]
        self.assertEqual("brutality", lifecycle["kind"])
        self.assertGreaterEqual(lifecycle["expected_remaining_turns"], 10)
        self.assertTrue(lifecycle["gambler_alternative"])
        self.assertLessEqual(lifecycle["adjustment"], 0.75)

    def test_new_combust_counts_only_its_actual_delayed_self_cost(self):
        target = monster("Boss", 100)
        combust = card("Combust", CardType.POWER, cost=1, magic=5)
        game = GameStub([target], [combust], hp=30, energy=1, act=2)
        game.player.max_hp = 85
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, combust)
        search = self.planner.last_decision["search"]
        self.assertEqual(1, search["projected_loss"])
        self.assertEqual(1, search["delayed_voluntary_hp_cost"])
        self.assertEqual(4, search["terminal_risk_budget"])
        lifecycle = self.planner.last_decision["lifecycle"]
        self.assertEqual("combust", lifecycle["kind"])
        self.assertGreater(
            lifecycle["expected_benefit"], lifecycle["expected_cost"]
        )

    def test_demon_form_uses_safe_collector_setup_window(self):
        """Replay: do not retain Demon Form through an entire long Boss."""

        collector = monster(
            "TheCollector", 235, intent=Intent.DEFEND_BUFF
        )
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=2
        )
        hand = [
            card("True Grit", CardType.SKILL, cost=1, block=7),
            demon_form,
            card(
                "Bash", CardType.ATTACK, cost=2, damage=10,
                target=True,
            ),
            card(
                "Strike_R_1", CardType.ATTACK, damage=9, target=True
            ),
            card(
                "Uppercut", CardType.ATTACK, cost=2, damage=13,
                target=True, magic=2,
            ),
            card(
                "Strike_R_2", CardType.ATTACK, damage=9, target=True
            ),
            card(
                "Strike_R_3", CardType.ATTACK, damage=9, target=True
            ),
            card(
                "Anger", CardType.ATTACK, cost=0, damage=6,
                target=True,
            ),
            card("Rage", CardType.SKILL, cost=0, magic=5),
            card(
                "Strike_R_4", CardType.ATTACK, damage=9, target=True
            ),
        ]
        game = GameStub(
            [collector], hand, hp=36, energy=3, act=2
        )
        game.room_type = "MonsterRoomBoss"
        game.turn = 8
        game.relics = [Relic("Runic Pyramid", "Runic Pyramid")]
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, demon_form)
        self.assertEqual("demonform", planner.last_decision["lifecycle"]["kind"])
        lifecycle = planner.last_decision["lifecycle"]
        self.assertGreaterEqual(lifecycle["expected_remaining_turns"], 6)
        self.assertLessEqual(lifecycle["expected_remaining_turns"], 8)

    @staticmethod
    def _demon_form_snecko_growth_game(enemy_hp=65, *, growth=True):
        powers = [Power("Strength", "Strength", 6)]
        if growth:
            powers.insert(
                0,
                Power("Generic Strength Up Power", "Growth", 3),
            )
        enemy = monster(
            "GrowingHallway",
            enemy_hp,
            intent=Intent.ATTACK,
            damage=21,
            hits=1,
            powers=powers,
        )
        demon_form = card(
            "Demon Form", CardType.POWER,
            cost=1, magic=3, upgrades=1,
        )
        cards = [demon_form]
        cards.extend(
            card(f"Defend_R_{index}", CardType.SKILL, block=5)
            for index in range(4)
        )
        for card_id, cost, damage in (
            ("Strike_R", 1, 8),
            ("Bash", 2, 12),
            ("Pommel Strike", 1, 11),
            ("Perfected Strike", 2, 12),
            ("Thunderclap", 1, 6),
            ("Whirlwind", -1, 5),
            ("Anger", 0, 8),
            ("Sword Boomerang", 1, 5),
            ("Iron Wave", 1, 9),
        ):
            cards.append(card(
                card_id,
                CardType.ATTACK,
                cost=cost,
                damage=damage,
                target=card_id not in {
                    "Thunderclap", "Whirlwind", "Sword Boomerang",
                },
                upgrades=1 if card_id == "Sword Boomerang" else 0,
            ))
        for card_id, cost, block in (
            ("Shrug It Off", 1, 8),
            ("Impervious", 2, 40),
            ("Flame Barrier", 2, 12),
        ):
            block_card = card(
                card_id, CardType.SKILL, cost=cost, block=block,
            )
            if card_id == "Impervious":
                block_card.exhausts = True
            cards.append(block_card)
        for card_id, cost in (
            ("Burning Pact", 1),
            ("Limit Break", 1),
            ("Havoc", 0),
            ("Dual Wield", 1),
        ):
            cards.append(card(card_id, CardType.SKILL, cost=cost))
        cards.extend(
            card(f"Ghostly_{index}", CardType.SKILL)
            for index in range(5)
        )
        game = GameStub(
            [enemy], cards, hp=40, energy=3, act=3,
        )
        game.player.max_hp = 40
        game.player.powers = [Power("Strength", "Strength", 2)]
        game.relics = [Relic("Snecko Eye", "Snecko Eye")]
        return game, demon_form

    def test_demon_form_horizon_calibrates_snecko_growth_hallway(self):
        game, demon_form = self._demon_form_snecko_growth_game()
        legacy_turns = self.planner._expected_remaining_turns(game)

        lifecycle = self.planner._lifecycle_evaluation(
            game, demon_form, total_loss=23,
        )

        self.assertEqual(7.0, self.planner._future_cards_seen_per_turn(game))
        self.assertEqual(2, lifecycle["triggers"])
        self.assertEqual(3, lifecycle["turns"])
        self.assertLess(lifecycle["turns"], legacy_turns)
        self.assertEqual(4, lifecycle["room_trigger_cap"])
        self.assertEqual(2, lifecycle["kill_clock_trigger_cap"])
        self.assertGreaterEqual(
            lifecycle["future_attack_damage_per_turn"], 18.0,
        )
        self.assertEqual(3.0, lifecycle["enemy_attack_growth_per_turn"])

    def test_demon_form_growth_cost_is_current_intent_invariant(self):
        growing_game, growing_form = self._demon_form_snecko_growth_game(
            enemy_hp=120, growth=True,
        )
        flat_game, flat_form = self._demon_form_snecko_growth_game(
            enemy_hp=120, growth=False,
        )

        growing = self.planner._lifecycle_evaluation(
            growing_game, growing_form, total_loss=23,
        )
        flat = self.planner._lifecycle_evaluation(
            flat_game, flat_form, total_loss=23,
        )

        self.assertEqual(growing["triggers"], flat["triggers"])
        # Serialized card damage is already the current displayed value.  The
        # invariant is equality across intent/growth frames, and the 120-HP
        # kill clock therefore remains on the fourth future trigger.
        self.assertEqual(4, growing["kill_clock_trigger_cap"])
        self.assertGreater(growing["enemy_growth_cost"], 0.0)
        self.assertEqual(0.0, flat["enemy_growth_cost"])
        self.assertLess(growing["adjustment"], flat["adjustment"])

        # A safe BUFF frame must not erase serialized recurring growth.
        growing_game.monsters[0].intent = Intent.BUFF
        growing_game.monsters[0].move_adjusted_damage = 0
        growing_game.monsters[0].move_hits = 0
        buff_frame = self.planner._lifecycle_evaluation(
            growing_game, growing_form, total_loss=23,
        )
        self.assertEqual(growing["triggers"], buff_frame["triggers"])
        self.assertEqual(
            growing["enemy_attack_growth_per_turn"],
            buff_frame["enemy_attack_growth_per_turn"],
        )
        self.assertEqual(
            growing["enemy_growth_cost"],
            buff_frame["enemy_growth_cost"],
        )

    def test_demon_form_growth_cost_respects_persisted_barricade_block(self):
        enemy = monster(
            "GrowingBoss", 500, intent=Intent.BUFF,
            powers=[Power("Growth", "Growth", 3)],
        )
        demon_form = card(
            "Demon Form", CardType.POWER,
            cost=3, magic=3, upgrades=1,
        )
        attacks = [
            card(
                f"Heavy_{index}", CardType.ATTACK,
                cost=1, damage=12, target=True,
            )
            for index in range(5)
        ]
        game = GameStub(
            [enemy], [demon_form] + attacks,
            hp=80, block=200, energy=3,
        )
        game.player.max_hp = 80
        game.player.powers = [Power("Barricade", "Barricade", 1)]
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(IroncladPriority())

        lifecycle = planner._lifecycle_evaluation(
            game, demon_form, total_loss=0,
        )
        action = planner.choose_card_action(game)

        self.assertGreater(lifecycle["enemy_growth_raw_debt"], 0.0)
        self.assertGreaterEqual(
            lifecycle["persistent_block_credit"],
            lifecycle["enemy_growth_raw_debt"],
        )
        self.assertEqual(0.0, lifecycle["enemy_growth_cost"])
        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, demon_form)

    def test_demon_form_recurring_units_exclude_exhaust_attacks(self):
        enemy = monster("LongBoss", 500, intent=Intent.BUFF)
        demon_form = card(
            "Demon Form", CardType.POWER,
            cost=3, magic=3, upgrades=1,
        )
        pummel = card(
            "Pummel", CardType.ATTACK,
            cost=1, damage=5, target=True, upgrades=1,
        )
        pummel.exhausts = True
        game = GameStub([enemy], [demon_form, pummel], hp=70, energy=3)
        game.room_type = "MonsterRoomBoss"

        lifecycle = self.planner._lifecycle_evaluation(
            game, demon_form, total_loss=0,
        )

        self.assertEqual(0.0, lifecycle["strength_units_per_turn"])
        self.assertEqual(0.0, lifecycle["benefit"])
        self.assertLess(lifecycle["adjustment"], 0.0)

    def test_demon_form_x_cost_units_use_future_energy_not_current_roll(self):
        enemy = monster("LongBoss", 500, intent=Intent.BUFF)
        demon_form = card(
            "Demon Form", CardType.POWER,
            cost=3, magic=3, upgrades=1,
        )
        whirlwind = card(
            "Whirlwind", CardType.ATTACK,
            cost=-1, damage=5, target=False,
        )
        game = GameStub([enemy], [demon_form, whirlwind], hp=70, energy=0)
        game.room_type = "MonsterRoomBoss"
        zero_energy_units = self.planner._future_strength_units_per_turn(game)
        game.player.energy = 10
        high_energy_units = self.planner._future_strength_units_per_turn(game)

        self.assertGreater(zero_energy_units, 0.0)
        self.assertEqual(zero_energy_units, high_energy_units)

    @staticmethod
    def _champ_demon_form_deck():
        """Return the persistent 17-card deck from the live Champ fight.

        CommunicationMod's persistent deck copies serialize ``damage=-1``;
        combat-zone copies with the same UUID carry the resolved damage.
        Keeping that distinction here is what reproduces seq 183038/183049.
        """

        cards = []

        def add(value, count=1):
            for _ in range(count):
                instance = card(
                    value.card_id,
                    value.type,
                    cost=value.cost,
                    damage=value.base_damage,
                    block=value.base_block,
                    target=value.has_target,
                    magic=value.magic_number,
                    upgrades=value.upgrades,
                )
                instance.uuid = f"champ-deck-{len(cards)}-{instance.card_id}"
                instance.damage = -1
                cards.append(instance)

        add(card(
            "Strike_R", CardType.ATTACK, cost=1, damage=9,
            target=True, upgrades=1,
        ), 3)
        add(card(
            "Defend_R", CardType.SKILL, cost=1, block=8,
            upgrades=1,
        ), 4)
        add(card(
            "Bash", CardType.ATTACK, cost=2, damage=10,
            target=True, magic=3, upgrades=1,
        ))
        add(card("Inflame", CardType.POWER, cost=1, magic=2))
        add(card(
            "Headbutt", CardType.ATTACK, cost=1, damage=9,
            target=True,
        ))
        add(card(
            "Heavy Blade", CardType.ATTACK, cost=2, damage=14,
            target=True, magic=3,
        ))
        add(card(
            "Shrug It Off", CardType.SKILL, cost=1, block=8,
        ), 2)
        add(card("Flex", CardType.SKILL, cost=0, magic=2))
        add(card(
            "Sword Boomerang", CardType.ATTACK, cost=1,
            damage=3, magic=3,
        ))
        add(card(
            "Demon Form", CardType.POWER, cost=3, magic=3, upgrades=1,
        ))
        add(card(
            "Twin Strike", CardType.ATTACK, cost=1, damage=5,
            target=True,
        ))
        return cards

    @staticmethod
    def _bind_live_champ_combat_zones(game, persistent_deck):
        """Bind resolved combat copies to their persistent-deck UUIDs."""

        remaining = list(persistent_deck)
        for hand_card in game.hand:
            master = next(
                value for value in remaining
                if value.card_id == hand_card.card_id
            )
            remaining.remove(master)
            hand_card.uuid = master.uuid

        # Inflame was already played and is therefore absent from all cycling
        # combat zones, even though it remains in the persistent master deck.
        inflame = next(
            value for value in remaining if value.card_id == "Inflame"
        )
        remaining.remove(inflame)
        game.deck = persistent_deck
        game.draw_pile = []
        for master in remaining:
            combat_copy = card(
                master.card_id,
                master.type,
                cost=master.cost,
                damage=max(0, master.base_damage),
                block=max(0, master.base_block),
                target=master.has_target,
                magic=master.magic_number,
                upgrades=master.upgrades,
            )
            combat_copy.uuid = master.uuid
            game.draw_pile.append(combat_copy)

    @staticmethod
    def _configure_live_champ_frame(game, *, turn):
        game.room_type = "MonsterRoomBoss"
        game.floor = 33
        game.turn = turn
        game.player.max_hp = 80
        game.relics = [
            Relic(relic_id, relic_id)
            for relic_id in (
                "Burning Blood", "MealTicket", "Boot", "Orichalcum",
                "Philosopher's Stone", "Nunchaku", "Bag of Marbles",
                "Self Forming Clay", "Regal Pillow",
            )
        ]

    def test_known_combat_cards_does_not_revive_exhausted_master_deck(self):
        exhausted = card(
            "Fiend Fire", CardType.ATTACK, cost=2, damage=7, target=True,
        )
        game = GameStub([monster("Boss", 100)], [], hp=40, energy=3)
        game.deck = [exhausted]
        game.draw_pile = []
        game.discard_pile = []
        game.exhaust_pile = [exhausted]

        self.assertEqual([], self.planner._known_combat_cards(game))

    def test_known_combat_cards_falls_back_for_deck_only_fixture(self):
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True,
        )
        partial_fixture = SimpleNamespace(deck=[strike])

        self.assertEqual(
            [strike], self.planner._known_combat_cards(partial_fixture)
        )

    def test_demon_form_uses_live_champ_turn_four_safe_scaling_window(self):
        """Exact A2F33 T4 replay from attempt 82be8602, seq 183038."""

        champ = monster(
            "Champ", 321, intent=Intent.DEBUFF,
            powers=[
                Power("Strength", "Strength", 1),
                Power("Vulnerable", "Vulnerable", 1),
            ],
        )
        champ.max_hp = 420
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=3, upgrades=1,
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=11,
            target=True, upgrades=1,
        )
        boomerang = card(
            "Sword Boomerang", CardType.ATTACK, cost=1,
            damage=5, magic=3,
        )
        heavy_blade = card(
            "Heavy Blade", CardType.ATTACK, cost=2, damage=20,
            target=True, magic=3,
        )
        defend = card(
            "Defend_R", CardType.SKILL, cost=1, block=6, upgrades=1,
        )
        game = GameStub(
            [champ],
            [demon_form, strike, boomerang, heavy_blade, defend],
            hp=62, block=3, energy=4, act=2,
        )
        game.player.powers = [
            Power("Strength", "Strength", 2),
            Power("Vulnerable", "Vulnerable", 1),
            Power("Frail", "Frail", 1),
        ]
        self._bind_live_champ_combat_zones(
            game, self._champ_demon_form_deck()
        )
        self.assertEqual(11, len(game.draw_pile))
        self._configure_live_champ_frame(game, turn=4)
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, demon_form)
        self.assertEqual("demonform", planner.last_decision["lifecycle"]["kind"])
        self.assertGreater(planner._future_strength_units_per_turn(game), 0)
        self.assertGreater(
            planner.last_decision["lifecycle"]["expected_benefit"], 0
        )
        self.assertEqual(0, planner.last_decision["search"]["actual_loss"])

    def test_demon_form_counts_base_damage_from_unresolved_draw_pile(self):
        """Replay F45: off-hand ``damage=-1`` must not erase scaling value."""

        giant_head = monster("GiantHead", 625, intent=Intent.BUFF)
        demon_form = card(
            "Demon Form", CardType.POWER,
            cost=3, magic=3, upgrades=1,
        )
        defends = [
            card(
                f"Defend_R_{index}", CardType.SKILL,
                cost=1, block=8,
            )
            for index in range(4)
        ]
        future_attack = card(
            "Heavy Blade", CardType.ATTACK,
            cost=2, damage=14, target=True, magic=3,
        )
        # CommunicationMod only resolves dynamic damage for cards in hand.
        # The upgraded base value remains authoritative in cycling zones.
        future_attack.damage = -1
        game = GameStub(
            [giant_head], [demon_form, *defends],
            hp=70, energy=5, act=3,
        )
        game.room_type = "MonsterRoomElite"
        game.floor = 45
        game.turn = 1
        game.draw_pile = [future_attack]
        game.discard_pile = []
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, demon_form)
        self.assertGreater(planner._future_strength_units_per_turn(game), 0)
        lifecycle = planner.last_decision["lifecycle"]
        self.assertEqual("demonform", lifecycle["kind"])
        self.assertGreater(lifecycle["expected_benefit"], 0)

    def test_demon_form_uses_live_champ_turn_six_safe_scaling_window(self):
        """Exact A2F33 T6 replay from attempt 82be8602, seq 183049."""

        champ = monster(
            "Champ", 236, intent=Intent.BUFF,
            powers=[Power("Strength", "Strength", 1)],
        )
        champ.max_hp = 420
        defend_one = card(
            "Defend_R", CardType.SKILL, cost=1, block=8, upgrades=1,
        )
        twin_strike = card(
            "Twin Strike", CardType.ATTACK, cost=1, damage=5, target=True,
        )
        headbutt = card(
            "Headbutt", CardType.ATTACK, cost=1, damage=8, target=True,
        )
        defend_two = card(
            "Defend_R", CardType.SKILL, cost=1, block=8, upgrades=1,
        )
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=3, upgrades=1,
        )
        game = GameStub(
            [champ],
            [defend_one, twin_strike, headbutt, defend_two, demon_form],
            hp=61, block=3, energy=4, act=2,
        )
        game.player.powers = [
            Power("Strength", "Strength", 2),
            Power("Vulnerable", "Vulnerable", 1),
            Power("Weakened", "Weakened", 1),
        ]
        self._bind_live_champ_combat_zones(
            game, self._champ_demon_form_deck()
        )
        self.assertEqual(11, len(game.draw_pile))
        self._configure_live_champ_frame(game, turn=6)
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, demon_form)
        self.assertEqual("demonform", planner.last_decision["lifecycle"]["kind"])
        self.assertGreater(planner._future_strength_units_per_turn(game), 0)
        self.assertGreater(
            planner.last_decision["lifecycle"]["expected_benefit"], 0
        )
        self.assertEqual(0, planner.last_decision["search"]["actual_loss"])

    def test_champ_phase_crossing_still_requires_immediate_defense(self):
        champ = monster(
            "Champ", 218, intent=Intent.ATTACK, damage=30, hits=1,
        )
        champ.max_hp = 420
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=3, upgrades=1,
        )
        phase_attack = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=8, target=True,
        )
        defend = card("Defend_R", CardType.SKILL, cost=1, block=5)
        game = GameStub(
            [champ], [demon_form, phase_attack, defend], hp=30, energy=3, act=2,
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIsNot(action.card, demon_form)
        self.assertEqual(
            [], planner.last_decision["search"]["action_suppressed_enemy_indexes"]
        )
        self.assertEqual(25, planner.last_decision["search"]["actual_loss"])

    def test_demon_form_does_not_replace_emergency_defense(self):
        collector = monster(
            "TheCollector", 235, intent=Intent.ATTACK,
            damage=20, hits=1,
        )
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=2
        )
        defend_one = card(
            "Defend_R_1", CardType.SKILL, cost=1, block=8
        )
        defend_two = card(
            "Defend_R_2", CardType.SKILL, cost=1, block=8
        )
        game = GameStub(
            [collector], [demon_form, defend_one, defend_two],
            hp=20, energy=3, act=2,
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIn(action.card, [defend_one, defend_two])
        self.assertEqual(4, planner.last_decision["search"]["actual_loss"])

    def test_second_wind_prices_exhausted_demon_form_but_still_survives(self):
        """Replay F27: losing Demon Form is costly, but saving 10 HP wins."""

        guardian = monster(
            "SphericGuardian", 20, intent=Intent.ATTACK,
            damage=10, hits=2,
        )
        guardian.block = 20
        second_wind = card(
            "Second Wind", CardType.SKILL, cost=1, block=5, upgrades=1,
        )
        second_wind.base_block = 7
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=2,
        )
        defend = card("Defend_R", CardType.SKILL, cost=1, block=5)
        bash = card(
            "Bash", CardType.ATTACK, cost=2, damage=8, target=True,
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True,
        )
        game = GameStub(
            [guardian], [second_wind, bash, strike, defend, demon_form],
            hp=51, energy=4, act=2,
        )
        game.floor = 27
        game.turn = 3
        game.player.max_hp = 80
        game.player.powers = [
            Power("Strength", "Strength", 3),
            Power("Frail", "Frail", 5),
        ]
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, second_wind)
        search = planner.last_decision["search"]
        self.assertEqual(10, search["actual_loss"])
        self.assertGreater(search["forced_exhaust_lifecycle_cost"], 0)
        self.assertEqual(
            ["Demon Form"],
            [
                item["card_id"]
                for item in search["forced_exhaust_lifecycle_cards"]
            ],
        )

    def test_demon_form_does_not_replace_short_boss_kill(self):
        collector = monster(
            "TheCollector", 18, intent=Intent.DEFEND_BUFF
        )
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=2
        )
        strikes = [
            card(
                f"Strike_R_{index}", CardType.ATTACK, cost=1,
                damage=6, target=True,
            )
            for index in range(3)
        ]
        game = GameStub(
            [collector], [demon_form, *strikes], hp=40,
            energy=3, act=2,
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIn(action.card, strikes)
        self.assertEqual(
            "guaranteed_attack_combo_lethal",
            planner.last_decision["reason"],
        )

    def test_upgraded_demon_form_has_larger_future_strength_value(self):
        base = card(
            "Demon Form", CardType.POWER, cost=3, magic=2
        )
        upgraded = card(
            "Demon Form+", CardType.POWER, cost=3, magic=3,
            upgrades=1,
        )

        def lifecycle(demon_form):
            attacks = [
                card(
                    f"Strike_{index}", CardType.ATTACK, damage=6,
                    target=True,
                )
                for index in range(4)
            ]
            game = GameStub(
                [monster("TheCollector", 120)],
                [demon_form, *attacks], hp=50, energy=3, act=2,
            )
            game.room_type = "MonsterRoomBoss"
            return self.planner._lifecycle_evaluation(
                game, demon_form, total_loss=0
            )

        base_lifecycle = lifecycle(base)
        upgraded_lifecycle = lifecycle(upgraded)

        self.assertEqual(
            base_lifecycle["triggers"], upgraded_lifecycle["triggers"]
        )
        self.assertEqual(2, base_lifecycle["strength_per_trigger"])
        self.assertEqual(3, upgraded_lifecycle["strength_per_trigger"])
        self.assertGreater(
            upgraded_lifecycle["benefit"], base_lifecycle["benefit"]
        )

    def test_demon_form_future_attack_value_respects_energy_budget(self):
        attacks = [
            card(
                f"Expensive Attack {index}",
                CardType.ATTACK,
                cost=3,
                damage=9,
                target=True,
            )
            for index in range(5)
        ]
        game = GameStub(
            [monster("TheCollector", 300)],
            attacks,
            hp=60,
            energy=3,
            act=2,
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(IroncladPriority())

        three_energy_units = planner._future_strength_units_per_turn(game)
        game.relics = [Relic("Fusion Hammer", "Fusion Hammer")]
        four_energy_units = planner._future_strength_units_per_turn(game)

        self.assertAlmostEqual(1.0, three_energy_units, places=3)
        self.assertAlmostEqual(4.0 / 3.0, four_energy_units, places=3)

    def test_demon_form_prices_awakened_phase_two_before_rebirth(self):
        awakened = monster(
            "AwakenedOne", 40, intent=Intent.ATTACK,
            damage=20, hits=1,
            powers=[
                Power("CuriosityPower", "Curiosity", 1),
                Power("Unawakened", "Unawakened", -1),
            ],
        )
        awakened.max_hp = 300
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=3,
            upgrades=1,
        )
        strikes = [
            card(
                f"Strike_R_{index}", CardType.ATTACK, damage=9,
                target=True,
            )
            for index in range(3)
        ]
        game = GameStub(
            [awakened], [demon_form, *strikes], hp=60,
            block=30, energy=3, act=3,
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, demon_form)
        lifecycle = planner.last_decision["lifecycle"]
        self.assertGreaterEqual(lifecycle["expected_remaining_turns"], 7)
        self.assertGreater(lifecycle["expected_cost"], 0)

    def test_demon_form_has_no_curiosity_cost_in_awakened_phase_two(self):
        awakened = monster(
            "AwakenedOne", 300, intent=Intent.ATTACK,
            damage=20, hits=1,
            powers=[Power("Regenerate", "Regenerate", 10)],
        )
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=3,
            upgrades=1,
        )
        strikes = [
            card(
                f"Strike_R_{index}", CardType.ATTACK, damage=9,
                target=True,
            )
            for index in range(3)
        ]
        game = GameStub(
            [awakened], [demon_form, *strikes], hp=60,
            block=25, energy=3, act=3,
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, demon_form)
        self.assertEqual(
            0, planner.last_decision["lifecycle"]["expected_cost"]
        )

    def test_demon_form_does_not_add_strength_to_same_turn_attack(self):
        collector = monster(
            "TheCollector", 400, intent=Intent.DEFEND_BUFF
        )
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=2
        )
        heavy_blade = card(
            "Heavy Blade", CardType.ATTACK, cost=2, damage=14,
            target=True, magic=3,
        )
        game = GameStub(
            [collector], [demon_form, heavy_blade], hp=60,
            energy=5, act=2,
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, demon_form)
        self.assertEqual(
            ["Demon Form", "Heavy Blade"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(
            [386], planner.last_decision["search"]["final_enemy_hp"]
        )
        self.assertEqual(
            0, planner.last_decision["search"]["player_strength_bonus"]
        )

    def test_combust_cannot_label_buffer_exposed_enemy_attack_as_self_cost(self):
        attacker = monster(
            "Boss", 100, intent=Intent.ATTACK, damage=10, hits=1
        )
        combust = card("Combust", CardType.POWER, cost=1, magic=5)
        game = GameStub([attacker], [combust], hp=20, energy=1, act=2)
        game.player.max_hp = 85
        game.player.powers = [Power("Buffer", "Buffer", 1)]
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        # Waiting lets Buffer absorb the attack. Combust's own one-point
        # event consumes Buffer first and exposes ten enemy damage; none of
        # that downstream attack belongs in the voluntary-cost allowance.
        self.assertIsInstance(action, EndTurnAction)

    def test_voluntary_self_cost_does_not_hide_heart_beat_loss(self):
        heart = monster(
            "CorruptHeart",
            300,
            powers=[Power("BeatOfDeathPower", "Beat of Death", 2)],
        )
        hemokinesis = card(
            "Hemokinesis",
            CardType.ATTACK,
            cost=1,
            damage=22,
            target=True,
        )
        game = GameStub([heart], [hemokinesis], hp=10, energy=1, act=4)
        game.player.max_hp = 85
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "no_positive_marginal_action",
            self.planner.last_decision["reason"],
        )

    def test_self_damage_does_not_hide_reserve_breaking_thorns(self):
        spiker = monster(
            "Spiker",
            100,
            powers=[Power("Thorns", "Thorns", 2)],
        )
        hemokinesis = card(
            "Hemokinesis",
            CardType.ATTACK,
            cost=1,
            damage=22,
            target=True,
        )
        game = GameStub([spiker], [hemokinesis], hp=5, energy=1, act=2)
        game.player.max_hp = 85
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_guardian_mode_shift_block_absorbs_later_multihits(self):
        guardian = monster(
            "TheGuardian",
            100,
            intent=Intent.ATTACK,
            damage=24,
            hits=1,
            powers=[Power("Mode Shift", "Mode Shift", 6)],
        )
        pummel = card(
            "Pummel", CardType.ATTACK, damage=5, target=True
        )
        game = GameStub([guardian], [pummel], hp=20, energy=1)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        # Four five-damage hits: the first two deal ten and trigger Mode
        # Shift; the remaining ten damage is absorbed by the new 20 Block.
        self.assertEqual([90], search["final_enemy_hp"])
        self.assertEqual([10], search["final_enemy_block"])
        self.assertEqual(0, search["actual_loss"])

    def test_guardian_mode_shift_counts_choke_before_later_attack(self):
        guardian = monster(
            "TheGuardian",
            100,
            intent=Intent.ATTACK,
            damage=24,
            hits=1,
            powers=[
                Power("Mode Shift", "Mode Shift", 6),
                Power("Choke", "Choke", 3),
            ],
        )
        attacks = [
            card(f"Strike-{index}", CardType.ATTACK, damage=3, target=True)
            for index in range(2)
        ]
        game = GameStub([guardian], attacks, hp=20, energy=2)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        # First attack plus its Choke trigger consume the six-point counter.
        # The second attack hits the new 20 Block, then Choke bypasses it.
        self.assertEqual([91], search["final_enemy_hp"])
        self.assertEqual([17], search["final_enemy_block"])
        self.assertEqual([0], search["action_suppressed_enemy_indexes"])

    def test_panache_countdown_counts_only_crossed_speculative_triggers(self):
        game = GameStub([monster("Cultist", 100)], [], hp=20, energy=3)
        game.player.powers = [Power("Panache", "Panache", 3)]

        self.assertEqual(0, self.planner._panache_trigger_count(game, 0, 2))
        self.assertEqual(1, self.planner._panache_trigger_count(game, 0, 3))
        self.assertEqual(1, self.planner._panache_trigger_count(game, 2, 7))
        self.assertEqual(2, self.planner._panache_trigger_count(game, 0, 8))

    def test_live_choked_id_counts_before_later_attack(self):
        guardian = monster(
            "TheGuardian",
            100,
            intent=Intent.ATTACK,
            damage=24,
            hits=1,
            powers=[
                Power("Mode Shift", "Mode Shift", 6),
                Power("Choked", "Choked", 3),
            ],
        )
        attacks = [
            card(f"Strike-{index}", CardType.ATTACK, damage=3, target=True)
            for index in range(2)
        ]
        game = GameStub([guardian], attacks, hp=20, energy=2)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual([91], search["final_enemy_hp"])
        self.assertEqual([17], search["final_enemy_block"])
        self.assertEqual([0], search["action_suppressed_enemy_indexes"])

    def test_guardian_mode_shift_counts_end_turn_lightning(self):
        guardian = monster(
            "TheGuardian",
            100,
            intent=Intent.ATTACK,
            damage=24,
            hits=1,
            powers=[Power("Mode Shift", "Mode Shift", 6)],
        )
        jab = card("Jab", CardType.ATTACK, damage=1, target=True)
        game = GameStub([guardian], [jab], hp=10, energy=1)
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 8)]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual([91], search["final_enemy_hp"])
        self.assertEqual([20], search["final_enemy_block"])
        self.assertEqual(0, search["actual_loss"])

    def test_guardian_mode_shift_counts_poison_hp_loss(self):
        guardian = monster(
            "TheGuardian",
            100,
            intent=Intent.ATTACK,
            damage=24,
            hits=1,
            powers=[
                Power("Mode Shift", "Mode Shift", 6),
                Power("Poison", "Poison", 6),
            ],
        )
        jab = card("Jab", CardType.ATTACK, damage=1, target=True)
        game = GameStub([guardian], [jab], hp=10, energy=1)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual([93], search["final_enemy_hp"])
        self.assertEqual([20], search["final_enemy_block"])
        self.assertEqual(0, search["actual_loss"])

    def test_guardian_mode_shift_counts_corpse_explosion_once(self):
        guardian = monster(
            "TheGuardian",
            100,
            intent=Intent.ATTACK,
            damage=24,
            hits=1,
            powers=[Power("Mode Shift", "Mode Shift", 6)],
        )
        corpse = monster(
            "Cultist",
            1,
            powers=[Power("Corpse Explosion", "Corpse Explosion", 1)],
        )
        corpse.max_hp = 10
        cleave = card("Cleave", CardType.ATTACK, damage=1, target=False)
        game = GameStub([guardian, corpse], [cleave], hp=10, energy=1)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        # Cleave deals one, then the ten-point explosion triggers Mode Shift.
        # The corpse must not detonate again in later terminal phases.
        self.assertEqual([89, 0], search["final_enemy_hp"])
        self.assertEqual([20, 0], search["final_enemy_block"])
        self.assertEqual(0, search["actual_loss"])

    def test_unblockable_hp_loss_still_respects_intangible(self):
        for power in (
            Power("Poison", "Poison", 6),
            Power("Choke", "Choke", 3),
        ):
            with self.subTest(power=power.power_id):
                planner = FastCombatPlanner(PrioritiesStub())
                nemesis = monster(
                    "Nemesis",
                    100,
                    powers=[
                        Power("Intangible", "Intangible", 1),
                        power,
                    ],
                )
                jab = card(
                    "Jab", CardType.ATTACK, damage=1, target=True
                )
                game = GameStub([nemesis], [jab], hp=20, energy=1)

                planner.choose_card_action(game)

                # The attack and the HP-loss packet are separate events, but
                # Intangible caps each of them at one.
                self.assertEqual(
                    [98], planner.last_decision["search"]["final_enemy_hp"]
                )

    def test_large_slime_split_cancels_current_attack(self):
        acid = monster(
            "AcidSlime_L", 36, intent=Intent.ATTACK, damage=16, hits=1
        )
        acid.max_hp = 70
        strike = card("Strike", CardType.ATTACK, damage=6, target=True)
        defend = card("Defend", CardType.SKILL, block=5)
        game = GameStub([acid], [strike, defend], hp=20, energy=1)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["actual_loss"])
        self.assertEqual([30], search["final_enemy_hp"])
        self.assertEqual([0], search["split_pending_enemy_indexes"])
        self.assertEqual(60, search["future_spawn_hp"])

    def test_slime_control_target_does_not_replace_damage_focus(self):
        spike = monster(
            "SpikeSlime_L", 61, intent=Intent.ATTACK, damage=16, hits=1
        )
        acid = monster("AcidSlime_L", 49)
        spike.max_hp = 70
        acid.max_hp = 70
        neutralize = card(
            "Neutralize",
            CardType.ATTACK,
            cost=0,
            damage=3,
            target=True,
            magic=1,
        )
        game = GameStub([spike, acid], [neutralize], hp=30, energy=3)

        control = self.planner.choose_card_action(game)

        self.assertIs(control.target_monster, spike)
        # The planner has no in-memory focus here, as after a controller
        # restart.  It must recover the more advanced Acid damage line from
        # authoritative HP, while still sending the control card at Spike.
        self.assertEqual(("acidslimel", 1), self.planner.focus_key)

        next_spike = monster(
            "SpikeSlime_L", 48, intent=Intent.ATTACK, damage=16, hits=1
        )
        next_acid = monster("AcidSlime_L", 39)
        next_spike.max_hp = 70
        next_acid.max_hp = 70
        choke = card(
            "Choke", CardType.ATTACK, cost=2, damage=12,
            target=True, magic=3,
        )
        next_game = GameStub(
            [next_spike, next_acid], [choke], hp=28, energy=3
        )

        damage = self.planner.choose_card_action(next_game)

        self.assertIs(damage.target_monster, next_acid)
        self.assertEqual(
            27,
            self.planner.last_decision["search"]["final_enemy_hp"][1],
        )

    def test_low_hp_slime_focus_creates_next_turn_shiv_kill_window(self):
        idle_spike = monster("SpikeSlime_M", 27, intent=Intent.DEBUFF)
        idle_spike.max_hp = 30
        attacking_spike = monster(
            "SpikeSlime_M", 30,
            intent=Intent.ATTACK_DEBUFF, damage=8, hits=1,
        )
        attacking_spike.max_hp = 30
        tackle_acid = monster(
            "AcidSlime_M", 29,
            intent=Intent.ATTACK, damage=10, hits=1,
        )
        spit_acid = monster(
            "AcidSlime_M", 29,
            intent=Intent.ATTACK_DEBUFF, damage=7, hits=1,
        )
        defend = card("Defend_G", CardType.SKILL, cost=1, block=3)
        defend.base_block = 5
        dash = card(
            "Dash", CardType.ATTACK,
            cost=2, damage=10, block=7, target=True,
        )
        dash.base_block = 10
        shiv = card(
            "Shiv", CardType.ATTACK,
            cost=0, damage=8, target=True,
        )
        shiv.base_damage = 4
        dagger_spray = card(
            "Dagger Spray", CardType.ATTACK,
            cost=1, damage=4,
        )
        slimed_one = card("Slimed", CardType.STATUS, cost=1)
        slimed_one.uuid = "slimed-one"
        slimed_one.exhausts = True
        slimed_two = card("Slimed", CardType.STATUS, cost=1)
        slimed_two.uuid = "slimed-two"
        slimed_two.exhausts = True
        game = GameStub(
            [idle_spike, attacking_spike, tackle_acid, spit_acid],
            [shiv, slimed_one, slimed_two, defend, dash, dagger_spray],
            hp=15,
            energy=3,
            act=1,
        )
        game.turn = 10
        game.player.max_hp = 75
        game.room_type = "MonsterRoomBoss"
        game.ascension_level = 0
        game.player.powers = [
            Power("Thorns", "Thorns", 3),
            Power("InfiniteBladesPower", "Infinite Blades", 1),
            Power("AfterImagePower", "After Image", 1),
            Power("Accuracy", "Accuracy", 4),
            Power("Frail", "Frail", 2),
        ]
        game.relics = [Relic("Bronze Scales", "Bronze Scales")]
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        planned = planner.last_decision["planned_sequence"]
        self.assertCountEqual(
            ["Defend_G", "Dash", "Shiv"],
            [entry["card_id"] for entry in planned],
        )
        attack_targets = {
            tuple(entry["target_key"])
            for entry in planned
            if entry["card_id"] in {"Dash", "Shiv"}
        }
        self.assertEqual({("acidslimem", 3)}, attack_targets)
        search = planner.last_decision["search"]
        self.assertEqual(12, search["actual_loss"])
        self.assertEqual(13, search["final_player_block"])
        self.assertEqual([27, 27, 26, 8], search["final_enemy_hp"])
        self.assertEqual(9, search["player_thorns_reaction_damage"])
        self.assertEqual(0, search["bronze_scales_reaction_damage"])
        window = search["next_turn_finish_window"]
        self.assertTrue(window["eligible"])
        self.assertEqual("infinite_blades", window["source"])
        self.assertEqual(1, window["generated_cards"])
        self.assertEqual(8, window["damage_per_card"])
        self.assertEqual([3], window["selected_indexes"])
        self.assertEqual(10, window["attack_upper_bound_removed"])
        self.assertEqual(10.0, window["credit"])
        tiebreak = search["target_allocation_tiebreak"]
        self.assertTrue(tiebreak["applied"])
        self.assertEqual(0.0, tiebreak["baseline_credit"])
        self.assertEqual(10.0, tiebreak["selected_credit"])
        self.assertEqual(
            "same_cards_same_loss_same_total_progress",
            tiebreak["scope"],
        )

        game.player.powers = [
            power for power in game.player.powers
            if power.power_id != "InfiniteBladesPower"
        ]
        no_window_planner = FastCombatPlanner(SilentPriority())
        no_window_planner.choose_card_action(game)
        no_window_targets = {
            tuple(entry["target_key"])
            for entry in no_window_planner.last_decision["planned_sequence"]
            if entry["card_id"] in {"Dash", "Shiv"}
        }
        self.assertEqual({("spikeslimem", 0)}, no_window_targets)
        self.assertEqual(
            0.0,
            no_window_planner.last_decision["search"]
            ["next_turn_finish_window"]["credit"],
        )

    def test_one_infinite_blades_shiv_credits_only_one_slime(self):
        first = monster(
            "AcidSlime_M", 8,
            intent=Intent.ATTACK_DEBUFF, damage=7, hits=1,
        )
        second = monster(
            "AcidSlime_M", 8,
            intent=Intent.ATTACK_DEBUFF, damage=7, hits=1,
        )
        game = GameStub([first, second], [], hp=10, energy=0, act=1)
        game.ascension_level = 0
        game.player.powers = [
            Power("InfiniteBladesPower", "Infinite Blades", 1),
            Power("Accuracy", "Accuracy", 4),
        ]

        window = self.planner._slime_generated_shiv_finish_window(
            game,
            SimpleNamespace(player_accuracy=4),
            game.monsters,
            [8, 8],
            [0, 0],
        )

        self.assertEqual([1, 0], window["candidate_indexes"])
        self.assertEqual(1, len(window["selected_indexes"]))
        self.assertEqual(10, window["attack_upper_bound_removed"])
        self.assertEqual(10.0, window["potential_credit"])

    def test_future_weak_and_negative_strength_reduce_shiv_window(self):
        lick = monster("AcidSlime_M", 7, intent=Intent.DEBUFF)
        companion = monster("SpikeSlime_M", 30, intent=Intent.DEBUFF)
        game = GameStub([lick, companion], [], hp=10, energy=0, act=1)
        game.ascension_level = 0
        game.player.powers = [
            Power("InfiniteBladesPower", "Infinite Blades", 1),
            Power("Accuracy", "Accuracy", 4),
        ]
        state = SimpleNamespace(
            player_accuracy=4,
            player_strength_bonus=0,
        )

        weak_window = self.planner._slime_generated_shiv_finish_window(
            game, state, game.monsters, [7, 30], [0, 0]
        )

        self.assertEqual(6, weak_window["damage_per_card"])
        self.assertEqual([], weak_window["candidate_indexes"])
        weak_kill_window = self.planner._slime_generated_shiv_finish_window(
            game, state, game.monsters, [6, 30], [0, 0]
        )
        self.assertEqual([0], weak_kill_window["candidate_indexes"])

        lick.intent = Intent.ATTACK_DEBUFF
        lick.move_adjusted_damage = 7
        game.player.powers.append(Power("Strength", "Strength", -2))
        strength_window = self.planner._slime_generated_shiv_finish_window(
            game, state, game.monsters, [7, 30], [0, 0]
        )
        self.assertEqual(6, strength_window["damage_per_card"])
        self.assertEqual([], strength_window["candidate_indexes"])

        game.relics = [Relic("Runic Pyramid", "Runic Pyramid")]
        pyramid_window = self.planner._slime_generated_shiv_finish_window(
            game,
            SimpleNamespace(
                player_accuracy=4,
                player_strength_bonus=0,
                hand_size=5,
            ),
            game.monsters,
            [6, 30],
            [0, 0],
        )
        self.assertIsNone(pyramid_window["source"])
        self.assertEqual(0, pyramid_window["generated_cards"])

    def test_slime_boss_target_concentration_changes_next_turn_survival(self):
        def make_game(focused_spike_hp, dangerous_acid_hp):
            enemies = [
                monster(
                    "SpikeSlime_M", focused_spike_hp,
                    intent=Intent.ATTACK_DEBUFF, damage=8, hits=1,
                ),
                monster(
                    "SpikeSlime_M", 27,
                    intent=Intent.ATTACK_DEBUFF, damage=8, hits=1,
                ),
                monster("AcidSlime_M", 26, intent=Intent.DEBUFF),
                monster(
                    "AcidSlime_M", dangerous_acid_hp,
                    intent=Intent.ATTACK, damage=10, hits=1,
                ),
            ]
            cards = [
                card(
                    "Shiv", CardType.ATTACK,
                    cost=0, damage=8, target=True,
                ),
                card(
                    "Cloak And Dagger", CardType.SKILL,
                    cost=1, block=4, magic=1,
                ),
                card("Defend_G", CardType.SKILL, cost=1, block=3),
                card("Defend_G", CardType.SKILL, cost=1, block=3),
                card(
                    "Dagger Spray", CardType.ATTACK,
                    cost=1, damage=4,
                ),
            ]
            cards[0].base_damage = 4
            cards[1].base_block = 6
            cards[2].base_block = 5
            cards[2].uuid = "defend-one"
            cards[3].base_block = 5
            cards[3].uuid = "defend-two"
            value = GameStub(enemies, cards, hp=3, energy=3, act=1)
            value.turn = 11
            value.player.max_hp = 75
            value.room_type = "MonsterRoomBoss"
            value.ascension_level = 0
            value.player.powers = [
                Power("Thorns", "Thorns", 3),
                Power("InfiniteBladesPower", "Infinite Blades", 1),
                Power("AfterImagePower", "After Image", 1),
                Power("Accuracy", "Accuracy", 4),
                Power("Frail", "Frail", 1),
            ]
            return value

        old_focus_planner = FastCombatPlanner(SilentPriority())
        old_focus_planner.choose_card_action(make_game(9, 26))
        concentrated_planner = FastCombatPlanner(SilentPriority())
        concentrated_planner.choose_card_action(make_game(27, 8))

        old_search = old_focus_planner.last_decision["search"]
        concentrated_search = concentrated_planner.last_decision["search"]
        old_targets = {
            tuple(entry["target_key"])
            for entry in old_focus_planner.last_decision["planned_sequence"]
            if entry["target_key"] is not None
        }
        concentrated_targets = {
            tuple(entry["target_key"])
            for entry in concentrated_planner.last_decision["planned_sequence"]
            if entry["target_key"] is not None
        }
        self.assertIn(("spikeslimem", 0), old_targets)
        self.assertIn(("acidslimem", 3), concentrated_targets)
        self.assertEqual(0, concentrated_search["final_enemy_hp"][3])
        self.assertGreaterEqual(old_search["actual_loss"], 3)
        self.assertLess(concentrated_search["actual_loss"], 3)
        self.assertGreaterEqual(
            old_search["actual_loss"] - concentrated_search["actual_loss"],
            3,
        )
        self.assertEqual(0, old_search["tier"])
        self.assertGreater(concentrated_search["tier"], 0)

    def test_random_discard_attack_cannot_assume_expertise_survives(self):
        slime = monster("SlimeBoss", 71)
        slime.max_hp = 140
        all_out = card(
            "All Out Attack", CardType.ATTACK, damage=10, target=False
        )
        expertise = card(
            "Expertise", CardType.SKILL, magic=6
        )
        defends = [
            card(f"Defend-{index}", CardType.SKILL, block=7)
            for index in range(2)
        ]
        game = GameStub(
            [slime], [all_out, expertise, *defends], hp=31, energy=3
        )
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        sequence = [
            entry["card_id"]
            for entry in self.planner.last_decision["planned_sequence"]
        ]
        # Expertise is safe before the random discard and leaves enough energy
        # for the same split damage. The inverse order cannot guarantee that
        # Expertise is still in hand, so it is not a legal deterministic plan.
        self.assertIs(action.card, expertise)
        self.assertLess(
            sequence.index("Expertise"), sequence.index("All Out Attack")
        )
        self.assertTrue(
            self.planner.last_decision["search"]["random_hand_unknown"]
        )
        self.assertEqual(
            [61], self.planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_random_discard_is_exact_when_only_one_card_can_be_discarded(self):
        idle = monster("JawWorm", 40)
        all_out = card(
            "All Out Attack", CardType.ATTACK, damage=10, target=False
        )
        regret = card(
            "Regret", CardType.CURSE, cost=-2, playable=False
        )
        game = GameStub([idle], [all_out, regret], hp=20, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, all_out)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["remaining_hand_size"])
        self.assertEqual(0, search["projected_loss"])
        self.assertFalse(search["random_hand_unknown"])

    def test_random_discard_uses_exact_size_and_conservative_hazard(self):
        idle = monster("JawWorm", 40)
        all_out = card(
            "All Out Attack", CardType.ATTACK, damage=10, target=False
        )
        regret = card(
            "Regret", CardType.CURSE, cost=-2, playable=False
        )
        wound = card(
            "Wound", CardType.STATUS, cost=-2, playable=False
        )
        game = GameStub(
            [idle], [all_out, regret, wound], hp=20, energy=1
        )

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, all_out)
        search = self.planner.last_decision["search"]
        # One of two cards is random, so keep the harmful Regret as the safe
        # upper bound while reporting the exact one-card final hand.
        self.assertEqual(1, search["remaining_hand_size"])
        self.assertEqual(1, search["projected_loss"])
        self.assertTrue(search["random_hand_unknown"])

    def test_awakened_one_rebirth_does_not_trigger_fatal_reward(self):
        for feed_first in (False, True):
            with self.subTest(feed_first=feed_first):
                planner = FastCombatPlanner(PrioritiesStub())
                awakened = monster(
                    "AwakenedOne",
                    6,
                    powers=[
                        Power("Unawakened", "Unawakened", 1),
                        Power("Curiosity", "Curiosity", 1),
                    ],
                )
                awakened.max_hp = 300
                strike = card(
                    "Strike_R", CardType.ATTACK, damage=6, target=True
                )
                feed = card(
                    "Feed", CardType.ATTACK, damage=6, target=True
                )
                feed.exhausts = True
                hand = [feed, strike] if feed_first else [strike, feed]
                game = GameStub([awakened], hand, hp=30, energy=1)
                game.room_type = "MonsterRoomBoss"

                action = planner.choose_card_action(game)

                # Both cards force Rebirth, but Fatal does not trigger in
                # phase one. Preserve the exhausting Feed for phase two,
                # independently of serialized hand order.
                self.assertIs(action.card, strike)
                self.assertFalse(
                    planner.last_decision["search"]["true_combat_end"]
                )
                self.assertEqual(
                    [0], planner.last_decision["search"][
                        "reviving_enemy_indexes"
                    ]
                )

    def test_awakened_one_rebirth_still_triggers_corpse_explosion(self):
        awakened = monster(
            "AwakenedOne",
            1,
            powers=[
                Power("Unawakened", "Unawakened", 1),
                Power("Curiosity", "Curiosity", 1),
                Power("Corpse Explosion", "Corpse Explosion", 1),
            ],
        )
        awakened.max_hp = 300
        cultist = monster("Cultist", 100)
        cleave = card("Cleave", CardType.ATTACK, damage=1, target=False)
        game = GameStub(
            [awakened, cultist], [cleave], hp=30, energy=1
        )
        game.room_type = "MonsterRoomBoss"

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual([0, 0], search["final_enemy_hp"])
        self.assertEqual([0], search["reviving_enemy_indexes"])
        self.assertEqual(1, search["enemies_dead"])
        self.assertFalse(search["true_combat_end"])

    def test_darkling_knockdown_is_not_a_fatal_kill(self):
        for feed_first in (False, True):
            with self.subTest(feed_first=feed_first):
                planner = FastCombatPlanner(PrioritiesStub())
                weak = monster("Darkling", 6)
                strong = monster("Darkling", 100)
                weak.max_hp = strong.max_hp = 100
                strike = card(
                    "Strike_R", CardType.ATTACK, damage=6, target=True
                )
                feed = card(
                    "Feed", CardType.ATTACK, damage=6, target=True
                )
                feed.exhausts = True
                hand = [feed, strike] if feed_first else [strike, feed]
                game = GameStub([weak, strong], hand, hp=30, energy=1)

                action = planner.choose_card_action(game)

                self.assertIs(action.card, strike)
                search = planner.last_decision["search"]
                self.assertEqual([0], search["reviving_enemy_indexes"])
                self.assertEqual(0, search["enemies_dead"])
                self.assertFalse(search["true_combat_end"])

    def test_last_darkling_in_same_branch_can_claim_fatal_reward(self):
        for feed_first in (False, True):
            with self.subTest(feed_first=feed_first):
                planner = FastCombatPlanner(PrioritiesStub())
                darklings = [monster("Darkling", 6) for _ in range(2)]
                for enemy in darklings:
                    enemy.max_hp = 100
                strikes = [
                    card(
                        f"Strike-{index}",
                        CardType.ATTACK,
                        damage=6,
                        target=True,
                    )
                    for index in range(2)
                ]
                feed = card(
                    "Feed", CardType.ATTACK, damage=6, target=True
                )
                feed.exhausts = True
                hand = (
                    [feed, *strikes]
                    if feed_first
                    else [*strikes, feed]
                )
                game = GameStub(darklings, hand, hp=30, energy=2)

                planner.choose_card_action(game)

                sequence = [
                    entry["card_id"]
                    for entry in planner.last_decision["planned_sequence"]
                ]
                # The first knockdown is not Fatal; once only the last
                # Darkling remains, Feed is the strictly better second kill.
                self.assertTrue(sequence[0].startswith("Strike-"))
                self.assertEqual("Feed", sequence[1])
                self.assertTrue(
                    planner.last_decision["search"]["true_combat_end"]
                )

    def test_true_kill_claims_fatal_reward_independent_of_hand_order(self):
        for feed_first in (False, True):
            with self.subTest(feed_first=feed_first):
                planner = FastCombatPlanner(PrioritiesStub())
                cultist = monster("Cultist", 6)
                strike = card(
                    "Strike_R", CardType.ATTACK, damage=6, target=True
                )
                feed = card(
                    "Feed", CardType.ATTACK, damage=6, target=True
                )
                feed.exhausts = True
                hand = [feed, strike] if feed_first else [strike, feed]
                game = GameStub([cultist], hand, hp=30, energy=1)

                action = planner.choose_card_action(game)

                self.assertIs(action.card, feed)
                self.assertTrue(
                    planner.last_decision["search"]["true_combat_end"]
                )

    def test_ordinary_lethal_preserves_nonfatal_feed(self):
        cultist = monster("Cultist", 12, intent=Intent.STUN)
        strike = card(
            "Strike_R", CardType.ATTACK, damage=12, target=True
        )
        feed = card(
            "Feed", CardType.ATTACK, damage=10, target=True
        )
        feed.exhausts = True
        game = GameStub(
            [cultist], [feed, strike], hp=30, energy=2, act=2
        )
        game.draw_pile = []
        game.discard_pile = []

        action = FastCombatPlanner(IroncladPriority()).choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)

    def test_same_turn_chip_attack_precedes_feed_fatal(self):
        cultist = monster("Cultist", 12, intent=Intent.STUN)
        strike = card(
            "Strike_R", CardType.ATTACK, damage=6, target=True
        )
        feed = card(
            "Feed", CardType.ATTACK, damage=10, target=True
        )
        feed.exhausts = True
        game = GameStub(
            [cultist], [feed, strike], hp=30, energy=2, act=2
        )
        game.draw_pile = []
        game.discard_pile = []
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIs(action.card, strike)
        self.assertEqual(
            ["Strike_R", "Feed"],
            [
                entry["card_id"]
                for entry in planner.last_decision["planned_sequence"]
            ],
        )
        self.assertTrue(planner.last_decision["search"]["true_combat_end"])

    def test_safe_topdeck_feed_waits_one_turn_instead_of_killing(self):
        cultist = monster("Cultist", 6, intent=Intent.STUN)
        strike = card(
            "Strike_R", CardType.ATTACK, damage=6, target=True
        )
        feed = card(
            "Feed", CardType.ATTACK, damage=6, target=True
        )
        feed.exhausts = True
        game = GameStub([cultist], [strike], hp=30, energy=1, act=2)
        game.draw_pile = [feed]
        game.discard_pile = []
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual("wait_for_feed_fatal", planner.last_decision["reason"])
        self.assertEqual("Feed", planner.last_decision["feed_card_id"])
        self.assertEqual(0, planner.last_decision["projected_hp_loss"])
        self.assertEqual(1, planner.last_decision["wait_turns"])

        # The following authoritative turn contains the promised top card.
        # Even if the enemy now intends to attack, Feed must claim the true
        # kill before that intent rather than continue stalling.
        game.turn += 1
        game.hand = [feed]
        game.draw_pile = []
        game.player.energy = 3
        cultist.intent = Intent.ATTACK
        cultist.move_adjusted_damage = 30
        cultist.move_hits = 1

        followup = planner.choose_card_action(game)

        self.assertIsInstance(followup, PlayCardAction)
        self.assertIs(followup.card, feed)
        self.assertTrue(planner.last_decision["search"]["true_combat_end"])

    def test_feed_wait_fails_closed_when_next_turn_is_not_certain(self):
        cases = (
            ("enemy_can_act", Intent.BUFF, 6, 2, [], False),
            ("feed_not_on_top", Intent.STUN, 6, 2, ["cover"], False),
            ("feed_not_lethal", Intent.STUN, 7, 2, [], False),
            ("terminal_act", Intent.STUN, 6, 4, [], False),
            ("enemy_regenerates", Intent.STUN, 6, 2, [], True),
        )
        for name, intent, hp, act, cover, regenerates in cases:
            with self.subTest(name=name):
                enemy = monster("Cultist", hp, intent=intent)
                if regenerates:
                    enemy.powers = [
                        Power("Regeneration", "Regeneration", 1)
                    ]
                strike = card(
                    "Strike_R", CardType.ATTACK, damage=7, target=True
                )
                feed = card(
                    "Feed", CardType.ATTACK, damage=6, target=True
                )
                feed.exhausts = True
                draw_pile = [feed]
                if cover:
                    draw_pile.append(
                        card("Defend_R", CardType.SKILL, block=5)
                    )
                game = GameStub(
                    [enemy], [strike], hp=30, energy=1, act=act
                )
                game.draw_pile = draw_pile
                game.discard_pile = []
                planner = FastCombatPlanner(IroncladPriority())

                action = planner.choose_card_action(game)

                self.assertIsInstance(action, PlayCardAction)
                self.assertIs(action.card, strike)
                self.assertNotEqual(
                    "wait_for_feed_fatal", planner.last_decision["reason"]
                )

    def test_champ_cumulative_damage_cannot_replace_required_defense(self):
        champ = monster(
            "Champ", 221, intent=Intent.ATTACK, damage=30, hits=1
        )
        champ.max_hp = 420
        attacks = [
            card(f"Strike-{index}", CardType.ATTACK, damage=6, target=True)
            for index in range(2)
        ]
        defend = card("Defend", CardType.SKILL, block=5)
        game = GameStub([champ], [*attacks, defend], hp=40, energy=2)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        # Two Strikes cross half health but still take the full 30. The
        # survivable line spends the second energy on five real Block.
        self.assertEqual([215], search["final_enemy_hp"])
        self.assertEqual([], search["action_suppressed_enemy_indexes"])
        self.assertEqual(25, search["actual_loss"])

    def test_second_wind_counts_exhausted_non_attacks_as_block(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=30, hits=1)
        second_wind = card("Second Wind", CardType.SKILL, magic=-1)
        wounds = [
            card(f"Wound-{index}", CardType.STATUS, cost=-2, playable=False)
            for index in range(5)
        ]
        for wound in wounds:
            wound.card_id = "Wound"
        game = GameStub([attacker], [second_wind, *wounds], hp=10)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, second_wind)

    def test_no_block_survivor_cannot_claim_a_false_lethal_escape(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=12, hits=1
        )
        survivor = card("Survivor", CardType.SKILL, block=8)
        game = GameStub([attacker], [survivor], hp=7, energy=1)
        game.player.powers = [Power("NoBlockPower", "No Block", 1)]

        candidate = self.planner._candidate(
            game, survivor, None, None, 12, 12, 12
        )
        action = self.planner.choose_card_action(game)

        self.assertEqual(0, candidate.block_gain)
        self.assertEqual(0, candidate.state_block_mitigation)
        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            12, self.planner.last_decision["projected_hp_loss"]
        )

    def test_no_block_suppresses_after_image_rage_fnp_and_bandage_gains(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=12, hits=1
        )
        dagger = card(
            "Dagger Throw", CardType.ATTACK, damage=6, target=True
        )
        dagger.exhausts = True
        wound = card("Wound", CardType.STATUS, cost=-2, playable=False)
        game = GameStub([attacker], [dagger, wound], hp=20, energy=1)
        game.player.powers = [
            Power("NoBlockPower", "No Block", 1),
            Power("AfterImagePower", "After Image", 1),
            Power("RagePower", "Rage", 3),
            Power("FeelNoPainPower", "Feel No Pain", 4),
        ]
        fan = Relic("Ornamental Fan", "Ornamental Fan")
        fan.counter = 2
        game.relics = [
            fan,
            Relic("Tough Bandages", "Tough Bandages"),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, dagger)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["final_player_block"])
        self.assertEqual(12, search["actual_loss"])

    def test_no_block_suppresses_channelled_frost_in_ordered_plan(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=10, hits=1
        )
        cold_snap = card(
            "Cold Snap", CardType.ATTACK, damage=6, target=True
        )
        game = GameStub([attacker], [cold_snap], hp=20, energy=1)
        game.player.powers = [Power("NoBlockPower", "No Block", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, cold_snap)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["final_player_block"])
        self.assertEqual(10, search["actual_loss"])

    def test_no_block_ordered_plan_still_spends_preexisting_block(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=12, hits=1
        )
        strike = card(
            "Strike_R", CardType.ATTACK, damage=6, target=True
        )
        game = GameStub(
            [attacker], [strike], hp=20, block=5, energy=1
        )
        game.player.powers = [Power("NoBlockPower", "No Block", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        search = self.planner.last_decision["search"]
        self.assertEqual(7, search["actual_loss"])
        # Terminal diagnostics report block available before the attack; the
        # seven HP loss proves that the existing five was then spent once.
        self.assertEqual(5, search["final_player_block"])

    def test_second_wind_does_not_add_serialized_per_card_block_twice(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=20, hits=1)
        # CommunicationMod exposes Second Wind's per-card amount in ``block``.
        # It is not an extra five block granted before exhausting the hand.
        second_wind = card("Second Wind", CardType.SKILL, block=5)
        wounds = [
            card(f"Wound-{index}", CardType.STATUS, cost=-2, playable=False)
            for index in range(2)
        ]
        for wound in wounds:
            wound.card_id = "Wound"
        game = GameStub([attacker], [second_wind, *wounds], hp=20)

        candidate = self.planner._candidate(
            game,
            second_wind,
            None,
            None,
            incoming=20,
            attack_loss=20,
            total_loss=20,
        )

        self.assertEqual(10, candidate.mitigation)
        self.assertEqual(10, candidate.block_gain)

    def test_second_wind_uses_frail_adjusted_serialized_block(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=20, hits=1)
        second_wind = card("Second Wind", CardType.SKILL, block=3)
        second_wind.base_block = 5
        wounds = [
            card(f"Wound-{index}", CardType.STATUS, cost=-2, playable=False)
            for index in range(2)
        ]
        for wound in wounds:
            wound.card_id = "Wound"
        game = GameStub([attacker], [second_wind, *wounds], hp=20)

        candidate = self.planner._candidate(
            game,
            second_wind,
            None,
            None,
            incoming=20,
            attack_loss=20,
            total_loss=20,
        )

        self.assertEqual(6, candidate.mitigation)
        self.assertEqual(6, candidate.block_gain)

    def test_multi_card_exact_lethal_preempts_insufficient_defense(self):
        attacker = monster("Attacker", 12, intent=Intent.ATTACK, damage=50, hits=1)
        first_strike = card("Strike_R", CardType.ATTACK, damage=6, target=True)
        second_strike = card("Strike_R-2", CardType.ATTACK, damage=6, target=True)
        defend = card("Defend_R", CardType.SKILL, block=5)
        game = GameStub(
            [attacker],
            [first_strike, second_strike, defend],
            hp=10,
            energy=2,
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIn(action.card, [first_strike, second_strike])
        self.assertEqual(
            "guaranteed_attack_combo_lethal",
            self.planner.last_decision.get("reason"),
        )
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])
        self.assertEqual(1, self.planner.last_decision["search"]["enemies_dead"])
        self.assertEqual(2, self.planner.last_decision["search"]["tier"])

    def test_exact_combo_preserves_per_card_multihit_rounding(self):
        """Replay seq 465769: Pummel plus Strike leaves Slime Boss at 2."""

        slime_boss = monster(
            "SlimeBoss",
            58,
            powers=[Power("Vulnerable", "Vulnerable", 3)],
        )
        pummel = card(
            "Pummel", CardType.ATTACK,
            cost=1, damage=7, target=True,
        )
        pummel.magic_number = 4
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=11, target=True,
        )
        game = GameStub(
            [slime_boss], [pummel, strike], hp=90, energy=2, act=1
        )

        combo = self.planner._guaranteed_attack_combo(
            game,
            [pummel, strike],
            [slime_boss],
            total_loss=0,
            end_turn_loss=0,
        )

        self.assertIsNone(combo)

    def test_beat_of_death_is_reactive_card_hp_cost(self):
        heart = monster(
            "CorruptHeart",
            300,
            # Live CommunicationMod frames omit the conventional ``Power``
            # suffix and localize the display name.
            powers=[Power("BeatOfDeath", "Localized Beat", 1)],
        )
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub([heart], [strike], hp=20, energy=1, act=4)

        candidate = self.planner._candidate(
            game,
            strike,
            heart,
            None,
            incoming=0,
            attack_loss=0,
            total_loss=0,
        )

        self.assertEqual(1, candidate.self_hp_cost)
        self.assertEqual(1, candidate.reactive_hp_cost)

    def test_serialized_pen_nib_does_not_make_every_combo_attack_double(self):
        """Replay seq 206488: only the first visible doubled Attack is boosted."""

        guardian = monster(
            "TheGuardian",
            45,
            intent=Intent.ATTACK,
            damage=5,
            hits=1,
            powers=[
                Power("Vulnerable", "Vulnerable", 2),
                Power("Sharp Hide", "Sharp Hide", 3),
            ],
        )
        anger = card(
            "Anger", CardType.ATTACK, cost=0, damage=12, target=True
        )
        strikes = [
            card(
                f"Strike_R-{index}",
                CardType.ATTACK,
                cost=1,
                damage=12,
                target=True,
            )
            for index in range(2)
        ]
        defend = card("Defend_R", CardType.SKILL, cost=1, block=5)
        game = GameStub(
            [guardian],
            [anger, *strikes, defend],
            hp=40,
            block=5,
            energy=2,
            act=1,
        )
        # CommunicationMod serializes all visible Attacks with doubled damage
        # while the power is ready, although only the first Attack consumes it.
        game.player.powers = [Power("Pen Nib", "Pen Nib", 1)]

        self.planner.choose_card_action(game)

        self.assertNotEqual(
            "guaranteed_attack_combo_lethal",
            self.planner.last_decision.get("reason"),
        )
        self.assertFalse(self.planner.last_decision["search"]["true_combat_end"])
        self.assertGreater(
            self.planner.last_decision["search"]["final_enemy_hp"][0],
            0,
        )

    def test_pen_nib_counter_combo_falls_back_to_exact_ordered_lethal(self):
        """A counter-only ready frame still proves one boost, not zero or many."""

        target = monster(
            "Attacker",
            36,
            intent=Intent.ATTACK,
            damage=50,
            hits=1,
            powers=[Power("Vulnerable", "Vulnerable", 2)],
        )
        attacks = [
            card(
                f"Strike_R-{index}",
                CardType.ATTACK,
                cost=0 if index == 0 else 1,
                damage=6,
                target=True,
            )
            for index in range(3)
        ]
        game = GameStub(
            [target], attacks, hp=20, energy=2, act=2
        )
        game.relics = [Relic("Pen Nib", "Pen Nib", counter=9)]

        self.planner.choose_card_action(game)

        self.assertEqual(
            "ordered_turn_search",
            self.planner.last_decision.get("reason"),
        )
        self.assertTrue(self.planner.last_decision["search"]["true_combat_end"])
        self.assertEqual(
            [0], self.planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_fiend_fire_combo_does_not_double_count_cards_played_first(self):
        attacker = monster(
            "Attacker",
            30,
            intent=Intent.ATTACK,
            damage=15,
            hits=1,
        )
        bash = card("Bash", CardType.ATTACK, cost=2, damage=8, target=True)
        fiend_fire = card(
            "Fiend Fire",
            CardType.ATTACK,
            cost=2,
            damage=7,
            target=True,
        )
        defends = [
            card(f"Defend-{index}", CardType.SKILL, block=5)
            for index in range(3)
        ]
        game = GameStub(
            [attacker],
            [bash, fiend_fire, *defends],
            hp=4,
            energy=4,
        )

        combo = self.planner._guaranteed_attack_combo(
            game,
            [bash, fiend_fire],
            [attacker],
            total_loss=15,
            end_turn_loss=0,
        )

        # Bash leaves only three cards for Fiend Fire to exhaust:
        # 8 + (3 * 7) = 29, not the old false-positive 36.
        self.assertIsNone(combo)

    def test_fiend_fire_exact_combo_plays_other_attack_first(self):
        attacker = monster("Attacker", 29)
        strike = card("Strike_R", CardType.ATTACK, damage=8, target=True)
        fiend_fire = card(
            "Fiend Fire",
            CardType.ATTACK,
            damage=7,
            target=True,
        )
        defends = [
            card(f"Defend-{index}", CardType.SKILL, block=5)
            for index in range(3)
        ]
        game = GameStub(
            [attacker],
            [strike, fiend_fire, *defends],
            hp=20,
            energy=2,
        )

        combo = self.planner._guaranteed_attack_combo(
            game,
            [strike, fiend_fire],
            [attacker],
            total_loss=0,
            end_turn_loss=0,
        )

        self.assertIsNotNone(combo)
        self.assertIs(strike, combo[0])

    def test_exact_combo_combines_pain_cost_with_remaining_attack(self):
        target = monster("Cultist", 12)
        remaining_attacker = monster(
            "RemainingAttacker",
            40,
            intent=Intent.ATTACK,
            damage=3,
            hits=1,
        )
        attacks = [
            card("Strike-A", CardType.ATTACK, damage=6, target=True),
            card("Strike-B", CardType.ATTACK, damage=6, target=True),
        ]
        pain = card("Pain", CardType.CURSE, cost=-2, playable=False)
        game = GameStub(
            [target, remaining_attacker],
            [*attacks, pain],
            hp=5,
            energy=2,
        )

        combo = self.planner._guaranteed_attack_combo(
            game,
            attacks,
            [target, remaining_attacker],
            total_loss=3,
            end_turn_loss=0,
        )

        # Two Pain triggers plus the remaining 3 damage are exactly lethal.
        self.assertIsNone(combo)

    def test_awakened_one_first_phase_is_not_treated_as_combat_end(self):
        awakened = monster(
            "AwakenedOne",
            12,
            powers=[Power("Curiosity", "Curiosity", 1)],
        )
        attacks = [
            card("Strike-A", CardType.ATTACK, damage=6, target=True),
            card("Strike-B", CardType.ATTACK, damage=6, target=True),
        ]
        burn = card("Burn", CardType.STATUS, cost=-2, playable=False)
        game = GameStub(
            [awakened],
            [*attacks, burn],
            hp=2,
            energy=2,
        )

        combo = self.planner._guaranteed_attack_combo(
            game,
            attacks,
            [awakened],
            total_loss=2,
            end_turn_loss=2,
        )

        self.assertIsNone(combo)

    def test_main_beam_keeps_end_turn_damage_after_awakened_phase_one(self):
        awakened = monster(
            "AwakenedOne",
            6,
            intent=Intent.ATTACK,
            damage=20,
            hits=1,
            powers=[Power("Unawakened", "Unawakened", 1)],
        )
        strike = card(
            "Strike", CardType.ATTACK, damage=6, target=True
        )
        burn = card(
            "Burn", CardType.STATUS, cost=-2, playable=False
        )
        game = GameStub([awakened], [strike, burn], hp=2, energy=1)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertFalse(search["true_combat_end"])
        self.assertEqual([0], search["reviving_enemy_indexes"])
        self.assertEqual(2, search["projected_loss"])
        self.assertEqual(0, search["tier"])

    def test_awakened_one_second_phase_can_end_before_burn_ticks(self):
        awakened = monster("AwakenedOne", 12)
        attacks = [
            card("Strike-A", CardType.ATTACK, damage=6, target=True),
            card("Strike-B", CardType.ATTACK, damage=6, target=True),
        ]
        burn = card("Burn", CardType.STATUS, cost=-2, playable=False)
        game = GameStub(
            [awakened],
            [*attacks, burn],
            hp=2,
            energy=2,
        )

        combo = self.planner._guaranteed_attack_combo(
            game,
            attacks,
            [awakened],
            total_loss=2,
            end_turn_loss=2,
        )

        self.assertIsNotNone(combo)

    def test_exact_lethal_rejects_curl_up_reactive_block(self):
        louse = monster(
            "FuzzyLouseNormal",
            12,
            powers=[Power("Curl Up", "Curl Up", 3)],
        )
        attacks = [
            card("Strike-A", CardType.ATTACK, damage=6, target=True),
            card("Strike-B", CardType.ATTACK, damage=6, target=True),
        ]
        game = GameStub([louse], attacks, hp=20, energy=2)

        combo = self.planner._guaranteed_attack_combo(
            game,
            attacks,
            [louse],
            total_loss=0,
            end_turn_loss=0,
        )

        self.assertIsNone(combo)

    def test_ordered_search_applies_malleable_between_attacks(self):
        snake_plant = monster(
            "SnakePlant",
            14,
            powers=[
                Power("Malleable", "Malleable", 4),
                Power("Vulnerable", "Vulnerable", 1),
            ],
        )
        snake_plant.block = 3
        attacks = [
            card(
                f"Strike-{index}",
                CardType.ATTACK,
                damage=6,
                target=True,
            )
            for index in range(2)
        ]
        game = GameStub([snake_plant], attacks, hp=20, energy=2, act=2)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertFalse(search["true_combat_end"])
        self.assertEqual([3], search["final_enemy_hp"])
        self.assertEqual([5], search["final_enemy_block"])

    def test_ordered_search_applies_curl_up_after_first_attack(self):
        louse = monster(
            "FuzzyLouseNormal",
            14,
            powers=[Power("Curl Up", "Curl Up", 5)],
        )
        attacks = [
            card("Pommel Strike", CardType.ATTACK, damage=9, target=True),
            card("Strike_R", CardType.ATTACK, damage=6, target=True),
        ]
        game = GameStub([louse], attacks, hp=20, energy=2, act=1)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertFalse(search["true_combat_end"])
        self.assertEqual([4], search["final_enemy_hp"])

    def test_curl_up_waits_for_entire_multihit_card_to_finish(self):
        louse = monster(
            "FuzzyLouseNormal",
            20,
            powers=[Power("Curl Up", "Curl Up", 5)],
        )
        attacks = [
            card("Twin Strike", CardType.ATTACK, damage=5, target=True),
            card("Strike_R", CardType.ATTACK, damage=6, target=True),
        ]
        game = GameStub([louse], attacks, hp=20, energy=2, act=1)

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertFalse(search["true_combat_end"])
        self.assertEqual([9], search["final_enemy_hp"])

    def test_drawn_state_replans_and_preserves_authoritative_aoe_lethal(self):
        """Pommel draws must re-plan, while the refreshed lethal stays exact."""

        enemies = [
            monster(
                "FuzzyLouseDefensive",
                16,
                intent=Intent.DEBUFF,
                powers=[Power("Curl Up", "Curl Up", 6)],
            ),
            monster(
                "FuzzyLouseNormal",
                13,
                intent=Intent.BUFF,
                powers=[Power("Curl Up", "Curl Up", 3)],
            ),
            monster(
                "FuzzyLouseDefensive",
                16,
                intent=Intent.ATTACK,
                damage=6,
                hits=1,
                powers=[Power("Curl Up", "Curl Up", 4)],
            ),
        ]
        pommel = card(
            "Pommel Strike",
            CardType.ATTACK,
            cost=1,
            damage=10,
            target=True,
            magic=2,
            upgrades=1,
        )
        bash = card(
            "Bash", CardType.ATTACK, cost=2, damage=10,
            target=True, magic=3, upgrades=1,
        )
        whirlwind = card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=8,
            upgrades=1,
        )
        defend = card(
            "Defend_R", CardType.SKILL, cost=1, block=8, upgrades=1,
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=9,
            target=True, upgrades=1,
        )
        game = GameStub(
            enemies, [pommel, bash, whirlwind, defend, strike],
            hp=61, energy=3, act=1,
        )
        game.turn = 1
        planner = FastCombatPlanner(IroncladPriority())

        first = planner.choose_card_action(game)

        self.assertIs(first.card, pommel)
        self.assertTrue(planner.last_decision["search"]["true_combat_end"])
        self.assertEqual(
            ["Pommel Strike", "Whirlwind"],
            [
                item["card_id"]
                for item in planner.last_decision["planned_sequence"]
            ],
        )

        # Apply the authoritative result of Pommel Strike and refresh the
        # hand/energy exactly as CommunicationMod does before the next call.
        game.hand.remove(pommel)
        game.player.energy = 2
        enemies[2].current_hp = 6
        enemies[2].block = 4
        enemies[2].powers = []

        second = planner.choose_card_action(game)

        self.assertIs(second.card, whirlwind)
        self.assertEqual(
            "ordered_turn_search",
            planner.last_decision["reason"],
        )

    def test_resource_rescue_preserves_target_for_targeted_draw_card(self):
        """Emergency Pommel rescue must not drop its enemy binding."""

        enemy = monster(
            "WrithingMass", 80, intent=Intent.ATTACK, damage=15, hits=1
        )
        pommel = card(
            "Pommel Strike", CardType.ATTACK, cost=1, damage=10,
            target=True, magic=2, upgrades=1,
        )
        game = GameStub([enemy], [pommel], hp=30, energy=1, act=3)
        game.draw_pile = [
            card("Strike_R", CardType.ATTACK, damage=6, target=True),
            card("Defend_R", CardType.SKILL, block=5),
        ]

        rescue = self.planner._resource_generation_rescue(
            game, game.hand, total_loss=12, risk_budget=2
        )

        self.assertIsNotNone(rescue)
        rescued_card, target, self_hp_cost = rescue
        self.assertIs(rescued_card, pommel)
        self.assertIs(target, enemy)
        self.assertEqual(0, self_hp_cost)

    def test_resource_rescue_prices_sharp_hide_before_playing_pommel(self):
        guardian = monster(
            "TheGuardian", 91, intent=Intent.ATTACK, damage=6, hits=1,
            powers=[Power("SharpHidePower", "Sharp Hide", 3)],
        )
        pommel = card(
            "Pommel Strike", CardType.ATTACK, cost=1, damage=11,
            target=True, magic=2, upgrades=1,
        )
        game = GameStub([guardian], [pommel], hp=42, energy=3, act=1)
        game.draw_pile = [
            card("Strike_R", CardType.ATTACK, damage=6, target=True),
            card("Defend_R", CardType.SKILL, block=5),
        ]

        rescue = self.planner._resource_generation_rescue(
            game, game.hand, total_loss=11, risk_budget=2
        )

        self.assertIsNotNone(rescue)
        rescued_card, target, self_hp_cost = rescue
        self.assertIs(rescued_card, pommel)
        self.assertIs(target, guardian)
        self.assertEqual(3, self_hp_cost)

    def test_aoe_first_action_hp_loss_caps_each_enemy_overkill(self):
        looter = monster("Looter", 15)
        mugger = monster("Mugger", 40)
        immolate = card(
            "Immolate", CardType.ATTACK, cost=2, damage=28,
            target=False,
        )
        strike = card(
            "Perfected Strike", CardType.ATTACK, cost=2, damage=20,
            target=True,
        )
        game = GameStub(
            [looter, mugger], [immolate, strike], hp=54, energy=4, act=2,
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, immolate)
        self.assertEqual(
            43,
            self.planner.last_decision["first_action_enemy_hp_loss"],
        )

    def test_ordered_search_reduces_flight_one_stack_per_attack(self):
        byrd = monster(
            "Byrd",
            15,
            powers=[
                Power("Flight", "Flight", 3),
                Power("Vulnerable", "Vulnerable", 1),
            ],
        )
        attacks = [
            card("Pommel Strike", CardType.ATTACK, damage=9, target=True),
            card("Strike_R", CardType.ATTACK, damage=6, target=True),
        ]
        game = GameStub([byrd], attacks, hp=20, energy=2, act=2)

        action = self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertFalse(search["true_combat_end"])
        self.assertEqual([5], search["final_enemy_hp"])
        expected_first_loss = (
            6 if action.card.card_id == "Pommel Strike" else 4
        )
        self.assertEqual(
            expected_first_loss, search["first_action_enemy_hp_loss"]
        )

    def test_multihit_keeps_flight_reduction_for_whole_card_resolution(self):
        """Replay seq 117135: the fourth Whirlwind hit is still halved."""

        byrd = monster(
            "Byrd", 31, powers=[Power("Flight", "Flight", 3)]
        )
        whirlwind = card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=10
        )
        game = GameStub([byrd], [whirlwind], hp=66, energy=4, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        search = self.planner.last_decision["search"]
        self.assertEqual(20, search["first_action_enemy_hp_loss"])
        self.assertEqual([11], search["final_enemy_hp"])

    def test_double_tap_duplicates_whirlwind_damage_and_spiker_thorns(self):
        """Replay seq 117436: two complete X=2 resolutions cost 10 HP."""

        repulsor = monster("Repulsor", 34)
        spiker = monster(
            "Spiker", 43, powers=[Power("Thorns", "Thorns", 3)]
        )
        whirlwind = card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=8,
            upgrades=1,
        )
        game = GameStub(
            [repulsor, spiker], [whirlwind], hp=60, block=2,
            energy=2, act=3,
        )
        game.player.powers = [Power("DoubleTapPower", "Double Tap", 2)]

        action = self.planner.choose_card_action(game)

        # With the real duplicated Thorns cost exposed, the old low-value
        # reactive-progress play is correctly rejected.
        self.assertIsInstance(action, EndTurnAction)
        search = next(iter(self.planner._last_single_card_search.values()))[0]
        self.assertEqual(64, search["first_action_enemy_hp_loss"])
        self.assertEqual([2, 11], search["final_enemy_hp"])
        self.assertEqual(10, search["actual_loss"])
        self.assertEqual(50, search["player_hp_after_cards"])

    def test_double_tap_consumes_pen_nib_before_duplicate_resolution(self):
        """Replay seq 121808: 27 boosted damage plus 13 ordinary damage."""

        target = monster(
            "Serpent",
            81,
            intent=Intent.ATTACK,
            damage=1,
            powers=[Power("Vulnerable", "Vulnerable", 3)],
        )
        # CommunicationMod serializes the Pen Nib-modified damage (18).
        anger = card(
            "Anger", CardType.ATTACK, cost=0, damage=18, target=True
        )
        game = GameStub(
            [target], [anger], hp=41, energy=2, act=3
        )
        game.player.powers = [
            Power("DoubleTapPower", "Double Tap", 1),
            Power("Pen Nib", "Pen Nib", 1),
            Power("IntangiblePlayer", "Intangible", 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        search = self.planner.last_decision["search"]
        self.assertEqual(40, search["first_action_enemy_hp_loss"])
        self.assertEqual([41], search["final_enemy_hp"])
        self.assertFalse(search["true_combat_end"])

    def test_pen_nib_relic_counter_is_used_when_power_is_not_serialized(self):
        """A ready relic counter must not be mistaken for an ordinary hit."""

        target = monster(
            "Serpent",
            30,
            intent=Intent.ATTACK,
            damage=1,
            powers=[],
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub([target], [strike], hp=30, energy=1, act=2)
        # CommunicationMod emits the ready relic as counter=9 in some frames
        # instead of adding PenNibPower to player.powers.
        game.relics = [Relic("Pen Nib", "Pen Nib", counter=9)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(12, self.planner.last_decision["search"]["first_action_enemy_hp_loss"])
        self.assertEqual([18], self.planner.last_decision["search"]["final_enemy_hp"])

    def test_branch_local_double_tap_precedes_attack_for_exact_lethal(self):
        target = monster(
            "Attacker", 12, intent=Intent.ATTACK, damage=20, hits=1
        )
        double_tap = card(
            "Double Tap", CardType.SKILL, cost=1, magic=1
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub(
            [target], [strike, double_tap], hp=20, energy=2, act=3
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, double_tap)
        self.assertEqual(
            ["Double Tap", "Strike_R"],
            [
                item["card_id"]
                for item in self.planner.last_decision["planned_sequence"]
            ],
        )
        self.assertTrue(self.planner.last_decision["search"]["true_combat_end"])

    def test_double_tap_progress_fallback_does_not_end_with_attack_available(self):
        """A zero-loss turn must not strand a newly played Double Tap."""

        target = monster(
            "WrithingMass", 160,
            intent=Intent.ATTACK, damage=15, hits=1,
            powers=[Power("Malleable", "Malleable", 3)],
        )
        double_tap = card(
            "Double Tap", CardType.SKILL, cost=1, magic=1
        )
        pommel = card(
            "Pommel Strike", CardType.ATTACK, cost=1, damage=12, target=True
        )
        game = GameStub(
            [target], [double_tap, pommel], hp=73, block=15, energy=4
        )
        game.player.powers = [Power("FlameBarrierPower", "Flame Barrier", 4)]

        # Force the narrow fallback boundary: this reproduces the historical
        # state where the full beam rejected every line and returned END even
        # though the Double Tap -> Attack prefix was executable.
        with patch.object(self.planner, "_best_plan", return_value=(0.0, [])):
            action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, double_tap)
        self.assertEqual(
            "double_tap_progress_setup",
            self.planner.last_decision["reason"],
        )

    def test_active_double_tap_progress_fallback_plays_attack(self):
        target = monster(
            "WrithingMass", 160,
            intent=Intent.ATTACK, damage=15, hits=1,
            powers=[Power("Malleable", "Malleable", 3)],
        )
        pommel = card(
            "Pommel Strike", CardType.ATTACK, cost=1, damage=12, target=True
        )
        game = GameStub([target], [pommel], hp=73, block=15, energy=4)
        game.player.powers = [
            Power("DoubleTapPower", "Double Tap", 1),
            Power("FlameBarrierPower", "Flame Barrier", 4),
        ]

        with patch.object(self.planner, "_best_plan", return_value=(0.0, [])):
            action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, pommel)
        self.assertIs(action.target_monster, target)
        self.assertEqual(
            "double_tap_attack_progress_fallback",
            self.planner.last_decision["reason"],
        )

    def test_zero_loss_writhing_mass_turn_does_not_end_with_hp_attack(self):
        """Replay F39 T4: Block covered the hit, but attacks still had HP value."""

        target = monster(
            "WrithingMass", 156,
            intent=Intent.ATTACK_DEBUFF, damage=10, hits=1,
            powers=[
                Power("Malleable", "Malleable", 3),
                Power("Vulnerable", "Vulnerable", 1),
                Power("Compulsive", "Compulsive", -1),
            ],
        )
        target.block = 2
        attacks = [
            card("Thunderclap", CardType.ATTACK, cost=1, damage=8, target=True),
            card("Clothesline", CardType.ATTACK, cost=2, damage=16, target=True),
            card("Heavy Blade", CardType.ATTACK, cost=0, damage=26, target=True),
            card("Fiend Fire", CardType.ATTACK, cost=0, damage=11, target=True),
        ]
        game = GameStub([target], attacks, hp=73, block=11, energy=4, act=3)
        game.player.max_hp = 85
        game.player.powers = [
            Power("Corruption", "Corruption", -1),
            Power("Metallicize", "Metallicize", 3),
            Power("DemonForm", "Demon Form", 2),
            Power("Strength", "Strength", 4),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, target)
        self.assertEqual("attack_progress_fallback", self.planner.last_decision["reason"])
        self.assertGreater(self.planner.last_decision["raw_attack_progress"], 0)

    def test_writhing_mass_replans_to_attack_after_blocking_setup(self):
        """Replay F39 T1 after Flame Barrier/Spot Weakness refreshed the frame."""

        target = monster(
            "WrithingMass",
            154,
            intent=Intent.ATTACK_DEBUFF,
            damage=8,
            hits=1,
            powers=[
                Power("Malleable", "Malleable", 3),
                Power("Vulnerable", "Vulnerable", 1),
                Power("Compulsive", "Compulsive", -1),
                Power("Weakened", "Weakened", 1),
            ],
        )
        attacks = [
            card(
                "Strike_R", CardType.ATTACK,
                cost=1, damage=14, target=True,
            ),
            card(
                "Pommel Strike", CardType.ATTACK,
                cost=1, damage=15, target=True,
            ),
            card(
                "Strike_R", CardType.ATTACK,
                cost=1, damage=14, target=True,
            ),
        ]
        game = GameStub(
            [target], attacks, hp=30, block=16, energy=1, act=3,
        )
        game.floor = 39
        game.turn = 1
        game.player.max_hp = 85
        game.player.powers = [
            Power("FlameBarrierPower", "Flame Barrier", 4),
            Power("Corruption", "Corruption", -1),
            Power("Strength", "Strength", 4),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual("Pommel Strike", action.card.card_id)
        self.assertEqual(
            "attack_progress_fallback", self.planner.last_decision["reason"]
        )
        self.assertGreater(self.planner.last_decision["raw_attack_progress"], 0)

    def test_authoritative_double_tap_plus_duplicates_next_two_attacks(self):
        target = monster("Target", 20)
        strikes = [
            card("Strike-A", CardType.ATTACK, damage=6, target=True),
            card("Strike-B", CardType.ATTACK, damage=6, target=True),
        ]
        game = GameStub([target], strikes, hp=20, energy=2, act=3)
        game.player.powers = [Power("DoubleTapPower", "Double Tap", 2)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        search = self.planner.last_decision["search"]
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertEqual(4, search["card_resolutions_played"])
        self.assertEqual(0, search["player_double_tap"])

    def test_duplication_power_duplicates_successive_cards_one_layer_each(self):
        target = monster("Target", 100)
        strikes = [
            card(
                "Strike-A", CardType.ATTACK,
                cost=0, damage=6, target=True,
            ),
            card(
                "Strike-B", CardType.ATTACK,
                cost=0, damage=6, target=True,
            ),
        ]
        game = GameStub([target], strikes, hp=30, energy=0)
        game.player.powers = [
            Power("DuplicationPower", "Duplication", 2)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        search = self.planner.last_decision["search"]
        self.assertEqual([76], search["final_enemy_hp"])
        self.assertEqual(4, search["card_resolutions_played"])
        self.assertEqual(0, search["player_duplication"])

    def test_duplication_echo_and_double_tap_add_one_attack_copy_each(self):
        target = monster("Target", 100)
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=0, damage=6, target=True,
        )
        game = GameStub([target], [strike], hp=30, energy=0)
        game.player.powers = [
            Power("EchoFormPower", "Echo Form", 1),
            Power("DoubleTapPower", "Double Tap", 1),
            Power("DuplicationPower", "Duplication", 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, strike)
        search = self.planner.last_decision["search"]
        self.assertEqual([76], search["final_enemy_hp"])
        self.assertEqual(4, search["card_resolutions_played"])
        self.assertEqual(0, search["player_double_tap"])
        self.assertEqual(0, search["player_duplication"])

    def test_duplication_echo_and_burst_add_one_skill_copy_each(self):
        attacker = monster(
            "Cultist", 100,
            intent=Intent.ATTACK, damage=25, hits=1,
        )
        defend = card(
            "Defend_G", CardType.SKILL, cost=0, block=5
        )
        game = GameStub([attacker], [defend], hp=30, energy=0)
        game.player.powers = [
            Power("EchoFormPower", "Echo Form", 1),
            Power("BurstPower", "Burst", 1),
            Power("DuplicationPower", "Duplication", 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, defend)
        search = self.planner.last_decision["search"]
        self.assertEqual(5, search["actual_loss"])
        self.assertEqual(4, search["card_resolutions_played"])
        self.assertEqual(0, search["player_burst"])
        self.assertEqual(0, search["player_duplication"])

    def test_duplication_and_echo_repeat_power_body_and_card_hooks(self):
        attacker = monster(
            "BookOfStabbing", 100,
            intent=Intent.ATTACK, damage=10, hits=3,
        )
        buffer = card("Buffer", CardType.POWER, cost=0)
        game = GameStub([attacker], [buffer], hp=30, energy=0)
        game.player.powers = [
            Power("EchoFormPower", "Echo Form", 1),
            Power("DuplicationPower", "Duplication", 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, buffer)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["actual_loss"])
        self.assertEqual(3, search["card_resolutions_played"])
        self.assertEqual(0, search["player_duplication"])

    def test_duplication_repeats_draw_block_and_created_cards(self):
        attacker = monster(
            "Cultist", 100,
            intent=Intent.ATTACK, damage=10, hits=1,
        )
        backflip = card(
            "Backflip", CardType.SKILL, cost=0, block=5
        )
        game = GameStub([attacker], [backflip], hp=30, energy=0)
        game.player.powers = [
            Power("DuplicationPower", "Duplication", 1)
        ]
        game.draw_pile = [object() for _ in range(10)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, backflip)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["actual_loss"])
        self.assertEqual(4, search["remaining_hand_size"])
        self.assertEqual(6, search["remaining_draw_pile_size"])

        target = monster("Cultist", 100)
        blade_dance = card(
            "Blade Dance", CardType.SKILL,
            cost=0, magic=3,
        )
        game = GameStub([target], [blade_dance], hp=30, energy=0)
        game.player.powers = [
            Power("DuplicationPower", "Duplication", 1)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, blade_dance)
        self.assertEqual(
            6,
            self.planner.last_decision["search"]["remaining_hand_size"],
        )

    def test_duplication_copy_purges_after_body_without_second_exhaust(self):
        target = monster("Cultist", 100)
        seeing_red = card(
            "Seeing Red", CardType.SKILL, cost=0
        )
        seeing_red.exhausts = True
        game = GameStub([target], [seeing_red], hp=30, energy=0)
        game.player.powers = [
            Power("FeelNoPainPower", "Feel No Pain", 3),
            Power("DarkEmbracePower", "Dark Embrace", 1),
            Power("DuplicationPower", "Duplication", 1),
        ]
        game.draw_pile = [object() for _ in range(10)]

        self.planner.choose_card_action(game)

        search = next(iter(self.planner._last_single_card_search.values()))[0]
        # Both card bodies grant energy, but the JAR's purgeOnUse branch
        # removes the queued copy before UseCardAction's exhaust branch.  Only
        # the original therefore fires Feel No Pain and Dark Embrace.
        self.assertEqual(2, search["card_resolutions_played"])
        self.assertEqual(4, search["remaining_energy"])
        self.assertEqual(3, search["final_non_orichalcum_block"])
        self.assertEqual(1, search["remaining_hand_size"])
        self.assertEqual(9, search["remaining_draw_pile_size"])
        self.assertEqual(0, search["player_duplication"])

    def test_duplication_replays_poison_artifact_and_beat_hooks(self):
        target = monster(
            "Cultist", 100,
            powers=[Power("Artifact", "Artifact", 1)],
        )
        poisoned_stab = card(
            "Poisoned Stab", CardType.ATTACK,
            cost=0, damage=6, magic=3, target=True,
        )
        game = GameStub([target], [poisoned_stab], hp=30, energy=0)
        game.player.powers = [
            Power("DuplicationPower", "Duplication", 1)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, poisoned_stab)
        search = self.planner.last_decision["search"]
        # The first copy's Poison consumes Artifact; the second copy's three
        # Poison then lands and ticks after both six-damage resolutions.
        self.assertEqual([85], search["final_enemy_hp"])

        heart = monster(
            "CorruptHeart", 12,
            powers=[Power("BeatOfDeathPower", "Beat of Death", 2)],
        )
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=0, damage=6, target=True,
        )
        game = GameStub([heart], [strike], hp=30, energy=0, act=4)
        game.player.powers = [
            Power("AfterImagePower", "After Image", 1),
            Power("DuplicationPower", "Duplication", 1),
        ]

        self.planner.choose_card_action(game)

        search = next(iter(self.planner._last_single_card_search.values()))[0]
        self.assertEqual(2, search["card_resolutions_played"])
        self.assertEqual(2, search["actual_loss"])

    def test_duplication_consumes_layer_when_queued_copy_cannot_resolve(self):
        cases = (
            ("dead_target", 10, 6, True, None),
            ("time_warp", 11, 100, False, None),
            ("choker", None, 100, False, 5),
        )
        for label, time_warp, target_hp, add_bystander, choker in cases:
            with self.subTest(label=label):
                target = monster(
                    (
                        "TimeEater"
                        if time_warp is not None
                        else "Cultist"
                    ),
                    target_hp,
                    intent=(
                        Intent.ATTACK
                        if label in {"time_warp", "choker"}
                        else Intent.BUFF
                    ),
                    damage=(
                        20 if label in {"time_warp", "choker"} else 0
                    ),
                    hits=(
                        1 if label in {"time_warp", "choker"} else 0
                    ),
                    powers=(
                        [Power(
                            "TimeWarpPower", "Time Warp", time_warp
                        )]
                        if time_warp is not None
                        else []
                    ),
                )
                enemies = [target]
                if add_bystander:
                    enemies.append(monster("Cultist", 100))
                strike = card(
                    (
                        "Iron Wave"
                        if label in {"time_warp", "choker"}
                        else "Strike_R"
                    ),
                    CardType.ATTACK,
                    cost=0,
                    damage=6,
                    block=(
                        20 if label in {"time_warp", "choker"} else 0
                    ),
                    target=True,
                )
                game = GameStub(enemies, [strike], hp=30, energy=0)
                game.player.powers = [
                    Power("DuplicationPower", "Duplication", 1)
                ]
                if choker is not None:
                    game.relics = [
                        Relic(
                            "Velvet Choker", "Velvet Choker",
                            counter=choker,
                        )
                    ]
                planner = FastCombatPlanner(PrioritiesStub())

                action = planner.choose_card_action(game)

                self.assertIs(action.card, strike)
                search = planner.last_decision["search"]
                self.assertEqual(1, search["card_resolutions_played"])
                self.assertEqual(0, search["player_duplication"])

    def test_echo_form_plus_duplicates_first_two_manual_cards(self):
        target = monster("Target", 100)
        strikes = [
            card("Strike-A", CardType.ATTACK, cost=0, damage=6, target=True),
            card("Strike-B", CardType.ATTACK, cost=0, damage=6, target=True),
        ]
        game = GameStub([target], strikes, hp=30, energy=0)
        game.player.powers = [Power("EchoFormPower", "Echo Form", 2)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        search = self.planner.last_decision["search"]
        self.assertEqual([76], search["final_enemy_hp"])
        self.assertEqual(4, search["card_resolutions_played"])

    def test_echo_form_manual_copy_budget_persists_across_frames(self):
        for echo_layers in (1, 2):
            with self.subTest(echo_layers=echo_layers):
                target = monster("Target", 100)
                game = GameStub([target], [], hp=30, energy=0)
                game.player.powers = [
                    Power("EchoFormPower", "Echo Form", echo_layers)
                ]
                planner = FastCombatPlanner(PrioritiesStub())
                expected_uses = [2] * echo_layers + [1]

                for index, expected in enumerate(expected_uses):
                    strike = card(
                        f"Strike-{index}", CardType.ATTACK,
                        cost=0, damage=1, target=True,
                    )
                    game.hand = [strike]

                    action = planner.choose_card_action(game)

                    self.assertIs(action.card, strike)
                    search = planner.last_decision["search"]
                    self.assertEqual(
                        expected,
                        search["first_action_resolution_count"],
                    )
                    target.current_hp = search["final_enemy_hp"][0]
                    game.hand = []
                    planner._sync_confirmed_card_plays(game)

                self.assertEqual(
                    len(expected_uses), planner._confirmed_cards_played
                )
                self.assertEqual(
                    sum(expected_uses),
                    planner._confirmed_card_resolutions,
                )

    def test_echo_form_duplicates_spiker_thorns(self):
        spiker = monster(
            "Spiker", 15, powers=[Power("Thorns", "Thorns", 9)]
        )
        melter = card(
            "Melter", CardType.ATTACK, damage=10, target=True
        )
        game = GameStub([spiker], [melter], hp=41, energy=1)
        game.player.powers = [Power("Echo Form", "Echo Form", 1)]
        planner = FastCombatPlanner(DefectPowerPriority())

        planner.choose_card_action(game)

        search = planner.last_decision["search"]
        self.assertTrue(search["true_combat_end"])
        self.assertEqual(18, search["reactive_hp_cost"])
        self.assertEqual(18, search["actual_loss"])

    def test_melter_removes_block_before_each_resolution_damage(self):
        for duplicated, expected_hp, expected_resolutions in (
            (False, 90, 1),
            (True, 80, 2),
        ):
            with self.subTest(duplicated=duplicated):
                target = monster("Cultist", 100)
                target.block = 7
                melter = card(
                    "Melter", CardType.ATTACK,
                    cost=0, damage=10, target=True,
                )
                game = GameStub([target], [melter], hp=30, energy=0)
                if duplicated:
                    game.player.powers = [
                        Power("DoubleTapPower", "Double Tap", 1)
                    ]
                planner = FastCombatPlanner(PrioritiesStub())

                action = planner.choose_card_action(game)

                self.assertIs(action.card, melter)
                search = planner.last_decision["search"]
                self.assertEqual([expected_hp], search["final_enemy_hp"])
                self.assertEqual(
                    expected_resolutions,
                    search["first_action_resolution_count"],
                )

        target = monster(
            "SnakePlant", 100,
            powers=[Power("Malleable", "Malleable", 4)],
        )
        target.block = 7
        melter = card(
            "Melter", CardType.ATTACK,
            cost=0, damage=10, target=True,
        )
        game = GameStub([target], [melter], hp=30, energy=0)
        game.player.powers = [
            Power("DoubleTapPower", "Double Tap", 1)
        ]
        planner = FastCombatPlanner(PrioritiesStub())

        action = planner.choose_card_action(game)

        self.assertIs(action.card, melter)
        search = planner.last_decision["search"]
        self.assertEqual([80], search["final_enemy_hp"])
        self.assertEqual([5], search["final_enemy_block"])

    def test_dynamic_damage_attacks_fail_closed_when_duplicated(self):
        for card_id in (
            "Rampage",
            "Claw",
            "Glass Knife",
            "Body Slam",
            "Finisher",
        ):
            with self.subTest(card_id=card_id):
                target = monster("Cultist", 200)
                dynamic_attack = card(
                    card_id, CardType.ATTACK,
                    cost=0, damage=100, target=True,
                )
                safe_strike = card(
                    "Strike_R", CardType.ATTACK,
                    cost=0, damage=6, target=True,
                )
                game = GameStub(
                    [target], [dynamic_attack, safe_strike],
                    hp=30, energy=0,
                )
                game.player.powers = [
                    Power("DoubleTapPower", "Double Tap", 1)
                ]
                planner = FastCombatPlanner(PrioritiesStub())

                action = planner.choose_card_action(game)

                self.assertIs(action.card, safe_strike)
                self.assertEqual(
                    12,
                    planner.last_decision["search"][
                        "first_action_enemy_hp_loss"
                    ],
                )
                self.assertEqual(
                    2,
                    planner.last_decision["search"][
                        "first_action_resolution_count"
                    ],
                )

    def test_double_tap_iron_wave_repeats_block_and_damage(self):
        attacker = monster(
            "Cultist", 100, intent=Intent.ATTACK, damage=10, hits=1
        )
        iron_wave = card(
            "Iron Wave", CardType.ATTACK,
            cost=0, damage=5, block=5, target=True,
        )
        game = GameStub([attacker], [iron_wave], hp=30, energy=0)
        game.player.powers = [Power("DoubleTapPower", "Double Tap", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, iron_wave)
        search = self.planner.last_decision["search"]
        self.assertEqual([90], search["final_enemy_hp"])
        self.assertEqual(0, search["actual_loss"])

    def test_double_tap_iron_wave_interleaves_each_spiker_reaction(self):
        spiker = monster(
            "Spiker", 2, powers=[Power("Thorns", "Thorns", 8)]
        )
        iron_wave = card(
            "Iron Wave", CardType.ATTACK,
            cost=0, damage=1, block=5, target=True,
        )
        game = GameStub([spiker], [iron_wave], hp=3, energy=0)
        game.player.powers = [
            Power("Buffer", "Buffer", 1),
            Power("DoubleTapPower", "Double Tap", 1),
        ]

        action = self.planner.choose_card_action(game)

        # Copy one gains 5 Block, then its 8 Thorns consumes that Block and
        # Buffer.  Copy two gains only its own 5 Block, so the second 8
        # Thorns packet is lethal.  Pooling both Block gains would fabricate
        # a safe terminal win.
        self.assertIsInstance(action, EndTurnAction)

    def test_zero_output_attack_still_resolves_debuff_and_spiker_thorns(self):
        spiker = monster(
            "Spiker", 100,
            intent=Intent.ATTACK, damage=20, hits=1,
            powers=[Power("Thorns", "Thorns", 3)],
        )
        neutralize = card(
            "Neutralize", CardType.ATTACK,
            cost=0, damage=0, magic=1, target=True,
        )
        game = GameStub([spiker], [neutralize], hp=30, energy=0)

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, neutralize)
        search = self.planner.last_decision["search"]
        self.assertGreater(search["final_enemy_weak"][0], 0)
        self.assertEqual(3, search["reactive_hp_cost"])

    def test_zero_output_double_tap_bash_still_applies_vulnerable(self):
        target = monster("Cultist", 100)
        bash = card(
            "Bash", CardType.ATTACK,
            cost=0, damage=0, magic=1, target=True,
        )
        game = GameStub([target], [bash], hp=30, energy=0)
        game.player.powers = [Power("DoubleTapPower", "Double Tap", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, bash)
        self.assertGreater(
            self.planner.last_decision["search"][
                "final_enemy_vulnerable"
            ][0],
            0,
        )

    def test_target_death_cancels_copy_before_time_warp_and_hooks(self):
        time_eater = monster(
            "TimeEater", 6,
            powers=[Power("TimeWarpPower", "Time Warp", 10)],
        )
        bystander = monster("Cultist", 100)
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=0, damage=6, target=True,
        )
        game = GameStub(
            [time_eater, bystander], [strike], hp=30, energy=0
        )
        game.player.powers = [
            Power("DoubleTapPower", "Double Tap", 1)
        ]

        self.planner.choose_card_action(game)

        search = next(iter(self.planner._last_single_card_search.values()))[0]
        self.assertEqual(1, search["card_resolutions_played"])
        self.assertEqual(1, search["attack_resolutions_played"])
        self.assertFalse(search["forced_end"])

    def test_lethal_attack_still_pays_that_resolution_beat_of_death(self):
        heart = monster(
            "CorruptHeart", 6,
            powers=[Power("BeatOfDeathPower", "Beat of Death", 2)],
        )
        bystander = monster("Cultist", 100)
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=0, damage=6, target=True,
        )
        game = GameStub(
            [heart, bystander], [strike], hp=1, energy=0
        )
        game.player.powers = [
            Power("DoubleTapPower", "Double Tap", 1)
        ]

        action = self.planner.choose_card_action(game)

        # The original use already queued Beat before its targeted copy is
        # rejected for the dying Heart.  Killing the Heart is not a free
        # terminal line for a one-HP player.
        self.assertIsInstance(action, EndTurnAction)

    def test_poisoned_stab_sadistic_kill_cancels_targeted_copy(self):
        target = monster(
            "Cultist-A", 6,
            intent=Intent.ATTACK, damage=20, hits=1,
        )
        bystander = monster("Cultist-B", 100)
        poisoned_stab = card(
            "Poisoned Stab", CardType.ATTACK,
            cost=0, damage=1, magic=3, target=True,
        )
        game = GameStub(
            [target, bystander], [poisoned_stab], hp=30, energy=0
        )
        game.player.powers = [
            Power("DoubleTapPower", "Double Tap", 1),
            Power("SadisticNaturePower", "Sadistic Nature", 5),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, poisoned_stab)
        search = self.planner.last_decision["search"]
        self.assertEqual([0, 100], search["final_enemy_hp"])
        self.assertEqual(1, search["card_resolutions_played"])
        self.assertEqual(1, search["attack_resolutions_played"])

    def test_catalyst_sadistic_kill_cancels_targeted_burst_copy(self):
        target = monster(
            "Cultist", 5,
            powers=[Power("Poison", "Poison", 1)],
        )
        heart = monster(
            "CorruptHeart", 100,
            powers=[Power("BeatOfDeathPower", "Beat of Death", 2)],
        )
        catalyst = card(
            "Catalyst", CardType.SKILL,
            cost=0, magic=-1, target=True,
        )
        game = GameStub([target, heart], [catalyst], hp=30, energy=0)
        game.player.powers = [
            Power("BurstPower", "Burst", 1),
            Power("SadisticNaturePower", "Sadistic Nature", 5),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, catalyst)
        search = self.planner.last_decision["search"]
        self.assertEqual([0, 100], search["final_enemy_hp"])
        self.assertEqual(1, search["card_resolutions_played"])
        self.assertEqual(28, search["player_hp_after_cards"])

    def test_pen_nib_counter_crossing_buffs_only_the_copy(self):
        for counter, expected_hp in ((8, 82), (9, 82)):
            with self.subTest(counter=counter):
                target = monster("Cultist", 100)
                strike = card(
                    "Strike_R", CardType.ATTACK,
                    cost=0, damage=6, target=True,
                )
                game = GameStub([target], [strike], hp=30, energy=0)
                game.player.powers = [
                    Power("DoubleTapPower", "Double Tap", 1)
                ]
                game.relics = [
                    Relic("Pen Nib", "Pen Nib", counter=counter)
                ]
                planner = FastCombatPlanner(PrioritiesStub())

                action = planner.choose_card_action(game)

                self.assertIs(action.card, strike)
                self.assertEqual(
                    [expected_hp],
                    planner.last_decision["search"]["final_enemy_hp"],
                )

    def test_copy_can_use_boundaries_for_time_warp_and_choker(self):
        cases = (
            ("time_warp_10", 10, None, 2, 88),
            ("time_warp_11", 11, None, 1, 94),
            ("choker_4", None, 4, 2, 88),
            ("choker_5", None, 5, 1, 94),
        )
        for label, time_warp, choker, expected_uses, expected_hp in cases:
            with self.subTest(label=label):
                powers = (
                    [Power("TimeWarpPower", "Time Warp", time_warp)]
                    if time_warp is not None
                    else []
                )
                target_id = "TimeEater" if time_warp is not None else "Cultist"
                target = monster(
                    target_id, 100,
                    intent=Intent.ATTACK, damage=10, hits=1,
                    powers=powers,
                )
                strike = card(
                    "Iron Wave", CardType.ATTACK,
                    cost=0, damage=6, block=5, target=True,
                )
                game = GameStub([target], [strike], hp=30, energy=0)
                game.player.powers = [
                    Power("DoubleTapPower", "Double Tap", 1)
                ]
                if choker is not None:
                    game.relics = [
                        Relic(
                            "Velvet Choker", "Velvet Choker",
                            counter=choker,
                        )
                    ]
                planner = FastCombatPlanner(PrioritiesStub())

                action = planner.choose_card_action(game)

                self.assertIs(action.card, strike)
                search = planner.last_decision["search"]
                self.assertEqual(expected_uses, search["card_resolutions_played"])
                self.assertEqual([expected_hp], search["final_enemy_hp"])

    def test_normality_counts_confirmed_autoplay_resolutions(self):
        for confirmed, expected_uses, expected_hp in (
            (1, 2, 88),
            (2, 1, 94),
        ):
            with self.subTest(confirmed=confirmed):
                target = monster("Cultist", 100)
                strike = card(
                    "Strike_R", CardType.ATTACK,
                    cost=0, damage=6, target=True,
                )
                normality = card(
                    "Normality", CardType.CURSE,
                    cost=-2, playable=False,
                )
                game = GameStub(
                    [target], [strike, normality], hp=30, energy=0
                )
                game.player.powers = [
                    Power("DoubleTapPower", "Double Tap", 1)
                ]
                planner = FastCombatPlanner(PrioritiesStub())
                planner._card_play_turn_key = planner._combat_turn_key(game)
                planner._confirmed_cards_played = 1
                planner._confirmed_card_resolutions = confirmed

                action = planner.choose_card_action(game)

                self.assertIs(action.card, strike)
                search = planner.last_decision["search"]
                self.assertEqual(expected_uses, search["card_resolutions_played"])
                self.assertEqual([expected_hp], search["final_enemy_hp"])

    def test_echo_defend_repeats_intrinsic_block(self):
        attacker = monster(
            "Cultist", 100, intent=Intent.ATTACK, damage=10, hits=1
        )
        defend = card("Defend_B", CardType.SKILL, cost=0, block=5)
        game = GameStub([attacker], [defend], hp=30, energy=0)
        game.player.powers = [Power("EchoFormPower", "Echo Form", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, defend)
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])

    def test_echo_attack_on_use_block_and_sharp_hide_interleave_per_copy(self):
        guardian = monster(
            "TheGuardian", 100,
            powers=[Power("SharpHidePower", "Sharp Hide", 5)],
        )
        strike = card(
            "Strike_G", CardType.ATTACK,
            cost=0, damage=1, target=True,
        )
        game = GameStub([guardian], [strike], hp=3, energy=0)
        game.player.powers = [
            Power("EchoFormPower", "Echo Form", 1),
            Power("AfterImagePower", "After Image", 3),
            Power("Buffer", "Buffer", 1),
        ]

        self.planner.choose_card_action(game)

        search = next(iter(self.planner._last_single_card_search.values()))[0]
        self.assertEqual(1, search["player_hp_after_cards"])
        self.assertEqual(2, search["reactive_hp_cost"])

    def test_echo_defend_interleaves_each_beat_with_buffer(self):
        heart = monster(
            "CorruptHeart", 100,
            powers=[Power("BeatOfDeathPower", "Beat of Death", 6)],
        )
        defend = card("Defend_B", CardType.SKILL, cost=0, block=5)
        game = GameStub([heart], [defend], hp=3, energy=0)
        game.player.powers = [
            Power("EchoFormPower", "Echo Form", 1),
            Power("Buffer", "Buffer", 1),
        ]

        self.planner.choose_card_action(game)

        search = next(iter(self.planner._last_single_card_search.values()))[0]
        self.assertEqual(
            2,
            search["player_hp_after_cards"],
        )

    def test_echo_power_fires_heatsinks_on_each_resolution(self):
        target = monster("Cultist", 100)
        defragment = card("Defragment", CardType.POWER, cost=0)
        game = GameStub([target], [defragment], hp=30, energy=0)
        game.player.powers = [
            Power("EchoFormPower", "Echo Form", 1),
            Power("Heatsink", "Heatsink", 1),
        ]
        game.draw_pile = [object() for _ in range(4)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, defragment)
        search = self.planner.last_decision["search"]
        self.assertEqual(2, search["remaining_hand_size"])
        self.assertEqual(2, search["remaining_draw_pile_size"])

    def test_echo_defragment_interleaves_each_body_with_storm_overflow(self):
        attacker = monster(
            "Cultist", 100,
            intent=Intent.ATTACK, damage=18, hits=1,
        )
        defragment = card("Defragment", CardType.POWER, cost=0)
        game = GameStub([attacker], [defragment], hp=30, energy=0)
        game.player.powers = [
            Power("EchoFormPower", "Echo Form", 1),
            Power("StormPower", "Storm", 1),
        ]
        game.player.orbs = [
            Orb("Frost", "Frost", 5, 2),
            Orb("Frost", "Frost", 5, 2),
            Orb("Frost", "Frost", 5, 2),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, defragment)
        search = self.planner.last_decision["search"]
        # Copy one evokes at Focus+1 (6), copy two at Focus+2 (7),
        # and the remaining Frost passively blocks 4: exactly 17.
        self.assertEqual(1, search["actual_loss"])
        self.assertEqual(2, search["player_focus_bonus"])

    def test_echo_copy_advances_ink_bottle_and_hex_hooks(self):
        target = monster("Chosen", 100)
        defend = card("Defend_B", CardType.SKILL, cost=0, block=5)
        game = GameStub([target], [defend], hp=30, energy=0)
        game.player.powers = [
            Power("EchoFormPower", "Echo Form", 1),
            Power("HexPower", "Hex", 1),
        ]
        game.relics = [Relic("Ink Bottle", "Ink Bottle", counter=8)]
        game.draw_pile = [object() for _ in range(5)]

        self.planner.choose_card_action(game)

        search = next(iter(self.planner._last_single_card_search.values()))[0]
        self.assertEqual(2, search["generated_dazed"])
        self.assertEqual(1, search["remaining_hand_size"])
        self.assertEqual(-1, search["remaining_draw_pile_size"])
        self.assertTrue(search["future_draw_pile_unknown"])

    def test_branch_burst_then_defend_repeats_intrinsic_block(self):
        attacker = monster(
            "Cultist", 100, intent=Intent.ATTACK, damage=10, hits=1
        )
        burst = card("Burst", CardType.SKILL, cost=0, magic=1)
        defend = card("Defend_G", CardType.SKILL, cost=0, block=5)
        game = GameStub([attacker], [defend, burst], hp=30, energy=0)

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, burst)
        self.assertEqual(
            ["Burst", "Defend_G"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])

    def test_echo_turbo_repeats_current_energy_gain(self):
        target = monster("Cultist", 100)
        turbo = card("Turbo", CardType.SKILL, cost=0)
        game = GameStub([target], [turbo], hp=30, energy=0)
        game.player.powers = [Power("EchoFormPower", "Echo Form", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        search = next(iter(self.planner._last_single_card_search.values()))[0]
        self.assertEqual(
            4, search["remaining_energy"]
        )

        self.assertEqual(2, search["generated_voids"])

    def test_echo_reaper_heals_from_each_actual_resolution(self):
        target = monster("Cultist", 100)
        reaper = card(
            "Reaper", CardType.ATTACK,
            cost=0, damage=4, target=False,
        )
        game = GameStub([target], [reaper], hp=10, energy=0)
        game.player.powers = [Power("EchoFormPower", "Echo Form", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, reaper)
        search = self.planner.last_decision["search"]
        self.assertEqual([92], search["final_enemy_hp"])
        self.assertEqual(18, search["player_hp_after_cards"])

    def test_echo_dualcast_consumes_and_evokes_two_front_orbs(self):
        target = monster("Cultist", 100)
        dualcast = card("Dualcast", CardType.SKILL, cost=0)
        game = GameStub([target], [dualcast], hp=30, energy=0)
        game.player.powers = [Power("EchoFormPower", "Echo Form", 1)]
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Lightning", "Lightning", 8, 3),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, dualcast)
        search = self.planner.last_decision["search"]
        self.assertEqual([68], search["final_enemy_hp"])
        self.assertEqual([], search["final_orb_ids"])

    def test_duplicated_vulnerable_attacks_interleave_use_resolutions(self):
        cases = (
            ("Bash", True),
            ("Beam Cell", True),
            ("Thunderclap", False),
            ("Uppercut", True),
        )
        for card_id, targeted in cases:
            with self.subTest(card_id=card_id):
                target = monster("Cultist", 100)
                setup = card(
                    card_id, CardType.ATTACK,
                    cost=0, damage=10, magic=1, target=targeted,
                )
                game = GameStub([target], [setup], hp=30, energy=0)
                game.player.powers = [
                    Power("DoubleTapPower", "Double Tap", 1)
                ]
                planner = FastCombatPlanner(PrioritiesStub())

                planner.choose_card_action(game)

                self.assertEqual(
                    [75], planner.last_decision["search"]["final_enemy_hp"]
                )

    def test_duplicated_bash_artifact_is_consumed_at_each_copy_boundary(self):
        for artifact, expected_hp, expected_vulnerable in (
            (1, 80, True),
            (2, 80, False),
        ):
            with self.subTest(artifact=artifact):
                target = monster(
                    "Cultist", 100,
                    powers=[Power("Artifact", "Artifact", artifact)],
                )
                bash = card(
                    "Bash", CardType.ATTACK,
                    cost=0, damage=10, magic=1, target=True,
                )
                game = GameStub([target], [bash], hp=30, energy=0)
                game.player.powers = [
                    Power("DoubleTapPower", "Double Tap", 1)
                ]
                planner = FastCombatPlanner(PrioritiesStub())

                planner.choose_card_action(game)

                search = planner.last_decision["search"]
                self.assertEqual([expected_hp], search["final_enemy_hp"])
                self.assertEqual(
                    expected_vulnerable,
                    search["final_enemy_vulnerable"][0] > 0,
                )

    def test_duplicated_block_cards_recompute_nonlinear_resolution_state(self):
        cases = (
            ("Auto-Shields", 0, 11, [], 15, 4),
            ("Entrench", 5, 0, [], 20, 0),
            (
                "Second Wind", 0, 5,
                [card("Defend_R", CardType.SKILL, playable=False)],
                10, 5,
            ),
        )
        for card_id, starting_block, card_block, extras, attack, loss in cases:
            with self.subTest(card_id=card_id):
                enemy = monster(
                    "Cultist", 100,
                    intent=Intent.ATTACK, damage=attack, hits=1,
                )
                block_card = card(
                    card_id, CardType.SKILL, cost=0, block=card_block
                )
                game = GameStub(
                    [enemy], [block_card] + extras,
                    hp=30, block=starting_block, energy=0,
                )
                game.player.powers = [
                    Power("EchoFormPower", "Echo Form", 1)
                ]
                planner = FastCombatPlanner(PrioritiesStub())

                action = planner.choose_card_action(game)

                self.assertIs(action.card, block_card)
                self.assertEqual(
                    loss, planner.last_decision["search"]["actual_loss"]
                )

    def test_echo_buffer_repeats_buffer_layers(self):
        attacker = monster(
            "BookOfStabbing", 100,
            intent=Intent.ATTACK, damage=10, hits=2,
        )
        buffer = card("Buffer", CardType.POWER, cost=0)
        game = GameStub([attacker], [buffer], hp=30, energy=0)
        game.player.powers = [Power("EchoFormPower", "Echo Form", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, buffer)
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])

    def test_echo_backflip_repeats_draw_but_heatsinks_is_one_play_trigger(self):
        target = monster("Cultist", 100)
        backflip = card("Backflip", CardType.SKILL, cost=0, block=5)
        game = GameStub([target], [backflip], hp=30, energy=0)
        game.player.powers = [Power("EchoFormPower", "Echo Form", 1)]
        game.draw_pile = [object() for _ in range(10)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, backflip)
        search = self.planner.last_decision["search"]
        self.assertEqual(4, search["remaining_hand_size"])
        self.assertEqual(6, search["remaining_draw_pile_size"])

    def test_echo_double_energy_iterates_each_resolution(self):
        target = monster("Cultist", 100)
        double_energy = card("Double Energy", CardType.SKILL, cost=1)
        game = GameStub([target], [double_energy], hp=30, energy=3)
        game.player.powers = [Power("EchoFormPower", "Echo Form", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        search = next(iter(self.planner._last_single_card_search.values()))[0]
        self.assertEqual(
            8, search["remaining_energy"]
        )

    def test_echo_conserve_battery_repeats_block_and_next_turn_energy(self):
        attacker = monster(
            "Cultist", 100, intent=Intent.ATTACK, damage=10, hits=1
        )
        charge = card(
            "Conserve Battery", CardType.SKILL, cost=0, block=5
        )
        game = GameStub([attacker], [charge], hp=30, energy=0)
        game.player.powers = [Power("EchoFormPower", "Echo Form", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, charge)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["actual_loss"])
        self.assertEqual(2, search["next_turn_energy"])

    def test_echo_consume_recomputes_focus_and_slot_eviction_per_copy(self):
        target = monster("Cultist", 100)
        consume = card("Consume", CardType.SKILL, cost=0, magic=2)
        game = GameStub([target], [consume], hp=30, energy=0)
        game.player.powers = [Power("EchoFormPower", "Echo Form", 1)]
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Lightning", "Lightning", 8, 3),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, consume)
        search = self.planner.last_decision["search"]
        self.assertEqual(22, search["first_action_enemy_hp_loss"])
        # The one remaining Lightning also passively deals seven at END.
        self.assertEqual([71], search["final_enemy_hp"])
        self.assertEqual(["lightning"], search["final_orb_ids"])
        self.assertEqual(4, search["player_focus_bonus"])

    def test_champion_belt_weak_triggers_only_after_vulnerable_lands(self):
        for artifact, double_tap, expected_loss, expected_heal in (
            (0, False, 0, 0),
            (1, False, 3, 3),
            (1, True, 0, 0),
        ):
            with self.subTest(artifact=artifact, double_tap=double_tap):
                powers = (
                    [Power("Artifact", "Artifact", artifact)]
                    if artifact
                    else []
                )
                parasite = monster(
                    "ShelledParasite", 50,
                    intent=Intent.ATTACK_BUFF, damage=10, hits=1,
                    powers=powers,
                )
                parasite.max_hp = 72
                parasite.move_base_damage = 10
                bash = card(
                    "Bash", CardType.ATTACK,
                    cost=0, damage=1, magic=1, target=True,
                )
                game = GameStub(
                    [parasite], [bash], hp=30, block=7, energy=0
                )
                game.relics = [Relic("Champion Belt", "Champion Belt")]
                if double_tap:
                    game.player.powers = [
                        Power("DoubleTapPower", "Double Tap", 1)
                    ]
                planner = FastCombatPlanner(PrioritiesStub())

                planner.choose_card_action(game)

                search = planner.last_decision["search"]
                self.assertEqual(expected_loss, search["actual_loss"])
                self.assertEqual(expected_heal, search["enemy_attack_healing"])

    def test_snecko_skull_adds_one_to_each_poison_application(self):
        cases = (
            (
                "flask",
                card("Bouncing Flask", CardType.SKILL, cost=0, magic=3),
                [],
                88,
            ),
            (
                "catalyst",
                card("Catalyst", CardType.SKILL, cost=0, magic=-1),
                [Power("Poison", "Poison", 10)],
                79,
            ),
            (
                "echo_catalyst",
                card("Catalyst", CardType.SKILL, cost=0, magic=-1),
                [Power("Poison", "Poison", 10)],
                57,
            ),
        )
        for label, poison_card, powers, expected_hp in cases:
            with self.subTest(label=label):
                target = monster("Cultist", 100, powers=powers)
                game = GameStub([target], [poison_card], hp=30, energy=0)
                game.relics = [Relic("Snecko Skull", "Snecko Skull")]
                if label == "echo_catalyst":
                    game.player.powers = [
                        Power("EchoFormPower", "Echo Form", 1)
                    ]
                planner = FastCombatPlanner(PrioritiesStub())

                planner.choose_card_action(game)

                self.assertEqual(
                    [expected_hp],
                    planner.last_decision["search"]["final_enemy_hp"],
                )
                if label == "catalyst":
                    self.assertEqual(
                        21,
                        planner._poison_after_card(
                            game, poison_card, target
                        ),
                    )

    def test_sadistic_nature_resolves_each_landed_debuff_and_belt_followup(self):
        for artifact, expected_hp in ((0, 80), (1, 90)):
            with self.subTest(artifact=artifact):
                powers = (
                    [Power("Artifact", "Artifact", artifact)]
                    if artifact
                    else []
                )
                target = monster("Cultist", 100, powers=powers)
                bash = card(
                    "Bash", CardType.ATTACK,
                    cost=0, damage=10, magic=1, target=True,
                )
                game = GameStub([target], [bash], hp=30, energy=0)
                game.relics = [Relic("Champion Belt", "Champion Belt")]
                game.player.powers = [
                    Power("SadisticNaturePower", "Sadistic Nature", 5)
                ]
                planner = FastCombatPlanner(PrioritiesStub())

                planner.choose_card_action(game)

                self.assertEqual(
                    [expected_hp],
                    planner.last_decision["search"]["final_enemy_hp"],
                )

    def test_sadistic_nature_and_snecko_skull_trigger_for_each_flask_packet(self):
        target = monster("Cultist", 100)
        flask = card("Bouncing Flask", CardType.SKILL, cost=0, magic=3)
        game = GameStub([target], [flask], hp=30, energy=0)
        game.relics = [Relic("Snecko Skull", "Snecko Skull")]
        game.player.powers = [
            Power("SadisticNaturePower", "Sadistic Nature", 5)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, flask)
        # Three landed debuffs deal 15 immediately; twelve Poison then ticks.
        self.assertEqual(
            [73], self.planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_sadistic_nature_thorns_damage_is_absorbed_by_enemy_block(self):
        target = monster("Cultist", 5)
        target.block = 5
        trip = card("Trip", CardType.SKILL, cost=0, magic=2, target=True)
        game = GameStub([target], [trip], hp=30, energy=0)
        game.player.powers = [
            Power("SadisticNaturePower", "Sadistic Nature", 5)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, trip)
        search = self.planner.last_decision["search"]
        self.assertEqual([5], search["final_enemy_hp"])
        self.assertEqual([0], search["final_enemy_block"])
        self.assertFalse(search["true_combat_end"])

    def test_branch_sadistic_nature_applies_to_later_same_turn_debuff(self):
        target = monster("Cultist", 12)
        sadistic = card(
            "Sadistic Nature", CardType.POWER, cost=0, magic=5
        )
        trip = card("Trip", CardType.SKILL, cost=0, magic=2, target=True)
        game = GameStub([target], [trip, sadistic], hp=30, energy=0)

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, sadistic)
        self.assertEqual(
            ["Sadistic Nature", "Trip"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(
            [7], self.planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_exact_lethal_rejects_guardian_mode_shift_block(self):
        guardian = monster(
            "TheGuardian",
            12,
            powers=[Power("Mode Shift", "Mode Shift", 6)],
        )
        attacks = [
            card("Strike-A", CardType.ATTACK, damage=6, target=True),
            card("Strike-B", CardType.ATTACK, damage=6, target=True),
        ]
        game = GameStub([guardian], attacks, hp=20, energy=2)

        combo = self.planner._guaranteed_attack_combo(
            game,
            attacks,
            [guardian],
            total_loss=0,
            end_turn_loss=0,
        )

        self.assertIsNone(combo)

    def test_exact_lethal_can_kill_slime_boss_after_split_is_queued(self):
        slime_boss = monster("SlimeBoss", 75)
        slime_boss.max_hp = 140
        attacks = [
            card("Strike-A", CardType.ATTACK, damage=40, target=True),
            card("Strike-B", CardType.ATTACK, damage=40, target=True),
        ]
        game = GameStub([slime_boss], attacks, hp=20, energy=2)

        combo = self.planner._guaranteed_attack_combo(
            game,
            attacks,
            [slime_boss],
            total_loss=0,
            end_turn_loss=0,
        )

        # Crossing half health only queues Split for the enemy action.  The
        # player turn continues, so the second hit can kill the boss and
        # prevent either child from spawning.
        self.assertIsNotNone(combo)

    def test_shockwave_aoe_weak_prevents_lethal_multi_enemy_attack(self):
        attackers = [
            monster(f"Attacker-{index}", 80, intent=Intent.ATTACK, damage=20, hits=1)
            for index in range(2)
        ]
        shockwave = card("Shockwave", CardType.SKILL, cost=2, magic=3)
        defend = card("Defend_R", CardType.SKILL, block=5)
        game = GameStub(attackers, [shockwave, defend], hp=33, energy=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, shockwave)

    def test_crippling_poison_aoe_weak_prevents_lethal_multi_enemy_attack(self):
        attackers = [
            monster(f"Attacker-{index}", 80, intent=Intent.ATTACK, damage=20, hits=1)
            for index in range(2)
        ]
        crippling_poison = card(
            "Crippling Poison",
            CardType.SKILL,
            cost=2,
            magic=4,
        )
        defend = card("Defend_G", CardType.SKILL, block=5)
        game = GameStub(attackers, [crippling_poison, defend], hp=33, energy=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, crippling_poison)

    def test_aoe_weak_does_not_double_count_enemy_already_weak(self):
        attacker = monster(
            "Attacker",
            80,
            intent=Intent.ATTACK,
            damage=7,
            hits=5,
            powers=[Power("Weakened", "Weak", 1)],
        )
        shockwave = card("Shockwave", CardType.SKILL, cost=1, magic=3)
        block_card = card("Big Block", CardType.SKILL, cost=1, block=25)
        game = GameStub([attacker], [shockwave, block_card], hp=11, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, block_card)

    def test_malaise_x_cost_debuff_prevents_lethal_multihit(self):
        attacker = monster(
            "BookOfStabbing",
            160,
            intent=Intent.ATTACK,
            damage=5,
            hits=5,
        )
        malaise = card(
            "Malaise",
            CardType.SKILL,
            cost=-1,
            magic=-1,
            target=True,
        )
        defend = card("Defend_G", CardType.SKILL, block=5)
        game = GameStub([attacker], [malaise, defend], hp=15, energy=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, malaise)

    def test_entrench_uses_existing_block_on_lethal_turn(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=30, hits=1)
        entrench = card("Entrench", CardType.SKILL, cost=2)
        game = GameStub([attacker], [entrench], hp=5, block=20, energy=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, entrench)

    def test_zero_cost_card_reduces_lethal_regret_hand_size(self):
        idle = monster("Idle", 50)
        deflect = card("Deflect", CardType.SKILL, cost=0, block=5)
        regret = card("Regret", CardType.CURSE, cost=-2, playable=False)
        game = GameStub([idle], [deflect, regret], hp=2, energy=0)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, deflect)

    def test_intangible_regret_uses_remaining_hand_and_buffer_in_order(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=20, hits=1
        )
        big_hit = card(
            "BigHit", CardType.ATTACK, damage=30, target=True
        )
        buffer = card("Buffer", CardType.POWER)
        regret = card("Regret", CardType.CURSE, cost=-2, playable=False)
        game = GameStub(
            [attacker], [big_hit, buffer, regret], hp=2, energy=1
        )
        game.player.powers = [
            Power("IntangiblePlayer", "Intangible", 1)
        ]

        action = self.planner.choose_card_action(game)

        # Regret is still one capped event after either card. Buffer must be
        # played so that event consumes the new layer and the attack costs 1.
        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, buffer)

    def test_medical_kit_burn_is_removed_before_forced_end(self):
        time_eater = monster(
            "TimeEater",
            200,
            powers=[Power("TimeWarpPower", "Time Warp", 11)],
        )
        burn = card("Burn", CardType.STATUS, cost=0, playable=True)
        big_hit = card(
            "BigHit", CardType.ATTACK, cost=0, damage=30, target=True
        )
        game = GameStub(
            [time_eater], [burn, big_hit], hp=2, energy=0, act=3
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, burn)

    def test_blue_candle_curse_cannot_be_played_at_one_hp(self):
        idle = monster("Idle", 50)
        for curse_id in ("Regret", "Decay"):
            with self.subTest(curse_id=curse_id):
                planner = FastCombatPlanner(PrioritiesStub())
                curse = card(
                    curse_id,
                    CardType.CURSE,
                    cost=0,
                    playable=True,
                )
                game = GameStub([idle], [curse], hp=1, energy=0)
                game.relics = [Relic("Blue Candle", "Blue Candle")]

                action = planner.choose_card_action(game)

                self.assertIsInstance(action, EndTurnAction)

    def test_dark_embrace_draw_keeps_regret_lethal_after_burn_exhaust(self):
        idle = monster("Idle", 50)
        burn = card("Burn", CardType.STATUS, cost=0, playable=True)
        regret = card("Regret", CardType.CURSE, cost=-2, playable=False)
        game = GameStub([idle], [burn, regret], hp=2, energy=0)
        game.relics = [Relic("Medical Kit", "Medical Kit")]
        game.player.powers = [
            Power("DarkEmbracePower", "Dark Embrace", 1)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, burn)
        # Dark Embrace replaces the exhausted Burn, so Regret still sees a
        # two-card hand. The planner may choose the least-loss doomed line,
        # but must not expose it to potion policy as survival.
        self.assertEqual(2, self.planner.last_decision["search"]["actual_loss"])
        self.assertEqual(0, self.planner.last_decision["search"]["tier"])
        self.assertEqual(
            2,
            self.planner.last_decision["search"]["remaining_hand_size"],
        )

    def test_dead_branch_generation_keeps_regret_lethal_after_exhaust(self):
        idle = monster("Idle", 50)
        burn = card("Burn", CardType.STATUS, cost=0, playable=True)
        regret = card("Regret", CardType.CURSE, cost=-2, playable=False)
        game = GameStub([idle], [burn, regret], hp=2, energy=0)
        game.relics = [
            Relic("Medical Kit", "Medical Kit"),
            Relic("Dead Branch", "Dead Branch"),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, burn)
        self.assertEqual(2, self.planner.last_decision["search"]["actual_loss"])
        self.assertEqual(0, self.planner.last_decision["search"]["tier"])
        self.assertEqual(
            2,
            self.planner.last_decision["search"]["remaining_hand_size"],
        )

    def test_ink_bottle_draw_keeps_regret_hand_size_on_tenth_card(self):
        idle = monster("Idle", 50)
        big_hit = card(
            "BigHit", CardType.ATTACK, cost=0, damage=30, target=True
        )
        regret = card("Regret", CardType.CURSE, cost=-2, playable=False)
        game = GameStub([idle], [big_hit, regret], hp=2, energy=0)
        game.relics = [Relic("Ink Bottle", "Ink Bottle", counter=9)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, big_hit)
        self.assertEqual(2, self.planner.last_decision["search"]["actual_loss"])
        self.assertEqual(0, self.planner.last_decision["search"]["tier"])
        self.assertEqual(
            2,
            self.planner.last_decision["search"]["remaining_hand_size"],
        )

    def test_strange_spoon_self_exhaust_block_is_not_guaranteed(self):
        attacker = monster(
            "Attacker", 50, intent=Intent.ATTACK, damage=6, hits=1
        )
        adrenaline = card("Adrenaline", CardType.SKILL, cost=0)
        adrenaline.exhausts = True
        game = GameStub([attacker], [adrenaline], hp=4, energy=0)
        game.relics = [Relic("Strange Spoon", "Strange Spoon")]
        game.player.powers = [
            Power("FeelNoPainPower", "Feel No Pain", 3)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, adrenaline)
        self.assertEqual(6, self.planner.last_decision["search"]["actual_loss"])
        self.assertEqual(0, self.planner.last_decision["search"]["tier"])

    def test_strange_spoon_self_exhaust_does_not_guarantee_draw_triggers(self):
        idle = monster("Idle", 50)
        slimed = card("Slimed", CardType.STATUS, cost=0, playable=True)
        slimed.exhausts = True
        wound = card("Wound", CardType.STATUS, cost=-2, playable=False)
        game = GameStub([idle], [slimed, wound], hp=20, energy=0)
        game.relics = [
            Relic("Strange Spoon", "Strange Spoon"),
            Relic("Dead Branch", "Dead Branch"),
        ]
        game.player.powers = [
            Power("DarkEmbracePower", "Dark Embrace", 1)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, slimed)
        self.assertEqual(
            1, self.planner.last_decision["search"]["remaining_hand_size"]
        )

    def test_feel_no_pain_exhaust_block_disables_orichalcum(self):
        attacker = monster(
            "Attacker", 50, intent=Intent.ATTACK, damage=6, hits=1
        )
        burn = card("Burn", CardType.STATUS, cost=0, playable=True)
        game = GameStub([attacker], [burn], hp=3, energy=0, act=4)
        game.relics = [
            Relic("Medical Kit", "Medical Kit"),
            Relic("Orichalcum", "Orichalcum"),
        ]
        game.player.powers = [
            Power("FeelNoPainPower", "Feel No Pain", 3)
        ]

        action = self.planner.choose_card_action(game)

        # END: Ori 6 -> Burn consumes 2 -> attack loses 2, so HP remains 1.
        # Playing Burn exhausts it and gains 3 immediate block, disabling Ori;
        # the subsequent six damage would then be exactly lethal.
        self.assertIsInstance(action, EndTurnAction)

    def test_rage_attack_block_disables_orichalcum(self):
        attacker = monster(
            "Attacker", 50, intent=Intent.ATTACK, damage=6, hits=1
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=0, damage=1, target=True
        )
        game = GameStub([attacker], [strike], hp=3, energy=0, act=4)
        game.relics = [Relic("Orichalcum", "Orichalcum")]
        game.player.powers = [Power("RagePower", "Rage", 3)]

        action = self.planner.choose_card_action(game)

        # END is fully blocked by Ori. Strike gains three Rage block first,
        # disables Ori, and would then take three lethal damage.
        self.assertIsInstance(action, EndTurnAction)

    def test_same_turn_rage_is_played_before_attack_to_prevent_lethal(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=5, hits=1
        )
        rage = card(
            "Rage", CardType.SKILL, cost=0, magic=5, upgrades=1
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=1, target=True
        )
        game = GameStub(
            [attacker], [strike, rage], hp=5, energy=1, act=2
        )
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        seeded_attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=5, hits=1
        )
        seeded_strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=1, target=True
        )
        seeded_game = GameStub(
            [seeded_attacker], [seeded_strike], hp=5, energy=1, act=2
        )
        seeded_game.player.powers = [Power("RagePower", "Rage", 5)]
        seeded_planner = FastCombatPlanner(IroncladPriority())
        seeded_action = seeded_planner.choose_card_action(seeded_game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, rage)
        self.assertEqual(
            ["Rage", "Strike_R"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(0, planner.last_decision["search"]["actual_loss"])
        self.assertIsInstance(seeded_action, PlayCardAction)
        self.assertEqual(
            seeded_planner.last_decision["search"]["actual_loss"],
            planner.last_decision["search"]["actual_loss"],
        )

    def test_same_turn_rage_is_not_played_without_a_downstream_attack(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=5, hits=1
        )
        rage = card(
            "Rage", CardType.SKILL, cost=0, magic=5, upgrades=1
        )
        game = GameStub([attacker], [rage], hp=10, energy=1, act=2)
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_exact_attack_combo_does_not_bypass_free_rage_setup(self):
        parasite = monster(
            "Shelled Parasite",
            69,
            intent=Intent.ATTACK,
            damage=10,
            hits=1,
        )
        fungi = monster(
            "FungiBeast",
            14,
            intent=Intent.ATTACK,
            damage=6,
            hits=1,
        )
        rage = card("Rage", CardType.SKILL, cost=0, magic=5)
        bash = card(
            "Bash", CardType.ATTACK, cost=2, damage=8, target=True
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub(
            [parasite, fungi], [bash, strike, rage],
            hp=65, energy=3, act=2,
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, rage)
        self.assertEqual(
            "guaranteed_combo_setup_before_attacks",
            self.planner.last_decision["reason"],
        )

    def test_exact_attack_combo_does_not_bypass_after_image_setup(self):
        cultist = monster("Cultist", 12)
        awakened = monster(
            "AwakenedOne",
            300,
            intent=Intent.ATTACK,
            damage=15,
            hits=1,
            powers=[Power("CuriosityPower", "Curiosity", 1)],
        )
        after_image = card("After Image", CardType.POWER, cost=1, magic=1)
        attacks = [
            card(
                f"Shiv-{index}",
                CardType.ATTACK,
                cost=0,
                damage=4,
                target=True,
            )
            for index in range(3)
        ]
        game = GameStub(
            [cultist, awakened],
            [*attacks, after_image],
            hp=20,
            energy=1,
            act=3,
        )
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, after_image)
        self.assertEqual(
            "guaranteed_combo_setup_before_attacks",
            planner.last_decision["reason"],
        )
        self.assertEqual("after_image", planner.last_decision["setup_kind"])

    def test_same_turn_after_image_matches_preexisting_power_for_payoff(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=1, hits=1
        )
        after_image = card("After Image", CardType.POWER, cost=1)
        strike = card(
            "Strike_G", CardType.ATTACK, cost=1, damage=1, target=True
        )
        setup_game = GameStub(
            [attacker], [strike, after_image], hp=1, energy=2, act=4
        )
        setup_planner = FastCombatPlanner(SilentPriority())

        setup_action = setup_planner.choose_card_action(setup_game)

        seeded_attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=1, hits=1
        )
        seeded_strike = card(
            "Strike_G", CardType.ATTACK, cost=1, damage=1, target=True
        )
        seeded_game = GameStub(
            [seeded_attacker], [seeded_strike], hp=1, energy=1, act=4
        )
        seeded_game.player.powers = [
            Power("AfterImagePower", "After Image", 1)
        ]
        seeded_planner = FastCombatPlanner(SilentPriority())
        seeded_action = seeded_planner.choose_card_action(seeded_game)

        self.assertIsInstance(setup_action, PlayCardAction)
        self.assertIs(setup_action.card, after_image)
        self.assertEqual(
            ["After Image", "Strike_G"],
            [
                step["card_id"]
                for step in setup_planner.last_decision["planned_sequence"]
            ],
        )
        self.assertIsInstance(seeded_action, PlayCardAction)
        self.assertEqual(
            seeded_planner.last_decision["search"]["actual_loss"],
            setup_planner.last_decision["search"]["actual_loss"],
        )
        self.assertEqual(
            1,
            setup_planner.last_decision["search"]["player_after_image"],
        )

    def test_same_turn_thousand_cuts_precedes_attacks_and_shivs(self):
        automaton = monster("BronzeAutomaton", 300)
        thousand_cuts = card(
            "A Thousand Cuts",
            CardType.POWER,
            cost=2,
            magic=2,
            upgrades=1,
        )
        sucker_punch = card(
            "Sucker Punch", CardType.ATTACK, damage=7, target=True
        )
        shivs = [
            card(f"Shiv-{index}", CardType.ATTACK, cost=0, damage=4, target=True)
            for index in range(3)
        ]
        # Preserve the live card id while keeping unique UUIDs for grouping.
        for shiv in shivs:
            shiv.card_id = "Shiv"
        game = GameStub(
            [automaton],
            [sucker_punch, *shivs, thousand_cuts],
            energy=3,
            act=2,
        )
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, thousand_cuts)
        self.assertEqual(
            [
                "A Thousand Cuts",
                "Sucker Punch",
                "Shiv",
                "Shiv",
                "Shiv",
            ],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        # The power itself must not trigger Thousand Cuts.  Four later cards
        # deal 8 power damage in addition to 19 direct attack damage.
        self.assertEqual(
            [273], planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_active_thousand_cuts_makes_redundant_defend_profitable(self):
        darklings = [monster(f"Darkling-{index}", 40) for index in range(3)]
        defend = card("Defend_G", CardType.SKILL, cost=1, block=5)
        game = GameStub(darklings, [defend], energy=1, act=3)
        game.player.powers = [
            Power("ThousandCutsPower", "Thousand Cuts", 2)
        ]
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)
        self.assertEqual(
            [38, 38, 38],
            planner.last_decision["search"]["final_enemy_hp"],
        )

    def test_same_turn_accuracy_precedes_shiv(self):
        enemy = monster("Maw", 300)
        accuracy = card(
            "Accuracy", CardType.POWER, cost=1, magic=6, upgrades=1
        )
        shiv = card("Shiv", CardType.ATTACK, cost=0, damage=4, target=True)
        game = GameStub([enemy], [shiv, accuracy], energy=1, act=3)
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, accuracy)
        self.assertEqual(
            ["Accuracy", "Shiv"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(
            [290], planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_unload_removes_non_attacks_from_ordered_branch(self):
        enemy = monster("Maw", 300)
        accuracy = card(
            "Accuracy", CardType.POWER, cost=1, magic=6, upgrades=1
        )
        unload = card(
            "Unload", CardType.ATTACK, cost=1, damage=14, target=True
        )
        shiv = card("Shiv", CardType.ATTACK, cost=0, damage=4, target=True)
        defend = card("Defend_G", CardType.SKILL, cost=1, block=5)
        game = GameStub(
            [enemy], [accuracy, defend, unload, shiv], energy=3, act=3
        )
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, accuracy)
        plan = [
            step["card_id"]
            for step in planner.last_decision["planned_sequence"]
        ]
        unload_index = plan.index("Unload")
        self.assertNotIn("Accuracy", plan[unload_index + 1:])
        self.assertNotIn("Defend_G", plan[unload_index + 1:])

    def test_shuriken_orders_aoe_after_triggering_attack(self):
        enemies = [monster(f"Cultist-{index}", 100) for index in range(2)]
        dagger_throw = card(
            "Dagger Throw", CardType.ATTACK, cost=0, damage=9, target=True
        )
        neutralize = card(
            "Neutralize", CardType.ATTACK, cost=0, damage=3, target=True
        )
        dagger_spray = card(
            "Dagger Spray", CardType.ATTACK, cost=0, damage=4
        )
        shiv = card("Shiv", CardType.ATTACK, cost=0, damage=4, target=True)
        game = GameStub(
            enemies,
            [dagger_throw, neutralize, dagger_spray, shiv],
            energy=0,
            act=2,
        )
        game.relics = [Relic("Shuriken", "Shuriken", counter=0)]
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        plan = [
            step["card_id"]
            for step in planner.last_decision["planned_sequence"]
        ]
        self.assertEqual(4, len(plan))
        self.assertEqual("Dagger Spray", plan[-1])
        self.assertEqual(
            164,
            sum(planner.last_decision["search"]["final_enemy_hp"]),
        )
        self.assertEqual(
            1, planner.last_decision["search"]["player_strength_bonus"]
        )

    def test_new_after_image_does_not_block_its_own_beat_of_death(self):
        heart = monster(
            "CorruptHeart",
            300,
            powers=[Power("BeatOfDeath", "Localized Beat", 1)],
        )
        after_image = card("After Image", CardType.POWER, cost=0)
        game = GameStub([heart], [after_image], hp=1, energy=0, act=4)
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_same_turn_feel_no_pain_matches_preexisting_power_for_exhaust(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=3, hits=1
        )
        feel_no_pain = card(
            "Feel No Pain", CardType.POWER, cost=1, magic=3
        )
        seeing_red = card("Seeing Red", CardType.SKILL, cost=0)
        seeing_red.exhausts = True
        setup_game = GameStub(
            [attacker], [seeing_red, feel_no_pain], hp=3, energy=1, act=4
        )
        setup_planner = FastCombatPlanner(IroncladPriority())

        setup_action = setup_planner.choose_card_action(setup_game)

        seeded_attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=3, hits=1
        )
        seeded_seeing_red = card("Seeing Red", CardType.SKILL, cost=0)
        seeded_seeing_red.exhausts = True
        seeded_game = GameStub(
            [seeded_attacker], [seeded_seeing_red], hp=3, energy=0, act=4
        )
        seeded_game.player.powers = [
            Power("FeelNoPainPower", "Feel No Pain", 3)
        ]
        seeded_planner = FastCombatPlanner(IroncladPriority())
        seeded_action = seeded_planner.choose_card_action(seeded_game)

        self.assertIsInstance(setup_action, PlayCardAction)
        self.assertIs(setup_action.card, feel_no_pain)
        self.assertEqual(
            ["Feel No Pain", "Seeing Red"],
            [
                step["card_id"]
                for step in setup_planner.last_decision["planned_sequence"]
            ],
        )
        self.assertIsInstance(seeded_action, PlayCardAction)
        self.assertEqual(
            seeded_planner.last_decision["search"]["actual_loss"],
            setup_planner.last_decision["search"]["actual_loss"],
        )
        self.assertEqual(
            3,
            setup_planner.last_decision["search"]["player_feel_no_pain"],
        )

    def test_same_turn_dark_embrace_matches_preexisting_power_hand_count(self):
        idle = monster("Idle", 80)
        dark_embrace = card("Dark Embrace", CardType.POWER, cost=1)
        seeing_red = card("Seeing Red", CardType.SKILL, cost=0)
        seeing_red.exhausts = True
        setup_game = GameStub(
            [idle], [seeing_red, dark_embrace], hp=20, energy=1, act=2
        )
        setup_planner = FastCombatPlanner(IroncladPriority())

        setup_action = setup_planner.choose_card_action(setup_game)

        seeded_idle = monster("Idle", 80)
        seeded_seeing_red = card("Seeing Red", CardType.SKILL, cost=0)
        seeded_seeing_red.exhausts = True
        seeded_game = GameStub(
            [seeded_idle], [seeded_seeing_red], hp=20, energy=0, act=2
        )
        seeded_game.player.powers = [
            Power("DarkEmbracePower", "Dark Embrace", 1)
        ]
        seeded_planner = FastCombatPlanner(IroncladPriority())
        seeded_action = seeded_planner.choose_card_action(seeded_game)

        self.assertIsInstance(setup_action, PlayCardAction)
        self.assertIs(setup_action.card, dark_embrace)
        self.assertEqual(
            ["Dark Embrace", "Seeing Red"],
            [
                step["card_id"]
                for step in setup_planner.last_decision["planned_sequence"]
            ],
        )
        self.assertIsInstance(seeded_action, PlayCardAction)
        self.assertEqual(
            seeded_planner.last_decision["search"]["remaining_hand_size"],
            setup_planner.last_decision["search"]["remaining_hand_size"],
        )
        self.assertEqual(
            1,
            setup_planner.last_decision["search"]["player_dark_embrace"],
        )

    def test_same_turn_dark_embrace_stacks_match_preexisting_layers(self):
        idle = monster("Idle", 80)
        first_embrace = card("Dark Embrace", CardType.POWER, cost=1)
        first_embrace.uuid = "dark-embrace-1"
        second_embrace = card("Dark Embrace", CardType.POWER, cost=1)
        second_embrace.uuid = "dark-embrace-2"
        seeing_red = card("Seeing Red", CardType.SKILL, cost=0)
        seeing_red.exhausts = True
        setup_game = GameStub(
            [idle],
            [seeing_red, first_embrace, second_embrace],
            hp=20,
            energy=2,
            act=2,
        )
        setup_planner = FastCombatPlanner(IroncladPriority())

        setup_action = setup_planner.choose_card_action(setup_game)

        seeded_idle = monster("Idle", 80)
        seeded_seeing_red = card("Seeing Red", CardType.SKILL, cost=0)
        seeded_seeing_red.exhausts = True
        seeded_game = GameStub(
            [seeded_idle], [seeded_seeing_red], hp=20, energy=0, act=2
        )
        seeded_game.player.powers = [
            Power("DarkEmbracePower", "Dark Embrace", 2)
        ]
        seeded_planner = FastCombatPlanner(IroncladPriority())
        seeded_action = seeded_planner.choose_card_action(seeded_game)

        self.assertIsInstance(setup_action, PlayCardAction)
        self.assertEqual(
            ["Dark Embrace", "Dark Embrace", "Seeing Red"],
            [
                step["card_id"]
                for step in setup_planner.last_decision["planned_sequence"]
            ],
        )
        self.assertIsInstance(seeded_action, PlayCardAction)
        setup_search = setup_planner.last_decision["search"]
        seeded_search = seeded_planner.last_decision["search"]
        self.assertEqual(2, setup_search["player_dark_embrace"])
        self.assertEqual(
            seeded_search["remaining_hand_size"],
            setup_search["remaining_hand_size"],
        )

    def test_same_turn_corruption_matches_preexisting_power_skill_costs(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=15, hits=1
        )
        corruption = card("Corruption", CardType.POWER, cost=2)
        setup_defends = [
            card(f"Defend_R-{index}", CardType.SKILL, cost=1, block=5)
            for index in range(3)
        ]
        setup_game = GameStub(
            [attacker], [*setup_defends, corruption], hp=15, energy=2, act=4
        )
        setup_planner = FastCombatPlanner(IroncladPriority())

        setup_action = setup_planner.choose_card_action(setup_game)

        seeded_attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=15, hits=1
        )
        seeded_defends = [
            card(f"Defend_R-seeded-{index}", CardType.SKILL, cost=1, block=5)
            for index in range(3)
        ]
        seeded_game = GameStub(
            [seeded_attacker], seeded_defends, hp=15, energy=0, act=4
        )
        seeded_game.player.powers = [Power("Corruption", "Corruption", -1)]
        seeded_planner = FastCombatPlanner(IroncladPriority())
        seeded_action = seeded_planner.choose_card_action(seeded_game)

        self.assertIsInstance(setup_action, PlayCardAction)
        self.assertIs(setup_action.card, corruption)
        self.assertIsInstance(seeded_action, PlayCardAction)
        self.assertEqual(0, setup_planner.last_decision["search"]["actual_loss"])
        self.assertEqual(
            seeded_planner.last_decision["search"]["actual_loss"],
            setup_planner.last_decision["search"]["actual_loss"],
        )
        self.assertTrue(
            setup_planner.last_decision["search"]["player_corruption"]
        )

    def test_reachable_corruption_admits_unaffordable_skill_to_beam(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=15, hits=1
        )
        corruption = card("Corruption", CardType.POWER, cost=2)
        expensive_block = card(
            "Expensive Skill",
            CardType.SKILL,
            cost=3,
            block=15,
            playable=False,
        )
        game = GameStub(
            [attacker],
            [expensive_block, corruption],
            hp=15,
            energy=2,
            act=4,
        )
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, corruption)
        self.assertEqual(
            ["Corruption", "Expensive Skill"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(0, planner.last_decision["search"]["actual_loss"])

    def test_corruption_then_rage_exhausts_before_attack_payoff(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=8, hits=1
        )
        corruption = card("Corruption", CardType.POWER, cost=2)
        rage = card("Rage", CardType.SKILL, cost=0, magic=5, upgrades=1)
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=1, target=True
        )
        game = GameStub(
            [attacker], [strike, rage, corruption], hp=8, energy=3, act=4
        )
        game.player.powers = [
            Power("FeelNoPainPower", "Feel No Pain", 3),
            Power("DarkEmbracePower", "Dark Embrace", 1),
        ]
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, corruption)
        self.assertEqual(
            ["Corruption", "Rage", "Strike_R"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        search = planner.last_decision["search"]
        self.assertEqual(0, search["actual_loss"])
        self.assertEqual(8, search["final_player_block"])
        self.assertEqual(1, search["remaining_hand_size"])
        self.assertEqual(5, search["player_rage"])
        self.assertTrue(search["player_corruption"])

    def test_champ_regression_existing_corruption_uses_rage_before_two_attacks(self):
        champ = monster(
            "Champ", 300, intent=Intent.ATTACK, damage=24, hits=1
        )
        power_through = card(
            "Power Through", CardType.SKILL, cost=1, block=11
        )
        perfected = card(
            "Perfected Strike", CardType.ATTACK,
            cost=2, damage=10, target=True,
        )
        sword_boomerang = card(
            "Sword Boomerang", CardType.ATTACK,
            cost=1, damage=3, target=True,
        )
        basic_defend = card(
            "Defend_R", CardType.SKILL, cost=1, block=3
        )
        rage = card(
            "Rage", CardType.SKILL, cost=0, magic=5, upgrades=1
        )
        game = GameStub(
            [champ],
            [power_through, perfected, sword_boomerang, basic_defend, rage],
            hp=66,
            energy=3,
            act=2,
        )
        game.room_type = "MonsterRoomBoss"
        game.player.powers = [Power("Corruption", "Corruption", -1)]
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        sequence = [
            step["card_id"]
            for step in planner.last_decision["planned_sequence"]
        ]
        self.assertLess(sequence.index("Rage"), sequence.index("Perfected Strike"))
        self.assertLess(sequence.index("Rage"), sequence.index("Sword Boomerang"))
        self.assertEqual(0, planner.last_decision["search"]["actual_loss"])
        self.assertEqual(24, planner.last_decision["search"]["final_player_block"])

    def test_ornamental_fan_third_attack_block_disables_orichalcum(self):
        attacker = monster(
            "Attacker", 50, intent=Intent.ATTACK, damage=6, hits=1
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=0, damage=1, target=True
        )
        game = GameStub([attacker], [strike], hp=2, energy=0, act=4)
        game.relics = [
            Relic("Orichalcum", "Orichalcum"),
            Relic("Ornamental Fan", "Ornamental Fan", counter=2),
        ]

        action = self.planner.choose_card_action(game)

        # The third attack grants four block and disables Ori; two damage is
        # exactly lethal, whereas ending immediately is fully blocked.
        self.assertIsInstance(action, EndTurnAction)

    def test_tough_bandages_discard_block_disables_orichalcum(self):
        attacker = monster(
            "Attacker", 50, intent=Intent.ATTACK, damage=6, hits=1
        )
        dagger_throw = card(
            "Dagger Throw",
            CardType.ATTACK,
            cost=0,
            damage=1,
            target=True,
        )
        game = GameStub(
            [attacker], [dagger_throw], hp=3, energy=0, act=4
        )
        game.relics = [
            Relic("Orichalcum", "Orichalcum"),
            Relic("Tough Bandages", "Tough Bandages"),
        ]

        action = self.planner.choose_card_action(game)

        # Dagger Throw's forced discard grants three block. That disables Ori
        # and leaves three lethal attack damage; END takes zero.
        self.assertIsInstance(action, EndTurnAction)

    def test_safe_end_orb_kill_is_not_replaced_by_base_fission(self):
        attacker = monster(
            "Attacker", 8, intent=Intent.ATTACK, damage=20, hits=1
        )
        fission = card("Fission", CardType.SKILL, cost=0)
        defend = card(
            "Defend_B", CardType.SKILL, cost=1, block=5, playable=False
        )
        burn = card("Burn", CardType.STATUS, cost=-2, playable=False)
        game = GameStub(
            [attacker], [fission, defend, burn], hp=2, energy=0
        )
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 8)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "all_enemies_passively_doomed",
            self.planner.last_decision.get("reason"),
        )

    def test_passive_poison_kill_blocks_burn_before_ending_turn(self):
        doomed = monster(
            "Cultist",
            5,
            powers=[Power("Poison", "Poison", 5)],
        )
        burns = [
            card(f"Burn-{index}", CardType.STATUS, cost=-2, playable=False)
            for index in range(2)
        ]
        for burn in burns:
            burn.card_id = "Burn"
        survivor = card("Survivor", CardType.SKILL, cost=1, block=11)
        game = GameStub([doomed], [survivor, *burns], hp=61, energy=3)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, survivor)
        self.assertEqual(
            "prevent_passive_kill_end_turn_loss",
            self.planner.last_decision.get("reason"),
        )
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])

    def test_end_orb_kill_does_not_waste_playable_block_on_burn(self):
        doomed = monster("Cultist", 8)
        burn = card("Burn", CardType.STATUS, cost=-2, playable=False)
        defend = card("Defend_B", CardType.SKILL, cost=1, block=5)
        game = GameStub([doomed], [defend, burn], hp=2, energy=1)
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 8)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "all_enemies_passively_doomed",
            self.planner.last_decision.get("reason"),
        )

    def test_passive_kill_does_not_play_block_for_unblockable_combust(self):
        doomed = monster(
            "Cultist",
            5,
            powers=[Power("Poison", "Poison", 5)],
        )
        defend = card("Defend_B", CardType.SKILL, cost=1, block=10)
        game = GameStub([doomed], [defend], hp=10, energy=1)
        game.player.powers = [Power("Combust", "Combust", 5, misc=1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "all_enemies_passively_doomed",
            self.planner.last_decision.get("reason"),
        )

    def test_combust_kill_with_only_attack_left_keeps_passive_doom_reason(self):
        doomed = monster("Cultist", 5)
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub([doomed], [strike], hp=10, energy=1)
        game.player.powers = [Power("Combust", "Combust", 5, misc=1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "all_enemies_passively_doomed",
            self.planner.last_decision.get("reason"),
        )
        self.assertTrue(
            self.planner.last_decision["search"]["true_combat_end"]
        )

    def test_snake_plant_production_hand_keeps_post_death_state_mutable(self):
        snake_plant = monster(
            "SnakePlant",
            77,
            intent=Intent.ATTACK,
            damage=7,
            hits=3,
            powers=[Power("Malleable", "Malleable", 3)],
        )
        first_flame_barrier = card(
            "Flame Barrier", CardType.SKILL, cost=2, block=12
        )
        second_flame_barrier = card(
            "Flame Barrier", CardType.SKILL, cost=2, block=12
        )
        second_flame_barrier.uuid = "flame-barrier-2"
        pummel = card(
            "Pummel",
            CardType.ATTACK,
            cost=1,
            damage=2,
            magic=4,
            target=True,
        )
        pummel.exhausts = True
        game = GameStub(
            [snake_plant],
            [
                card(
                    "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
                ),
                first_flame_barrier,
                second_flame_barrier,
                card(
                    "Anger", CardType.ATTACK, cost=0, damage=6, target=True
                ),
                card("Defend_R", CardType.SKILL, cost=1, block=5),
                card(
                    "Sword Boomerang",
                    CardType.ATTACK,
                    cost=1,
                    damage=3,
                    magic=3,
                ),
                pummel,
            ],
            hp=74,
            block=0,
            energy=4,
            act=2,
        )
        game.floor = 27
        game.turn = 1
        game.player.max_hp = 74
        game.player.current_hp = 74
        game.player.powers = [Power("No Draw", "No Draw", -1)]
        game.relics = [
            Relic("Burning Blood", "Burning Blood"),
            Relic("Bottled Flame", "Bottled Flame"),
            Relic("Golden Idol", "Golden Idol"),
            Relic("Fusion Hammer", "Fusion Hammer"),
            Relic("Torii", "Torii"),
            Relic("Charon's Ashes", "Charon's Ashes"),
        ]
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual("Flame Barrier", action.card.card_id)
        self.assertEqual([50], planner.last_decision["search"]["final_enemy_hp"])
        self.assertEqual(0, planner.last_decision["search"]["actual_loss"])

    def test_charons_ashes_packet_requires_relic_and_exhaust_after_malleable(self):
        outcomes = {}
        for exhausts, has_charons_ashes in (
            (True, True),
            (True, False),
            (False, True),
        ):
            with self.subTest(
                exhausts=exhausts, has_charons_ashes=has_charons_ashes
            ):
                snake_plant = monster(
                    "SnakePlant",
                    77,
                    intent=Intent.ATTACK,
                    damage=7,
                    hits=3,
                    powers=[Power("Malleable", "Malleable", 3)],
                )
                pummel = card(
                    "Pummel",
                    CardType.ATTACK,
                    cost=1,
                    damage=2,
                    magic=4,
                    target=True,
                )
                pummel.exhausts = exhausts
                game = GameStub(
                    [snake_plant], [pummel], hp=74, energy=1, act=2
                )
                if has_charons_ashes:
                    game.relics = [
                        Relic("Charon's Ashes", "Charon's Ashes")
                    ]
                planner = FastCombatPlanner(IroncladPriority())

                action = planner.choose_card_action(game)

                self.assertIsInstance(action, PlayCardAction)
                self.assertIs(action.card, pummel)
                search = planner.last_decision["search"]
                outcomes[(exhausts, has_charons_ashes)] = {
                    "hp": search["final_enemy_hp"],
                    "block": search["final_enemy_block"],
                }

        self.assertEqual(
            {"hp": [73], "block": [0]}, outcomes[(True, True)]
        )
        self.assertEqual(
            {"hp": [74], "block": [2]}, outcomes[(True, False)]
        )
        self.assertEqual(
            {"hp": [74], "block": [2]}, outcomes[(False, True)]
        )

    def test_new_orbs_use_signed_negative_focus(self):
        game = GameStub([], [], hp=20, energy=0)
        game.player.powers = [Power("Focus", "Focus", -2)]

        frost = self.planner._projected_orb(game, "frost")
        lightning = self.planner._projected_orb(game, "lightning")

        self.assertEqual(0, frost.passive_amount)
        self.assertEqual(3, frost.evoke_amount)
        self.assertEqual(1, lightning.passive_amount)
        self.assertEqual(6, lightning.evoke_amount)

    def test_bandage_up_healing_counts_before_forced_enemy_attack(self):
        time_eater = monster(
            "TimeEater",
            200,
            intent=Intent.ATTACK,
            damage=6,
            hits=1,
            powers=[Power("TimeWarpPower", "Time Warp", 11)],
        )
        bandage = card(
            "Bandage Up", CardType.SKILL, cost=0, magic=4
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=0, damage=6, target=True
        )
        game = GameStub(
            [time_eater], [bandage, strike], hp=5, energy=0, act=3
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, bandage)
        self.assertEqual(
            9,
            self.planner.last_decision["search"]["player_hp_after_cards"],
        )

    def test_time_eater_forced_strength_flows_through_new_block(self):
        time_eater = monster(
            "TimeEater",
            200,
            intent=Intent.ATTACK,
            damage=26,
            hits=1,
            powers=[Power("TimeWarpPower", "Time Warp", 11)],
        )
        impervious = card(
            "Impervious", CardType.SKILL, cost=0, block=30
        )
        game = GameStub([time_eater], [impervious], hp=5, energy=0)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, impervious)
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])

    def test_time_eater_forced_strength_respects_new_intangible(self):
        time_eater = monster(
            "TimeEater",
            200,
            intent=Intent.ATTACK,
            damage=30,
            hits=1,
            powers=[Power("TimeWarpPower", "Time Warp", 11)],
        )
        apparition = card("Apparition", CardType.SKILL, cost=0)
        game = GameStub([time_eater], [apparition], hp=2, energy=0)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, apparition)
        self.assertEqual(1, self.planner.last_decision["search"]["actual_loss"])

    def test_combo_does_not_reuse_buffer_spent_by_pain(self):
        target = monster("Target", 12)
        remaining = monster(
            "Remaining", 40, intent=Intent.ATTACK, damage=10, hits=1
        )
        attacks = [
            card("Strike-A", CardType.ATTACK, damage=6, target=True),
            card("Strike-B", CardType.ATTACK, damage=6, target=True),
        ]
        pain = card("Pain", CardType.CURSE, cost=-2, playable=False)
        game = GameStub(
            [target, remaining], [*attacks, pain], hp=11, energy=2
        )
        game.player.powers = [Power("Buffer", "Buffer", 1)]

        combo = self.planner._guaranteed_attack_combo(
            game,
            attacks,
            [target, remaining],
            total_loss=10,
            end_turn_loss=0,
        )

        self.assertIsNone(combo)

    def test_verified_avoidable_fallback_exposes_terminal_search(self):
        attacker = monster(
            "Attacker", 30, intent=Intent.ATTACK, damage=6, hits=1
        )
        defend = card("Defend", CardType.SKILL, block=5)
        game = GameStub(
            [attacker], [defend], hp=20, block=5, energy=1
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(
            "prevent_avoidable_hp_loss",
            self.planner.last_decision.get("reason"),
        )
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])
        self.assertEqual(2, self.planner.last_decision["search"]["tier"])

    def test_stale_basic_block_fallback_is_rejected_when_current_block_covers_attack(self):
        """A stale fallback must not replay Defend after the hit is covered."""
        chosen = monster(
            "Chosen", 43, intent=Intent.ATTACK, damage=7, hits=1,
            powers=[Power("HexPower", "Hex", 1)],
        )
        defend = card("Defend_G", CardType.SKILL, cost=1, block=5)
        game = GameStub([chosen], [defend], hp=57, block=9, energy=1, act=2)
        planner = FastCombatPlanner(PrioritiesStub())
        stale = SimpleNamespace(
            card=defend,
            target=None,
            damage=0,
            base_score=1,
            mitigation=5,
            intrinsic_mitigation=5,
            block_gain=5,
            end_turn_relief=0,
            self_hp_cost=0,
        )

        def stale_search(*_args, **_kwargs):
            planner._last_verified_fallback = stale
            planner._last_verified_fallback_search = {}
            return 0.0, []

        with patch.object(planner, "_best_plan", side_effect=stale_search):
            action = planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "no_positive_marginal_action",
            planner.last_decision.get("reason"),
        )

    def test_all_character_defends_end_when_current_block_covers_hex_attack(self):
        """Base Defend ids use normalized tokens in the zero-loss guard."""

        for defend_id in ("Defend_R", "Defend_G", "Defend_B"):
            with self.subTest(defend_id=defend_id):
                chosen = monster(
                    "Chosen", 43, intent=Intent.ATTACK,
                    damage=5, hits=2,
                )
                defend = card(
                    defend_id, CardType.SKILL, cost=1, block=5
                )
                game = GameStub(
                    [chosen], [defend], hp=13, block=10,
                    energy=1, act=2,
                )
                game.player.powers = [Power("Hex", "Hex", 1)]
                planner = FastCombatPlanner(PrioritiesStub())

                action = planner.choose_card_action(game)

                self.assertIsInstance(action, EndTurnAction)
                self.assertEqual(
                    "no_positive_marginal_action",
                    planner.last_decision.get("reason"),
                )

    def test_covered_defend_prefix_yields_to_safe_attack_progress(self):
        for defend_id in ("Defend_R", "Defend_G", "Defend_B"):
            with self.subTest(defend_id=defend_id):
                chosen = monster(
                    "Chosen", 17, intent=Intent.ATTACK,
                    damage=4, hits=2,
                )
                defend = card(
                    defend_id, CardType.SKILL, cost=1, block=5
                )
                strike = card(
                    "Strike_R", CardType.ATTACK,
                    cost=1, damage=6, target=True,
                )
                game = GameStub(
                    [chosen], [defend, strike], hp=13,
                    block=8, energy=2, act=2,
                )
                game.player.powers = [Power("Hex", "Hex", 1)]
                planner = FastCombatPlanner(PrioritiesStub())

                action = planner.choose_card_action(game)

                self.assertIsInstance(action, PlayCardAction)
                self.assertIs(action.card, strike)
                self.assertNotEqual(
                    defend_id, planner.last_decision.get("card_id")
                )

    def test_stale_survivor_fallback_is_rejected_when_current_block_covers_chosen(self):
        """Survivor is still an energy/Hex cost when the attack is covered."""
        chosen = monster(
            "Chosen", 29, intent=Intent.ATTACK, damage=5, hits=2,
            powers=[Power("HexPower", "Hex", 1)],
        )
        survivor = card("Survivor", CardType.SKILL, cost=1, block=9)
        game = GameStub(
            [chosen], [survivor], hp=33, block=12, energy=1, act=2
        )
        planner = FastCombatPlanner(PrioritiesStub())
        stale = SimpleNamespace(
            card=survivor,
            target=None,
            damage=0,
            base_score=1,
            mitigation=0,
            intrinsic_mitigation=0,
            block_gain=9,
            end_turn_relief=0,
            self_hp_cost=0,
        )

        def stale_search(*_args, **_kwargs):
            planner._last_verified_fallback = stale
            planner._last_verified_fallback_search = {}
            return 0.0, []

        with patch.object(planner, "_best_plan", side_effect=stale_search):
            action = planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "no_positive_marginal_action",
            planner.last_decision.get("reason"),
        )

    def test_calipers_retained_block_is_scored_as_persistent_defense(self):
        """A large Defend can be useful even on a zero-incoming turn."""
        enemy = monster("Cultist", 50)
        defend = card("Defend_G", CardType.SKILL, cost=1, block=20)
        game = GameStub([enemy], [defend], hp=30, energy=1)
        game.relics = [Relic("Calipers", "Calipers")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)
        search = self.planner.last_decision["search"]
        self.assertEqual(5, search["calipers_retained_block_gain"])
        self.assertEqual(20, search["final_player_block"])

    def test_calipers_does_not_discard_small_block_before_current_attack(self):
        """Reproduce the F50 Donu/Deca turns that ended with two Defends."""

        attacker = monster(
            "Deca", 198, intent=Intent.ATTACK_DEBUFF,
            damage=13, hits=2,
        )
        buffer_enemy = monster("Donu", 176, intent=Intent.BUFF)
        first = card("Defend_R", CardType.SKILL, cost=1, block=5)
        second = card("Defend_R", CardType.SKILL, cost=1, block=5)
        second.uuid = "second-defend"
        game = GameStub(
            [attacker, buffer_enemy], [first, second],
            hp=79, block=0, energy=1, act=3,
        )
        game.room_type = "MonsterRoomBoss"
        game.relics = [Relic("Calipers", "Calipers")]
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertTrue(action.card is first or action.card is second)
        self.assertEqual(
            "ordered_turn_search", planner.last_decision["reason"]
        )
        self.assertEqual(
            21,
            combat_predictor.projected_attack_hp_loss(game, extra_block=5),
        )

    def test_terminal_plan_allows_visible_cards_when_future_draw_is_unknown(self):
        """Unknown draw state must not reject a UUID-bound visible lethal line."""
        first = card("Strike_R", CardType.ATTACK, cost=1, damage=6, target=True)
        second = card("Bash", CardType.ATTACK, cost=1, damage=8, target=True)
        game = GameStub([monster("Cultist", 6)], [first, second], hp=30, energy=2)
        first_candidate = SimpleNamespace(card=first, target=game.monsters[0])
        second_candidate = SimpleNamespace(card=second, target=game.monsters[0])

        self.planner._arm_terminal_plan(
            game,
            [first_candidate, second_candidate],
            {"true_combat_end": True, "future_draw_pile_unknown": True},
        )

        self.assertIsNotNone(self.planner._terminal_plan)
        self.planner._clear_terminal_plan()
        hidden = card("Strike_R", CardType.ATTACK, cost=1, damage=6, target=True)
        hidden.uuid = "hidden-card"
        self.planner._arm_terminal_plan(
            game,
            [SimpleNamespace(card=hidden, target=game.monsters[0]), second_candidate],
            {"true_combat_end": True, "future_draw_pile_unknown": True},
        )
        self.assertIsNone(self.planner._terminal_plan)

    def test_multiple_cheap_blocks_never_end_on_avoidable_damage(self):
        """A low-value search result must still take a safe block.

        This mirrors the live failure where two affordable defensive cards
        were in hand, but the planner's score cutoff collapsed to END while
        one card would have removed the only incoming hit.
        """
        attacker = monster(
            "Healer", 53, intent=Intent.ATTACK, damage=7, hits=1,
            powers=[Power("Weak", "Weak", 1)],
        )
        defend = card("Defend_G", CardType.SKILL, cost=1, block=5)
        dodge = card("Dodge and Roll+", CardType.SKILL, cost=1, block=6)
        game = GameStub(
            [attacker], [defend, dodge], hp=59, energy=2, act=2
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIn(action.card, (defend, dodge))
        self.assertNotEqual(
            "no_positive_marginal_action",
            self.planner.last_decision.get("reason"),
        )
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])

    def test_status_cleanup_exposes_verified_terminal_search(self):
        idle = monster("Idle", 50)
        slimed = card("Slimed", CardType.STATUS, cost=1, playable=True)
        game = GameStub([idle], [slimed], hp=20, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(
            "exhaust_dead_status",
            self.planner.last_decision.get("reason"),
        )
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])
        self.assertEqual(2, self.planner.last_decision["search"]["tier"])

    def test_pain_prevents_one_hp_attack_suicide(self):
        idle = monster("Idle", 50)
        strike = card("Strike_R", CardType.ATTACK, damage=6, target=True)
        pain = card("Pain", CardType.CURSE, cost=-2, playable=False)
        game = GameStub([idle], [strike, pain], hp=1)

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_exact_lethal_search_does_not_bypass_lethal_pain_trigger(self):
        idle = monster("Idle", 12)
        first_strike = card("Strike_R", CardType.ATTACK, damage=6, target=True)
        second_strike = card("Strike_R-2", CardType.ATTACK, damage=6, target=True)
        pain = card("Pain", CardType.CURSE, cost=-2, playable=False)
        game = GameStub(
            [idle],
            [first_strike, second_strike, pain],
            hp=1,
            energy=2,
        )

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_random_attack_still_counts_passively_doomed_monster_as_target(self):
        doomed = monster(
            "Cultist",
            5,
            powers=[Power("Poison", "Poison", 5)],
        )
        attacker = monster("Attacker", 18, intent=Intent.ATTACK, damage=50, hits=1)
        first = card("Sword Boomerang", CardType.ATTACK, cost=1, damage=3)
        second = card("Sword Boomerang-2", CardType.ATTACK, cost=1, damage=3)
        second.card_id = "Sword Boomerang"
        defend = card("Defend_R", CardType.SKILL, cost=1, block=5)
        game = GameStub(
            [doomed, attacker],
            [first, second, defend],
            hp=10,
            energy=2,
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)
        self.assertNotEqual(
            "guaranteed_attack_combo_lethal",
            self.planner.last_decision.get("reason"),
        )

    def test_combust_is_not_played_at_one_hp(self):
        idle = monster("Idle", 50)
        combust = card("Combust", CardType.POWER, magic=5)
        game = GameStub([idle], [combust], hp=1)

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_defend_prevents_lethal_burn_before_enemy_turn(self):
        idle = monster("Idle", 50)
        burn = card("Burn", CardType.STATUS, cost=-2, playable=False)
        defend = card("Defend_B", CardType.SKILL, cost=1, block=5)
        game = GameStub([idle], [burn, defend], hp=2, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)

    def test_defend_prevents_lethal_constricted_before_poison_kill(self):
        doomed = monster(
            "Doomed",
            1,
            powers=[Power("Poison", "Poison", 1)],
        )
        defend = card("Defend_B", CardType.SKILL, cost=1, block=10)
        game = GameStub([doomed], [defend], hp=5, energy=1)
        game.player.powers = [Power("Constricted", "Constricted", 10)]

        self.assertFalse(combat_predictor.safe_to_wait_for_passive_kills(game))
        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)

    def test_burn_consumes_buffer_before_enemy_attack_in_planner(self):
        attacker = monster(
            "Attacker", 50, intent=Intent.ATTACK, damage=20, hits=1
        )
        burn = card("Burn", CardType.STATUS, cost=-2, playable=False)
        defend = card("Big Block", CardType.SKILL, cost=1, block=15)
        game = GameStub([attacker], [burn, defend], hp=10, energy=1)
        game.player.powers = [Power("Buffer", "Buffer", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)

    def test_intangible_caps_offering_self_loss(self):
        attacker = monster(
            "Attacker", 50, intent=Intent.ATTACK, damage=20, hits=1
        )
        offering = card("Offering", CardType.SKILL, cost=0)
        impervious = card("Impervious", CardType.SKILL, cost=2, block=30)
        impervious.is_playable = False
        game = GameStub(
            [attacker], [offering, impervious], hp=2, energy=0
        )
        game.player.powers = [Power("IntangiblePlayer", "Intangible", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, offering)

    def test_buffer_prevents_offering_self_loss(self):
        attacker = monster(
            "Attacker", 50, intent=Intent.ATTACK, damage=20, hits=1
        )
        offering = card("Offering", CardType.SKILL, cost=0)
        impervious = card("Impervious", CardType.SKILL, cost=2, block=30)
        impervious.is_playable = False
        game = GameStub(
            [attacker], [offering, impervious], hp=2, energy=0
        )
        game.player.powers = [Power("Buffer", "Buffer", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, offering)

    def test_dualcast_frost_beats_insufficient_defend(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=15, hits=1)
        dualcast = card("Dualcast", CardType.SKILL)
        defend = card("Defend_B", CardType.SKILL, block=5)
        game = GameStub([attacker], [dualcast, defend], hp=6)
        game.player.orbs = [Orb("Frost", "Frost", 5, 2)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, dualcast)

    def test_dualcast_lightning_is_scored_as_immediate_damage(self):
        target = monster("SphericGuardian", 16)
        dualcast = card("Dualcast", CardType.SKILL, cost=1)
        game = GameStub([target], [dualcast], hp=30, energy=1)
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 3)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, dualcast)
        self.assertEqual(
            16,
            self.planner.last_decision["search"]["first_action_enemy_hp_loss"],
        )
        self.assertEqual(
            [0], self.planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_terminal_plan_drops_dualcast_when_orb_queue_is_empty(self):
        """A stale bound line must not replay a no-orb continuation."""

        target = monster("SphericGuardian", 16)
        dualcast = card("Dualcast", CardType.SKILL, cost=1)
        game = GameStub([target], [dualcast], hp=30, energy=1)
        context = self.planner._combat_turn_key(game)
        self.planner._terminal_plan = {
            "context": context,
            "entries": [{
                "card_id": dualcast.card_id,
                "card_uuid": dualcast.uuid,
                "target_key": None,
            }],
            "search": {"true_combat_end": True},
        }
        self.planner._terminal_plan_selected_uuid = "already-returned"

        action = self.planner._continue_terminal_plan(game)

        self.assertIsNone(action)
        self.assertIsNone(self.planner._terminal_plan)

    def test_multi_enemy_lightning_reports_expected_first_action_damage(self):
        """Random Lightning is useful damage, but cannot claim a kill target."""

        left = monster("FungiBeast", 26)
        right = monster("FungiBeast", 26)
        dualcast = card("Dualcast", CardType.SKILL, cost=1)
        game = GameStub([left, right], [dualcast], hp=30, energy=1)
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 8),
            Orb("Frost", "Frost", 5, 5),
            Orb("Frost", "Frost", 5, 5),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, dualcast)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["first_action_enemy_hp_loss"])
        self.assertEqual(16, search["first_action_expected_enemy_hp_loss"])
        self.assertTrue(search["first_action_enemy_hp_loss_is_expected"])
        self.assertEqual([26, 26], search["final_enemy_hp"])

    def test_full_orb_ball_lightning_counts_overflow_lightning_evoke(self):
        """Replay the observed 7 predicted versus 15 actual channel packet."""

        target = monster("SphericGuardian", 100)
        ball = card(
            "Ball Lightning",
            CardType.ATTACK,
            cost=1,
            damage=7,
            target=True,
        )
        game = GameStub([target], [ball], hp=40, energy=1, act=2)
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Dark", "Dark", 12, 6),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, ball)
        self.assertEqual(
            15,
            self.planner.last_decision["search"][
                "first_action_enemy_hp_loss"
            ],
        )

    def test_full_orb_cold_snap_counts_overflow_lightning_evoke(self):
        """Cold Snap's attack and the displaced orb are one ordered action."""

        target = monster("SphericGuardian", 100)
        cold_snap = card(
            "Cold Snap",
            CardType.ATTACK,
            cost=1,
            damage=6,
            target=True,
        )
        game = GameStub(
            [target], [cold_snap], hp=40, energy=1, act=2
        )
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Dark", "Dark", 12, 6),
            Orb("Lightning", "Lightning", 8, 3),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, cold_snap)
        search = self.planner.last_decision["search"]
        self.assertEqual(14, search["first_action_enemy_hp_loss"])
        self.assertEqual(2, search["final_player_block"])

    def test_cold_snap_overflow_evokes_before_malleable_block(self):
        snake_plant = monster(
            "SnakePlant", 40,
            powers=[Power("Malleable", "Malleable", 3)],
        )
        cold_snap = card(
            "Cold Snap", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [snake_plant], [cold_snap], hp=40, energy=1, act=2
        )
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Dark", "Dark", 12, 6),
            Orb("Lightning", "Lightning", 8, 3),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, cold_snap)
        search = self.planner.last_decision["search"]
        self.assertEqual(14, search["first_action_enemy_hp_loss"])
        self.assertEqual([26], search["final_enemy_hp"])

    def test_full_orb_zap_counts_overflow_dark_evoke(self):
        """A zero-damage channel card still has exact immediate Dark damage."""

        target = monster("SphericGuardian", 60)
        zap = card("Zap", CardType.SKILL, cost=0, upgrades=1)
        game = GameStub([target], [zap], hp=40, energy=0, act=2)
        game.player.orbs = [
            Orb("Dark", "Dark", 12, 6),
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Frost", "Frost", 5, 2),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, zap)
        self.assertEqual(
            12,
            self.planner.last_decision["search"][
                "first_action_enemy_hp_loss"
            ],
        )

    def test_reduced_authoritative_orb_slots_force_zap_overflow(self):
        """Two occupied rows after Consume are a full two-slot queue."""

        target = monster("SphericGuardian", 60)
        zap = card("Zap", CardType.SKILL, cost=0, upgrades=1)
        game = GameStub([target], [zap], hp=40, energy=0, act=2)
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Lightning", "Lightning", 8, 3),
        ]
        game.player.max_orbs = 2

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, zap)
        self.assertEqual(
            8,
            self.planner.last_decision["search"][
                "first_action_enemy_hp_loss"
            ],
        )

    def test_player_json_preserves_authoritative_orb_capacity(self):
        player = Player.from_json({
            "max_hp": 70,
            "current_hp": 40,
            "block": 0,
            "energy": 2,
            "powers": [],
            "orbs": [{
                "name": "Lightning",
                "id": "Lightning",
                "evoke_amount": 8,
                "passive_amount": 3,
            }],
            "max_orbs": 2,
        })

        self.assertEqual(2, player.max_orbs)
        self.assertEqual(1, len(player.orbs))

    def test_tingsha_single_target_damage_is_exact(self):
        target = monster("Cultist", 50)
        all_out = card(
            "All Out Attack", CardType.ATTACK, damage=10
        )
        curse = card(
            "Curse", CardType.CURSE, cost=-2, playable=False
        )
        game = GameStub([target], [all_out, curse], hp=30, energy=1)
        game.relics = [Relic("Tingsha", "Tingsha")]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, all_out)
        search = self.planner.last_decision["search"]
        self.assertEqual(13, search["first_action_enemy_hp_loss"])
        self.assertEqual(13, search["first_action_expected_enemy_hp_loss"])

    def test_tingsha_multi_target_damage_is_expected_not_fake_kill(self):
        left = monster("Centurion", 50)
        right = monster("Mystic", 50)
        all_out = card(
            "All Out Attack", CardType.ATTACK, damage=10
        )
        curse = card(
            "Curse", CardType.CURSE, cost=-2, playable=False
        )
        game = GameStub(
            [left, right], [all_out, curse], hp=30, energy=1
        )
        game.relics = [Relic("Tingsha", "Tingsha")]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, all_out)
        search = self.planner.last_decision["search"]
        self.assertEqual(20, search["first_action_enemy_hp_loss"])
        self.assertEqual(23, search["first_action_expected_enemy_hp_loss"])
        self.assertTrue(search["first_action_enemy_hp_loss_is_expected"])

    def test_echo_form_repeats_channel_and_each_overflow_evoke(self):
        """A duplicated Zap channels twice rather than only duplicating score."""

        target = monster("SphericGuardian", 80)
        zap = card("Zap", CardType.SKILL, cost=0, upgrades=1)
        game = GameStub([target], [zap], hp=40, energy=0, act=2)
        game.player.powers = [Power("Echo Form", "Echo Form", 1)]
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Dark", "Dark", 12, 6),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, zap)
        self.assertEqual(
            16,
            self.planner.last_decision["search"][
                "first_action_enemy_hp_loss"
            ],
        )

    def test_channelled_plasma_evoke_unlocks_same_turn_followup(self):
        """Plasma displaced by Zap must fund the next branch card."""

        target = monster(
            "SphericGuardian",
            32,
            intent=Intent.ATTACK,
            damage=30,
            hits=1,
        )
        zap = card("Zap", CardType.SKILL, cost=0, upgrades=1)
        sunder = card(
            "Sunder",
            CardType.ATTACK,
            cost=2,
            damage=32,
            target=True,
            playable=False,
        )
        game = GameStub(
            [target], [zap, sunder], hp=20, energy=0, act=2
        )
        game.player.orbs = [
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Frost", "Frost", 5, 2),
            Orb("Dark", "Dark", 12, 6),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, zap)
        self.assertEqual(
            ["Zap", "Sunder"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        self.assertTrue(
            self.planner.last_decision["search"]["true_combat_end"]
        )

    def test_dualcast_plasma_energy_unlocks_same_turn_followup(self):
        """Two Plasma evocations grant four usable branch energy."""

        target = monster(
            "SphericGuardian",
            32,
            intent=Intent.ATTACK,
            damage=30,
            hits=1,
        )
        dualcast = card(
            "Dualcast", CardType.SKILL, cost=0, upgrades=1
        )
        sunder = card(
            "Sunder",
            CardType.ATTACK,
            cost=3,
            damage=32,
            target=True,
            playable=False,
        )
        game = GameStub(
            [target], [dualcast, sunder], hp=20, energy=0, act=2
        )
        game.player.orbs = [
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Frost", "Frost", 5, 2),
            Orb("Dark", "Dark", 12, 6),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, dualcast)
        self.assertEqual(
            ["Dualcast", "Sunder"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        self.assertTrue(
            self.planner.last_decision["search"]["true_combat_end"]
        )

    def test_electrodynamics_channels_and_passives_hit_all_enemies(self):
        """The new power applies before its overflow evokes and END passives."""

        left = monster("Louse", 30)
        right = monster("Cultist", 30)
        electro = card(
            "Electrodynamics", CardType.POWER, cost=1, magic=2
        )
        game = GameStub(
            [left, right], [electro], hp=40, energy=1, act=2
        )
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Frost", "Frost", 5, 2),
            Orb("Dark", "Dark", 12, 6),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, electro)
        search = self.planner.last_decision["search"]
        self.assertTrue(search["player_electrodynamics"])
        # The overflow Lightning deals 8 to both immediately; two new
        # Lightning passives then deal 3 + 3 to both at END.
        self.assertEqual(16, search["first_action_enemy_hp_loss"])
        self.assertEqual([16, 16], search["final_enemy_hp"])
        self.assertEqual(5, search["final_player_block"])

    def test_after_image_block_disables_orichalcum_before_attack(self):
        attacker = monster(
            "Attacker",
            80,
            intent=Intent.ATTACK,
            damage=10,
            hits=1,
        )
        strike = card(
            "Strike_G",
            CardType.ATTACK,
            damage=6,
            target=True,
        )
        game = GameStub([attacker], [strike], hp=5, energy=1, act=4)
        game.relics = [Relic("Orichalcum", "Orichalcum")]
        game.player.powers = [Power("AfterImagePower", "After Image", 1)]

        action = self.planner.choose_card_action(game)

        # Ending now takes 4. Playing Strike creates 1 block, disables the
        # relic, and would take 9.
        self.assertIsInstance(action, EndTurnAction)

    def test_new_frost_channel_stacks_after_orichalcum_triggers(self):
        attacker = monster(
            "Attacker",
            80,
            intent=Intent.ATTACK,
            damage=10,
            hits=1,
        )
        cold_snap = card(
            "ColdSnap",
            CardType.ATTACK,
            damage=6,
            target=True,
        )
        game = GameStub([attacker], [cold_snap], hp=5, energy=1, act=4)
        game.relics = [Relic("Orichalcum", "Orichalcum")]
        game.player.orbs = []

        action = self.planner.choose_card_action(game)

        # Orichalcum checks current block before end-turn Frost passives, so
        # the new orb's two block stacks with its six.
        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, cold_snap)

    def test_base_fission_keeps_orichalcum_but_removes_frost_passive(self):
        attacker = monster(
            "Attacker",
            80,
            intent=Intent.ATTACK,
            damage=6,
            hits=1,
        )
        fission = card("Fission", CardType.SKILL, cost=0)
        game = GameStub([attacker], [fission], hp=4, energy=0, act=4)
        game.player.orbs = [Orb("Frost", "Frost", 5, 2)]
        game.relics = [Relic("Orichalcum", "Orichalcum")]

        self.assertEqual(8, combat_predictor.projected_player_block(game))

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, fission)
        self.assertEqual(0, self.planner._last_search["projected_loss"])
        self.assertTrue(self.planner._last_search["orichalcum_reactivated"])
        self.assertEqual(6, self.planner._last_search["final_player_block"])

    def test_fission_defend_does_not_replace_safer_frost_orichalcum_end(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=10, hits=1,
        )
        fission = card("Fission", CardType.SKILL, cost=0)
        defend = card("Defend_B", CardType.SKILL, cost=1, block=7)
        game = GameStub([attacker], [fission, defend], hp=4, energy=0, act=4)
        game.player.orbs = [Orb("Frost", "Frost", 5, 2)]
        game.relics = [Relic("Orichalcum", "Orichalcum")]

        action = self.planner.choose_card_action(game)

        # END gives 2 Frost + 6 Orichalcum block. Fission -> Defend gives
        # only 7, so the old energy bonus induced one extra avoidable HP loss.
        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(2, self.planner._last_search["projected_loss"])
        self.assertEqual(8, self.planner._last_search["final_player_block"])

    def test_large_block_card_correctly_replaces_orichalcum(self):
        attacker = monster(
            "Attacker",
            80,
            intent=Intent.ATTACK,
            damage=10,
            hits=1,
        )
        block_card = card("Big Block", CardType.SKILL, block=8)
        game = GameStub([attacker], [block_card], hp=4, energy=1, act=4)
        game.relics = [Relic("Orichalcum", "Orichalcum")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, block_card)

    def test_two_small_block_cards_pay_orichalcum_cost_only_once(self):
        attacker = monster(
            "Attacker",
            80,
            intent=Intent.ATTACK,
            damage=10,
            hits=1,
        )
        first_block = card("Block-A", CardType.SKILL, block=4)
        second_block = card("Block-B", CardType.SKILL, block=4)
        game = GameStub(
            [attacker],
            [first_block, second_block],
            hp=4,
            energy=2,
            act=4,
        )
        game.relics = [Relic("Orichalcum", "Orichalcum")]

        action = self.planner.choose_card_action(game)

        # Ending uses Orichalcum and takes 4, which is lethal. Either single
        # card disables the relic and is worse, but both cards make 8 block
        # and reduce the hit to 2. The six-block opportunity cost is paid once
        # by the complete plan, so the first block card must be played.
        self.assertIsInstance(action, PlayCardAction)
        self.assertIn(action.card, [first_block, second_block])

    def test_orichalcum_waits_when_one_or_two_small_blocks_do_not_help(self):
        for card_count in (1, 2):
            with self.subTest(card_count=card_count):
                attacker = monster(
                    "Attacker",
                    80,
                    intent=Intent.ATTACK,
                    damage=8,
                    hits=1,
                )
                defends = [
                    card(
                        f"Defend-{index}",
                        CardType.SKILL,
                        cost=1,
                        block=3,
                    )
                    for index in range(card_count)
                ]
                game = GameStub(
                    [attacker], defends, hp=20, energy=3, act=2
                )
                game.relics = [Relic("Orichalcum", "Orichalcum")]
                game.player.powers = [Power("Frail", "Frail", 1)]

                action = self.planner.choose_card_action(game)

                self.assertIsInstance(action, EndTurnAction)
                self.assertEqual(
                    "no_positive_marginal_action",
                    self.planner.last_decision["reason"],
                )
                self.assertEqual(
                    2, self.planner.last_decision["search"]["actual_loss"]
                )

    def test_three_small_blocks_cross_orichalcum_floor_without_early_end(self):
        attacker = monster(
            "Attacker",
            80,
            intent=Intent.ATTACK,
            damage=8,
            hits=1,
        )
        defends = [
            card(
                f"Defend-{index}",
                CardType.SKILL,
                cost=1,
                block=3,
            )
            for index in range(4)
        ]
        game = GameStub(
            [attacker], defends, hp=20, energy=3, act=2
        )
        game.relics = [Relic("Orichalcum", "Orichalcum")]
        game.player.powers = [Power("Frail", "Frail", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIn(action.card, defends)
        self.assertEqual(
            "orichalcum_cumulative_block_threshold",
            self.planner.last_decision["reason"],
        )
        self.assertEqual([3, 6, 9], self.planner.last_decision["prefix_blocks"])
        self.assertEqual(2, self.planner.last_decision["baseline_loss"])
        self.assertEqual(0, self.planner.last_decision["planned_loss"])
        self.assertEqual(
            3, len(self.planner.last_decision["planned_sequence"])
        )
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])

        played = []
        for expected_block in (3, 6, 9):
            self.assertIsInstance(action, PlayCardAction)
            played.append(action.card)
            game.hand.remove(action.card)
            game.player.block += action.card.block
            game.player.energy -= action.card.cost
            self.assertEqual(expected_block, game.player.block)
            action = self.planner.choose_card_action(game)
        self.assertEqual(3, len({id(card) for card in played}))
        self.assertIsInstance(action, EndTurnAction)

    def test_heart_beat_allows_two_card_block_sequence(self):
        heart = monster(
            "CorruptHeart",
            300,
            intent=Intent.ATTACK,
            damage=10,
            hits=1,
            powers=[
                Power("BeatOfDeathPower", "Beat of Death", 2),
                Power("InvinciblePower", "Invincible", 200),
            ],
        )
        first_defend = card("Defend-A", CardType.SKILL, block=5)
        second_defend = card("Defend-B", CardType.SKILL, block=5)
        game = GameStub(
            [heart],
            [first_defend, second_defend],
            hp=5,
            energy=2,
            act=4,
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIn(action.card, [first_defend, second_defend])

    def test_heart_beat_is_reported_as_immediate_card_hp_loss(self):
        heart = monster(
            "CorruptHeart",
            100,
            powers=[Power("BeatOfDeath", "Localized Beat", 1)],
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=100, target=True
        )
        game = GameStub([heart], [strike], hp=20, energy=1, act=4)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertEqual(1, self.planner.last_decision["card_self_hp_cost"])

    def test_blocked_heart_beat_reports_zero_immediate_card_hp_loss(self):
        heart = monster(
            "CorruptHeart",
            300,
            powers=[Power("BeatOfDeath", "Localized Beat", 1)],
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub([heart], [strike], hp=20, energy=1, act=4)
        game.player.block = 1

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertEqual(0, self.planner.last_decision["card_self_hp_cost"])

    def test_heart_beat_rejects_insufficient_single_defend(self):
        heart = monster(
            "CorruptHeart",
            300,
            intent=Intent.ATTACK,
            damage=10,
            hits=1,
            powers=[
                Power("BeatOfDeathPower", "Beat of Death", 2),
                Power("InvinciblePower", "Invincible", 200),
            ],
        )
        defend = card("Defend", CardType.SKILL, block=5)
        game = GameStub([heart], [defend], hp=5, energy=1, act=4)

        self.assertIsInstance(
            self.planner.choose_card_action(game),
            EndTurnAction,
        )

    def test_torii_does_not_reduce_beat_of_death_in_block_search(self):
        heart = monster(
            "CorruptHeart",
            300,
            powers=[Power("BeatOfDeathPower", "Beat of Death", 5)],
        )
        defend = card("Defend", CardType.SKILL, block=1)
        game = GameStub([heart], [defend], hp=4, energy=1, act=4)
        game.relics = [Relic("Torii", "Torii")]

        self.assertFalse(
            self.planner._heart_block_sequence_survives(game, heart, defend)
        )

    def test_torii_does_not_reduce_each_pain_trigger_in_block_search(self):
        heart = monster(
            "CorruptHeart",
            300,
            powers=[Power("BeatOfDeathPower", "Beat of Death", 1)],
        )
        defend = card("Defend", CardType.SKILL, block=5)
        pains = [
            card(f"Pain-{index}", CardType.CURSE, cost=-2, playable=False)
            for index in range(2)
        ]
        for pain in pains:
            pain.card_id = "Pain"
        game = GameStub(
            [heart],
            [defend, *pains],
            hp=2,
            energy=1,
            act=4,
        )
        game.relics = [Relic("Torii", "Torii")]

        self.assertFalse(
            self.planner._heart_block_sequence_survives(game, heart, defend)
        )

    def test_tungsten_rod_still_reduces_beat_of_death(self):
        heart = monster(
            "CorruptHeart",
            300,
            powers=[Power("BeatOfDeathPower", "Beat of Death", 2)],
        )
        defend = card("Defend", CardType.SKILL, block=1)
        game = GameStub([heart], [defend], hp=1, energy=1, act=4)
        game.relics = [Relic("Tungsten Rod", "Tungsten Rod")]

        self.assertTrue(
            self.planner._heart_block_sequence_survives(game, heart, defend)
        )

    def test_second_wind_does_not_create_false_heart_block_sequence(self):
        heart = monster(
            "CorruptHeart",
            300,
            intent=Intent.ATTACK,
            damage=10,
            hits=1,
            powers=[Power("BeatOfDeathPower", "Beat of Death", 2)],
        )
        defend = card("Defend", CardType.SKILL, block=5)
        second_wind = card("Second Wind", CardType.SKILL, block=5)
        game = GameStub(
            [heart],
            [defend, second_wind],
            hp=5,
            energy=2,
            act=4,
        )

        # If Defend is first, it has left the hand and cannot increase Second
        # Wind's exhaust count. If Second Wind is first, it exhausts Defend,
        # so Defend cannot be played afterward. Neither ordering survives.
        self.assertFalse(
            self.planner._heart_block_sequence_survives(game, heart, defend)
        )
        self.assertFalse(
            self.planner._heart_block_sequence_survives(
                game, heart, second_wind
            )
        )

    def test_wraith_form_intangible_does_not_count_as_beat_block(self):
        heart = monster(
            "CorruptHeart",
            300,
            intent=Intent.ATTACK,
            damage=40,
            hits=1,
            powers=[
                Power("BeatOfDeathPower", "Beat of Death", 2),
                Power("InvinciblePower", "Invincible", 200),
            ],
        )
        wraith = card("Wraith Form v2", CardType.POWER, cost=3, magic=2)
        game = GameStub([heart], [wraith], hp=1, energy=3, act=4)

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_killing_attacker_counts_as_full_mitigation(self):
        attacker = monster("Attacker", 6, intent=Intent.ATTACK, damage=10, hits=1)
        strike = card("Strike_R", CardType.ATTACK, damage=6, target=True)
        defend = card("Defend_R", CardType.SKILL, block=5)
        game = GameStub([attacker], [strike, defend], hp=4)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)

    def test_artifact_blocks_piercing_wail_mitigation(self):
        attacker = monster(
            "Attacker",
            80,
            intent=Intent.ATTACK,
            damage=10,
            hits=5,
            powers=[Power("Artifact", "Artifact", 1)],
        )
        wail = card("PiercingWail", CardType.SKILL, magic=6)
        block_card = card("Big Block", CardType.SKILL, block=25)
        game = GameStub([attacker], [wail, block_card], hp=26)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, block_card)

    def test_strength_down_respects_existing_weak_multiplier(self):
        attacker = monster(
            "BookOfStabbing",
            160,
            intent=Intent.ATTACK,
            damage=7,
            hits=5,
            powers=[Power("Weakened", "Weak", 1)],
        )
        wail = card("PiercingWail", CardType.SKILL, cost=1, magic=6)
        block_card = card("Big Block", CardType.SKILL, cost=1, block=25)
        game = GameStub([attacker], [wail, block_card], hp=11, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, block_card)

    def test_multihit_thorns_self_damage_has_hard_lethal_guard(self):
        spiker = monster(
            "Spiker",
            100,
            powers=[Power("Thorns", "Thorns", 3)],
        )
        riddle = card("Riddle With Holes", CardType.ATTACK, damage=3, target=True)
        game = GameStub([spiker], [riddle], hp=10)

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_echo_form_first_attack_charges_duplicate_spiker_thorns(self):
        spiker = monster(
            "Spiker", 15, powers=[Power("Thorns", "Thorns", 9)]
        )
        melter = card("Melter", CardType.ATTACK, cost=1, damage=10)
        game = GameStub([spiker], [melter], hp=50, energy=1)
        game.player.powers = [Power("Echo Form", "Echo Form", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, melter)
        self.assertEqual(18, self.planner._last_search["reactive_hp_cost"])
        self.assertEqual(18, self.planner.last_decision["card_self_hp_cost"])

    def test_random_multihit_prices_reachable_spiker_thorns(self):
        spikers = [
            monster(
                f"Spiker-{index}",
                hp,
                powers=[Power("Thorns", "Thorns", thorns)],
            )
            for index, (hp, thorns) in enumerate(((6, 5), (15, 7)))
        ]
        boomerang = card(
            "Sword Boomerang",
            CardType.ATTACK,
            cost=1,
            damage=9,
            target=False,
            upgrades=1,
        )
        game = GameStub(spikers, [boomerang], hp=7, energy=1)

        candidate = self.planner._candidate(
            game, boomerang, None, spikers[0], 0, 0, 0
        )
        action = self.planner.choose_card_action(game)

        self.assertEqual((7, 7, 7, 7), candidate.thorns_damage_events)
        self.assertEqual(28, candidate.reactive_hp_cost)
        self.assertIsInstance(action, EndTurnAction)

    def test_aoe_combo_counts_thorns_from_every_living_target(self):
        repulsor = monster("Repulsor", 12)
        spikers = [
            monster(
                f"Spiker-{index}",
                40,
                powers=[Power("Thorns", "Thorns", thorns)],
            )
            for index, thorns in enumerate((3, 5))
        ]
        thunderclap = card(
            "Thunderclap", CardType.ATTACK,
            cost=1, damage=6, target=False,
        )
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [repulsor, *spikers],
            [thunderclap, strike],
            hp=8,
            energy=2,
        )

        combo = self.planner._guaranteed_attack_combo(
            game,
            [thunderclap, strike],
            [repulsor, *spikers],
            total_loss=0,
            end_turn_loss=0,
        )

        self.assertIsNone(combo)

    def test_tungsten_allows_one_hp_spiker_kill(self):
        spiker = monster(
            "Spiker",
            8,
            powers=[Power("Thorns", "Thorns", 1)],
        )
        strike = card("Strike_B", CardType.ATTACK, damage=9, target=True)
        game = GameStub([spiker], [strike], hp=1, energy=1)
        game.relics = [Relic("Tungsten Rod", "Tungsten Rod")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertEqual(0, self.planner._last_search["reactive_hp_cost"])

    def test_buffer_is_consumed_before_tungsten_on_multihit_thorns(self):
        spiker = monster(
            "Spiker",
            8,
            powers=[Power("Thorns", "Thorns", 1)],
        )
        riddle = card(
            "Riddle With Holes", CardType.ATTACK, damage=3, target=True
        )
        game = GameStub([spiker], [riddle], hp=70, energy=1)
        game.player.powers = [Power("Buffer", "Buffer", 1)]
        game.relics = [Relic("Tungsten Rod", "Tungsten Rod")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, riddle)
        self.assertEqual(0, self.planner._last_search["reactive_hp_cost"])
        self.assertEqual(0, self.planner._last_search["player_buffer"])

    def test_sharp_hide_triggers_once_for_a_multihit_attack(self):
        guardian = monster(
            "TheGuardian",
            8,
            powers=[Power("SharpHidePower", "Sharp Hide", 3)],
        )
        riddle = card(
            "Riddle With Holes", CardType.ATTACK, damage=3, target=True
        )
        game = GameStub([guardian], [riddle], hp=70, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, riddle)
        self.assertEqual(3, self.planner._last_search["reactive_hp_cost"])

    def test_zero_damage_attack_still_triggers_sharp_hide_once(self):
        guardian = monster(
            "TheGuardian",
            80,
            powers=[Power("SharpHidePower", "Sharp Hide", 3)],
        )
        whirlwind = card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=5, target=False
        )
        game = GameStub([guardian], [whirlwind], hp=20, energy=0)

        candidate = self.planner._candidate(
            game, whirlwind, None, guardian, 0, 0, 0
        )

        self.assertEqual((3,), candidate.sharp_hide_damage_events)
        self.assertEqual(3, candidate.reactive_hp_cost)

    def test_tungsten_reduces_each_pain_trigger_separately(self):
        target = monster("Target", 8)
        strike = card("Strike_B", CardType.ATTACK, damage=9, target=True)
        pains = [
            card(f"Pain-{index}", CardType.CURSE, cost=-2, playable=False)
            for index in range(2)
        ]
        for pain in pains:
            pain.card_id = "Pain"
        game = GameStub([target], [strike, *pains], hp=1, energy=1)
        game.relics = [Relic("Tungsten Rod", "Tungsten Rod")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)

    def test_existing_block_absorbs_spiker_thorns_before_hp(self):
        spiker = monster(
            "Spiker",
            8,
            powers=[Power("Thorns", "Thorns", 7)],
        )
        strike = card("Strike_B", CardType.ATTACK, damage=9, target=True)
        game = GameStub([spiker], [strike], hp=1, block=7, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertEqual(0, self.planner._last_search["reactive_hp_cost"])

    def test_defend_block_carries_into_followup_spiker_attack(self):
        spiker = monster(
            "Spiker",
            8,
            powers=[Power("Thorns", "Thorns", 7)],
        )
        defend = card("Defend_B", CardType.SKILL, cost=0, block=7)
        strike = card("Strike_B", CardType.ATTACK, cost=1, damage=9, target=True)
        game = GameStub([spiker], [strike, defend], hp=1, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)
        self.assertEqual(
            ["Defend_B", "Strike_B"],
            [
                item["card_id"]
                for item in self.planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(0, self.planner._last_search["reactive_hp_cost"])

    def test_after_image_absorbs_sharp_hide_after_attack(self):
        guardian = monster(
            "TheGuardian",
            8,
            powers=[Power("SharpHidePower", "Sharp Hide", 1)],
        )
        strike = card("Strike_G", CardType.ATTACK, damage=9, target=True)
        game = GameStub([guardian], [strike], hp=1, energy=1)
        game.player.powers = [Power("AfterImagePower", "After Image", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertEqual(0, self.planner._last_search["reactive_hp_cost"])

    def test_affordable_spiker_kill_prevents_permanent_zero_loss_wait(self):
        spiker = monster(
            "Spiker",
            8,
            powers=[Power("Thorns", "Thorns", 7)],
        )
        strike = card("Strike_B", CardType.ATTACK, damage=9, target=True)
        game = GameStub([spiker], [strike], hp=27, energy=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)

    def test_spiker_kill_is_still_rejected_when_reactive_damage_is_critical(self):
        spiker = monster(
            "Spiker",
            8,
            powers=[Power("Thorns", "Thorns", 7)],
        )
        strike = card("Strike_B", CardType.ATTACK, damage=9, target=True)
        game = GameStub([spiker], [strike], hp=8, energy=1)

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_high_but_safe_spiker_thorns_does_not_cause_permanent_wait(self):
        spiker = monster(
            "Spiker",
            8,
            powers=[Power("Thorns", "Thorns", 11)],
        )
        strike = card("Strike_B", CardType.ATTACK, damage=9, target=True)
        game = GameStub([spiker], [strike], hp=70, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)

    def test_exact_multi_card_spiker_lethal_keeps_low_hp_reserve(self):
        spiker = monster(
            "Spiker",
            12,
            powers=[Power("Thorns", "Thorns", 7)],
        )
        strikes = [
            card(f"Strike-{index}", CardType.ATTACK, damage=6, target=True)
            for index in range(2)
        ]
        game = GameStub([spiker], strikes, hp=15, energy=2)

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_injured_player_can_finish_spiker_above_dynamic_reserve(self):
        spiker = monster(
            "Spiker",
            8,
            powers=[Power("Thorns", "Thorns", 11)],
        )
        strike = card("Strike_B", CardType.ATTACK, damage=9, target=True)
        game = GameStub([spiker], [strike], hp=20, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)

    def test_artifact_source_is_played_before_biased_cognition(self):
        target = monster("Target", 100)
        panacea = card("Panacea", CardType.SKILL, cost=0, magic=1)
        biased = card("Biased Cognition", CardType.POWER, cost=1)
        game = GameStub([target], [biased, panacea], hp=30, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, panacea)

    def test_fission_with_no_orbs_is_not_invented_as_energy_or_draw(self):
        target = monster("Target", 100)
        fission = card("Fission", CardType.SKILL, cost=0)
        game = GameStub([target], [fission], hp=30, energy=1)

        self.assertEqual(0, self.planner._energy_gain(fission, game))
        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_fission_energy_scales_with_orbs_and_upgraded_plasma_evoke(self):
        target = monster("Target", 100)
        fission = card("Fission", CardType.SKILL, cost=0, upgrades=1)
        game = GameStub([target], [fission], hp=30, energy=1)
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Empty", "Empty", 0, 0),
        ]

        self.assertEqual(4, self.planner._energy_gain(fission, game))

    def test_upgraded_fission_uses_lightning_evoke_against_single_enemy(self):
        target = monster("Target", 8)
        fission = card("Fission", CardType.SKILL, cost=0, upgrades=1)
        game = GameStub([target], [fission], hp=30, energy=1)
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 3)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, fission)

    def test_upgraded_fission_evoke_respects_enemy_block(self):
        target = monster("Target", 10)
        target.block = 50
        fission = card("Fission", CardType.SKILL, cost=0, upgrades=1)
        game = GameStub([target], [fission], hp=30, energy=1)
        game.player.orbs = [Orb("Dark", "Dark", 50, 6)]

        groups = self.planner._build_candidates(
            game, [fission], [target], target, 0, 0, 0
        )
        _, plan = self.planner._best_plan(game, groups, 0, 0, 0)

        self.assertEqual([fission], [candidate.card for candidate in plan])
        self.assertEqual(0, self.planner._last_search["enemies_dead"])

    def test_upgraded_fission_evoke_respects_intangible(self):
        target = monster(
            "Nemesis",
            10,
            powers=[Power("Intangible", "Intangible", 1)],
        )
        fission = card("Fission", CardType.SKILL, cost=0, upgrades=1)
        game = GameStub([target], [fission], hp=30, energy=1)
        game.player.orbs = [Orb("Dark", "Dark", 50, 6)]

        groups = self.planner._build_candidates(
            game, [fission], [target], target, 0, 0, 0
        )
        _, plan = self.planner._best_plan(game, groups, 0, 0, 0)

        self.assertEqual([fission], [candidate.card for candidate in plan])
        self.assertEqual(0, self.planner._last_search["enemies_dead"])

    def test_upgraded_fission_never_claims_invincible_kill(self):
        target = monster(
            "CorruptHeart",
            10,
            powers=[Power("InvinciblePower", "Invincible", 200)],
        )
        fission = card("Fission", CardType.SKILL, cost=0, upgrades=1)
        game = GameStub([target], [fission], hp=30, energy=1, act=4)
        game.player.orbs = [Orb("Dark", "Dark", 50, 6)]

        groups = self.planner._build_candidates(
            game, [fission], [target], target, 0, 0, 0
        )
        _, plan = self.planner._best_plan(game, groups, 0, 0, 0)

        self.assertEqual([fission], [candidate.card for candidate in plan])
        self.assertEqual(0, self.planner._last_search["enemies_dead"])

    def test_fission_dark_targets_poison_doomed_lowest_hp_enemy(self):
        doomed = monster(
            "Doomed",
            5,
            powers=[Power("Poison", "Poison", 5)],
        )
        attacker = monster(
            "Attacker",
            40,
            intent=Intent.ATTACK,
            damage=45,
            hits=1,
        )
        fission = card("Fission", CardType.SKILL, cost=0, upgrades=1)
        defend = card("Defend_B", CardType.SKILL, cost=1, block=30)
        game = GameStub([doomed, attacker], [fission, defend], hp=30, energy=1)
        game.player.orbs = [Orb("Dark", "Dark", 50, 6)]

        action = self.planner.choose_card_action(game)

        # Dark evokes before poison and must hit the still-living 5-HP
        # monster. It cannot be redirected to invent a kill on the attacker.
        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)
        self.assertEqual(1, self.planner._last_search["enemies_dead"])
        self.assertEqual(15, self.planner._last_search["projected_loss"])

    def test_base_fission_preserves_a_charged_dark_orb(self):
        target = monster("Target", 100)
        fission = card("Fission", CardType.SKILL, cost=0)
        game = GameStub([target], [fission], hp=30, energy=1)
        game.player.orbs = [Orb("Dark", "Dark", 50, 6)]

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_fission_consumes_orbs_only_once_in_turn_search(self):
        target = monster("Target", 100)
        first = card("Fission", CardType.SKILL, cost=0, upgrades=1)
        second = card("Fission", CardType.SKILL, cost=0, upgrades=1)
        first.uuid = "fission-1"
        second.uuid = "fission-2"
        game = GameStub([target], [first, second], hp=30, energy=0)
        game.player.orbs = [Orb("Plasma", "Plasma", 2, 1)]

        groups = self.planner._build_candidates(
            game, [first, second], [target], target, 0, 0, 0
        )
        _, plan = self.planner._best_plan(game, groups, 0, 0, 0)

        self.assertEqual(1, len(plan))
        self.assertEqual("Fission", plan[0].card.card_id)

    def test_fission_and_dualcast_cannot_reuse_the_same_orb(self):
        target = monster("Target", 100)
        fission = card("Fission", CardType.SKILL, cost=0, upgrades=1)
        dualcast = card("Dualcast", CardType.SKILL, cost=0)
        game = GameStub([target], [fission, dualcast], hp=30, energy=0)
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 3)]

        groups = self.planner._build_candidates(
            game, [fission, dualcast], [target], target, 0, 0, 0
        )
        _, plan = self.planner._best_plan(game, groups, 0, 0, 0)

        orb_consumers = {
            candidate.card.card_id for candidate in plan
            if candidate.card.card_id in {"Fission", "Dualcast"}
        }
        self.assertEqual(1, len(orb_consumers))

    def test_attack_targets_active_enemy_not_poison_doomed_enemy(self):
        doomed = monster(
            "Doomed",
            5,
            intent=Intent.ATTACK,
            damage=20,
            hits=1,
            powers=[Power("Poison", "Poison", 5)],
        )
        active = monster("Active", 30, intent=Intent.ATTACK, damage=6, hits=1)
        strike = card("Strike_G", CardType.ATTACK, damage=6, target=True)
        game = GameStub([doomed, active], [strike], hp=30, energy=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, active)

    def test_bloodletting_is_used_to_unlock_lethal_block(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=10, hits=1)
        bloodletting = card("Bloodletting", CardType.SKILL, cost=0)
        impervious = card(
            "Impervious",
            CardType.SKILL,
            cost=2,
            block=30,
            playable=False,
        )
        game = GameStub([attacker], [bloodletting, impervious], hp=10, energy=0)
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, bloodletting)

    def test_offering_rescues_zero_energy_hexaghost_turn(self):
        """Do not leave Offering unused before a large multi-hit attack."""

        attacker = monster(
            "Hexaghost", 218, intent=Intent.ATTACK, damage=5, hits=6
        )
        offering = card(
            "Offering", CardType.SKILL, cost=0, magic=5, upgrades=1
        )
        offering.base_block = -1
        offering.block = 0
        offering.exhausts = True
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6,
            target=True, playable=False
        )
        game = GameStub(
            [attacker], [offering, strike], hp=57, block=10, energy=0, act=1
        )
        game.player.max_hp = 72
        game.turn = 2
        # The rescue is justified by available draws, not free energy alone.
        game.draw_pile = [
            card("Defend_R", CardType.SKILL, cost=1, block=5),
            card("Strike_R", CardType.ATTACK, cost=1, damage=6, target=True),
        ]

        action = FastCombatPlanner(IroncladPriority()).choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, offering)

    def test_acrobatics_is_played_when_draw_has_positive_value(self):
        idle = monster("Idle", 50)
        acrobatics = card("Acrobatics", CardType.SKILL, cost=1)
        game = GameStub([idle], [acrobatics], hp=50, energy=3)
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, acrobatics)

    def test_reinforced_body_uses_all_remaining_energy_for_block(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=20, hits=1)
        reinforced_body = card("Reinforced Body", CardType.SKILL, cost=-1, block=7)
        defends = [
            card(f"Defend_B-{index}", CardType.SKILL, cost=1, block=5)
            for index in range(2)
        ]
        for defend in defends:
            defend.card_id = "Defend_B"
        game = GameStub(
            [attacker],
            [reinforced_body, *defends],
            hp=6,
            energy=3,
        )
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, reinforced_body)

    def test_reinforced_body_precedes_setup_against_double_orb_walkers(self):
        """Do not spend X-cost block energy on setup in a high-pressure race."""

        walkers = [
            monster(
                f"OrbWalker-{index}",
                hp,
                intent=Intent.ATTACK,
                damage=damage,
                hits=1,
            )
            for index, (hp, damage) in enumerate(((99, 10), (97, 15)))
        ]
        for walker in walkers:
            walker.monster_id = "OrbWalker"
        self_repair = card("Self Repair", CardType.POWER, cost=1, magic=7)
        loop = card("Loop", CardType.POWER, cost=1, magic=1)
        reinforced_body = card(
            "Reinforced Body", CardType.SKILL, cost=-1, block=7,
        )
        game = GameStub(
            walkers,
            [self_repair, loop, reinforced_body],
            hp=58,
            energy=4,
            act=3,
        )
        game.turn = 1
        game.relics = [Relic("Ice Cream", "Ice Cream")]
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, reinforced_body)
        self.assertEqual(0, planner.last_decision["search"]["actual_loss"])
        self.assertEqual(28, planner.last_decision["search"]["final_player_block"])

    def test_chemical_x_reinforced_body_blocks_at_zero_energy(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=10, hits=1
        )
        reinforced_body = card(
            "Reinforced Body", CardType.SKILL, cost=-1, block=7
        )
        game = GameStub(
            [attacker], [reinforced_body], hp=5, energy=0
        )
        game.relics = [Relic("Chemical X", "Chemical X")]
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, reinforced_body)
        self.assertEqual(14, planner.last_decision["search"]["final_player_block"])
        self.assertEqual(0, planner.last_decision["search"]["actual_loss"])

    def test_buffer_is_treated_as_immediate_one_hit_mitigation(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=20, hits=1)
        buffer = card("Buffer", CardType.POWER, cost=2)
        defend = card("Defend_B", CardType.SKILL, cost=1, block=5)
        game = GameStub([attacker], [buffer, defend], hp=10, energy=2)
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, buffer)

    def test_auto_shields_is_not_counted_when_player_already_has_block(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=10, hits=1)
        auto_shields = card("Auto Shields", CardType.SKILL, cost=1, block=11)
        defend = card("Defend_B", CardType.SKILL, cost=1, block=5)
        game = GameStub(
            [attacker],
            [auto_shields, defend],
            hp=5,
            block=1,
            energy=1,
        )
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)

    def test_disarm_strength_reduction_is_counted_against_multihit(self):
        book = monster(
            "BookOfStabbing",
            160,
            intent=Intent.ATTACK,
            damage=10,
            hits=5,
        )
        disarm = card(
            "Disarm",
            CardType.SKILL,
            cost=1,
            magic=3,
            target=True,
            upgrades=1,
        )
        block_card = card("Big Block", CardType.SKILL, cost=1, block=10)
        game = GameStub([book], [disarm, block_card], hp=36, energy=1)
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, disarm)

    def test_passively_doomed_enemy_is_killed_before_lethal_combust_tick(self):
        doomed = monster(
            "Cultist",
            5,
            powers=[Power("Poison", "Poison", 5)],
        )
        alchemize = card("Alchemize", CardType.SKILL, cost=1)
        strike = card("Strike_G", CardType.ATTACK, cost=1, damage=6, target=True)
        game = GameStub([doomed], [alchemize, strike], hp=1, energy=1)
        game.player.powers = [Power("Combust", "Combust", 5)]
        game.are_potions_full = lambda: False
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)

    def test_poison_card_prefers_target_without_artifact(self):
        artifact_target = monster(
            "CultistWithArtifact",
            40,
            powers=[Power("Artifact", "Artifact", 1)],
        )
        clean_target = monster("CultistWithoutArtifact", 40)
        deadly_poison = card(
            "Deadly Poison",
            CardType.SKILL,
            cost=1,
            magic=5,
            target=True,
        )
        game = GameStub([artifact_target, clean_target], [deadly_poison], hp=50)
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, deadly_poison)
        self.assertIs(action.target_monster, clean_target)

    def test_poison_card_prefers_live_target_over_passively_doomed_target(self):
        """Do not spend a poison application on a target that poison already kills."""
        doomed = monster(
            "Doomed",
            5,
            powers=[Power("Poison", "Poison", 5)],
        )
        active = monster(
            "Active",
            40,
            intent=Intent.ATTACK,
            damage=12,
            hits=1,
        )
        deadly_poison = card(
            "Deadly Poison",
            CardType.SKILL,
            cost=1,
            magic=5,
            target=True,
        )
        game = GameStub([doomed, active], [deadly_poison], hp=50)

        self.assertEqual(0.0, self.planner._poison_value(game, deadly_poison, doomed))
        self.assertGreater(
            self.planner._poison_value(game, deadly_poison, active),
            0.0,
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, deadly_poison)
        self.assertIs(action.target_monster, active)

    def test_poison_card_is_not_selected_when_every_target_is_passively_doomed(self):
        doomed = monster(
            "Doomed",
            5,
            powers=[Power("Poison", "Poison", 5)],
        )
        deadly_poison = card(
            "Deadly Poison",
            CardType.SKILL,
            cost=1,
            magic=5,
            target=True,
        )
        game = GameStub([doomed], [deadly_poison], hp=50)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual("all_enemies_passively_doomed", self.planner.last_decision["reason"])

    def test_redo_alias_evokes_frost_before_lethal_attack(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=8, hits=1)
        redo = card("Redo", CardType.SKILL, cost=0, upgrades=1)
        game = GameStub([attacker], [redo], hp=5, energy=0)
        game.player.orbs = [Orb("Frost", "Frost", 5, 2)]
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, redo)

    def test_upgraded_multicast_evokes_once_at_zero_energy(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=5, hits=1)
        multicast = card("Multi-Cast", CardType.SKILL, cost=-1, upgrades=1)
        game = GameStub([attacker], [multicast], hp=3, energy=0)
        game.player.orbs = [Orb("Frost", "Frost", 5, 2)]
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, multicast)

    def test_multicast_uses_branch_energy_after_channel_setup(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=9, hits=1
        )
        cold_snap = card(
            "Cold Snap", CardType.ATTACK, cost=1, damage=1, target=True
        )
        multicast = card("Multi-Cast", CardType.SKILL, cost=-1)
        game = GameStub(
            [attacker], [multicast, cold_snap], hp=5, energy=1
        )
        game.relics = [Relic("Chemical X", "Chemical X")]
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, cold_snap)
        self.assertEqual(
            ["Cold Snap", "Multi-Cast"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        # Cold Snap consumes the only energy. Chemical X therefore grants
        # exactly two five-block Frost evokes, not the stale 3x from turn
        # start energy plus Chemical X.
        self.assertEqual(10, planner.last_decision["search"]["final_player_block"])
        self.assertEqual(0, planner.last_decision["search"]["actual_loss"])

    def test_chemical_x_and_upgrade_keep_malaise_effect_at_zero_energy(self):
        attacker = monster(
            "Attacker", 80, intent=Intent.ATTACK, damage=5, hits=3
        )
        malaise = card(
            "Malaise",
            CardType.SKILL,
            cost=-1,
            target=True,
            upgrades=1,
        )
        game = GameStub([attacker], [malaise], hp=5, energy=0)
        game.relics = [Relic("Chemical X", "Chemical X")]
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, malaise)
        self.assertEqual(3, planner.last_decision["search"]["actual_loss"])

    def test_ghostly_alias_grants_immediate_intangible(self):
        attacker = monster("Attacker", 80, intent=Intent.ATTACK, damage=40, hits=1)
        apparition = card("Ghostly", CardType.SKILL, cost=1)
        defend = card("Defend_G", CardType.SKILL, cost=1, block=5)
        game = GameStub([attacker], [apparition, defend], hp=10, energy=1)
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, apparition)

    def test_champ_transition_does_not_reuse_buffer_consumed_this_turn(self):
        champ = Monster(
            "Champ",
            "Champ",
            420,
            214,
            0,
            Intent.ATTACK,
            False,
            False,
            move_adjusted_damage=20,
            move_hits=1,
        )
        strike = card("Strike_G", CardType.ATTACK, cost=1, damage=6, target=True)
        backflip = card("Backflip", CardType.SKILL, cost=1, block=5)
        game = GameStub([champ], [strike, backflip], hp=25, energy=1, act=2)
        game.player.powers = [Power("Buffer", "Buffer", 1)]
        game.floor = 33
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, backflip)

    def test_heart_beat_consumes_new_buffer_before_enemy_attack(self):
        heart = monster(
            "CorruptHeart",
            300,
            intent=Intent.ATTACK,
            damage=40,
            hits=1,
            powers=[
                Power("BeatOfDeathPower", "Beat of Death", 2),
                Power("InvinciblePower", "Invincible", 200),
            ],
        )
        buffer = card("Buffer", CardType.POWER, cost=2)
        apparition = card("Ghostly", CardType.SKILL, cost=1)
        game = GameStub([heart], [buffer, apparition], hp=10, energy=2, act=4)
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, apparition)

    def test_new_intangible_caps_beat_and_then_enemy_attack(self):
        heart = monster(
            "CorruptHeart",
            300,
            intent=Intent.ATTACK,
            damage=20,
            hits=1,
            powers=[
                Power("BeatOfDeathPower", "Beat of Death", 2),
                Power("InvinciblePower", "Invincible", 200),
            ],
        )
        apparition = card("Ghostly", CardType.SKILL, cost=1)
        game = GameStub([heart], [apparition], hp=3, energy=1, act=4)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, apparition)
        self.assertEqual(1, self.planner._last_search["projected_loss"])
        self.assertEqual(2, self.planner._last_search["actual_loss"])

    def test_heart_beat_does_not_reuse_existing_buffer_after_playing_attack(self):
        heart = monster(
            "CorruptHeart",
            300,
            intent=Intent.ATTACK,
            damage=40,
            hits=1,
            powers=[
                Power("BeatOfDeathPower", "Beat of Death", 2),
                Power("InvinciblePower", "Invincible", 200),
            ],
        )
        strike = card("Strike_G", CardType.ATTACK, cost=1, damage=6, target=True)
        game = GameStub([heart], [strike], hp=10, energy=1, act=4)
        game.player.powers = [Power("Buffer", "Buffer", 1)]
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_time_eater_twelfth_card_uses_real_block_instead_of_passing(self):
        eater = monster(
            "TimeEater",
            456,
            intent=Intent.ATTACK,
            damage=26,
            hits=1,
            powers=[Power("TimeWarpPower", "Time Warp", 11)],
        )
        survivor = card("Survivor", CardType.SKILL, cost=1, block=11)
        strike = card("Strike_G", CardType.ATTACK, cost=1, damage=6, target=True)
        game = GameStub([eater], [survivor, strike], hp=37, energy=3, act=3)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, survivor)

    def test_time_eater_counter_eleven_resets_when_small_extra_loss_is_safe(self):
        """Replay seq 117777: do not carry an avoidable one-card turn."""

        eater = monster(
            "TimeEater",
            395,
            intent=Intent.ATTACK,
            damage=24,
            hits=1,
            powers=[Power("TimeWarpPower", "Time Warp", 11)],
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=9, target=True
        )
        anger = card(
            "Anger", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub(
            [eater], [strike, anger], hp=68, energy=1, act=3
        )
        game.room_type = "MonsterRoomBoss"
        game.player.block = 10

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertTrue(self.planner.last_decision["search"]["forced_end"])

    def test_time_eater_counter_eleven_resets_on_non_attack_setup_turn(self):
        """Replay seq 122030: do not defer reset to next turn's first card."""

        eater = monster(
            "TimeEater",
            324,
            intent=Intent.DEFEND_DEBUFF,
            powers=[Power("TimeWarpPower", "Time Warp", 11)],
        )
        shrug = card(
            "Shrug It Off", CardType.SKILL, cost=1, block=8, magic=1
        )
        metallicize = card(
            "Metallicize", CardType.POWER, cost=1, magic=3
        )
        game = GameStub(
            [eater], [shrug, metallicize], hp=19, energy=4, act=3
        )
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIn(action.card, (shrug, metallicize))
        self.assertTrue(self.planner.last_decision["search"]["forced_end"])

    def test_time_eater_counter_eleven_uses_safe_reset_card_when_search_is_empty(self):
        """A harmless defend/debuff intent must not defer the forced reset."""

        eater = monster(
            "TimeEater",
            220,
            intent=Intent.DEFEND_DEBUFF,
            powers=[
                Power("TimeWarpPower", "Time Warp", 11),
                Power("Strength", "Strength", 6),
                Power("Poison", "Poison", 5),
            ],
        )
        panacea = card("Panacea", CardType.SKILL, cost=0, magic=1)
        panacea.exhausts = True
        defend = card("Defend_G", CardType.SKILL, cost=1, block=8)
        finisher = card(
            "Finisher", CardType.ATTACK, cost=1, damage=8, target=True
        )
        game = GameStub(
            [eater], [panacea, defend, finisher], hp=13, block=4,
            energy=3, act=3,
        )
        game.player.max_hp = 81
        game.player.powers = [
            Power("Strength", "Strength", 2),
            Power("AfterImagePower", "After Image", 2),
            Power("InfiniteBladesPower", "Infinite Blades", 1),
            Power("ThousandCutsPower", "Thousand Cuts", 1),
            Power("WraithFormPower", "Wraith Form v2", -1),
        ]
        planner = FastCombatPlanner(SilentPriority())

        with patch.object(planner, "_best_plan", return_value=(0.0, [])):
            action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual("time_eater_reset_progress", planner.last_decision["reason"])
        self.assertIs(action.card, defend)

    def test_time_eater_seq_225910_uses_persistent_setup_as_twelfth_card(self):
        """Do not waste the next turn's first card on an avoidable reset."""

        eater = monster(
            "TimeEater",
            397,
            intent=Intent.ATTACK,
            damage=7,
            hits=3,
            powers=[
                Power("TimeWarpPower", "Time Warp", 11),
                Power("Poison", "Poison", 11),
            ],
        )
        eater.max_hp = 456
        all_out_attack = card(
            "All Out Attack", CardType.ATTACK,
            cost=1, damage=12,
        )
        noxious_fumes = card(
            "Noxious Fumes", CardType.POWER,
            cost=1, magic=2,
        )
        bullet_time = card(
            "Bullet Time", CardType.SKILL,
            cost=2,
        )
        curse = card(
            "CurseOfTheBell", CardType.CURSE,
            cost=-2, playable=True,
        )
        game = GameStub(
            [eater],
            [all_out_attack, noxious_fumes, bullet_time, curse],
            hp=44, block=23, energy=3, act=3,
        )
        game.room_type = "MonsterRoomBoss"
        game.player.powers = [
            Power("Thorns", "Thorns", 3),
            Power("Strength", "Strength", 2),
            Power("NoxiousFumesPower", "Noxious Fumes", 3),
        ]
        game.relics = [Relic("Blue Candle", "Blue Candle")]
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, noxious_fumes)
        self.assertEqual(
            "time_eater_reset_progress", planner.last_decision["reason"]
        )
        self.assertTrue(planner.last_decision["search"]["forced_end"])
        self.assertEqual(4, planner.last_decision["search"]["actual_loss"])

    def test_time_eater_setup_reset_keeps_dynamic_safety_reserve(self):
        """A nonlethal reset is still unsafe when it spends the HP reserve."""

        eater = monster(
            "TimeEater",
            397,
            intent=Intent.ATTACK,
            damage=10,
            hits=1,
            powers=[Power("TimeWarpPower", "Time Warp", 11)],
        )
        eater.max_hp = 456
        noxious_fumes = card(
            "Noxious Fumes", CardType.POWER,
            cost=1, magic=2,
        )
        game = GameStub(
            [eater], [noxious_fumes],
            hp=3, block=10, energy=1, act=3,
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_time_eater_counter_eleven_does_not_reset_into_lethal(self):
        """Replay seq 117795: surviving on two HP beats forced reset."""

        eater = monster(
            "TimeEater",
            302,
            intent=Intent.ATTACK,
            damage=39,
            hits=1,
            powers=[Power("TimeWarpPower", "Time Warp", 11)],
        )
        eater.block = 20
        pommel = card(
            "Pommel Strike", CardType.ATTACK, cost=1, damage=9,
            target=True, magic=2,
        )
        headbutt = card(
            "Headbutt", CardType.ATTACK, cost=2, damage=9, target=True
        )
        game = GameStub(
            [eater], [pommel, headbutt], hp=31, energy=4, act=3
        )
        game.room_type = "MonsterRoomBoss"
        game.player.block = 8

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_time_eater_twelfth_card_uses_strong_free_turn_play(self):
        eater = monster(
            "TimeEater",
            456,
            powers=[Power("TimeWarpPower", "Time Warp", 11)],
        )
        blade_dance = card("Blade Dance", CardType.SKILL, cost=1, magic=4)
        game = GameStub([eater], [blade_dance], hp=37, energy=3, act=3)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, blade_dance)

    def test_time_eater_counter_ten_does_not_create_bad_eleventh_card(self):
        eater = monster(
            "TimeEater",
            456,
            powers=[Power("TimeWarpPower", "Time Warp", 10)],
        )
        eater.block = 20
        weak_attack = card(
            "Endless Agony", CardType.ATTACK, cost=0, damage=4, target=True
        )
        game = GameStub([eater], [weak_attack], hp=37, energy=3, act=3)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_time_eater_confirmed_plays_are_not_counted_twice_after_refresh(self):
        """The Time Warp counter already includes ten confirmed actions."""

        eater = monster(
            "TimeEater",
            456,
            powers=[Power("TimeWarpPower", "Time Warp", 10)],
        )
        first = card(
            "Heavy Zero A", CardType.ATTACK, cost=0, damage=20,
            target=True,
        )
        second = card(
            "Heavy Zero B", CardType.ATTACK, cost=0, damage=20,
            target=True,
        )
        game = GameStub([eater], [first, second], hp=50, energy=0, act=3)
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(SilentPriority())
        planner._card_play_turn_key = planner._combat_turn_key(game)
        planner._confirmed_cards_played = 10

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        sequence = planner.last_decision["planned_sequence"]
        self.assertEqual(2, len(sequence))
        self.assertTrue(planner.last_decision["search"]["forced_end"])

    def test_time_eater_pending_haste_does_not_invest_more_poison(self):
        eater = monster(
            "TimeEater",
            225,
            powers=[
                Power("TimeWarpPower", "Time Warp", 11),
                Power("Poison", "Poison", 19),
            ],
        )
        eater.max_hp = 456
        deadly_poison = card(
            "Deadly Poison", CardType.SKILL, cost=1, magic=7, target=True
        )
        game = GameStub([eater], [deadly_poison], hp=37, energy=3, act=3)
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_time_eater_haste_resets_low_hp_before_terminal_damage_score(self):
        eater = monster(
            "TimeEater",
            150,
            powers=[Power("TimeWarpPower", "Time Warp", 11)],
        )
        eater.max_hp = 456
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=10, target=True
        )
        game = GameStub([eater], [strike], hp=50, energy=1, act=3)

        action = self.planner.choose_card_action(game)

        # The twelfth-card boundary heals Time Eater to half max before the
        # next enemy action, so the line is not scored as a guaranteed 10 HP
        # reduction merely because it reaches the threshold.
        self.assertIsInstance(action, EndTurnAction)

    def test_ordered_search_applies_poison_before_catalyst(self):
        enemy = monster("BookOfStabbing", 120)
        catalyst = card(
            "Catalyst", CardType.SKILL, cost=1, magic=2, target=True
        )
        deadly_poison = card(
            "Deadly Poison", CardType.SKILL, cost=1, magic=5, target=True
        )
        game = GameStub(
            [enemy], [catalyst, deadly_poison], hp=50, energy=2, act=2
        )
        game.room_type = "MonsterRoomElite"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, deadly_poison)
        self.assertEqual(
            ["Deadly Poison", "Catalyst"],
            [step["card_id"] for step in self.planner.last_decision["planned_sequence"]],
        )

    def test_multi_enemy_bouncing_flask_replans_before_catalyst(self):
        enemies = [
            monster("BronzeOrb", 50, powers=[Power("Poison", "Poison", 8)]),
            monster(
                "BronzeAutomaton",
                260,
                powers=[Power("Poison", "Poison", 7)],
            ),
            monster("BronzeOrb", 50, powers=[Power("Poison", "Poison", 8)]),
        ]
        catalyst = card(
            "Catalyst",
            CardType.SKILL,
            cost=1,
            magic=-1,
            target=True,
            upgrades=1,
        )
        catalyst.exhausts = True
        flask = card(
            "Bouncing Flask", CardType.SKILL, cost=2, magic=3
        )
        game = GameStub(
            enemies, [catalyst, flask], hp=50, energy=3, act=2
        )
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, flask)
        self.assertEqual(
            "stochastic_poison_setup_before_catalyst",
            self.planner.last_decision["reason"],
        )
        # The random Flask distribution is not guessed.  Catalyst is selected
        # from the authoritative next frame instead of being terminal-bound.
        self.assertEqual(
            ["Bouncing Flask"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )

    def test_multi_enemy_flask_does_not_delay_survival_catalyst(self):
        attacker = monster(
            "Cultist",
            8,
            intent=Intent.ATTACK,
            damage=50,
            hits=1,
            powers=[Power("Poison", "Poison", 5)],
        )
        other = monster("JawWorm", 80)
        catalyst = card(
            "Catalyst", CardType.SKILL, cost=1, magic=2, target=True
        )
        catalyst.exhausts = True
        flask = card(
            "Bouncing Flask", CardType.SKILL, cost=2, magic=3
        )
        game = GameStub(
            [attacker, other],
            [catalyst, flask],
            hp=10,
            energy=3,
            act=2,
        )
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, catalyst)
        self.assertEqual(0, self.planner.last_decision["search"]["actual_loss"])

    def test_multi_enemy_flask_keeps_complete_zero_loss_catalyst_combo(self):
        """A random setup card must not replace a proven two-card save."""

        attacker = monster(
            "Cultist",
            5,
            intent=Intent.ATTACK,
            damage=60,
            hits=1,
            powers=[Power("Poison", "Poison", 1)],
        )
        other = monster("JawWorm", 30)
        catalyst = card(
            "Catalyst",
            CardType.SKILL,
            cost=1,
            magic=-1,
            target=True,
            upgrades=1,
        )
        catalyst.exhausts = True
        flask = card(
            "Bouncing Flask", CardType.SKILL, cost=2, magic=3
        )
        finishing_attack = card(
            "Tiny Attack",
            CardType.ATTACK,
            cost=1,
            damage=2,
            target=True,
        )
        game = GameStub(
            [attacker, other],
            [catalyst, flask, finishing_attack],
            hp=20,
            energy=3,
            act=2,
        )
        game.room_type = "MonsterRoomElite"
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIsNot(action.card, flask)
        self.assertEqual(0, planner.last_decision["search"]["actual_loss"])
        self.assertEqual(
            0, planner.last_decision["search"]["final_enemy_hp"][0]
        )

    def test_bouncing_flask_packets_individually_consume_artifact(self):
        for artifact_layers, poison_after_flask, final_hp in (
            (1, 14, 58),
            (2, 11, 67),
        ):
            with self.subTest(artifact=artifact_layers):
                heart = monster(
                    "CorruptHeart",
                    100,
                    powers=[
                        Power("Poison", "Poison", 5),
                        Power(
                            "Artifact", "Artifact", artifact_layers
                        ),
                    ],
                )
                flask = card(
                    "Bouncing Flask",
                    CardType.SKILL,
                    cost=2,
                    magic=4,
                    upgrades=1,
                )
                catalyst = card(
                    "Catalyst",
                    CardType.SKILL,
                    cost=1,
                    magic=-1,
                    target=True,
                    upgrades=1,
                )
                catalyst.exhausts = True
                game = GameStub(
                    [heart],
                    [catalyst, flask],
                    hp=50,
                    energy=3,
                    act=4,
                )
                game.room_type = "MonsterRoomBoss"
                planner = FastCombatPlanner(SilentPriority())

                action = planner.choose_card_action(game)

                self.assertIs(action.card, flask)
                self.assertEqual(
                    ["Bouncing Flask", "Catalyst"],
                    [
                        step["card_id"]
                        for step in planner.last_decision[
                            "planned_sequence"
                        ]
                    ],
                )
                self.assertEqual(
                    [final_hp],
                    planner.last_decision["search"]["final_enemy_hp"],
                )
                self.assertEqual(
                    poison_after_flask,
                    planner._poison_after_card(flask, heart),
                )

    def test_artifact_is_stripped_before_upgraded_catalyst(self):
        heart = monster(
            "CorruptHeart",
            200,
            powers=[
                Power("Poison", "Poison", 5),
                Power("Artifact", "Artifact", 1),
            ],
        )
        catalyst = card(
            "Catalyst",
            CardType.SKILL,
            cost=1,
            magic=-1,
            target=True,
            upgrades=1,
        )
        catalyst.exhausts = True
        deadly_poison = card(
            "Deadly Poison", CardType.SKILL, cost=1, magic=5, target=True
        )
        game = GameStub(
            [heart], [catalyst, deadly_poison], hp=50, energy=2, act=4
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIs(action.card, deadly_poison)
        self.assertEqual(
            ["Deadly Poison", "Catalyst"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )

    def test_two_artifact_layers_do_not_fabricate_catalyst_amplification(self):
        heart = monster(
            "CorruptHeart",
            200,
            powers=[
                Power("Poison", "Poison", 5),
                Power("Artifact", "Artifact", 2),
            ],
        )
        catalyst = card(
            "Catalyst",
            CardType.SKILL,
            cost=1,
            magic=-1,
            target=True,
            upgrades=1,
        )
        catalyst.exhausts = True
        deadly_poison = card(
            "Deadly Poison", CardType.SKILL, cost=1, magic=5, target=True
        )
        game = GameStub(
            [heart], [catalyst, deadly_poison], hp=50, energy=2, act=4
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIs(action.card, deadly_poison)
        self.assertNotIn(
            "Catalyst",
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )

    def test_zero_poison_after_artifact_strip_does_not_exhaust_catalyst(self):
        heart = monster(
            "CorruptHeart",
            200,
            powers=[Power("Artifact", "Artifact", 1)],
        )
        catalyst = card(
            "Catalyst",
            CardType.SKILL,
            cost=1,
            magic=-1,
            target=True,
            upgrades=1,
        )
        catalyst.exhausts = True
        deadly_poison = card(
            "Deadly Poison", CardType.SKILL, cost=1, magic=5, target=True
        )
        game = GameStub(
            [heart], [catalyst, deadly_poison], hp=50, energy=2, act=4
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIs(action.card, deadly_poison)
        self.assertEqual(
            ["Deadly Poison"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )

    def test_poisoned_stab_and_crippling_poison_precede_catalyst(self):
        for poison_card in (
            card(
                "Poisoned Stab",
                CardType.ATTACK,
                cost=1,
                damage=6,
                magic=3,
                target=True,
            ),
            card("Crippling Poison", CardType.SKILL, cost=2, magic=4),
        ):
            with self.subTest(card=poison_card.card_id):
                enemy = monster(
                    "BookOfStabbing",
                    120,
                    powers=[Power("Poison", "Poison", 5)],
                )
                catalyst = card(
                    "Catalyst",
                    CardType.SKILL,
                    cost=1,
                    magic=2,
                    target=True,
                )
                catalyst.exhausts = True
                game = GameStub(
                    [enemy],
                    [catalyst, poison_card],
                    hp=50,
                    energy=3,
                    act=2,
                )
                game.room_type = "MonsterRoomElite"

                action = self.planner.choose_card_action(game)

                self.assertIs(action.card, poison_card)
                self.assertEqual(
                    poison_card.card_id,
                    self.planner.last_decision["planned_sequence"][0][
                        "card_id"
                    ],
                )

    def test_ironclad_vulnerable_is_applied_before_followup_attack(self):
        enemy = monster("Cultist", 60)
        strike = card("Strike_R", CardType.ATTACK, cost=1, damage=6, target=True)
        bash = card("Bash", CardType.ATTACK, cost=1, damage=8, target=True)
        game = GameStub([enemy], [strike, bash], hp=70, energy=2, act=1)
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, bash)

    def test_ironclad_flex_is_played_before_attacks_it_strengthens(self):
        enemy = monster("Cultist", 60)
        twin_strike = card(
            "Twin Strike", CardType.ATTACK, cost=1, damage=5, target=True
        )
        flex = card("Flex", CardType.SKILL, cost=0, magic=2)
        game = GameStub([enemy], [twin_strike, flex], hp=70, energy=1, act=1)
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, flex)
        self.assertEqual(
            ["Flex", "Twin Strike"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )

    def test_exact_attack_combo_places_flex_before_both_attacks(self):
        """The cumulative-lethal fast path must not bypass Flex setup."""

        enemy = monster("SphericGuardian", 15)
        first = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=8, target=True
        )
        second = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=8, target=True
        )
        # Distinct UUIDs model two copies in an authoritative hand.
        second.uuid = "Strike_R-2"
        flex = card("Flex", CardType.SKILL, cost=0, magic=2)
        game = GameStub(
            [enemy], [first, second, flex], hp=70, energy=2, act=1
        )
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, flex)
        self.assertEqual(
            "guaranteed_combo_setup_before_attacks",
            planner.last_decision["reason"],
        )
        self.assertEqual("flex", planner.last_decision["setup_kind"])

    def test_flex_is_not_played_when_no_downstream_card_consumes_strength(self):
        enemy = monster("Cultist", 60)
        flex = card("Flex", CardType.SKILL, cost=0, magic=2)
        game = GameStub([enemy], [flex], hp=70, energy=3, act=1)
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "no_positive_marginal_action", planner.last_decision["reason"]
        )

    def test_zero_strength_limit_break_does_not_replace_real_block(self):
        enemy = monster(
            "Chosen", 100, intent=Intent.ATTACK, damage=10, hits=1
        )
        headbutt = card(
            "Headbutt", CardType.ATTACK, cost=1, damage=9, target=True
        )
        limit_break = card(
            "Limit Break", CardType.SKILL, cost=1, upgrades=1
        )
        sword_boomerang = card(
            "Sword Boomerang", CardType.ATTACK, cost=1, damage=3
        )
        true_grit = card(
            "True Grit", CardType.SKILL, cost=1, block=9, upgrades=1
        )
        basic_defend = card(
            "Defend_R", CardType.SKILL, cost=1, block=5
        )
        game = GameStub(
            [enemy],
            [
                headbutt,
                limit_break,
                sword_boomerang,
                true_grit,
                basic_defend,
            ],
            hp=41,
            energy=3,
            act=2,
        )
        game.relics = [Relic("Orichalcum", "Orichalcum")]
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        sequence = [
            step["card_id"]
            for step in planner.last_decision["planned_sequence"]
        ]
        self.assertNotIn("Limit Break", sequence)
        self.assertIn("True Grit", sequence)
        self.assertEqual(1, planner.last_decision["search"]["actual_loss"])

    def test_true_grit_cannot_plan_the_only_other_card_after_forced_exhaust(self):
        parasite = monster(
            "Shelled Parasite",
            52,
            intent=Intent.ATTACK,
            damage=12,
            hits=1,
            powers=[
                Power("Plated Armor", "Plated Armor", 12),
                Power("Thorns", "Thorns", 3),
            ],
        )
        parasite.block = 12
        true_grit = card(
            "True Grit", CardType.SKILL,
            cost=1, block=12, upgrades=1,
        )
        thunderclap = card(
            "Thunderclap", CardType.ATTACK,
            cost=1, damage=4,
        )
        game = GameStub(
            [parasite], [true_grit, thunderclap],
            hp=3, energy=2, act=2,
        )
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, true_grit)
        self.assertEqual(
            ["True Grit"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )

    def test_burning_pact_cannot_plan_the_only_other_card_after_forced_exhaust(
        self,
    ):
        """Replay A2F33 T12: Pact must consume the sole Whirlwind."""

        champ = monster(
            "Champ",
            27,
            intent=Intent.ATTACK,
            damage=24,
            hits=2,
            powers=[Power("Vulnerable", "Vulnerable", 2)],
        )
        burning_pact = card(
            "Burning Pact", CardType.SKILL, cost=1, magic=2
        )
        burning_pact.exhausts = True
        whirlwind = card(
            "Whirlwind",
            CardType.ATTACK,
            cost=-1,
            damage=8,
            target=False,
            upgrades=1,
        )
        game = GameStub(
            [champ], [burning_pact, whirlwind],
            hp=35, energy=4, act=2,
        )
        game.draw_pile = [
            card("Anger", CardType.ATTACK, cost=0, damage=6, target=True),
            card("Anger", CardType.ATTACK, cost=0, damage=6, target=True),
        ]
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, whirlwind)
        self.assertEqual(
            ["Whirlwind"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        self.assertTrue(planner.last_decision["search"]["true_combat_end"])

    def test_true_grit_preserves_two_damage_sources_against_recurring_defense(self):
        parasite = monster(
            "Shelled Parasite",
            52,
            intent=Intent.ATTACK,
            damage=6,
            hits=2,
            powers=[Power("Plated Armor", "Plated Armor", 12)],
        )
        parasite.block = 12
        true_grit = card(
            "True Grit", CardType.SKILL,
            cost=1, block=9, upgrades=1,
        )
        perfected = card(
            "Perfected Strike", CardType.ATTACK,
            cost=2, damage=8, target=True,
        )
        thunderclap = card(
            "Thunderclap", CardType.ATTACK,
            cost=1, damage=4,
        )
        game = GameStub(
            [parasite], [true_grit, perfected, thunderclap],
            hp=27, block=8, energy=2, act=2,
        )
        game.player.powers = [Power("Combust", "Combust", 5, misc=1)]
        game.draw_pile = []
        game.discard_pile = [
            card("Shrug It Off", CardType.SKILL, cost=1, block=8)
        ]
        game.exhaust_pile = []
        game.limbo = []
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIsNot(action.card, true_grit)
        guard = planner.last_decision["true_grit_damage_reserve_guard"]
        self.assertTrue(guard["engaged"])
        self.assertEqual(2, guard["attack_source_count"])
        self.assertEqual(2, guard["minimum_attack_source_reserve"])

    def test_safe_spheric_guardian_turn_preserves_demon_form_window(self):
        """Floor 18 must not spend Demon Form's Energy on inert Block."""

        guardian = monster(
            "SphericGuardian",
            20,
            intent=Intent.DEFEND,
            powers=[
                Power("Barricade", "Barricade", 1),
                Power("Artifact", "Artifact", 3),
            ],
        )
        guardian.block = 40
        shrugs = [
            card("Shrug It Off", CardType.SKILL, cost=1, block=8)
            for _ in range(3)
        ]
        for index, shrug in enumerate(shrugs):
            shrug.uuid = f"shrug-{index}"
        defend_card = card(
            "Defend_R", CardType.SKILL, cost=1, block=5
        )
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=2
        )
        future_attacks = [
            card(
                "Strike_R", CardType.ATTACK,
                cost=1, damage=6, target=True,
            )
            for _ in range(5)
        ]
        for index, attack in enumerate(future_attacks):
            attack.uuid = f"future-strike-{index}"
        game = GameStub(
            [guardian],
            [shrugs[0], shrugs[1], defend_card, demon_form, shrugs[2]],
            hp=70,
            energy=3,
            act=2,
        )
        game.player.max_hp = 80
        game.turn = 1
        game.draw_pile = future_attacks
        game.discard_pile = []
        game.exhaust_pile = []
        game.limbo = []
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, demon_form)
        self.assertEqual(
            ["Demon Form"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        self.assertGreater(
            planner.last_decision["lifecycle"]["adjustment"], 0
        )

    def test_spheric_guardian_meaningful_progress_can_beat_demon_form(self):
        """Floor 22's 46 durability progress is a reasonable short-fight line."""
        guardian = monster(
            "SphericGuardian",
            20,
            intent=Intent.DEFEND,
            powers=[
                Power("Barricade", "Barricade", 1),
                Power("Artifact", "Artifact", 3),
            ],
        )
        guardian.block = 40
        pommel = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=9, magic=1, target=True,
        )
        thunderclap = card(
            "Thunderclap", CardType.ATTACK, cost=1, damage=4,
        )
        bash = card(
            "Bash", CardType.ATTACK,
            cost=2, damage=10, magic=3, target=True, upgrades=1,
        )
        immolate = card(
            "Immolate", CardType.ATTACK, cost=2, damage=28, upgrades=1
        )
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=2
        )
        rage = card(
            "Rage", CardType.SKILL, cost=0, magic=5, upgrades=1
        )
        strike_card = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=9, target=True,
        )
        game = GameStub(
            [guardian],
            [
                pommel, thunderclap, bash, immolate,
                demon_form, rage, strike_card,
            ],
            hp=79,
            block=10,
            energy=4,
            act=2,
        )
        game.player.max_hp = 85
        game.turn = 1
        game.draw_pile = [
            card("Defend_R", CardType.SKILL, cost=1, block=5)
        ]
        game.discard_pile = []
        game.exhaust_pile = []
        game.limbo = []
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIsNot(action.card, demon_form)
        self.assertEqual(
            [14], planner.last_decision["search"]["final_enemy_hp"]
        )
        self.assertEqual(
            0.0,
            planner.last_decision["search"]["foregone_scaling_setup_cost"],
        )

    def test_demon_form_setup_plan_survives_deterministic_replan_boundary(self):
        """A searched Immolate -> Demon Form line is one strategic promise."""
        awakened = monster(
            "AwakenedOne", 300, intent=Intent.ATTACK, damage=20,
            powers=[Power("Curiosity", "Curiosity", 1)],
        )
        immolate = card("Immolate", CardType.ATTACK, cost=2, damage=42)
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=2
        )
        game = GameStub(
            [awakened], [immolate, demon_form],
            hp=99, block=10, energy=5, act=3,
        )
        game.player.max_hp = 99
        game.room_type = "MonsterRoomBoss"
        game.turn = 1
        game.draw_pile = [
            card(
                "Strike_R", CardType.ATTACK,
                cost=1, damage=6, target=True,
            )
            for _ in range(6)
        ]
        planner = FastCombatPlanner(IroncladPriority())
        plan = [
            SimpleNamespace(card=immolate, target=None),
            SimpleNamespace(card=demon_form, target=None),
        ]

        planner._arm_terminal_plan(
            game, plan,
            {"true_combat_end": False, "actual_loss": 11, "tier": 2},
        )
        self.assertIsNotNone(planner._terminal_plan)
        self.assertEqual(
            "demon_form_setup", planner._terminal_plan["plan_binding"]
        )
        planner._terminal_plan_selected_uuid = immolate.uuid
        game.hand.remove(immolate)
        game.player.energy = 3

        action = planner._continue_terminal_plan(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, demon_form)
        self.assertEqual(
            "strategic_setup_plan_continuation",
            planner.last_decision["reason"],
        )

    def test_demon_form_setup_plan_aborts_when_loss_exceeds_promise(self):
        awakened = monster(
            "AwakenedOne", 300, intent=Intent.BUFF,
            powers=[Power("Curiosity", "Curiosity", 1)],
        )
        immolate = card("Immolate", CardType.ATTACK, cost=2, damage=42)
        demon_form = card(
            "Demon Form", CardType.POWER, cost=3, magic=2
        )
        game = GameStub(
            [awakened], [immolate, demon_form],
            hp=30, energy=5, act=3,
        )
        game.room_type = "MonsterRoomBoss"
        game.turn = 1
        game.draw_pile = [
            card(
                "Strike_R", CardType.ATTACK,
                cost=1, damage=6, target=True,
            )
            for _ in range(6)
        ]
        planner = FastCombatPlanner(IroncladPriority())
        plan = [
            SimpleNamespace(card=immolate, target=None),
            SimpleNamespace(card=demon_form, target=None),
        ]
        planner._arm_terminal_plan(
            game, plan,
            {"true_combat_end": False, "actual_loss": 0, "tier": 2},
        )
        self.assertIsNotNone(planner._terminal_plan)
        planner._terminal_plan_selected_uuid = immolate.uuid
        game.hand.remove(immolate)
        game.player.energy = 3
        awakened.intent = Intent.ATTACK
        awakened.move_adjusted_damage = 20
        awakened.move_hits = 1

        self.assertIsNone(planner._continue_terminal_plan(game))
        self.assertIsNone(planner._terminal_plan)

    def test_slime_boss_known_statuses_make_upgraded_evolve_setup_first(self):
        """Replay 8fc20a4a turn 2: do not leave Evolve+ unplayed."""

        slime_boss = monster("SlimeBoss", 121)
        evolve = card(
            "Evolve", CardType.POWER, cost=1, magic=2, upgrades=1
        )
        strikes = [
            card(
                f"Strike-{index}", CardType.ATTACK,
                cost=1, damage=9, target=True,
            )
            for index in range(2)
        ]
        game = GameStub(
            [slime_boss], [*strikes, evolve], hp=77, energy=2, act=1
        )
        game.room_type = "MonsterRoomBoss"
        game.turn = 2
        game.draw_pile = [
            card(
                f"Future Strike-{index}", CardType.ATTACK,
                cost=1, damage=6, target=True,
            )
            for index in range(6)
        ]
        game.discard_pile = [
            card(f"Slimed-{index}", CardType.STATUS, cost=1)
            for index in range(3)
        ]
        for status in game.discard_pile:
            status.card_id = "Slimed"
        game.exhaust_pile = []
        game.limbo = []
        planner = FastCombatPlanner(IroncladPriority())

        lifecycle = planner._lifecycle_evaluation(game, evolve, 0)
        action = planner.choose_card_action(game)

        self.assertEqual("evolve", lifecycle["kind"])
        self.assertEqual(3, lifecycle["known_future_statuses"])
        self.assertEqual(3, lifecycle["triggers"])
        self.assertEqual(2, lifecycle["draws_per_trigger"])
        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, evolve)
        self.assertEqual("evolve", planner.last_decision["lifecycle"]["kind"])

    def test_evolve_lifecycle_does_not_count_status_already_in_hand(self):
        enemy = monster("Cultist", 80)
        evolve = card(
            "Evolve", CardType.POWER, cost=1, magic=2, upgrades=1
        )
        current_status = card("Slimed", CardType.STATUS, cost=1)
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=9, target=True
        )
        game = GameStub(
            [enemy], [evolve, current_status, strike],
            hp=70, energy=1, act=1,
        )
        game.draw_pile = []
        game.discard_pile = []
        game.exhaust_pile = []
        game.limbo = []
        planner = FastCombatPlanner(IroncladPriority())

        lifecycle = planner._lifecycle_evaluation(game, evolve, 0)

        self.assertEqual(0, lifecycle["known_future_statuses"])
        self.assertEqual(0, lifecycle["triggers"])

    def test_evolve_lifecycle_deduplicates_same_status_uuid_across_zones(self):
        enemy = monster("Cultist", 80)
        evolve = card(
            "Evolve", CardType.POWER, cost=1, magic=2, upgrades=1
        )
        duplicate_status = card("Slimed", CardType.STATUS, cost=1)
        game = GameStub([enemy], [evolve], hp=70, energy=1, act=1)
        game.draw_pile = [duplicate_status]
        game.discard_pile = [duplicate_status]
        game.exhaust_pile = []
        game.limbo = []
        planner = FastCombatPlanner(IroncladPriority())

        lifecycle = planner._lifecycle_evaluation(game, evolve, 0)

        self.assertEqual(1, lifecycle["known_future_statuses"])
        self.assertEqual(1, lifecycle["triggers"])

    def test_evolve_lifecycle_excludes_statuses_drawn_before_setup(self):
        enemy = monster("Cultist", 80)
        evolve = card(
            "Evolve", CardType.POWER, cost=1, magic=2, upgrades=1
        )
        future_strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        statuses = [
            card(f"Slimed-{index}", CardType.STATUS, cost=1)
            for index in range(2)
        ]
        game = GameStub([enemy], [evolve], hp=70, energy=1, act=1)
        # The top is serialized last.  A prior exact draw of two cards leaves
        # only the first card in the active draw-pile prefix.
        game.draw_pile = [future_strike, *statuses]
        game.discard_pile = []
        game.exhaust_pile = []
        game.limbo = []
        planner = FastCombatPlanner(IroncladPriority())

        before_draw = planner._lifecycle_evaluation(game, evolve, 0)
        after_draw = planner._lifecycle_evaluation(
            game, evolve, 0, draw_pile_size=1
        )

        self.assertEqual(2, before_draw["known_future_statuses"])
        self.assertEqual(0, after_draw["known_future_statuses"])
        self.assertGreater(
            before_draw["adjustment"], after_draw["adjustment"]
        )

    def test_same_turn_flex_still_enables_limit_break(self):
        enemy = monster("Cultist", 100)
        flex = card("Flex", CardType.SKILL, cost=0, magic=2)
        limit_break = card("Limit Break", CardType.SKILL, cost=1)
        twin_strike = card(
            "Twin Strike", CardType.ATTACK, cost=1, damage=5, target=True
        )
        game = GameStub(
            [enemy], [limit_break, twin_strike, flex], hp=70, energy=2, act=1
        )
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, flex)
        self.assertEqual(
            ["Flex", "Limit Break", "Twin Strike"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )

    def test_flex_strength_bonus_is_applied_once_per_attack_hit(self):
        twin_strike = card(
            "Twin Strike", CardType.ATTACK, cost=1, damage=5, target=True
        )
        game = GameStub(
            [monster("Cultist", 60)], [twin_strike], hp=70, energy=1, act=1
        )
        planner = FastCombatPlanner(IroncladPriority())

        self.assertEqual(
            4,
            planner._same_turn_strength_damage_bonus(
                game, twin_strike, strength_bonus=2, hits=2
            ),
        )

    def test_same_turn_strength_bonus_respects_weakened_alias(self):
        twin_strike = card(
            "Twin Strike", CardType.ATTACK, cost=1, damage=5, target=True
        )
        game = GameStub(
            [monster("Cultist", 60)], [twin_strike], hp=70, energy=1, act=1
        )
        game.player.powers = [Power("Weakened", "Weak", 1)]
        planner = FastCombatPlanner(IroncladPriority())

        # The extra two Strength points are reduced by Weak before the
        # per-hit integer rounding; the protocol's ``Weakened`` id is an
        # alias used by live frames and must not bypass that modifier.
        self.assertEqual(
            2,
            planner._same_turn_strength_damage_bonus(
                game, twin_strike, strength_bonus=2, hits=2
            ),
        )

    def test_attacking_target_prefers_stronger_spot_weakness_setup_over_inflame(self):
        """The live Giant Head loss exposed a setup-prefix ranking bug.

        Both lines cost two energy and end in the same attack.  Upgraded Spot
        Weakness grants four persistent Strength while upgraded Inflame grants
        three, so a target with an attacking intent must select Spot rather
        than allowing the static Power priority to override the simulated
        transition.
        """

        enemy = monster(
            "GiantHead", 405, intent=Intent.ATTACK, damage=17, hits=1,
            powers=[Power("Slow", "Slow", 2)],
        )
        spot = card(
            "Spot Weakness", CardType.SKILL, cost=1, target=True, magic=4
        )
        inflame = card("Inflame", CardType.POWER, cost=1, magic=3)
        cleave = card("Cleave", CardType.ATTACK, cost=1, damage=8)
        game = GameStub(
            [enemy], [spot, inflame, cleave], hp=109, block=11,
            energy=2, act=3,
        )
        game.player.powers = [Power("Strength", "Strength", 4)]
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, spot)
        self.assertEqual(
            ["Spot Weakness", "Cleave"],
            [row["card_id"] for row in planner.last_decision["planned_sequence"]],
        )
        # The fixture's Cleave damage is already the displayed packet after
        # player modifiers. Giant Head's two Slow stacks amplify that 12 raw
        # damage to 15; Strength must not be applied again.
        self.assertEqual([390], planner.last_decision["search"]["final_enemy_hp"])

    def test_spot_weakness_excludes_every_nonattacking_target(self):
        """The live F31/F46 trace spent Energy on a guaranteed no-op."""

        sentry = monster("Sentry", 26, intent=Intent.DEBUFF)
        guardian = monster("SphericGuardian", 20, intent=Intent.DEFEND)
        spot = card(
            "Spot Weakness", CardType.SKILL, cost=1, target=True, magic=3
        )
        game = GameStub([sentry, guardian], [spot], hp=74, energy=1, act=2)
        planner = FastCombatPlanner(IroncladPriority())

        self.assertEqual(
            [],
            planner._target_pool_for_card(game, spot, [sentry, guardian]),
        )
        self.assertIsInstance(planner.choose_card_action(game), EndTurnAction)

    def test_spot_weakness_keeps_only_attacking_targets(self):
        attacker = monster(
            "Cultist", 40, intent=Intent.ATTACK, damage=6, hits=1
        )
        buffer = monster("Sentry", 38, intent=Intent.DEBUFF)
        spot = card(
            "Spot Weakness", CardType.SKILL, cost=1, target=True, magic=3
        )
        planner = FastCombatPlanner(IroncladPriority())

        self.assertEqual(
            [attacker],
            planner._target_pool_for_card(
                GameStub([attacker, buffer], [spot]),
                spot,
                [attacker, buffer],
            ),
        )

    def test_active_envenom_adds_only_bounded_nonlethal_attack_utility(self):
        enemy = monster("Cultist", 40, intent=Intent.BUFF)
        strike = card(
            "Strike_G", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub([enemy], [strike], hp=70, energy=1, act=1)
        planner = FastCombatPlanner(SilentPriority())
        baseline = planner._candidate(
            game, strike, enemy, enemy, 0, 0, 0
        ).base_score

        game.player.powers = [Power("Envenom", "Envenom", 1)]
        with_envenom = planner._candidate(
            game, strike, enemy, enemy, 0, 0, 0
        ).base_score

        self.assertGreater(with_envenom, baseline)
        self.assertLessEqual(with_envenom - baseline, 1.0)

    def test_mummified_hand_adds_bounded_power_discount_utility(self):
        enemy = monster("Cultist", 40, intent=Intent.BUFF)
        power = card("Defragment", CardType.POWER, cost=1)
        expensive = card(
            "Sunder", CardType.ATTACK, cost=3, damage=24,
            target=True, playable=False,
        )
        game = GameStub(
            [enemy], [power, expensive], hp=70, energy=1, act=2
        )
        planner = FastCombatPlanner(DefectPowerPriority())
        baseline = planner._candidate(
            game, power, None, enemy, 0, 0, 0
        ).base_score

        game.relics = [Relic("Mummified Hand", "Mummified Hand")]
        with_relic = planner._candidate(
            game, power, None, enemy, 0, 0, 0
        ).base_score

        self.assertEqual(4.5, with_relic - baseline)

    def test_terminal_plan_order_is_not_rewritten_after_search(self):
        enemy = monster("Cultist", 100)
        attack = card(
            "Carnage", CardType.ATTACK, cost=1, damage=20, target=True
        )
        inflame = card("Inflame", CardType.POWER, cost=1, magic=2)
        game = GameStub(
            [enemy], [attack, inflame], hp=70, energy=2, act=1
        )
        planner = FastCombatPlanner(IroncladPriority())

        def fixed_terminal_plan(
            _game, groups, _attack_loss, _total_loss, _risk_budget
        ):
            candidates = [candidate for group in groups for candidate in group]
            attack_candidate = next(
                candidate for candidate in candidates
                if candidate.card is attack
            )
            inflame_candidate = next(
                candidate for candidate in candidates
                if candidate.card is inflame
            )
            planner._last_search = {"actual_loss": 0, "tier": 2}
            planner._last_initial_search = {"actual_loss": 0, "tier": 2}
            planner._last_verified_fallback = None
            return 5.0, [attack_candidate, inflame_candidate]

        with patch.object(
            planner, "_best_plan", side_effect=fixed_terminal_plan
        ):
            action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, attack)
        self.assertEqual(
            ["Carnage", "Inflame"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )

    def test_terminal_plan_does_not_bind_hand_mutating_cards(self):
        """Concentrate can discard a later lethal card, so re-plan after it."""

        enemy = monster("Cultist", 20)
        concentrate = card("Concentrate", CardType.SKILL, cost=0)
        catalyst = card("Catalyst", CardType.SKILL, cost=1, target=True)
        game = GameStub(
            [enemy], [concentrate, catalyst], hp=30, energy=1, act=2
        )
        planner = FastCombatPlanner(IroncladPriority())
        plan = [
            SimpleNamespace(card=concentrate, target=None),
            SimpleNamespace(card=catalyst, target=enemy),
        ]

        planner._arm_terminal_plan(
            game, plan, {"true_combat_end": True, "actual_loss": 0}
        )

        self.assertIsNone(planner._terminal_plan)

    def test_terminal_plan_clears_when_next_target_is_passively_doomed(self):
        enemy = monster(
            "Darkling",
            14,
            powers=[Power("Poison", "Poison", 10)],
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub([enemy], [strike], hp=30, energy=1, act=3)
        game.player.powers = [Power("Noxious Fumes", "Noxious Fumes", 5)]
        planner = FastCombatPlanner(IroncladPriority())
        planner._terminal_plan = {
            "context": planner._combat_turn_key(game),
            "entries": [{
                "card_id": "Strike_R",
                "card_uuid": strike.uuid,
                "target_key": ["cultist", enemy.monster_index],
            }],
            "search": {"true_combat_end": True},
        }
        planner._terminal_plan_selected_uuid = strike.uuid

        self.assertIsNone(planner._continue_terminal_plan(game))
        self.assertIsNone(planner._terminal_plan)

    def test_terminal_plan_continuation_records_current_card_damage(self):
        enemy = monster("Cultist", 20)
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub([enemy], [strike], hp=30, energy=1, act=1)
        planner = FastCombatPlanner(IroncladPriority())
        planner._terminal_plan = {
            "context": planner._combat_turn_key(game),
            "entries": [{
                "card_id": "Strike_R",
                "card_uuid": strike.uuid,
                "target_key": ["cultist", enemy.monster_index],
            }],
            "search": {
                "true_combat_end": True,
                # Deliberately stale whole-line value; this must not be
                # copied to the continuation's per-card audit record.
                "first_action_enemy_hp_loss": 0,
            },
        }
        planner._terminal_plan_selected_uuid = strike.uuid

        action = planner._continue_terminal_plan(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(6, planner.last_decision["card_damage"])

    def test_terminal_plan_continuation_recomputes_thousand_cuts_damage(self):
        enemy = monster("Cultist", 20)
        defend = card("Defend_G", CardType.SKILL, cost=1, block=5)
        game = GameStub([enemy], [defend], hp=30, energy=1, act=1)
        game.player.powers = [
            Power("ThousandCutsPower", "A Thousand Cuts", 1)
        ]
        planner = FastCombatPlanner(SilentPriority())
        planner._terminal_plan = {
            "context": planner._combat_turn_key(game),
            "entries": [{
                "card_id": "Defend_G",
                "card_uuid": defend.uuid,
                "target_key": None,
            }],
            "search": {
                "true_combat_end": True,
                "first_action_enemy_hp_loss": 0,
            },
        }
        planner._terminal_plan_selected_uuid = "already-returned"

        action = planner._continue_terminal_plan(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(1, planner.last_decision["card_damage"])
        self.assertEqual(
            1,
            planner.last_decision["search"][
                "first_action_enemy_hp_loss"
            ],
        )

    def test_jax_three_hp_cost_is_terminal_and_cannot_be_lethal(self):
        enemy = monster("Cultist", 100)
        twin_strike = card(
            "Twin Strike", CardType.ATTACK, cost=1, damage=5, target=True
        )
        jax = card("J.A.X.", CardType.SKILL, cost=0, magic=2)
        game = GameStub(
            [enemy], [twin_strike, jax], hp=3, energy=1, act=2
        )
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, twin_strike)
        self.assertNotIn(
            "J.A.X.",
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )

    def test_awakened_curiosity_power_is_counted_per_attack_hit(self):
        awakened = monster(
            "AwakenedOne",
            300,
            intent=Intent.ATTACK,
            damage=5,
            hits=4,
            powers=[Power("CuriosityPower", "Curiosity", 1)],
        )
        inflame = card("Inflame", CardType.POWER, cost=1, magic=2)
        game = GameStub(
            [awakened], [inflame], hp=23, energy=1, act=3
        )
        game.room_type = "MonsterRoomBoss"
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        # END loses 20 and survives. Inflame would trigger Curiosity first,
        # changing the four hits to 6x4 and killing the player.
        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(20, planner.last_decision["projected_hp_loss"])

    def test_curiosity_accumulates_once_for_each_selected_power(self):
        awakened = monster(
            "AwakenedOne",
            300,
            powers=[Power("CuriosityPower", "Curiosity", 1)],
        )
        inflame = card("Inflame", CardType.POWER, cost=1, magic=2)
        echo_form = card("Echo Form", CardType.POWER, cost=1)
        game = GameStub(
            [awakened], [inflame, echo_form], hp=70, energy=2, act=1
        )
        game.draw_pile = [
            card(
                "Heavy Blade", CardType.ATTACK,
                cost=2, damage=14, target=True,
            )
        ]
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(
            2,
            len(planner.last_decision["planned_sequence"]),
        )
        self.assertEqual(
            [2],
            planner.last_decision["search"]["enemy_strength_bonus"],
        )

    def test_curiosity_does_not_trigger_after_owner_is_dead(self):
        awakened = monster(
            "AwakenedOne",
            1,
            powers=[Power("CuriosityPower", "Curiosity", 1)],
        )
        inflame = card("Inflame", CardType.POWER, cost=1, magic=2)

        self.assertEqual(
            1,
            self.planner._curiosity_strength_gain(awakened, inflame, 1),
        )
        self.assertEqual(
            0,
            self.planner._curiosity_strength_gain(awakened, inflame, 0),
        )

    def test_power_after_awakened_phase_death_adds_no_curiosity(self):
        awakened = monster(
            "AwakenedOne",
            5,
            powers=[Power("CuriosityPower", "Curiosity", 1)],
        )
        cultist = monster("Cultist", 100)
        strike_card = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        inflame = card("Inflame", CardType.POWER, cost=1, magic=2)
        game = GameStub(
            [awakened, cultist],
            [strike_card, inflame],
            hp=70,
            energy=2,
            act=3,
        )
        planner = FastCombatPlanner(IroncladPriority())
        active = combat_predictor.active_monsters(game)
        preferred = planner._prepare_focus(game, active)
        outcome = combat_predictor.projected_turn_outcome(game)
        total_loss = outcome.attack_hp_loss + outcome.end_turn_hp_loss
        groups = planner._build_candidates(
            game,
            game.hand,
            active,
            preferred,
            combat_predictor.incoming_damage(game),
            outcome.attack_hp_loss,
            total_loss,
        )

        _, plan = planner._best_plan(
            game, groups, outcome.attack_hp_loss, total_loss, risk_budget=2
        )

        self.assertEqual(
            ["Strike_R", "Inflame"],
            [candidate.card.card_id for candidate in plan],
        )
        self.assertEqual(
            [0, 0],
            planner._last_search["enemy_strength_bonus"],
        )

    def test_curiosity_strength_is_adjusted_before_player_vulnerable(self):
        awakened = monster(
            "AwakenedOne",
            300,
            powers=[Power("CuriosityPower", "Curiosity", 1)],
        )
        game = GameStub([awakened], [], hp=70, energy=0, act=3)
        game.player.powers = [Power("Vulnerable", "Vulnerable", 1)]

        self.assertEqual(
            2,
            self.planner._strength_damage_bonus_per_hit(
                game, awakened, 1
            ),
        )

    def test_dynamic_block_on_attack_does_not_disable_orichalcum(self):
        attacker = monster(
            "BookOfStabbing", 80, intent=Intent.ATTACK, damage=9, hits=1
        )
        strike_card = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        strike_card.block = 2
        strike_card.base_block = -1
        game = GameStub([attacker], [strike_card], hp=20, energy=1, act=2)
        game.relics = [Relic("Orichalcum", "Orichalcum")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike_card)

    def test_known_long_fights_have_scaling_pressure(self):
        self.assertGreater(
            self.planner._monster_scaling_pressure(monster("GiantHead", 500)),
            9,
        )
        self.assertGreater(
            self.planner._monster_scaling_pressure(monster("Hexaghost", 250)),
            4,
        )

    def test_lagavulin_persistent_debuff_window_uses_three_attacks(self):
        lagavulin = monster(
            "Lagavulin", 35,
            intent=Intent.ATTACK, damage=18, hits=1,
            powers=[Power("Regenerate", "Regenerate", 3)],
        )
        lagavulin.max_hp = 109

        def unique(value, suffix):
            value.uuid = f"{value.card_id}:{suffix}"
            return value

        defend = unique(card(
            "Defend_R", CardType.SKILL, cost=1, block=4
        ), "hand")
        strikes = [
            unique(card(
                "Strike_R", CardType.ATTACK,
                cost=1, damage=5, target=True,
            ), index)
            for index in range(3)
        ]
        whirlwind = unique(card(
            "Whirlwind", CardType.ATTACK,
            cost=-1, damage=4, target=False,
        ), "hand")
        game = GameStub(
            [lagavulin], [defend, *strikes, whirlwind],
            hp=44, energy=3, act=1,
        )
        game.player.max_hp = 80
        game.player.powers = [
            Power("Dexterity", "Dexterity", -1),
            Power("Strength", "Strength", -1),
        ]
        game.turn = 6
        game.floor = 7
        game.draw_pile = [
            unique(card("Defend_R", CardType.SKILL, block=5), "draw"),
            unique(card("Double Tap", CardType.SKILL, magic=1), "draw"),
            unique(card(
                "Bash", CardType.ATTACK,
                cost=2, damage=10, target=True, magic=3,
            ), "draw"),
            unique(card(
                "Strike_R", CardType.ATTACK,
                damage=6, target=True,
            ), "draw"),
        ]
        game.discard_pile = [
            *[
                unique(card(
                    "Anger", CardType.ATTACK,
                    cost=0, damage=6, target=True,
                ), f"discard-{index}")
                for index in range(6)
            ],
            unique(card("Defend_R", CardType.SKILL, block=5), "discard-a"),
            unique(card("Defend_R", CardType.SKILL, block=5), "discard-b"),
        ]
        game.exhaust_pile = []
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual("Strike_R", action.card.card_id)
        self.assertEqual(
            ["Strike_R", "Strike_R", "Strike_R"],
            [item["card_id"] for item in planner.last_decision["planned_sequence"]],
        )
        self.assertEqual(18, planner.last_decision["search"]["actual_loss"])
        self.assertGreaterEqual(
            planner.last_decision["search"]["effective_risk_budget"], 18
        )
        self.assertGreater(
            44 - planner.last_decision["search"]["actual_loss"],
            planner.last_decision["search"]["safety_reserve"],
        )

    def test_nemesis_debuff_turn_sets_up_weak_then_persistent_block(self):
        nemesis = monster(
            "Nemesis", 182, intent=Intent.DEBUFF,
            powers=[
                Power("Weakened", "Weakened", 1),
            ],
        )
        nemesis.max_hp = 185
        bash = card(
            "Bash", CardType.ATTACK,
            cost=2, damage=10, target=True, magic=3, upgrades=1,
        )
        double_tap = card("Double Tap", CardType.SKILL, cost=1, magic=1)
        shockwave = card(
            "Shockwave", CardType.SKILL,
            cost=2, magic=5, upgrades=1,
        )
        shockwave.exhausts = True
        body_slam = card(
            "Body Slam", CardType.ATTACK, cost=1, target=True
        )
        impervious = card(
            "Impervious", CardType.SKILL, cost=2, block=30
        )
        impervious.exhausts = True
        metallicize = card(
            "Metallicize", CardType.POWER, cost=1, magic=3
        )
        pommel = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=9, target=True, magic=1,
        )
        writhe = card(
            "Writhe", CardType.CURSE,
            cost=-2, playable=False,
        )
        exact_card_fields = [
            (writhe, CardRarity.CURSE, -1, -1, -1),
            (bash, CardRarity.BASIC, 10, -1, 3),
            (double_tap, CardRarity.RARE, -1, -1, 1),
            (shockwave, CardRarity.UNCOMMON, -1, -1, 5),
            (body_slam, CardRarity.COMMON, 0, -1, -1),
            (impervious, CardRarity.RARE, -1, 30, -1),
            (metallicize, CardRarity.UNCOMMON, -1, -1, 3),
            (pommel, CardRarity.COMMON, 9, -1, 1),
        ]
        for exact_card, rarity, damage, block, magic in exact_card_fields:
            (exact_card.rarity, exact_card.base_damage,
             exact_card.base_block, exact_card.base_magic_number) = (
                rarity, damage, block, magic
            )
        game = GameStub(
            [nemesis],
            [writhe, bash, double_tap, shockwave, body_slam,
             impervious, metallicize, pommel],
            hp=29, energy=6, act=3,
        )
        game.player.max_hp = 94
        game.relics = [
            Relic("Burning Blood", "Burning Blood"),
            Relic("Mercury Hourglass", "Mercury Hourglass"),
            Relic("Ornamental Fan", "Ornamental Fan", counter=0),
            Relic("SlaversCollar", "SlaversCollar"),
            Relic("Red Mask", "Red Mask"),
            Relic("Mango", "Mango"),
            Relic("Runic Cube", "Runic Cube"),
        ]
        game.turn = 1
        game.room_type = "MonsterRoomElite"
        game.draw_pile = [
            card("Shrug It Off", CardType.SKILL, cost=1, block=8),
            card("Defend_R", CardType.SKILL, cost=1, block=5),
            card("Anger", CardType.ATTACK, cost=0, damage=6, target=True),
            card(
                "Whirlwind", CardType.ATTACK,
                cost=-1, damage=8, upgrades=1,
            ),
            card("Defend_R", CardType.SKILL, cost=1, block=5),
            card("Defend_R", CardType.SKILL, cost=1, block=5),
            card("Strike_R", CardType.ATTACK, cost=1, damage=6, target=True),
            card("Strike_R", CardType.ATTACK, cost=1, damage=6, target=True),
        ]
        for index, value in enumerate(game.draw_pile):
            value.uuid = f"draw:{index}:{value.card_id}"
        offering = card("Offering", CardType.SKILL, cost=0)
        offering.exhausts = True
        game.exhaust_pile = [offering]
        game.discard_pile = []
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIs(action.card, shockwave, planner.last_decision)
        self.assertEqual(12, planner.last_decision["future_turn_weak_value"])

        follow_nemesis = monster(
            "Nemesis", 146, intent=Intent.DEBUFF,
            powers=[
                Power("Vulnerable", "Vulnerable", 3),
                Power("Weakened", "Weakened", 1),
            ],
        )
        follow_nemesis.max_hp = 185
        follow_up = GameStub(
            [follow_nemesis],
            [
                writhe,
                shockwave,
                body_slam,
                impervious,
                metallicize,
                card(
                    "Strike_R", CardType.ATTACK,
                    cost=1, damage=6, target=True,
                ),
                card(
                    "Strike_R", CardType.ATTACK,
                    cost=1, damage=6, target=True,
                ),
            ],
            hp=29, energy=2, act=3,
        )
        follow_up.player.max_hp = 94
        follow_up.player.block = 4
        follow_up.relics = list(game.relics)
        follow_up.turn = 1
        follow_up.room_type = "MonsterRoomElite"
        follow_up.draw_pile = list(game.draw_pile[:-2])
        follow_up.discard_pile = [bash, double_tap, pommel]
        follow_up.exhaust_pile = [offering]
        follow_up_planner = FastCombatPlanner(IroncladPriority())

        follow_up_action = follow_up_planner.choose_card_action(follow_up)

        self.assertIs(
            follow_up_action.card, metallicize,
            follow_up_planner.last_decision,
        )
        self.assertEqual(
            6, follow_up_planner.last_decision["future_turn_block_value"]
        )

    def test_giant_head_slow_is_included_in_card_damage(self):
        giant_head = monster(
            "GiantHead", 100, powers=[Power("Slow", "Slow", 2)]
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=10, target=True
        )
        game = GameStub([giant_head], [strike], hp=50, energy=1, act=3)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(12, self.planner.last_decision["card_damage"])

    def test_regenerate_reduces_nonlethal_attack_value(self):
        guardian = monster(
            "SphericGuardian", 30,
            powers=[Power("Regenerate", "Regenerate", 7)],
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=10, target=True
        )
        game = GameStub([guardian], [strike], hp=50, energy=1, act=2)

        action = self.planner.choose_card_action(game)

        # A 10-damage hit leaving a 7-point Regenerate tick is not treated as
        # a free positive attack when there is no incoming threat.
        self.assertIsInstance(action, EndTurnAction)

    def test_defect_energy_card_unlocks_currently_unplayable_followup(self):
        enemy = monster("SphericGuardian", 100)
        turbo = card("Turbo", CardType.SKILL, cost=0)
        sunder = card(
            "Sunder", CardType.ATTACK, cost=3, damage=32,
            target=True, playable=False,
        )
        game = GameStub([enemy], [sunder, turbo], hp=50, energy=1, act=2)
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, turbo)

    def test_wraith_form_is_saved_on_trivial_turn_when_setup_is_available(self):
        enemy = monster("GremlinLeader", 140)
        wraith = card("Wraith Form v2", CardType.POWER, cost=3, magic=2)
        plans = card("Well-Laid Plans", CardType.POWER, cost=1)
        game = GameStub([enemy], [wraith, plans], hp=57, energy=3, act=2)
        game.room_type = "MonsterRoomElite"
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, plans)

    def test_wraith_form_is_deferred_in_long_boss_even_at_six_loss(self):
        champ = monster(
            "TheChamp", 420, intent=Intent.ATTACK, damage=16, hits=1
        )
        wraith = card(
            "Wraith Form v2", CardType.POWER, cost=3, magic=3,
            upgrades=1,
        )
        footwork = card("Footwork", CardType.POWER, cost=1, magic=2)
        game = GameStub(
            [champ], [wraith, footwork], hp=70, block=10,
            energy=3, act=2,
        )
        game.player.max_hp = 70
        game.room_type = "MonsterRoomBoss"
        game.turn = 1

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, footwork)
        # The former <=5 fixed threshold was bypassed by this exact six-loss
        # state. The continuous model must expose the post-Intangible debt.
        lifecycle_candidates = self.planner._build_candidates(
            game,
            [wraith],
            [champ],
            champ,
            16,
            6,
            6,
        )
        lifecycle = self.planner._lifecycle_trace(
            lifecycle_candidates[0][0]
        )
        self.assertGreater(lifecycle["post_intangible_turns"], 0)
        self.assertLess(lifecycle["adjustment"], 0)

    def test_gremlin_nob_enrage_cost_does_not_override_larger_block(self):
        nob = monster(
            "GremlinNob",
            82,
            intent=Intent.ATTACK,
            damage=20,
            hits=1,
            powers=[Power("EnragePower", "Enrage", 2)],
        )
        defend = card("Defend_G", CardType.SKILL, cost=1, block=8)
        game = GameStub([nob], [defend], hp=50, energy=2, act=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)

    def test_gremlin_nob_frontload_beats_compounding_small_defends(self):
        """Replay floor 13: do not trade five Block for permanent Strength."""

        nob = monster(
            "GremlinNob",
            38,
            intent=Intent.ATTACK,
            damage=18,
            hits=1,
            powers=[
                Power("Anger", "Anger", 2),
                Power("Strength", "Strength", 4),
                Power("Metallicize", "Metallicize", 4),
            ],
        )
        nob.max_hp = 83
        nob.block = 4
        hemokinesis = card(
            "Hemokinesis", CardType.ATTACK, cost=1, damage=15,
            target=True,
        )
        pommel = card(
            "Pommel Strike", CardType.ATTACK, cost=1, damage=9,
            target=True,
        )
        bash = card(
            "Bash", CardType.ATTACK, cost=2, damage=10,
            target=True,
        )
        defends = [
            card(f"Defend-{index}", CardType.SKILL, cost=1, block=5)
            for index in range(3)
        ]
        game = GameStub(
            [nob], [hemokinesis, pommel, bash, *defends],
            hp=52, energy=3, act=1,
        )
        game.turn = 3
        game.player.powers = [Power("Metallicize", "Metallicize", 3)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(CardType.ATTACK, action.card.type)
        self.assertFalse(any(
            item["card_id"].startswith("Defend-")
            for item in self.planner.last_decision["planned_sequence"]
        ))

    def test_gremlin_nob_prices_one_future_hit_for_each_small_block(self):
        """A high-HP race must not spend spare energy compounding Enrage."""

        nob = monster(
            "GremlinNob",
            82,
            intent=Intent.ATTACK,
            damage=14,
            hits=1,
            powers=[Power("EnragePower", "Enrage", 2)],
        )
        strike_card = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        small_blocks = [
            card("Steam Barrier", CardType.SKILL, cost=1, block=6),
            card("Conserve Battery", CardType.SKILL, cost=1, block=7),
        ]
        game = GameStub(
            [nob], [strike_card, *small_blocks],
            hp=55, energy=3, act=1,
        )
        game.turn = 2
        game.room_type = "MonsterRoomElite"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike_card)
        self.assertTrue(all(
            item["card_id"] not in {"Steam Barrier", "Conserve Battery"}
            for item in self.planner.last_decision["planned_sequence"]
        ))

    def test_gremlin_nob_pure_draw_does_not_scale_when_attacks_fill_energy(self):
        """Replay seq 122978: unknown draw is not worth known Enrage."""

        nob = monster(
            "GremlinNob",
            57,
            intent=Intent.ATTACK,
            damage=24,
            hits=1,
            powers=[Power("Anger", "Anger", 2)],
        )
        battle_trance = card(
            "Battle Trance", CardType.SKILL, cost=0, magic=3
        )
        strike = card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub(
            [nob], [battle_trance, strike], hp=56, energy=1, act=1
        )
        game.turn = 3
        game.player.block = 22

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertNotIn(
            "Battle Trance",
            [
                item["card_id"]
                for item in self.planner.last_decision["planned_sequence"]
            ],
        )

    def test_gremlin_nob_tier_one_rank_prices_permanent_strength(self):
        """Replay seq 122981: frontload beats two superficially safer blocks."""

        nob = monster(
            "GremlinNob",
            51,
            intent=Intent.ATTACK,
            damage=18,
            hits=1,
            powers=[Power("Anger", "Anger", 2)],
        )
        strike_a = card(
            "Strike-A", CardType.ATTACK, cost=1, damage=6, target=True
        )
        strike_b = card(
            "Strike-B", CardType.ATTACK, cost=1, damage=6, target=True
        )
        bash = card(
            "Bash", CardType.ATTACK, cost=2, damage=8, target=True,
            magic=2,
        )
        defends = [
            card(f"Defend-{index}", CardType.SKILL, cost=1, block=5)
            for index in range(2)
        ]
        game = GameStub(
            [nob], [strike_a, bash, *defends, strike_b],
            hp=52, energy=3, act=1,
        )
        game.turn = 4

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(CardType.ATTACK, action.card.type)
        self.assertTrue(all(
            not item["card_id"].startswith("Defend-")
            for item in self.planner.last_decision["planned_sequence"]
        ))

    def test_zero_energy_x_attack_is_not_played_without_a_trigger(self):
        nob = monster(
            "GremlinNob", 38, intent=Intent.ATTACK, damage=20, hits=1
        )
        whirlwind = card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=5,
            target=False,
        )
        game = GameStub([nob], [whirlwind], hp=40, energy=0, act=1)
        game.turn = 5

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_upgraded_whirlwind_is_not_x_plus_one_at_zero_energy(self):
        """Replay seq 286261: Whirlwind+ upgrades damage, not X count."""

        champ = monster(
            "Champ", 150, intent=Intent.ATTACK, damage=18, hits=1
        )
        whirlwind = card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=8,
            target=False, upgrades=1,
        )
        game = GameStub([champ], [whirlwind], hp=20, energy=0, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_zero_energy_multicast_is_not_played_without_x_effect(self):
        guardian = monster(
            "SphericGuardian", 48, intent=Intent.ATTACK, damage=10, hits=2
        )
        multicast = card("Multi-Cast", CardType.SKILL, cost=-1)
        game = GameStub([guardian], [multicast], hp=30, energy=0)
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 3)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_zero_energy_x_attack_does_not_waste_redundant_rage_block(self):
        """Replay seq 118454: covered incoming is not a real trigger."""

        attacker = monster(
            "JawWorm", 29, intent=Intent.ATTACK, damage=21, hits=1
        )
        whirlwind = card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=5,
            target=False,
        )
        game = GameStub(
            [attacker], [whirlwind], hp=52, block=21, energy=0, act=3
        )
        game.player.powers = [Power("Rage", "Rage", 3)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_guardian_combo_ignores_serialized_block_on_attacks(self):
        """Replay seq 118098: fake card.block must not absorb Sharp Hide."""

        guardian = monster(
            "TheGuardian",
            53,
            intent=Intent.ATTACK,
            damage=9,
            hits=1,
            powers=[
                Power("Sharp Hide", "Sharp Hide", 3),
                Power("Vulnerable", "Vulnerable", 4),
            ],
        )
        attacks = [
            card("Pommel Strike", CardType.ATTACK, cost=1, damage=14, target=True),
            card("Strike_R", CardType.ATTACK, cost=1, damage=11, target=True),
            card("Strike_R", CardType.ATTACK, cost=1, damage=11, target=True),
        ]
        for attack in attacks:
            attack.block = 3
            attack.base_block = -1
        rage = card("Rage", CardType.SKILL, cost=0, magic=3)
        game = GameStub(
            [guardian], [*attacks, rage], hp=64, energy=4, act=1
        )
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, rage)
        self.assertEqual(
            "guaranteed_combo_setup_before_attacks",
            self.planner.last_decision["reason"],
        )
        self.assertEqual(9, self.planner.last_decision["search"]["actual_loss"])
        self.assertEqual(
            9, self.planner.last_decision["search"]["reactive_hp_cost"]
        )

    def test_reptomancer_focus_switches_to_attacking_dagger(self):
        reptomancer = monster("Reptomancer", 180)
        dagger = monster("Dagger", 20, intent=Intent.ATTACK, damage=25, hits=1)
        strike = card("Strike_G", CardType.ATTACK, cost=1, damage=6, target=True)
        game = GameStub([reptomancer, dagger], [strike], hp=60, energy=1, act=3)
        self.planner.focus_key = ("reptomancer", 0)
        self.planner.combat_key = self.planner._current_combat_key(game)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, dagger)

    def test_reptomancer_aoe_beam_beats_single_target_combo_after_defense(self):
        """Replay 399622: Bash + Anger must not hide safer Whirlwind."""

        left_dagger = monster(
            "Dagger", 20, intent=Intent.ATTACK, damage=37, hits=1,
            powers=[Power("Minion", "Minion", -1)],
        )
        reptomancer = monster(
            "Reptomancer", 184,
            intent=Intent.ATTACK_DEBUFF, damage=25, hits=2,
            powers=[Power("Strength", "Strength", 4)],
        )
        right_dagger = monster(
            "Dagger", 21,
            intent=Intent.ATTACK_DEBUFF, damage=13, hits=1,
            powers=[Power("Minion", "Minion", -1)],
        )
        whirlwind = card(
            "Whirlwind", CardType.ATTACK,
            cost=-1, damage=12, target=False,
        )
        bash = card(
            "Bash", CardType.ATTACK,
            cost=2, damage=14, target=True,
        )
        anger = card(
            "Anger", CardType.ATTACK,
            cost=0, damage=10, target=True,
        )
        game = GameStub(
            [left_dagger, reptomancer, right_dagger],
            [whirlwind, bash, anger],
            hp=45, block=17, energy=2, act=3,
        )
        game.room_type = "MonsterRoomElite"
        game.player.powers = [
            Power("Thorns", "Thorns", 3),
            Power("Strength", "Strength", 4),
            Power("Vulnerable", "Vulnerable", 1),
            Power("Flame Barrier", "Flame Barrier", 4),
        ]
        game.relics = [
            Relic("Bronze Scales", "Bronze Scales"),
            Relic("Paper Frog", "Paper Frog"),
            Relic("TungstenRod", "TungstenRod"),
        ]
        planner = FastCombatPlanner(IroncladPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, whirlwind)
        self.assertEqual("ordered_turn_search", planner.last_decision["reason"])
        self.assertEqual(31, planner.last_decision["search"]["actual_loss"])
        self.assertEqual(
            [0, 136, 0],
            planner.last_decision["search"]["final_enemy_hp"],
        )

    def test_reptomancer_restores_noxious_doomed_dagger_before_current_attack(self):
        left_dagger = monster(
            "Dagger", 6, intent=Intent.ATTACK_DEBUFF, damage=9, hits=1,
            powers=[
                Power("Minion", "Minion", -1),
                Power("Poison", "Poison", 6),
            ],
        )
        reptomancer = monster(
            "Reptomancer", 172,
            intent=Intent.ATTACK_DEBUFF, damage=17, hits=2,
            powers=[Power("Poison", "Poison", 5)],
        )
        right_dagger = monster(
            "Dagger", 6, intent=Intent.ATTACK, damage=25, hits=1,
            powers=[
                Power("Minion", "Minion", -1),
                Power("Poison", "Poison", 5),
            ],
        )
        first_shiv = card(
            "Shiv", CardType.ATTACK, cost=0, damage=6, target=True
        )
        first_shiv.uuid = "first-shiv"
        second_shiv = card(
            "Shiv", CardType.ATTACK, cost=0, damage=6, target=True
        )
        second_shiv.uuid = "second-shiv"
        game = GameStub(
            [left_dagger, reptomancer, right_dagger],
            [first_shiv, second_shiv],
            hp=32, block=12, energy=0, act=3,
        )
        game.room_type = "MonsterRoomElite"
        game.player.powers = [
            Power("Noxious Fumes", "Noxious Fumes", 3),
            Power("After Image", "After Image", 1),
        ]
        planner = FastCombatPlanner(SilentPriority())

        # Noxious Fumes applies Poison only at the next player-turn start and
        # does not deal damage then. Five existing Poison leaves this dagger
        # on one HP for its current move, so it remains a valid target.
        self.assertIn(
            right_dagger, combat_predictor.active_monsters(game)
        )
        self.assertNotIn(
            left_dagger, combat_predictor.attack_monsters(game)
        )
        self.assertIn(
            right_dagger, combat_predictor.attack_monsters(game)
        )
        baseline_loss = combat_predictor.projected_turn_outcome(
            game
        ).total_hp_loss
        self.assertGreaterEqual(baseline_loss, game.player.current_hp)
        self.assertLess(
            combat_predictor.projected_turn_outcome(
                game, excluded_monsters=[right_dagger]
            ).total_hp_loss,
            game.player.current_hp,
        )

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, right_dagger)
        self.assertEqual(("dagger", 2), planner.focus_key)
        self.assertLess(planner.last_decision["search"]["actual_loss"], 32)
        self.assertEqual(
            0, planner.last_decision["search"]["final_enemy_hp"][2]
        )

    def test_reptomancer_restored_dagger_is_in_aoe_search_targets(self):
        reptomancer = monster(
            "Reptomancer", 172,
            intent=Intent.ATTACK_DEBUFF, damage=17, hits=2,
            powers=[Power("Poison", "Poison", 5)],
        )
        dagger = monster(
            "Dagger", 6, intent=Intent.ATTACK, damage=25, hits=1,
            powers=[
                Power("Minion", "Minion", -1),
                Power("Poison", "Poison", 5),
            ],
        )
        dagger_spray = card(
            "Dagger Spray", CardType.ATTACK,
            cost=0, damage=6, target=False,
        )
        game = GameStub(
            [reptomancer, dagger], [dagger_spray],
            hp=32, block=12, energy=0, act=3,
        )
        game.room_type = "MonsterRoomElite"
        game.player.powers = [
            Power("Noxious Fumes", "Noxious Fumes", 3),
            Power("After Image", "After Image", 1),
        ]
        planner = FastCombatPlanner(SilentPriority())

        self.assertGreaterEqual(
            combat_predictor.projected_turn_outcome(game).total_hp_loss,
            game.player.current_hp,
        )

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, dagger_spray)
        self.assertEqual(
            0, planner.last_decision["search"]["final_enemy_hp"][1]
        )
        self.assertLess(
            planner.last_decision["search"]["actual_loss"],
            game.player.current_hp,
        )

    def test_reptomancer_dagger_is_not_restored_for_independent_lethal_loss(self):
        reptomancer = monster("Reptomancer", 172)
        dagger = monster(
            "Dagger", 6, intent=Intent.ATTACK, damage=25, hits=1,
            powers=[Power("Poison", "Poison", 5)],
        )
        shiv = card(
            "Shiv", CardType.ATTACK, cost=0, damage=6, target=True
        )
        game = GameStub(
            [reptomancer, dagger], [shiv],
            hp=3, block=100, energy=0, act=3,
        )
        game.player.powers = [
            Power("Noxious Fumes", "Noxious Fumes", 3),
            Power("Brutality", "Brutality", 3),
        ]
        planner = FastCombatPlanner(SilentPriority())
        active = combat_predictor.active_monsters(game)

        self.assertGreaterEqual(
            combat_predictor.projected_turn_outcome(game).total_hp_loss,
            game.player.current_hp,
        )
        self.assertGreaterEqual(
            combat_predictor.projected_turn_outcome(
                game, excluded_monsters=[dagger]
            ).total_hp_loss,
            game.player.current_hp,
        )

        restored = planner._restore_immediate_reptomancer_attack_targets(
            game, [shiv], active
        )

        self.assertEqual(active, restored)

    def test_reptomancer_does_not_restore_dagger_poisoned_before_its_move(self):
        reptomancer = monster(
            "Reptomancer", 172,
            intent=Intent.ATTACK_DEBUFF, damage=17, hits=2,
        )
        dagger = monster(
            "Dagger", 6, intent=Intent.ATTACK, damage=25, hits=1,
            powers=[
                Power("Minion", "Minion", -1),
                Power("Poison", "Poison", 6),
            ],
        )
        shiv = card(
            "Shiv", CardType.ATTACK, cost=0, damage=6, target=True
        )
        game = GameStub(
            [reptomancer, dagger], [shiv],
            hp=20, block=0, energy=0, act=3,
        )
        game.room_type = "MonsterRoomElite"
        game.player.powers = [
            Power("Noxious Fumes", "Noxious Fumes", 3)
        ]
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, reptomancer)

    def test_reptomancer_immediate_boss_kill_precedes_dagger_restoration(self):
        reptomancer = monster(
            "Reptomancer", 6,
            intent=Intent.ATTACK_DEBUFF, damage=17, hits=2,
        )
        dagger = monster(
            "Dagger", 6, intent=Intent.ATTACK, damage=25, hits=1,
            powers=[
                Power("Minion", "Minion", -1),
                Power("Poison", "Poison", 5),
            ],
        )
        shiv = card(
            "Shiv", CardType.ATTACK, cost=0, damage=6, target=True
        )
        game = GameStub(
            [reptomancer, dagger], [shiv],
            hp=10, block=0, energy=0, act=3,
        )
        game.room_type = "MonsterRoomElite"
        game.player.powers = [
            Power("Noxious Fumes", "Noxious Fumes", 3)
        ]
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, reptomancer)

    def test_awakened_one_focus_clears_cultist_before_boss(self):
        awakened = monster(
            "AwakenedOne",
            300,
            powers=[Power("CuriosityPower", "Curiosity", 1)],
        )
        cultist = monster("Cultist", 30, intent=Intent.ATTACK, damage=12, hits=1)
        strike = card("Strike_G", CardType.ATTACK, cost=1, damage=6, target=True)
        game = GameStub([awakened, cultist], [strike], hp=60, energy=1, act=3)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, cultist)

    def test_healthy_player_attacks_ritual_scaler_instead_of_overblocking(self):
        cultist = monster(
            "Cultist",
            48,
            intent=Intent.ATTACK,
            damage=12,
            hits=1,
            powers=[Power("Ritual", "Ritual", 3)],
        )
        strike = card("Strike_G", CardType.ATTACK, cost=1, damage=6, target=True)
        defend = card("Defend_G", CardType.SKILL, cost=1, block=5)
        game = GameStub([cultist], [strike, defend], hp=60, energy=1, act=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertGreater(
            self.planner.last_decision["search"]["effective_risk_budget"],
            self.planner.last_decision["risk_budget"],
        )

    def test_collector_tier_one_replan_keeps_lower_actual_loss(self):
        collector = monster(
            "TheCollector",
            200,
            intent=Intent.ATTACK,
            damage=20,
            hits=1,
        )
        tempting_attack = card(
            "TemptingAttack",
            CardType.ATTACK,
            cost=1,
            damage=12,
            target=True,
        )
        defend = card("Defend_B", CardType.SKILL, cost=1, block=5)
        game = GameStub(
            [collector],
            [tempting_attack, defend],
            hp=33,
            energy=1,
            act=2,
        )
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)
        self.assertEqual(1, self.planner.last_decision["search"]["tier"])
        self.assertEqual(15, self.planner.last_decision["search"]["actual_loss"])

    def test_choker_replan_keeps_serialized_weakened_state_for_survival_line(self):
        """Replay A4F54: protocol Weakened must survive the ordered search."""

        shield = monster(
            "SpireShield", 101,
            intent=Intent.ATTACK_DEFEND, damage=38, hits=1,
            powers=[
                Power("BackAttack", "Back Attack", -1),
                Power("Poison", "Poison", 5),
                # CommunicationMod serializes the runtime id as Weakened;
                # localized clients do not expose the English display name.
                Power("Weakened", "localized weak", 2),
            ],
        )
        spear = monster(
            "SpireSpear", 53,
            intent=Intent.ATTACK_DEBUFF, damage=5, hits=2,
            powers=[Power("Poison", "Poison", 9)],
        )
        bouncing = card("Bouncing Flask", CardType.SKILL, cost=2, target=False)
        backflip = card("Backflip", CardType.SKILL, cost=1, block=16)
        defend_card = card("Defend_G", CardType.SKILL, cost=1, block=16)
        fumes = card("Noxious Fumes", CardType.POWER, cost=1)
        deadly = card(
            "Deadly Poison", CardType.SKILL, cost=1, target=True, magic=7,
        )
        crippling = card(
            "Crippling Poison", CardType.SKILL, cost=2, magic=7,
        )
        game = GameStub(
            [shield, spear],
            [bouncing, backflip, defend_card, fumes, deadly, crippling],
            hp=13, block=4, energy=4, act=4,
        )
        game.floor = 54
        game.room_type = "MonsterRoomElite"
        game.player.max_hp = 85
        game.player.powers = [
            Power("Surrounded", "Surrounded", -1),
            Power("Thorns", "Thorns", 3),
            Power("Dexterity", "Dexterity", 8),
            Power("Noxious Fumes", "Noxious Fumes", 3),
            Power("After Image", "After Image", 1),
            Power("Strength", "Strength", -1),
        ]
        game.relics = [
            Relic("Velvet Choker", "Velvet Choker", counter=4),
            Relic("Bronze Scales", "Bronze Scales"),
        ]
        game.draw_pile = []
        game.discard_pile = [object() for _ in range(14)]
        game.exhaust_pile = []
        planner = FastCombatPlanner(SilentPriority())
        planner._card_play_turn_key = planner._combat_turn_key(game)
        planner._confirmed_cards_played = 4
        planner._confirmed_card_resolutions = 4
        planner._confirmed_attack_resolutions = 3

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual("Backflip", action.card.card_id, planner.last_decision)
        self.assertEqual(
            ["Backflip", "Defend_G"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        self.assertLess(planner.last_decision["search"]["actual_loss"], 13)
        self.assertEqual(10, planner.last_decision["search"]["actual_loss"])
        self.assertEqual("ordered_turn_search", planner.last_decision["reason"])

    def test_scaling_urgency_never_overrides_survival(self):
        cultist = monster(
            "Cultist",
            48,
            intent=Intent.ATTACK,
            damage=12,
            hits=1,
            powers=[Power("Ritual", "Ritual", 3)],
        )
        strike = card("Strike_G", CardType.ATTACK, cost=1, damage=6, target=True)
        defend = card("Defend_G", CardType.SKILL, cost=1, block=5)
        game = GameStub([cultist], [strike, defend], hp=8, energy=1, act=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)

    def test_sharp_hide_rejects_low_value_nonlethal_attack_at_low_hp(self):
        guardian = monster(
            "TheGuardian",
            100,
            powers=[Power("SharpHidePower", "Sharp Hide", 3)],
        )
        strike = card("Strike_G", CardType.ATTACK, cost=1, damage=6, target=True)
        game = GameStub([guardian], [strike], hp=20, energy=1, act=1)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_guardian_sharp_hide_uses_one_high_value_progress_attack(self):
        guardian = monster(
            "TheGuardian",
            203,
            intent=Intent.ATTACK,
            damage=9,
            hits=1,
            powers=[Power("SharpHidePower", "Sharp Hide", 3)],
        )
        bash = card("Bash", CardType.ATTACK, cost=2, damage=8, target=True)
        strikes = [
            card(f"Strike-{index}", CardType.ATTACK, cost=1, damage=6, target=True)
            for index in range(3)
        ]
        for strike in strikes:
            strike.card_id = "Strike_R"
        perfected = card(
            "Perfected Strike", CardType.ATTACK, cost=2, damage=16, target=True
        )
        game = GameStub(
            [guardian], [bash, *strikes, perfected], hp=59, energy=3, act=1
        )
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, perfected)
        self.assertEqual(
            "reactive_progress_fallback",
            self.planner.last_decision.get("reason"),
        )
        retry = self.planner.choose_card_action(game)
        self.assertIsInstance(retry, PlayCardAction)
        self.assertIs(retry.card, perfected)
        # Confirm the authoritative removal and re-plan the same turn.  The
        # fallback must not spend another three HP on a weak Strike.
        game.hand.remove(perfected)
        game.player.current_hp = 56
        game.player.energy = 1
        guardian.current_hp -= 16

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_reactive_lethal_prefers_block_before_attack_when_energy_allows(self):
        """A legal Defend -> two-hit lethal beats direct Thorns damage."""

        spiker = monster(
            "Spiker",
            12,
            intent=Intent.ATTACK,
            damage=5,
            hits=1,
            powers=[Power("Thorns", "Thorns", 3)],
        )
        first = card("Strike-A", CardType.ATTACK, damage=6, target=True)
        second = card("Strike-B", CardType.ATTACK, damage=6, target=True)
        block = card("Defend_R", CardType.SKILL, cost=1, block=5)
        game = GameStub(
            [spiker], [first, second, block], hp=30, energy=3, act=1
        )
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, block)
        self.assertEqual(
            ["Defend_R", "Strike-A", "Strike-B"],
            [item["card_id"] for item in self.planner.last_decision["planned_sequence"]],
        )
        self.assertEqual(1, self.planner.last_decision["search"]["reactive_hp_cost"])

    def test_outmaneuver_is_delayed_energy_setup_not_current_energy(self):
        target = monster("Target", 100, intent=Intent.BUFF)
        setup = card("Outmaneuver", CardType.SKILL, cost=1)
        game = GameStub([target], [setup], hp=30, energy=1)
        planner = FastCombatPlanner(SilentPriority())

        self.assertEqual(0, planner._energy_gain(setup, game))
        self.assertEqual(2, planner._next_turn_energy_gain(setup))
        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, setup)
        self.assertEqual("future_turn_energy_setup", planner.last_decision["reason"])
        self.assertGreater(planner.last_decision["future_turn_energy_value"], 0)

    def test_conserve_battery_is_played_for_next_turn_energy_when_safe(self):
        target = monster("Target", 100, intent=Intent.BUFF)
        setup = card("Conserve Battery", CardType.SKILL, cost=1, block=7)
        game = GameStub([target], [setup], hp=30, energy=1)
        planner = FastCombatPlanner(DefectPowerPriority())

        self.assertEqual(1, planner._next_turn_energy_gain(setup))
        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, setup)
        self.assertEqual("future_turn_energy_setup", planner.last_decision["reason"])

    def test_guardian_sharp_hide_prefers_carnage_at_sixteen_incoming(self):
        guardian = monster(
            "TheGuardian",
            203,
            intent=Intent.ATTACK,
            damage=16,
            hits=1,
            powers=[Power("SharpHidePower", "Sharp Hide", 3)],
        )
        carnage = card(
            "Carnage", CardType.ATTACK, cost=2, damage=20, target=True
        )
        bash = card("Bash", CardType.ATTACK, cost=2, damage=8, target=True)
        strikes = [
            card(f"Strike-{index}", CardType.ATTACK, cost=1, damage=6, target=True)
            for index in range(2)
        ]
        game = GameStub(
            [guardian], [carnage, bash, *strikes], hp=50, energy=5, act=1
        )
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, carnage)
        self.assertEqual(
            "reactive_progress_fallback",
            self.planner.last_decision.get("reason"),
        )

    def test_guardian_sharp_hide_counts_block_consumption_as_added_loss(self):
        guardian = monster(
            "TheGuardian",
            150,
            intent=Intent.ATTACK,
            damage=16,
            hits=1,
            powers=[Power("SharpHidePower", "Sharp Hide", 3)],
        )
        strike = card("Strike_R", CardType.ATTACK, cost=1, damage=8, target=True)
        thunderclap = card(
            "Thunderclap", CardType.ATTACK, cost=1, damage=6, target=False
        )
        game = GameStub(
            [guardian], [strike, thunderclap], hp=15, block=13, energy=1, act=1
        )
        game.room_type = "MonsterRoomBoss"

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, strike)
        self.assertEqual(3, self.planner.last_decision["reactive_added_hp_loss"])

    def test_reactive_progress_keeps_low_hp_safety_reserve(self):
        guardian = monster(
            "TheGuardian",
            100,
            intent=Intent.ATTACK,
            damage=3,
            hits=1,
            powers=[Power("SharpHidePower", "Sharp Hide", 3)],
        )
        carnage = card(
            "Carnage", CardType.ATTACK, cost=2, damage=20, target=True
        )
        game = GameStub([guardian], [carnage], hp=4, energy=2, act=1)
        game.room_type = "MonsterRoomBoss"

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_panic_button_is_reserved_for_small_current_hit(self):
        automaton = monster(
            "BronzeAutomaton", 300, intent=Intent.ATTACK, damage=10, hits=1
        )
        panic = card("Panic Button", CardType.SKILL, cost=0, block=30)
        game = GameStub([automaton], [panic], hp=73, energy=1, act=2)
        game.room_type = "MonsterRoomBoss"

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_panic_button_is_used_to_prevent_current_lethal(self):
        attacker = monster(
            "Attacker", 100, intent=Intent.ATTACK, damage=25, hits=1
        )
        panic = card("Panic Button", CardType.SKILL, cost=0, block=30)
        game = GameStub([attacker], [panic], hp=20, energy=1, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, panic)

    def test_calculated_gamble_uses_visible_top_cards_for_unique_rescue(self):
        attacker = monster(
            "Attacker", 100, intent=Intent.ATTACK, damage=24, hits=1
        )
        gamble = card("Calculated Gamble", CardType.SKILL, cost=0)
        dazed = [
            card(f"Dazed-{index}", CardType.STATUS, cost=-2, playable=False)
            for index in range(2)
        ]
        for status in dazed:
            status.card_id = "Dazed"
        bottom_wound = card(
            "Wound", CardType.STATUS, cost=-2, playable=False
        )
        deflect = card("Deflect", CardType.SKILL, cost=0, block=4)
        survivor = card("Survivor", CardType.SKILL, cost=1, block=8)
        game = GameStub(
            [attacker], [gamble, *dazed], hp=7, block=6, energy=2, act=2
        )
        # CardGroup's authoritative top is the end of the serialized group.
        # Drawing two therefore sees Survivor then Deflect, not the Wound.
        game.draw_pile = [bottom_wound, deflect, survivor]
        game.discard_pile = []
        game.cards_discarded_this_turn = 0

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, gamble)
        self.assertEqual(
            "emergency_redraw_survival",
            self.planner.last_decision.get("reason"),
        )
        self.assertEqual(
            ["Survivor", "Deflect"],
            self.planner.last_decision["redraw_card_ids"],
        )
        self.assertEqual(6, self.planner.last_decision["search"]["actual_loss"])

    def test_calculated_gamble_is_not_used_on_ordinary_nonlethal_turn(self):
        attacker = monster(
            "Attacker", 100, intent=Intent.ATTACK, damage=6, hits=1
        )
        gamble = card("Calculated Gamble", CardType.SKILL, cost=0)
        dazed = card("Dazed", CardType.STATUS, cost=-2, playable=False)
        defend = card("Defend_G", CardType.SKILL, cost=1, block=5)
        game = GameStub([attacker], [gamble, dazed], hp=20, energy=1, act=2)
        game.draw_pile = [defend]
        game.discard_pile = []
        game.cards_discarded_this_turn = 0

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_calculated_gamble_does_not_claim_rescue_requiring_shuffle(self):
        attacker = monster(
            "Attacker", 100, intent=Intent.ATTACK, damage=24, hits=1
        )
        gamble = card("Calculated Gamble", CardType.SKILL, cost=0)
        dazed = [
            card(f"Dazed-{index}", CardType.STATUS, cost=-2, playable=False)
            for index in range(2)
        ]
        for status in dazed:
            status.card_id = "Dazed"
        deflect = card("Deflect", CardType.SKILL, cost=0, block=4)
        survivor = card("Survivor", CardType.SKILL, cost=1, block=8)
        game = GameStub(
            [attacker], [gamble, *dazed], hp=7, block=6, energy=2, act=2
        )
        game.draw_pile = [deflect]
        game.discard_pile = [survivor]
        game.cards_discarded_this_turn = 0

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_calculated_gamble_rejects_reflex_or_tactician_in_original_hand(self):
        attacker = monster(
            "Attacker", 100, intent=Intent.ATTACK, damage=24, hits=1
        )
        for trigger_id in ("Reflex", "Tactician"):
            with self.subTest(trigger_id=trigger_id):
                gamble = card("Calculated Gamble", CardType.SKILL, cost=0)
                trigger = card(
                    trigger_id, CardType.SKILL, cost=-2, playable=False
                )
                dazed = card("Dazed", CardType.STATUS, cost=-2, playable=False)
                deflect = card("Deflect", CardType.SKILL, cost=0, block=4)
                survivor = card("Survivor", CardType.SKILL, cost=1, block=8)
                game = GameStub(
                    [attacker], [gamble, trigger, dazed],
                    hp=7, block=6, energy=2, act=2,
                )
                game.draw_pile = [deflect, survivor]
                game.discard_pile = []
                game.cards_discarded_this_turn = 0

                action = self.planner.choose_card_action(game)

                self.assertIsInstance(action, EndTurnAction)

    def test_calculated_gamble_rejects_drawn_normality(self):
        attacker = monster(
            "Attacker", 100, intent=Intent.ATTACK, damage=18, hits=1
        )
        gamble = card("Calculated Gamble", CardType.SKILL, cost=0)
        dazed = [
            card(f"Dazed-{index}", CardType.STATUS, cost=-2, playable=False)
            for index in range(2)
        ]
        for status in dazed:
            status.card_id = "Dazed"
        normality = card(
            "Normality", CardType.CURSE, cost=-2, playable=False
        )
        survivor = card("Survivor", CardType.SKILL, cost=1, block=8)
        game = GameStub(
            [attacker], [gamble, *dazed], hp=7, block=6, energy=2, act=2
        )
        game.draw_pile = [normality, survivor]
        game.discard_pile = []
        game.cards_discarded_this_turn = 0

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_calculated_gamble_rejects_evolve_status_draw_trigger(self):
        attacker = monster(
            "Attacker", 100, intent=Intent.ATTACK, damage=18, hits=1
        )
        gamble = card("Calculated Gamble", CardType.SKILL, cost=0)
        dazed = [
            card(f"Dazed-{index}", CardType.STATUS, cost=-2, playable=False)
            for index in range(2)
        ]
        for status in dazed:
            status.card_id = "Dazed"
        wound = card("Wound", CardType.STATUS, cost=-2, playable=False)
        survivor = card("Survivor", CardType.SKILL, cost=1, block=8)
        game = GameStub(
            [attacker], [gamble, *dazed], hp=7, block=6, energy=2, act=2
        )
        game.player.powers = [Power("EvolvePower", "Evolve", 1)]
        game.draw_pile = [survivor, wound]
        game.discard_pile = []
        game.cards_discarded_this_turn = 0

        self.assertIsInstance(self.planner.choose_card_action(game), EndTurnAction)

    def test_base_calculated_gamble_rejects_exhaust_draw_or_generation(self):
        attacker = monster(
            "Attacker", 100, intent=Intent.ATTACK, damage=24, hits=1
        )
        for trigger_id in ("dark_embrace", "dead_branch"):
            with self.subTest(trigger_id=trigger_id):
                gamble = card("Calculated Gamble", CardType.SKILL, cost=0)
                dazed = [
                    card(
                        f"Dazed-{index}", CardType.STATUS,
                        cost=-2, playable=False,
                    )
                    for index in range(2)
                ]
                for status in dazed:
                    status.card_id = "Dazed"
                deflect = card("Deflect", CardType.SKILL, cost=0, block=4)
                survivor = card("Survivor", CardType.SKILL, cost=1, block=8)
                game = GameStub(
                    [attacker], [gamble, *dazed],
                    hp=7, block=6, energy=2, act=2,
                )
                game.draw_pile = [deflect, survivor]
                game.discard_pile = []
                game.cards_discarded_this_turn = 0
                if trigger_id == "dark_embrace":
                    game.player.powers = [
                        Power("DarkEmbracePower", "Dark Embrace", 1)
                    ]
                else:
                    game.relics = [Relic("Dead Branch", "Dead Branch")]

                action = self.planner.choose_card_action(game)

                self.assertIsInstance(action, EndTurnAction)

    def test_upgraded_calculated_gamble_can_ignore_exhaust_only_triggers(self):
        attacker = monster(
            "Attacker", 100, intent=Intent.ATTACK, damage=24, hits=1
        )
        gamble = card(
            "Calculated Gamble", CardType.SKILL, cost=0, upgrades=1
        )
        dazed = [
            card(f"Dazed-{index}", CardType.STATUS, cost=-2, playable=False)
            for index in range(2)
        ]
        for status in dazed:
            status.card_id = "Dazed"
        deflect = card("Deflect", CardType.SKILL, cost=0, block=4)
        survivor = card("Survivor", CardType.SKILL, cost=1, block=8)
        game = GameStub(
            [attacker], [gamble, *dazed], hp=7, block=6, energy=2, act=2
        )
        game.player.powers = [
            Power("DarkEmbracePower", "Dark Embrace", 1)
        ]
        game.relics = [Relic("Dead Branch", "Dead Branch")]
        game.draw_pile = [deflect, survivor]
        game.discard_pile = []
        game.cards_discarded_this_turn = 0

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, gamble)
        self.assertEqual(
            "emergency_redraw_survival",
            self.planner.last_decision.get("reason"),
        )

    def test_aggregate_uses_live_draw_pile_divisor_boundaries(self):
        enemy = monster("JawWorm", 40)
        base = card("Aggregate", CardType.SKILL, cost=1)
        upgraded = card(
            "Aggregate", CardType.SKILL, cost=1, upgrades=1
        )
        game = GameStub([enemy], [base], energy=1, act=2)

        for draw_size, expected in ((7, 1), (8, 2)):
            with self.subTest(upgraded=False, draw_size=draw_size):
                game.draw_pile = [object()] * draw_size
                self.assertEqual(
                    expected, self.planner._energy_gain(base, game)
                )
        for draw_size, expected in ((5, 1), (6, 2)):
            with self.subTest(upgraded=True, draw_size=draw_size):
                game.draw_pile = [object()] * draw_size
                self.assertEqual(
                    expected, self.planner._energy_gain(upgraded, game)
                )

    def test_sundial_third_shuffle_funds_same_turn_followup(self):
        enemy = monster("JawWorm", 100)
        pommel = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=9, target=True,
        )
        bash = card(
            "Bash", CardType.ATTACK,
            cost=2, damage=8, target=True,
        )
        game = GameStub([enemy], [pommel, bash], energy=1, act=2)
        game.relics = [Relic("Sundial", "Sundial", counter=2)]
        game.draw_pile = []
        game.discard_pile = [
            card("Defend_R", CardType.SKILL, playable=False)
        ]
        game.cards_discarded_this_turn = 0

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, pommel)
        search = self.planner.last_decision["search"]
        self.assertEqual(1, search["sundial_shuffle_count"])
        self.assertEqual(2, search["sundial_energy_gained"])
        self.assertEqual(0, search["sundial_counter"])
        self.assertEqual(
            ["Pommel Strike", "Bash"],
            [item["card_id"] for item in self.planner.last_decision[
                "planned_sequence"
            ]],
        )

    def test_sundial_does_not_trigger_without_a_discard_pile(self):
        enemy = monster("JawWorm", 100)
        pommel = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=9, target=True,
        )
        game = GameStub([enemy], [pommel], energy=1, act=2)
        game.relics = [Relic("Sundial", "Sundial", counter=2)]
        game.draw_pile = []
        game.discard_pile = []

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["sundial_shuffle_count"])
        self.assertEqual(0, search["sundial_energy_gained"])
        self.assertEqual(2, search["sundial_counter"])

    def test_concentrate_upgrade_reduces_discard_not_energy_gain(self):
        enemy = monster("JawWorm", 40)
        for upgrades, expected_discard in ((0, 3), (1, 2)):
            with self.subTest(upgrades=upgrades):
                concentrate = card(
                    "Concentrate", CardType.SKILL,
                    cost=0, upgrades=upgrades,
                )
                game = GameStub(
                    [enemy], [concentrate], energy=0, act=2
                )
                self.assertEqual(
                    2, self.planner._energy_gain(concentrate, game)
                )
                self.assertEqual(
                    expected_discard,
                    self.planner._deterministic_discard_count(
                        concentrate, hand_size_before_play=5
                    ),
                )

    def test_dropkick_refund_requires_existing_vulnerable(self):
        for vulnerable, expected_length in ((0, 1), (1, 2)):
            with self.subTest(vulnerable=vulnerable):
                enemy = monster("JawWorm", 80)
                if vulnerable:
                    enemy.powers = [
                        Power("Vulnerable", "Vulnerable", 1)
                    ]
                dropkick = card(
                    "Dropkick", CardType.ATTACK,
                    cost=1, damage=5, target=True,
                )
                strike = card(
                    "Strike_R", CardType.ATTACK,
                    cost=1, damage=20, target=True,
                )
                game = GameStub(
                    [enemy], [dropkick, strike], energy=1, act=2
                )
                game.draw_pile = [object()] * 8
                planner = FastCombatPlanner(PrioritiesStub())

                planner.choose_card_action(game)

                self.assertEqual(
                    expected_length,
                    len(planner.last_decision["planned_sequence"]),
                )

    def test_prior_bash_unlocks_dropkick_refund_in_same_branch(self):
        enemy = monster("JawWorm", 100)
        bash = card(
            "Bash", CardType.ATTACK,
            cost=1, damage=8, target=True, magic=2,
        )
        dropkick = card(
            "Dropkick", CardType.ATTACK,
            cost=1, damage=5, target=True,
        )
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=10, target=True,
        )
        game = GameStub(
            [enemy], [bash, dropkick, strike], energy=2, act=2
        )
        game.draw_pile = [object()] * 8

        self.planner.choose_card_action(game)

        self.assertEqual(
            ["Bash", "Dropkick", "Strike_R"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )

    def test_heel_hook_refund_observes_prior_weak_and_artifact(self):
        for artifact, expected_length in ((0, 3), (1, 2)):
            with self.subTest(artifact=artifact):
                enemy = monster(
                    "JawWorm", 100,
                    intent=Intent.ATTACK, damage=5, hits=1,
                )
                if artifact:
                    enemy.powers = [Power("Artifact", "Artifact", 1)]
                neutralize = card(
                    "Neutralize", CardType.ATTACK,
                    cost=0, damage=3, target=True, magic=1,
                )
                heel_hook = card(
                    "Heel Hook", CardType.ATTACK,
                    cost=1, damage=5, target=True,
                )
                strike = card(
                    "Strike_G", CardType.ATTACK,
                    cost=1, damage=10, target=True,
                )
                game = GameStub(
                    [enemy], [neutralize, heel_hook, strike],
                    energy=1, act=2,
                )
                game.draw_pile = [object()] * 8
                planner = FastCombatPlanner(PrioritiesStub())

                planner.choose_card_action(game)

                self.assertEqual(
                    expected_length,
                    len(planner.last_decision["planned_sequence"]),
                )

    def test_sneaky_strike_refund_uses_authoritative_discard_counter(self):
        for discarded, expected_length in ((0, 1), (1, 2)):
            with self.subTest(discarded=discarded):
                enemy = monster("JawWorm", 100)
                sneaky = card(
                    "Sneaky Strike", CardType.ATTACK,
                    cost=2, damage=12, target=True,
                )
                strike = card(
                    "Strike_G", CardType.ATTACK,
                    cost=2, damage=15, target=True,
                )
                game = GameStub(
                    [enemy], [sneaky, strike], energy=2, act=2
                )
                game.cards_discarded_this_turn = discarded
                planner = FastCombatPlanner(PrioritiesStub())

                planner.choose_card_action(game)

                self.assertEqual(
                    expected_length,
                    len(planner.last_decision["planned_sequence"]),
                )

    def test_flying_knee_energy_is_delayed_and_upgrade_does_not_add_more(self):
        enemy = monster("JawWorm", 40)
        for upgrades in (0, 1):
            with self.subTest(upgrades=upgrades):
                flying_knee = card(
                    "Flying Knee", CardType.ATTACK,
                    cost=1, damage=8, target=True, upgrades=upgrades,
                )
                game = GameStub([enemy], [flying_knee], energy=1, act=2)
                self.assertEqual(
                    0, self.planner._energy_gain(flying_knee, game)
                )
                self.assertEqual(
                    1, self.planner._next_turn_energy_gain(flying_knee)
                )

        conserve = card(
            "Conserve Battery", CardType.SKILL,
            cost=1, block=5, upgrades=1,
        )
        outmaneuver = card(
            "Outmaneuver", CardType.SKILL, cost=1, upgrades=1
        )
        self.assertEqual(1, self.planner._next_turn_energy_gain(conserve))
        self.assertEqual(3, self.planner._next_turn_energy_gain(outmaneuver))

    def test_dynamic_draw_counts_cover_orbs_hand_fill_and_reboot_upgrade(self):
        enemy = monster("JawWorm", 40)
        compile_driver = card(
            "Compile Driver", CardType.ATTACK,
            cost=1, damage=7, target=True,
        )
        expertise = card("Expertise", CardType.SKILL, cost=1)
        expertise_plus = card(
            "Expertise", CardType.SKILL, cost=1, upgrades=1
        )
        reboot = card("Reboot", CardType.SKILL, cost=0)
        reboot_plus = card(
            "Reboot", CardType.SKILL, cost=0, upgrades=1
        )
        game = GameStub([enemy], [compile_driver], energy=1, act=2)
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Frost", "Frost", 5, 2),
            Orb("Lightning", "Lightning", 8, 3),
        ]

        self.assertEqual(
            2, self.planner._card_draw_count(compile_driver, game)
        )
        self.assertEqual(
            5,
            self.planner._card_draw_count(
                expertise, game, hand_size_before_play=2
            ),
        )
        self.assertEqual(
            6,
            self.planner._card_draw_count(
                expertise_plus, game, hand_size_before_play=2
            ),
        )
        self.assertEqual(
            0,
            self.planner._card_draw_count(
                expertise, game, hand_size_before_play=7
            ),
        )
        self.assertEqual(
            1,
            self.planner._card_draw_count(
                expertise_plus, game, hand_size_before_play=7
            ),
        )
        self.assertEqual(4, self.planner._card_draw_count(reboot, game))
        self.assertEqual(
            6, self.planner._card_draw_count(reboot_plus, game)
        )

    def test_reboot_clears_old_hand_and_forces_authoritative_replan(self):
        enemy = monster("JawWorm", 15)
        reboot = card("Reboot", CardType.SKILL, cost=0)
        reboot.exhausts = True
        strike = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=10, target=True,
        )
        game = GameStub([enemy], [reboot, strike], energy=1, act=2)
        game.draw_pile = [object()] * 10

        action = self.planner.choose_card_action(game)

        sequence = [
            step["card_id"]
            for step in self.planner.last_decision["planned_sequence"]
        ]
        self.assertIs(action.card, strike)
        self.assertEqual("Reboot", sequence[-1])
        self.assertNotIn("Strike_B", sequence[sequence.index("Reboot") + 1:])
        self.assertTrue(
            self.planner.last_decision["search"]["random_hand_unknown"]
        )
        self.assertEqual(
            4, self.planner.last_decision["search"]["remaining_hand_size"]
        )

    def test_survivor_discard_choice_truncates_named_followups(self):
        enemy = monster(
            "JawWorm", 60,
            intent=Intent.ATTACK, damage=10, hits=1,
        )
        survivor = card("Survivor", CardType.SKILL, cost=1, block=8)
        regret = card(
            "Regret", CardType.CURSE, cost=-2, playable=False
        )
        game = GameStub(
            [enemy], [survivor, regret], hp=8, energy=1, act=2
        )

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, survivor)
        self.assertEqual(
            ["Survivor"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        self.assertTrue(
            self.planner.last_decision["search"]["random_hand_unknown"]
        )
        self.assertTrue(
            self.planner.last_decision["search"]["discarded_this_turn"]
        )

    def test_bane_second_hit_requires_poison_already_on_target(self):
        for poison, expected_first_hit_loss in ((0, 7), (3, 14)):
            with self.subTest(poison=poison):
                enemy = monster("JawWorm", 20)
                if poison:
                    enemy.powers = [Power("Poison", "Poison", poison)]
                bane = card(
                    "Bane", CardType.ATTACK,
                    cost=1, damage=7, target=True,
                )
                game = GameStub([enemy], [bane], energy=1, act=2)
                planner = FastCombatPlanner(PrioritiesStub())

                action = planner.choose_card_action(game)

                self.assertIs(action.card, bane)
                self.assertEqual(
                    expected_first_hit_loss,
                    planner.last_decision["search"][
                        "first_action_enemy_hp_loss"
                    ],
                )

    def test_deadly_poison_before_bane_unlocks_second_hit_and_true_kill(self):
        enemy = monster("JawWorm", 14)
        deadly_poison = card(
            "Deadly Poison", CardType.SKILL,
            cost=1, magic=5, target=True,
        )
        bane = card(
            "Bane", CardType.ATTACK,
            cost=1, damage=7, target=True,
        )
        game = GameStub(
            [enemy], [bane, deadly_poison], energy=2, act=2
        )

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, deadly_poison)
        self.assertEqual(
            ["Deadly Poison", "Bane"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(
            [0], self.planner.last_decision["search"]["final_enemy_hp"]
        )
        self.assertTrue(
            self.planner.last_decision["search"]["true_combat_end"]
        )

    def test_sunder_true_kill_refund_funds_same_turn_followup(self):
        attacker = monster(
            "Cultist", 32,
            intent=Intent.ATTACK, damage=20, hits=1,
        )
        louse = monster("Louse", 12)
        sunder = card(
            "Sunder", CardType.ATTACK,
            cost=3, damage=32, target=True,
        )
        strike = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=12, target=True,
        )
        game = GameStub(
            [attacker, louse], [sunder, strike],
            hp=20, energy=3, act=2,
        )

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, sunder)
        self.assertIs(action.target_monster, attacker)
        self.assertEqual(
            ["Sunder", "Strike_B"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(
            2, self.planner.last_decision["search"]["remaining_energy"]
        )
        self.assertTrue(
            self.planner.last_decision["search"]["true_combat_end"]
        )

    def test_sunder_nonkill_and_split_transition_do_not_refund(self):
        cases = []
        ordinary = monster("JawWorm", 40)
        cases.append(("ordinary_nonkill", ordinary, []))
        slime = monster("SlimeBoss", 60)
        slime.max_hp = 140
        cases.append(("split_transition", slime, [0]))

        for name, enemy, expected_split in cases:
            with self.subTest(name=name):
                sunder = card(
                    "Sunder", CardType.ATTACK,
                    cost=3, damage=32, target=True,
                )
                game = GameStub([enemy], [sunder], energy=3, act=2)
                planner = FastCombatPlanner(PrioritiesStub())

                action = planner.choose_card_action(game)

                self.assertIs(action.card, sunder)
                search = planner.last_decision["search"]
                self.assertEqual(0, search["remaining_energy"])
                self.assertFalse(search["true_combat_end"])
                self.assertEqual(
                    expected_split, search["split_pending_enemy_indexes"]
                )

    def test_sunder_revival_states_do_not_refund(self):
        awakened = monster(
            "AwakenedOne", 32,
            powers=[Power("Unawakened", "Unawakened", 1)],
        )
        sunder = card(
            "Sunder", CardType.ATTACK,
            cost=3, damage=32, target=True,
        )
        game = GameStub([awakened], [sunder], energy=3, act=3)

        self.planner.choose_card_action(game)

        self.assertEqual(
            0, self.planner.last_decision["search"]["remaining_energy"]
        )
        self.assertEqual(
            [0], self.planner.last_decision["search"][
                "reviving_enemy_indexes"
            ]
        )

        fallen = monster("Darkling", 32)
        living = monster("Darkling", 100)
        darkling_game = GameStub(
            [fallen, living], [sunder], energy=3, act=3
        )
        darkling_planner = FastCombatPlanner(PrioritiesStub())

        darkling_planner.choose_card_action(darkling_game)

        self.assertEqual(
            0,
            darkling_planner.last_decision["search"]["remaining_energy"],
        )
        self.assertEqual(
            [0],
            darkling_planner.last_decision["search"][
                "reviving_enemy_indexes"
            ],
        )

    def test_active_heatsinks_draws_from_known_pile_on_power(self):
        enemy = monster("JawWorm", 50)
        defragment = card("Defragment", CardType.POWER, cost=0)
        drawn = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub([enemy], [defragment], energy=0, act=2)
        game.player.powers = [Power("Heatsink", "Heatsink", 1)]
        game.draw_pile = [drawn]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, defragment)
        search = self.planner.last_decision["search"]
        self.assertEqual(1, search["remaining_hand_size"])
        self.assertEqual(0, search["remaining_draw_pile_size"])
        self.assertEqual(1, search["player_heatsinks"])

    def test_new_heatsinks_does_not_trigger_itself_but_triggers_later_power(self):
        enemy = monster("JawWorm", 50)
        heatsinks = card(
            "Heatsinks", CardType.POWER, cost=0, magic=1
        )
        game = GameStub([enemy], [heatsinks], energy=0, act=2)
        game.draw_pile = [object()]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, heatsinks)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["remaining_hand_size"])
        self.assertEqual(1, search["remaining_draw_pile_size"])
        self.assertEqual(1, search["player_heatsinks"])

        defragment = card("Defragment", CardType.POWER, cost=0)
        chained_game = GameStub(
            [enemy], [heatsinks, defragment], energy=0, act=2
        )
        chained_game.draw_pile = [object()]
        chained_planner = FastCombatPlanner(PrioritiesStub())

        chained_action = chained_planner.choose_card_action(chained_game)

        self.assertIs(chained_action.card, heatsinks)
        self.assertEqual(
            ["Heatsinks", "Defragment"],
            [
                step["card_id"]
                for step in chained_planner.last_decision[
                    "planned_sequence"
                ]
            ],
        )
        chained_search = chained_planner.last_decision["search"]
        self.assertEqual(1, chained_search["remaining_hand_size"])
        self.assertEqual(0, chained_search["remaining_draw_pile_size"])

    def test_active_storm_uses_shared_overflow_queue_and_post_power_focus(self):
        """Storm must preserve Frost/Plasma/Dark overflow effects in order."""

        enemy = monster("JawWorm", 50)
        defragment = card("Defragment", CardType.POWER, cost=0)
        game = GameStub([enemy], [defragment], energy=0, act=2)
        game.player.powers = [Power("StormPower", "Storm", 3)]
        game.player.orbs = [
            Orb("Frost", "Frost", 5, 2),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Dark", "Dark", 12, 6),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, defragment)
        search = self.planner.last_decision["search"]
        # Defragment resolves before Storm: Frost evokes for 6, Dark for 13,
        # and all three new Lightning passives are 4 at END.
        self.assertEqual(13, search["first_action_enemy_hp_loss"])
        self.assertEqual([25], search["final_enemy_hp"])
        self.assertEqual(6, search["final_player_block"])
        self.assertEqual(2, search["remaining_energy"])
        self.assertEqual(1, search["player_focus_bonus"])
        self.assertEqual(
            ["lightning", "lightning", "lightning"],
            search["final_orb_ids"],
        )

    def test_storm_overflow_honors_electrodynamics_and_target_local_lock_on(self):
        locked = monster(
            "Cultist", 30,
            powers=[Power("Lockon", "Lock-On", 2)],
        )
        plain = monster("JawWorm", 30)
        loop = card("Loop", CardType.POWER, cost=0)
        game = GameStub([locked, plain], [loop], energy=0, act=2)
        game.player.powers = [
            Power("StormPower", "Storm", 1),
            Power("Electrodynamics", "Electrodynamics", 1),
        ]
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Plasma", "Plasma", 2, 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, loop)
        search = self.planner.last_decision["search"]
        # Overflow is 12/8, then the new passive is 4/3. Lock-On is local.
        self.assertEqual(20, search["first_action_enemy_hp_loss"])
        self.assertEqual([14, 19], search["final_enemy_hp"])

    def test_random_storm_lightning_never_invents_a_multi_enemy_kill(self):
        left = monster("Louse", 5)
        right = monster("Cultist", 5)
        loop = card("Loop", CardType.POWER, cost=0)
        game = GameStub([left, right], [loop], energy=0, act=2)
        game.player.powers = [Power("StormPower", "Storm", 1)]
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Plasma", "Plasma", 2, 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, loop)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["first_action_enemy_hp_loss"])
        self.assertEqual([5, 5], search["final_enemy_hp"])
        self.assertFalse(search["true_combat_end"])

    def test_new_storm_skips_itself_then_triggers_a_later_power(self):
        enemy = monster("JawWorm", 30)
        storm = card("Storm", CardType.POWER, cost=0)
        loop = card("Loop", CardType.POWER, cost=0)
        game = GameStub([enemy], [storm, loop], energy=0, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, storm)
        self.assertEqual(
            ["Storm", "Loop"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        search = self.planner.last_decision["search"]
        self.assertEqual(1, search["player_storm"])
        # Exactly one orb proves Storm did not trigger itself; Loop did.
        self.assertEqual(["lightning"], search["final_orb_ids"])
        self.assertEqual([27], search["final_enemy_hp"])

    def test_capacitor_expands_slots_before_existing_storm_channels(self):
        enemy = monster("JawWorm", 30)
        capacitor = card(
            "Capacitor", CardType.POWER, cost=0, magic=2
        )
        game = GameStub([enemy], [capacitor], energy=0, act=2)
        game.player.powers = [Power("StormPower", "Storm", 1)]
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Plasma", "Plasma", 2, 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, capacitor)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["first_action_enemy_hp_loss"])
        self.assertEqual(
            ["lightning", "plasma", "plasma", "lightning"],
            search["final_orb_ids"],
        )
        self.assertEqual([24], search["final_enemy_hp"])

    def test_heatsinks_draw_respects_no_draw_hand_cap_and_known_pile(self):
        enemy = monster("JawWorm", 50)
        defragment = card("Defragment", CardType.POWER, cost=0)

        no_draw_game = GameStub(
            [enemy], [defragment], energy=0, act=2
        )
        no_draw_game.player.powers = [
            Power("Heatsink", "Heatsink", 2),
            Power("NoDrawPower", "No Draw", 1),
        ]
        no_draw_game.draw_pile = [object(), object(), object()]
        no_draw_planner = FastCombatPlanner(PrioritiesStub())

        no_draw_planner.choose_card_action(no_draw_game)

        no_draw_search = no_draw_planner.last_decision["search"]
        self.assertEqual(0, no_draw_search["remaining_hand_size"])
        self.assertEqual(3, no_draw_search["remaining_draw_pile_size"])

        full_hand = [defragment] + [
            card(
                f"Wound-{index}", CardType.STATUS,
                cost=-2, playable=False,
            )
            for index in range(9)
        ]
        capped_game = GameStub(
            [enemy], full_hand, energy=0, act=2
        )
        capped_game.player.powers = [
            Power("Heatsink", "Heatsink", 2)
        ]
        capped_game.draw_pile = [object(), object(), object()]
        capped_planner = FastCombatPlanner(PrioritiesStub())

        capped_planner.choose_card_action(capped_game)

        capped_search = capped_planner.last_decision["search"]
        self.assertEqual(10, capped_search["remaining_hand_size"])
        self.assertEqual(2, capped_search["remaining_draw_pile_size"])

        one_known_game = GameStub(
            [enemy], [defragment], energy=0, act=2
        )
        one_known_game.player.powers = [
            Power("Heatsink", "Heatsink", 2)
        ]
        one_known_game.draw_pile = [object()]
        one_known_planner = FastCombatPlanner(PrioritiesStub())

        one_known_planner.choose_card_action(one_known_game)

        one_known_search = one_known_planner.last_decision["search"]
        self.assertEqual(1, one_known_search["remaining_hand_size"])
        self.assertEqual(0, one_known_search["remaining_draw_pile_size"])

    def test_static_discharge_frost_overflow_blocks_later_multihit(self):
        attacker = monster(
            "BookOfStabbing",
            40,
            intent=Intent.ATTACK,
            damage=2,
            hits=2,
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [attacker], [unavailable], hp=20, energy=0, act=2
        )
        game.player.powers = [
            Power("StaticDischarge", "Static Discharge", 1)
        ]
        game.player.orbs = [
            Orb("Frost", "Frost", 5, 2),
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Lightning", "Lightning", 8, 3),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        search = self.planner.last_decision["search"]
        self.assertEqual(2, search["projected_loss"])
        self.assertEqual(2, search["projected_attack_hp_loss"])
        self.assertEqual(2, search["projected_hp_loss"])
        self.assertEqual(1, search["static_discharge_triggered_hits"])
        self.assertEqual(1, search["static_discharge_channels"])
        self.assertEqual(5, search["static_discharge_frost_block"])
        self.assertEqual(
            ["lightning", "lightning", "lightning"],
            search["final_orb_ids"],
        )

    def test_echo_compile_driver_pays_drawn_void_energy_debt(self):
        """Replay d6 seq244401: Echo draws Leap, then Void, for E3-1-1."""

        giant = monster("GiantHead", 80, intent=Intent.BUFF)
        compile_driver = card(
            "Compile Driver",
            CardType.ATTACK,
            cost=1,
            damage=7,
            target=True,
        )
        game = GameStub(
            [giant], [compile_driver], hp=28, energy=3, act=3
        )
        game.player.powers = [Power("EchoForm", "Echo Form", 1)]
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 3)]
        # The protocol exposes the draw-pile top at the list's end.
        game.draw_pile = [
            card("Void", CardType.STATUS, cost=-2, playable=False),
            card("Leap", CardType.SKILL, cost=1, block=9),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, compile_driver)
        search = self.planner.last_decision["search"]
        self.assertEqual(1, search["drawn_void_count"])
        self.assertEqual(1, search["draw_energy_loss"])
        self.assertEqual(1, search["remaining_energy"])
        self.assertEqual(0, search["remaining_draw_pile_size"])

    def test_draw_card_terminal_line_is_not_bound_past_authoritative_draw(self):
        giant = monster(
            "GiantHead", 12, intent=Intent.ATTACK, damage=20, hits=1
        )
        compile_driver = card(
            "Compile Driver",
            CardType.ATTACK,
            cost=1,
            damage=7,
            target=True,
        )
        steam = card(
            "Steam Power", CardType.SKILL, cost=1, block=5
        )
        game = GameStub(
            [giant], [compile_driver, steam], hp=28, energy=2, act=3
        )
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 3)]
        game.draw_pile = [
            card("Void", CardType.STATUS, cost=-2, playable=False)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, compile_driver)
        self.assertIsNone(self.planner._terminal_plan)

    def test_fatal_giant_head_hit_suppresses_static_and_thorns(self):
        """Replay d6: reactions queued after HP reaches zero cannot kill."""

        giant = monster(
            "GiantHead", 19, intent=Intent.ATTACK, damage=50, hits=1
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [giant], [unavailable], hp=28, block=5, energy=0, act=3
        )
        game.player.powers = [
            Power("Focus", "Focus", -1),
            Power("StaticDischarge", "Static Discharge", 2),
            Power("Thorns", "Thorns", 3),
        ]
        game.player.orbs = [
            Orb("Lightning", "Lightning", 7, 2),
            Orb("Lightning", "Lightning", 7, 2),
            Orb("Lightning", "Lightning", 7, 2),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        search = self.planner.last_decision["search"]
        self.assertEqual(45, search["projected_loss"])
        self.assertEqual(0, search["static_discharge_triggered_hits"])
        self.assertEqual(0, search["enemy_reaction_damage_events"])
        self.assertGreaterEqual(
            search["post_death_reactions_suppressed"], 1
        )
        self.assertEqual([13], search["final_enemy_hp"])
        self.assertFalse(search["true_combat_end"])

    def test_nonfatal_one_hp_hit_still_allows_static_discharge(self):
        attacker = monster(
            "GiantHead", 6, intent=Intent.ATTACK, damage=50, hits=1
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [attacker], [unavailable], hp=29, block=22, energy=0, act=3
        )
        game.player.powers = [
            Power("StaticDischarge", "Static Discharge", 1)
        ]
        game.player.orbs = [
            Orb("Dark", "Dark", 8, 6),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Plasma", "Plasma", 2, 1),
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(28, search["projected_loss"])
        self.assertEqual(1, search["static_discharge_triggered_hits"])
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertTrue(search["true_combat_end"])

    def test_fatal_hit_suppresses_thorns_without_static_discharge(self):
        attacker = monster(
            "GiantHead", 3, intent=Intent.ATTACK, damage=10, hits=1
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [attacker], [unavailable], hp=5, energy=0, act=3
        )
        game.player.powers = [Power("Thorns", "Thorns", 3)]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(10, search["projected_loss"])
        self.assertEqual(0, search["enemy_reaction_damage_events"])
        self.assertEqual([3], search["final_enemy_hp"])
        self.assertFalse(search["true_combat_end"])

    def test_nonfatal_hit_still_allows_thorns_without_static_discharge(self):
        attacker = monster(
            "GiantHead", 3, intent=Intent.ATTACK, damage=10, hits=1
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [attacker], [unavailable], hp=11, energy=0, act=3
        )
        game.player.powers = [Power("Thorns", "Thorns", 3)]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(10, search["projected_loss"])
        self.assertEqual(1, search["enemy_reaction_damage_events"])
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertTrue(search["true_combat_end"])

    def test_regeneration_before_attack_keeps_thorns_reaction_alive(self):
        """HP5 heals to10 before a six-damage hit, then Thorns may fire."""

        attacker = monster(
            "Cultist", 3, intent=Intent.ATTACK, damage=6, hits=1
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [attacker], [unavailable], hp=5, energy=0, act=2
        )
        game.player.max_hp = 10
        game.player.powers = [
            Power("Regeneration", "Regeneration", 5),
            Power("Thorns", "Thorns", 3),
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(5, search[
            "player_end_turn_healing_before_attacks"
        ])
        self.assertEqual(1, search["enemy_reaction_damage_events"])
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertTrue(search["true_combat_end"])
        self.assertEqual(4, search["projected_player_hp_after_turn"])
        self.assertEqual(1, search["actual_loss"])
        self.assertGreater(search["tier"], 0)

    def test_reaction_victory_never_applies_next_turn_brutality_or_spends_fairy(self):
        for with_fairy in (False, True):
            with self.subTest(with_fairy=with_fairy):
                attacker = monster("Cultist", 3, intent=Intent.ATTACK, damage=6, hits=1)
                unavailable = card("Strike_B", CardType.ATTACK, cost=1, damage=6, target=True)
                game = GameStub([attacker], [unavailable], hp=5, energy=0, act=2)
                game.player.max_hp = 10
                game.player.powers = [Power("Regeneration", "Regeneration", 5),
                                      Power("Thorns", "Thorns", 3),
                                      Power("Brutality", "Brutality", 4)]
                game.potions = [Potion("FairyPotion", "Fairy in a Bottle", False, True, False)] if with_fairy else []
                self.planner.choose_card_action(game)
                search = self.planner.last_decision["search"]
                self.assertTrue(search["true_combat_end"])
                self.assertEqual(4, search["projected_player_hp_after_turn"])
                self.assertEqual(0, search["projected_next_turn_start_hp_loss"])
                self.assertFalse(search["fairy_revive_consumed"])
                self.assertGreater(search["tier"], 0)

    def test_fairy_health_ledger_controls_each_reflected_hit(self):
        # Expected values follow ordered game events, independent of scores.
        cases = (
            ("burn_revive", True, 3, 6, 1, 0, 24, 0, 3),
            ("attack_revive_stops_multihit", False, 3, 6, 2, 0, 30, 0, 3),
            ("revive_then_regenerate", True, 3, 6, 1, 5, 29, 0, 3),
            ("burn_revive_then_die", True, 3, 30, 1, 0, 0, 3, 0),
            ("second_hit_kills_after_revive", False, 6, 30, 2, 0, 0, 3, 3),
        )
        for reaction in ("Thorns", "Flame Barrier"):
            for name, burn, enemy_hp, damage, hits, regen, hp, final_enemy, reflected in cases:
                with self.subTest(reaction=reaction, case=name):
                    hand = [card("Strike_B", CardType.ATTACK, cost=1, damage=6, target=True)]
                    if burn:
                        hand.append(card("Burn", CardType.STATUS, cost=-2, playable=False))
                    attacker = monster("Cultist", enemy_hp, intent=Intent.ATTACK, damage=damage, hits=hits)
                    game = GameStub([attacker], hand, hp=1, energy=0, act=2)
                    game.player.max_hp = 100
                    game.player.powers = [Power(reaction, reaction, 3),
                                          Power("Regeneration", "Regeneration", regen)]
                    game.potions = [Potion("FairyPotion", "Fairy in a Bottle", False, True, False)]
                    self.planner.choose_card_action(game)
                    search = self.planner.last_decision["search"]
                    self.assertTrue(search["fairy_revive_consumed"])
                    self.assertEqual(30, search["fairy_revive_healing"])
                    self.assertEqual(hp, search["projected_player_hp_after_turn"])
                    self.assertEqual([final_enemy], search["final_enemy_hp"])
                    self.assertEqual(reflected, search["player_thorns_reaction_damage"])
                    self.assertEqual(final_enemy == 0, search["true_combat_end"])
                    if hp == 0:
                        self.assertEqual(0, search["tier"])

    def test_fairy_revive_preserves_static_discharge_and_painful_stabs(self):
        attacker = monster("Cultist", 8, intent=Intent.ATTACK, damage=6, hits=2,
                           powers=[Power("Painful Stabs", "Painful Stabs", 1)])
        game = GameStub([attacker], [card("Burn", CardType.STATUS, cost=-2, playable=False),
                                    card("Strike_B", CardType.ATTACK, cost=1, damage=6, target=True)],
                        hp=1, energy=0, act=2)
        game.player.max_hp = 100
        game.player.powers = [Power("Static Discharge", "Static Discharge", 1)]
        game.player.orbs = [Orb("Dark", "Dark", 8, 6),
                            Orb("Plasma", "Plasma", 2, 1),
                            Orb("Plasma", "Plasma", 2, 1)]
        game.potions = [Potion("FairyPotion", "Fairy in a Bottle", False, True, False)]
        self.planner.choose_card_action(game)
        search = self.planner.last_decision["search"]
        self.assertEqual(24, search["projected_player_hp_after_turn"])
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertTrue(search["true_combat_end"])
        self.assertEqual(1, search["static_discharge_triggered_hits"])
        self.assertEqual(1, search["painful_stabs_wounds"])

    def test_regeneration_exact_fatal_boundary_still_suppresses_thorns(self):
        """HP5+Regen1 reaches six, so a six-damage hit is still fatal."""

        attacker = monster(
            "Cultist", 3, intent=Intent.ATTACK, damage=6, hits=1
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [attacker], [unavailable], hp=5, energy=0, act=2
        )
        game.player.max_hp = 10
        game.player.powers = [
            Power("Regeneration", "Regeneration", 1),
            Power("Thorns", "Thorns", 3),
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(1, search[
            "player_end_turn_healing_before_attacks"
        ])
        self.assertEqual(0, search["enemy_reaction_damage_events"])
        self.assertEqual([3], search["final_enemy_hp"])
        self.assertFalse(search["true_combat_end"])
        self.assertEqual(0, search["projected_player_hp_after_turn"])
        self.assertEqual(0, search["tier"])

    def test_played_static_discharge_reacts_in_same_enemy_turn(self):
        attacker = monster(
            "Cultist", 8, intent=Intent.ATTACK, damage=1, hits=2
        )
        static_discharge = card(
            "Static Discharge", CardType.POWER, cost=1, magic=1
        )
        game = GameStub(
            [attacker], [static_discharge], hp=20, energy=1, act=2
        )
        game.player.orbs = [
            Orb("Dark", "Dark", 8, 6),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Plasma", "Plasma", 2, 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, static_discharge)
        search = self.planner.last_decision["search"]
        self.assertEqual(1, search["player_static_discharge"])
        self.assertEqual(1, search["static_discharge_triggered_hits"])
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertTrue(search["true_combat_end"])

    def test_static_discharge_fires_before_tungsten_zeroes_hp_loss(self):
        attacker = monster(
            "Cultist", 8, intent=Intent.ATTACK, damage=1, hits=2
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [attacker], [unavailable], hp=20, energy=0, act=2
        )
        game.player.powers = [
            Power("StaticDischarge", "Static Discharge", 1)
        ]
        game.player.orbs = [
            Orb("Dark", "Dark", 8, 6),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Plasma", "Plasma", 2, 1),
        ]
        game.relics = [Relic("Tungsten Rod", "Tungsten Rod")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["projected_loss"])
        self.assertEqual(1, search["static_discharge_triggered_hits"])
        self.assertEqual(1, search["static_discharge_zero_hp_loss_triggers"])
        self.assertEqual([0], search[
            "static_discharge_stopped_attacker_indexes"
        ])
        self.assertTrue(search["true_combat_end"])
        self.assertEqual([0], search["final_enemy_hp"])

    def test_static_discharge_buffered_hit_does_not_trigger(self):
        attacker = monster(
            "BookOfStabbing",
            40,
            intent=Intent.ATTACK,
            damage=10,
            hits=2,
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [attacker], [unavailable], hp=30, energy=0, act=2
        )
        game.player.powers = [
            Power("Buffer", "Buffer", 1),
            Power("StaticDischarge", "Static Discharge", 1),
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(10, search["projected_loss"])
        self.assertEqual(1, search["static_discharge_triggered_hits"])
        self.assertEqual(1, search["static_discharge_channels"])

    def test_static_discharge_uses_intangible_hit_and_all_power_layers(self):
        attacker = monster(
            "BookOfStabbing",
            40,
            intent=Intent.ATTACK,
            damage=10,
            hits=2,
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [attacker], [unavailable], hp=30, energy=0, act=2
        )
        game.player.powers = [
            Power("IntangiblePlayer", "Intangible", 1),
            Power("StaticDischarge", "Static Discharge", 2),
        ]
        game.player.orbs = [
            Orb("Empty", "Empty", 0, 0) for _ in range(5)
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(2, search["projected_loss"])
        self.assertEqual(2, search["static_discharge_triggered_hits"])
        self.assertEqual(4, search["static_discharge_channels"])
        self.assertEqual(
            ["lightning", "lightning", "lightning", "lightning"],
            search["final_orb_ids"],
        )

    def test_static_discharge_random_lightning_never_invents_multitarget_kill(self):
        first = monster(
            "Cultist", 5, intent=Intent.ATTACK, damage=1, hits=1
        )
        second = monster(
            "JawWorm", 5, intent=Intent.ATTACK, damage=10, hits=1
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [first, second], [unavailable], hp=30, energy=0, act=2
        )
        game.player.powers = [
            Power("StaticDischarge", "Static Discharge", 1)
        ]
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Plasma", "Plasma", 2, 1),
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(1, search[
            "static_discharge_random_target_evokes"
        ])
        self.assertFalse(search["true_combat_end"])
        self.assertEqual([5, 5], search["final_enemy_hp"])
        self.assertEqual(11, search["projected_loss"])

    def test_static_discharge_electrodynamics_kills_later_attacker(self):
        first = monster(
            "Cultist", 8, intent=Intent.ATTACK, damage=1, hits=2
        )
        second = monster(
            "JawWorm", 8, intent=Intent.ATTACK, damage=20, hits=1
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [first, second], [unavailable], hp=30, energy=0, act=2
        )
        game.player.powers = [
            Power("StaticDischarge", "Static Discharge", 1),
            Power("Electro", "Electrodynamics", 1),
        ]
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Plasma", "Plasma", 2, 1),
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(1, search["projected_loss"])
        self.assertEqual([0, 0], search["final_enemy_hp"])
        self.assertTrue(search["true_combat_end"])
        self.assertEqual([0], search[
            "static_discharge_stopped_attacker_indexes"
        ])

    def test_static_discharge_fungi_kill_weakens_later_attack(self):
        fungi = monster(
            "FungiBeast",
            8,
            intent=Intent.ATTACK,
            damage=1,
            hits=1,
            powers=[Power("SporeCloudPower", "Spore Cloud", 2)],
        )
        cultist = monster(
            "Cultist", 20, intent=Intent.ATTACK, damage=10, hits=1
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [fungi, cultist], [unavailable], hp=40, energy=0, act=1
        )
        game.player.powers = [
            Power("StaticDischarge", "Static Discharge", 1)
        ]
        game.player.orbs = [
            Orb("Dark", "Dark", 8, 6),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Plasma", "Plasma", 2, 1),
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(16, search["projected_loss"])
        self.assertEqual([0, 20], search["final_enemy_hp"])
        self.assertEqual(2, search["static_discharge_triggered_hits"])
        self.assertEqual(2, search["static_discharge_channels"])

    def test_bronze_scales_full_block_kills_on_third_hit_and_stops_move(self):
        attacker = monster(
            "Guardian",
            9,
            intent=Intent.ATTACK,
            damage=5,
            hits=5,
        )
        unavailable = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [attacker], [unavailable],
            hp=30, block=25, energy=0, act=2,
        )
        game.relics = [Relic("Bronze Scales", "Bronze Scales")]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["projected_loss"])
        self.assertEqual(3, search["enemy_reaction_damage_events"])
        self.assertEqual(9, search["bronze_scales_reaction_damage"])
        self.assertEqual([0], search[
            "enemy_reaction_stopped_attacker_indexes"
        ])
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertTrue(search["true_combat_end"])

    def test_seq183613_suck_heals_before_thorns_not_false_combat_end(self):
        """Replay Ironclad A2F33 after Shrug, before Perfected Strike."""

        parasite = monster(
            "Shelled Parasite",
            15,
            intent=Intent.ATTACK_BUFF,
            damage=10,
            hits=1,
            powers=[Power("Plated Armor", "Plated Armor", 10)],
        )
        parasite.max_hp = 72
        parasite.block = 11
        perfected = card(
            "Perfected Strike",
            CardType.ATTACK,
            cost=2,
            damage=24,
            target=True,
        )
        game = GameStub(
            [parasite], [perfected],
            hp=14, block=8, energy=2, act=2,
        )
        game.player.max_hp = 85
        game.player.powers = [Power("Thorns", "Thorns", 3)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, perfected)
        search = self.planner.last_decision["search"]
        self.assertEqual(2, search["actual_loss"])
        self.assertEqual(2, search["enemy_attack_healing_requested"])
        self.assertEqual(2, search["enemy_attack_healing"])
        self.assertEqual(1, search["enemy_attack_healing_events"])
        self.assertEqual(3, search["enemy_reaction_total_damage"])
        self.assertEqual([1], search["final_enemy_hp"])
        self.assertFalse(search["true_combat_end"])

    def test_same_turn_weak_reduces_suck_heal_with_modeled_hp_loss(self):
        parasite = monster(
            "ShelledParasite",
            20,
            intent=Intent.ATTACK_BUFF,
            damage=10,
            hits=1,
        )
        parasite.max_hp = 72
        neutralize = card(
            "Neutralize",
            CardType.ATTACK,
            cost=0,
            damage=3,
            target=True,
            magic=1,
        )
        game = GameStub(
            [parasite], [neutralize], hp=30, energy=0, act=2
        )

        action = self.planner.choose_card_action(game)

        self.assertIs(action.target_monster, parasite)
        search = self.planner.last_decision["search"]
        self.assertEqual(7, search["actual_loss"])
        self.assertEqual(7, search["enemy_attack_healing"])
        self.assertEqual([24], search["final_enemy_hp"])
        self.assertEqual([7], search[
            "branch_enemy_attack_damage_per_hit"
        ])
        self.assertEqual(
            0, search["unattributed_attack_hp_loss_reduction"]
        )

    def test_repeated_weak_does_not_reduce_same_suck_attack_twice(self):
        parasite = monster(
            "ShelledParasite",
            30,
            intent=Intent.ATTACK_BUFF,
            damage=10,
            hits=1,
        )
        parasite.max_hp = 72
        first = card(
            "Neutralize",
            CardType.ATTACK,
            cost=0,
            damage=3,
            target=True,
            magic=1,
        )
        second = card(
            "Neutralize",
            CardType.ATTACK,
            cost=0,
            damage=3,
            target=True,
            magic=1,
        )
        second.uuid = "neutralize-second-copy"
        game = GameStub(
            [parasite], [first, second], hp=30, energy=0, act=2
        )

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(
            ["Neutralize", "Neutralize"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        self.assertEqual(7, search["actual_loss"])
        self.assertEqual(7, search["enemy_attack_healing"])
        self.assertEqual([31], search["final_enemy_hp"])
        self.assertEqual([7], search[
            "branch_enemy_attack_damage_per_hit"
        ])

    def test_branch_weak_is_floored_before_block_and_suck_heal(self):
        parasite = monster(
            "ShelledParasite", 30,
            intent=Intent.ATTACK_BUFF, damage=10, hits=1,
        )
        parasite.max_hp = 72
        neutralize = card(
            "Neutralize", CardType.ATTACK,
            cost=0, damage=3, target=True, magic=1,
        )
        game = GameStub(
            [parasite], [neutralize],
            hp=30, block=5, energy=0, act=2,
        )

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual([7], search[
            "branch_enemy_attack_damage_per_hit"
        ])
        self.assertEqual(2, search["actual_loss"])
        self.assertEqual(2, search["enemy_attack_healing"])
        self.assertEqual([29], search["final_enemy_hp"])

    def test_authoritative_intangible_clamp_survives_branch_weak(self):
        parasite = monster(
            "ShelledParasite", 30,
            intent=Intent.ATTACK_BUFF, damage=1, hits=1,
        )
        parasite.move_base_damage = 10
        parasite.max_hp = 72
        neutralize = card(
            "Neutralize", CardType.ATTACK,
            cost=0, damage=3, target=True, magic=1,
        )
        game = GameStub(
            [parasite], [neutralize],
            hp=30, energy=0, act=2,
        )
        game.player.powers = [
            Power("IntangiblePlayer", "Intangible", 1)
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual([1], search[
            "branch_enemy_attack_damage_per_hit"
        ])
        self.assertEqual(1, search["actual_loss"])
        self.assertEqual(1, search["enemy_attack_healing"])

    def test_targeted_weak_budget_is_not_spent_by_earlier_attacker(self):
        cultist = monster(
            "Cultist",
            80,
            intent=Intent.ATTACK,
            damage=2,
            hits=1,
        )
        parasite = monster(
            "Shelled Parasite",
            20,
            intent=Intent.ATTACK_BUFF,
            damage=10,
            hits=1,
        )
        parasite.max_hp = 72
        neutralize = card(
            "Neutralize",
            CardType.ATTACK,
            cost=0,
            damage=3,
            target=True,
            magic=1,
        )
        game = GameStub(
            [cultist, parasite], [neutralize],
            hp=30, energy=0, act=2,
        )

        action = self.planner.choose_card_action(game)

        self.assertIs(action.target_monster, parasite)
        search = self.planner.last_decision["search"]
        self.assertEqual(9, search["actual_loss"])
        self.assertEqual(7, search["enemy_attack_healing"])
        self.assertEqual([80, 24], search["final_enemy_hp"])
        self.assertEqual([2, 7], search[
            "branch_enemy_attack_damage_per_hit"
        ])

    def test_branch_weak_precedes_block_and_suppresses_static_discharge(self):
        cultist = monster(
            "Cultist", 9,
            intent=Intent.ATTACK, damage=2, hits=1,
        )
        neutralize = card(
            "Neutralize", CardType.ATTACK,
            cost=0, damage=3, target=True, magic=1,
        )
        game = GameStub(
            [cultist], [neutralize],
            hp=30, block=1, energy=0, act=2,
        )
        game.player.powers = [
            Power("StaticDischarge", "Static Discharge", 1)
        ]
        game.player.orbs = [
            Orb("Dark", "Dark", 8, 6),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Plasma", "Plasma", 2, 1),
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual([1], search[
            "branch_enemy_attack_damage_per_hit"
        ])
        self.assertEqual(0, search["actual_loss"])
        self.assertEqual(0, search["static_discharge_triggered_hits"])
        self.assertEqual([6], search["final_enemy_hp"])
        self.assertFalse(search["true_combat_end"])

    def test_branch_weak_precedes_block_and_avoids_painful_stabs(self):
        book = monster(
            "BookOfStabbing", 40,
            intent=Intent.ATTACK, damage=2, hits=3,
            powers=[Power("PainfulStabs", "Painful Stabs", 1)],
        )
        neutralize = card(
            "Neutralize", CardType.ATTACK,
            cost=0, damage=3, target=True, magic=1,
        )
        game = GameStub(
            [book], [neutralize],
            hp=30, block=3, energy=0, act=2,
        )

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual([1], search[
            "branch_enemy_attack_damage_per_hit"
        ])
        self.assertEqual(0, search["actual_loss"])
        self.assertEqual(0, search["generated_wounds"])

    def test_prior_debuff_removes_artifact_then_branch_weak_lands(self):
        parasite = monster(
            "ShelledParasite", 40,
            intent=Intent.ATTACK_BUFF, damage=10, hits=1,
            powers=[Power("Artifact", "Artifact", 1)],
        )
        parasite.max_hp = 72
        beam_cell = card(
            "Beam Cell", CardType.ATTACK,
            cost=0, damage=3, target=True, magic=2,
        )
        neutralize = card(
            "Neutralize", CardType.ATTACK,
            cost=0, damage=3, target=True, magic=1,
        )
        game = GameStub(
            [parasite], [beam_cell, neutralize],
            hp=30, energy=0, act=2,
        )

        self.planner.choose_card_action(game)

        self.assertEqual(
            ["Beam Cell", "Neutralize"],
            [
                step["card_id"]
                for step in self.planner.last_decision["planned_sequence"]
            ],
        )
        search = self.planner.last_decision["search"]
        self.assertGreater(search["final_enemy_weak"][0], 0)
        self.assertEqual([7], search[
            "branch_enemy_attack_damage_per_hit"
        ])
        self.assertEqual(7, search["actual_loss"])
        self.assertEqual(7, search["enemy_attack_healing"])

    def test_crippling_poison_aoe_weak_uses_each_branch_artifact(self):
        first = monster(
            "ShelledParasite", 40,
            intent=Intent.ATTACK_BUFF, damage=8, hits=1,
            powers=[Power("Artifact", "Artifact", 1)],
        )
        second = monster(
            "ShelledParasite", 40,
            intent=Intent.ATTACK_BUFF, damage=8, hits=1,
        )
        first.max_hp = second.max_hp = 72
        crippling = card(
            "Crippling Poison", CardType.SKILL,
            cost=0, magic=4,
        )
        game = GameStub(
            [first, second], [crippling],
            hp=30, energy=0, act=2,
        )

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertTrue(all(value > 0 for value in search["final_enemy_weak"]))
        self.assertEqual([6, 6], search[
            "branch_enemy_attack_damage_per_hit"
        ])
        self.assertEqual(12, search["actual_loss"])
        self.assertEqual(12, search["enemy_attack_healing"])

    def test_weak_vulnerable_cards_respect_real_artifact_order(self):
        for card_id, card_type, target in (
            ("Shockwave", CardType.SKILL, False),
            ("Uppercut", CardType.ATTACK, True),
        ):
            with self.subTest(card_id=card_id):
                enemy = monster(
                    "Cultist", 40,
                    intent=Intent.ATTACK, damage=8, hits=1,
                    powers=[Power("Artifact", "Artifact", 1)],
                )
                debuff = card(
                    card_id, card_type,
                    cost=0,
                    damage=(1 if target else 0),
                    target=target,
                    magic=1,
                )
                hand = [debuff]
                if card_id == "Shockwave":
                    hand.append(card(
                        "Strike_R", CardType.ATTACK,
                        cost=0, damage=6, target=True,
                    ))
                game = GameStub(
                    [enemy], hand, hp=30, energy=0, act=2
                )
                planner = FastCombatPlanner(PrioritiesStub())

                planner.choose_card_action(game)

                search = planner.last_decision["search"]
                self.assertEqual([0], search["final_enemy_weak"])
                self.assertGreater(search["final_enemy_vulnerable"][0], 0)
                self.assertEqual([8], search[
                    "branch_enemy_attack_damage_per_hit"
                ])

    def test_duplicated_debuff_cards_replay_artifact_actions_per_copy(self):
        cases = (
            (
                "double_tap_uppercut",
                card(
                    "Uppercut", CardType.ATTACK,
                    cost=0, damage=1, target=True, magic=1,
                ),
                Power("DoubleTapPower", "Double Tap", 1),
                True,
            ),
            (
                "echo_shockwave",
                card("Shockwave", CardType.SKILL, cost=0, magic=1),
                Power("EchoFormPower", "Echo Form", 1),
                True,
            ),
            (
                "echo_crippling_poison",
                card(
                    "Crippling Poison", CardType.SKILL,
                    cost=0, magic=4,
                ),
                Power("EchoFormPower", "Echo Form", 1),
                False,
            ),
        )
        for label, debuff, duplicate_power, expect_vulnerable in cases:
            with self.subTest(label=label):
                enemy = monster(
                    "Cultist", 80,
                    intent=Intent.ATTACK, damage=8, hits=1,
                    powers=[Power("Artifact", "Artifact", 2)],
                )
                enemy.move_base_damage = 8
                game = GameStub(
                    [enemy], [debuff], hp=30, energy=0, act=2
                )
                game.player.powers = [duplicate_power]
                planner = FastCombatPlanner(PrioritiesStub())

                action = planner.choose_card_action(game)

                self.assertIsInstance(action, PlayCardAction)
                search = planner.last_decision["search"]
                self.assertGreater(search["final_enemy_weak"][0], 0)
                if expect_vulnerable:
                    self.assertGreater(
                        search["final_enemy_vulnerable"][0], 0
                    )
                self.assertEqual(
                    [6], search["branch_enemy_attack_damage_per_hit"]
                )
                self.assertEqual(6, search["actual_loss"])

    def test_double_tap_poisoned_stab_repeats_damage_and_poison(self):
        enemy = monster("Cultist", 100)
        poisoned_stab = card(
            "Poisoned Stab", CardType.ATTACK,
            cost=0, damage=6, target=True, magic=3,
        )
        game = GameStub([enemy], [poisoned_stab], energy=0, act=2)
        game.player.powers = [
            Power("DoubleTapPower", "Double Tap", 1)
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, poisoned_stab)
        # Two six-damage resolutions followed by the repeated six Poison.
        self.assertEqual(
            [82], self.planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_echo_and_burst_repeat_catalyst_against_evolving_poison(self):
        for label, hand, powers in (
            (
                "echo",
                [card(
                    "Catalyst", CardType.SKILL,
                    cost=0, magic=-1, upgrades=1,
                )],
                [Power("EchoFormPower", "Echo Form", 1)],
            ),
            (
                "burst_then_catalyst",
                [
                    card("Burst", CardType.SKILL, cost=0, magic=1),
                    card(
                        "Catalyst", CardType.SKILL,
                        cost=0, magic=-1, upgrades=1,
                    ),
                ],
                [],
            ),
        ):
            with self.subTest(label=label):
                enemy = monster(
                    "Cultist", 1000,
                    powers=[Power("Poison", "Poison", 10)],
                )
                game = GameStub([enemy], hand, energy=0, act=2)
                game.player.powers = powers
                planner = FastCombatPlanner(PrioritiesStub())

                planner.choose_card_action(game)

                if label == "burst_then_catalyst":
                    self.assertEqual(
                        ["Burst", "Catalyst"],
                        [
                            step["card_id"]
                            for step in planner.last_decision[
                                "planned_sequence"
                            ]
                        ],
                    )
                # Catalyst+ evolves the same stack 10 -> 30 -> 90; the
                # terminal Poison tick therefore leaves 910 HP.
                self.assertEqual(
                    [910], planner.last_decision["search"]["final_enemy_hp"]
                )

    def test_bouncing_flask_terminal_line_always_replans(self):
        enemy = monster("Cultist", 18)
        flask = card(
            "Bouncing Flask", CardType.SKILL, cost=0, magic=3
        )
        catalyst = card(
            "Catalyst", CardType.SKILL, cost=0, magic=-1, target=True
        )
        game = GameStub([enemy], [flask, catalyst], energy=0, act=2)

        self.planner.choose_card_action(game)

        self.assertTrue(
            self.planner.last_decision["search"]["true_combat_end"]
        )
        self.assertIsNone(self.planner._terminal_plan)

    def test_unknown_intent_modifiers_scale_branch_delta_conservatively(self):
        back_attack = monster(
            "BackAttack", 40,
            intent=Intent.ATTACK, damage=15, hits=1,
        )
        back_attack.move_base_damage = 10
        game = GameStub([back_attack], [], hp=30, energy=0, act=2)
        weak_delta = self.planner._branch_enemy_attack_damage_delta(
            game, back_attack, weak_amount=1
        )
        # The unknown 1.5x modifier may round the real result to 11. Keeping
        # 12 is conservative; applying Weak to the serialized 15 as if it
        # were the base would incorrectly produce 11 without evidence.
        self.assertEqual(12, 15 + weak_delta)

        wrath = monster(
            "WrathAttack", 40,
            intent=Intent.ATTACK, damage=20, hits=1,
        )
        wrath.move_base_damage = 10
        wrath_delta = self.planner._branch_enemy_attack_damage_delta(
            game, wrath, strength_gain=2
        )
        # A positive Strength delta must inherit the unknown 2x multiplier.
        self.assertEqual(24, 20 + wrath_delta)

    def test_spore_vulnerable_and_new_weak_share_one_rounding_chain(self):
        doomed = monster(
            "FungiBeast", 3,
            powers=[
                Power("Poison", "Poison", 3),
                Power("SporeCloudPower", "Spore Cloud", 2),
            ],
        )
        attacker = monster(
            "Cultist", 40,
            intent=Intent.ATTACK, damage=10, hits=1,
        )
        attacker.move_base_damage = 10
        neutralize = card(
            "Neutralize", CardType.ATTACK,
            cost=0, damage=3, target=True, magic=1,
        )
        game = GameStub(
            [doomed, attacker], [neutralize], hp=30, energy=0, act=2
        )

        action = self.planner.choose_card_action(game)

        self.assertIs(action.target_monster, attacker)
        search = self.planner.last_decision["search"]
        # floor(10 * .75 Weak * 1.5 Vulnerable) = 11. Applying the
        # multipliers in two separately-floored stages would report ten.
        self.assertEqual([0, 11], search[
            "branch_enemy_attack_damage_per_hit"
        ])
        self.assertEqual(11, search["actual_loss"])

    def test_weak_then_vulnerable_reconstructs_single_hit_before_rounding(self):
        """A weak 9-damage hit must not become a 9-damage Vulnerable hit.

        The protocol's dynamic ``damage`` is already ``floor(9 * .75) ==
        6``.  The game applies target Vulnerable in the same full multiplier
        chain, so the hit is ten before the Snake Plant's three Block, for
        seven actual HP loss.  Reusing the serialized six and then rounding
        would incorrectly leave the target on 34 HP.
        """

        snake_plant = monster(
            "SnakePlant",
            40,
            powers=[Power("Vulnerable", "Vulnerable", 1)],
        )
        snake_plant.block = 3
        pommel_strike = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        pommel_strike.base_damage = 9
        game = GameStub([snake_plant], [pommel_strike], hp=30, energy=1, act=2)
        game.player.powers = [Power("Weak", "Weak", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, pommel_strike)
        search = self.planner.last_decision["search"]
        self.assertEqual([33], search["final_enemy_hp"])

    def test_weak_vulnerable_reconstruction_includes_existing_strength(self):
        """Strength belongs before the single Weak/Vulnerable rounding chain.

        This is the exact shape observed in the current canary: the bridge
        has already serialized ``floor((9 + 1) * .75) == 7`` for a weakened
        Pommel Strike, while the game applies target Vulnerable before the
        final integer truncation and deals eleven.
        """

        target = monster(
            "Chosen", 40,
            powers=[Power("Vulnerable", "Vulnerable", 1)],
        )
        pommel_strike = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=7, target=True,
        )
        pommel_strike.base_damage = 9
        game = GameStub([target], [pommel_strike], hp=30, energy=1, act=2)
        game.player.powers = [
            Power("Weak", "Weak", 1),
            Power("Strength", "Strength", 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, pommel_strike)
        self.assertEqual(
            [29], self.planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_paper_frog_and_slow_share_one_planner_rounding_chain(self):
        target = monster(
            "GiantHead",
            500,
            powers=[
                Power("Slow", "Slow", 3),
                Power("Vulnerable", "Vulnerable", 1),
            ],
        )
        bludgeon = card(
            "Bludgeon", CardType.ATTACK,
            cost=3, damage=34, target=True,
        )
        bludgeon.base_damage = 32
        game = GameStub([target], [bludgeon], hp=45, energy=3, act=3)
        game.player.powers = [Power("Strength", "Strength", 2)]
        game.relics = [Relic("Paper Frog", "Paper Frog")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(
            77,
            self.planner.last_decision["search"][
                "first_action_enemy_hp_loss"
            ],
        )

    def test_weak_paper_frog_and_slow_share_one_planner_rounding_chain(self):
        target = monster(
            "GiantHead",
            100,
            powers=[
                Power("Slow", "Slow", 3),
                Power("Vulnerable", "Vulnerable", 3),
            ],
        )
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=4, target=True,
        )
        strike.base_damage = 6
        game = GameStub([target], [strike], hp=45, energy=1, act=3)
        game.player.powers = [Power("Weakened", "Weakened", 1)]
        game.relics = [Relic("Paper Frog", "Paper Frog")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        # floor(6 * .75 * 1.75 * 1.3) == 10.
        self.assertEqual(
            10,
            self.planner.last_decision["search"][
                "first_action_enemy_hp_loss"
            ],
        )

    def test_weak_whirlwind_reconstructs_each_hit_in_planner(self):
        target = monster(
            "TheMaw", 100,
            powers=[Power("Vulnerable", "Vulnerable", 2)],
        )
        whirlwind = card(
            "Whirlwind", CardType.ATTACK,
            cost=-1, damage=7, upgrades=1,
        )
        whirlwind.base_damage = 8
        game = GameStub([target], [whirlwind], hp=45, energy=3, act=3)
        game.player.powers = [
            Power("Weakened", "Weakened", 2),
            Power("Strength", "Strength", 2),
        ]
        game.relics = [Relic("Paper Frog", "Paper Frog")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(
            39,
            self.planner.last_decision["search"][
                "first_action_enemy_hp_loss"
            ],
        )

    def test_boot_uses_each_live_hit_after_block_consumption(self):
        """Replay the two Boot packet shapes from the failed A0 attempt."""

        single = monster("ShelledParasite", 40)
        single.block = 11
        pommel = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=12, target=True,
        )
        pommel.base_damage = 9
        game = GameStub([single], [pommel], hp=30, energy=1, act=2)
        game.player.powers = [Power("Strength", "Strength", 3)]
        game.relics = [Relic("Boot", "Boot")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        # 12 into 11 Block leaves one positive unblocked hit, so The Boot
        # raises it to five rather than allowing the old one-damage result.
        self.assertEqual(
            [35], self.planner.last_decision["search"]["final_enemy_hp"]
        )

        multi = monster("BronzeAutomaton", 40)
        multi.block = 9
        boomerang = card(
            "Sword Boomerang", CardType.ATTACK,
            cost=1, damage=6, magic=3,
        )
        multi_game = GameStub(
            [multi], [boomerang], hp=30, energy=1, act=2
        )
        multi_game.relics = [Relic("Boot", "Boot")]

        multi_action = self.planner.choose_card_action(multi_game)

        self.assertIsInstance(multi_action, PlayCardAction)
        # 6 + 6 + 6 into nine Block resolves 0, 5, 6: the trace's observed
        # eleven HP loss, not the aggregate-block shortcut's nine.
        self.assertEqual(
            [29], self.planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_letter_opener_damage_respects_each_enemys_block(self):
        blocked = monster("FatGremlin", 13)
        blocked.block = 6
        open_enemy = monster("GremlinLeader", 140)
        defend = card(
            "Defend_R", CardType.SKILL, cost=1, block=5,
        )
        game = GameStub(
            [blocked, open_enemy], [defend], hp=30, energy=1, act=2
        )
        game.relics = [Relic("Letter Opener", "Letter Opener", counter=2)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)
        search = self.planner.last_decision["search"]
        self.assertEqual(5, search["first_action_enemy_hp_loss"])
        self.assertEqual([13, 135], search["final_enemy_hp"])
        self.assertEqual([1, 0], search["final_enemy_block"])

    def test_guaranteed_combo_first_damage_uses_strength_weak_vulnerable_chain(self):
        """The exact-lethal shortcut reports the same first packet as beam."""

        target = monster(
            "Chosen", 18,
            powers=[Power("Vulnerable", "Vulnerable", 1)],
        )
        pommel_strike = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=7, target=True,
        )
        pommel_strike.base_damage = 9
        strike_card = card(
            "Strike", CardType.ATTACK,
            cost=1, damage=5, target=True,
        )
        strike_card.base_damage = 6
        game = GameStub(
            [target], [pommel_strike, strike_card], hp=30, energy=2, act=2
        )
        game.player.powers = [
            Power("Weak", "Weak", 1),
            Power("Strength", "Strength", 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual(
            "guaranteed_attack_combo_lethal",
            self.planner.last_decision["reason"],
        )
        self.assertEqual(
            11, self.planner.last_decision["first_action_enemy_hp_loss"]
        )

    def test_malaise_strength_stacks_without_reapplying_existing_weak(self):
        parasite = monster(
            "ShelledParasite", 40,
            intent=Intent.ATTACK_BUFF, damage=7, hits=1,
            powers=[Power("Weak", "Weak", 1)],
        )
        parasite.move_base_damage = 10
        parasite.max_hp = 72
        malaise = card(
            "Malaise", CardType.SKILL,
            cost=-1, target=True,
        )
        game = GameStub(
            [parasite], [malaise], hp=30, energy=2, act=2
        )

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual([2], search["final_enemy_strength_reduction"])
        self.assertGreater(search["final_enemy_weak"][0], 1)
        self.assertEqual([6], search[
            "branch_enemy_attack_damage_per_hit"
        ])
        self.assertEqual(6, search["actual_loss"])
        self.assertEqual(6, search["enemy_attack_healing"])

    def test_fully_blocked_suck_heals_zero_before_thorns_kill(self):
        parasite = monster(
            "ShelledParasite",
            2,
            intent=Intent.ATTACK_BUFF,
            damage=10,
            hits=1,
        )
        parasite.max_hp = 72
        unavailable = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [parasite], [unavailable],
            hp=30, block=10, energy=0, act=2,
        )
        game.player.powers = [Power("Thorns", "Thorns", 3)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["actual_loss"])
        self.assertEqual(0, search["enemy_attack_healing_requested"])
        self.assertEqual(0, search["enemy_attack_healing"])
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertTrue(search["true_combat_end"])

    def test_suck_heal_then_thorns_kill_stops_remaining_hits(self):
        parasite = monster(
            "Shelled Parasite",
            1,
            intent=Intent.ATTACK_BUFF,
            damage=10,
            hits=2,
        )
        parasite.max_hp = 72
        unavailable = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [parasite], [unavailable],
            hp=30, block=8, energy=0, act=2,
        )
        game.player.powers = [Power("Thorns", "Thorns", 3)]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(2, search["actual_loss"])
        self.assertEqual(2, search["enemy_attack_healing"])
        self.assertEqual(1, search["enemy_attack_healing_events"])
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertEqual(
            [0], search["enemy_reaction_stopped_attacker_indexes"]
        )
        self.assertTrue(search["true_combat_end"])

    def test_suck_healing_is_capped_by_missing_enemy_hp(self):
        parasite = monster(
            "ShelledParasite",
            71,
            intent=Intent.ATTACK_BUFF,
            damage=10,
            hits=1,
        )
        parasite.max_hp = 72
        unavailable = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [parasite], [unavailable],
            hp=30, block=0, energy=0, act=2,
        )
        game.player.powers = [Power("Thorns", "Thorns", 3)]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(10, search["enemy_attack_healing_requested"])
        self.assertEqual(1, search["enemy_attack_healing"])
        self.assertEqual([69], search["final_enemy_hp"])
        self.assertFalse(search["true_combat_end"])

    def test_bronze_scales_does_not_fabricate_poison_kill_through_block(self):
        """Replay Silent F24: Spheric Guardian retained 17 Block."""

        guardian = monster(
            "SphericGuardian",
            20,
            intent=Intent.ATTACK_DEFEND,
            damage=7,
            hits=1,
            powers=[Power("Poison", "Poison", 9)],
        )
        guardian.block = 17
        catalyst = card(
            "Catalyst",
            CardType.SKILL,
            cost=1,
            magic=-1,
            target=True,
        )
        catalyst.exhausts = True
        game = GameStub(
            [guardian], [catalyst],
            hp=40, block=20, energy=1, act=2,
        )
        game.relics = [Relic("Bronze Scales", "Bronze Scales")]
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIs(action.card, catalyst)
        search = planner.last_decision["search"]
        self.assertEqual([2], search["final_enemy_hp"])
        self.assertEqual([29], search["final_enemy_block"])
        self.assertEqual(0, search["bronze_scales_reaction_damage"])
        self.assertFalse(search["true_combat_end"])

    def test_spheric_guardian_attack_defend_blocks_thorns_before_attack(self):
        """Replay 8ed147d8 F30 T4: 7 HP, Strike, then 15 Block."""

        guardian = monster(
            "SphericGuardian",
            7,
            intent=Intent.ATTACK_DEFEND,
            damage=10,
            hits=1,
            powers=[Power("Barricade", "Barricade", -1)],
        )
        strike_card = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [guardian], [strike_card],
            hp=32, block=3, energy=1, act=2,
        )
        game.player.powers = [Power("Thorns", "Thorns", 3)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, strike_card)
        search = self.planner.last_decision["search"]
        self.assertEqual([1], search["final_enemy_hp"])
        self.assertEqual([12], search["final_enemy_block"])
        self.assertEqual(0, search["player_thorns_reaction_damage"])
        self.assertFalse(search["true_combat_end"])

    def test_spheric_guardian_barricade_block_is_durable_progress(self):
        """Replay 05658fd1 F18 T41: 20 HP / 50 persistent Block."""

        guardian = monster(
            "SphericGuardian",
            20,
            intent=Intent.ATTACK_DEFEND,
            damage=7,
            hits=1,
            powers=[Power("Barricade", "Barricade", -1)],
        )
        guardian.block = 50
        defend = card(
            "Defend_G", CardType.SKILL, cost=1, block=5
        )
        strike = card(
            "Strike_G",
            CardType.ATTACK,
            cost=1,
            damage=6,
            target=True,
        )
        defend.uuid = "spheric-t41-defend"
        strike.uuid = "spheric-t41-strike"
        game = GameStub(
            [guardian], [defend, strike],
            hp=57, energy=2, act=2,
        )
        game.turn = 41
        planner = FastCombatPlanner(SilentPriority())

        first_action = planner.choose_card_action(game)

        self.assertIsInstance(first_action, PlayCardAction)
        self.assertIs(first_action.card, defend)
        self.assertEqual(
            ["Defend_G", "Strike_G"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        search = planner.last_decision["search"]
        self.assertEqual([20], search["final_enemy_hp"])
        self.assertEqual([44], search["final_enemy_block"])
        self.assertEqual(6, search["persistent_enemy_block_progress"])
        self.assertEqual(
            [6], search["persistent_enemy_block_progress_by_monster"]
        )
        self.assertLess(
            sum(search["final_enemy_hp"])
            + sum(search["final_enemy_block"]),
            guardian.current_hp + guardian.block,
        )

        # CommunicationMod returns an authoritative frame after Defend.  The
        # same planner must now emit the planned durability-progress attack,
        # rather than ending or starting another defense-only loop.
        game.hand = [strike]
        game.player.energy = 1
        game.player.block = 5
        second_action = planner.choose_card_action(game)

        self.assertIsInstance(second_action, PlayCardAction)
        self.assertIs(second_action.card, strike)
        self.assertIs(second_action.target_monster, guardian)
        self.assertEqual(
            6,
            planner.last_decision["search"][
                "persistent_enemy_block_progress"
            ],
        )

    def test_ordinary_long_boss_block_is_not_durable_progress(self):
        """Temporary Block on a normal long boss still expires for free."""

        champ = monster(
            "TheChamp",
            420,
            intent=Intent.ATTACK_DEFEND,
            damage=7,
            hits=1,
        )
        champ.block = 50
        defend = card(
            "Defend_G", CardType.SKILL, cost=1, block=5
        )
        strike = card(
            "Strike_G",
            CardType.ATTACK,
            cost=1,
            damage=6,
            target=True,
        )
        game = GameStub(
            [champ], [defend, strike],
            hp=57, energy=2, act=2,
        )
        game.room_type = "MonsterRoomBoss"
        game.turn = 41
        planner = FastCombatPlanner(SilentPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, defend)
        self.assertEqual(
            ["Defend_G"],
            [
                step["card_id"]
                for step in planner.last_decision["planned_sequence"]
            ],
        )
        search = planner.last_decision["search"]
        self.assertEqual([50], search["final_enemy_block"])
        self.assertEqual(0, search["persistent_enemy_block_progress"])
        self.assertEqual(
            [0], search["persistent_enemy_block_progress_by_monster"]
        )

    def test_shelled_parasite_plated_armor_is_durable_attack_progress(self):
        """A Strike must chip Plated Armor instead of ending with energy."""
        parasite = monster(
            "Shelled Parasite", 57,
            intent=Intent.ATTACK_DEBUFF, damage=18, hits=1,
            powers=[Power("Plated Armor", "Plated Armor", 13)],
        )
        parasite.block = 7
        strikes = [
            card(
                "Strike_B", CardType.ATTACK,
                cost=1, damage=6, target=True,
            )
            for _ in range(3)
        ]
        for index, strike in enumerate(strikes):
            strike.uuid = f"shelled-strike-{index}"
        game = GameStub(
            [parasite], strikes,
            hp=13, block=14, energy=1, act=2,
        )
        planner = FastCombatPlanner(DefectPowerPriority())

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIn(action.card, strikes)
        search = planner.last_decision["search"]
        self.assertEqual([1], search["final_enemy_block"])
        self.assertEqual(6, search["persistent_enemy_block_progress"])
        self.assertEqual([6], search[
            "persistent_enemy_block_progress_by_monster"
        ])

    def test_shelled_parasite_rebound_progress_is_not_discarded(self):
        """Replay the historical F39-style Rebound opportunity."""
        parasite = monster(
            "Shelled Parasite", 42,
            intent=Intent.ATTACK, damage=18, hits=1,
            powers=[Power("Plated Armor", "Plated Armor", 14)],
        )
        parasite.block = 14
        rebound = card(
            "Rebound", CardType.ATTACK,
            cost=1, damage=9, target=True,
        )
        defend = card(
            "Defend_B", CardType.SKILL,
            cost=1, block=8,
        )
        rebound.uuid = "shelled-rebound-historical"
        defend.uuid = "shelled-rebound-defend"
        game = GameStub(
            [parasite], [rebound, defend],
            hp=64, block=11, energy=2, act=2,
        )

        planner = FastCombatPlanner(DefectPowerPriority())
        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        # The correct first move is to cover the incoming hit and then spend
        # the remaining turn on Rebound; ending the turn was the historical
        # defect, not the choice between these two legal prefixes.
        self.assertIs(action.card, defend)
        self.assertEqual(
            ["Defend_B", "Rebound"],
            [step["card_id"] for step in planner.last_decision[
                "planned_sequence"
            ]],
        )
        search = planner.last_decision["search"]
        self.assertEqual([5], search["final_enemy_block"])
        self.assertEqual(9, search["persistent_enemy_block_progress"])

    def test_serialized_bronze_scales_thorns_is_not_counted_twice(self):
        """Replay Silent F30: Shelled Parasite took only six Thorns."""

        parasite = monster(
            "ShelledParasite", 10,
            intent=Intent.ATTACK, damage=5, hits=2,
        )
        unavailable = card(
            "Strike_G", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [parasite], [unavailable],
            hp=30, block=10, energy=0, act=2,
        )
        game.relics = [Relic("Bronze Scales", "Bronze Scales")]
        game.player.powers = [Power("Thorns", "Thorns", 3)]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual([4], search["final_enemy_hp"])
        self.assertEqual(2, search["enemy_reaction_damage_events"])
        self.assertEqual(0, search["bronze_scales_reaction_damage"])
        self.assertEqual(6, search["player_thorns_reaction_damage"])
        self.assertEqual(0, search["projected_loss"])
        self.assertFalse(search["true_combat_end"])

    def test_flame_barrier_and_bronze_are_separate_intangible_packets(self):
        attacker = monster(
            "Nemesis",
            3,
            intent=Intent.ATTACK,
            damage=5,
            hits=5,
            powers=[Power("Intangible", "Intangible", 1)],
        )
        flame_barrier = card(
            "Flame Barrier",
            CardType.SKILL,
            cost=2,
            block=12,
            magic=4,
        )
        game = GameStub(
            [attacker], [flame_barrier],
            hp=30, block=0, energy=2, act=3,
        )
        game.relics = [Relic("Bronze Scales", "Bronze Scales")]
        # CommunicationMod serializes Bronze Scales as ThornsPower.
        game.player.powers = [Power("Thorns", "Thorns", 3)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, flame_barrier)
        search = self.planner.last_decision["search"]
        self.assertEqual(3, search["player_thorns_power"])
        self.assertEqual(4, search["player_flame_barrier"])
        self.assertEqual(7, search["player_thorns"])
        self.assertEqual(3, search["enemy_reaction_damage_events"])
        self.assertEqual([0], search["final_enemy_hp"])
        # The first two 5-damage hits are fully covered by 12 Block. Two
        # separately Intangible-capped reaction packets kill on the second
        # hit, so no third hit leaks three HP through the remaining Block.
        self.assertEqual(0, search["projected_loss"])
        self.assertTrue(search["true_combat_end"])

    def test_flame_barrier_packet_observes_bronze_mode_shift_block(self):
        guardian = monster(
            "TheGuardian",
            30,
            intent=Intent.ATTACK,
            damage=5,
            hits=1,
            powers=[Power("ModeShiftPower", "Mode Shift", 3)],
        )
        unavailable = card(
            "Strike_R",
            CardType.ATTACK,
            cost=1,
            damage=6,
            target=True,
        )
        game = GameStub(
            [guardian], [unavailable],
            hp=30, block=10, energy=0, act=1,
        )
        game.relics = [Relic("Bronze Scales", "Bronze Scales")]
        game.player.powers = [
            Power("Thorns", "Thorns", 3),
            Power("FlameBarrierPower", "Flame Barrier", 4),
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        # Bronze's first three-point packet crosses Mode Shift and grants 20
        # Block immediately. Flame Barrier's later four-point packet consumes
        # that new Block instead of being merged into a seven-point HP hit.
        self.assertEqual([27], search["final_enemy_hp"])
        self.assertEqual([16], search["final_enemy_block"])
        self.assertEqual(2, search["enemy_reaction_damage_events"])
        self.assertEqual(3, search["player_thorns_reaction_damage"])
        self.assertEqual(0, search["projected_loss"])
        self.assertFalse(search["true_combat_end"])

    def test_played_flame_barrier_reflects_after_blocking_same_turn_hit(self):
        attacker = monster(
            "JawWorm", 4, intent=Intent.ATTACK, damage=10, hits=1
        )
        flame_barrier = card(
            "Flame Barrier", CardType.SKILL,
            cost=2, block=12, magic=4,
        )
        game = GameStub(
            [attacker], [flame_barrier], hp=20, energy=2, act=2
        )

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, flame_barrier)
        search = self.planner.last_decision["search"]
        self.assertEqual(4, search["player_thorns"])
        self.assertEqual(4, search["player_thorns_reaction_damage"])
        self.assertEqual(0, search["projected_loss"])
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertTrue(search["true_combat_end"])

    def test_flame_barrier_reflection_respects_enemy_intangible(self):
        attacker = monster(
            "Nemesis",
            3,
            intent=Intent.ATTACK,
            damage=5,
            hits=5,
            powers=[Power("Intangible", "Intangible", 1)],
        )
        unavailable = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [attacker], [unavailable],
            hp=30, block=25, energy=0, act=3,
        )
        game.player.powers = [
            Power("FlameBarrierPower", "Flame Barrier", 4)
        ]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(3, search["enemy_reaction_damage_events"])
        self.assertEqual(3, search["player_thorns_reaction_damage"])
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertTrue(search["true_combat_end"])

    def test_bronze_scales_never_claims_kill_through_invincible(self):
        heart = monster(
            "CorruptHeart",
            2,
            intent=Intent.ATTACK,
            damage=5,
            hits=3,
            powers=[Power("InvinciblePower", "Invincible", 200)],
        )
        unavailable = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [heart], [unavailable],
            hp=30, block=15, energy=0, act=4,
        )
        game.relics = [Relic("Bronze Scales", "Bronze Scales")]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(3, search["enemy_reaction_damage_events"])
        self.assertEqual(1, search["bronze_scales_reaction_damage"])
        self.assertEqual([1], search["final_enemy_hp"])
        self.assertFalse(search["true_combat_end"])
        self.assertEqual([], search[
            "enemy_reaction_stopped_attacker_indexes"
        ])

    def test_reactive_fungi_death_applies_spore_cloud_to_later_attack(self):
        fungi = monster(
            "FungiBeast",
            3,
            intent=Intent.ATTACK,
            damage=1,
            hits=1,
            powers=[Power("SporeCloudPower", "Spore Cloud", 2)],
        )
        cultist = monster(
            "Cultist",
            20,
            intent=Intent.ATTACK,
            damage=10,
            hits=1,
        )
        unavailable = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [fungi, cultist], [unavailable],
            hp=40, energy=0, act=1,
        )
        game.relics = [Relic("Bronze Scales", "Bronze Scales")]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(16, search["projected_loss"])
        self.assertEqual([0, 17], search["final_enemy_hp"])
        self.assertEqual(6, search["bronze_scales_reaction_damage"])

    def test_writhing_mass_attack_invalidates_intent_and_terminal_binding(self):
        mass = monster(
            "WrithingMass",
            12,
            intent=Intent.ATTACK,
            damage=38,
            hits=1,
            powers=[Power("Compulsive", "Compulsive", 1)],
        )
        first = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        first.uuid = "first-strike"
        second = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        second.uuid = "second-strike"
        game = GameStub([mass], [first, second], hp=70, energy=2, act=3)
        game.ascension_level = 20

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        search = self.planner.last_decision["search"]
        self.assertTrue(search["writhing_mass_replan_required"])
        self.assertTrue(search["writhing_mass_old_intent_ignored"])
        self.assertEqual([0], search[
            "writhing_mass_intent_unknown_indexes"
        ])
        self.assertEqual(
            "worst_case_not_predicted_intent",
            search["writhing_mass_risk_envelopes"][0]["policy"],
        )
        self.assertIsNone(self.planner._terminal_plan)

    def test_lethal_writhing_mass_attack_does_not_claim_compulsive_reroll(self):
        mass = monster(
            "WrithingMass",
            6,
            intent=Intent.ATTACK,
            damage=38,
            hits=1,
            powers=[Power("Compulsive", "Compulsive", 1)],
        )
        strike = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub([mass], [strike], hp=70, energy=1, act=3)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        search = self.planner.last_decision["search"]
        self.assertFalse(search["writhing_mass_replan_required"])
        self.assertFalse(search["writhing_mass_old_intent_ignored"])

    def test_low_hp_targetless_attack_does_not_reroll_writhing_mass_lethal(self):
        """F41 replay: a progress fallback must price the Compulsive reroll."""

        mass = monster(
            "WrithingMass",
            66,
            intent=Intent.ATTACK_DEBUFF,
            damage=15,
            hits=1,
            powers=[
                Power("Malleable", "Malleable", 3),
                Power("Vulnerable", "Vulnerable", 3),
                Power("Compulsive", "Compulsive", -1),
            ],
        )
        mass.block = 4
        thunderclap = card(
            "Thunderclap", CardType.ATTACK,
            cost=1, damage=6, target=False,
        )
        game = GameStub(
            [mass], [thunderclap], hp=8, block=23, energy=2, act=3
        )
        game.floor = 41
        game.player.max_hp = 80
        game.player.powers = [
            Power("Strength", "Strength", 5),
            Power("Vulnerable", "Vulnerable", 1),
            Power("Weakened", "Weakened", 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            "no_positive_marginal_action",
            self.planner.last_decision["reason"],
        )

    def test_writhing_mass_vulnerable_fallback_cannot_reroll_lethal(self):
        """Replay 470584: 1 HP/16 Block must not Bash a 12-hit into 32."""

        mass = monster(
            "WrithingMass", 68,
            intent=Intent.ATTACK_DEFEND, damage=12, hits=1,
            powers=[
                Power("Malleable", "Malleable", 3),
                Power("Compulsive", "Compulsive", -1),
            ],
        )
        bash = card(
            "Bash", CardType.ATTACK,
            cost=2, damage=26, target=True, magic=3, upgrades=1,
        )
        game = GameStub(
            [mass], [bash], hp=1, block=16, energy=2, act=3,
        )

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertNotEqual(
            "no_incoming_vulnerable_setup",
            self.planner.last_decision["reason"],
        )

    def test_writhing_mass_resource_rescue_cannot_reroll_lethal(self):
        """Replay 470578: Pommel draw value cannot bypass Compulsive risk."""

        mass = monster(
            "WrithingMass", 121,
            intent=Intent.ATTACK_DEFEND, damage=12, hits=1,
            powers=[Power("Compulsive", "Compulsive", -1)],
        )
        pommel = card(
            "Pommel Strike", CardType.ATTACK,
            cost=1, damage=21, target=True, magic=1,
        )
        game = GameStub(
            [mass], [pommel], hp=15, block=10, energy=3, act=3,
        )
        game.draw_pile = [
            card("Defend_R", CardType.SKILL, cost=1, block=5),
        ]

        rescue = self.planner._resource_generation_rescue(
            game, game.hand, total_loss=5, risk_budget=1,
        )

        self.assertIsNone(rescue)

    def test_grounding_byrd_suppresses_old_attack_and_thorns_kill(self):
        """Replay 469553: falling Byrd stuns instead of attacking Thorns."""

        byrd = monster(
            "Byrd", 12,
            intent=Intent.ATTACK, damage=2, hits=5,
            powers=[Power("Flight", "Flight", 1)],
        )
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub([byrd], [strike], hp=74, energy=1, act=2)
        game.player.powers = [Power("Thorns", "Thorns", 3)]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        search = self.planner.last_decision["search"]
        self.assertFalse(search["true_combat_end"])
        self.assertEqual([9], search["final_enemy_hp"])
        self.assertEqual([0], search["action_suppressed_enemy_indexes"])
        self.assertEqual(0, search["enemy_reaction_damage_events"])

    def test_multi_enemy_targetless_attack_does_not_fabricate_target_progress(self):
        """AOE/random attacks stay unbound while several enemies live."""

        first = monster(
            "WrithingMass", 66,
            intent=Intent.ATTACK_DEBUFF, damage=15, hits=1,
            powers=[Power("Malleable", "Malleable", 3)],
        )
        second = monster(
            "Cultist", 30,
            intent=Intent.ATTACK, damage=4, hits=1,
        )
        thunderclap = card(
            "Thunderclap", CardType.ATTACK,
            cost=1, damage=6, target=False,
        )
        game = GameStub(
            [first, second], [thunderclap],
            hp=40, block=30, energy=1, act=3,
        )

        with patch.object(self.planner, "_best_plan", return_value=(0.0, [])):
            action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertNotEqual(
            "attack_progress_fallback",
            self.planner.last_decision.get("reason"),
        )

    def test_new_noxious_fumes_does_not_tick_before_current_enemy_move(self):
        attacker = monster(
            "Cultist", 2, intent=Intent.ATTACK, damage=20, hits=1
        )
        fumes = card(
            "Noxious Fumes", CardType.POWER, cost=1, magic=2
        )
        game = GameStub([attacker], [fumes], hp=50, energy=1, act=2)

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, fumes)
        search = self.planner.last_decision["search"]
        self.assertFalse(search["true_combat_end"])
        self.assertEqual([2], search["final_enemy_hp"])
        self.assertEqual(20, search["projected_loss"])

    def test_existing_noxious_fumes_does_not_make_future_poison_immediate(self):
        attacker = monster(
            "Cultist", 9, intent=Intent.ATTACK, damage=11, hits=1,
            powers=[Power("Poison", "Poison", 8)],
        )
        game = GameStub([attacker], [], hp=30, energy=0, act=2)
        game.player.powers = [
            Power("NoxiousFumesPower", "Noxious Fumes", 3)
        ]

        self.assertIn(attacker, combat_predictor.active_monsters(game))
        self.assertIn(attacker, combat_predictor.attack_monsters(game))
        self.assertEqual(
            11,
            combat_predictor.projected_turn_outcome(game).attack_hp_loss,
        )

    def test_rupture_offering_strength_reaches_later_attack(self):
        enemy = monster("Cultist", 8)
        offering = card(
            "Offering", CardType.SKILL, cost=0, magic=3
        )
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        pain = card("Pain", CardType.CURSE, cost=-2, playable=False)
        game = GameStub(
            [enemy], [offering, strike, pain], hp=30, energy=0
        )
        game.player.powers = [Power("Rupture", "Rupture", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, offering)
        search = self.planner.last_decision["search"]
        # Pain triggers for both Offering and the later Strike; only the two
        # Offering-phase losses can affect that already-calculated Strike.
        self.assertEqual(3, search["rupture_triggers"])
        self.assertEqual(3, search["rupture_strength_gained"])
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertTrue(search["true_combat_end"])

    def test_rupture_bloodletting_strength_reaches_later_attack(self):
        enemy = monster("Cultist", 7)
        bloodletting = card("Bloodletting", CardType.SKILL, cost=0)
        strike = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [enemy], [bloodletting, strike], hp=30, energy=0
        )
        game.player.powers = [Power("Rupture", "Rupture", 1)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, bloodletting)
        search = self.planner.last_decision["search"]
        self.assertEqual(1, search["rupture_triggers"])
        self.assertEqual([0], search["final_enemy_hp"])

    def test_rupture_requires_real_hp_loss_after_buffer_and_tungsten(self):
        enemy = monster("Cultist", 20, intent=Intent.ATTACK, damage=1, hits=1)
        unavailable = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        buffered = GameStub([enemy], [unavailable], hp=30, energy=0)
        buffered.player.powers = [
            Power("Rupture", "Rupture", 1),
            Power("Combust", "Combust", 5, misc=1),
            Power("Buffer", "Buffer", 1),
        ]
        tungsten = GameStub([enemy], [unavailable], hp=30, energy=0)
        tungsten.player.powers = [
            Power("Rupture", "Rupture", 1),
            Power("Combust", "Combust", 5, misc=1),
        ]
        tungsten.relics = [Relic("Tungsten Rod", "Tungsten Rod")]
        normal = GameStub([enemy], [unavailable], hp=30, energy=0)
        normal.player.powers = [
            Power("Rupture", "Rupture", 2),
            Power("Combust", "Combust", 5, misc=1),
        ]

        buffered_search = FastCombatPlanner(PrioritiesStub())
        buffered_search.choose_card_action(buffered)
        tungsten_planner = FastCombatPlanner(PrioritiesStub())
        tungsten_planner.choose_card_action(tungsten)
        normal_planner = FastCombatPlanner(PrioritiesStub())
        normal_planner.choose_card_action(normal)

        self.assertEqual(
            0, buffered_search.last_decision["search"]["rupture_triggers"]
        )
        self.assertEqual(
            0, tungsten_planner.last_decision["search"]["rupture_triggers"]
        )
        self.assertEqual(
            1, normal_planner.last_decision["search"]["rupture_triggers"]
        )
        self.assertEqual(
            2,
            normal_planner.last_decision["search"][
                "delayed_rupture_strength"
            ],
        )

    def test_lock_on_applies_to_overflow_and_end_lightning_not_attack(self):
        target = monster(
            "Cultist",
            26,
            intent=Intent.ATTACK,
            damage=20,
            hits=1,
            powers=[Power("Lockon", "Lock-On", 2)],
        )
        ball = card(
            "Ball Lightning", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub([target], [ball], hp=30, energy=1, act=2)
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Plasma", "Plasma", 2, 1),
            Orb("Plasma", "Plasma", 2, 1),
        ]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, ball)
        search = self.planner.last_decision["search"]
        # Attack stays 6; the overflow evoke is 8*1.5=12 and the newly
        # channelled passive is 3*1.5=4 (integer truncation).
        self.assertEqual([4], search["final_enemy_hp"])

    def test_lock_on_never_amplifies_attack_or_poison_packets(self):
        target = monster(
            "Cultist",
            30,
            powers=[
                Power("Lockon", "Lock-On", 2),
                Power("Poison", "Poison", 4),
            ],
        )

        self.assertEqual(6, combat_predictor.attack_hp_loss(target, 6))
        self.assertEqual(4, combat_predictor.poison_damage_before_action(target))
        self.assertEqual(
            9, combat_predictor.orb_damage_after_lock_on(target, 6)
        )

    def test_lock_on_electrodynamics_is_target_local(self):
        locked = monster(
            "Cultist", 20,
            powers=[Power("Lockon", "Lock-On", 2)],
        )
        plain = monster("JawWorm", 20)
        dualcast = card("Dualcast", CardType.SKILL, cost=1)
        game = GameStub(
            [locked, plain], [dualcast], hp=30, energy=1, act=2
        )
        game.player.powers = [
            Power("Electrodynamics", "Electrodynamics", 1)
        ]
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 3)]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual([0, 4], search["final_enemy_hp"])

    def test_played_lock_on_buffs_later_explicit_evoke_only(self):
        target = monster("Cultist", 31)
        lock_on = card(
            "Lockon", CardType.ATTACK,
            cost=1, damage=8, magic=2, target=True,
        )
        dualcast = card("Dualcast", CardType.SKILL, cost=1)
        game = GameStub(
            [target], [lock_on, dualcast], hp=30, energy=2, act=2
        )
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 3)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, lock_on)
        search = self.planner.last_decision["search"]
        self.assertEqual([0], search["final_enemy_hp"])
        self.assertEqual([2], search["final_enemy_lock_on"])
        self.assertTrue(search["true_combat_end"])

    def test_hex_tracks_random_draw_pile_dazed_and_forces_replan(self):
        attacker = monster(
            "Chosen", 50, intent=Intent.ATTACK, damage=10, hits=1
        )
        defend = card("Defend_R", CardType.SKILL, cost=1, block=5)
        game = GameStub([attacker], [defend], hp=30, energy=1, act=2)
        game.player.powers = [Power("Hex", "Hex", 2)]
        game.draw_pile = [object() for _ in range(6)]

        action = self.planner.choose_card_action(game)

        self.assertIs(action.card, defend)
        search = self.planner.last_decision["search"]
        self.assertEqual(2, search["generated_dazed"])
        self.assertTrue(search["future_draw_pile_unknown"])
        self.assertEqual(-1, search["remaining_draw_pile_size"])
        self.assertEqual(3.0, search["status_deck_penalty"])
        self.assertIsNone(self.planner._terminal_plan)

    def test_painful_stabs_adds_one_wound_per_actual_damaging_hit(self):
        book = monster(
            "BookOfStabbing",
            100,
            intent=Intent.ATTACK,
            damage=2,
            hits=3,
            powers=[Power("Painful Stabs", "Painful Stabs", -1)],
        )
        unavailable = card(
            "Strike_R", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [book], [unavailable], hp=30, block=2, energy=0, act=2
        )

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(2, search["generated_wounds"])
        self.assertEqual(4.0, search["status_deck_penalty"])

        tungsten = GameStub(
            [monster(
                "BookOfStabbing", 100,
                intent=Intent.ATTACK, damage=1, hits=3,
                powers=[Power("Painful Stabs", "Painful Stabs", -1)],
            )],
            [unavailable], hp=30, energy=0, act=2,
        )
        tungsten.relics = [Relic("Tungsten Rod", "Tungsten Rod")]
        tungsten_planner = FastCombatPlanner(PrioritiesStub())
        tungsten_planner.choose_card_action(tungsten)
        self.assertEqual(
            0,
            tungsten_planner.last_decision["search"]["generated_wounds"],
        )

    def test_centurion_healer_support_breaks_stale_centurion_focus(self):
        centurion = monster(
            "Centurion", 41, intent=Intent.ATTACK, damage=14, hits=1
        )
        centurion.max_hp = 78
        healer = monster("Healer", 40, intent=Intent.BUFF)
        healer.max_hp = 48
        cold_snap = card(
            "Cold Snap", CardType.ATTACK,
            cost=1, damage=9, target=True,
        )
        game = GameStub(
            [centurion, healer], [cold_snap], hp=58, energy=1, act=2
        )
        planner = FastCombatPlanner(DefectPowerPriority())
        planner.combat_key = planner._current_combat_key(game)
        planner.focus_key = ("centurion", 0)

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, cold_snap)
        self.assertIs(action.target_monster, healer)
        self.assertEqual(("healer", 1), planner.focus_key)

    def test_centurion_exact_combo_kill_precedes_healer_priority(self):
        centurion = monster(
            "Centurion", 10, intent=Intent.ATTACK, damage=14, hits=1
        )
        centurion.max_hp = 78
        healer = monster("Healer", 40, intent=Intent.BUFF)
        healer.max_hp = 48
        first = card(
            "Strike_B", CardType.ATTACK,
            cost=1, damage=5, target=True,
        )
        second = card(
            "Cold Snap", CardType.ATTACK,
            cost=1, damage=5, target=True,
        )
        game = GameStub(
            [centurion, healer], [first, second],
            hp=58, energy=2, act=2,
        )
        planner = FastCombatPlanner(DefectPowerPriority())
        planner.combat_key = planner._current_combat_key(game)
        planner.focus_key = ("centurion", 0)

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, centurion)
        self.assertEqual(
            "guaranteed_attack_combo_lethal",
            planner.last_decision["reason"],
        )

    def test_lethal_centurion_turn_still_focuses_healer_without_a_kill(self):
        centurion = monster(
            "Centurion", 44, intent=Intent.ATTACK, damage=14, hits=1
        )
        centurion.max_hp = 78
        healer = monster(
            "Healer", 32,
            intent=Intent.ATTACK_DEBUFF, damage=10, hits=1,
        )
        healer.max_hp = 48
        healer.block = 15
        compile_driver = card(
            "Compile Driver", CardType.ATTACK,
            cost=1, damage=7, target=True,
        )
        ball_lightning = card(
            "Ball Lightning", CardType.ATTACK,
            cost=1, damage=7, target=True,
        )
        cold_snap = card(
            "Cold Snap", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub(
            [centurion, healer],
            [compile_driver, ball_lightning, cold_snap],
            hp=13, energy=3, act=2,
        )
        planner = FastCombatPlanner(DefectPowerPriority())
        planner.combat_key = planner._current_combat_key(game)
        planner.focus_key = ("centurion", 0)

        action = planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.target_monster, healer)
        self.assertEqual(("healer", 1), planner.focus_key)
        targeted_steps = [
            step["target_key"]
            for step in planner.last_decision["planned_sequence"]
            if step["target_key"] is not None
        ]
        self.assertTrue(targeted_steps)
        self.assertTrue(
            all(step == ["healer", 1] for step in targeted_steps)
        )

    def test_lethal_centurion_single_card_kill_remains_control_target(self):
        centurion = monster(
            "Centurion", 9, intent=Intent.ATTACK, damage=60, hits=1
        )
        centurion.max_hp = 78
        healer = monster("Healer", 40, intent=Intent.BUFF)
        healer.max_hp = 48
        cold_snap = card(
            "Cold Snap", CardType.ATTACK,
            cost=1, damage=9, target=True,
        )
        game = GameStub(
            [centurion, healer], [cold_snap],
            hp=20, energy=1, act=2,
        )
        planner = FastCombatPlanner(DefectPowerPriority())
        planner.combat_key = planner._current_combat_key(game)
        planner.focus_key = ("healer", 1)

        target = planner._prepare_focus(game, [centurion, healer])

        self.assertIs(target, centurion)
        self.assertEqual(("centurion", 0), planner.focus_key)

    def test_lethal_healer_attack_does_not_switch_to_killable_idle_centurion(self):
        centurion = monster(
            "Centurion", 9, intent=Intent.DEFEND
        )
        centurion.max_hp = 78
        healer = monster(
            "Healer", 40,
            intent=Intent.ATTACK_DEBUFF, damage=60, hits=1,
        )
        healer.max_hp = 48
        cold_snap = card(
            "Cold Snap", CardType.ATTACK,
            cost=1, damage=9, target=True,
        )
        game = GameStub(
            [centurion, healer], [cold_snap],
            hp=20, energy=1, act=2,
        )
        planner = FastCombatPlanner(DefectPowerPriority())
        planner.combat_key = planner._current_combat_key(game)
        planner.focus_key = ("healer", 1)

        target = planner._prepare_focus(game, [centurion, healer])

        self.assertIs(target, healer)
        self.assertEqual(("healer", 1), planner.focus_key)


if __name__ == "__main__":
    unittest.main()
