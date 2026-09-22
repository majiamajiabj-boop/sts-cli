"""Whole-decision contracts and independent cost-distribution checks."""
import ast
import copy
import itertools
import json
import unittest
from pathlib import Path
from tests.test_terminal_survival import game_for, SimpleAgent
from tests.test_live_macro_decisions import Node, MapScreen, make_map
from spirecomm.spire.relic import Relic

ROOT = Path(__file__).resolve().parents[1]
FRAMES = json.loads((ROOT / "test_fixtures/survival_system_frames.json").read_text())


def agent_for(seq):
    game = game_for(FRAMES[str(seq)]["frame"])
    agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
    agent.game = game
    return agent


class SurvivalSystemTests(unittest.TestCase):
    def test_logged_recovery_paths_remain_eligible_when_all_routes_are_risky(self):
        # MAP snapshots omit the full graph. Reconstruct only paths explicitly
        # recorded by the producer; do not claim an exact full-map replay.
        agent = agent_for(756010)
        nodes = {}
        def node(label):
            symbol, xy = label.split("@")
            x, y = map(int, xy.split(","))
            return nodes.setdefault((x, y), Node(x, y, symbol))
        entries = []
        for candidate in FRAMES["756010"]["candidates"]:
            route = candidate["consequences"]["route"]
            entry = node(f"{route['symbol']}@{route['x']},{route['y']}")
            entries.append(entry)
            for path in candidate["consequences"]["route_summary"]["legal_path_options"]:
                chain = [node(label) for label in path["sequence"]]
                for depth in path["burning_elite_depths"]:
                    chain[depth].has_emerald_key = True
                for parent, child in zip(chain, chain[1:]):
                    if child not in parent.children:
                        parent.children.append(child)
        current = Node(1, 4, "M")
        current.children = entries
        agent.game.map = make_map(current, *nodes.values())
        agent.game.screen = MapScreen(current, entries, False)
        agent.game.key_system_unlocked = True
        action = agent.make_map_choice()
        self.assertEqual("R", action.node.symbol)
        candidates = agent.last_noncombat_decision["candidates"]
        rest = next(c for c in candidates if c["id"] == "map:1,5")
        self.assertTrue(rest["selection_eligible"])
        self.assertTrue(agent.last_noncombat_decision["emerald_survival_fallback"])

    def test_energy_distribution_matches_independent_enumeration(self):
        agent = agent_for(755678)
        # Small mixed-cost deck makes the exact independent enumeration cheap.
        cards = [copy.copy(agent.game.deck[0]) for _ in range(7)]
        for card, cost in zip(cards, [0, 0, 0, 1, 1, 2, 2]):
            card.cost = cost
        agent.game.deck = cards
        expected = sum(sum(c.cost for c in hand) > 3
                       for hand in itertools.combinations(cards, 5)) / 21
        profile = agent._boss_energy_marginal_profile("Ectoplasm")
        self.assertAlmostEqual(expected, profile["extra_energy_spendable_hand_fraction"], places=3)
        for card in cards:
            card.cost = 0
        self.assertEqual(0, agent._boss_energy_marginal_profile("Ectoplasm")["local_marginal_adjustment"])

    def test_logged_boss_choices_preserve_drawbacks_and_use_payable_energy(self):
        for seq, preferred, other in [
            (755678, "Ectoplasm", "Black Blood"),
            (752139, "Black Star", "Sozu"),
            (757976, "Nuclear Battery", "Fusion Hammer"),
        ]:
            with self.subTest(seq=seq):
                agent = agent_for(seq)
                self.assertGreater(
                    agent._boss_relic_score(Relic(preferred, preferred)),
                    agent._boss_relic_score(Relic(other, other)),
                )
        agent = agent_for(755678)
        self.assertEqual(-1000, agent._boss_relic_score(Relic("Runic Dome", "Runic Dome")))

    def test_known_empty_offense_does_not_invent_act_damage(self):
        agent = agent_for(756029)
        game = agent.game
        for zone in ("hand", "draw_pile", "discard_pile"):
            setattr(game, zone, [])
        profile = agent.combat_planner._combat_progress_profile(game)
        self.assertEqual(0, profile["damage_per_turn"])
        self.assertIsNone(profile["unbounded_turns"])
        self.assertTrue(profile["horizon_truncated"])

    def test_test_methods_are_not_silently_shadowed(self):
        for path in (ROOT / "tests").glob("test_*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for cls in ast.walk(tree):
                if not isinstance(cls, ast.ClassDef):
                    continue
                names = [n.name for n in cls.body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and n.name.startswith("test_")]
                self.assertEqual(len(names), len(set(names)),
                                 f"{path.name}:{cls.name}: shadowed test method")


if __name__ == "__main__":
    unittest.main()
