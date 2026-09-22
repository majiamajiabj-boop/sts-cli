import json
import unittest
from copy import deepcopy
from pathlib import Path
from tests.test_terminal_survival import game_for, SimpleAgent
from tests.test_combat_survival_regressions import card, CardType
from spirecomm.spire.screen import CardRewardScreen

FRAME = json.loads((Path(__file__).resolve().parents[1] / "test_fixtures/conditional_power_reward.json").read_text(encoding="utf-8"))


class ConditionalPowerRewardTests(unittest.TestCase):
    def agent(self):
        raw = FRAME["frame"]
        game = game_for(raw)
        screen = deepcopy(raw["game_state"]["screen_state"])
        for value in screen["cards"]:
            value["uuid"] = value["card_instance_id"]
        game.screen = CardRewardScreen.from_json(screen)
        agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
        agent.game = game
        return agent

    def test_logged_unsupported_heatsinks_loses_to_upgraded_defense(self):
        agent = self.agent()
        self.assertEqual("Conserve Battery", agent.choose_card_reward().card.card_id)
        heat = agent.game.screen.cards[0]
        self.assertEqual(0, agent._card_boss_gap_relief(heat, agent._deck_profile()))

    def test_trigger_consumers_do_not_supply_each_other(self):
        agent = self.agent()
        for name in ("Heatsinks", "Storm", "Heatsinks"):
            agent.game.deck.append(card(name, CardType.POWER))
        support = agent._scaling_mechanism_support(agent._deck_profile())
        self.assertNotIn("powers", support["paired_engines"])

    def test_real_power_generator_enables_payoff(self):
        agent = self.agent()
        agent.game.deck.extend([card("Creative AI", CardType.POWER, cost=3),
                                card("Heatsinks", CardType.POWER)])
        support = agent._scaling_mechanism_support(agent._deck_profile())
        self.assertGreater(support["paired_engines"]["powers"], 0)

    def test_intrinsic_engine_keeps_its_value(self):
        agent = self.agent()
        agent.game.deck.append(card("Echo Form", CardType.POWER, cost=3))
        support = agent._scaling_mechanism_support(agent._deck_profile())
        self.assertEqual(1, support["standalone"]["echoform"])


if __name__ == "__main__":
    unittest.main()
