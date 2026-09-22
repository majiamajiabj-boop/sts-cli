import json
import unittest
from pathlib import Path

from macro_policy import MacroPolicyConfig
from prompt_assembler import PromptAssembler


ROOT = Path(__file__).resolve().parents[1]


class PromptAssemblerTests(unittest.TestCase):
    def setUp(self):
        self.config = MacroPolicyConfig()
        self.assembler = PromptAssembler(ROOT, self.config)

    def build(
        self,
        character="DEFECT",
        hp=40,
        decision_id="d1",
        rule_choice_id="card:a",
    ):
        return self.assembler.build(
            decision_type="CARD_REWARD",
            character=character,
            state={"act": 2, "boss": "Collector", "hp": {"current": hp}},
            candidates=[
                {"candidate_id": "card:a", "local_score": 10.0},
                {"candidate_id": "skip", "local_score": 8.0},
            ],
            rule_choice_id=rule_choice_id,
            hard_constraints=["ONLY_LISTED"],
            binding={
                "attempt_id": "attempt",
                "run_id": "run",
                "state_seq": 2,
                "decision_id": decision_id,
                "phase": "CARD_REWARD",
            },
            effort="low",
            max_output_tokens=512,
        )

    def test_stable_prefix_is_byte_identical_across_dynamic_states(self):
        first = self.build(hp=40, decision_id="d1")
        second = self.build(hp=12, decision_id="d2")
        self.assertEqual(first["body"]["input"][:4], second["body"]["input"][:4])
        self.assertEqual(first["body"]["input"][4], second["body"]["input"][4])
        self.assertNotEqual(first["body"]["input"][5], second["body"]["input"][5])
        self.assertNotEqual(first["advice_id"], second["advice_id"])

    def test_route_progress_moves_to_volatile_suffix_for_prefix_reuse(self):
        first = self.assembler.build(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={
                "ascension": 0,
                "act": 1,
                "boss": "Guardian",
                "keys": {"ruby": False},
                "deck": [["Strike_B", 0]],
                "relic_catalog": [["Burning Blood", "Burning Blood"]],
                "hp": {"current": 60},
            },
            candidates=[{"candidate_id": "card:a", "local_score": 1}],
            rule_choice_id="card:a",
            hard_constraints=[],
            binding={"state_seq": 1},
            effort="none",
            max_output_tokens=64,
        )
        second = self.assembler.build(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={
                "ascension": 0,
                "act": 2,
                "boss": "Collector",
                "keys": {"ruby": True},
                "deck": [["Strike_B", 0]],
                "relic_catalog": [["Burning Blood", "Burning Blood"]],
                "hp": {"current": 60},
            },
            candidates=[{"candidate_id": "card:a", "local_score": 1}],
            rule_choice_id="card:a",
            hard_constraints=[],
            binding={"state_seq": 2},
            effort="none",
            max_output_tokens=64,
        )
        self.assertEqual(
            first["body"]["input"][:5], second["body"]["input"][:5]
        )
        first_dynamic = first["body"]["input"][-1]["content"]
        second_dynamic = second["body"]["input"][-1]["content"]
        self.assertNotEqual(first_dynamic, second_dynamic)
        self.assertNotIn('"act"', first["body"]["input"][4]["content"])

    def test_stable_state_wire_order_keeps_fastest_changing_deck_last(self):
        def package(deck):
            return self.assembler.build(
                decision_type="CARD_REWARD",
                character="DEFECT",
                # Deliberately use a different input order. STABLE_STATE_KEYS,
                # not the caller's dictionary order, defines cache layout.
                state={
                    "deck": deck,
                    "deck_card_facts": [["Strike_B"], ["Defend_B"]],
                    "ascension": 0,
                    "relic_catalog": [["Cracked Core", "Cracked Core"]],
                },
                candidates=[{"candidate_id": "skip", "local_score": 0}],
                rule_choice_id="skip",
                hard_constraints=[],
                binding={"state_seq": len(deck)},
                effort="none",
                max_output_tokens=64,
            )

        first = package([0, 1])
        second = package([0, 1, 0])
        first_text = first["body"]["input"][-2]["content"]
        second_text = second["body"]["input"][-2]["content"]
        first_state = json.loads(first_text.split("\n", 1)[1])
        self.assertEqual(
            ["ascension", "relic_catalog", "deck_card_facts", "deck"],
            list(first_state),
        )
        # Appending a deck reference invalidates only the final stable segment;
        # the expensive relic/card catalogs remain a byte-identical prefix.
        self.assertEqual(
            first_text.partition('"deck":')[0],
            second_text.partition('"deck":')[0],
        )
        self.assertNotEqual(first_text, second_text)

    def test_snapshot_hash_remains_canonical_after_wire_order_change(self):
        common = {
            "decision_type": "EVENT",
            "character": "DEFECT",
            "candidates": [{"candidate_id": "event:0"}],
            "rule_choice_id": "event:0",
            "hard_constraints": [],
        }
        first = self.assembler.snapshot_hash(
            state={"act": 1, "hp": {"current": 40}}, **common
        )
        second = self.assembler.snapshot_hash(
            state={"hp": {"current": 40}, "act": 1}, **common
        )
        self.assertEqual(first, second)

    def test_static_output_contract_precedes_volatile_suffix(self):
        package = self.build()
        messages = package["body"]["input"]
        contract = next(
            message["content"]
            for message in messages
            if message["content"].startswith("STATIC_OUTPUT_CONTRACT\n")
        )
        dynamic = messages[-1]["content"]
        stable = messages[-2]["content"]
        self.assertTrue(contract.startswith("STATIC_OUTPUT_CONTRACT\n"))
        self.assertIn("binding.advice_id", contract)
        self.assertNotIn('"attempt_id"', contract)
        self.assertNotIn('"attempt_id"', dynamic)
        self.assertNotIn('"run_id"', dynamic)
        self.assertNotIn('"state_seq"', dynamic)
        self.assertIn('"advice_id"', dynamic)
        self.assertIn('"output_guard"', dynamic)
        self.assertIn('"candidate_fact_catalog"', dynamic)
        self.assertNotIn('"snapshot_schema_version"', dynamic)
        self.assertNotIn('"goal"', dynamic)
        self.assertNotIn('"character"', dynamic)
        self.assertTrue(stable.startswith("STATE_STABLE_CONTEXT\n"))

    def test_mutable_relic_runtime_stays_after_stable_cache_prefix(self):
        package = self.assembler.build(
            decision_type="SHOP",
            character="DEFECT",
            state={
                "act": 2,
                "relic_catalog": [["Sundial", "Sundial"]],
                "relic_runtime": [[1, "Every third shuffle gains Energy."]],
                "floor": 12,
            },
            candidates=[{"candidate_id": "shop:skip", "local_score": 0}],
            rule_choice_id="shop:skip",
            hard_constraints=[],
            binding={"state_seq": 1},
            effort="none",
            max_output_tokens=128,
            temperature=0.0,
        )
        stable_text = package["body"]["input"][-2]["content"]
        volatile_text = package["body"]["input"][-1]["content"]
        self.assertIn('"relic_catalog"', stable_text)
        self.assertNotIn('"relic_runtime"', stable_text)
        self.assertIn('"relic_runtime"', volatile_text)

    def test_dynamic_layout_places_binding_after_candidates(self):
        package = self.build()
        text = package["body"]["input"][-1]["content"]
        self.assertLess(text.index('"state"'), text.index('"decision_type"'))
        self.assertLess(
            text.index('"decision_type"'), text.index('"candidate_fact_catalog"')
        )
        self.assertLess(
            text.index('"candidate_fact_catalog"'), text.index('"candidates"')
        )
        self.assertLess(text.index('"candidates"'), text.index('"output_guard"'))
        self.assertLess(text.index('"output_guard"'), text.index('"binding"'))

    def test_dynamic_binding_exposes_only_opaque_advice_id(self):
        package = self.build()
        dynamic = package["dynamic"]
        self.assertEqual({"advice_id": package["advice_id"]}, dynamic["binding"])
        self.assertEqual(
            {"ranking_count": 2},
            dynamic["output_guard"],
        )

    def test_model_candidates_are_blind_to_local_choice_and_score(self):
        package = self.build(rule_choice_id="skip")
        dynamic = package["dynamic"]
        self.assertNotIn("rule_choice_id", dynamic)
        self.assertTrue(dynamic["candidates"])
        self.assertTrue(all(
            "local_score" not in candidate
            for candidate in dynamic["candidates"]
        ))
        # The hidden deterministic prior still binds cache and stale checks.
        self.assertNotEqual(
            package["snapshot_hash"],
            self.build(rule_choice_id="card:a")["snapshot_hash"],
        )

    def test_candidate_facts_are_catalogued_without_changing_exact_ids(self):
        package = self.assembler.build(
            decision_type="GRID",
            character="IRONCLAD",
            state={"act": 1},
            candidates=[
                {
                    "candidate_id": f"grid:uuid-{index}",
                    "kind": "permanent_card_selection",
                    "label": "Strike",
                    "local_score": 2.0,
                    "facts": {
                        "card_id": "Strike_R",
                        "type": "ATTACK",
                        "upgrades": 0,
                        "cost": 1,
                        "damage": 6,
                        "description": "Deal 6 damage.",
                    },
                }
                for index in range(3)
            ],
            rule_choice_id="grid:uuid-0",
            hard_constraints=["ONLY_EXACT_CARD_INSTANCES"],
            binding={"state_seq": 1},
            effort="none",
            max_output_tokens=512,
            temperature=0.0,
        )
        dynamic = package["dynamic"]
        self.assertEqual(1, len(dynamic["candidate_fact_catalog"]))
        self.assertEqual(
            ["grid:uuid-0", "grid:uuid-1", "grid:uuid-2"],
            [item["candidate_id"] for item in dynamic["candidates"]],
        )
        self.assertEqual([0, 0, 0], [
            item["fact_ref"] for item in dynamic["candidates"]
        ])

    def test_static_prefix_contains_lossless_wire_legend(self):
        common = self.assembler.stable_input("DEFECT")[1]["content"]
        self.assertIn('"version":"macro-wire-v4"', common)
        self.assertIn('"deck_card_fact_columns"', common)
        self.assertIn('"candidate_binding"', common)
        self.assertIn('"goal":"DEFEAT_HEART"', common)
        self.assertIn('"snapshot_schema_version":"sts-snapshot-v6"', common)
        self.assertIn('"cache_layout_version":"prefix-layout-v9"', common)

    def test_static_prefix_contains_authoritative_basegame_boss_facts(self):
        common = self.assembler.stable_input("DEFECT")[1]["content"]
        payload = json.loads(common.split("\n", 1)[1])
        knowledge = payload["game_knowledge"]["bosses"]
        bosses = knowledge["bosses"]

        self.assertIn("splits", bosses["SLIME_BOSS"]["structure"])
        self.assertIn("never has minions", bosses["THE_GUARDIAN"]["structure"])
        self.assertIn("never has minions", bosses["HEXAGHOST"]["structure"])
        self.assertIn("multi-hit", bosses["HEXAGHOST"]["structure"])
        self.assertIn("twelfth card", bosses["TIME_EATER"]["planning"])
        self.assertIn("Corrupt Heart", knowledge["act_structure"]["ACT_4"])

    def test_static_prefix_distinguishes_irreversible_event_outcomes(self):
        common = self.assembler.stable_input("THE_SILENT")[1]["content"]
        payload = json.loads(common.split("\n", 1)[1])
        events = payload["game_knowledge"]["events"]["irreversible_events"]

        mind_bloom = events["MIND_BLOOM"]
        self.assertIn("Mark of the Bloom", mind_bloom["awake"])
        self.assertIn("Doubt", mind_bloom["healthy"])
        self.assertIn("replaces Rich", mind_bloom["healthy"])
        self.assertIn("exactly 5 Bites", events["VAMPIRES"]["accept"])
        self.assertIn("Ascension 15+", events["COUNCIL_OF_GHOSTS"]["accept"])

        event_policy = self.assembler.decision_policy["decision_types"]["EVENT"]
        self.assertIn("Structured consequences are signed", event_policy)
        self.assertIn("never treat a mentioned resource as gained", event_policy)

    def test_static_policy_covers_neow_run_plan_and_grid_direction(self):
        decision_types = self.assembler.decision_policy["decision_types"]
        self.assertIn("NEOW", decision_types)
        self.assertIn("RUN_PLAN", decision_types)
        self.assertIn("best card to remove", decision_types["GRID"])
        self.assertIn("marginal upgrade gain", decision_types["GRID"])
        self.assertIn("Skip or Singing Bowl", decision_types["CARD_REWARD"])
        self.assertIn("paired observed support", decision_types["RUN_PLAN"])

        prompt = self.assembler.global_prompt
        self.assertIn("game_knowledge.bosses is authoritative", prompt)
        self.assertIn("Never\ninvent minions", prompt)
        self.assertIn(
            "directly observed runtime fields", prompt
        )
        self.assertIn("controller-derived estimates", prompt)
        self.assertIn(
            "Structured does not automatically mean observed", prompt
        )

    def test_static_prompt_defines_independent_key_mechanics(self):
        global_prompt = self.assembler.stable_input("DEFECT")[0]["content"]

        self.assertIn("RUBY is obtained only by choosing RECALL", global_prompt)
        self.assertIn("SAPPHIRE is", global_prompt)
        self.assertIn("EMERALD is", global_prompt)
        self.assertIn("Never use the status or deadline", global_prompt)

    def test_each_supported_character_has_a_stable_role_pack(self):
        common = None
        roles = []
        for character in ("IRONCLAD", "THE_SILENT", "DEFECT"):
            stable = self.assembler.stable_input(character)
            if common is None:
                common = stable[:2]
            self.assertEqual(common, stable[:2])
            roles.append(stable[2]["content"])
        self.assertEqual(3, len(set(roles)))
        self.assertEqual(
            1,
            len({
                self.assembler.stable_input(character)[3]["content"]
                for character in ("IRONCLAD", "THE_SILENT", "DEFECT")
            }),
        )

    def test_character_and_rule_choice_are_part_of_cache_identity(self):
        baseline = self.build(character="DEFECT", rule_choice_id="card:a")
        other_character = self.build(
            character="IRONCLAD", rule_choice_id="card:a"
        )
        other_rule = self.build(
            character="DEFECT", rule_choice_id="skip"
        )
        self.assertNotEqual(
            baseline["snapshot_hash"], other_character["snapshot_hash"]
        )
        self.assertNotEqual(
            baseline["snapshot_hash"], other_rule["snapshot_hash"]
        )
        self.assertNotEqual(baseline["advice_id"], other_rule["advice_id"])

    def test_schema_enum_contains_only_current_candidate_ids(self):
        package = self.build()
        schema = package["body"]["text"]["format"]["schema"]
        self.assertEqual(
            ["card:a", "skip"],
            schema["properties"]["choice_id"]["enum"],
        )

    def test_schema_defines_bounded_complete_ranking_contract(self):
        package = self.build()
        properties = package["body"]["text"]["format"]["schema"][
            "properties"
        ]
        confidence = properties["confidence"]
        self.assertEqual(0.0, confidence["minimum"])
        self.assertEqual(1.0, confidence["maximum"])

        rankings = properties["rankings"]
        self.assertEqual(2, rankings["minItems"])
        self.assertEqual(2, rankings["maxItems"])
        self.assertTrue(rankings["uniqueItems"])
        score = rankings["items"]["properties"]["score"]
        self.assertEqual(0.0, score["minimum"])
        self.assertEqual(100.0, score["maximum"])
        self.assertIn("exactly once", rankings["description"])

    def test_duplicate_candidates_are_rejected(self):
        with self.assertRaises(ValueError):
            self.assembler.build(
                decision_type="EVENT",
                character="DEFECT",
                state={},
                candidates=[
                    {"candidate_id": "event:0", "local_score": 1},
                    {"candidate_id": "event:0", "local_score": 2},
                ],
                rule_choice_id="event:0",
                hard_constraints=[],
                binding={},
                effort="none",
                max_output_tokens=256,
            )

    def test_secret_never_appears_in_prompt_or_profile(self):
        package = self.build()
        serialized = json.dumps(
            {"body": package["body"], "profile": self.assembler.profile()}
        )
        self.assertNotIn("DEEPSEEK_API_KEY", serialized)
        self.assertNotIn("Bearer", serialized)

    def test_warmup_is_exactly_the_same_role_prefix(self):
        stable = self.assembler.stable_input("DEFECT")
        body = self.assembler.warmup_body("DEFECT")
        self.assertEqual(stable, body["input"])
        self.assertEqual(1, body["max_output_tokens"])
        self.assertEqual(
            {"type": "object"},
            body["text"]["format"]["schema"],
        )

    def test_global_prompt_defines_output_scales(self):
        prompt = self.assembler.global_prompt
        self.assertIn("0-to-100", prompt)
        self.assertIn("0.0 to 1.0", prompt)
        self.assertIn("every supplied candidate_id exactly once", prompt)


if __name__ == "__main__":
    unittest.main()
