import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai import combat_predictor
from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.communication.action import (
    CardSelectAction,
    ChooseAction,
    EndTurnAction,
    PlayCardAction,
)
from spirecomm.spire.card import Card, CardRarity, CardType
from spirecomm.spire.character import Intent, PlayerClass
from spirecomm.spire.relic import Relic
from spirecomm.spire.screen import GridSelectScreen

from test_combat_survival_regressions import (
    GameStub,
    PrioritiesStub,
    card,
    monster,
)
from test_relic_policy_regressions import MacroGameStub
from test_live_macro_decisions import (
    LiveDecisionGame,
    defend as live_defend,
    event_screen,
    make_card as live_card,
    strike as live_strike,
)
from spirecomm.ai.agent import SimpleAgent
import strategy_audit
from test_strategy_audit import decision


class RelicCombatStrategyRegressionTests(unittest.TestCase):
    def setUp(self):
        self.planner = FastCombatPlanner(PrioritiesStub())

    def test_hovering_kite_first_discard_funds_follow_up_card(self):
        enemy = monster(
            "JawWorm", 40, intent=Intent.ATTACK, damage=8, hits=1
        )
        survivor = card("Survivor", CardType.SKILL, cost=1, block=8)
        strike = card(
            "Strike_G", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub([enemy], [survivor, strike], energy=1)
        game.cards_discarded_this_turn = 0
        game.relics = [Relic("HoveringKite", "Hovering Kite")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        search = self.planner.last_decision["search"]
        self.assertEqual(1, search["hovering_kite_energy_gained"])
        # Survivor opens a discard choice, so the named plan correctly stops
        # for an authoritative re-plan; the gained Energy must survive that
        # boundary and fund the visible follow-up in the refreshed frame.
        self.assertEqual(1, search["remaining_energy"])

    def test_hovering_kite_does_not_trigger_after_prior_discard(self):
        enemy = monster(
            "JawWorm", 40, intent=Intent.ATTACK, damage=8, hits=1
        )
        survivor = card("Survivor", CardType.SKILL, cost=1, block=8)
        strike = card(
            "Strike_G", CardType.ATTACK,
            cost=1, damage=6, target=True,
        )
        game = GameStub([enemy], [survivor, strike], energy=1)
        game.cards_discarded_this_turn = 1
        game.relics = [Relic("HoveringKite", "Hovering Kite")]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertEqual(0, search["hovering_kite_energy_gained"])
        self.assertEqual(
            1, len(self.planner.last_decision["planned_sequence"])
        )

    def test_pocketwatch_preserves_three_card_budget(self):
        enemy = monster("JawWorm", 80)
        attacks = []
        for index in range(4):
            attack = card(
                f"Poke-{index}", CardType.ATTACK,
                cost=0, damage=1, target=True,
            )
            attack.uuid = f"poke-{index}"
            attacks.append(attack)
        game = GameStub([enemy], attacks, energy=0)
        game.relics = [Relic("Pocketwatch", "Pocketwatch")]

        self.planner.choose_card_action(game)

        search = self.planner.last_decision["search"]
        self.assertTrue(search["pocketwatch_preserved"])
        self.assertEqual(3, search["pocketwatch_cards_played"])
        self.assertEqual(
            3, len(self.planner.last_decision["planned_sequence"])
        )

    def test_ice_cream_end_records_retained_energy(self):
        defend = card("Defend_G", CardType.SKILL, cost=1, block=5)
        game = GameStub([monster("JawWorm", 40)], [defend], energy=3)
        game.relics = [Relic("Ice Cream", "Ice Cream")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(
            3,
            self.planner.last_decision["search"][
                "ice_cream_energy_retained"
            ],
        )

    def test_runtime_snake_skull_id_adds_poison(self):
        enemy = monster("JawWorm", 30)
        poison = card(
            "Deadly Poison", CardType.SKILL,
            cost=0, magic=5, target=True,
        )
        game = GameStub([enemy], [poison], energy=0)
        game.relics = [Relic("Snake Skull", "Snecko Skull")]

        self.planner.choose_card_action(game)

        self.assertEqual(
            [24], self.planner.last_decision["search"]["final_enemy_hp"]
        )

    def test_art_of_war_rejects_negligible_attack_progress(self):
        enemy = monster("JawWorm", 40)
        poke = card(
            "Poke", CardType.ATTACK, cost=0, damage=1, target=True
        )
        game = GameStub([enemy], [poke], energy=0)
        game.relics = [Relic("Art of War", "Art of War")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        search = self.planner.last_decision["search"]
        self.assertTrue(search["art_of_war_preserved"])
        self.assertEqual(0, search["art_of_war_attacks_played"])

    def test_stone_calendar_turn_seven_damage_is_in_terminal_state(self):
        enemy = monster(
            "JawWorm", 40, intent=Intent.ATTACK, damage=20, hits=1
        )
        defend = card("Defend_G", CardType.SKILL, cost=1, block=5)
        game = GameStub([enemy], [defend], hp=10, energy=1)
        game.turn = 7
        game.relics = [Relic("StoneCalendar", "Stone Calendar")]

        action = self.planner.choose_card_action(game)

        self.assertIsInstance(action, EndTurnAction)
        search = self.planner.last_decision["search"]
        self.assertTrue(search["true_combat_end"])
        self.assertEqual(52, search["stone_calendar_damage"])
        self.assertEqual([0], search["final_enemy_hp"])


class HoveringKiteMacroRegressionTests(unittest.TestCase):
    def make_agent(self):
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
        agent.game = MacroGameStub()
        agent.game.deck = [
            card("Strike_G", CardType.ATTACK),
            card("Defend_G", CardType.SKILL, block=5),
            card("Survivor", CardType.SKILL, block=8),
        ] + [
            card(f"Deck-{index}", CardType.SKILL)
            for index in range(17)
        ]
        return agent

    def test_single_survivor_makes_hovering_kite_unreliable(self):
        agent = self.make_agent()
        profile = agent._deck_profile()

        kite = agent._hovering_kite_profile(profile)

        self.assertLess(kite["estimated_trigger_probability"], 0.3)
        self.assertLess(kite["local_reliability_adjustment"], -14)

    def test_owned_hovering_kite_energy_support_is_conditional(self):
        agent = self.make_agent()
        agent.game.relics = [Relic("HoveringKite", "Hovering Kite")]

        profile = agent._deck_profile()

        self.assertEqual(["HoveringKite"], profile["energy_relic_ids"])
        self.assertGreater(profile["energy_relic_support"], 0)
        self.assertLess(profile["energy_relic_support"], 0.3)


class RelicCoverageRegistryRegressionTests(unittest.TestCase):
    def test_brimstone_is_decision_modeled_for_strategy_audit(self):
        coverage = combat_predictor.relic_strategy_coverage({
            "relics": [{"id": "Brimstone"}]
        })

        self.assertEqual(2, coverage["coverage_contract_version"])
        self.assertEqual([], coverage["unsupported_relic_ids"])
        self.assertEqual([], coverage["unclassified_relic_ids"])
        self.assertEqual(
            "symmetric_strength_growth",
            coverage["decision_handlers"]["brimstone"],
        )
        self.assertIn(
            coverage["decision_handlers"]["brimstone"],
            combat_predictor.NEGATIVE_RELIC_STRATEGY_HANDLERS,
        )

    def test_centennial_puzzle_has_first_hp_loss_strategy_handler(self):
        coverage = combat_predictor.relic_strategy_coverage({
            "relics": [{"id": "Centennial Puzzle"}]
        })

        self.assertEqual([], coverage["heuristic_relic_ids"])
        self.assertEqual(
            "first_hp_loss_draw_value",
            coverage["decision_handlers"]["centennial puzzle"],
        )

    def test_red_skull_has_state_reflected_threshold_handler(self):
        coverage = combat_predictor.relic_strategy_coverage({
            "relics": [{"id": "Red Skull"}]
        })

        self.assertEqual([], coverage["heuristic_relic_ids"])
        self.assertEqual(
            "state_reflected_half_hp_strength",
            coverage["decision_handlers"]["red skull"],
        )

        report = strategy_audit.audit_records([
            decision(
                relic_ids_before=["Red Skull"],
                hp_before=30,
                projected_hp_loss_before=0,
                player_before={
                    "current_hp": 30, "max_hp": 80, "block": 0,
                    "energy": 3,
                    "powers": [{"id": "Strength", "amount": 3}],
                    "orbs": [],
                },
                hand_before=[],
                monsters_before=[{
                    "id": "JawWorm", "current_hp": 40,
                    "is_gone": False, "half_dead": False, "powers": [],
                }],
            )
        ], "new")
        relic = report["relic_strategy_audit"]["per_relic"]["red skull"]
        self.assertEqual("decision_modeled", relic["category"])
        self.assertEqual("clear", relic["status"])
        self.assertEqual(1, relic["evaluated"])

    def test_observed_runtime_aliases_are_not_unclassified(self):
        # These ids were all previously emitted as unclassified by the
        # 89-attempt autonomous corpus even though they are base-game relics.
        observed_ids = (
            "Fusion Hammer", "Blue Candle", "Empty Cage", "Prayer Wheel",
            "Red Mask", "Du-Vu Doll", "Gremlin Horn", "Incense Burner",
            "Dream Catcher", "The Courier", "Question Card", "MawBank",
            "Paper Crane", "Pocketwatch", "Matryoshka", "Toxic Egg 2",
            "Dead Branch", "HoveringKite", "White Beast Statue",
            "Juzu Bracelet", "Tiny House", "Peace Pipe", "Spirit Poop",
            "Shovel", "Singing Bowl", "Art of War", "Calling Bell",
            "Nloth's Gift", "StoneCalendar", "War Paint", "Enchiridion",
            "Unceasing Top", "Champion Belt", "The Specimen", "WristBlade",
            "Mark of Pain", "Ninja Scroll", "Tough Bandages", "Brimstone",
            "WingedGreaves",
        )

        coverage = combat_predictor.relic_model_coverage({
            "relics": [{"id": relic_id} for relic_id in observed_ids]
        })

        self.assertEqual([], coverage["unclassified_relic_ids"])
        self.assertEqual(
            [],
            combat_predictor.relic_coverage_contract_violations()[
                "overlaps"
            ],
        )

    def test_target_run_relics_have_strategy_classification(self):
        target_relics = (
            "Ring of the Snake", "Pocketwatch", "Juzu Bracelet",
            "Bottled Lightning", "Odd Mushroom", "Nloth's Gift",
            "HoveringKite", "Mark of the Bloom", "Calipers", "Nunchaku",
        )

        coverage = combat_predictor.relic_strategy_coverage({
            "relics": [{"id": relic_id} for relic_id in target_relics]
        })

        self.assertEqual([], coverage["unsupported_relic_ids"])
        self.assertEqual([], coverage["unclassified_relic_ids"])
        self.assertIn("pocketwatch", coverage["decision_handlers"])
        self.assertEqual(
            "first_discard_energy",
            coverage["decision_handlers"]["hoveringkite"],
        )

    def test_bird_faced_urn_is_run_level_sustain_classified(self):
        coverage = combat_predictor.relic_strategy_coverage({
            "relics": [{"id": "Bird Faced Urn"}]
        })

        self.assertEqual(["bird faced urn"], coverage["noncombat_relic_ids"])
        self.assertEqual([], coverage["unclassified_relic_ids"])

    def test_specimen_random_transfer_is_nonblocking_heuristic(self):
        coverage = combat_predictor.relic_strategy_coverage({
            "relics": [
                {"id": "Mummified Hand"},
                {"id": "The Specimen"},
            ]
        })

        self.assertEqual(
            {"the specimen"},
            set(coverage["heuristic_relic_ids"]),
        )
        self.assertEqual([], coverage["unsupported_relic_ids"])
        self.assertEqual(
            "random_hand_cost_zero_after_power",
            coverage["decision_handlers"]["mummified hand"],
        )

    def test_random_follow_up_relics_stay_explicitly_heuristic(self):
        coverage = combat_predictor.relic_strategy_coverage({
            "relics": [
                {"id": "Gremlin Horn"},
                {"id": "Unceasing Top"},
            ]
        })

        self.assertEqual(
            {"gremlin horn", "unceasing top"},
            set(coverage["heuristic_relic_ids"]),
        )

    def test_lizard_tail_is_combat_heuristic_not_noncombat(self):
        coverage = combat_predictor.relic_strategy_coverage({
            "relics": [{"id": "Lizard Tail"}]
        })

        self.assertEqual(
            ["lizard tail"], coverage["heuristic_relic_ids"]
        )
        self.assertEqual([], coverage["noncombat_relic_ids"])
        report = strategy_audit.audit_records([
            decision(relic_ids_before=["Lizard Tail"])
        ], "new")
        relic = report["relic_strategy_audit"]["per_relic"][
            "lizard tail"
        ]
        self.assertEqual("heuristic", relic["category"])
        self.assertEqual("inconclusive", relic["status"])


class LiveDiscoveryRegressionTests(unittest.TestCase):
    def test_time_eater_forced_end_uses_plan_loss_not_zero_card_self_cost(self):
        attack = {
            "card_instance_id": "cleave-1",
            "id": "Cleave",
            "type": "ATTACK",
            "cost": 1,
            "is_playable": True,
            "base_block": -1,
            "block": 0,
            "damage": 15,
            "upgrades": 0,
        }
        record = decision(
            action="play",
            card_instance_id="cleave-1",
            card_id="Cleave",
            hp_before=40,
            hp_after=20,
            decision_outcome={"hp_delta": -20},
            hand_before=[attack],
            decision={
                "card_self_hp_cost": 0,
                "search": {
                    "actual_loss": 20,
                    "forced_end": True,
                },
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "card_play_damage_underprediction", report["issue_counts"]
        )
        self.assertEqual(
            "clear",
            report["audit_coverage"][
                "card_play_damage_underprediction"
            ]["status"],
        )

    def test_drug_dealer_prices_mutagenic_strength_from_exact_outcome(self):
        deck = [
            live_strike(0), live_strike(1),
            live_card("Inflame", card_type=CardType.POWER),
            live_card(
                "Heavy Blade", card_type=CardType.ATTACK,
                rarity=CardRarity.UNCOMMON,
            ),
            live_card(
                "Twin Strike", card_type=CardType.ATTACK,
                rarity=CardRarity.COMMON,
            ),
        ]
        game = LiveDecisionGame(
            event_screen(
                "Drug Dealer",
                ["试一下J.A.X.", "当一下实验对象", "喝一下突变剂"],
            ),
            deck=deck,
            act=2,
            floor=19,
            hp=80,
            max_hp=80,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(2, action.choice_index)
        self.assertEqual(
            "drug_dealer_exact_outcome_value",
            agent.last_noncombat_decision["reason"],
        )
        rows = {
            row["semantic_kind"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertEqual(
            "drug_dealer_transform_two",
            rows["transform_two"]["consequences"]["operation"],
        )
        self.assertGreater(
            rows["take_mutagenic_strength"]["score"],
            rows["transform_two"]["score"],
        )
        self.assertIsNone(agent.pending_selection_context)

    def test_drug_dealer_transform_binds_unlabeled_two_card_grid(self):
        deck = [live_strike(0), live_strike(1), live_defend(0)]
        game = LiveDecisionGame(
            event_screen(
                "Drug Dealer",
                ["J.A.X.", "Test Subject", "Mutagens"],
            ),
            deck=deck,
            act=2,
            floor=19,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        with (
            patch.object(
                agent, "_relic_acquisition_score", return_value=-100.0
            ),
            patch.object(agent, "_card_reward_score", return_value=-100.0),
        ):
            event_action = agent.get_next_action_in_game(game)

        self.assertEqual(1, event_action.choice_index)
        self.assertEqual(
            "destructive_card",
            agent.pending_selection_context["kind"],
        )
        game.set_screen(GridSelectScreen(
            cards=deck,
            selected_cards=[],
            num_cards=2,
            any_number=False,
            confirm_up=False,
            for_upgrade=False,
            for_transform=False,
            for_purge=False,
        ))

        grid_action = agent.get_next_action_in_game(game)

        self.assertIsInstance(grid_action, CardSelectAction)
        self.assertEqual(2, len(grid_action.cards))
        self.assertEqual(
            "grid_remove_or_transform_weakest",
            agent.last_noncombat_decision["reason"],
        )
        self.assertTrue(all(
            row["consequences"]["operation"] == "grid_transform"
            for row in agent.last_noncombat_decision["candidates"]
        ))
        for selected in grid_action.cards:
            agent.confirm_card_selection(game, selected.uuid)
        self.assertIsNone(agent.pending_selection_context)


class DeckAwareRemovalRegressionTests(unittest.TestCase):
    @staticmethod
    def deck_card(
        card_id, card_type, index, *, rarity=CardRarity.COMMON,
        damage=0, block=0,
    ):
        return Card(
            card_id,
            card_id,
            card_type,
            rarity,
            cost=1,
            uuid=f"{card_id}-{index}",
            damage=damage,
            base_damage=damage,
            block=block,
            base_block=block,
        )

    def target_run_deck(self):
        make = self.deck_card
        return [
            make(
                "Strike_R", CardType.ATTACK, index,
                rarity=CardRarity.BASIC, damage=6,
            )
            for index in range(2)
        ] + [
            make(
                "Defend_R", CardType.SKILL, index,
                rarity=CardRarity.BASIC, block=5,
            )
            for index in range(4)
        ] + [
            make(
                "Bash", CardType.ATTACK, 0,
                rarity=CardRarity.BASIC, damage=10,
            ),
            make("Shrug It Off", CardType.SKILL, 0, block=8),
            make("Shrug It Off", CardType.SKILL, 1, block=8),
            make("Perfected Strike", CardType.ATTACK, 0, damage=6),
            make("Perfected Strike", CardType.ATTACK, 1, damage=6),
            make("Bludgeon", CardType.ATTACK, 0, damage=32),
            make("Flex", CardType.SKILL, 0),
            make("Inflame", CardType.POWER, 0),
            make("Twin Strike", CardType.ATTACK, 0, damage=5),
            make("Impervious", CardType.SKILL, 0, block=30),
            make("Disarm", CardType.SKILL, 0),
        ]

    def test_transform_two_rebalances_after_first_selected_card(self):
        deck = self.target_run_deck()
        screen = GridSelectScreen(
            cards=deck,
            selected_cards=[],
            num_cards=2,
            any_number=False,
            confirm_up=False,
            for_upgrade=False,
            for_transform=True,
            for_purge=False,
        )
        game = LiveDecisionGame(screen, deck=deck, act=2, floor=19)
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual(
            ["Defend_R", "Strike_R"],
            sorted(selected.card_id for selected in action.cards),
        )
        batch = agent.last_noncombat_decision["removal_batch"]
        self.assertEqual(2, batch["selection_count"])
        self.assertEqual(
            ["Defend_R", "Strike_R"], sorted(batch["chosen_card_ids"])
        )
        self.assertEqual(
            "grid_stateful_removal_batch_inclusion_utility",
            agent.last_noncombat_decision["candidate_contract"][
                "score_source"
            ],
        )
        for candidate in agent.last_noncombat_decision["candidates"]:
            evaluation = candidate["consequences"]["score_components"][
                "removal_evaluation"
            ]
            self.assertIn("attack_sources", evaluation["role_context"])
            self.assertIn("defense_sources", evaluation["role_context"])

    def test_attack_scarcity_can_make_defend_the_better_purge(self):
        for player_class, suffix in (
            (PlayerClass.THE_SILENT, "G"),
            (PlayerClass.IRONCLAD, "R"),
            (PlayerClass.DEFECT, "B"),
        ):
            with self.subTest(player_class=player_class):
                strike = self.deck_card(
                    f"Strike_{suffix}", CardType.ATTACK, 0,
                    rarity=CardRarity.BASIC, damage=6,
                )
                defend = self.deck_card(
                    f"Defend_{suffix}", CardType.SKILL, 0,
                    rarity=CardRarity.BASIC, block=5,
                )
                deck = [
                    strike,
                    self.deck_card(
                        "Class Attack", CardType.ATTACK, 0,
                        rarity=CardRarity.BASIC, damage=10,
                    ),
                    defend,
                ] + [
                    self.deck_card(
                        "Reliable Block", CardType.SKILL, index, block=8,
                    )
                    for index in range(5)
                ]
                game = LiveDecisionGame(
                    GridSelectScreen(
                        cards=[strike, defend], selected_cards=[],
                        num_cards=1, any_number=False, confirm_up=False,
                        for_upgrade=False, for_transform=False,
                        for_purge=True,
                    ),
                    deck=deck,
                    act=2,
                    floor=25,
                )
                agent = SimpleAgent(player_class)

                action = agent.get_next_action_in_game(game)

                self.assertEqual(
                    [f"Defend_{suffix}"],
                    [card.card_id for card in action.cards],
                )

    def test_upgrade_priority_uses_engine_support_for_all_characters(self):
        cases = (
            (
                PlayerClass.THE_SILENT,
                self.deck_card("Catalyst", CardType.SKILL, 0),
                self.deck_card(
                    "Backflip", CardType.SKILL, 0, block=5,
                ),
            ),
            (
                PlayerClass.IRONCLAD,
                self.deck_card("Limit Break", CardType.SKILL, 0),
                self.deck_card(
                    "Shrug It Off", CardType.SKILL, 0, block=8,
                ),
            ),
            (
                PlayerClass.DEFECT,
                self.deck_card("Defragment", CardType.POWER, 0),
                self.deck_card(
                    "Glacier", CardType.SKILL, 0, block=7,
                ),
            ),
        )
        for player_class, unsupported, reliable in cases:
            with self.subTest(player_class=player_class):
                deck = [unsupported, reliable] + [
                    self.deck_card(
                        "Filler Attack", CardType.ATTACK, index, damage=6,
                    )
                    for index in range(4)
                ]
                game = LiveDecisionGame(
                    GridSelectScreen(
                        cards=[unsupported, reliable], selected_cards=[],
                        num_cards=1, any_number=False, confirm_up=False,
                        for_upgrade=True, for_transform=False,
                        for_purge=False,
                    ),
                    deck=deck,
                    act=2,
                    floor=25,
                )
                agent = SimpleAgent(player_class)

                action = agent.get_next_action_in_game(game)

                self.assertEqual([reliable.card_id], [
                    card.card_id for card in action.cards
                ])
                candidates = agent.last_noncombat_decision["candidates"]
                for candidate in candidates:
                    self.assertIn(
                        "upgrade_evaluation",
                        candidate["consequences"]["score_components"],
                    )

    def test_bottled_skill_avoids_unsupported_payoff_for_all_characters(self):
        cases = (
            (
                PlayerClass.THE_SILENT,
                self.deck_card("Catalyst", CardType.SKILL, 0),
                self.deck_card("Backflip", CardType.SKILL, 0, block=5),
            ),
            (
                PlayerClass.IRONCLAD,
                self.deck_card("Limit Break", CardType.SKILL, 0),
                self.deck_card("Shrug It Off", CardType.SKILL, 0, block=8),
            ),
            (
                PlayerClass.DEFECT,
                self.deck_card("Fission", CardType.SKILL, 0),
                self.deck_card("Coolheaded", CardType.SKILL, 0, block=5),
            ),
        )
        for player_class, unsupported, reliable in cases:
            with self.subTest(player_class=player_class):
                game = LiveDecisionGame(
                    GridSelectScreen(
                        cards=[unsupported, reliable], selected_cards=[],
                        num_cards=1, any_number=False, confirm_up=False,
                        for_upgrade=False, for_transform=False,
                        for_purge=False,
                    ),
                    deck=[unsupported, reliable] + [
                        self.deck_card(
                            "Bottle Filler", CardType.ATTACK, index,
                            damage=6,
                        )
                        for index in range(4)
                    ],
                    act=2,
                    floor=25,
                )
                agent = SimpleAgent(player_class)
                agent.game = game

                self.assertGreater(
                    agent._bottled_lightning_opening_score(reliable),
                    agent._bottled_lightning_opening_score(unsupported),
                )

    def test_generic_event_deck_operations_are_contextual_for_all_characters(self):
        for player_class in (
            PlayerClass.THE_SILENT,
            PlayerClass.IRONCLAD,
            PlayerClass.DEFECT,
        ):
            with self.subTest(player_class=player_class):
                curse = self.deck_card(
                    "Normality", CardType.CURSE, 0,
                    rarity=CardRarity.CURSE,
                )
                deck = [curse] + [
                    self.deck_card(
                        f"Useful {index}", CardType.SKILL, index, block=7,
                    )
                    for index in range(5)
                ]
                game = LiveDecisionGame(
                    event_screen(
                        "Modded Cleanup", ["Remove a card", "Leave"]
                    ),
                    deck=deck,
                    act=2,
                    floor=25,
                )
                agent = SimpleAgent(player_class)

                action = agent.get_next_action_in_game(game)

                self.assertEqual(0, action.choice_index)
                remove = next(
                    row
                    for row in agent.last_noncombat_decision["candidates"]
                    if row["choice_index"] == 0
                )
                evaluation = remove["consequences"]["score_components"][
                    "deck_operation_evaluation"
                ]
                self.assertEqual(26.0, evaluation["remove"])
                self.assertEqual(130.0, evaluation["best_removal_score"])
                self.assertNotIn("context_status", evaluation)


class PerRelicStrategyAuditRegressionTests(unittest.TestCase):
    @staticmethod
    def block_card(instance_id="defend-card"):
        return {
            "id": "Defend_G", "card_instance_id": instance_id,
            "type": "SKILL", "cost": 1, "base_block": 8,
            "block": 8, "damage": 0, "is_playable": True,
        }

    def test_centennial_puzzle_audits_only_first_hp_loss_trigger(self):
        records = [
            decision(
                before_seq=10, action="play", card_id="Defend_R",
                card_instance_id="defend-1", hp_before=20, hp_after=20,
                relic_ids_before=["Centennial Puzzle"],
                decision_outcome={"hp_delta": 0, "player_hp_loss": 0},
            ),
            decision(
                before_seq=11, action="end", hp_before=20, hp_after=17,
                projected_hp_loss_before=3,
                projected_attack_hp_loss_before=3,
                relic_ids_before=["Centennial Puzzle"],
                decision_outcome={"hp_delta": -3, "player_hp_loss": 3},
                damage_model={
                    "monsters_to_hero_basis": "end_turn_player_hp_delta",
                    "monsters_to_hero_predicted": 3,
                    "monsters_to_hero_actual": 3,
                },
            ),
            decision(
                before_seq=12, action="end", turn=2,
                phase="COMBAT_TURN_2", hp_before=17, hp_after=14,
                projected_hp_loss_before=3,
                projected_attack_hp_loss_before=3,
                relic_ids_before=["Centennial Puzzle"],
                decision_outcome={"hp_delta": -3, "player_hp_loss": 3},
                damage_model={
                    "monsters_to_hero_basis": "end_turn_player_hp_delta",
                    "monsters_to_hero_predicted": 3,
                    "monsters_to_hero_actual": 3,
                },
            ),
        ]

        report = strategy_audit._relic_strategy_audit_report(records, [])
        relic = report["per_relic"]["centennial puzzle"]

        self.assertEqual("decision_modeled", relic["category"])
        self.assertEqual(1, relic["eligible"])
        self.assertEqual(1, relic["evaluated"])
        self.assertEqual(0, relic["unknown"])
        self.assertEqual("clear", relic["status"])

    def test_sundial_audits_draw_actions_not_unrelated_plays_or_end(self):
        complete_search = {
            "sundial_draw_pile_size": 4,
            "sundial_discard_pile_size": 3,
            "sundial_counter": 2,
            "sundial_shuffle_count": 0,
            "sundial_energy_gained": 0,
        }
        records = [
            decision(
                before_seq=10, action="play", card_id="Strike_R",
                relic_ids_before=["Sundial"],
            ),
            decision(
                before_seq=11, action="end",
                relic_ids_before=["Sundial"],
            ),
            decision(
                before_seq=12, action="play", card_id="Shrug It Off",
                relic_ids_before=["Sundial"],
                decision={"search": complete_search},
            ),
        ]

        report = strategy_audit._relic_strategy_audit_report(records, [])
        relic = report["per_relic"]["sundial"]
        self.assertEqual(3, relic["occurrences"])
        self.assertEqual(1, relic["eligible"])
        self.assertEqual(1, relic["evaluated"])
        self.assertEqual(0, relic["unknown"])
        self.assertEqual("clear", relic["status"])

        missing = strategy_audit._relic_strategy_audit_report([
            decision(
                action="play", card_id="Shrug It Off",
                relic_ids_before=["Sundial"],
            ),
        ], [])["per_relic"]["sundial"]
        self.assertEqual(1, missing["eligible"])
        self.assertEqual(0, missing["evaluated"])
        self.assertEqual(1, missing["unknown"])
        self.assertEqual("inconclusive", missing["status"])

    def test_calipers_resource_violation_is_attributed_once(self):
        record = decision(
            action="end",
            decision={"reason": "no_positive_marginal_action"},
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            projected_block_before=38,
            player_block_before=38,
            player_before={
                "current_hp": 1, "max_hp": 70, "block": 38, "energy": 1,
                "powers": [], "orbs": [],
            },
            relic_ids_before=["Calipers"],
            hand_before=[self.block_card()],
            energy_before=1,
            end_turn_resources={
                "energy_before": 1,
                "playable_card_count": 1,
                "playable_card_ids": ["Defend_G"],
                "reason": "no_positive_marginal_action",
                "safety_class": "evaluated_no_positive_marginal_action",
            },
        )

        report = strategy_audit.audit_records([record], "new")

        relic = report["relic_strategy_audit"]["per_relic"]["calipers"]
        self.assertEqual("issues", relic["status"])
        self.assertEqual(1, relic["violations"])
        self.assertEqual(
            1,
            report["audit_coverage"]["end_turn_with_resources"][
                "violations"
            ],
        )

    def test_pocketwatch_fourth_redundant_card_is_attributed(self):
        records = []
        for index in range(3):
            instance = f"setup-{index}"
            records.append(decision(
                before_seq=7 + index,
                action="play",
                card_id="Setup",
                card_instance_id=instance,
                projected_hp_loss_before=0,
                projected_attack_hp_loss_before=0,
                decision_outcome={"hp_delta": 0, "enemy_hp_loss": 0},
                hand_before=[{
                    "id": "Setup", "card_instance_id": instance,
                    "type": "POWER", "cost": 0, "base_block": 0,
                    "block": 0, "damage": 0, "is_playable": True,
                }],
                relic_ids_before=["Pocketwatch"],
            ))
        records.append(decision(
            before_seq=10,
            action="play",
            card_id="Defend_G",
            card_instance_id="defend-card",
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            projected_block_before=19,
            player_block_before=19,
            player_before={
                "current_hp": 40, "max_hp": 70, "block": 19, "energy": 1,
                "powers": [], "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:collector",
                "id": "TheCollector", "monster_index": 0,
                "current_hp": 190, "max_hp": 282, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 19,
                "move_hits": 1, "is_gone": False, "half_dead": False,
                "powers": [],
            }],
            hand_before=[self.block_card()],
            relic_ids_before=["Pocketwatch"],
        ))

        report = strategy_audit.audit_records(records, "new")

        issue = next(
            item for item in report["issues"]
            if item["kind"] == "redundant_covered_block_play"
        )
        self.assertEqual(4, issue["card_number"])
        relic = report["relic_strategy_audit"]["per_relic"]["pocketwatch"]
        self.assertEqual(1, relic["violations"])
        self.assertEqual("issues", relic["status"])

    def test_ice_cream_safe_end_is_not_unused_resource(self):
        record = decision(
            action="end",
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            hand_before=[self.block_card()],
            energy_before=3,
            relic_ids_before=["Ice Cream"],
            end_turn_resources={
                "energy_before": 3,
                "playable_card_count": 1,
                "playable_card_ids": ["Defend_G"],
                "reason": "no_positive_marginal_action",
                "safety_class": "evaluated_no_positive_marginal_action",
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "end_turn_with_resources", report["issue_counts"]
        )
        self.assertEqual(
            "clear",
            report["relic_strategy_audit"]["per_relic"]["ice cream"][
                "status"
            ],
        )

    def test_specimen_random_transfer_keeps_strategy_audit_nonblocking(self):
        report = strategy_audit.audit_records([
            decision(relic_ids_before=["The Specimen"])
        ], "new")

        relic = report["relic_strategy_audit"]["per_relic"]["the specimen"]
        self.assertEqual("heuristic", relic["category"])
        self.assertGreater(relic["unknown"], 0)
        self.assertEqual("inconclusive", relic["status"])
        self.assertEqual(
            "inconclusive",
            report["audit_coverage"]["relic_strategy_decision"]["status"],
        )


if __name__ == "__main__":
    unittest.main()
