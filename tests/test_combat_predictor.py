import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai import combat_predictor
from spirecomm.spire.character import Intent, Monster, Orb, Player
from spirecomm.spire.power import Power
from spirecomm.spire.relic import Relic
from spirecomm.spire.card import Card, CardRarity, CardType
from spirecomm.spire.potion import Potion


def power(power_id, amount, damage=0):
    return Power(power_id, power_id, amount, damage=damage)


def monster(name, hp, damage=0, hits=1, block=0, powers=None, index=0):
    item = Monster(
        name=name,
        monster_id=name,
        max_hp=hp,
        current_hp=hp,
        block=block,
        intent=Intent.ATTACK if damage else Intent.BUFF,
        half_dead=False,
        is_gone=False,
        move_adjusted_damage=damage,
        move_hits=hits,
    )
    item.powers = list(powers or [])
    item.monster_index = index
    return item


class GameStub:
    def __init__(self, monsters, hp=70, block=0, powers=None, orbs=None, hand=None, relics=None):
        self.monsters = monsters
        self.player = Player(70, hp, block, energy=3)
        self.player.powers = list(powers or [])
        self.player.orbs = list(orbs or [])
        self.hand = list(hand or [])
        self.relics = list(relics or [])
        self.potions = []


class CombatProjectionTests(unittest.TestCase):
    def test_acid_slime_next_attack_bound_respects_a0_tackle_lockout(self):
        tackle = monster("AcidSlime_M", 29, damage=10)
        spit = monster("AcidSlime_M", 29, damage=7)
        spit.intent = Intent.ATTACK_DEBUFF
        lick = monster("AcidSlime_M", 29)
        lick.intent = Intent.DEBUFF
        game = GameStub([tackle, spit, lick])
        game.ascension_level = 0

        self.assertEqual(
            7, combat_predictor.next_turn_attack_upper_bound(game, tackle)
        )
        self.assertEqual(
            10, combat_predictor.next_turn_attack_upper_bound(game, spit)
        )
        self.assertEqual(
            10, combat_predictor.next_turn_attack_upper_bound(game, lick)
        )

        game.ascension_level = 17
        self.assertEqual(
            12, combat_predictor.next_turn_attack_upper_bound(game, tackle)
        )
        self.assertEqual(
            0,
            combat_predictor.next_turn_attack_upper_bound(
                game, monster("Cultist", 50, damage=6)
            ),
        )

    def test_runic_cube_draws_once_per_confirmed_hp_loss_event(self):
        game = GameStub([], relics=[Relic("Runic Cube", "Runic Cube")])

        self.assertEqual(3, combat_predictor.runic_cube_draw(game, 3))
        self.assertEqual(0, combat_predictor.runic_cube_draw(game, 0))
        self.assertEqual(
            0,
            combat_predictor.runic_cube_draw(GameStub([]), 3),
        )

    def test_relic_model_coverage_exposes_unclassified_effects(self):
        game = GameStub([], relics=[
            Relic("Orichalcum", "Orichalcum"),
            Relic("Unknown Combat Relic", "Unknown Combat Relic"),
        ])

        coverage = combat_predictor.relic_model_coverage(game)

        self.assertIn("orichalcum", coverage["modeled_relic_ids"])
        self.assertIn(
            "unknown combat relic", coverage["unclassified_relic_ids"]
        )
        reflected = combat_predictor.relic_model_coverage(
            GameStub([], relics=[Relic("ClockworkSouvenir", "ClockworkSouvenir")])
        )
        self.assertIn(
            "clockworksouvenir", reflected["state_reflected_relic_ids"]
        )
        reflected = combat_predictor.relic_model_coverage(
            GameStub([], relics=[
                Relic("CrackedCore", "CrackedCore"),
                Relic("Philosopher's Stone", "Philosopher's Stone"),
                Relic("InkBottle", "InkBottle"),
            ])
        )
        self.assertIn("crackedcore", reflected["state_reflected_relic_ids"])
        self.assertIn(
            "philosopher's stone", reflected["state_reflected_relic_ids"]
        )
        emotion_coverage = combat_predictor.relic_model_coverage(
            GameStub([], relics=[Relic("Emotion Chip", "Emotion Chip")])
        )
        self.assertIn(
            "emotion chip", emotion_coverage["state_reflected_relic_ids"]
        )
        self.assertIn("inkbottle", reflected["modeled_relic_ids"])
        compact = combat_predictor.relic_model_coverage(
            GameStub([], relics=[
                Relic("TungstenRod", "TungstenRod"),
                Relic("FrozenCore", "FrozenCore"),
                Relic("Vajra", "Vajra"),
                Relic("MutagenicStrength", "MutagenicStrength"),
                Relic("OddlySmoothStone", "OddlySmoothStone"),
                Relic("Lantern", "Lantern"),
                Relic("NeowsBlessing", "NeowsBlessing"),
                Relic("LizardTail", "LizardTail"),
                Relic("Oddly Smooth Stone", "Oddly Smooth Stone"),
                Relic("Potion Belt", "Potion Belt"),
                Relic("PreservedInsect", "PreservedInsect"),
                Relic("Whetstone", "Whetstone"),
                Relic("FossilizedHelix", "FossilizedHelix"),
                Relic("Smiling Mask", "Smiling Mask"),
                Relic("Strawberry", "Strawberry"),
                Relic("Astrolabe", "Astrolabe"),
                Relic("Bottled Flame", "Bottled Flame"),
                Relic("Bottled Lightning", "Bottled Lightning"),
                Relic("Bag of Preparation", "Bag of Preparation"),
                Relic("Anchor", "Anchor"),
                Relic("Girya", "Girya"),
                Relic("Ancient Tea Set", "Ancient Tea Set"),
                Relic("Happy Flower", "Happy Flower"),
                Relic("Snecko Eye", "Snecko Eye"),
                Relic("Lee's Waffle", "Lee's Waffle"),
                Relic("Ring of the Serpent", "Ring of the Serpent"),
            ])
        )
        self.assertIn("tungstenrod", compact["modeled_relic_ids"])
        self.assertIn("frozencore", compact["modeled_relic_ids"])
        self.assertIn("vajra", compact["state_reflected_relic_ids"])
        self.assertIn("oddlysmoothstone", compact["state_reflected_relic_ids"])
        self.assertIn("oddly smooth stone", compact["state_reflected_relic_ids"])
        self.assertIn("neowsblessing", compact["noncombat_relic_ids"])
        self.assertIn("potion belt", compact["noncombat_relic_ids"])
        self.assertIn(
            "preservedinsect", compact["state_reflected_relic_ids"]
        )
        self.assertIn("whetstone", compact["state_reflected_relic_ids"])
        self.assertIn(
            "fossilizedhelix", compact["state_reflected_relic_ids"]
        )
        self.assertIn("smiling mask", compact["state_reflected_relic_ids"])
        self.assertIn("strawberry", compact["state_reflected_relic_ids"])
        self.assertIn("lee's waffle", compact["state_reflected_relic_ids"])
        self.assertIn("ring of the serpent", compact["state_reflected_relic_ids"])
        self.assertIn("astrolabe", compact["state_reflected_relic_ids"])
        self.assertIn("bottled flame", compact["state_reflected_relic_ids"])
        self.assertIn(
            "bottled lightning", compact["state_reflected_relic_ids"]
        )
        self.assertIn(
            "bag of preparation", compact["state_reflected_relic_ids"]
        )
        self.assertIn("anchor", compact["state_reflected_relic_ids"])
        self.assertIn("girya", compact["state_reflected_relic_ids"])
        self.assertIn("ancient tea set", compact["state_reflected_relic_ids"])
        self.assertIn("happy flower", compact["state_reflected_relic_ids"])
        self.assertIn("snecko eye", compact["state_reflected_relic_ids"])

    def test_exact_relic_coverage_requires_a_declared_handler(self):
        violations = combat_predictor.relic_coverage_contract_violations()

        self.assertEqual([], violations["overlaps"])
        self.assertEqual([], violations["exact_without_handler"])
        self.assertEqual(
            combat_predictor.EXACT_BRANCH_COMBAT_RELIC_IDS,
            combat_predictor.MODELED_COMBAT_RELIC_IDS,
        )
        self.assertEqual(
            combat_predictor.EXACT_BRANCH_COMBAT_RELIC_IDS,
            frozenset(combat_predictor.EXACT_BRANCH_RELIC_HANDLERS),
        )
        self.assertTrue(all(
            str(handler).strip()
            for handler in
            combat_predictor.EXACT_BRANCH_RELIC_HANDLERS.values()
        ))

    def test_observable_relic_handlers_require_real_audit_contracts(self):
        coverage = combat_predictor.relic_model_coverage(
            GameStub([], relics=[
                Relic("BlackStar", "Black Star"),
                Relic("BustedCrown", "Busted Crown"),
                Relic("Omamori", "Omamori"),
            ])
        )

        self.assertEqual(
            ["blackstar", "bustedcrown", "omamori"],
            coverage["noncombat_relic_ids"],
        )
        self.assertEqual(
            {
                "blackstar": (
                    "elite_combat_reward_contains_two_stably_bound_relics"
                ),
                "bustedcrown": "standard_card_reward_count_minus_two",
                "omamori": "prevent_curse_gain_and_consume_one_counter",
            },
            coverage["observable_noncombat_handlers"],
        )
        self.assertEqual([], coverage["unclassified_relic_ids"])

    def test_tingsha_is_registered_as_partial_combat_coverage(self):
        coverage = combat_predictor.relic_model_coverage(
            GameStub([], relics=[Relic("Tingsha", "Tingsha")])
        )

        self.assertEqual(["tingsha"], coverage["heuristic_relic_ids"])
        self.assertEqual([], coverage["unsupported_relic_ids"])
        self.assertEqual([], coverage["unclassified_relic_ids"])
        violations = combat_predictor.relic_coverage_contract_violations()
        self.assertEqual([], violations["observable_noncombat_without_handler"])
        self.assertEqual([], violations["observable_noncombat_not_registered"])
        self.assertEqual(
            "elite_combat_reward_contains_two_stably_bound_relics",
            combat_predictor.OBSERVABLE_NONCOMBAT_RELIC_HANDLERS[
                "black star"
            ],
        )
        self.assertNotIn(
            "blackstar", combat_predictor.EXACT_BRANCH_COMBAT_RELIC_IDS
        )

        original = combat_predictor.OBSERVABLE_NONCOMBAT_RELIC_HANDLERS[
            "omamori"
        ]
        try:
            combat_predictor.OBSERVABLE_NONCOMBAT_RELIC_HANDLERS["omamori"] = ""
            self.assertEqual(
                ["omamori"],
                combat_predictor.relic_coverage_contract_violations()[
                    "observable_noncombat_without_handler"
                ],
            )
        finally:
            combat_predictor.OBSERVABLE_NONCOMBAT_RELIC_HANDLERS[
                "omamori"
            ] = original

    def test_the_specimen_is_conservative_heuristic_coverage(self):
        game = GameStub([], relics=[Relic("The Specimen", "The Specimen")])

        model = combat_predictor.relic_model_coverage(game)
        strategy = combat_predictor.relic_strategy_coverage(game)

        self.assertEqual(["the specimen"], model["heuristic_relic_ids"])
        self.assertEqual([], model["unsupported_relic_ids"])
        self.assertEqual([], model["unclassified_relic_ids"])
        self.assertEqual(["the specimen"], strategy["heuristic_relic_ids"])
        self.assertEqual([], strategy["unsupported_relic_ids"])
        self.assertEqual([], strategy["unclassified_relic_ids"])

    def test_partial_relics_are_not_reported_as_exact_branch_coverage(self):
        game = GameStub([], relics=[
            Relic("Mummified Hand", "Mummified Hand"),
            Relic("Sundial", "Sundial"),
            Relic("Ice Cream", "Ice Cream"),
            Relic("Self Forming Clay", "Self Forming Clay"),
            Relic("Sacred Bark", "Sacred Bark"),
            Relic("Centennial Puzzle", "Centennial Puzzle"),
        ])

        coverage = combat_predictor.relic_model_coverage(game)

        self.assertEqual(2, coverage["coverage_contract_version"])
        self.assertEqual(["sundial"], coverage["exact_branch_relic_ids"])
        self.assertEqual(["sundial"], coverage["modeled_relic_ids"])
        self.assertEqual(
            [
                "centennial puzzle", "ice cream", "mummified hand",
                "sacred bark", "self forming clay",
            ],
            coverage["heuristic_relic_ids"],
        )
        self.assertEqual([], coverage["unsupported_relic_ids"])
        strategy = combat_predictor.relic_strategy_coverage(game)
        self.assertIn(
            "mummified hand", strategy["decision_modeled_relic_ids"]
        )
        self.assertEqual(
            "random_hand_cost_zero_after_power",
            strategy["decision_handlers"]["mummified hand"],
        )
        self.assertEqual([], coverage["unclassified_relic_ids"])

    def test_state_reflected_relic_does_not_claim_exact_branch_handler(self):
        coverage = combat_predictor.relic_model_coverage(
            GameStub([], relics=[
                Relic("Mercury Hourglass", "Mercury Hourglass"),
                Relic("Nuclear Battery", "Nuclear Battery"),
                Relic("Runic Pyramid", "Runic Pyramid"),
            ])
        )

        self.assertEqual([], coverage["exact_branch_relic_ids"])
        self.assertEqual(
            ["mercury hourglass", "nuclear battery", "runic pyramid"],
            coverage["state_reflected_relic_ids"],
        )

    def test_eternal_feather_is_registered_as_noncombat_route_relic(self):
        coverage = combat_predictor.relic_model_coverage(
            GameStub([], relics=[Relic("Eternal Feather", "Eternal Feather")])
        )

        self.assertEqual(["eternal feather"], coverage["noncombat_relic_ids"])
        self.assertEqual([], coverage["unclassified_relic_ids"])

    def test_membership_card_is_registered_as_noncombat_shop_relic(self):
        game = GameStub(
            [], relics=[Relic("Membership Card", "Membership Card")]
        )

        for coverage in (
            combat_predictor.relic_model_coverage(game),
            combat_predictor.relic_strategy_coverage(game),
        ):
            self.assertEqual(
                ["membership card"], coverage["noncombat_relic_ids"]
            )
            self.assertEqual([], coverage["unclassified_relic_ids"])

    def test_power_coverage_contract_requires_role_specific_exact_handlers(self):
        violations = combat_predictor.power_coverage_contract_violations()

        self.assertEqual([], violations["overlaps"])
        self.assertEqual([], violations["exact_without_handler"])
        for role in ("player", "monster"):
            self.assertTrue(all(
                str(handler).strip()
                for handler in combat_predictor.EXACT_BRANCH_POWER_HANDLERS[
                    role
                ].values()
            ))

        # Existing player Vulnerable is reflected in serialized enemy intent,
        # but monster Vulnerable is applied target-locally by the attack path.
        player_vulnerable = combat_predictor.classify_active_power(
            {"id": "Vulnerable", "name": "Vulnerable", "amount": 1},
            "player",
        )
        monster_vulnerable = combat_predictor.classify_active_power(
            {"id": "Vulnerable", "name": "Vulnerable", "amount": 1},
            "monster",
        )
        self.assertEqual("state_reflected", player_vulnerable["category"])
        self.assertEqual("exact_branch", monster_vulnerable["category"])
        self.assertTrue(monster_vulnerable["handler"])
        noxious = combat_predictor.classify_active_power(
            {
                "id": "Noxious Fumes", "name": "Noxious Fumes",
                "amount": 2,
            },
            "player",
        )
        self.assertEqual("state_reflected", noxious["category"])
        self.assertIsNone(noxious["handler"])
        demon_form = combat_predictor.classify_active_power(
            {
                "id": "DemonFormPower", "name": "Demon Form",
                "amount": 2,
            },
            "player",
        )
        self.assertEqual("heuristic", demon_form["category"])
        self.assertIsNone(demon_form["handler"])
        infinite_blades = combat_predictor.classify_active_power(
            {
                "id": "InfiniteBladesPower", "name": "localized display",
                "amount": 1,
            },
            "player",
        )
        self.assertEqual("heuristic", infinite_blades["category"])
        self.assertIsNone(infinite_blades["handler"])
        self.assertNotIn(
            "infiniteblades",
            combat_predictor.UNSUPPORTED_COMBAT_POWER_IDS["player"],
        )
        coverage = combat_predictor.power_model_coverage({
            "combat_state": {
                "player": {"powers": [{
                    "id": "InfiniteBladesPower",
                    "name": "localized display",
                    "amount": 1,
                }]},
                "monsters": [],
            },
        })
        self.assertEqual(
            ["infinitebladespower"], coverage["heuristic_power_ids"]
        )
        self.assertEqual([], coverage["unsupported_power_ids"])

    def test_stasis_is_observable_only_with_uuid_lifecycle_handler(self):
        classified = combat_predictor.classify_active_power(
            {
                "id": "Stasis", "name": "Stasis", "amount": -1,
                "card": {
                    "id": "Demon Form", "card_instance_id": "held-rare",
                },
            },
            "monster",
        )

        self.assertEqual("state_reflected", classified["category"])
        self.assertEqual(
            "bronze_orb_stasis_remove_card_then_return_to_hand_on_owner_death",
            classified["handler"],
        )
        self.assertNotIn(
            "stasis",
            combat_predictor.UNSUPPORTED_COMBAT_POWER_IDS["monster"],
        )
        violations = combat_predictor.power_coverage_contract_violations()
        self.assertEqual([], violations["observable_state_without_handler"])
        self.assertEqual([], violations["observable_state_not_registered"])

        original = combat_predictor.OBSERVABLE_STATE_POWER_HANDLERS[
            "monster"
        ]["stasis"]
        try:
            combat_predictor.OBSERVABLE_STATE_POWER_HANDLERS["monster"][
                "stasis"
            ] = ""
            self.assertIn(
                ("monster", "stasis"),
                combat_predictor.power_coverage_contract_violations()[
                    "observable_state_without_handler"
                ],
            )
        finally:
            combat_predictor.OBSERVABLE_STATE_POWER_HANDLERS["monster"][
                "stasis"
            ] = original

    def test_power_coverage_records_raw_owner_ids_and_known_modeling_gaps(self):
        raw_game = {
            "combat_state": {
                "player": {"powers": [
                    {"id": "StaticDischarge", "name": "Static Discharge", "amount": 1},
                    {"id": "Hex", "name": "Hex", "amount": 1},
                    {"id": "Juggernaut", "name": "Juggernaut", "amount": 5},
                    {"id": "MysteryPower", "name": "Mystery Power", "amount": 2},
                ]},
                "monsters": [{
                    "enemy_instance_id": "enemy:snake",
                    "monster_index": 0,
                    "powers": [
                        {"id": "Painful Stabs", "name": "Painful Stabs", "amount": 1},
                        {"id": "Compulsive", "name": "Reactive", "amount": 1},
                        {"id": "Shifting", "name": "Shifting", "amount": 1},
                    ],
                }],
            },
        }

        coverage = combat_predictor.power_model_coverage(raw_game)
        entries = {
            (entry["owner_role"], entry["raw_id"]): entry
            for entry in coverage["active_powers"]
        }

        self.assertEqual(1, coverage["coverage_contract_version"])
        self.assertEqual(
            "exact_branch",
            entries[("player", "StaticDischarge")]["category"],
        )
        self.assertTrue(entries[("player", "StaticDischarge")]["handler"])
        self.assertEqual(
            "exact_branch", entries[("player", "Hex")]["category"]
        )
        self.assertTrue(entries[("player", "Hex")]["handler"])
        self.assertEqual(
            "heuristic", entries[("player", "Juggernaut")]["category"]
        )
        self.assertNotIn(
            "juggernaut",
            combat_predictor.UNSUPPORTED_COMBAT_POWER_IDS["player"],
        )
        self.assertIn(
            "juggernaut",
            combat_predictor.HEURISTIC_COMBAT_POWER_IDS["player"],
        )
        self.assertEqual(
            "exact_branch",
            entries[("monster", "Painful Stabs")]["category"],
        )
        self.assertTrue(entries[("monster", "Painful Stabs")]["handler"])
        for power_id in ("Compulsive", "Shifting"):
            self.assertEqual(
                "heuristic", entries[("monster", power_id)]["category"]
            )
            self.assertEqual(
                "enemy:snake",
                entries[("monster", power_id)]["owner_instance_id"],
            )
        self.assertEqual(
            "unclassified",
            entries[("player", "MysteryPower")]["category"],
        )

        for power_id in ("Choke", "Choked", "ChokedPower"):
            with self.subTest(power_id=power_id):
                classified = combat_predictor.classify_active_power(
                    {"id": power_id, "name": power_id, "amount": 3},
                    "monster",
                )
                self.assertEqual("exact_branch", classified["category"])
                self.assertEqual(
                    "advance_existing_choke_damage_after_later_cards",
                    classified["handler"],
                )

        for owner_role, power_id, expected in (
            ("player", "Fire Breathing", "heuristic"),
            ("player", "Creative AI", "heuristic"),
            ("player", "Hello", "heuristic"),
            ("player", "Envenom", "heuristic"),
            ("player", "Sadistic Nature", "exact_branch"),
            ("player", "Panache", "heuristic"),
            ("monster", "Lock-On", "exact_branch"),
            ("monster", "Lockon", "exact_branch"),
            ("monster", "Reactive", "heuristic"),
        ):
            with self.subTest(owner_role=owner_role, power_id=power_id):
                classified = combat_predictor.classify_active_power(
                    {"id": power_id, "name": power_id, "amount": 1},
                    owner_role,
                )
                self.assertEqual(expected, classified["category"])
                if expected == "exact_branch":
                    self.assertTrue(classified["handler"])
                else:
                    self.assertIsNone(classified["handler"])

        draw_reduction = combat_predictor.classify_active_power(
            {
                "id": "Draw Reduction", "name": "Draw Reduction",
                "amount": 1,
            },
            "player",
        )
        self.assertEqual("state_reflected", draw_reduction["category"])
        self.assertNotIn(
            "drawreduction",
            combat_predictor.UNSUPPORTED_COMBAT_POWER_IDS["player"],
        )
        self.assertIn(
            "envenom",
            combat_predictor.HEURISTIC_COMBAT_POWER_IDS["player"],
        )
        fire_breathing = combat_predictor.classify_active_power(
            {"id": "FireBreathingPower", "name": "Fire Breathing", "amount": 6},
            "player",
        )
        self.assertEqual("heuristic", fire_breathing["category"])
        self.assertIsNone(fire_breathing["handler"])
        self.assertNotIn(
            "firebreathing",
            combat_predictor.UNSUPPORTED_COMBAT_POWER_IDS["player"],
        )
        deferred_draw = combat_predictor.classify_active_power(
            {"id": "Draw Card", "name": "Draw Card", "amount": 2},
            "player",
        )
        self.assertEqual("state_reflected", deferred_draw["category"])
        self.assertNotIn(
            "drawcard",
            combat_predictor.UNSUPPORTED_COMBAT_POWER_IDS["player"],
        )
        mayhem = combat_predictor.classify_active_power(
            {"id": "Mayhem", "name": "Mayhem", "amount": 1},
            "player",
        )
        self.assertEqual("state_reflected", mayhem["category"])
        self.assertNotIn(
            "mayhem",
            combat_predictor.UNSUPPORTED_COMBAT_POWER_IDS["player"],
        )
        panache = combat_predictor.classify_active_power(
            {"id": "Panache", "name": "Panache", "amount": 2},
            "player",
        )
        self.assertEqual("heuristic", panache["category"])
        self.assertNotIn(
            "panache",
            combat_predictor.UNSUPPORTED_COMBAT_POWER_IDS["player"],
        )
        rebound = combat_predictor.classify_active_power(
            {"id": "Rebound", "name": "Rebound", "amount": 1},
            "player",
        )
        self.assertEqual("heuristic", rebound["category"])
        self.assertNotIn(
            "rebound",
            combat_predictor.UNSUPPORTED_COMBAT_POWER_IDS["player"],
        )
        self.assertIn(
            "rebound",
            combat_predictor.HEURISTIC_COMBAT_POWER_IDS["player"],
        )
        equilibrium = combat_predictor.classify_active_power(
            {"id": "Equilibrium", "name": "Equilibrium", "amount": 1},
            "player",
        )
        self.assertEqual("state_reflected", equilibrium["category"])
        self.assertNotIn(
            "equilibrium",
            combat_predictor.UNSUPPORTED_COMBAT_POWER_IDS["player"],
        )
        tools_of_the_trade = combat_predictor.classify_active_power(
            {
                "id": "ToolsOfTheTradePower",
                "name": "Tools of the Trade",
                "amount": 1,
            },
            "player",
        )
        self.assertEqual("state_reflected", tools_of_the_trade["category"])
        self.assertIsNone(tools_of_the_trade["handler"])
        self.assertNotIn(
            "toolsofthetrade",
            combat_predictor.UNSUPPORTED_COMBAT_POWER_IDS["player"],
        )

        for power_id in ("Rupture", "Hex", "Storm", "StormPower"):
            classified = combat_predictor.classify_active_power(
                {"id": power_id, "name": power_id, "amount": 1},
                "player",
            )
            self.assertEqual("exact_branch", classified["category"])
            self.assertTrue(classified["handler"])
        life_link = combat_predictor.classify_active_power(
            {"id": "Life Link", "name": "Life Link", "amount": 1},
            "monster",
        )
        self.assertEqual("exact_branch", life_link["category"])
        self.assertTrue(life_link["handler"])
        for power_id in (
            "Loop", "EnergizedBlue", "Berserk",
            "Magnetism", "Evolve", "DuplicationPower",
        ):
            classified = combat_predictor.classify_active_power(
                {"id": power_id, "name": power_id, "amount": 1},
                "player",
            )
            self.assertEqual("heuristic", classified["category"])

    def test_slow_scales_target_local_attack_damage(self):
        giant_head = monster(
            "GiantHead", 500, powers=[power("Slow", 2)]
        )
        game = GameStub([giant_head])

        self.assertEqual(
            12,
            combat_predictor.attack_hp_loss(
                giant_head, 10, hits=1
            ),
        )
        self.assertEqual(
            15,
            combat_predictor.attack_hp_loss(
                giant_head, 10, hits=1, extra_slow_cards=3
            ),
        )

        vulnerable_head = monster(
            "GiantHead", 500,
            powers=[power("Slow", 2), power("Vulnerable", 1)],
        )
        self.assertEqual(
            54,
            combat_predictor.attack_hp_loss(
                vulnerable_head, 30, hits=1
            ),
        )

    def test_paper_frog_and_slow_share_one_target_rounding_chain(self):
        giant_head = monster(
            "GiantHead",
            500,
            powers=[power("Vulnerable", 1), power("Slow", 3)],
        )
        game = GameStub(
            [giant_head], relics=[Relic("Paper Frog", "Paper Frog")]
        )

        # floor(34 * 1.75 * 1.3) == 77.  Flooring the Paper Frog
        # multiplier first and then Slow would incorrectly report 76.
        self.assertEqual(
            77,
            combat_predictor.attack_hp_loss(
                giant_head,
                34,
                **combat_predictor.attack_relic_modifiers(game),
            ),
        )

    def test_weak_whirlwind_reconstructs_each_hit_before_paper_frog(self):
        target = monster(
            "TheMaw", 300, powers=[power("Vulnerable", 2)]
        )
        whirlwind = Card(
            "Whirlwind", "Whirlwind", CardType.ATTACK,
            CardRarity.UNCOMMON, upgrades=1, cost=-1,
            uuid="card:whirlwind", has_target=False, is_playable=True,
            damage=7, base_damage=8, block=0, base_block=0,
            magic_number=0,
        )
        game = GameStub(
            [target],
            powers=[power("Weakened", 2), power("Strength", 2)],
            hand=[whirlwind],
            relics=[Relic("Paper Frog", "Paper Frog")],
        )

        # X=3: each hit is floor((8 + 2) * .75 * 1.75) == 13.
        self.assertEqual(
            39,
            combat_predictor.card_attack_hp_loss(game, whirlwind, target),
        )

    def test_enemy_regenerate_is_only_projected_when_target_survives(self):
        target = monster(
            "SphericGuardian", 100, powers=[power("Regenerate", 7)]
        )

        self.assertEqual(
            7,
            combat_predictor.projected_monster_end_turn_healing(
                target, 20
            ),
        )
        self.assertEqual(
            0,
            combat_predictor.projected_monster_end_turn_healing(
                target, 0
            ),
        )

    def test_same_turn_dexterity_delta_respects_frail_rounding(self):
        game = GameStub(
            [],
            powers=[
                power("Dexterity", 0),
                power("Frail", 1),
            ],
        )

        # Four raw Block is three while Frail is active; one newly gained
        # Dexterity still leaves it at three, so the marginal gain is zero.
        self.assertEqual(
            0,
            combat_predictor.block_after_extra_dexterity(game, 4, 1),
        )
        self.assertEqual(
            1,
            combat_predictor.block_after_extra_dexterity(game, None, 2),
        )

    def test_thorns_stops_remaining_multihit_attacks_after_kill(self):
        byrds = [
            monster("Byrd-1", 9, damage=1, hits=5, index=0),
            monster("Byrd-2", 8, damage=1, hits=5, index=1),
        ]
        game = GameStub(
            byrds,
            block=6,
            powers=[power("Thorns", 3)],
        )

        self.assertEqual(0, combat_predictor.projected_attack_hp_loss(game))

    def test_suck_heals_before_thorns_and_can_keep_multihit_alive(self):
        parasite = monster(
            "Shelled Parasite", 2, damage=10, hits=2
        )
        parasite.max_hp = 72
        parasite.intent = Intent.ATTACK_BUFF
        game = GameStub(
            [parasite],
            block=8,
            powers=[power("Thorns", 3)],
        )

        # Hit one loses two HP, heals the Parasite from two to four, then
        # Thorns leaves it at one.  The second hit must therefore still run.
        # Resolving Thorns before Suck would falsely stop at two player HP.
        self.assertEqual(
            12,
            combat_predictor.projected_attack_hp_loss(game),
        )

    def test_fully_blocked_suck_requests_no_healing(self):
        parasite = monster(
            "ShelledParasite", 2, damage=10, hits=1
        )
        parasite.max_hp = 72
        parasite.intent = Intent.ATTACK_BUFF
        game = GameStub([parasite], block=10)
        requested = []

        outcome = combat_predictor._projected_attack_outcome(
            game,
            enemy_attack_healing=(
                lambda _monster, amount: requested.append(amount)
            ),
        )

        self.assertEqual(0, outcome[0])
        self.assertEqual([], requested)

    def test_suck_healing_uses_final_damage_after_tungsten_rod(self):
        parasite = monster(
            "ShelledParasite", 20, damage=10, hits=1
        )
        parasite.max_hp = 72
        parasite.intent = Intent.ATTACK_BUFF
        game = GameStub(
            [parasite],
            block=8,
            relics=[Relic("TungstenRod", "TungstenRod")],
        )
        requested = []

        outcome = combat_predictor._projected_attack_outcome(
            game,
            enemy_attack_healing=(
                lambda _monster, amount: requested.append(amount)
            ),
        )

        # Two damage reaches HP after Block, then Tungsten reduces the
        # actual loss (and therefore Suck's healing) to one.
        self.assertEqual(1, outcome[0])
        self.assertEqual([1], requested)

    def test_regeneration_with_magic_flower_uses_half_up_healing(self):
        game = GameStub(
            [],
            hp=44,
            powers=[power("Regeneration", 5)],
            relics=[Relic("Magic Flower", "Magic Flower")],
        )

        self.assertEqual(
            8, combat_predictor.projected_end_turn_healing(game)
        )

    def test_regeneration_healing_is_capped_after_preceding_loss(self):
        game = GameStub(
            [], hp=69, powers=[power("Regeneration", 5)]
        )

        self.assertEqual(
            1, combat_predictor.projected_end_turn_healing(game)
        )
        self.assertEqual(
            5,
            combat_predictor.projected_end_turn_healing(
                game, preceding_hp_loss=4
            ),
        )

    def test_regeneration_survival_uses_ordered_stable_hp_without_fairy(self):
        # Java RegenPower/RegenAction heals before attacks, capped at max HP;
        # Brutality belongs to the following player turn, after surviving hits.
        for hp, regen, brutality, expected_hp in (
            (5, 5, 0, 4), (5, 1, 0, 0), (10, 5, 0, 4),
            (5, 5, 1, 3), (5, 5, 4, 0),
        ):
            with self.subTest(hp=hp, regen=regen, brutality=brutality):
                powers = [power("Regeneration", regen)]
                if brutality:
                    powers.append(power("Brutality", brutality))
                game = GameStub([monster("Cultist", 40, damage=6)], hp=hp, powers=powers)
                game.player.max_hp = 10
                result = combat_predictor.projected_turn_outcome(game)
                self.assertEqual(expected_hp, result.final_player_hp)
                self.assertEqual(expected_hp - hp, result.player_hp_delta)
                self.assertEqual(expected_hp > 0, result.player_survives)
                self.assertEqual(6 + brutality, result.total_hp_loss)

    def test_regeneration_does_not_revive_lethal_hand_damage(self):
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.COMMON, cost=-2)
        game = GameStub([monster("Cultist", 40)], hp=1,
                        powers=[power("Regeneration", 5)], hand=[burn])
        result = combat_predictor.projected_turn_outcome(game)
        self.assertEqual(0, result.end_turn_healing)
        self.assertEqual(0, result.final_player_hp)
        self.assertFalse(result.player_survives)

    def test_fairy_potion_revives_between_multihit_attack_packets(self):
        guardian = monster("TheGuardian", 50, damage=5, hits=4)
        game = GameStub(
            [guardian], hp=11, powers=[power("Metallicize", 3)]
        )
        game.player.max_hp = 85
        game.potions = [Potion(
            "FairyPotion", "Fairy in a Bottle",
            can_use=False, can_discard=True, requires_target=False,
        )]

        outcome = combat_predictor.projected_turn_outcome(game)

        self.assertEqual(16, outcome.attack_hp_loss)
        self.assertEqual(16, outcome.total_hp_loss)
        self.assertTrue(outcome.fairy_revive_consumed)
        self.assertEqual(25, outcome.fairy_revive_healing)
        self.assertEqual(20, outcome.final_player_hp)
        self.assertEqual(9, outcome.player_hp_delta)
        self.assertTrue(outcome.player_survives)

    def test_automatic_fairy_revive_does_not_trigger_toy_ornithopter(self):
        game = GameStub(
            [], relics=[Relic("Toy Ornithopter", "Toy Ornithopter")]
        )
        game.player.max_hp = 90
        game.potions = [Potion(
            "FairyPotion", "Fairy in a Bottle",
            can_use=False, can_discard=True, requires_target=False,
        )]

        self.assertEqual(27, combat_predictor.fairy_in_a_bottle_healing(game))

    def test_invincible_is_a_known_fail_closed_power(self):
        classified = combat_predictor.classify_active_power(
            {
                "id": "InvinciblePower", "name": "Invincible",
                "amount": 200,
            },
            "monster",
        )

        self.assertEqual("heuristic", classified["category"])
        self.assertIsNone(classified["handler"])

    def test_end_turn_lightning_crosses_slime_split_and_cancels_attack(self):
        slime = monster("SlimeBoss", 140, damage=35)
        slime.current_hp = 72
        game = GameStub(
            [slime],
            hp=75,
            block=5,
            orbs=[
                Orb("Frost", "Frost", 5, 2),
                Orb("Lightning", "Lightning", 8, 3),
            ],
        )

        outcome = combat_predictor.projected_turn_outcome(game)

        self.assertEqual(0, outcome.attack_hp_loss)
        self.assertEqual(
            [slime],
            combat_predictor.passive_action_suppressed_monsters(game),
        )

    def test_poison_crosses_large_slime_split_and_cancels_attack(self):
        cases = (
            ("AcidSlime_L", 50, 30, 6, 11),
            ("SpikeSlime_L", 70, 40, 7, 16),
        )
        for monster_id, max_hp, current_hp, poison, attack in cases:
            with self.subTest(monster_id=monster_id):
                slime = monster(
                    monster_id,
                    max_hp,
                    damage=attack,
                    powers=[power("Poison", poison)],
                )
                slime.current_hp = current_hp
                game = GameStub([slime], hp=75)

                outcome = combat_predictor.projected_turn_outcome(game)

                self.assertEqual(0, outcome.attack_hp_loss)
                self.assertEqual(0, outcome.total_hp_loss)
                self.assertEqual(
                    [slime],
                    combat_predictor.passive_action_suppressed_monsters(
                        game
                    ),
                )

    def test_end_turn_lightning_reduces_transient_attack_strength(self):
        transient = monster(
            "Transient",
            999,
            damage=30,
            powers=[power("Shifting", -1), power("Fading", 4)],
        )
        transient.current_hp = 935
        game = GameStub(
            [transient],
            hp=66,
            block=8,
            orbs=[
                Orb("Lightning", "Lightning", 10, 5),
                Orb("Frost", "Frost", 7, 4),
            ],
        )

        outcome = combat_predictor.projected_turn_outcome(game)

        # 30 intent - 5 Shifting loss - (8 current + 4 Frost block).
        self.assertEqual(13, outcome.attack_hp_loss)

    def test_transient_shifting_poison_respects_paper_crane_weak_rounding(self):
        transient = monster(
            "Transient",
            999,
            damage=17,
            powers=[
                power("Shifting", -1),
                power("Fading", 4),
                power("Poison", 15),
                power("Shackled", 11),
                power("Strength", -11),
                power("Weakened", 1),
            ],
        )
        transient.current_hp = 953
        game = GameStub(
            [transient],
            hp=44,
            powers=[
                power("Thorns", 6),
                power("Noxious Fumes", 6),
            ],
            relics=[Relic("Paper Crane", "Paper Crane")],
        )

        outcome = combat_predictor.projected_turn_outcome(game)

        # Poison removes 15 Strength, but the displayed move is already
        # weakened by Paper Crane: 17 - floor(15 * .60) = 8.
        self.assertEqual(8, outcome.attack_hp_loss)

    def test_transient_shifting_weak_rounding_applies_before_block(self):
        transient = monster(
            "Transient",
            999,
            damage=25,
            powers=[
                power("Shifting", -1),
                power("Fading", 3),
                power("Poison", 20),
                power("Shackled", 8),
                power("Strength", -8),
                power("Weakened", 3),
            ],
        )
        transient.current_hp = 932
        game = GameStub(
            [transient],
            hp=41,
            block=10,
            relics=[Relic("Paper Crane", "Paper Crane")],
        )

        self.assertEqual(
            3, combat_predictor.projected_turn_outcome(game).attack_hp_loss
        )

    def test_transient_with_one_fading_still_executes_current_attack(self):
        transient = monster(
            "Transient",
            999,
            damage=39,
            powers=[power("Fading", 1)],
        )
        game = GameStub([transient], block=37)

        outcome = combat_predictor.projected_turn_outcome(game)

        self.assertEqual(2, outcome.attack_hp_loss)

    def test_transient_attack_excludes_new_noxious_fumes_application(self):
        transient = monster(
            "Transient",
            999,
            damage=24,
            powers=[power("Poison", 8)],
        )
        game = GameStub(
            [transient],
            block=8,
            powers=[power("Noxious Fumes", 5)],
        )

        outcome = combat_predictor.projected_turn_outcome(game)

        # Existing Poison reduces Transient's current attack through
        # Shifting, while this turn's Noxious Fumes application happens after
        # the attack and must not be counted a second time.
        self.assertEqual(8, outcome.attack_hp_loss)

    def test_poison_doomed_enemy_is_removed_from_targets_and_incoming(self):
        doomed = monster("doomed", 6, damage=30, powers=[power("Poison", 8)], index=0)
        active = monster("active", 20, damage=7, index=1)
        game = GameStub([doomed, active])

        self.assertEqual([active], combat_predictor.active_monsters(game))
        self.assertEqual(7, combat_predictor.incoming_damage(game))
        self.assertIs(active, combat_predictor.choose_attack_target(game))

    def test_noxious_fumes_tick_does_not_suppress_current_enemy_move(self):
        target = monster(
            "Darkling", 7, damage=8, hits=2, block=4,
            powers=[power("Poison", 4)],
        )
        game = GameStub(
            [target], block=15, powers=[power("Noxious Fumes", 3)]
        )

        self.assertEqual([], combat_predictor.projected_doomed_monsters(game))
        # Noxious Fumes is applied after this enemy move in the live game;
        # the current attack must remain in the survival forecast even though
        # the target remains alive and targetable in this decision frame.
        self.assertEqual(1, combat_predictor.projected_attack_hp_loss(game))
        self.assertFalse(combat_predictor.safe_to_wait_for_passive_kills(game))

    def test_existing_poison_below_hp_keeps_current_attack_with_noxious(self):
        target = monster(
            "Champ",
            11,
            damage=8,
            hits=2,
            powers=[power("Poison", 9)],
        )
        game = GameStub(
            [target], hp=3, block=13, powers=[power("Noxious Fumes", 2)]
        )

        # Poison 9 does not kill an 11-HP target before this move, and the
        # additional Noxious tick is later than the current enemy action.
        self.assertEqual(3, combat_predictor.projected_attack_hp_loss(game))
        self.assertFalse(combat_predictor.safe_to_wait_for_passive_kills(game))

    def test_champ_poison_crossing_keeps_current_attack(self):
        champ = monster(
            "Champ", 217, damage=12, block=0,
            powers=[power("Poison", 49)],
        )
        champ.max_hp = 420
        game = GameStub([champ], block=3)

        self.assertEqual(9, combat_predictor.projected_attack_hp_loss(game))

    def test_spore_cloud_from_doomed_enemy_vulnerables_before_other_attack(self):
        doomed = monster(
            "FungiBeast",
            3,
            powers=[power("Poison", 3), power("Spore Cloud", 2)],
            index=0,
        )
        attacker = monster("active", 20, damage=6, index=1)
        game = GameStub([doomed, attacker])

        self.assertTrue(
            combat_predictor.projected_deaths_apply_player_vulnerable(game)
        )
        self.assertEqual(9, combat_predictor.incoming_damage(game))
        self.assertEqual(9, combat_predictor.projected_attack_hp_loss(game))

    def test_random_passive_lightning_spore_kill_is_survival_bound(self):
        parasite = monster(
            "ShelledParasite",
            64,
            damage=18,
            index=0,
        )
        fungi = monster(
            "FungiBeast",
            2,
            powers=[power("Spore Cloud", 2)],
            index=1,
        )
        game = GameStub(
            [parasite, fungi],
            hp=53,
            block=7,
            powers=[power("Plated Armor", 4)],
            orbs=[
                Orb("Lightning", "Lightning", 10, 5),
                Orb("Frost", "Frost", 7, 4),
            ],
        )

        # The random Lightning branch which kills the 2-HP beast applies
        # Vulnerable before Parasite's hit: floor(18 * 1.5) - 15 = 12.
        self.assertEqual(12, combat_predictor.projected_attack_hp_loss(game))

    def test_poisoned_attacker_hp_is_used_before_thorns_spore_death(self):
        buffing = monster(
            "FungiBeast",
            7,
            powers=[
                power("Poison", 3),
                power("Spore Cloud", 2),
            ],
            index=0,
        )
        reflected = monster(
            "FungiBeast",
            8,
            damage=9,
            powers=[
                power("Poison", 6),
                power("Spore Cloud", 2),
            ],
            index=1,
        )
        later = monster(
            "FungiBeast",
            22,
            damage=9,
            powers=[
                power("Poison", 3),
                power("Spore Cloud", 2),
            ],
            index=2,
        )
        game = GameStub(
            [buffing, reflected, later],
            hp=54,
            block=13,
            powers=[power("Thorns", 6)],
        )

        # Poison leaves the middle beast at two before it attacks. The fully
        # blocked hit still triggers six Thorns, then Spore Cloud strengthens
        # the last hit from nine to thirteen.
        self.assertEqual(9, combat_predictor.projected_attack_hp_loss(game))

    def test_artifact_absorbs_projected_spore_cloud_vulnerable(self):
        doomed = monster(
            "FungiBeast",
            3,
            powers=[power("Poison", 3), power("SporeCloudPower", 2)],
            index=0,
        )
        attacker = monster("active", 20, damage=6, index=1)
        game = GameStub(
            [doomed, attacker], powers=[power("Artifact", 1)]
        )

        self.assertFalse(
            combat_predictor.projected_deaths_apply_player_vulnerable(game)
        )
        self.assertEqual(6, combat_predictor.projected_attack_hp_loss(game))

    def test_flame_barrier_kill_applies_spore_cloud_before_next_attack(self):
        first = monster(
            "FungiBeast",
            5,
            damage=9,
            powers=[power("Spore Cloud", 2)],
            index=0,
        )
        second = monster(
            "FungiBeast",
            21,
            damage=9,
            powers=[power("Spore Cloud", 2)],
            index=1,
        )
        game = GameStub(
            [first, second],
            block=16,
            powers=[power("Flame Barrier", 6)],
        )

        # First hit consumes nine Block and the reflection kills its source.
        # Spore Cloud then makes the second nine-damage intent hit for 13;
        # seven remaining Block leaves six real HP loss.
        self.assertEqual(
            6, combat_predictor.projected_attack_hp_loss(game)
        )

    def test_predeath_spore_consumes_artifact_before_reflected_spore(self):
        doomed = monster(
            "FungiBeast",
            3,
            powers=[
                power("Poison", 3),
                power("Spore Cloud", 2),
            ],
            index=0,
        )
        reflected = monster(
            "FungiBeast",
            5,
            damage=9,
            powers=[power("Spore Cloud", 2)],
            index=1,
        )
        later = monster("Cultist", 30, damage=9, index=2)
        game = GameStub(
            [doomed, reflected, later],
            block=16,
            powers=[power("Artifact", 1), power("Flame Barrier", 6)],
        )

        # The poisoned beast consumes Artifact before attacks. Reflection
        # kills the second beast; its Spore Cloud then makes the final attack
        # deal 13 into seven remaining Block.
        self.assertEqual(
            6, combat_predictor.projected_attack_hp_loss(game)
        )

    def test_poison_ignores_block(self):
        target = monster("blocked", 6, damage=9, block=99, powers=[power("Poison", 6)])
        self.assertEqual([target], combat_predictor.projected_doomed_monsters(GameStub([target])))

    def test_intangible_caps_poison_to_one(self):
        target = monster("Nemesis", 6, damage=9, powers=[power("Poison", 8), power("Intangible", 1)])
        self.assertEqual(1, combat_predictor.poison_damage_before_action(target))
        self.assertEqual([], combat_predictor.projected_doomed_monsters(GameStub([target])))

    def test_invincible_is_never_optimistically_reserved_as_dead(self):
        target = monster("CorruptHeart", 5, damage=45, powers=[power("Poison", 999), power("Invincible", 300)])
        self.assertEqual([], combat_predictor.projected_doomed_monsters(GameStub([target])))

    def test_all_doomed_enemies_allow_end_turn(self):
        target = monster("poisoned", 4, damage=20, powers=[power("Poison", 5)])
        self.assertTrue(combat_predictor.safe_to_wait_for_passive_kills(GameStub([target], hp=10)))

    def test_constricted_resolves_before_final_poison_kill(self):
        target = monster("poisoned", 4, damage=20, powers=[power("Poison", 5)])
        game = GameStub([target], hp=5, powers=[power("Constricted", 5)])
        self.assertFalse(combat_predictor.safe_to_wait_for_passive_kills(game))

    def test_lethal_combust_still_prevents_passive_wait(self):
        target = monster("poisoned", 4, damage=20, powers=[power("Poison", 5)])
        game = GameStub([target], hp=1, powers=[power("Combust", 5)])
        self.assertFalse(combat_predictor.safe_to_wait_for_passive_kills(game))

    def test_corpse_explosion_cascade_reserves_second_enemy(self):
        source = monster(
            "exploder",
            5,
            damage=8,
            powers=[power("Poison", 5), power("CorpseExplosionPower", 1)],
        )
        source.max_hp = 30
        second = monster("second", 20, damage=12)
        self.assertEqual(
            {id(source), id(second)},
            {id(item) for item in combat_predictor.projected_doomed_monsters(GameStub([source, second]))},
        )

    def test_poisoned_corpse_explosion_suppresses_later_enemy_attack(self):
        """Replay run 5 F38: poison death cascades before the next move."""

        source = monster(
            "Orb Walker",
            93,
            damage=15,
            powers=[
                power("Poison", 33),
                power("CorpseExplosionPower", 1),
            ],
            index=0,
        )
        source.current_hp = 17
        later = monster(
            "Orb Walker",
            91,
            damage=22,
            powers=[power("Poison", 5)],
            index=1,
        )
        later.current_hp = 68
        game = GameStub([source, later], hp=12, block=5)

        outcome = combat_predictor.projected_turn_outcome(game)

        self.assertEqual(0, outcome.attack_hp_loss)
        self.assertEqual(
            {id(source), id(later)},
            {
                id(item)
                for item in combat_predictor.passive_action_suppressed_monsters(
                    game
                )
            },
        )

    def test_later_corpse_explosion_does_not_suppress_earlier_attack(self):
        """Enemy order remains authoritative for delayed poison cascades."""

        earlier = monster("Cultist", 68, damage=22, index=0)
        source = monster(
            "Orb Walker",
            93,
            damage=15,
            powers=[
                power("Poison", 33),
                power("CorpseExplosionPower", 1),
            ],
            index=1,
        )
        source.current_hp = 17
        game = GameStub([earlier, source], hp=50, block=5)

        outcome = combat_predictor.projected_turn_outcome(game)

        self.assertEqual(17, outcome.attack_hp_loss)
        self.assertEqual(
            [source],
            combat_predictor.passive_action_suppressed_monsters(game),
        )

    def test_defect_single_target_lightning_can_reserve_kill(self):
        target = monster("single", 3, damage=10)
        game = GameStub([target], orbs=[Orb("Lightning", "Lightning", 8, 3)])
        self.assertEqual([target], combat_predictor.projected_doomed_monsters(game))

    def test_defect_lightning_is_not_assigned_to_a_target_in_multi_enemy_combat(self):
        first = monster("first", 3, damage=10)
        second = monster("second", 30, damage=4)
        game = GameStub([first, second], orbs=[Orb("Lightning", "Lightning", 8, 3)])
        self.assertEqual([], combat_predictor.projected_doomed_monsters(game))

    def test_frost_metallicize_plated_armor_and_orichalcum_are_projected(self):
        game = GameStub(
            [],
            powers=[power("Metallicize", 3), power("Plated Armor", 4)],
            orbs=[Orb("Frost", "Frost", 5, 2)],
        )
        self.assertEqual(9, combat_predictor.projected_player_block(game))

        orichalcum_game = GameStub([], relics=[Relic("Orichalcum", "Orichalcum")])
        self.assertEqual(6, combat_predictor.projected_player_block(orichalcum_game))

    def test_no_block_preserves_existing_block_but_suppresses_every_end_gain(self):
        attacker = monster("attacker", 80, damage=12)
        game = GameStub(
            [attacker],
            block=5,
            powers=[
                power("NoBlockPower", 1),
                power("Metallicize", 3),
                power("Plated Armor", 4),
            ],
            orbs=[Orb("Frost", "Frost", 5, 2)],
            relics=[
                Relic("Orichalcum", "Orichalcum"),
                Relic("Cables", "Cables"),
                Relic("Frozen Core", "Frozen Core"),
            ],
        )

        self.assertFalse(combat_predictor.can_gain_block(game))
        self.assertEqual(5, combat_predictor.projected_player_block(game))
        self.assertEqual(
            5,
            combat_predictor.projected_end_block(game, extra_block=99),
        )
        outcome = combat_predictor.projected_turn_outcome(
            game,
            extra_block=99,
            passive_block_override=99,
        )
        self.assertEqual(7, outcome.attack_hp_loss)
        self.assertEqual(0, outcome.block)

    def test_no_block_does_not_disable_buffer_or_intangible(self):
        attacker = monster("multi", 80, damage=12, hits=2)
        game = GameStub(
            [attacker],
            hp=7,
            powers=[
                power("NoBlockPower", 1),
                power("Buffer", 1),
                power("IntangiblePlayer", 1),
            ],
        )

        outcome = combat_predictor.projected_turn_outcome(game)

        self.assertEqual(1, outcome.attack_hp_loss)
        self.assertEqual(0, outcome.buffer)

    def test_cables_and_frozen_core_frost_stack_after_orichalcum(self):
        game = GameStub(
            [],
            orbs=[
                Orb("Frost", "Frost", 5, 2),
                Orb("Empty", "Empty", 0, 0),
                Orb("Empty", "Empty", 0, 0),
            ],
            relics=[
                Relic("Orichalcum", "Orichalcum"),
                Relic("Cables", "Cables"),
                Relic("Frozen Core", "Frozen Core"),
            ],
        )

        # Ori 6 + Frost 2 + Cables 2 + Frozen Core's new Frost 2.
        self.assertEqual(12, combat_predictor.projected_player_block(game))

    def test_negative_focus_reduces_frozen_core_frost(self):
        game = GameStub(
            [],
            powers=[power("Focus", -2)],
            orbs=[
                Orb("Empty", "Empty", 0, 0),
                Orb("Empty", "Empty", 0, 0),
                Orb("Empty", "Empty", 0, 0),
            ],
            relics=[
                Relic("Orichalcum", "Orichalcum"),
                Relic("Frozen Core", "Frozen Core"),
            ],
        )

        self.assertEqual(-2, combat_predictor.signed_power_amount(game.player, "Focus"))
        # The newly channelled Frost has zero passive block at -2 Focus.
        self.assertEqual(6, combat_predictor.projected_player_block(game))

    def test_player_intangible_caps_each_incoming_hit(self):
        attacker = monster("multi", 30, damage=12, hits=3)
        game = GameStub([attacker], powers=[power("IntangiblePlayer", 1)])
        self.assertEqual(3, combat_predictor.incoming_damage(game))
        self.assertEqual(3, combat_predictor.projected_attack_hp_loss(game))

    def test_exploder_countdown_one_is_a_blockable_damage_packet(self):
        exploder = monster(
            "Exploder",
            30,
            powers=[power("Explosive", 1)],
        )
        game = GameStub([exploder], hp=47, block=13)

        packets = combat_predictor.monster_damage_packets(exploder)
        self.assertEqual(
            [("exploder_explosion", 30, 1)],
            [(item.source, item.damage_per_hit, item.hits) for item in packets],
        )
        self.assertEqual(30, combat_predictor.incoming_damage(game))
        self.assertEqual(17, combat_predictor.projected_attack_hp_loss(game))

    def test_exploder_thorns_damage_skips_torii_but_uses_tungsten(self):
        exploder = monster(
            "Exploder",
            30,
            powers=[power("Explosive", 1)],
        )
        game = GameStub(
            [exploder],
            block=25,
            relics=[
                Relic("Torii", "Torii"),
                Relic("Tungsten Rod", "Tungsten Rod"),
            ],
        )

        packet = combat_predictor.monster_damage_packets(exploder)[0]
        self.assertFalse(packet.torii_eligible)
        # Thirty minus 25 block leaves five. Torii cannot reduce THORNS
        # damage, while Tungsten Rod still reduces the event to four.
        self.assertEqual(4, combat_predictor.projected_attack_hp_loss(game))

    def test_exploder_earlier_countdown_does_not_invent_damage(self):
        exploder = monster(
            "Exploder",
            30,
            powers=[power("Explosive", 2)],
        )
        game = GameStub([exploder])

        self.assertEqual([], combat_predictor.monster_damage_packets(exploder))
        self.assertEqual(0, combat_predictor.projected_attack_hp_loss(game))

    def test_poison_kills_exploder_before_countdown_damage(self):
        exploder = monster(
            "Exploder",
            5,
            powers=[power("Poison", 5), power("Explosive", 1)],
        )
        game = GameStub([exploder], hp=10)

        self.assertEqual([], combat_predictor.active_monsters(game))
        self.assertEqual(0, combat_predictor.projected_attack_hp_loss(game))
        self.assertTrue(combat_predictor.safe_to_wait_for_passive_kills(game))

    def test_buffer_torii_and_tungsten_are_applied_after_block(self):
        attacker = monster("multi", 30, damage=5, hits=3)
        game = GameStub(
            [attacker],
            block=4,
            powers=[power("Buffer", 1)],
            relics=[Relic("Torii", "Torii"), Relic("Tungsten Rod", "Tungsten Rod")],
        )
        # First hit leaves one damage and consumes Buffer.  Torii reduces each
        # later five-damage hit to one, then Tungsten Rod reduces it to zero.
        self.assertEqual(0, combat_predictor.projected_attack_hp_loss(game))

    def test_burn_decay_and_regret_are_counted_before_waiting(self):
        hand = [
            Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL, upgrades=0),
            Card("Decay", "Decay", CardType.CURSE, CardRarity.CURSE),
            Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE),
        ]
        game = GameStub([], hand=hand)
        self.assertEqual(7, combat_predictor.player_end_turn_hp_loss(game))
        self.assertEqual(7, combat_predictor.projected_turn_hp_loss(game))

    def test_tungsten_reduces_each_end_turn_hp_loss_event(self):
        hand = [
            Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL, upgrades=0),
            Card("Decay", "Decay", CardType.CURSE, CardRarity.CURSE),
            Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE),
        ]
        game = GameStub(
            [],
            hand=hand,
            relics=[Relic("Tungsten Rod", "Tungsten Rod")],
        )

        # All hand effects are queued while the full hand remains, so Regret
        # sees three cards. Rod reduces 2, 2, 3 to 1, 1, 2.
        self.assertEqual(4, combat_predictor.player_end_turn_hp_loss(game))

    def test_hand_status_order_carries_block_and_buffer_between_events(self):
        hand = [
            Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL),
            Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE),
        ]
        game = GameStub(
            [],
            hp=10,
            block=1,
            powers=[power("Buffer", 1)],
            hand=hand,
        )

        outcome = combat_predictor.projected_turn_outcome(game)

        # Burn spends one block then Buffer; Regret still sees the original
        # two-card hand and deals two after both resources are gone.
        self.assertEqual(2, outcome.end_turn_hp_loss)
        self.assertEqual(0, outcome.buffer)

    def test_hand_override_rebuilds_regret_from_remaining_hand(self):
        strike = Card("Strike_R", "Strike", CardType.ATTACK, CardRarity.BASIC)
        regret = Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE)
        game = GameStub([], hp=10, hand=[strike, regret])

        outcome = combat_predictor.projected_turn_outcome(
            game,
            hand_override=[regret],
            hand_size_override=1,
        )

        self.assertEqual(1, outcome.end_turn_hp_loss)

    def test_end_orb_kill_ends_combat_before_burn(self):
        target = monster("target", 8, damage=20)
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub(
            [target],
            hp=2,
            hand=[burn],
            orbs=[Orb("Lightning", "Lightning", 8, 8)],
        )

        outcome = combat_predictor.projected_turn_outcome(game)

        self.assertEqual(0, outcome.end_turn_hp_loss)
        self.assertEqual(0, outcome.attack_hp_loss)

    def test_end_turn_damage_consumes_buffer_before_enemy_attack(self):
        attacker = monster("attacker", 30, damage=20)
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub(
            [attacker],
            hp=10,
            powers=[power("Buffer", 1)],
            hand=[burn],
        )

        outcome = combat_predictor.projected_turn_outcome(game)

        self.assertEqual(0, outcome.end_turn_hp_loss)
        self.assertEqual(20, outcome.attack_hp_loss)
        self.assertEqual(0, outcome.buffer)

    def test_combust_uses_serialized_hp_loss_misc(self):
        combust = power("Combust", 10)
        combust.misc = 2
        game = GameStub([], hp=10, powers=[combust])

        self.assertEqual(2, combat_predictor.player_end_turn_hp_loss(game))

    def test_stacked_brutality_is_one_event_for_its_power_amount(self):
        game = GameStub(
            [monster("idle", 20)],
            powers=[power("Brutality", 3)],
        )

        outcome = combat_predictor.projected_turn_outcome(game)

        self.assertEqual(0, outcome.end_turn_hp_loss)
        self.assertEqual(0, outcome.attack_hp_loss)
        self.assertEqual(3, outcome.next_turn_start_hp_loss)
        self.assertEqual(3, outcome.total_hp_loss)

    def test_brutality_is_counted_after_a_survived_enemy_attack(self):
        game = GameStub(
            [monster("attacker", 20, damage=12)],
            hp=5,
            block=8,
            powers=[power("Brutality", 1)],
        )

        outcome = combat_predictor.projected_turn_outcome(game)

        self.assertEqual(4, outcome.attack_hp_loss)
        self.assertEqual(1, outcome.next_turn_start_hp_loss)
        self.assertEqual(5, outcome.total_hp_loss)

    def test_brutality_does_not_fire_after_true_combat_end_or_prior_death(self):
        brutality = [power("Brutality", 3)]
        survivor = GameStub([monster("idle", 20)], hp=5, powers=brutality)
        ended = combat_predictor.projected_turn_outcome(
            survivor, combat_ends_before_next_turn=True
        )
        lethal_attack = GameStub(
            [monster("attacker", 20, damage=5)], hp=5, powers=brutality
        )

        self.assertEqual(0, ended.next_turn_start_hp_loss)
        self.assertEqual(
            0,
            combat_predictor.projected_turn_outcome(
                lethal_attack
            ).next_turn_start_hp_loss,
        )

    def test_tungsten_reduces_once_and_buffer_prevents_stacked_brutality(self):
        brutality = [power("Brutality", 3)]
        tungsten = GameStub(
            [monster("idle", 20)],
            powers=brutality,
            relics=[Relic("Tungsten Rod", "Tungsten Rod")],
        )
        buffered = GameStub(
            [monster("idle", 20)],
            powers=brutality + [power("Buffer", 1)],
        )

        self.assertEqual(
            2,
            combat_predictor.projected_turn_outcome(
                tungsten
            ).next_turn_start_hp_loss,
        )
        buffered_outcome = combat_predictor.projected_turn_outcome(buffered)
        self.assertEqual(0, buffered_outcome.next_turn_start_hp_loss)
        self.assertEqual(0, buffered_outcome.buffer)

    def test_branch_local_brutality_merges_with_existing_stack(self):
        tungsten = GameStub(
            [monster("idle", 20)],
            powers=[power("Brutality", 2)],
            relics=[Relic("Tungsten Rod", "Tungsten Rod")],
        )
        buffered = GameStub(
            [monster("idle", 20)],
            powers=[power("Brutality", 2), power("Buffer", 1)],
        )

        tungsten_outcome = combat_predictor.projected_turn_outcome(
            tungsten, extra_brutality_amount=1
        )
        buffered_outcome = combat_predictor.projected_turn_outcome(
            buffered, extra_brutality_amount=1
        )

        # Existing two stacks plus the branch-local card are one event of
        # three, so Tungsten applies once and Buffer consumes the whole event.
        self.assertEqual(2, tungsten_outcome.next_turn_start_hp_loss)
        self.assertEqual(0, buffered_outcome.next_turn_start_hp_loss)
        self.assertEqual(0, buffered_outcome.buffer)

    def test_branch_local_brutality_is_suppressed_after_true_combat_end(self):
        game = GameStub([monster("idle", 20)], hp=1)

        outcome = combat_predictor.projected_turn_outcome(
            game,
            extra_brutality_amount=1,
            combat_ends_before_next_turn=True,
        )

        self.assertEqual(0, outcome.next_turn_start_hp_loss)

    def test_extra_block_disables_instead_of_stacking_orichalcum(self):
        attacker = monster("attacker", 30, damage=10)
        game = GameStub(
            [attacker],
            relics=[Relic("Orichalcum", "Orichalcum")],
        )

        self.assertEqual(4, combat_predictor.projected_turn_hp_loss(game))
        outcome = combat_predictor.projected_turn_outcome(game, extra_block=5)
        self.assertEqual(5, outcome.attack_hp_loss)

    def test_total_turn_loss_spends_block_on_burn_before_enemy_attack(self):
        attacker = monster("attacker", 30, damage=10)
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([attacker], hp=20, block=6, hand=[burn])
        self.assertEqual(4, combat_predictor.projected_attack_hp_loss(game))
        self.assertEqual(0, combat_predictor.player_end_turn_hp_loss(game))
        self.assertEqual(6, combat_predictor.projected_turn_hp_loss(game))
        self.assertEqual(2, combat_predictor.projected_turn_hp_loss(game, extra_block=4))

    def test_random_sword_boomerang_is_not_a_bound_multi_enemy_kill(self):
        card = Card(
            "Sword Boomerang",
            "Sword Boomerang",
            CardType.ATTACK,
            CardRarity.COMMON,
            damage=3,
        )
        first = monster("first", 9)
        second = monster("second", 20)
        # Random targeting prevents guaranteed damage on one enemy, but the
        # hit count remains authoritative for Thorns/Sharp Hide survival.
        self.assertEqual((0, 3), combat_predictor.card_attack_profile(GameStub([first, second]), card))
        self.assertEqual((9, 3), combat_predictor.card_attack_profile(GameStub([first]), card))

    def test_rip_and_tear_has_two_random_packets_not_one_per_enemy(self):
        card = Card(
            "Rip and Tear",
            "Rip and Tear",
            CardType.ATTACK,
            CardRarity.UNCOMMON,
            damage=6,
        )
        first = monster("first", 30)
        second = monster("second", 30)
        third = monster("third", 30)

        # The two packets are total damage, not an AOE packet repeated for
        # every living enemy.  The target remains random when more than one
        # enemy is alive, but the hit count is authoritative in both frames.
        self.assertEqual(
            (12, 2),
            combat_predictor.card_attack_profile(
                GameStub([first, second, third]), card
            ),
        )
        self.assertEqual(
            (12, 2),
            combat_predictor.card_attack_profile(GameStub([first]), card),
        )

    def test_multihit_and_x_cost_profiles_are_deterministic(self):
        twin_strike = Card(
            "Twin Strike",
            "Twin Strike",
            CardType.ATTACK,
            CardRarity.COMMON,
            damage=5,
        )
        whirlwind = Card(
            "Whirlwind",
            "Whirlwind",
            CardType.ATTACK,
            CardRarity.UNCOMMON,
            cost=-1,
            damage=5,
        )
        game = GameStub([monster("target", 30)])
        self.assertEqual((10, 2), combat_predictor.card_attack_profile(game, twin_strike))
        self.assertEqual((15, 3), combat_predictor.card_attack_profile(game, whirlwind))
        self.assertEqual(3, combat_predictor.card_energy_cost(game, whirlwind))

    def test_live_card_damage_is_already_player_adjusted_per_hit(self):
        # CommunicationMod exposes AbstractCard.damage after player-side
        # modifiers.  A visible Strength/Flex stack must not be added again.
        game = GameStub(
            [monster("target", 60)],
            powers=[power("Strength", 2), power("Flex", 2)],
        )
        bite = Card(
            "Bite", "Bite", CardType.ATTACK, CardRarity.SPECIAL,
            damage=8,
        )
        twin = Card(
            "Twin Strike", "Twin Strike", CardType.ATTACK,
            CardRarity.COMMON, damage=8,
        )

        self.assertEqual((8, 1), combat_predictor.card_attack_profile(game, bite))
        self.assertEqual((16, 2), combat_predictor.card_attack_profile(game, twin))

    def test_target_vulnerable_is_applied_per_attack_hit_before_block(self):
        target = monster(
            "target",
            30,
            block=5,
            powers=[power("Vulnerable", 1)],
        )

        # Twin Strike's two five-damage hits each round down to seven under
        # Vulnerable, then the target's five Block is consumed once: 14-5=9.
        self.assertEqual(
            9,
            combat_predictor.attack_hp_loss(target, 10, hits=2),
        )
        self.assertEqual(
            14,
            combat_predictor.attack_damage_after_target_modifiers(
                target, 10, hits=2
            ),
        )

    def test_flight_halves_every_packet_in_one_card_resolution(self):
        airborne = monster(
            "Byrd", 40, powers=[power("Flight", 3)],
        )
        nearly_grounded = monster(
            "Byrd", 40, powers=[power("Flight", 1)],
        )

        self.assertEqual(
            8,
            combat_predictor.attack_damage_after_target_modifiers(
                airborne, 16, hits=2,
            ),
        )
        self.assertEqual(
            8,
            combat_predictor.attack_damage_after_target_modifiers(
                nearly_grounded, 16, hits=2,
            ),
        )
        self.assertEqual(
            8,
            combat_predictor.attack_hp_loss(
                nearly_grounded, 16, hits=2,
            ),
        )

    def test_paper_frog_and_boot_are_target_side_attack_modifiers(self):
        vulnerable = monster(
            "target",
            40,
            powers=[power("Vulnerable", 2)],
        )
        frog_game = GameStub(
            [vulnerable],
            relics=[Relic("Paper Frog", "Paper Frog")],
        )

        # Paper Frog changes each five-damage hit to floor(5 * 1.75) = 8.
        self.assertEqual(
            16,
            combat_predictor.attack_hp_loss(
                vulnerable,
                10,
                hits=2,
                **combat_predictor.attack_relic_modifiers(frog_game),
            ),
        )

        boot_target = monster("target", 40)
        boot_game = GameStub(
            [boot_target],
            relics=[Relic("Boot", "Boot")],
        )
        # The Boot is applied to each positive *unblocked* packet.  Three raw
        # damage into two Block therefore becomes one, then floors to five.
        boot_target.block = 2
        self.assertEqual(
            5,
            combat_predictor.attack_hp_loss(
                boot_target,
                3,
                **combat_predictor.attack_relic_modifiers(boot_game),
            ),
        )
        # A fully absorbed hit never becomes a fake five-damage packet, while
        # later hits observe the Block consumed by earlier ones.  This is the
        # exact Sword Boomerang ordering from the live Act 2 Boss trace:
        # 6+6+6 into 9 Block => 0 + 5 + 6 = 11.
        boot_target.block = 3
        self.assertEqual(
            0,
            combat_predictor.attack_hp_loss(
                boot_target,
                3,
                **combat_predictor.attack_relic_modifiers(boot_game),
            ),
        )
        boot_target.block = 9
        self.assertEqual(
            11,
            combat_predictor.attack_hp_loss(
                boot_target,
                18,
                hits=3,
                **combat_predictor.attack_relic_modifiers(boot_game),
            ),
        )
        vulnerable_boot_target = monster(
            "target",
            40,
            powers=[power("Vulnerable", 1)],
        )
        # Vulnerable is resolved before The Boot.  Four damage becomes six,
        # so the low-damage floor does not inflate this hit to seven.
        self.assertEqual(
            6,
            combat_predictor.attack_hp_loss(
                vulnerable_boot_target,
                4,
                **combat_predictor.attack_relic_modifiers(boot_game),
            ),
        )

    def test_card_packet_reconstructs_weak_vulnerable_before_final_rounding(self):
        """A real Weak+Vulnerable packet cannot reuse rounded card.damage."""

        target = monster(
            "SnakePlant", 40, powers=[power("Vulnerable", 2)],
        )
        card = Card(
            "Reckless Charge",
            "Reckless Charge",
            CardType.ATTACK,
            CardRarity.UNCOMMON,
            damage=7,
        )
        card.base_damage = 7
        game = GameStub(
            [target],
            powers=[power("Strength", 3), power("Weak", 2)],
        )

        # The bridge reports floor((7 + 3) * .75) == 7, whereas the game
        # resolves floor((7 + 3) * .75 * 1.5) == 11 against Vulnerable.
        self.assertEqual(
            11,
            combat_predictor.card_attack_hp_loss(game, card, target),
        )

    def test_perfected_strike_reconstructs_dynamic_pre_weak_packet(self):
        target = monster(
            "Champ", 129, powers=[power("Vulnerable", 1)],
        )
        perfected = Card(
            "Perfected Strike", "Perfected Strike",
            CardType.ATTACK, CardRarity.COMMON, damage=15,
        )
        perfected.base_damage = 6
        perfected.magic_number = 3
        game = GameStub(
            [target], powers=[power("Weakened", 2)],
        )
        game.deck = [
            *[
                Card(
                    "Strike_R", "Strike",
                    CardType.ATTACK, CardRarity.BASIC, damage=4,
                )
                for _ in range(3)
            ],
            perfected,
            Card(
                "Pommel Strike", "Pommel Strike",
                CardType.ATTACK, CardRarity.COMMON, damage=7,
            ),
        ]

        # The unweakened packet is 6 + 3 * 5 == 21. The bridge exposes
        # floor(21 * .75) == 15, but the game applies Vulnerable before the
        # final truncation: floor(21 * .75 * 1.5) == 23.
        self.assertEqual(
            23,
            combat_predictor.card_attack_hp_loss(game, perfected, target),
        )

    def test_bronze_scales_reflects_every_attack_hit_even_when_blocked(self):
        attacker = monster("attacker", 3, damage=5, hits=2)
        game = GameStub(
            [attacker],
            block=5,
            relics=[Relic("Bronze Scales", "Bronze Scales", counter=3)],
        )

        # Bronze Scales is a Thorns trigger, not an HP-loss trigger: the
        # first fully blocked hit still reflects three direct damage and kills
        # the attacker before its second hit.
        self.assertEqual(
            0,
            combat_predictor.projected_attack_hp_loss(game),
        )

    def test_bronze_scales_kill_stops_remaining_multihit_attacks(self):
        attacker = monster("attacker", 3, damage=5, hits=3)
        game = GameStub(
            [attacker],
            relics=[Relic("Bronze Scales", "Bronze Scales", counter=3)],
        )

        # One hit reflects three and ends a three-hit attacker.
        self.assertEqual(
            5,
            combat_predictor.projected_attack_hp_loss(game),
        )

    def test_bronze_scales_reaction_is_absorbed_by_enemy_block(self):
        attacker = monster("attacker", 3, damage=5, hits=2)
        attacker.block = 3
        game = GameStub(
            [attacker],
            relics=[Relic("Bronze Scales", "Bronze Scales", counter=3)],
        )

        # The first reflected packet removes the enemy's three Block.  Only
        # the second packet kills, so both declared attack hits resolve.
        self.assertEqual(
            10,
            combat_predictor.projected_attack_hp_loss(game),
        )

    def test_player_thorns_reaction_is_absorbed_by_enemy_block(self):
        attacker = monster("attacker", 3, damage=5, hits=2)
        attacker.block = 3
        game = GameStub([attacker])
        game.player.powers = [power("Thorns", 3)]

        self.assertEqual(
            10,
            combat_predictor.projected_attack_hp_loss(game),
        )

    def test_serialized_bronze_scales_thorns_is_not_counted_twice(self):
        attacker = monster("attacker", 10, damage=5, hits=3)
        game = GameStub(
            [attacker],
            relics=[Relic("Bronze Scales", "Bronze Scales", counter=3)],
        )
        # CommunicationMod exposes Bronze Scales both as the relic and as the
        # authoritative Thorns power. Three hits deal nine reflected damage;
        # a duplicated relic packet would incorrectly stop the third hit.
        game.player.powers = [power("Thorns", 3)]

        self.assertEqual(
            15,
            combat_predictor.projected_attack_hp_loss(game),
        )

    def test_bronze_scales_and_caltrops_are_one_intangible_packet(self):
        attacker = monster(
            "attacker",
            3,
            damage=5,
            hits=5,
            powers=[power("Intangible", 1)],
        )
        game = GameStub(
            [attacker],
            relics=[Relic("Bronze Scales", "Bronze Scales", counter=3)],
        )
        # Bronze Scales and Caltrops stack in the same ThornsPower. Even at
        # amount six, Intangible caps that one DamageAction to one per hit.
        game.player.powers = [power("Thorns", 6)]

        self.assertEqual(
            15,
            combat_predictor.projected_attack_hp_loss(game),
        )

    def test_bronze_fallback_merges_with_branch_caltrops_override(self):
        attacker = monster(
            "attacker",
            3,
            damage=5,
            hits=5,
            powers=[power("Intangible", 1)],
        )
        game = GameStub(
            [attacker],
            relics=[Relic("Bronze Scales", "Bronze Scales", counter=3)],
        )

        # A branch which played base Caltrops owns one effective six-point
        # Thorns stack. The relic fallback must not add a second packet.
        self.assertEqual(
            15,
            combat_predictor._projected_attack_outcome(
                game, player_thorns_override=6
            )[0],
        )

    def test_flame_barrier_is_separate_from_intangible_thorns_packet(self):
        attacker = monster(
            "attacker",
            3,
            damage=5,
            hits=5,
            powers=[power("Intangible", 1)],
        )
        game = GameStub(
            [attacker],
            relics=[Relic("Bronze Scales", "Bronze Scales", counter=3)],
        )
        game.player.powers = [
            power("Thorns", 3),
            power("FlameBarrierPower", 4),
        ]

        # Each power queues its own THORNS DamageAction. Intangible reduces
        # both to one, so the attacker dies after two hits rather than three.
        self.assertEqual(
            10,
            combat_predictor.projected_attack_hp_loss(game),
        )

    def test_calipers_uses_full_block_for_current_enemy_attack(self):
        attacker = monster("attacker", 30, damage=20)
        game = GameStub(
            [attacker],
            block=0,
            relics=[Relic("Calipers", "Calipers")],
        )

        # Calipers changes the next start-of-turn reset.  All 20 Block still
        # protects against the current enemy attack; only 5 is retained after
        # that attack when no Block was consumed.
        self.assertEqual(
            20,
            combat_predictor.projected_end_block(game, extra_block=20),
        )
        self.assertEqual(
            0,
            combat_predictor.projected_attack_hp_loss(game, extra_block=20),
        )
        self.assertEqual(
            5,
            combat_predictor.calipers_retained_block(game, 20),
        )

    def test_calipers_does_not_haircut_barricade_or_blur(self):
        attacker = monster("attacker", 30, damage=20)
        for power_id in ("Barricade", "Blur"):
            with self.subTest(power_id=power_id):
                game = GameStub(
                    [attacker],
                    relics=[Relic("Calipers", "Calipers")],
                    powers=[power(power_id, 1)],
                )
                self.assertEqual(
                    20,
                    combat_predictor.projected_end_block(
                        game, extra_block=20
                    ),
                )

    def test_countered_relic_packets_are_exposed_to_the_planner(self):
        game = GameStub(
            [],
            relics=[
                Relic("Akabeko", "Akabeko", counter=1),
                Relic("Letter Opener", "Letter Opener", counter=2),
                Relic("Charon's Ashes", "Charon's Ashes"),
            ],
        )

        self.assertEqual(8, combat_predictor.akabeko_bonus(game))
        self.assertEqual(5, combat_predictor.letter_opener_damage(game, 0))
        self.assertEqual(3, combat_predictor.charons_ashes_damage(game))

    def test_non_attack_packets_do_not_receive_target_vulnerable(self):
        target = monster(
            "target",
            30,
            block=5,
            powers=[power("Vulnerable", 1)],
        )

        self.assertEqual(
            5,
            combat_predictor.attack_hp_loss(
                target,
                10,
                vulnerable_eligible=False,
            ),
        )

        # Passive Lightning uses the same block/intangible utility path but
        # is not an Attack and therefore stays at ten raw damage.
        game = GameStub(
            [target],
            orbs=[Orb("Lightning", "Lightning", 30, 10)],
        )
        self.assertEqual(5, combat_predictor._lightning_damage(game, target, 1))

    def test_x_cost_attack_profile_can_use_branch_energy(self):
        game = GameStub([monster("target", 30)])
        attacks = (
            Card(
                "Whirlwind",
                "Whirlwind",
                CardType.ATTACK,
                CardRarity.UNCOMMON,
                cost=-1,
                damage=5,
            ),
            Card(
                "Skewer",
                "Skewer",
                CardType.ATTACK,
                CardRarity.UNCOMMON,
                cost=-1,
                damage=7,
            ),
        )

        for attack in attacks:
            with self.subTest(card=attack.card_id):
                self.assertEqual(
                    (0, 0),
                    combat_predictor.card_attack_profile(
                        game, attack, energy_override=0
                    ),
                )

        game.relics = [Relic("Chemical X", "Chemical X")]
        self.assertEqual(
            (10, 2),
            combat_predictor.card_attack_profile(
                game, attacks[0], energy_override=0
            ),
        )
        self.assertEqual(
            (14, 2),
            combat_predictor.card_attack_profile(
                game, attacks[1], energy_override=0
            ),
        )

    def test_barrage_counts_only_occupied_orb_slots(self):
        barrage = Card(
            "Barrage",
            "Barrage",
            CardType.ATTACK,
            CardRarity.COMMON,
            damage=4,
        )
        game = GameStub(
            [monster("target", 30)],
            orbs=[
                Orb("Lightning", "Lightning", 8, 3),
                Orb("Empty", "Empty", 0, 0),
                Orb("Empty", "Empty", 0, 0),
            ],
        )

        self.assertEqual((4, 1), combat_predictor.card_attack_profile(game, barrage))


if __name__ == "__main__":
    unittest.main()
