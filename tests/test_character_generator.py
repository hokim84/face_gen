"""GPU/로그인 없이 실제 모의 CLI 프로세스로 생성 계약을 검증한다."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import character_generator as CG

ROW = dict(name="리나", subject="1girl", age="", hair="black hair", eyes="blue eyes",
           outfit="white dress", extra="", personality="명랑")


class GeneratorTest(unittest.TestCase):
    def test_monster_schema_and_subprocess(self):
        code = """
import json, pathlib, sys
sys.stdin.reconfigure(encoding='utf-8')
prompt = sys.stdin.read()
assert 'game monster' in prompt
schema = json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text(encoding='utf-8'))
assert set(schema['properties']['rows']['items']['properties']) == {'name', 'prompt'}
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps({'rows':[{'name':'Slime','prompt':'green slime, wide shot'}]}), encoding='utf-8')
"""
        with self.fake_command(code):
            self.assertEqual(CG.generate_monsters("슬라임", 1), [{"name": "Slime", "prompt": "green slime, wide shot"}])

    def fake_command(self, code):
        return patch.object(CG, "cli_command", return_value=[sys.executable, "-c", code])

    def test_real_subprocess_contract(self):
        code = """
import json, pathlib, sys
sys.stdin.reconfigure(encoding='utf-8')
prompt = sys.stdin.read()
assert '마을 주민' in prompt
assert '--sandbox' in sys.argv and 'read-only' in sys.argv
assert '--skip-git-repo-check' in sys.argv
schema = json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text(encoding='utf-8'))
assert schema['properties']['rows']['minItems'] == 2
rows = [json.loads(%r)] * 2
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps({'rows':rows}), encoding='utf-8')
""" % json.dumps(ROW)
        with self.fake_command(code):
            rows = CG.generate_characters('마을 주민 $(echo no) " &', 2, ["명랑"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {**ROW, "expressions": "", "seed": ""})
        self.assertNotIn("id", rows[0])

    def test_invalid_result(self):
        for data in ([], {}, {"rows": []}, {"rows": [ROW], "other": True}):
            with self.subTest(data=data), self.assertRaises(CG.GenerationError):
                CG.validate_result(data, 1, ["명랑"])
        for key, value in (("name", " "), ("hair", ""), ("eyes", None), ("outfit", 12),
                           ("subject", "invalid"), ("personality", "없는성격"), ("extra", "x" * 2001)):
            row = {**ROW, key: value}
            with self.subTest(key=key), self.assertRaises(CG.GenerationError):
                CG.validate_result({"rows": [row]}, 1, ["명랑"])
        with self.assertRaises(CG.GenerationError):
            CG.validate_result({"rows": [{**ROW, "id": "injected"}]}, 1, ["명랑"])

    def test_missing_cli(self):
        with patch.object(CG.shutil, "which", return_value=None), self.assertRaises(CG.GenerationError) as cm:
            CG.generate_characters("주민", 1, ["명랑"])
        self.assertEqual(cm.exception.status, 503)

    def test_windows_npm_wrapper_uses_node_without_shell(self):
        with patch.object(CG.shutil, "which", side_effect=[r"C:\npm\codex.cmd", r"C:\node\node.exe"]), \
                patch.object(CG.Path, "is_file", return_value=True):
            command = CG.cli_command()
        self.assertEqual(command[0], r"C:\node\node.exe")
        self.assertEqual(Path(command[1]).name, "codex.js")

    def test_no_personalities(self):
        with self.assertRaises(CG.GenerationError) as cm:
            CG.generate_characters("주민", 1, [])
        self.assertEqual(cm.exception.status, 400)

    def test_cli_failure_and_auth(self):
        for message, status in (("execution failed", 502), ("authentication failed 401", 503)):
            with self.subTest(message=message), self.fake_command(
                    f"import sys; sys.stderr.write({message!r}); sys.exit(1)"), self.assertRaises(CG.GenerationError) as cm:
                CG.generate_characters("주민", 1, ["명랑"])
            self.assertEqual(cm.exception.status, status)

    def test_missing_and_malformed_json(self):
        for code in ("pass", "import pathlib,sys; pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('oops')"):
            with self.subTest(code=code), self.fake_command(code), self.assertRaises(CG.GenerationError):
                CG.generate_characters("주민", 1, ["명랑"])

    def test_timeout_stops_process_tree(self):
        original = CG.stop_process_tree
        with self.fake_command("import time; time.sleep(30)"), patch.object(
                CG, "stop_process_tree", wraps=original) as stop, self.assertRaises(CG.GenerationError) as cm:
            CG.generate_characters("주민", 1, ["명랑"], timeout=0.2)
        self.assertEqual(cm.exception.status, 504)
        stop.assert_called_once()
        self.assertIsNotNone(stop.call_args.args[0].poll())


if __name__ == "__main__":
    unittest.main()
