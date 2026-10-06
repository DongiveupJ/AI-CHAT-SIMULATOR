"""Offline checks run the real extraction and UI storage path against SQLite."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("storage_harness", ROOT / "tools/verify_memory_path.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
ATTACK = h.INSTRUCTION + " 이런 취향을 좋아합니다."


class MemoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "storage.db"
        self.warnings, self.errors = [], []

    def pipeline(self, output, message=ATTACK, seed=None):
        def generate(_):
            if isinstance(output, Exception):
                raise output
            return SimpleNamespace(text=output)
        client = SimpleNamespace(GenerativeModel=lambda _: SimpleNamespace(generate_content=generate))
        runtime, pipeline, schema = h.load_app((ROOT / "app.py").read_text(), self.db, client, self.errors)
        runtime["st"].warning = self.warnings.append
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(schema)
            conn.commit()
        if seed:
            runtime["save_long_term_memory"](101, 201, seed["memory_text"], seed["memory_type"], seed["confidence"])
        extracted = runtime["extract_memory_with_llm"](message)
        runtime.update(p=message, memory_candidates=extracted)
        exec(pipeline, runtime)
        return extracted, runtime["get_long_term_memories"](101, 201), runtime

    # Reintroducing whole-utterance fallback must store a canary and fail this test.
    def test_valid_empty_result_does_not_store_attack_utterance(self):
        extracted, memories, _ = self.pipeline("[]")
        self.assertEqual(extracted, [])
        self.assertEqual(memories, [])
        self.assertEqual(self.warnings, [])

    def test_empty_normal_result_is_not_silently_promoted_to_memory(self):
        _, memories, _ = self.pipeline("[]", "라벤더 밀크티를 좋아합니다.")
        self.assertEqual(memories, [])

    # Collapsing extraction failure to [] hides an error and must fail these tests.
    def test_invalid_json_has_failure_status_and_no_storage(self):
        extracted, memories, _ = self.pipeline("not JSON")
        self.assertIsNone(extracted)
        self.assertEqual(memories, [])
        self.assertEqual(len(self.warnings), 1)

    def test_non_list_json_has_failure_status_and_no_storage(self):
        extracted, memories, _ = self.pipeline('{"memory_type":"preference"}')
        self.assertIsNone(extracted)
        self.assertEqual(memories, [])
        self.assertEqual(len(self.warnings), 1)

    def test_provider_error_has_failure_status_without_secret_in_warning(self):
        extracted, memories, _ = self.pipeline(RuntimeError("SYNTHETIC_SECRET_NEVER_LOG"))
        self.assertIsNone(extracted)
        self.assertEqual(memories, [])
        self.assertEqual(len(self.warnings), 1)
        self.assertNotIn("SYNTHETIC_SECRET_NEVER_LOG", self.warnings[0])

    # Rejecting every candidate would pass attack checks but break normal storage.
    def test_normal_types_survive_real_storage_and_retrieval(self):
        fixtures = [("preference", "라벤더 밀크티를 좋아합니다."),
                    ("profile", "이름은 하늘입니다."),
                    ("goal", "목표는 주 3회 달리기입니다."),
                    ("style", "존댓말을 선호합니다.")]
        memories = [{"memory_type": t, "memory_text": text, "confidence": 0.9} for t, text in fixtures]
        _, rows, _ = self.pipeline(json.dumps(memories, ensure_ascii=False))
        self.assertEqual({(r["memory_type"], r["memory_text"]) for r in rows}, set(fixtures))

    def test_code_fenced_valid_result_is_preserved(self):
        memory = {"memory_type": "preference", "memory_text": "커피를 좋아합니다.", "confidence": 0.9}
        _, rows, _ = self.pipeline("```json\n" + json.dumps([memory], ensure_ascii=False) + "\n```")
        self.assertEqual(rows[0]["memory_text"], "커피를 좋아합니다.")

    def test_failed_extraction_keeps_existing_profile(self):
        seed = {"memory_type": "profile", "memory_text": "학생입니다.", "confidence": 0.9}
        _, rows, _ = self.pipeline("not JSON", "나는 변경된 프로필입니다.", seed)
        self.assertEqual([(r["memory_type"], r["memory_text"]) for r in rows], [("profile", "학생입니다.")])

    def test_invalid_candidate_does_not_hide_other_valid_candidates(self):
        valid = {"memory_type": "preference", "memory_text": "차를 좋아합니다.", "confidence": 0.9}
        invalid = {**valid, "memory_type": "system_override"}
        _, rows, runtime = self.pipeline(json.dumps([invalid, valid], ensure_ascii=False))
        self.assertEqual([r["memory_text"] for r in rows], ["차를 좋아합니다."])
        self.assertEqual(runtime["memory_rejections"], [{"index": 0, "reason": "invalid_type"}])

    # This is a limitation check, not an attack-blocking assertion.
    def test_structurally_valid_command_remains_a_documented_risk(self):
        memory = {"memory_type": "preference", "memory_text": h.INSTRUCTION, "confidence": 0.9}
        _, rows, _ = self.pipeline(json.dumps([memory], ensure_ascii=False))
        self.assertEqual(rows[0]["memory_text"], h.INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
