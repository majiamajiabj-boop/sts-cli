import unittest

from macro_policy import MacroPolicyConfig


class MacroPolicyTests(unittest.TestCase):
    def test_boss_relic_uses_latest_0731_model_with_bounded_reasoning(self):
        config = MacroPolicyConfig()
        policy = config.decision_policy("BOSS_RELIC")
        self.assertEqual("deepseek-v4-flash", policy.model)
        self.assertEqual("none", policy.effort)
        self.assertEqual(10.0, policy.timeout_seconds)
        self.assertEqual(2048, policy.max_output_tokens)
        self.assertTrue(policy.always_consult)
        self.assertTrue(policy.critical)

    def test_model_tier_and_reasoning_match_decision_complexity(self):
        policies = MacroPolicyConfig().decision_policies
        self.assertEqual("deepseek-v4-flash", policies["CARD_REWARD"].model)
        self.assertEqual("none", policies["CARD_REWARD"].effort)
        self.assertEqual("deepseek-v4-flash", policies["RUN_PLAN"].model)
        self.assertEqual("none", policies["RUN_PLAN"].effort)
        self.assertEqual(0.0, policies["RUN_PLAN"].temperature)
        self.assertEqual(
            {"deepseek-v4-flash"},
            {policy.model for policy in policies.values()},
        )

    def test_contract_change_bumps_adviser_revision(self):
        config = MacroPolicyConfig()
        self.assertEqual("deepseek-macro-v16", config.advisor_revision)
        self.assertEqual("DeepSeek-V4-Flash-0731", config.expected_release)
        self.assertEqual("sts-snapshot-v6", config.snapshot_schema_version)
        self.assertEqual("prefix-layout-v9", config.cache_layout_version)
        self.assertTrue(config.decision_policy("CAMPFIRE").always_consult)

    def test_reversible_scopes_use_tighter_value_bands(self):
        policies = MacroPolicyConfig().decision_policies
        self.assertLess(
            policies["CARD_REWARD"].adaptive_margin_multiplier,
            policies["MAP"].adaptive_margin_multiplier,
        )
        self.assertLess(
            policies["SHOP"].adaptive_margin_multiplier,
            policies["MAP"].adaptive_margin_multiplier,
        )
        self.assertGreater(
            policies["CARD_REWARD"].candidate_complexity_multiplier, 0.0
        )

    def test_flash_confidence_and_wait_budget_match_observed_provider_behavior(self):
        config = MacroPolicyConfig()
        self.assertEqual(0.70, config.decision_policies["CARD_REWARD"].confidence)
        self.assertEqual(0.70, config.decision_policies["CAMPFIRE"].confidence)
        self.assertEqual(0.70, config.decision_policies["RUN_PLAN"].confidence)
        self.assertEqual(120.0, config.max_total_wait_seconds)
        self.assertEqual(12.0, config.critical_wait_reserve_seconds)
        self.assertGreaterEqual(
            config.critical_wait_reserve_seconds,
            config.decision_policies["BOSS_RELIC"].timeout_seconds,
        )

    def test_all_0731_flash_scopes_use_reliable_none_thinking(self):
        policies = MacroPolicyConfig().decision_policies
        self.assertEqual({"none"}, {item.effort for item in policies.values()})
        self.assertEqual(0.0, policies["BOSS_RELIC"].temperature)
        self.assertEqual(0.0, policies["SAPPHIRE_KEY"].temperature)

    def test_policies_do_not_claim_unsupported_reasoning_efforts(self):
        policies = MacroPolicyConfig().decision_policies
        self.assertNotIn("low", {item.effort for item in policies.values()})
        self.assertNotIn("medium", {item.effort for item in policies.values()})
        self.assertEqual("none", policies["EVENT"].effort)
        self.assertEqual("none", policies["GRID"].effort)


if __name__ == "__main__":
    unittest.main()
