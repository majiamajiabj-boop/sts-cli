"""Portable, offline regressions from the 24-attempt failure review.

Fixtures retain source identities; these tests replay decisions, not whole wins.
"""
import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai.agent import SimpleAgent
from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.ai.priorities import SilentPriority
from spirecomm.spire.game import Game
from spirecomm.spire.character import Intent
from spirecomm.spire.power import Power
from test_combat_planner_boss import GameStub, PrioritiesStub, make_monster, strike
from spirecomm.spire.character import PlayerClass
from spirecomm.spire.map import Node
from spirecomm.spire.screen import MapScreen
from test_live_macro_decisions import LiveDecisionGame, make_map, event_screen, make_card


FRAMES = json.loads(
    (ROOT / "test_fixtures" / "attempt_failure_frames.json").read_text(encoding="utf-8")
)


def historical_game(seq, *, preserve_screen=False):
    frame = FRAMES[str(seq)]
    raw = copy.deepcopy(frame["game_state"])

    def restore(value):
        if isinstance(value, dict):
            if "card_instance_id" in value:
                value.setdefault("uuid", value["card_instance_id"])
            for item in list(value.values()):
                restore(item)
        elif isinstance(value, list):
            for item in value:
                restore(item)

    restore(raw)
    raw.setdefault("map", [])
    if not preserve_screen:
        raw.update(screen_type="NONE", screen_state={})
    return Game.from_json(raw, frame["available_commands"])


class ChampIntentRegressionTests(unittest.TestCase):
    def test_historical_poison_crossing_keeps_the_observed_seven_damage(self):
        game = historical_game(565716)
        planner = FastCombatPlanner(SilentPriority())
        projection = planner.project_end_turn(game)

        self.assertEqual(18, game.player.current_hp)
        self.assertEqual(11, FRAMES["565716"]["source"]["observed_hp_after"])
        self.assertEqual(7, projection["projected_hp_loss"])
        self.assertEqual([], projection["action_suppressed_enemy_indexes"])

    def test_champ_half_health_does_not_interrupt_the_current_attack(self):
        # Champ.getMove selects Anger for the next intent. Unlike Slime Boss
        # and Guardian, Champ.damage does not replace the current move.
        for source in ("cards", "poison", "combust"):
            with self.subTest(source=source):
                enemy = make_monster("Champ", 221, max_hp=420)
                enemy.intent = Intent.ATTACK
                enemy.move_adjusted_damage = 30
                enemy.move_base_damage = 30
                enemy.move_hits = 1
                hand = [strike(6), strike(6)] if source == "cards" else []
                for index, card in enumerate(hand):
                    card.uuid = f"strike-{index}"
                game = GameStub(enemy, hand, hp=70, energy=2)
                if source == "poison":
                    enemy.powers = [Power("Poison", "Poison", 12)]
                if source == "combust":
                    game.player.powers = [Power("Combust", "Combust", 12)]
                planner = FastCombatPlanner(PrioritiesStub())
                if source == "cards":
                    planner.choose_card_action(game)
                    projection = planner.last_decision["search"]
                else:
                    projection = planner.project_end_turn(game)
                self.assertEqual([209], projection["final_enemy_hp"])
                self.assertEqual([], projection["action_suppressed_enemy_indexes"])
                self.assertEqual(30, projection["projected_attack_hp_loss"])


class RewardDeficitRegressionTests(unittest.TestCase):
    def agent_for_reward(self, seq):
        game = historical_game(seq, preserve_screen=True)
        agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
        agent.game = game
        return agent

    def test_historical_rewards_add_a_missing_damage_plan(self):
        for seq, expected in (
            (558924, "Deadly Poison"),
            (566083, "Darkness"),
            (566228, "Combust"),
        ):
            with self.subTest(seq=seq):
                agent = self.agent_for_reward(seq)
                agent.choose_card_reward()
                self.assertEqual(expected, agent.last_noncombat_decision["chosen"])

    def test_poison_and_dark_sources_reduce_growth_deficit_without_a_payoff(self):
        for seq, expected in ((558924, "Deadly Poison"), (566083, "Darkness")):
            with self.subTest(seq=seq):
                agent = self.agent_for_reward(seq)
                before = agent._boss_entry_readiness()
                offered = next(c for c in agent.game.screen.cards if c.card_id == expected)
                after = agent._boss_entry_readiness(
                    agent._deck_profile(agent.game.deck + [offered])
                )
                self.assertLess(after["gaps"]["scaling"], before["gaps"]["scaling"])
                # One source is progress, not certification of a complete engine.
                self.assertLess(after["coverage"]["scaling"], 0.7)

    def test_an_existing_damage_engine_does_not_receive_duplicate_gap_credit(self):
        agent = self.agent_for_reward(566083)
        darkness = next(c for c in agent.game.screen.cards if c.card_id == "Darkness")
        first_score = agent._card_reward_score(darkness)
        agent.game.deck.append(copy.deepcopy(darkness))
        self.assertEqual(0, agent._card_boss_gap_relief(darkness, agent._deck_profile()))
        self.assertLess(agent._card_reward_score(darkness), first_score)

    def test_unsupported_energy_is_still_skipped_and_gap_credit_is_bounded(self):
        agent = self.agent_for_reward(566083)
        energy = next(c for c in agent.game.screen.cards if c.card_id == "Double Energy")
        self.assertLess(agent._card_reward_score(energy), agent._permanent_card_pick_hurdle())
        for card in agent.game.screen.cards:
            credit = agent._card_boss_gap_relief(card, agent._deck_profile())
            self.assertGreaterEqual(credit, 0)
            self.assertLessEqual(credit, 12)


class RouteAndOptionalFightRegressionTests(unittest.TestCase):
    def test_immediate_shop_remains_actionable_at_critical_hp(self):
        # The historical maps were not retained. Recreate only the observed
        # choice geometry: a rich, critical-HP player can enter a shop now,
        # whose later route remains dangerous.
        for hp, gold, emerald_pending in ((20, 396, True), (10, 359, False)):
            with self.subTest(hp=hp):
                current = Node(0, 3, "?")
                shop = Node(1, 4, "$")
                event = Node(0, 4, "?")
                elite = Node(1, 5, "E", has_emerald_key=emerald_pending)
                current.children = [shop, event]
                shop.children = [elite]
                game = LiveDecisionGame(
                    MapScreen(current, [shop, event], boss_available=False),
                    dungeon_map=make_map(current, shop, event, elite),
                    gold=gold, hp=hp, max_hp=75, act=2, floor=22,
                )
                game.key_system_unlocked = True
                game.has_emerald_key = not emerald_pending
                agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=None)
                agent.game = game
                action = agent.make_map_choice()
                self.assertIs(shop, action.node)
                decision = agent.last_noncombat_decision
                self.assertTrue(decision["high_gold_safe_shop_admission"])
                self.assertGreater(agent._path_survival_risk(shop), hp)
                row = next(c for c in decision["candidates"] if c["label"] == "$@1,4")
                self.assertEqual(1, row["consequences"]["shop_arrival_survival_floor"])

    def test_shop_behind_a_lethal_fight_is_still_unreachable(self):
        current = Node(0, 3, "?")
        fight = Node(0, 4, "M")
        event = Node(1, 4, "?")
        shop = Node(0, 5, "$")
        current.children = [fight, event]
        fight.children = [shop]
        game = LiveDecisionGame(
            MapScreen(current, [fight, event], boss_available=False),
            dungeon_map=make_map(current, fight, event, shop),
            gold=396, hp=10, max_hp=75, act=2, floor=22,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD, macro_advisor=None)
        agent.game = game
        self.assertIs(event, agent.make_map_choice().node)
        self.assertFalse(agent.last_noncombat_decision["high_gold_safe_shop_admission"])

    def bandit_agent(self, hp=None):
        game = historical_game(552731)
        if hp is not None:
            game.current_hp = hp
        game.screen = event_screen("Masked Bandits", ["Pay", "Fight"])
        agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
        agent.game = game
        return agent

    def test_historical_bandits_preserve_a_survival_reserve(self):
        agent = self.bandit_agent()
        agent.choose_event_action(agent.game.screen.options)
        decision = agent.last_noncombat_decision
        self.assertEqual("pay", decision["chosen_kind"])
        profile = decision["masked_bandits_profile"]
        self.assertGreater(profile["scores"]["pay"], profile["scores"]["fight"])
        self.assertGreater(profile["survival_reserve_hp"], 0)
        self.assertEqual("uncalibrated_survival_risk_index", profile["death_risk_semantics"])

    def test_defensive_scaling_does_not_invent_a_faster_damage_engine(self):
        agent = SimpleAgent(PlayerClass.THE_SILENT, macro_advisor=None)
        agent.game = historical_game(558924)
        from spirecomm.spire.card import CardType
        agent.game.deck = [make_card("Wraith Form", card_type=CardType.POWER)]
        defensive = agent._deck_readiness()["kill_clock"]["score"]
        agent.game.deck = [make_card("Noxious Fumes", card_type=CardType.POWER)]
        offensive = agent._deck_readiness()["kill_clock"]["score"]
        self.assertLess(defensive, offensive)

    def test_healthy_bandits_fight_is_not_blanket_banned(self):
        agent = self.bandit_agent(hp=85)
        profile = agent._masked_bandits_decision_profile()
        self.assertGreater(profile["scores"]["fight"], profile["scores"]["pay"])
        self.assertEqual(0, profile["death_risk"])


class HeartPreparationRegressionTests(unittest.TestCase):
    def agent_for(self, seq, preserve_screen=False):
        game = historical_game(seq, preserve_screen=preserve_screen)
        agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
        agent.game = game
        return agent

    def test_act_four_shop_buys_an_affordable_potion_for_the_empty_slot(self):
        from spirecomm.communication.action import BuyPotionAction
        agent = self.agent_for(560379, preserve_screen=True)
        agent.choose_shop_action()  # Remember the actual shop-entry inventory.
        agent.game = historical_game(560381, preserve_screen=True)
        action = agent.choose_shop_action()
        self.assertEqual(1, agent.shop_potions_bought)
        self.assertIsInstance(action, BuyPotionAction)
        self.assertEqual("AttackPotion", action.potion.potion_id)

    def test_final_shop_urgency_does_not_promote_an_ordinary_early_shop(self):
        agent = self.agent_for(560381, preserve_screen=True)
        potion = agent.game.screen.potions[0]
        final_value = agent._shop_potion_value(potion)
        agent.game.act = 2
        agent.game.floor = 19
        early_value = agent._shop_potion_value(potion)
        self.assertGreater(final_value, early_value)

    def test_heart_readiness_requires_damage_progress_even_at_full_health(self):
        agent = self.agent_for(560381)
        # Counterfactual defensive-only deck: plenty of Frost/block/draw is
        # not itself a way to finish the Heart before its damage escalates.
        agent.game.deck = [
            c for c in agent.game.deck if c.card_id in {"Glacier", "Coolheaded"}
        ]
        agent.game.relics = []
        readiness = agent._boss_entry_readiness()
        self.assertEqual("heart", readiness["boss_id"])
        self.assertGreater(readiness["gaps"].get("kill_clock", 0), 0)
        self.assertFalse(readiness["ready"])
        self.assertGreater(readiness["requirements"]["block"], 0.5)

    def test_choker_prices_the_opening_resources_owned_at_selection(self):
        agent = self.agent_for(561885, preserve_screen=True)
        self.assertFalse(agent._has_relic("Dead Branch"))
        self.assertFalse(agent._has_relic("Lantern"))
        profile = agent._velvet_choker_profile()
        self.assertEqual(4, profile["opening_draw_from_relics"])
        self.assertGreater(profile["total_pressure"], 0)
        agent.choose_boss_relic(agent.game.screen.relics)
        self.assertNotEqual("Velvet Choker", agent.last_noncombat_decision["chosen"])

    def test_choker_counts_owned_generation_without_inventing_future_relics(self):
        agent = self.agent_for(562372)
        owned = agent._velvet_choker_profile()
        agent.game.relics = [r for r in agent.game.relics if r.relic_id != "Dead Branch"]
        without_branch = agent._velvet_choker_profile()
        self.assertGreater(owned["generated_card_volume"], without_branch["generated_card_volume"])
        self.assertGreater(owned["total_pressure"], without_branch["total_pressure"])


class AuditAndReceiptRegressionTests(unittest.TestCase):
    def grid_agent(self):
        game = historical_game(564647, preserve_screen=True)
        agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
        agent.game = game
        return agent

    def test_astrolabe_candidates_match_the_independent_visible_consequence(self):
        import autoplay
        import independent_oracle
        agent = self.grid_agent()
        agent.choose_grid_action()
        frame = FRAMES["564647"]
        observed = frame["source"]["observed_transform_results"]
        self.assertEqual(3, len(observed))
        self.assertEqual([1, 1, 1], [c["upgrades"] for c in observed])
        state = {
            "phase": "GRID", "game_state": frame["game_state"],
            "options": frame["options"], "available_commands": frame["available_commands"],
        }
        rows = autoplay.canonical_legal_choices(
            state, {"action": "choose", "choice_index": 0}, agent.last_noncombat_decision,
        )
        self.assertEqual(len(frame["options"]), len(rows))
        record = {"phase": "GRID", "authoritative_state_before": state}
        for row in rows:
            with self.subTest(choice=row["choice_index"]):
                claim = row["producer_consequence_claim"]
                self.assertEqual(1, claim["random_effects"][0]["result_upgrades"])
                expected = independent_oracle._expected_visible_consequence("GRID", row, record)
                contradictions, unresolved = independent_oracle._review_producer_consequence_claim(
                    record, row, row, expected, None,
                )
                self.assertEqual([], contradictions)
                self.assertEqual([], unresolved)

    def test_astrolabe_ownership_does_not_upgrade_other_transform_sources(self):
        changes = (
            {"authority": "inferred"},
            {"parent_phase": "EVENT"},
            {"relic_id": "Pandoras Box"},
            {"select_count": 2},
            {"operation": "remove"},
        )
        for change in changes:
            with self.subTest(change=change):
                agent = self.grid_agent()
                # Three visible options suffice for these parent-binding
                # boundaries; the positive replay above checks all 21.
                agent.game.screen.cards = agent.game.screen.cards[:3]
                agent.game.screen.for_transform = True
                agent.game.screen.parent_choice_context.update(change)
                agent.choose_grid_action()
                for candidate in agent.last_noncombat_decision["candidates"]:
                    for effect in candidate.get("consequences", {}).get("random_effects", []):
                        self.assertNotIn("result_upgrades", effect)

    def test_historical_hand_confirmation_death_completes_the_receipt(self):
        import bridge
        frame = FRAMES["563760"]
        source = frame["source"]
        terminal = frame["terminal_state"]
        self.assertEqual("HAND_SELECT", source["accepted_phase"])
        self.assertEqual(0, terminal["game_state"]["current_hp"])
        pending = {
            "payload": {"action": "proceed"},
            "accepted_phase": source["accepted_phase"],
            "accepted_decision_id": source["accepted_decision_id"],
            "settle_frames": 0,
        }
        # Even a transient not-ready terminal frame must acknowledge the
        # combat confirmation; waiting for MAIN_MENU would misclassify death.
        for ready in (True, False):
            raw = copy.deepcopy(terminal)
            raw["ready_for_command"] = ready
            self.assertEqual("complete", bridge.pending_frame_outcome(pending, terminal, raw))


if __name__ == "__main__":
    unittest.main()
