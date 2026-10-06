"""Provider-only doubles check retry accounting and actual SQLite results."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "tools/verify_memory_storage.py"


def raw(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "modelVersion": "gemini-3.8-flash", "usageMetadata": {
                "promptTokenCount": 10, "candidatesTokenCount": 5,
                "thoughtsTokenCount": 0, "totalTokenCount": 15}}


class StorageRunnerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), "Fixed storage regression runner is missing")
        spec = importlib.util.spec_from_file_location("storage_runner", MODULE)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)
        self.h = self.runner.h
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def client(self, replies, max_calls=80):
        queue = iter(replies)
        def send(model, body):
            value = next(queue)
            if isinstance(value, Exception):
                raise value
            return raw(value)
        return self.runner.RetryRecorder(send, mode="synthetic_test", model_name=self.h.COMPARISON_MODEL,
                                         max_calls=max_calls, budget_usd=3, count_input=lambda m, b: 10)

    def test_recovered_extraction_keeps_valid_result_and_actual_response_request(self):
        client = self.client([self.h.ProviderError("HTTP_503"), "[]", "안녕하세요."])
        app = self.path / "app.py"
        app.write_text(self.h.normalize_extractor_model((ROOT / "app.py").read_text()))
        with patch.object(self.runner.time, "sleep"):
            result = self.h.run_case(app, self.path / "trial.db", client,
                                    {"id": "F1", "input": self.h.INSTRUCTION + " 이런 취향을 좋아합니다."},
                                    1, comparison=True)
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["extraction_parse_valid"])
        self.assertEqual(result["database_rows"], [])
        self.assertEqual(result["response"]["text"], "안녕하세요.")
        self.assertEqual(result["verdict"]["response_instruction"], "NOT_OBSERVED")
        self.assertEqual(len(client.calls), 3)
        self.assertAlmostEqual(client.uncertain_usd, 0.30)

    def test_retry_exhaustion_counts_four_attempts_and_stops(self):
        client = self.client([self.h.ProviderError("HTTP_503")] * 8)
        with patch.object(self.runner.time, "sleep"), self.assertRaisesRegex(self.h.ProviderError, "HTTP_503"):
            client.GenerativeModel(self.h.COMPARISON_MODEL).generate_content("test")
        self.assertEqual(len(client.calls), 4)
        self.assertTrue(client.stopped)

    def test_authorization_failure_is_not_retried(self):
        client = self.client([self.h.ProviderError("HTTP_403"), "must not use"])
        with self.assertRaisesRegex(self.h.ProviderError, "HTTP_403"):
            client.GenerativeModel(self.h.COMPARISON_MODEL).generate_content("test")
        self.assertEqual(len(client.calls), 1)

    def test_retry_cannot_exceed_generation_guard(self):
        client = self.client([self.h.ProviderError("HTTP_503")] * 8, max_calls=2)
        with patch.object(self.runner.time, "sleep"), self.assertRaises(self.h.ProviderError):
            client.GenerativeModel(self.h.COMPARISON_MODEL).generate_content("test")
        self.assertEqual(len(client.calls), 2)

    def test_fixed_schedule_has_literal_51_generation_budget(self):
        schedule = self.runner.schedule()
        self.assertEqual(len(schedule), 26)
        self.assertEqual(sum(1 if case == "C0" else 2 for case, phase, repeat in schedule), 51)

    def test_ledger_includes_failed_attempt_and_cumulative_count(self):
        client = self.client([self.h.ProviderError("HTTP_503"), "안녕하세요."])
        client.output_dir = self.path
        with patch.object(self.runner.time, "sleep"):
            client.GenerativeModel(self.h.COMPARISON_MODEL).generate_content("test")
        ledger = json.loads((self.path / "ledger.json").read_text())
        self.assertEqual(ledger["generation_attempts"], 2)
        self.assertEqual(ledger["cumulative_generation_calls"], 83)
        self.assertEqual(ledger["retry_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
