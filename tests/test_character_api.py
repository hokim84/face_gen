"""캐릭터 생성 API: CSV/이미지 큐를 변경하지 않는 계약 검증."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient
import server
import character_generator as CG


class CharacterAPITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "characters.csv"
        self.path.write_text("id,name,custom\nnpc_0001,기존,유지\n", encoding="utf-8-sig")
        self.original = self.path.read_bytes()
        self.path_patch = patch.object(server.Paths, "characters", self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def test_generate_then_explicit_save(self):
        row = dict(name="새인물", subject="1girl", age="", hair="black hair", eyes="blue eyes",
                   outfit="dress", extra="", personality="명랑", expressions="", seed="")
        before = server.QUEUE.summary()
        with patch.object(CG, "generate_characters", return_value=[row]) as generate:
            response = self.client.post("/api/characters/generate", json={"description": " 주민 ", "count": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"rows": [row]})
        self.assertEqual(generate.call_args.args[:2], ("주민", 1))
        self.assertIn("명랑", generate.call_args.args[2])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.path.with_suffix(".csv.bak").exists())
        self.assertEqual(server.QUEUE.summary(), before)
        current = self.client.get("/api/characters")
        rows = current.json() + [{**row, "id": "npc_0002", "custom": ""}]
        saved = self.client.put("/api/characters", json=rows,
                                headers={"X-Base-Version": current.headers["X-Version"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.client.get("/api/characters").json()[0]["custom"], "유지")
        self.assertNotEqual(self.path.read_bytes(), self.original)

    def test_bad_input_never_calls_cli(self):
        with patch.object(CG, "generate_characters") as generate:
            for payload in ({"description": " "}, {"description": "a" * 10001},
                            {"description": 1}, {"description": "주민", "count": 0},
                            {"description": "주민", "count": 51}, {"description": "주민", "count": 1.5},
                            {"description": "주민", "count": True}):
                with self.subTest(payload=str(payload)[:60]):
                    self.assertIn(self.client.post("/api/characters/generate", json=payload).status_code, (400, 422))
            generate.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_busy_and_failure_release_lock(self):
        with server.CHARACTER_GENERATION_LOCK:
            self.assertEqual(self.client.post("/api/characters/generate", json={"description": "주민"}).status_code, 409)
        for status in (502, 503, 504):
            with patch.object(CG, "generate_characters", side_effect=CG.GenerationError("생성 실패", status)):
                result = self.client.post("/api/characters/generate", json={"description": "주민"})
                self.assertEqual(result.status_code, status)
                self.assertEqual(result.json()["message"], "생성 실패")
            self.assertFalse(server.CHARACTER_GENERATION_LOCK.locked())
        self.assertEqual(self.path.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
