import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.blind_combat_benchmark import run_battle


class FinalActionAccountingTests(unittest.TestCase):
    def test_final_budgeted_action_records_terminal_result_not_cutoff(self):
        for outcome in ("win","loss","ongoing"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                first={"outcome":"ongoing","revision":0,"hp":10,"turn":1,
                       "actions":[{"kind":"end","token":123}],
                       "game_state":{"combat_state":{"player":{"energy":0,"block":0},"hand":[],"draw_pile":[]}}}
                final={"outcome":outcome,"revision":1,"hp":5,"turn":2}
                with patch("experiments.blind_combat_benchmark.NativeLab") as lab:
                    lab.return_value.call.side_effect=[first,{"token":123},final]
                    result=run_battle(Path("unused"),{},0,"sampled_search",Path(temp)/"result.json",1,1,1)
                self.assertEqual(result["status"],outcome if outcome!="ongoing" else "cutoff")


if __name__=="__main__":unittest.main()
