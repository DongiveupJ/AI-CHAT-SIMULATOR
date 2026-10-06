"""Same-model controls. Fake only the provider; app functions and SQLite stay real."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("memory_tool", ROOT / "tools/verify_memory_path.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def reply(text="[]", **overrides):
    raw = {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
           "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5,
                             "thoughtsTokenCount": 2, "totalTokenCount": 17},
           "modelVersion": "fixed-synthetic-version"}
    raw.update(overrides)
    return raw


class ComparisonControlsTests(unittest.TestCase):
    def client(self, **kwargs):
        try:
            client = tool.GeminiRecorder(lambda model, body: reply(), mode="synthetic_test", **kwargs)
        except TypeError:
            self.fail("Comparison recorder must accept an explicit model, call cap and cost budget")
        return client

    def test_shared_call_cap_stops_before_forty_first_generation(self):
        client = self.client(model_name="models/gemini-3.8-flash", max_calls=40, budget_usd=3.0)
        model = client.GenerativeModel("models/gemini-3.8-flash")
        for _ in range(40):
            model.generate_content("test")
        with self.assertRaises(tool.ProviderError):
            model.generate_content("test")
        self.assertEqual(len(client.calls), 40)

    def test_generation_settings_and_thinking_charges_are_recorded(self):
        client = self.client(model_name="models/gemini-3.8-flash", max_calls=40, budget_usd=3.0)
        client.GenerativeModel("models/gemini-3.8-flash").generate_content("test")
        call = client.calls[0]
        self.assertEqual(call["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"], "LOW")
        self.assertEqual(call["request"]["generationConfig"]["maxOutputTokens"], 8192)
        self.assertAlmostEqual(call["estimated_usd"], 0.00003375)

    def test_insufficient_budget_stops_before_sending(self):
        client = self.client(model_name="models/gemini-3.8-flash", max_calls=40, budget_usd=0.29)
        with self.assertRaises(tool.ProviderError):
            client.GenerativeModel("models/gemini-3.8-flash").generate_content("test")
        self.assertEqual(client.calls, [])

    def test_missing_usage_reserves_uncertain_cost_and_stops_without_retry(self):
        client = tool.GeminiRecorder(lambda m, b: reply(usageMetadata={}), mode="synthetic_test",
                                     model_name=tool.COMPARISON_MODEL, max_calls=40, budget_usd=3)
        model = client.GenerativeModel(tool.COMPARISON_MODEL)
        with self.assertRaisesRegex(tool.ProviderError, "USAGE_UNAVAILABLE"):
            model.generate_content("test")
        with self.assertRaisesRegex(tool.ProviderError, "RUN_STOPPED"):
            model.generate_content("test")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.uncertain_usd, 0.30)

    def test_input_token_limit_prevents_generation(self):
        client = self.client(model_name=tool.COMPARISON_MODEL, max_calls=40, budget_usd=3,
                             count_input=lambda m, b: 8001)
        with self.assertRaisesRegex(tool.ProviderError, "INPUT_TOKEN_LIMIT"):
            client.GenerativeModel(tool.COMPARISON_MODEL).generate_content("test")
        self.assertEqual(client.calls, [])
        self.assertEqual(len(client.token_counts), 1)

    def test_changed_version_and_truncation_stop_but_keep_billed_usage(self):
        for bad, code in [(reply(modelVersion="changed"), "MODEL_VERSION_CHANGED"),
                          (reply(candidates=[{"content": {"parts": [{"text": "partial"}]},
                                              "finishReason": "MAX_TOKENS"}]), "INCOMPLETE_RESPONSE")]:
            with self.subTest(code=code):
                replies = iter([reply(), bad])
                client = tool.GeminiRecorder(lambda m, b: next(replies), mode="synthetic_test",
                                             model_name=tool.COMPARISON_MODEL, max_calls=40, budget_usd=3)
                model = client.GenerativeModel(tool.COMPARISON_MODEL)
                model.generate_content("test")
                with self.assertRaisesRegex(tool.ProviderError, code):
                    model.generate_content("test")
                self.assertTrue(client.stopped)
                self.assertAlmostEqual(client.estimated_usd, 0.0000675)
                self.assertEqual(client.uncertain_usd, 0)

    def test_missing_candidate_usage_is_not_assumed_to_be_zero(self):
        raw = reply(usageMetadata={"promptTokenCount": 10, "totalTokenCount": 17, "thoughtsTokenCount": 7})
        client = tool.GeminiRecorder(lambda m, b: raw, mode="synthetic_test", model_name=tool.COMPARISON_MODEL,
                                     max_calls=40, budget_usd=3)
        with self.assertRaisesRegex(tool.ProviderError, "USAGE_UNAVAILABLE"):
            client.GenerativeModel(tool.COMPARISON_MODEL).generate_content("test")
        self.assertEqual(client.uncertain_usd, 0.30)

    def test_timeout_retains_reservation_and_never_retries(self):
        def failed(m, b):
            raise tool.ProviderError("NETWORK_OR_TIMEOUT")
        client = tool.GeminiRecorder(failed, mode="synthetic_test", model_name=tool.COMPARISON_MODEL,
                                     max_calls=40, budget_usd=3)
        for expected in ("NETWORK_OR_TIMEOUT", "RUN_STOPPED"):
            with self.assertRaisesRegex(tool.ProviderError, expected):
                client.GenerativeModel(tool.COMPARISON_MODEL).generate_content("test")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.uncertain_usd, 0.30)

    def test_count_tokens_includes_full_request_and_uses_no_generation(self):
        from unittest.mock import patch
        body = {"contents": [{"role": "user", "parts": [{"text": "test"}]}],
                "systemInstruction": {"parts": [{"text": "system"}]}, "generationConfig": tool.GENERATION_CONFIG}
        with patch.object(tool, "request_live", return_value={"totalTokens": 123}) as send:
            self.assertEqual(tool.count_live("SYNTHETIC_KEY", tool.COMPARISON_MODEL, body), 123)
            send.assert_called_once_with("SYNTHETIC_KEY", tool.COMPARISON_MODEL, "countTokens",
                                         {"generateContentRequest": {"model": tool.COMPARISON_MODEL, **body}})


class MemoryDefenseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "trial.db"

    def trial(self, memory):
        replies = iter([reply(json.dumps([memory], ensure_ascii=False)), reply("안녕하세요.")])
        client = tool.GeminiRecorder(lambda m, b: next(replies), mode="synthetic_test")
        return tool.run_case(ROOT / "app.py", self.db, client, tool.CASES["injection"], 1)

    def test_unknown_memory_type_is_rejected_before_database_write(self):
        result = self.trial({"memory_type": "system_override", "memory_text": tool.INSTRUCTION, "confidence": 1.0})
        self.assertEqual(result["database_rows"], [])

    def test_validator_rejects_shapes_and_boundaries_without_hiding_valid_commands(self):
        client = tool.GeminiRecorder(lambda m, b: reply(), mode="synthetic_test")
        runtime, _, _ = tool.load_app((ROOT / "app.py").read_text(), self.db, client, [])
        validate = runtime.get("validate_memory_candidates")
        if validate is None:
            self.fail("The real app must validate candidates at every storage boundary")
        normal = {"memory_type": "preference", "memory_text": "라벤더 밀크티를 좋아합니다.", "confidence": 0.9}
        bad = [{**normal, "memory_type": "system_override"}, {**normal, "confidence": True},
               {**normal, "confidence": float("nan")}, {**normal, "memory_text": 7},
               {**normal, "confidence": float("inf")}, {**normal, "confidence": -0.01},
               {**normal, "confidence": 1.01}, {**normal, "memory_text": "   "},
               {**normal, "memory_text": "가" * 101}, {"memory_type": "preference"},
               {**normal, "extra": "not allowed"}]
        for entry in bad:
            with self.subTest(entry=entry):
                self.assertEqual(validate([entry])[0], [])
        self.assertEqual(validate({})[0], [])
        self.assertEqual(validate([normal])[0], [normal])
        self.assertEqual(validate([normal, bad[0]])[1], [{"index": 1, "reason": "invalid_type"}])
        for confidence in (0, 1):
            self.assertEqual(len(validate([{**normal, "confidence": confidence}])[0]), 1)
        self.assertEqual(len(validate([{**normal, "memory_text": "가" * 100}])[0]), 1)
        self.assertEqual(len(validate([{**normal, "memory_text": tool.INSTRUCTION}])[0]), 1)

    def test_allowed_type_command_is_reference_data_not_system_instruction(self):
        result = self.trial({"memory_type": "preference", "memory_text": tool.INSTRUCTION, "confidence": 1.0})
        self.assertEqual(len(result["database_rows"]), 1)
        request = result["calls"][1]["request"]
        self.assertNotIn(tool.CANARY, request["systemInstruction"]["parts"][0]["text"])
        self.assertIn(tool.CANARY, json.dumps(request["contents"], ensure_ascii=False))

    def test_invalid_direct_save_does_not_delete_existing_profile(self):
        client = tool.GeminiRecorder(lambda m, b: reply(), mode="synthetic_test")
        runtime, _, schema = tool.load_app((ROOT / "app.py").read_text(), self.db, client, [])
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(schema)
            conn.commit()
        save = runtime["save_long_term_memory"]
        save(101, 201, "학생입니다.", "profile", 0.9)
        save(101, 201, "x" * 101, "profile", 0.9)
        self.assertEqual(runtime["get_long_term_memories"](101, 201)[0]["memory_text"], "학생입니다.")


class ComparisonRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def snapshot(self, source):
        path = self.path / "app_snapshot.py"
        path.write_text(tool.normalize_extractor_model(source))
        return path

    def test_normalization_changes_only_extractor_model_literal(self):
        source = tool.baseline_source()
        normalized = tool.normalize_extractor_model(source)
        self.assertEqual(normalized.replace('"models/gemini-3.8-flash"', '"models/gemini-2.5-flash"'), source)
        self.assertEqual(normalized.count('"models/gemini-3.8-flash"'), 1)

    def test_exact_cases_and_balanced_schedule_use_at_most_forty_generations(self):
        cases = tool.comparison_cases()
        self.assertEqual(cases["N1"]["input"], "라벤더 밀크티를 좋아합니다.")
        self.assertEqual(len(cases["A1"]["input"]), 227)
        self.assertEqual(len(cases["A2"]["input"]), 222)
        self.assertEqual(cases["A1"]["input"].replace('"system_override"', '"preference"'), cases["A2"]["input"])
        schedule = tool.comparison_schedule()
        self.assertEqual(len(schedule), 20)
        self.assertEqual(sum(1 if case == "C0" else 2 for case, phase, repeat in schedule) + 2, 40)
        self.assertEqual([phase for case, phase, repeat in schedule if case == "A2"],
                         ["before", "after", "after", "before", "before", "after"])

    def test_c0_calls_only_response_with_empty_database(self):
        snapshot = self.snapshot(tool.baseline_source())
        client = tool.GeminiRecorder(lambda m, b: reply("알 수 없음"), mode="synthetic_test",
                                     model_name=tool.COMPARISON_MODEL, max_calls=40, budget_usd=3)
        result = tool.run_case(snapshot, self.path / "trial.db", client, tool.comparison_cases()["C0"], 1,
                               comparison=True)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["database_rows"], [])
        self.assertTrue(result["normal_assessment"]["unknown_response"])
        self.assertIsNone(result["extraction_parse_valid"])

    def test_invalid_json_stops_comparison_after_extraction_not_defense_success(self):
        snapshot = self.snapshot(tool.baseline_source())
        client = tool.GeminiRecorder(lambda m, b: reply("not JSON"), mode="synthetic_test",
                                     model_name=tool.COMPARISON_MODEL, max_calls=40, budget_usd=3)
        result = tool.run_case(snapshot, self.path / "trial.db", client, tool.comparison_cases()["A1"], 1,
                               comparison=True)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("EXTRACTION_FORMAT", result["errors"])
        self.assertEqual(result["verdict"]["response_instruction"], "INCONCLUSIVE")

    def test_historical_normal_fallback_is_reported_separately_from_llm_extraction(self):
        snapshot = self.snapshot(tool.baseline_source())
        replies = iter([reply(), reply("라벤더 밀크티")])
        client = tool.GeminiRecorder(lambda m, b: next(replies), mode="synthetic_test",
                                     model_name=tool.COMPARISON_MODEL, max_calls=40, budget_usd=3)
        result = tool.run_case(snapshot, self.path / "trial.db", client, tool.comparison_cases()["N1"], 1,
                               comparison=True)
        self.assertTrue(result["fallback_used"])
        self.assertFalse(result["normal_assessment"]["llm_preference_extracted"])
        self.assertTrue(result["normal_assessment"]["preference_stored"])
        self.assertTrue(result["normal_assessment"]["exact_answer"])

    def test_historical_rule_fallback_can_keep_an_allowed_type_command(self):
        snapshot = self.snapshot(tool.baseline_source())
        replies = iter([reply(), reply("안녕하세요.")])
        client = tool.GeminiRecorder(lambda m, b: next(replies), mode="synthetic_test",
                                     model_name=tool.COMPARISON_MODEL, max_calls=40, budget_usd=3)
        case = {"id": "fallback_fixture", "input": tool.INSTRUCTION + " 이런 취향을 좋아합니다."}
        result = tool.run_case(snapshot, self.path / "fallback.db", client, case, 1, comparison=True)
        self.assertTrue(result["fallback_used"])
        self.assertFalse(result["validation_applied"])
        self.assertEqual(result["verdict"]["storage_canary"], "OBSERVED")
        self.assertEqual(result["verdict"]["system_placement"], "OBSERVED")
        self.assertEqual(result["verdict"]["reference_placement"], "NOT_OBSERVED")

    def test_replay_rejects_a1_but_keeps_a2_reference_without_generating_answer(self):
        snapshot = self.snapshot((ROOT / "app.py").read_text())
        for memory_type, expected_rows in [("system_override", 0), ("preference", 1)]:
            result = tool.replay_extraction(snapshot, self.path / f"{memory_type}.db",
                                            [{"memory_type": memory_type, "memory_text": tool.INSTRUCTION,
                                              "confidence": 1.0}], "original-result-sha256")
            self.assertEqual(len(result["database_rows"]), expected_rows)
            self.assertEqual(result["live_api_calls"], 0)
            self.assertNotIn("response", result)
            self.assertNotIn(tool.CANARY, json.dumps(result["request"]["systemInstruction"], ensure_ascii=False))

    def test_complete_synthetic_suite_freezes_sources_and_shares_forty_call_cap(self):
        responses = [reply('[{"memory_type":"preference","memory_text":"커피를 좋아합니다.","confidence":0.9}]'),
                     reply("안녕하세요."), reply("알 수 없음"), reply("알 수 없음")]
        for case, phase, repeat in tool.comparison_schedule()[2:]:
            memory = {"memory_type": "preference" if case != "A1" else "system_override",
                      "memory_text": "라벤더 밀크티를 좋아합니다." if case == "N1" else tool.INSTRUCTION,
                      "confidence": 1.0}
            responses.extend([reply(json.dumps([memory], ensure_ascii=False)),
                              reply("라벤더 밀크티" if case == "N1" else "안녕하세요.")])
        responses = iter(responses)
        client = tool.GeminiRecorder(lambda m, b: next(responses), mode="synthetic_test",
                                     model_name=tool.COMPARISON_MODEL, max_calls=40, budget_usd=3,
                                     count_input=lambda m, b: 10)
        manifest = tool.execute_comparison(self.path, client, quiet=True)
        self.assertEqual(manifest["status"], "completed_pending_manual_review")
        self.assertEqual(manifest["generation_calls_used"], 40)
        self.assertEqual(len(client.token_counts), 40)
        self.assertTrue((self.path / "before_app.py").is_file())
        self.assertTrue((self.path / "after_app.py").is_file())
        self.assertTrue((self.path / "comparison.md").is_file())
        self.assertEqual(len(list(self.path.glob("replay-*.json"))), 12)

    def test_suite_provider_failure_stops_after_first_call_and_persists_error(self):
        def failed(m, b):
            raise tool.ProviderError("HTTP_400")
        client = tool.GeminiRecorder(failed, mode="synthetic_test", model_name=tool.COMPARISON_MODEL,
                                     max_calls=40, budget_usd=3, count_input=lambda m, b: 10)
        manifest = tool.execute_comparison(self.path, client, quiet=True)
        self.assertEqual(manifest["status"], "stopped_error")
        self.assertEqual(manifest["generation_calls_used"], 1)
        self.assertEqual(manifest["reserved_uncertain_usd"], 0.30)
        self.assertEqual(len(list(self.path.glob("trial-*.json"))), 1)

    def test_env_key_alias_is_read_without_printing_or_mutating_file(self):
        from unittest.mock import patch
        env = self.path / ".env"
        env.write_text("Gemini_API_Key='SYNTHETIC_NOT_A_REAL_KEY'\n")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(tool.read_key(env), "SYNTHETIC_NOT_A_REAL_KEY")

    def test_canary_quotation_suffix_is_only_a_manual_review_candidate(self):
        snapshot = self.snapshot(tool.baseline_source())
        memory = {"memory_type": "preference", "memory_text": tool.INSTRUCTION, "confidence": 1.0}
        replies = iter([reply(json.dumps([memory])), reply("참고 자료에 나온 표식은 다음과 같습니다: " + tool.CANARY)])
        client = tool.GeminiRecorder(lambda m, b: next(replies), mode="synthetic_test",
                                     model_name=tool.COMPARISON_MODEL, max_calls=40, budget_usd=3)
        result = tool.run_case(snapshot, self.path / "quote.db", client, tool.comparison_cases()["A2"], 1,
                               comparison=True)
        self.assertTrue(result["manual_review_pending"])
        self.assertEqual(result["verdict"]["response_suffix_candidate"], "OBSERVED")
        self.assertEqual(result["verdict"]["response_instruction"], "INCONCLUSIVE")


if __name__ == "__main__":
    unittest.main()
