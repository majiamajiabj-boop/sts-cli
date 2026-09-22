import io
import json
import math
import os
import tempfile
import time
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deepseek_macro import (
    AdvisorResult,
    DeepSeekMacroAdvisor,
    DeepSeekRequestError,
    DeepSeekResponsesClient,
    LocalAdviceCache,
    _usage_summary,
    _validated_advice,
)
from macro_policy import MacroPolicyConfig, load_local_env
from prompt_assembler import PromptAssembler


ROOT = Path(__file__).resolve().parent


class LocalEnvironmentTests(unittest.TestCase):
    def test_local_env_loads_allowlisted_values_without_overriding_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "DEEPSEEK_API_KEY=file-key\n"
                "DEEPSEEK_PROXY_URL=http://127.0.0.1:7897\n"
                "STS_MACRO_ADVISOR_MODE=assist\n"
                "STS_MACRO_ADVISOR_SCOPES='CARD_REWARD,BOSS_RELIC'\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DEEPSEEK_API_KEY": "process-key"},
                clear=True,
            ):
                loaded = load_local_env(path)
                config = MacroPolicyConfig.from_env()
                self.assertEqual("process-key", os.environ["DEEPSEEK_API_KEY"])
                self.assertNotIn("DEEPSEEK_API_KEY", loaded)
                self.assertEqual("assist", config.mode)
                self.assertEqual("http://127.0.0.1:7897", config.proxy_url)
                self.assertEqual("chat_json", config.primary_transport)
                self.assertEqual(
                    ("CARD_REWARD", "BOSS_RELIC"), config.assist_scopes
                )

    def test_local_env_rejects_unrelated_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("PATH=unexpected\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError):
                    load_local_env(path)

    def test_proxy_url_validation_is_sanitized(self):
        secret_proxy = "http://user:secret@127.0.0.1:not-a-port"
        with patch.dict(
            os.environ,
            {"DEEPSEEK_PROXY_URL": secret_proxy},
            clear=True,
        ):
            with self.assertRaises(ValueError) as raised:
                MacroPolicyConfig.from_env()
        self.assertNotIn("secret", str(raised.exception))


class FakeHTTPResponse:
    def __init__(self, value):
        self.raw = json.dumps(value).encode("utf-8")

    def read(self, _limit):
        return self.raw


class FakeModelClient:
    def __init__(self, factory=None, errors=None, available=True):
        self.factory = factory or valid_response_for_body
        self.errors = list(errors or [])
        self.available = available
        self.calls = []

    def request(self, body, timeout):
        self.calls.append((body, timeout))
        if self.errors:
            raise self.errors.pop(0)
        return self.factory(body)


def valid_response_for_body(body, **overrides):
    dynamic = json.loads(body["input"][-1]["content"])
    candidate_ids = [item["candidate_id"] for item in dynamic["candidates"]]
    choice_id = overrides.pop("choice_id", candidate_ids[0])
    rankings = [
        {"candidate_id": candidate_id, "score": 90 - index * 10}
        for index, candidate_id in enumerate(candidate_ids)
    ]
    if choice_id != rankings[0]["candidate_id"]:
        selected = next(item for item in rankings if item["candidate_id"] == choice_id)
        selected["score"] = 100
    advice = {
        "schema_version": "sts-advice-v1",
        "advice_id": dynamic["binding"]["advice_id"],
        "choice_id": choice_id,
        "confidence": 0.9,
        "rankings": rankings,
        "reason_codes": ["DECK_SYNERGY"],
        "rationale": "Grounded test recommendation.",
        **overrides,
    }
    return {
        "model": "deepseek-v4-flash",
        "system_fingerprint": "test-fingerprint",
        "output_text": json.dumps(advice),
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "prompt_cache_hit_tokens": 700,
            "prompt_cache_miss_tokens": 300,
        },
    }


class DeepSeekClientTests(unittest.TestCase):
    def test_client_uses_configured_proxy_without_exposing_it_in_profile(self):
        proxy_url = "http://user:secret@127.0.0.1:7897"
        config = MacroPolicyConfig(proxy_url=proxy_url)
        fake_opener = SimpleNamespace(open=lambda *_args, **_kwargs: None)

        with patch("urllib.request.build_opener", return_value=fake_opener) as build:
            client = DeepSeekResponsesClient(config, api_key="secret-key")

        handler = build.call_args.args[0]
        self.assertIsInstance(handler, urllib.request.ProxyHandler)
        self.assertIs(client.opener, fake_opener.open)
        self.assertNotIn("proxy_url", config.profile())
        self.assertNotIn(proxy_url, repr(config))

    def test_usage_summary_accepts_provider_cache_read_aliases(self):
        summary = _usage_summary({
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 20,
                "prompt_cache_read_tokens": 760,
                "prompt_tokens_details": {"cached_tokens": 760},
            }
        })

        self.assertEqual(760, summary["prompt_cache_hit_tokens"])
        self.assertEqual(240, summary["prompt_cache_miss_tokens"])
        self.assertEqual(0.76, summary["prompt_cache_hit_ratio"])

    def test_client_builds_responses_request_without_leaking_key_in_errors(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.headers.get("Authorization")
            captured["timeout"] = timeout
            return FakeHTTPResponse({"output_text": "{}"})

        config = MacroPolicyConfig()
        client = DeepSeekResponsesClient(
            config, api_key="secret-test-key", opener=opener
        )
        result = client.request({"model": config.model}, 3.5)
        self.assertEqual("https://api.deepseek.com/responses", captured["url"])
        self.assertEqual("Bearer secret-test-key", captured["authorization"])
        self.assertEqual(3.5, captured["timeout"])
        self.assertEqual("{}", result["output_text"])

    def test_http_error_is_sanitized_and_retryable(self):
        def opener(_request, timeout=None):
            raise urllib.error.HTTPError(
                "url", 429, "secret body", {}, io.BytesIO(b"secret")
            )

        client = DeepSeekResponsesClient(
            MacroPolicyConfig(), api_key="secret", opener=opener
        )
        with self.assertRaises(DeepSeekRequestError) as raised:
            client.request({}, 1)
        self.assertEqual("http_error", raised.exception.category)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("secret", str(raised.exception))

    def test_insufficient_balance_is_an_account_error(self):
        def opener(_request, timeout=None):
            raise urllib.error.HTTPError(
                "url", 402, "secret body", {}, io.BytesIO(b"secret")
            )

        client = DeepSeekResponsesClient(
            MacroPolicyConfig(), api_key="secret", opener=opener
        )
        with self.assertRaises(DeepSeekRequestError) as raised:
            client.request({}, 1)
        self.assertEqual("account", raised.exception.category)
        self.assertFalse(raised.exception.retryable)

    def test_chat_json_fallback_normalizes_response_shape(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse({
                "model": "deepseek-v4-flash",
                "system_fingerprint": "fp",
                "choices": [{"message": {"content": "{\"ok\":true}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            })

        client = DeepSeekResponsesClient(
            MacroPolicyConfig(), api_key="secret", opener=opener
        )
        response = client.request_json_fallback({
            "model": "deepseek-v4-flash",
            "input": [{"role": "user", "content": "json"}],
            "reasoning": {"effort": "none"},
            "temperature": 0.0,
            "text": {
                "format": {
                    "schema": {
                        "type": "object",
                        "required": ["choice_id"],
                        "properties": {
                            "choice_id": {"type": "string", "enum": ["a"]}
                        },
                    }
                }
            },
            "max_output_tokens": 32,
        }, 2)
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual({"type": "json_object"}, captured["body"]["response_format"])
        self.assertEqual("disabled", captured["body"]["thinking"]["type"])
        self.assertEqual(0.0, captured["body"]["temperature"])
        self.assertEqual(2, len(captured["body"]["messages"]))
        self.assertIn("STRICT_OUTPUT_CONTRACT", captured["body"]["messages"][-1]["content"])
        self.assertIn("SCORE_SEMANTICS", captured["body"]["messages"][-1]["content"])
        self.assertIn("EXAMPLE_JSON_SHAPE", captured["body"]["messages"][-1]["content"])
        self.assertIn('"choice_id"', captured["body"]["messages"][-1]["content"])
        self.assertEqual('{"ok":true}', response["output_text"])

    def test_chat_json_uses_embedded_contract_without_volatile_schema_tail(self):
        captured = {}

        def opener(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse({
                "model": "deepseek-v4-flash",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {},
            })

        config = MacroPolicyConfig()
        assembler = PromptAssembler(ROOT, config)
        package = assembler.build(
            decision_type="CARD_REWARD",
            character="IRONCLAD",
            state={"act": 1},
            candidates=[
                {"candidate_id": "card:a", "local_score": 10.0},
                {"candidate_id": "skip", "local_score": 8.0},
            ],
            rule_choice_id="card:a",
            hard_constraints=[],
            binding={
                "attempt_id": "attempt",
                "run_id": "run",
                "state_seq": 1,
                "decision_id": "decision",
                "phase": "CARD_REWARD",
            },
            effort="none",
            max_output_tokens=128,
        )
        client = DeepSeekResponsesClient(
            config, api_key="secret", opener=opener
        )

        client.request_json_fallback(package["body"], 2)

        messages = captured["body"]["messages"]
        self.assertEqual(package["body"]["input"], messages)
        self.assertTrue(
            any(message["content"].startswith(
                "STATIC_OUTPUT_CONTRACT\n"
            ) for message in messages)
        )
        self.assertNotIn("STRICT_OUTPUT_CONTRACT", messages[-1]["content"])
        self.assertEqual(
            {"type": "json_object"},
            captured["body"]["response_format"],
        )

    def test_chat_warmup_uses_same_json_mode_as_live_decision(self):
        captured = {}

        def opener(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse({
                "model": "deepseek-v4-flash",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {},
            })

        config = MacroPolicyConfig()
        assembler = PromptAssembler(ROOT, config)
        client = DeepSeekResponsesClient(
            config, api_key="secret", opener=opener
        )

        client.request_json_fallback(
            assembler.warmup_body("IRONCLAD"), 2
        )

        self.assertEqual(
            assembler.stable_input("IRONCLAD"),
            captured["body"]["messages"],
        )
        self.assertEqual(
            {"type": "json_object"},
            captured["body"]["response_format"],
        )

    def test_schema_less_chat_request_does_not_enable_json_mode(self):
        captured = {}

        def opener(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse({
                "choices": [{"message": {"content": "READY"}}],
                "usage": {},
            })

        client = DeepSeekResponsesClient(
            MacroPolicyConfig(), api_key="secret", opener=opener
        )
        client.request_json_fallback({
            "model": "deepseek-v4-flash",
            "input": [{"role": "user", "content": "warm"}],
            "reasoning": {"effort": "none"},
            "max_output_tokens": 1,
        }, 2)
        self.assertNotIn("response_format", captured["body"])
        self.assertEqual(
            [{"role": "user", "content": "warm"}],
            captured["body"]["messages"],
        )

    def test_malformed_chat_choices_shape_is_sanitized(self):
        def opener(_request, timeout):
            return FakeHTTPResponse({
                "model": "deepseek-v4-flash",
                "choices": {"bad": 1},
                "usage": {"prompt_tokens": 10},
            })

        client = DeepSeekResponsesClient(
            MacroPolicyConfig(), api_key="secret", opener=opener
        )
        response = client.request_json_fallback({
            "model": "deepseek-v4-flash",
            "input": [],
        }, 1)
        self.assertEqual("invalid_response_shape", response["adapter_error"])
        self.assertIsNone(response["output_text"])

    def test_unexpected_client_exception_is_sanitized(self):
        def opener(_request, timeout=None):
            raise RuntimeError("provider internals")

        client = DeepSeekResponsesClient(
            MacroPolicyConfig(), api_key="secret", opener=opener
        )
        with self.assertRaises(DeepSeekRequestError) as raised:
            client.request({}, 1)
        self.assertEqual("network_error", raised.exception.category)
        self.assertEqual(
            {"exception_type": "RuntimeError"},
            raised.exception.safe_details(),
        )
        self.assertNotIn("provider internals", str(raised.exception))

    def test_network_error_exposes_only_safe_type_and_errno(self):
        secret = "http://user:secret@127.0.0.1:7897"

        def opener(_request, timeout=None):
            raise urllib.error.URLError(
                OSError(10060, f"failed through {secret}")
            )

        client = DeepSeekResponsesClient(
            MacroPolicyConfig(), api_key="secret-key", opener=opener
        )
        with self.assertRaises(DeepSeekRequestError) as raised:
            client.request({}, 1)
        details = raised.exception.safe_details()
        self.assertIn(
            details["exception_type"], {"OSError", "TimeoutError"}
        )
        self.assertEqual(10060, details["errno"])
        serialized = json.dumps(details)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("secret-key", serialized)


class DeepSeekAdvisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            MacroPolicyConfig(),
            mode="assist",
            cache_warmup=False,
            retry_count=0,
        )
        self.assembler = PromptAssembler(ROOT, self.config)

    def request(self, advisor, candidates=None):
        return advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"act": 1, "hp": {"current": 60, "maximum": 75}},
            candidates=candidates or [
                {"candidate_id": "card:a", "local_score": 10.0},
                {"candidate_id": "skip", "local_score": 8.0},
            ],
            rule_choice_id="card:a",
            hard_constraints=["ONLY_LISTED"],
            binding={
                "attempt_id": "a",
                "run_id": "r",
                "state_seq": 1,
                "decision_id": "d",
                "phase": "CARD_REWARD",
            },
        )

    def advisor(self, client):
        return DeepSeekMacroAdvisor(
            self.config,
            self.assembler,
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "cache"),
        )

    def test_malformed_billable_chat_shape_still_consumes_cost_budget(self):
        def opener(_request, timeout):
            return FakeHTTPResponse({
                "model": "deepseek-v4-flash",
                "system_fingerprint": "shape-fp",
                "choices": 7,
                "usage": {"prompt_tokens": 1000, "completion_tokens": 10},
            })

        config = replace(self.config, primary_transport="chat_json")
        advisor = DeepSeekMacroAdvisor(
            config,
            PromptAssembler(ROOT, config),
            client=DeepSeekResponsesClient(
                config, api_key="test-key", opener=opener
            ),
            cache=LocalAdviceCache(Path(self.temp.name) / "shape-cost-cache"),
        )
        result = self.request(advisor)
        self.assertEqual("invalid_response_shape", result.error_class)
        self.assertGreater(result.usage["estimated_cost_usd"], 0)
        self.assertEqual(
            result.usage["estimated_cost_usd"], advisor.total_cost_usd
        )

    def test_valid_response_is_parsed_and_usage_is_audited(self):
        result = self.request(self.advisor(FakeModelClient()))
        self.assertEqual("recommended", result.status)
        self.assertEqual("card:a", result.model_choice_id)
        self.assertEqual(700, result.usage["prompt_cache_hit_tokens"])
        self.assertEqual(0.7, result.usage["prompt_cache_hit_ratio"])
        self.assertGreater(result.usage["estimated_cost_usd"], 0)

    def test_unique_card_name_is_rebound_to_exact_candidate_id(self):
        def model_returns_display_name(body):
            response = valid_response_for_body(body)
            value = json.loads(response["output_text"])
            value["choice_id"] = "Steam"
            value["rankings"] = [
                {"candidate_id": "Steam", "score": 90},
                {"candidate_id": "skip", "score": 80},
            ]
            response["output_text"] = json.dumps(value)
            return response

        client = FakeModelClient(model_returns_display_name)
        advisor = self.advisor(client)
        result = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"act": 1},
            candidates=[
                {
                    "candidate_id": "card:steam-copy",
                    "local_score": 10.0,
                    "facts": {
                        "card_id": "Steam", "name": "Steam",
                        "type": "SKILL", "cost": 0,
                    },
                },
                {"candidate_id": "skip", "local_score": 8.0},
            ],
            rule_choice_id="card:steam-copy",
            binding={"state_seq": 99},
        )
        self.assertEqual("recommended", result.status)
        self.assertEqual("card:steam-copy", result.model_choice_id)
        self.assertEqual(
            "card:steam-copy", result.rankings[0]["candidate_id"]
        )

    def test_ambiguous_card_name_still_fails_closed(self):
        def model_returns_ambiguous_name(body):
            response = valid_response_for_body(body)
            value = json.loads(response["output_text"])
            value["choice_id"] = "Strike"
            value["rankings"] = [
                {"candidate_id": "Strike", "score": 90},
                {"candidate_id": "skip", "score": 80},
            ]
            response["output_text"] = json.dumps(value)
            return response

        result = self.advisor(
            FakeModelClient(model_returns_ambiguous_name)
        ).advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"act": 1},
            candidates=[
                {
                    "candidate_id": "card:strike-a",
                    "local_score": 10.0,
                    "facts": {
                        "card_id": "Strike", "name": "Strike",
                        "type": "ATTACK", "cost": 1,
                    },
                },
                {
                    "candidate_id": "card:strike-b",
                    "local_score": 9.0,
                    "facts": {
                        "card_id": "Strike", "name": "Strike",
                        "type": "ATTACK", "cost": 1,
                    },
                },
                {"candidate_id": "skip", "local_score": 8.0},
            ],
            rule_choice_id="card:strike-a",
            binding={"state_seq": 100},
        )
        self.assertEqual("unknown_choice", result.error_class)

    def test_policy_keeps_critical_choice_on_latest_0731_model_with_bounded_reasoning(self):
        def model_aware(body):
            response = valid_response_for_body(body)
            response["model"] = body["model"]
            response["system_fingerprint"] = "pro-test-fingerprint"
            return response

        client = FakeModelClient(model_aware)
        advisor = self.advisor(client)
        result = advisor.advise(
            decision_type="BOSS_RELIC",
            character="DEFECT",
            state={"act": 1},
            candidates=[
                {"candidate_id": "relic:a", "local_score": 10.0},
                {"candidate_id": "relic:b", "local_score": 9.0},
            ],
            rule_choice_id="relic:a",
            binding={"state_seq": 1},
        )
        self.assertEqual("recommended", result.status)
        body = client.calls[0][0]
        self.assertEqual("deepseek-v4-flash", body["model"])
        self.assertEqual("none", body["reasoning"]["effort"])
        self.assertEqual(0.0, body["temperature"])
        self.assertEqual("deepseek-v4-flash", result.requested_model)

    def test_identical_state_uses_local_cache_without_second_api_call(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        first = self.request(advisor)
        second = self.request(advisor)
        self.assertEqual(1, len(client.calls))
        self.assertFalse(first.local_cache_hit)
        self.assertTrue(second.local_cache_hit)
        self.assertEqual({}, second.usage)
        self.assertEqual(
            first.usage["estimated_cost_usd"], advisor.total_cost_usd
        )

    def test_disk_cache_survives_advisor_restart_without_rebilling(self):
        cache_root = Path(self.temp.name) / "restart-cache"
        first_client = FakeModelClient()
        first_advisor = DeepSeekMacroAdvisor(
            self.config,
            self.assembler,
            client=first_client,
            cache=LocalAdviceCache(cache_root),
        )
        first = self.request(first_advisor)
        self.assertEqual("recommended", first.status)

        second_client = FakeModelClient()
        second_advisor = DeepSeekMacroAdvisor(
            self.config,
            self.assembler,
            client=second_client,
            cache=LocalAdviceCache(cache_root),
        )
        second = self.request(second_advisor)
        self.assertTrue(second.local_cache_hit)
        self.assertEqual({}, second.usage)
        self.assertEqual([], second_client.calls)
        self.assertEqual(0.0, second_advisor.total_cost_usd)

    def test_real_chat_adapter_contract_round_trips_through_advisor(self):
        captured = {}

        def opener(request, timeout):
            body = json.loads(request.data.decode("utf-8"))
            captured["body"] = body
            dynamic = json.loads(body["messages"][-1]["content"])
            candidates = [
                item["candidate_id"] for item in dynamic["candidates"]
            ]
            advice = {
                "schema_version": "sts-advice-v1",
                "advice_id": dynamic["binding"]["advice_id"],
                "choice_id": candidates[0],
                "confidence": 0.9,
                "rankings": [
                    {"candidate_id": item, "score": 90 - index * 10}
                    for index, item in enumerate(candidates)
                ],
                "reason_codes": ["DECK_SYNERGY"],
                "rationale": "Grounded adapter seam test.",
            }
            return FakeHTTPResponse({
                "model": "deepseek-v4-flash",
                "system_fingerprint": "adapter-fp",
                "choices": [{"message": {"content": json.dumps(advice)}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            })

        config = replace(
            self.config,
            primary_transport="chat_json",
        )
        client = DeepSeekResponsesClient(
            config, api_key="test-key", opener=opener
        )
        advisor = DeepSeekMacroAdvisor(
            config,
            PromptAssembler(ROOT, config),
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "adapter-cache"),
        )
        result = self.request(advisor)
        self.assertEqual("recommended", result.status)
        self.assertEqual("chat_json", result.transport)
        self.assertEqual("disabled", captured["body"]["thinking"]["type"])
        self.assertTrue(
            any(message["content"].startswith(
                "STATIC_OUTPUT_CONTRACT\n"
            ) for message in captured["body"]["messages"])
        )
        self.assertNotIn(
            "STRICT_OUTPUT_CONTRACT",
            captured["body"]["messages"][-1]["content"],
        )

    def test_corrupt_local_cache_is_ignored_and_refetched(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        first = self.request(advisor)
        advisor.cache._memory[first.advice_id]["rankings"] = [
            {"candidate_id": "invented", "score": 100}
        ]
        second = self.request(advisor)
        self.assertEqual("recommended", second.status)
        self.assertFalse(second.local_cache_hit)
        self.assertEqual(2, len(client.calls))

    def test_changed_state_hash_does_not_reuse_cache(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        self.request(advisor)
        advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"act": 1, "hp": {"current": 59, "maximum": 75}},
            candidates=[
                {"candidate_id": "card:a", "local_score": 10.0},
                {"candidate_id": "skip", "local_score": 8.0},
            ],
            rule_choice_id="card:a",
            hard_constraints=["ONLY_LISTED"],
            binding={"attempt_id": "a", "run_id": "r", "state_seq": 2},
        )
        self.assertEqual(2, len(client.calls))

    def test_missing_key_is_a_non_throwing_fallback(self):
        result = self.request(self.advisor(FakeModelClient(available=False)))
        self.assertEqual("fallback", result.status)
        self.assertEqual("missing_api_key", result.error_class)

    def test_default_assist_scope_includes_selective_event_advice(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        result = advisor.advise(
            decision_type="EVENT",
            character="DEFECT",
            state={},
            candidates=[
                {"candidate_id": "event:0", "local_score": 2},
                {"candidate_id": "event:1", "local_score": 1},
            ],
            rule_choice_id="event:0",
            binding={},
        )
        self.assertEqual("recommended", result.status)
        self.assertEqual(1, len(client.calls))

    def test_invalid_json_falls_back(self):
        def invalid(_body):
            return {
                "model": "deepseek-v4-flash",
                "output_text": "not json",
            }

        result = self.request(self.advisor(FakeModelClient(invalid)))
        self.assertEqual("fallback", result.status)
        self.assertEqual("invalid_advice_json", result.error_class)

    def test_malformed_responses_output_shape_falls_back(self):
        def invalid(_body):
            return {
                "model": "deepseek-v4-flash",
                "output": 7,
                "usage": {},
            }

        result = self.request(self.advisor(FakeModelClient(invalid)))
        self.assertEqual("fallback", result.status)
        self.assertEqual("invalid_response_shape", result.error_class)

    def test_unknown_choice_falls_back(self):
        def invalid(body):
            response = valid_response_for_body(body)
            value = json.loads(response["output_text"])
            value["choice_id"] = "card:invented"
            response["output_text"] = json.dumps(value)
            return response

        result = self.request(self.advisor(FakeModelClient(invalid)))
        self.assertEqual("unknown_choice", result.error_class)

    def test_duplicate_ranking_falls_back(self):
        def invalid(body):
            response = valid_response_for_body(body)
            value = json.loads(response["output_text"])
            value["rankings"].append(dict(value["rankings"][0]))
            response["output_text"] = json.dumps(value)
            return response

        result = self.request(self.advisor(FakeModelClient(invalid)))
        self.assertEqual("invalid_ranking_candidate", result.error_class)

    def test_missing_candidate_ranking_falls_back(self):
        def invalid(body):
            response = valid_response_for_body(body)
            value = json.loads(response["output_text"])
            value["rankings"] = value["rankings"][:1]
            response["output_text"] = json.dumps(value)
            return response

        result = self.request(self.advisor(FakeModelClient(invalid)))
        self.assertEqual("incomplete_rankings", result.error_class)

    def test_ranking_metadata_is_ignored_after_bound_fields_validate(self):
        def repairable(body):
            response = valid_response_for_body(body)
            value = json.loads(response["output_text"])
            value["rankings"][0]["explanation"] = "local evidence"
            response["output_text"] = json.dumps(value)
            return response

        result = self.request(self.advisor(FakeModelClient(repairable)))

        self.assertEqual("recommended", result.status)
        self.assertEqual("card:a", result.model_choice_id)
        self.assertEqual(
            {"candidate_id", "score"}, set(result.rankings[0])
        )

    def test_model_schema_echo_is_normalized_after_bound_fields_validate(self):
        def repairable(body):
            return valid_response_for_body(
                body, schema_version="model-invented-version"
            )

        result = self.request(self.advisor(FakeModelClient(repairable)))

        self.assertEqual("recommended", result.status)
        self.assertEqual("card:a", result.model_choice_id)

    def test_non_string_schema_echo_still_falls_back(self):
        for schema_version in (None, 7, ""):
            with self.subTest(schema_version=schema_version):
                def invalid(body, value=schema_version):
                    return valid_response_for_body(
                        body, schema_version=value
                    )

                result = self.request(self.advisor(FakeModelClient(invalid)))
                self.assertEqual("schema_mismatch", result.error_class)

    def test_unknown_and_duplicate_reason_codes_are_safely_normalized(self):
        def repairable(body):
            return valid_response_for_body(
                body,
                reason_codes=[
                    "SURVIVAL", "INVENTED", "SURVIVAL", "DECK_SYNERGY",
                ],
            )

        result = self.request(self.advisor(FakeModelClient(repairable)))

        self.assertEqual("recommended", result.status)
        self.assertEqual(["SURVIVAL", "DECK_SYNERGY"], result.reason_codes)

    def test_all_unknown_reason_codes_do_not_discard_valid_ranking(self):
        def repairable(body):
            return valid_response_for_body(body, reason_codes=["INVENTED"])

        result = self.request(self.advisor(FakeModelClient(repairable)))

        self.assertEqual("recommended", result.status)
        self.assertEqual([], result.reason_codes)

    def test_non_string_reason_codes_still_fall_back(self):
        for reason_codes in ("SURVIVAL", ["SURVIVAL", 7]):
            with self.subTest(reason_codes=reason_codes):
                def invalid(body, codes=reason_codes):
                    return valid_response_for_body(body, reason_codes=codes)

                result = self.request(self.advisor(FakeModelClient(invalid)))
                self.assertEqual("invalid_reason_codes", result.error_class)

    def test_nan_confidence_falls_back(self):
        def invalid(body):
            return valid_response_for_body(body, confidence=float("nan"))

        result = self.request(self.advisor(FakeModelClient(invalid)))
        self.assertEqual("invalid_confidence", result.error_class)

    def test_finite_unbounded_ranking_scores_are_normalized(self):
        def unbounded(body):
            return valid_response_for_body(
                body,
                choice_id="card:a",
                rankings=[
                    {"candidate_id": "card:a", "score": 12.5},
                    {"candidate_id": "skip", "score": -8.0},
                ],
            )

        result = self.request(self.advisor(FakeModelClient(unbounded)))
        self.assertEqual("recommended", result.status)
        self.assertEqual("card:a", result.rankings[0]["candidate_id"])
        self.assertEqual(100.0, result.rankings[0]["score"])
        self.assertEqual(0.0, result.rankings[1]["score"])

    def test_extreme_finite_ranking_scores_normalize_without_overflow(self):
        def extreme(body):
            return valid_response_for_body(
                body,
                rankings=[
                    {"candidate_id": "card:a", "score": 1e308},
                    {"candidate_id": "skip", "score": -1e308},
                ],
            )

        result = self.request(self.advisor(FakeModelClient(extreme)))
        self.assertEqual("recommended", result.status)
        self.assertEqual([100.0, 0.0], [
            item["score"] for item in result.rankings
        ])

    def test_invalid_nonfinite_or_non_numeric_ranking_scores_fall_back(self):
        for index, score in enumerate((True, "90", float("nan"), float("inf"))):
            with self.subTest(score=score):
                def invalid(body, invalid_score=score):
                    return valid_response_for_body(
                        body,
                        rankings=[
                            {"candidate_id": "card:a", "score": invalid_score},
                            {"candidate_id": "skip", "score": 0},
                        ],
                    )

                result = self.request(self.advisor(FakeModelClient(invalid)))
                self.assertEqual("invalid_ranking_score", result.error_class)

    def test_chosen_candidate_stays_first_when_highest_scores_tie(self):
        def tied(body):
            return valid_response_for_body(
                body,
                choice_id="skip",
                rankings=[
                    {"candidate_id": "card:a", "score": 90},
                    {"candidate_id": "skip", "score": 90},
                ],
            )

        result = self.request(self.advisor(FakeModelClient(tied)))
        self.assertEqual("recommended", result.status)
        self.assertEqual("skip", result.rankings[0]["candidate_id"])

    def test_malformed_usage_counters_cannot_break_recommendation(self):
        def malformed(body):
            response = valid_response_for_body(body)
            response["usage"] = {
                "input_tokens": "not-a-number",
                "output_tokens": None,
                "prompt_cache_hit_tokens": float("inf"),
            }
            return response

        result = self.request(self.advisor(FakeModelClient(malformed)))
        self.assertEqual("recommended", result.status)
        self.assertEqual(0, result.usage["input_tokens"])
        self.assertEqual(0, result.usage["output_tokens"])

    def test_huge_usage_counters_are_bounded(self):
        def huge(body):
            response = valid_response_for_body(body)
            response["usage"] = {
                "input_tokens": 10 ** 10000,
                "output_tokens": 10 ** 10000,
                "prompt_cache_hit_tokens": 10 ** 10000,
            }
            return response

        result = self.request(self.advisor(FakeModelClient(huge)))
        self.assertEqual("recommended", result.status)
        self.assertLessEqual(result.usage["input_tokens"], 2_000_000_000)
        self.assertLessEqual(result.usage["output_tokens"], 1_000_000_000)
        self.assertTrue(math.isfinite(result.usage["estimated_cost_usd"]))

    def test_unclassified_prompt_tokens_are_billed_as_cache_misses(self):
        def incomplete(body):
            response = valid_response_for_body(body)
            response["usage"] = {
                "input_tokens": 1000,
                "output_tokens": 10,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 0,
            }
            return response

        result = self.request(self.advisor(FakeModelClient(incomplete)))
        self.assertEqual(1000, result.usage["prompt_cache_miss_tokens"])
        self.assertGreater(result.usage["estimated_cost_usd"], 0)

    def test_long_audit_rationale_is_bounded_without_discarding_ranking(self):
        def verbose(body):
            return valid_response_for_body(body, rationale="x" * 500)

        result = self.request(self.advisor(FakeModelClient(verbose)))
        self.assertEqual("recommended", result.status)
        self.assertEqual(240, len(result.rationale))

    def test_retryable_error_retries_once(self):
        config = replace(self.config, retry_count=1)
        client = FakeModelClient(errors=[
            DeepSeekRequestError("http_error", status=500, retryable=True)
        ])
        advisor = DeepSeekMacroAdvisor(
            config,
            PromptAssembler(ROOT, config),
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "retry-cache"),
        )
        result = self.request(advisor)
        self.assertEqual("recommended", result.status)
        self.assertEqual(2, len(client.calls))

    def test_retries_share_one_total_deadline(self):
        class SlowFailingClient(FakeModelClient):
            def request(self, body, timeout):
                self.calls.append((body, timeout))
                time.sleep(0.02)
                raise DeepSeekRequestError("timeout", retryable=True)

        policy = replace(
            self.config.decision_policies["CARD_REWARD"],
            timeout_seconds=0.06,
        )
        config = replace(
            self.config,
            retry_count=1,
            decision_policies={
                **self.config.decision_policies,
                "CARD_REWARD": policy,
            },
        )
        client = SlowFailingClient()
        advisor = DeepSeekMacroAdvisor(
            config,
            PromptAssembler(ROOT, config),
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "deadline-cache"),
        )
        result = self.request(advisor)
        self.assertEqual("timeout", result.error_class)
        self.assertEqual(2, len(client.calls))
        self.assertLess(client.calls[1][1], client.calls[0][1])
        self.assertLess(result.latency_ms, 100)

    def test_schema_rejection_uses_json_mode_then_local_validation(self):
        class SchemaFallbackClient(FakeModelClient):
            def request(self, body, timeout):
                self.calls.append((body, timeout))
                raise DeepSeekRequestError("http_error", status=400)

            def request_json_fallback(self, body, timeout):
                self.calls.append(("fallback", timeout))
                return valid_response_for_body(body)

        client = SchemaFallbackClient()
        result = self.request(self.advisor(client))
        self.assertEqual("recommended", result.status)
        self.assertEqual("chat_json_fallback", result.transport)
        self.assertEqual(2, len(client.calls))

    def test_responses_schema_fallback_stays_on_chat_for_retry(self):
        class StickyFallbackClient(FakeModelClient):
            def __init__(self):
                super().__init__()
                self.chat_attempts = 0

            def request(self, body, timeout):
                self.calls.append(("responses", timeout))
                raise DeepSeekRequestError("http_error", status=400)

            def request_json_fallback(self, body, timeout):
                self.calls.append(("chat", timeout))
                self.chat_attempts += 1
                if self.chat_attempts == 1:
                    raise DeepSeekRequestError(
                        "http_error", status=500, retryable=True
                    )
                return valid_response_for_body(body)

        config = replace(self.config, retry_count=1)
        client = StickyFallbackClient()
        advisor = DeepSeekMacroAdvisor(
            config,
            PromptAssembler(ROOT, config),
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "sticky-cache"),
        )
        result = self.request(advisor)
        self.assertEqual("recommended", result.status)
        self.assertEqual("chat_json_fallback", result.transport)
        self.assertEqual(
            ["responses", "chat", "chat"],
            [item[0] for item in client.calls],
        )

    def test_responses_schema_fallback_is_sticky_across_decisions(self):
        class StickyAcrossDecisionsClient(FakeModelClient):
            def request(self, body, timeout):
                self.calls.append(("responses", timeout))
                raise DeepSeekRequestError("http_error", status=400)

            def request_json_fallback(self, body, timeout):
                self.calls.append(("chat", timeout))
                return valid_response_for_body(body)

        client = StickyAcrossDecisionsClient()
        advisor = self.advisor(client)
        first = self.request(advisor)
        second = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"changed": True},
            candidates=[
                {"candidate_id": "card:a", "local_score": 2},
                {"candidate_id": "skip", "local_score": 1},
            ],
            rule_choice_id="card:a",
            binding={"state_seq": 2},
        )
        self.assertEqual("recommended", first.status)
        self.assertEqual("recommended", second.status)
        self.assertEqual(
            ["responses", "chat", "chat"],
            [item[0] for item in client.calls],
        )

    def test_retry_call_budget_preserves_the_provider_failure(self):
        config = replace(
            self.config,
            retry_count=1,
            max_calls_per_run=1,
        )
        client = FakeModelClient(errors=[
            DeepSeekRequestError("timeout", retryable=True)
        ])
        advisor = DeepSeekMacroAdvisor(
            config,
            PromptAssembler(ROOT, config),
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "call-cap-cache"),
        )
        result = self.request(advisor)
        self.assertEqual("timeout", result.error_class)
        self.assertEqual(1, advisor.consecutive_failures)

    def test_missing_response_model_fails_closed(self):
        def missing_model(body):
            response = valid_response_for_body(body)
            response.pop("model")
            return response

        result = self.request(self.advisor(FakeModelClient(missing_model)))
        self.assertEqual("unexpected_response_model", result.error_class)

    def test_chat_json_primary_does_not_probe_responses_first(self):
        class ChatPrimaryClient(FakeModelClient):
            def request(self, body, timeout):
                raise AssertionError("Responses transport must not be called")

            def request_json_fallback(self, body, timeout):
                self.calls.append(("chat_json", timeout))
                return valid_response_for_body(body)

        config = replace(self.config, primary_transport="chat_json")
        client = ChatPrimaryClient()
        advisor = DeepSeekMacroAdvisor(
            config,
            PromptAssembler(ROOT, config),
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "chat-primary-cache"),
        )
        result = self.request(advisor)
        self.assertEqual("recommended", result.status)
        self.assertEqual("chat_json", result.transport)
        self.assertEqual("chat_json", client.calls[0][0])
        self.assertGreater(client.calls[0][1], 7.9)
        self.assertLessEqual(client.calls[0][1], 8.0)

    def test_chat_json_primary_400_is_not_repeated_as_same_fallback(self):
        class RejectingChatClient(FakeModelClient):
            def request(self, body, timeout):
                raise AssertionError("Responses transport must not be called")

            def request_json_fallback(self, body, timeout):
                self.calls.append(("chat_json", timeout))
                raise DeepSeekRequestError("http_error", status=400)

        config = replace(self.config, primary_transport="chat_json")
        client = RejectingChatClient()
        advisor = DeepSeekMacroAdvisor(
            config,
            PromptAssembler(ROOT, config),
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "chat-400-cache"),
        )
        result = self.request(advisor)
        self.assertEqual("http_error", result.error_class)
        self.assertEqual(1, len(client.calls))
        self.assertTrue(advisor.circuit_open)
        second = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"new": True},
            candidates=[
                {"candidate_id": "card:a", "local_score": 2},
                {"candidate_id": "skip", "local_score": 1},
            ],
            rule_choice_id="card:a",
            binding={"state_seq": 2},
        )
        self.assertEqual("circuit_open", second.error_class)
        self.assertEqual(1, len(client.calls))

    def test_warmup_uses_primary_chat_transport_and_exact_stable_input(self):
        class WarmupChatClient(FakeModelClient):
            def request(self, body, timeout):
                raise AssertionError("Responses transport must not be called")

            def request_json_fallback(self, body, timeout):
                self.calls.append((body, timeout))
                return {
                    "model": "deepseek-v4-flash",
                    "system_fingerprint": "warm-fp",
                    "usage": {},
                }

        config = replace(self.config, primary_transport="chat_json")
        client = WarmupChatClient()
        events = []
        assembler = PromptAssembler(ROOT, config)
        advisor = DeepSeekMacroAdvisor(
            config,
            assembler,
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "warm-cache"),
            callback=events.append,
        )
        with patch(
            "deepseek_macro.time.monotonic",
            side_effect=[100.0, 100.25],
        ):
            advisor._execute_warmup("DEFECT")
        self.assertEqual(assembler.stable_input("DEFECT"), client.calls[0][0]["input"])
        self.assertEqual("chat_json", events[-1]["advisor"]["transport"])
        self.assertEqual(0, advisor.calls)
        self.assertEqual(1, advisor.warmup_calls)
        self.assertAlmostEqual(0.25, advisor.total_wait_seconds)

    def test_failed_warmup_wait_is_counted_exactly_once(self):
        client = FakeModelClient(errors=[
            DeepSeekRequestError("timeout", retryable=True)
        ])
        events = []
        advisor = DeepSeekMacroAdvisor(
            self.config,
            self.assembler,
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "warm-fail-cache"),
            callback=events.append,
        )
        with patch(
            "deepseek_macro.time.monotonic",
            side_effect=[100.0, 100.25],
        ):
            advisor._execute_warmup("DEFECT")
        self.assertAlmostEqual(0.25, advisor.total_wait_seconds)
        self.assertEqual(250, events[-1]["advisor"]["latency_ms"])

    def test_forced_chat_warmup_uses_chat_fallback_method(self):
        class ForcedChatClient(FakeModelClient):
            def request(self, body, timeout):
                raise AssertionError("Responses transport must not be called")

            def request_json_fallback(self, body, timeout):
                self.calls.append((body, timeout))
                return {
                    "model": "deepseek-v4-flash",
                    "system_fingerprint": "forced-warm-fp",
                    "usage": {},
                }

        client = ForcedChatClient()
        events = []
        advisor = DeepSeekMacroAdvisor(
            self.config,
            self.assembler,
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "forced-warm-cache"),
            callback=events.append,
        )
        advisor._force_chat_transport = True
        advisor._execute_warmup("DEFECT")
        self.assertEqual(1, len(client.calls))
        self.assertEqual(
            "chat_json_fallback", events[-1]["advisor"]["transport"]
        )

    def test_account_error_disables_future_requests_immediately(self):
        client = FakeModelClient(errors=[DeepSeekRequestError("account", status=402)])
        advisor = self.advisor(client)
        first = self.request(advisor)
        self.assertEqual("account", first.error_class)
        self.assertTrue(advisor.disabled)
        second = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"new": True},
            candidates=[
                {"candidate_id": "card:a", "local_score": 2},
                {"candidate_id": "skip", "local_score": 1},
            ],
            rule_choice_id="card:a",
            binding={"state_seq": 2},
        )
        self.assertEqual("advisor_disabled", second.error_class)
        self.assertEqual(1, len(client.calls))

    def test_clear_deterministic_margin_skips_api_in_assist(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        result = self.request(advisor, candidates=[
            {"candidate_id": "card:a", "local_score": 40.0},
            {"candidate_id": "skip", "local_score": 0.0},
        ])
        self.assertEqual("skipped", result.status)
        self.assertEqual("deterministic_margin", result.error_class)
        self.assertEqual([], client.calls)

    def test_clear_deterministic_margin_skips_shadow_remote_call(self):
        """Shadow keeps disputed calibration cases, not obvious local wins."""

        config = replace(self.config, mode="shadow")
        client = FakeModelClient()
        advisor = DeepSeekMacroAdvisor(
            config,
            PromptAssembler(ROOT, config),
            client=client,
            cache=LocalAdviceCache(Path(self.temp.name) / "shadow-margin-cache"),
        )

        result = self.request(advisor, candidates=[
            {"candidate_id": "card:a", "local_score": 40.0},
            {"candidate_id": "skip", "local_score": 0.0},
        ])

        self.assertEqual("skipped", result.status)
        self.assertEqual("deterministic_margin", result.error_class)
        self.assertEqual([], client.calls)
        advisor.close(wait=True)

    def test_adaptive_local_confidence_skips_medium_margin_without_uncertainty(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        # CARD_REWARD consult_margin=10; the adaptive 0.75 band treats a
        # nine-point local lead as sufficiently stable, while still leaving
        # genuinely close choices to the adviser.
        result = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"act": 1},
            candidates=[
                {"candidate_id": "card:a", "local_score": 9.0},
                {"candidate_id": "skip", "local_score": 0.0},
            ],
            rule_choice_id="card:a",
            binding={"state_seq": 1},
        )
        self.assertEqual("skipped", result.status)
        self.assertEqual("adaptive_local_confidence", result.error_class)
        self.assertEqual([], client.calls)

    def test_tighter_card_reward_band_skips_five_point_two_choice_lead(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        result = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"act": 1},
            candidates=[
                {"candidate_id": "card:a", "local_score": 5.0},
                {"candidate_id": "skip", "local_score": 0.0},
            ],
            rule_choice_id="card:a",
            binding={"state_seq": 1},
        )
        self.assertEqual("skipped", result.status)
        self.assertEqual("adaptive_local_confidence", result.error_class)
        self.assertEqual([], client.calls)

    def test_more_legal_card_choices_expand_the_close_review_band(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        result = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"act": 1},
            candidates=[
                {"candidate_id": "card:a", "local_score": 5.0},
                {"candidate_id": "card:b", "local_score": 0.0},
                {"candidate_id": "card:c", "local_score": -1.0},
                {"candidate_id": "skip", "local_score": -2.0},
            ],
            rule_choice_id="card:a",
            binding={"state_seq": 1},
        )
        self.assertEqual("recommended", result.status)
        self.assertEqual(1, len(client.calls))

    def test_adaptive_band_never_suppresses_explicit_uncertainty(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        result = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"act": 1, "decision_context": {"high_impact": True}},
            candidates=[
                {"candidate_id": "card:a", "local_score": 9.0},
                {"candidate_id": "skip", "local_score": 0.0},
            ],
            rule_choice_id="card:a",
            binding={"state_seq": 1},
        )
        self.assertEqual("recommended", result.status)
        self.assertEqual(1, len(client.calls))

    def test_adaptive_repeat_surface_never_suppresses_explicit_uncertainty(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        candidates = [
            {
                "candidate_id": "card:a",
                "local_score": 10.0,
                "uncertainty": "future_upgrade_unknown",
            },
            {"candidate_id": "skip", "local_score": 8.0},
        ]
        first = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"act": 1},
            candidates=candidates,
            rule_choice_id="card:a",
            binding={"state_seq": 1},
        )
        self.assertEqual("recommended", first.status)
        second = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"act": 1},
            candidates=candidates,
            rule_choice_id="card:a",
            binding={"state_seq": 2},
        )
        self.assertEqual("recommended", second.status)
        self.assertEqual(2, len(client.calls))

    def test_adaptive_repeat_surface_skips_only_after_state_sequence_changes(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        first = self.request(advisor)
        self.assertEqual("recommended", first.status)
        # Same state and same sequence remains a normal cache hit.
        second = self.request(advisor)
        self.assertTrue(second.local_cache_hit)
        # A new protocol frame with the same meaningful surface does not
        # re-query the remote model.
        third = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"act": 1, "hp": {"current": 60, "maximum": 75}},
            candidates=[
                {"candidate_id": "card:a", "local_score": 10.0},
                {"candidate_id": "skip", "local_score": 8.0},
            ],
            rule_choice_id="card:a",
            binding={"attempt_id": "a", "run_id": "r", "state_seq": 2},
        )
        self.assertEqual("skipped", third.status)
        self.assertEqual("adaptive_repeat_surface", third.error_class)
        self.assertEqual(1, len(client.calls))

    def test_critical_policy_bypasses_adaptive_band(self):
        client = FakeModelClient()
        advisor = self.advisor(client)
        result = advisor.advise(
            decision_type="BOSS_RELIC",
            character="DEFECT",
            state={"act": 1},
            candidates=[
                {"candidate_id": "relic:a", "local_score": 40.0},
                {"candidate_id": "relic:b", "local_score": 0.0},
            ],
            rule_choice_id="relic:a",
            binding={"state_seq": 1},
        )
        self.assertEqual("recommended", result.status)
        self.assertEqual(1, len(client.calls))

    def test_changed_release_fingerprint_opens_circuit(self):
        calls = {"count": 0}

        def changing(body):
            calls["count"] += 1
            response = valid_response_for_body(body)
            response["system_fingerprint"] = f"fingerprint-{calls['count']}"
            return response

        client = FakeModelClient(changing)
        advisor = self.advisor(client)
        first = self.request(advisor)
        self.assertEqual("recommended", first.status)
        second = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"changed": True},
            candidates=[
                {"candidate_id": "card:a", "local_score": 10.0},
                {"candidate_id": "skip", "local_score": 8.0},
            ],
            rule_choice_id="card:a",
            binding={"state_seq": 2},
        )
        self.assertEqual("model_fingerprint_changed", second.error_class)
        self.assertTrue(advisor.circuit_open)

    def test_corrupt_fingerprint_store_fails_closed_before_cache_write(self):
        cache_root = Path(self.temp.name) / "corrupt-fingerprint-cache"
        cache_root.mkdir()
        (cache_root / "release-fingerprints.json").write_text(
            "not-json", encoding="utf-8"
        )
        client = FakeModelClient()
        advisor = DeepSeekMacroAdvisor(
            self.config,
            self.assembler,
            client=client,
            cache=LocalAdviceCache(cache_root),
        )
        result = self.request(advisor)
        self.assertEqual("fingerprint_store_error", result.error_class)
        self.assertTrue(advisor.circuit_open)
        self.assertGreater(result.usage["estimated_cost_usd"], 0)

    def test_invalid_response_fingerprint_shape_fails_closed(self):
        def invalid_fingerprint(body):
            response = valid_response_for_body(body)
            response["system_fingerprint"] = {"bad": True}
            return response

        advisor = self.advisor(FakeModelClient(invalid_fingerprint))
        result = self.request(advisor)
        self.assertEqual("fingerprint_store_error", result.error_class)
        self.assertTrue(advisor.circuit_open)

    def test_invalid_billable_response_records_usage_transport_and_cost(self):
        def invalid(body):
            response = valid_response_for_body(body)
            value = json.loads(response["output_text"])
            value["rankings"][0]["score"] = "bad"
            response["output_text"] = json.dumps(value)
            return response

        advisor = self.advisor(FakeModelClient(invalid))
        result = self.request(advisor)
        self.assertEqual("invalid_ranking_score", result.error_class)
        self.assertEqual("responses", result.transport)
        self.assertGreater(result.usage["estimated_cost_usd"], 0)
        self.assertEqual(
            result.usage["estimated_cost_usd"], advisor.total_cost_usd
        )

    def test_content_failures_do_not_combine_with_timeout_to_open_circuit(self):
        class ScriptedClient(FakeModelClient):
            def __init__(self):
                super().__init__()
                self.index = 0

            def request(self, body, timeout):
                self.calls.append((body, timeout))
                self.index += 1
                if self.index <= 2:
                    response = valid_response_for_body(body)
                    value = json.loads(response["output_text"])
                    value["rankings"][0]["score"] = "bad"
                    response["output_text"] = json.dumps(value)
                    return response
                if self.index == 3:
                    raise DeepSeekRequestError("timeout", retryable=True)
                return valid_response_for_body(body)

        client = ScriptedClient()
        advisor = self.advisor(client)
        results = []
        for index in range(4):
            results.append(advisor.advise(
                decision_type="CARD_REWARD",
                character="DEFECT",
                state={"index": index},
                candidates=[
                    {"candidate_id": "card:a", "local_score": 2},
                    {"candidate_id": "skip", "local_score": 1},
                ],
                rule_choice_id="card:a",
                binding={"state_seq": index},
            ))
        self.assertEqual(
            ["invalid_ranking_score", "invalid_ranking_score", "timeout", None],
            [item.error_class for item in results],
        )
        self.assertEqual("recommended", results[-1].status)
        self.assertFalse(advisor.circuit_open)

    def test_three_failures_open_circuit(self):
        client = FakeModelClient(errors=[
            DeepSeekRequestError("timeout", retryable=True) for _ in range(3)
        ])
        advisor = self.advisor(client)
        for index in range(3):
            result = advisor.advise(
                decision_type="CARD_REWARD",
                character="DEFECT",
                state={"index": index},
                candidates=[
                    {"candidate_id": "card:a", "local_score": 1},
                    {"candidate_id": "skip", "local_score": 0},
                ],
                rule_choice_id="card:a",
                binding={"state_seq": index},
            )
            self.assertEqual("fallback", result.status)
        self.assertTrue(advisor.circuit_open)
        fourth = advisor.advise(
            decision_type="CARD_REWARD",
            character="DEFECT",
            state={"index": 4},
            candidates=[
                {"candidate_id": "card:a", "local_score": 1},
                {"candidate_id": "skip", "local_score": 0},
            ],
            rule_choice_id="card:a",
            binding={"state_seq": 4},
        )
        self.assertEqual("circuit_open", fourth.error_class)
        self.assertEqual(3, len(client.calls))

    def test_budget_blockers_are_reported_precisely(self):
        cases = {
            "advisor_disabled": ("disabled", True),
            "circuit_open": ("circuit_open", True),
            "call_budget": ("calls", self.config.max_calls_per_run),
            "wait_budget": (
                "total_wait_seconds", self.config.max_total_wait_seconds
            ),
            "cost_budget": (
                "total_cost_usd", self.config.max_cost_per_run_usd
            ),
        }
        for expected, (attribute, value) in cases.items():
            with self.subTest(expected=expected):
                advisor = self.advisor(FakeModelClient())
                setattr(advisor, attribute, value)
                self.assertEqual(expected, advisor._budget_blocker())

    def test_shadow_returns_immediately_and_emits_later(self):
        config = replace(self.config, mode="shadow")
        events = []
        advisor = DeepSeekMacroAdvisor(
            config,
            PromptAssembler(ROOT, config),
            client=FakeModelClient(),
            cache=LocalAdviceCache(Path(self.temp.name) / "shadow-cache"),
            callback=events.append,
        )
        result = self.request(advisor)
        self.assertEqual("shadow_queued", result.status)
        advisor.close(wait=True)
        self.assertTrue(events)
        self.assertEqual("shadow", events[-1]["advisor"]["status"])

    def test_shadow_worker_survives_unexpected_client_exception(self):
        class ExplodingClient(FakeModelClient):
            def request(self, body, timeout):
                raise RuntimeError("external failure")

        config = replace(self.config, mode="shadow")
        events = []
        advisor = DeepSeekMacroAdvisor(
            config,
            PromptAssembler(ROOT, config),
            client=ExplodingClient(),
            cache=LocalAdviceCache(Path(self.temp.name) / "shadow-fail-cache"),
            callback=events.append,
        )
        self.request(advisor)
        advisor.close(wait=True)
        self.assertEqual("fallback", events[-1]["advisor"]["status"])
        self.assertEqual(
            "advisor_exception", events[-1]["advisor"]["error_class"]
        )


class AgentFusionTests(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))
        from spirecomm.ai.agent import SimpleAgent
        from spirecomm.spire.card import CardRarity, CardType
        from spirecomm.spire.character import PlayerClass
        from spirecomm.spire.game import Game

        self.SimpleAgent = SimpleAgent
        self.CardRarity = CardRarity
        self.CardType = CardType
        self.PlayerClass = PlayerClass
        self.Game = Game

    def agent(self, result, mode="assist"):
        config = replace(MacroPolicyConfig(), mode=mode)

        class FakeAdvisor:
            def __init__(self):
                self.config = config

            def advise(self, **_kwargs):
                return result

        agent = self.SimpleAgent(
            self.PlayerClass.DEFECT,
            goal_mode="HEART",
            macro_advisor=FakeAdvisor(),
        )
        game = self.Game()
        game.character = self.PlayerClass.DEFECT
        game.act = 1
        game.floor = 1
        game.current_hp = 70
        game.max_hp = 75
        game.gold = 0
        game.deck = []
        game.relics = []
        game.potions = []
        agent.game = game
        agent.set_macro_protocol_context({"state_seq": 1, "decision_id": "d"})
        return agent

    def test_high_confidence_close_candidate_can_be_applied(self):
        result = AdvisorResult(
            status="recommended",
            model_choice_id="b",
            confidence=0.95,
            rankings=[
                {"candidate_id": "b", "score": 95},
                {"candidate_id": "a", "score": 70},
            ],
        )
        agent = self.agent(result)
        selected = agent._macro_rank(
            "CARD_REWARD",
            [
                {"candidate_id": "a", "local_score": 10},
                {"candidate_id": "b", "local_score": 8},
            ],
            "a",
        )
        self.assertEqual(["b"], selected)
        self.assertTrue(agent._pending_macro_advice["applied"])

    def test_low_confidence_keeps_rule_choice(self):
        result = AdvisorResult(
            status="recommended",
            model_choice_id="b",
            confidence=0.2,
            rankings=[{"candidate_id": "b", "score": 95}],
        )
        agent = self.agent(result)
        selected = agent._macro_rank(
            "CARD_REWARD",
            [
                {"candidate_id": "a", "local_score": 10},
                {"candidate_id": "b", "local_score": 8},
            ],
            "a",
        )
        self.assertEqual(["a"], selected)
        self.assertEqual("low_confidence", agent._pending_macro_advice["status"])

    def test_model_agreement_is_not_reported_as_an_applied_override(self):
        result = AdvisorResult(
            status="recommended",
            model_choice_id="a",
            confidence=0.95,
            rankings=[
                {"candidate_id": "a", "score": 95},
                {"candidate_id": "b", "score": 70},
            ],
        )
        agent = self.agent(result)
        selected = agent._macro_rank(
            "CARD_REWARD",
            [
                {"candidate_id": "a", "local_score": 10},
                {"candidate_id": "b", "local_score": 8},
            ],
            "a",
        )
        self.assertEqual(["a"], selected)
        self.assertFalse(agent._pending_macro_advice["applied"])
        self.assertEqual("agreed", agent._pending_macro_advice["status"])

    def test_advisor_exception_fails_closed_to_local_choice(self):
        agent = self.agent(AdvisorResult(status="recommended"))

        def fail(**_kwargs):
            raise RuntimeError("provider details must not escape")

        agent.macro_advisor.advise = fail
        selected = agent._macro_rank(
            "CARD_REWARD",
            [
                {"candidate_id": "a", "local_score": 10},
                {"candidate_id": "b", "local_score": 8},
            ],
            "a",
        )
        self.assertEqual(["a"], selected)
        self.assertEqual("fallback", agent._pending_macro_advice["status"])
        self.assertEqual(
            "advisor_exception",
            agent._pending_macro_advice["error_class"],
        )
        self.assertNotIn(
            "provider details",
            json.dumps(agent._pending_macro_advice),
        )

    def test_regret_cap_prevents_large_override(self):
        result = AdvisorResult(
            status="recommended",
            model_choice_id="b",
            confidence=1.0,
            rankings=[{"candidate_id": "b", "score": 100}],
        )
        agent = self.agent(result)
        selected = agent._macro_rank(
            "CARD_REWARD",
            [
                {"candidate_id": "a", "local_score": 30},
                {"candidate_id": "b", "local_score": 0},
            ],
            "a",
        )
        self.assertEqual(["a"], selected)

    def test_strong_model_gap_can_correct_feed_skip_case(self):
        result = AdvisorResult(
            status="recommended",
            model_choice_id="feed",
            confidence=0.85,
            rankings=[
                {"candidate_id": "feed", "score": 100},
                {"candidate_id": "skip", "score": 40},
                {"candidate_id": "iron_wave", "score": 30},
                {"candidate_id": "clash", "score": 10},
            ],
        )
        agent = self.agent(result)
        selected = agent._macro_rank(
            "CARD_REWARD",
            [
                {"candidate_id": "feed", "local_score": -6.7},
                {"candidate_id": "skip", "local_score": 0.0},
                {"candidate_id": "iron_wave", "local_score": -18.0},
                {"candidate_id": "clash", "local_score": -32.0},
            ],
            "skip",
        )
        self.assertEqual(["feed"], selected)
        advice = agent._pending_macro_advice
        self.assertTrue(advice["applied"])
        self.assertEqual(60.0, advice["fusion"]["model_vs_rule_gap"])
        self.assertEqual(6.7, advice["fusion"]["local_regret"])
        self.assertEqual("CARD_REWARD", advice["decision_type"])
        self.assertEqual("skip", advice["replay"]["rule_choice_ids"][0])

    def test_medium_confidence_bonus_cannot_turn_skip_into_filler(self):
        result = AdvisorResult(
            status="recommended",
            model_choice_id="filler",
            confidence=0.75,
            rankings=[
                {"candidate_id": "filler", "score": 100},
                {"candidate_id": "skip", "score": 60},
            ],
        )
        agent = self.agent(result)
        selected = agent._macro_rank(
            "CARD_REWARD",
            [
                {"candidate_id": "skip", "kind": "skip", "local_score": 0},
                {
                    "candidate_id": "filler",
                    "kind": "card",
                    "local_score": -1,
                },
            ],
            "skip",
        )
        self.assertEqual(["skip"], selected)
        self.assertEqual(
            "abstention_guard", agent._pending_macro_advice["status"]
        )

    def test_single_choice_fusion_never_applies_unendorsed_third_candidate(self):
        # Regression from a live A1F13 shop.  The adviser strongly preferred
        # Lee's Waffle, while Energy and Skill Potion were tied at a low model
        # score.  The old all-candidate adjusted sort nevertheless changed the
        # local Energy Potion rule into Skill Potion by candidate-ID tie-break.
        result = AdvisorResult(
            status="recommended",
            model_choice_id="shop:relic:Lee's Waffle:8",
            confidence=0.9,
            rankings=[
                {"candidate_id": "shop:relic:Lee's Waffle:8", "score": 100},
                {"candidate_id": "shop:card:Burning Pact:2", "score": 70},
                {"candidate_id": "shop:potion:Energy Potion:10", "score": 20},
                {"candidate_id": "shop:potion:SkillPotion:11", "score": 20},
                {"candidate_id": "shop:leave", "score": 10},
            ],
        )
        agent = self.agent(result)
        selected = agent._macro_rank(
            "SHOP",
            [
                {
                    "candidate_id": "shop:relic:Lee's Waffle:8",
                    "local_score": 0.375,
                },
                {
                    "candidate_id": "shop:card:Burning Pact:2",
                    "local_score": -13.2778,
                },
                {
                    "candidate_id": "shop:potion:Energy Potion:10",
                    "local_score": 22.5455,
                },
                {
                    "candidate_id": "shop:potion:SkillPotion:11",
                    "local_score": 22.5455,
                },
                {"candidate_id": "shop:leave", "local_score": 8.0},
            ],
            "shop:potion:Energy Potion:10",
        )

        self.assertEqual(["shop:potion:Energy Potion:10"], selected)
        advice = agent._pending_macro_advice
        self.assertEqual("no_change", advice["status"])
        self.assertEqual(
            ["shop:potion:Energy Potion:10"],
            advice["final_choice_ids"],
        )

    def test_single_choice_adjusted_score_tie_preserves_local_rule(self):
        result = AdvisorResult(
            status="recommended",
            model_choice_id="model-top",
            confidence=0.8,
            rankings=[
                {"candidate_id": "model-top", "score": 100},
                {"candidate_id": "local-rule", "score": 0},
                {"candidate_id": "third", "score": 0},
            ],
        )
        agent = self.agent(result)
        selected = agent._macro_rank(
            "SHOP",
            [
                {"candidate_id": "local-rule", "local_score": 10},
                {"candidate_id": "model-top", "local_score": 2},
                {"candidate_id": "third", "local_score": 1},
            ],
            "local-rule",
        )

        # SHOP bonus_cap=10, so the model top receives exactly +8 here and
        # ties the local rule at 10.  A tie is not evidence for an override.
        self.assertEqual(["local-rule"], selected)
        self.assertEqual("no_change", agent._pending_macro_advice["status"])

    def test_single_choice_model_score_tie_preserves_local_rule(self):
        result = AdvisorResult(
            status="recommended",
            model_choice_id="model-tied-top",
            confidence=0.99,
            rankings=[
                {"candidate_id": "model-tied-top", "score": 100},
                {"candidate_id": "local-rule", "score": 100},
                {"candidate_id": "third", "score": 10},
            ],
        )
        agent = self.agent(result)
        selected = agent._macro_rank(
            "SHOP",
            [
                {"candidate_id": "local-rule", "local_score": 10},
                {"candidate_id": "model-tied-top", "local_score": 9},
                {"candidate_id": "third", "local_score": 0},
            ],
            "local-rule",
        )

        self.assertEqual(["local-rule"], selected)
        self.assertEqual(
            "insufficient_model_margin",
            agent._pending_macro_advice["status"],
        )

    def test_macro_state_deduplicates_copy_uuids_but_preserves_deck_order(self):
        agent = self.agent(AdvisorResult(status="fallback"))

        def strike(uuid, upgrades=0, damage=6):
            return SimpleNamespace(
                uuid=uuid,
                card_id="Strike_B",
                name="Strike",
                type=self.CardType.ATTACK,
                rarity=self.CardRarity.BASIC,
                upgrades=upgrades,
                cost=1,
                damage=damage,
                base_damage=damage,
                block=0,
                base_block=0,
                magic_number=0,
                exhausts=False,
                description=f"Deal {damage} damage.",
            )

        agent.game.deck = [strike("uuid-a"), strike("uuid-b"), strike("uuid-c", 1, 9)]
        state = agent._macro_state()
        self.assertEqual([0, 0, 1], state["deck"])
        self.assertEqual(2, len(state["deck_card_facts"]))
        self.assertNotIn("uuid-a", json.dumps(state))
        self.assertEqual(
            [
                "ascension", "act", "boss", "keys", "deck_card_facts",
                "deck", "deck_plan", "relic_catalog", "model_run_plan",
                "heart_plan", "potions", "relic_runtime",
                "heart_plan_runtime", "run_position", "resource_summary",
                "gold", "hp", "floor",
            ],
            list(state),
        )
        self.assertNotIn("floor_in_act", state["heart_plan"])
        self.assertIn("floor_in_act", state["heart_plan_runtime"])

    def test_relic_counter_and_heart_floor_change_only_runtime_suffixes(self):
        agent = self.agent(AdvisorResult(status="fallback"))
        relic = SimpleNamespace(
            relic_id="Sundial",
            name="Sundial",
            counter=1,
            description="Every third shuffle gains Energy.",
        )
        agent.game.relics = [relic]
        agent.game.floor = 4
        first = agent._macro_state()

        relic.counter = 2
        agent.game.floor = 5
        second = agent._macro_state()

        self.assertEqual(first["relic_catalog"], second["relic_catalog"])
        self.assertNotEqual(first["relic_runtime"], second["relic_runtime"])
        self.assertEqual(first["heart_plan"], second["heart_plan"])
        self.assertNotEqual(
            first["heart_plan_runtime"], second["heart_plan_runtime"]
        )
        keys = list(first)
        self.assertLess(keys.index("deck"), keys.index("relic_runtime"))
        self.assertLess(keys.index("relic_catalog"), keys.index("relic_runtime"))
        self.assertLess(keys.index("heart_plan"), keys.index("heart_plan_runtime"))

    def test_energy_relic_is_reflected_in_readiness_and_resource_summary(self):
        agent = self.agent(AdvisorResult(status="fallback"))
        agent.game.relics = [SimpleNamespace(
            relic_id="Velvet Choker",
            name="Velvet Choker",
            counter=0,
            description="Gain 1 Energy. You cannot play more than 6 cards.",
        )]
        profile = agent._deck_profile()
        state = agent._macro_state()
        self.assertEqual(1.0, profile["roles"]["energy"])
        self.assertEqual(4, state["resource_summary"]["estimated_energy_each_turn"])
        self.assertIn(
            "Velvet Choker", state["resource_summary"]["energy_relic_ids"]
        )

    def test_macro_state_uses_authoritative_two_potion_slot_capacity(self):
        agent = self.agent(AdvisorResult(status="fallback"))
        agent.game.ascension_level = 11
        agent.game.potions = [
            SimpleNamespace(
                potion_id="Potion Slot",
                name="Potion Slot",
                description="",
                can_use=False,
                can_discard=False,
                requires_target=False,
            )
            for _ in range(2)
        ]

        state = agent._macro_state()

        self.assertEqual(2, state["resource_summary"]["potion_slots"])
        self.assertEqual(0, state["resource_summary"]["potions_filled"])

    def test_boss_reward_position_does_not_point_back_to_defeated_boss(self):
        from spirecomm.spire.screen import ScreenType

        agent = self.agent(AdvisorResult(status="fallback"))
        agent.game.act = 1
        agent.game.floor = 17
        agent.game.act_boss = "Hexaghost"
        agent.game.screen_type = ScreenType.BOSS_REWARD

        position = agent._macro_state()["run_position"]

        self.assertTrue(position["act_boss_defeated"])
        self.assertEqual(0, position["floors_to_act_boss"])
        self.assertEqual("BOSS_REWARD", position["screen_type"])

    def test_readiness_discounts_large_basic_heavy_deck_consistency(self):
        agent = self.agent(AdvisorResult(status="fallback"))
        roles = {
            "nonbasic_damage": 3.0,
            "nonbasic_block": 3.0,
            "draw": 2.0,
            "scaling": 1.0,
            "energy": 1.0,
            "aoe": 1.0,
        }
        compact = {
            "roles": roles,
            "size": 18,
            "starters": 2,
            "curses": 0,
            "upgraded": 8,
            "expensive": 3,
            "energy_relic_support": 1.0,
        }
        bloated = {
            **compact,
            "size": 32,
            "starters": 9,
            "curses": 2,
            "upgraded": 2,
            "energy_relic_support": 0.0,
            "expensive": 7,
        }
        good = agent._deck_readiness(compact)
        bad = agent._deck_readiness(bloated)
        self.assertGreater(good["consistency"], bad["consistency"])
        self.assertGreater(good["score"], bad["score"])

    def test_card_candidate_facts_flag_unsupported_archetype_payoff(self):
        agent = self.agent(AdvisorResult(status="fallback"))
        agent.chosen_class = self.PlayerClass.IRONCLAD
        agent.game.character = self.PlayerClass.IRONCLAD

        def card(card_id, name, card_type, damage=0, magic=0):
            return SimpleNamespace(
                uuid=f"uuid-{card_id}",
                card_id=card_id,
                name=name,
                type=card_type,
                rarity=self.CardRarity.UNCOMMON,
                upgrades=0,
                cost=1,
                damage=damage,
                base_damage=damage,
                block=0,
                base_block=0,
                magic_number=magic,
                misc=0,
                exhausts=False,
                ethereal=False,
                has_target=card_type == self.CardType.ATTACK,
                description="",
            )

        heavy_blade = card(
            "Heavy Blade", "Heavy Blade", self.CardType.ATTACK, damage=14
        )
        unsupported = agent._macro_card_candidate_facts(heavy_blade)
        self.assertIn(
            "strength", unsupported["deck_context"]["unsupported_payoffs"]
        )

        agent.game.deck = [
            card("Inflame", "Inflame", self.CardType.POWER, magic=2)
        ]
        supported = agent._macro_card_candidate_facts(heavy_blade)
        self.assertNotIn(
            "strength", supported["deck_context"]["unsupported_payoffs"]
        )

    def test_macro_card_facts_resolve_noncombat_sentinels_and_placeholders(self):
        agent = self.agent(AdvisorResult(status="fallback"))
        card = SimpleNamespace(
            uuid="uuid-defend",
            card_id="Defend_B",
            name="Defend",
            type=self.CardType.SKILL,
            rarity=self.CardRarity.BASIC,
            upgrades=0,
            cost=1,
            damage=-1,
            base_damage=-1,
            block=-1,
            base_block=5,
            magic_number=-1,
            misc=0,
            exhausts=False,
            ethereal=True,
            has_target=False,
            description="Gain !B! Block.",
        )
        facts = agent._macro_card_facts(card)
        self.assertIsNone(facts["damage"])
        self.assertEqual(5, facts["block"])
        self.assertEqual(5, facts["base_block"])
        self.assertIsNone(facts["magic_number"])
        self.assertEqual("ENERGY", facts["cost_kind"])
        self.assertTrue(facts["ethereal"])
        self.assertEqual("Gain 5 Block.", facts["description"])

        card.cost = -1
        card.card_id = "Whirlwind"
        self.assertEqual("X", agent._macro_card_facts(card)["cost_kind"])

    def test_card_parser_preserves_ethereal_and_misc(self):
        from spirecomm.spire.card import Card

        card = Card.from_json({
            "id": "Genetic Algorithm",
            "name": "Genetic Algorithm",
            "type": "SKILL",
            "rarity": "UNCOMMON",
            "upgrades": 0,
            "has_target": False,
            "cost": 1,
            "uuid": "ga",
            "misc": 17,
            "ethereal": True,
            "description": "Gain !B! Block.",
        })
        self.assertEqual(17, card.misc)
        self.assertTrue(card.ethereal)

    def test_candidate_compaction_keeps_exact_binding_and_strategic_facts(self):
        candidate = {
            "candidate_id": "card:uuid-a",
            "label": "Strike",
            "local_score": 12.5,
            "facts": {
                "instance_id": "uuid-a",
                "name": "Strike",
                "card_id": "Strike_B",
                "damage": 6,
                "cost": 1,
                "tags": ["damage"],
            },
        }
        compact = self.SimpleAgent._compact_macro_candidate(candidate)
        self.assertEqual("card:uuid-a", compact["candidate_id"])
        self.assertEqual(12.5, compact["local_score"])
        self.assertNotIn("instance_id", compact["facts"])
        self.assertNotIn("name", compact["facts"])
        self.assertEqual("Strike_B", compact["facts"]["card_id"])
        self.assertEqual(6, compact["facts"]["damage"])
        self.assertEqual(1, compact["facts"]["cost"])

        near_match = self.SimpleAgent._compact_macro_candidate({
            "candidate_id": "card:uuid-a-extra",
            "facts": {"instance_id": "uuid-a", "damage": 6},
        })
        self.assertEqual("uuid-a", near_match["facts"]["instance_id"])


if __name__ == "__main__":
    unittest.main()
