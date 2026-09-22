"""Native boundary tests; build experiments/native before running explicitly."""
from pathlib import Path
import copy
import tempfile
import unittest

from experiments.blind_combat_benchmark import NativeLab, ROOT, current_action
from spirecomm.ai.agent import SimpleAgent
from spirecomm.spire.character import PlayerClass
from spirecomm.spire.game import Game

BINARY = ROOT / "experiments/build/blind-combat-lab.exe"


def starter_spec():
    cards = []
    for card_id, count, kind, damage, block, cost in (
        ("Strike_R",5,"ATTACK",6,0,1), ("Defend_R",4,"SKILL",0,5,1),
        ("Bash",1,"ATTACK",8,0,2),
    ):
        for _ in range(count):
            cards.append({"id":card_id,"name":card_id,"type":kind,"rarity":"BASIC",
                          "base_damage":damage,"base_block":block,"cost":cost,
                          "upgrades":0,"has_target":kind=="ATTACK","magic_number":2,
                          "card_instance_id":str(len(cards))})
    return {"game_state":{"class":"IRONCLAD","act":2,"floor":18,
                           "ascension_level":0,"seed":111,"gold":0,"current_hp":70,
                           "max_hp":80,"deck":cards,"relics":[],"potions":[]},
            "candidates":[],"targets":["Spheric Guardian"]}


@unittest.skipUnless(BINARY.exists(), "build native offline lab first")
class BlindLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.lab = NativeLab(BINARY, Path(self.temp.name)/"stderr.log")

    def tearDown(self):
        self.lab.close()
        self.temp.cleanup()

    def start(self, spec=None):
        return self.lab.call({"op":"start","world":0,"spec":spec or starter_spec()})

    def test_hidden_rng_and_draw_permutation_cannot_change_root_choice(self):
        obs = self.start()
        for _ in range(3):
            result = self.lab.call({"op":"check_hidden_invariance", "planning_seed":9107,
                                    "worlds":3,"simulations":200})
            self.assertTrue(result["invariant"])
            self.assertEqual(obs,self.lab.call({"op":"view"}))
            end = next(a for a in obs["actions"] if a["kind"]=="end")
            obs = self.lab.call({"op":"step","revision":obs["revision"],"token":end["token"]})

    def test_adaptive_budget_covers_every_action_and_preserves_hidden_invariance(self):
        self.start()
        request={"op":"check_hidden_invariance", "planning_seed":9107,
                 "worlds":2,"simulations":600}
        balanced=self.lab.call(dict(request,allocation="balanced"))
        adaptive=self.lab.call(dict(request,allocation="adaptive_quarter"))
        self.assertTrue(adaptive["invariant"])
        for record in balanced["world_budgets"]:
            self.assertEqual(record["root_visits"],600)
            self.assertLessEqual(record["max_visits"]-record["min_visits"],1)
        floor=adaptive["configured_root_visit_floor"]
        for record in adaptive["world_budgets"]:
            self.assertEqual(record["stop_reason"],"simulation_budget")
            self.assertEqual(record["root_visits"],600)
            self.assertGreaterEqual(record["min_visits"],floor)
        self.assertTrue(any(r["max_visits"]-r["min_visits"]>1 for r in adaptive["world_budgets"]))
        before=self.lab.call({"op":"view"})
        with self.assertRaisesRegex(RuntimeError,"cannot cover"):
            self.lab.call(dict(request,allocation="adaptive_quarter",simulations=1))
        with self.assertRaisesRegex(RuntimeError,"unknown root allocation"):
            self.lab.call(dict(request,allocation="typo"))
        self.assertEqual(before,self.lab.call({"op":"view"}))

    def topdeck_fixture(self, counts, required):
        metadata={
            "Headbutt":("ATTACK",9,0,1,0), "Rage":("SKILL",0,0,0,3),
            "Shrug It Off":("SKILL",0,8,1,1), "Battle Trance":("SKILL",0,0,0,3),
            "Reckless Charge":("ATTACK",7,0,0,0),
        }
        spec=starter_spec();spec["game_state"]["deck"]=[]
        for name,count in counts.items():
            kind,damage,block,cost,magic=metadata[name]
            for _ in range(count):
                spec["game_state"]["deck"].append(dict(id=name,name=name,type=kind,
                    base_damage=damage,base_block=block,cost=cost,magic_number=magic,
                    upgrades=0,rarity="COMMON",has_target=kind=="ATTACK",
                    card_instance_id=str(len(spec["game_state"]["deck"]))))
        for world in range(100):
            obs=self.lab.call({"op":"start","world":world,"spec":spec,"remember_topdeck":True})
            hand=[c["id"] for c in obs["game_state"]["combat_state"]["hand"]]
            if all(hand.count(name)>=count for name,count in required.items()):return obs
        self.fail("fixture hand not found")

    def play_named(self, obs, name):
        card=next(c for c in obs["game_state"]["combat_state"]["hand"] if c["id"]==name)
        action=next(a for a in obs["actions"] if a.get("card_uuid")==card["uuid"])
        return self.lab.call({"op":"step","revision":obs["revision"],"token":action["token"]}), int(card["uuid"])

    def test_observed_headbutt_stack_is_sampled_then_consumed_one_card_at_a_time(self):
        obs=self.topdeck_fixture({"Rage":3,"Headbutt":4,"Shrug It Off":3},
                                 {"Rage":1,"Headbutt":2,"Shrug It Off":1})
        before_energy=obs["game_state"]["combat_state"]["player"]["energy"]
        obs,rage=self.play_named(obs,"Rage")
        self.assertEqual(obs["game_state"]["combat_state"]["player"]["energy"],before_energy)
        obs,headbutt=self.play_named(obs,"Headbutt")
        self.assertEqual(obs["known_topdeck"],[rage])
        obs,_=self.play_named(obs,"Headbutt")
        self.assertEqual(obs["known_topdeck"],[headbutt,rage])
        choice=self.lab.call({"op":"check_hidden_invariance","planning_seed":25,"worlds":3,"simulations":150})
        self.assertTrue(choice["invariant"])
        self.assertTrue(all(w["known_topdeck"]==[headbutt,rage] for w in choice["world_budgets"]))
        obs,_=self.play_named(obs,"Shrug It Off")
        self.assertEqual(obs["known_topdeck"],[rage])
        self.assertIn(str(headbutt),[c["uuid"] for c in obs["game_state"]["combat_state"]["hand"]])
        end=next(a for a in obs["actions"] if a["kind"]=="end")
        obs=self.lab.call({"op":"step","revision":obs["revision"],"token":end["token"]})
        self.assertEqual(obs["known_topdeck"],[])
        self.assertEqual(self.start()["known_topdeck"],[])

    def test_explicit_headbutt_choice_and_random_insertion_invalidates_memory(self):
        obs=self.topdeck_fixture({"Rage":4,"Headbutt":3,"Reckless Charge":3},
                                 {"Rage":2,"Headbutt":1,"Reckless Charge":1})
        obs,rage=self.play_named(obs,"Rage")
        obs,_=self.play_named(obs,"Rage")
        obs,_=self.play_named(obs,"Headbutt")
        self.assertEqual(obs["select_task"],"HEADBUTT")
        select=next(a for a in obs["actions"] if a.get("card_uuid")==str(rage))
        obs=self.lab.call({"op":"step","revision":obs["revision"],"token":select["token"]})
        self.assertEqual(obs["known_topdeck"],[rage])
        obs,_=self.play_named(obs,"Reckless Charge")
        self.assertEqual(obs["known_topdeck"],[])

    def test_no_draw_does_not_consume_known_topdeck(self):
        obs=self.topdeck_fixture({"Rage":2,"Headbutt":4,"Shrug It Off":2,"Battle Trance":2},
                                 {"Rage":1,"Headbutt":1,"Shrug It Off":1,"Battle Trance":1})
        obs,_=self.play_named(obs,"Battle Trance")
        obs,rage=self.play_named(obs,"Rage")
        obs,_=self.play_named(obs,"Headbutt")
        self.assertEqual(obs["select_task"],"HEADBUTT")
        select=next(a for a in obs["actions"] if a.get("card_uuid")==str(rage))
        obs=self.lab.call({"op":"step","revision":obs["revision"],"token":select["token"]})
        obs,_=self.play_named(obs,"Shrug It Off")
        self.assertEqual(obs["known_topdeck"],[rage])

    def test_native_curse_cost_is_exported_in_java_protocol_encoding(self):
        spec=starter_spec()
        for c in spec["game_state"]["deck"]:
            c.update(id="Doubt",name="Doubt",type="CURSE",cost=-2,base_damage=0,has_target=False)
        obs=self.start(spec)
        for c in obs["game_state"]["combat_state"]["hand"]:
            self.assertEqual(c["cost"],-2)
            self.assertEqual(c["combat_cost"],-2)
            self.assertFalse(c["is_playable"])

    def test_deck_cost_model_mismatch_is_rejected_before_policy_comparison(self):
        spec=starter_spec()
        spec["game_state"]["deck"][0]["cost"]=0
        with self.assertRaisesRegex(RuntimeError,"deck cost mismatch"):
            self.start(spec)
        del spec["game_state"]["deck"][0]["cost"]
        with self.assertRaisesRegex(RuntimeError,"deck cost required"):
            self.start(spec)

    def test_optional_combat_cost_cannot_silently_disagree_with_model(self):
        spec=starter_spec()
        spec["game_state"]["deck"][0]["combat_cost"]=1
        self.assertEqual(self.start(spec)["outcome"],"ongoing")
        for invalid in (99, None, "1"):
            spec["game_state"]["deck"][0]["combat_cost"]=invalid
            with self.assertRaisesRegex(RuntimeError,"combat_cost mismatch"):
                self.start(spec)

    def test_search_does_not_advance_execution_rng(self):
        obs = self.start()
        end = next(a for a in obs["actions"] if a["kind"]=="end")
        expected = self.lab.call({"op":"step","revision":0,"token":end["token"]})
        self.start()
        self.lab.call({"op":"search","planning_seed":23,"worlds":2,"simulations":100})
        actual = self.lab.call({"op":"step","revision":0,"token":end["token"]})
        self.assertEqual(actual,expected)

    def test_duplicate_card_instances_are_bindable_and_stale_action_is_rejected(self):
        obs = self.start()
        cs = obs["game_state"]["combat_state"]
        self.assertEqual(cs["turn"],1)
        self.assertEqual(obs["turn"],1)
        for card in cs["hand"]:
            if card["is_playable"]:
                self.assertTrue(any(a.get("card_uuid")==card["uuid"] for a in obs["actions"]))
        defend = next(c for c in cs["hand"] if c["id"]=="Defend_R")
        token = next(a["token"] for a in obs["actions"] if a.get("card_uuid")==defend["uuid"])
        after = self.lab.call({"op":"step","revision":0,"token":token})
        self.assertEqual(after["game_state"]["combat_state"]["player"]["block"],5)
        self.assertEqual(after["game_state"]["combat_state"]["player"]["energy"],2)
        with self.assertRaisesRegex(RuntimeError,"stale"):
            self.lab.call({"op":"step","revision":0,"token":token})
        self.assertEqual(after,self.lab.call({"op":"view"}))

    def test_native_unknown_order_reaches_actual_baseline_planner(self):
        spec=starter_spec()
        for card in spec["game_state"]["deck"][:5]:
            card.update(id="Pommel Strike",name="Pommel Strike",base_damage=9,magic_number=1,rarity="COMMON")
        obs=self.start(spec)
        game=Game.from_json(obs["game_state"],["play","end"])
        self.assertIs(game.draw_pile_order_known,False)
        reverse=copy.deepcopy(obs)
        reverse["game_state"]["combat_state"]["draw_pile"].reverse()
        choices=[current_action(SimpleAgent(chosen_class=PlayerClass.IRONCLAD,goal_mode="HEART"),o)
                 for o in (obs,reverse)]
        self.assertEqual(choices[0],choices[1])
        del reverse["game_state"]["combat_state"]["draw_pile_order_known"]
        self.assertTrue(Game.from_json(reverse["game_state"],["play","end"]).draw_pile_order_known)

    def test_gremlin_horn_trigger_is_visible_as_energy_and_draw(self):
        spec=starter_spec();spec["targets"]=["AUTOMATON"]
        for c in spec["game_state"]["deck"]:
            c.update(id="Immolate",name="Immolate",type="ATTACK",rarity="RARE",cost=2,
                     base_damage=28,base_block=0,upgrades=1,has_target=False,magic_number=0)
        plain=self.start(spec)
        horn_spec=copy.deepcopy(spec)
        horn_spec["game_state"]["relics"]=[{"id":"Gremlin Horn","name":"Gremlin Horn","counter":-1}]
        horn=NativeLab(BINARY,Path(self.temp.name)/"horn.stderr.log")
        try:
            with_horn=horn.call({"op":"start","world":0,"spec":horn_spec})
            for _ in range(30):
                before=plain["game_state"]["combat_state"]
                plays=[a for a in plain["actions"] if a["kind"]=="play"]
                action=plays[0] if plays else next(a for a in plain["actions"] if a["kind"]=="end")
                request={"op":"step","revision":plain["revision"],"token":action["token"]}
                plain=self.lab.call(request);with_horn=horn.call(request)
                self.assertEqual(plain["outcome"],"ongoing")
                after=plain["game_state"]["combat_state"]
                gained=with_horn["game_state"]["combat_state"]
                alive=lambda cs:sum(not m["is_gone"] for m in cs["monsters"])
                killed=alive(before)-alive(after)
                if killed>0:
                    self.assertEqual(gained["player"]["energy"]-after["player"]["energy"],killed)
                    self.assertEqual(len(gained["hand"])-len(after["hand"]),killed)
                    return
            self.fail("fixture never reached a nonterminal kill")
        finally:
            horn.close()

    def test_unsupported_relic_and_character_rejected_before_comparison(self):
        spec = starter_spec()
        spec["game_state"]["relics"] = [{"id":"Runic Dome","counter":-1}]
        with self.assertRaisesRegex(RuntimeError,"outside pilot coverage"):
            self.start(spec)
        spec = starter_spec()
        spec["game_state"]["class"]="THE_SILENT"
        with self.assertRaisesRegex(RuntimeError,"Ironclad only"):
            self.start(spec)


if __name__=="__main__":unittest.main()
