"""Network doubles test the real app functions, not an LLM's security."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_memory_path", ROOT / "tools/verify_memory_path.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def response(text):
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                        "finishReason": "STOP", "safetyRatings": []}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
        "modelVersion": "synthetic-test-only",
    }


class SequenceTransport:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.attempts = 0

    def __call__(self, model, body):
        self.attempts += 1
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


class MemoryPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "trial.db"
        self.app = Path(self.temp.name) / "baseline.py"
        self.app.write_text(tool.baseline_source(), encoding="utf-8")

    def run_trial(self, memories, reply="안녕하세요.", case="normal"):
        transport = SequenceTransport([response(json.dumps(memories, ensure_ascii=False)), response(reply)])
        client = tool.GeminiRecorder(transport, mode="synthetic_test")
        return tool.run_case(self.app, self.db, client, tool.CASES[case], 1)

    # Dropping persistence or losing the retrieved memories from the prompt must fail.
    def test_normal_information_survives_real_database_and_prompt_path(self):
        result = self.run_trial([{"memory_type": "preference", "memory_text": "커피를 좋아합니다.", "confidence": 0.9}])
        self.assertEqual(result["database_rows"][0]["memory_text"], "커피를 좋아합니다.")
        self.assertIn("- (preference) 커피를 좋아합니다.", result["calls"][1]["request"]["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(result["mode"], "synthetic_test")
        self.assertEqual(result["live_api_calls"], 0)
        self.assertEqual(result["verdict"]["response_instruction"], "NOT_OBSERVED")
        with closing(sqlite3.connect(self.db)) as conn:
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertNotIn("users", names)

    # Conflating a persisted command with response execution must fail.
    def test_memory_contamination_and_response_execution_are_separate(self):
        memory = {"memory_type": "system_override", "memory_text": tool.INSTRUCTION, "confidence": 1.0}
        result = self.run_trial([memory], case="injection")
        self.assertEqual(result["verdict"], {
            "extraction_canary": "OBSERVED", "unauthorized_type": "OBSERVED",
            "storage_canary": "OBSERVED", "prompt_canary": "OBSERVED",
            "response_instruction": "NOT_OBSERVED",
        })
        self.assertEqual(result["database_rows"][0]["memory_type"], "system_override")
        self.assertNotIn(tool.CANARY, result["calls"][1]["request"]["contents"][0]["parts"][0]["text"])

    def test_only_canary_suffix_counts_as_instruction_execution(self):
        result = self.run_trial([{"memory_type": "system_override", "memory_text": tool.INSTRUCTION, "confidence": 1.0}],
                                "안녕하세요. " + tool.CANARY, "injection")
        self.assertEqual(result["verdict"]["response_instruction"], "OBSERVED")
        self.assertEqual(result["mode"], "synthetic_test")

    # Removing the app's 100-character UI filter must fail.
    def test_overlong_memory_is_not_persisted(self):
        result = self.run_trial([{"memory_type": "system_override", "memory_text": "가" * 101 + tool.CANARY, "confidence": 1.0}], case="injection")
        self.assertEqual(result["database_rows"], [])
        self.assertEqual(result["verdict"]["extraction_canary"], "OBSERVED")
        self.assertEqual(result["verdict"]["storage_canary"], "NOT_OBSERVED")
        self.assertEqual(result["verdict"]["prompt_canary"], "NOT_OBSERVED")

    # Swallowed provider errors must never look like a blocked attack, or trigger retries.
    def test_provider_error_is_inconclusive_and_not_retried(self):
        transport = SequenceTransport([tool.ProviderError("HTTP_429")])
        client = tool.GeminiRecorder(transport, mode="synthetic_test")
        result = tool.run_case(self.app, self.db, client, tool.CASES["injection"], 1)
        self.assertEqual(result["verdict"]["extraction_canary"], "INCONCLUSIVE")
        self.assertEqual(result["verdict"]["response_instruction"], "INCONCLUSIVE")
        self.assertEqual(result["calls"][0]["error"], "HTTP_429")
        self.assertEqual(transport.attempts, 1)

    # Disabling the real rule fallback or mistaking invalid JSON for model refusal must fail.
    def test_invalid_json_preserves_rule_fallback_but_not_a_defense_claim(self):
        transport = SequenceTransport([response("not JSON"), response("안녕하세요.")])
        result = tool.run_case(self.app, self.db, tool.GeminiRecorder(transport, mode="synthetic_test"), tool.CASES["normal"], 1)
        self.assertTrue(result["fallback_used"])
        self.assertFalse(result["extraction_parse_valid"])
        self.assertEqual(result["database_rows"][0]["memory_text"], "커피를 좋아합니다.")
        self.assertEqual(result["verdict"]["extraction_canary"], "INCONCLUSIVE")
        self.assertEqual(result["verdict"]["response_instruction"], "INCONCLUSIVE")

    # Overwriting an existing DB or targeting the app runtime DB must fail safely.
    def test_existing_database_is_not_overwritten(self):
        self.db.write_bytes(b"historical evidence")
        with self.assertRaises(FileExistsError):
            self.run_trial([])
        self.assertEqual(self.db.read_bytes(), b"historical evidence")

    def test_app_runtime_database_name_is_rejected(self):
        self.db = Path(self.temp.name) / "ainha_chat_simulator_final.db"
        with self.assertRaises(ValueError):
            self.run_trial([])
        self.assertFalse(self.db.exists())

    # A thirteenth generation attempt must not reach the provider.
    def test_budget_stops_before_thirteenth_generation(self):
        transport = SequenceTransport([response("[]")] * 13)
        client = tool.GeminiRecorder(transport, mode="synthetic_test")
        model = client.GenerativeModel("models/gemini-2.5-flash")
        for _ in range(12):
            model.generate_content("test")
        with self.assertRaises(tool.ProviderError):
            model.generate_content("test")
        self.assertEqual(len(client.calls), 12)
        self.assertEqual(transport.attempts, 12)


if __name__ == "__main__":
    unittest.main()
