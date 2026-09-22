import copy
import json
import unittest
from pathlib import Path
from tests.test_terminal_survival import game_for, SimpleAgent
from spirecomm.communication.action import EndTurnAction

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "test_fixtures/shifting_potion_frame.json").read_text(encoding="utf-8"))

def agent_for():
    game = game_for(FIXTURE["frame"])
    agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
    agent.game = game
    agent.combat_planner.last_decision = copy.deepcopy(FIXTURE["decision"])
    return agent

class ShiftingPotionTests(unittest.TestCase):
    def test_historical_lethal_end_uses_fire_and_preserves_input(self):
        agent = agent_for()
        before = copy.deepcopy(agent.game.monsters[0].__dict__)
        proof = agent._hypothetical_shifting_potion_analysis("firepotion", agent.game.monsters[0])
        self.assertEqual(20, proof["loss"])
        self.assertTrue(proof["survives"])
        action = agent.use_best_potion(36, planned_turn_loss=35,
                                      planned_action=EndTurnAction(), planned_combat_end=False)
        self.assertIsNotNone(action)
        self.assertEqual(before, agent.game.monsters[0].__dict__)
        evaluation = agent.combat_planner.last_decision["potion_evaluation"]
        self.assertTrue(evaluation["accepted"])
        self.assertEqual(20, evaluation["candidates"][0]["damage_reaction"]["loss"])

    def test_block_absorbed_damage_does_not_create_strength_reduction(self):
        agent = agent_for()
        agent.game.monsters[0].block = 40
        self.assertIsNone(agent._hypothetical_shifting_potion_analysis("firepotion", agent.game.monsters[0]))

    def test_insufficient_rescue_is_not_reported_as_survival(self):
        agent = agent_for()
        agent.game.current_hp = agent.game.player.current_hp = 10
        proof = agent._hypothetical_shifting_potion_analysis("firepotion", agent.game.monsters[0])
        self.assertFalse(proof["survives"])

    def test_explosive_also_triggers_shifting_without_vulnerable_bonus(self):
        agent = agent_for()
        proof = agent._hypothetical_shifting_potion_analysis("explosivepotion", agent.game.monsters[0])
        self.assertEqual(10, proof["reactions"][0]["hp_loss"])
        self.assertLess(proof["loss"], 35)

    def test_non_shifting_or_lethal_enemy_is_not_given_a_false_reaction_proof(self):
        agent = agent_for()
        agent.game.monsters[0].powers = []
        self.assertIsNone(agent._hypothetical_shifting_potion_analysis("firepotion", agent.game.monsters[0]))
        agent = agent_for()
        agent.game.monsters[0].current_hp = 10
        self.assertIsNone(agent._hypothetical_shifting_potion_analysis("firepotion", agent.game.monsters[0]))

if __name__ == "__main__":
    unittest.main()
