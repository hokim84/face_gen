"""Type routing, result freshness and original-coordinate layer integration."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image
from fastapi.testclient import TestClient
import image_types as IT
import pipeline as P
import parts_tool as PT
import server

ROOT = Path(__file__).resolve().parent.parent


class ImageTypeTest(unittest.TestCase):
    def test_type_prompt_defaults_and_overrides(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.pop("type_prompts", None)
        for kind in IT.TYPES:
            row = {**self.row, "image_type": kind, "subject": "1boy"}
            positive = P.build_prompt(cfg, row, "closed mouth")
            self.assertIn(IT.DEFAULT_TYPE_PROMPTS[kind]["positive"], positive)
            negative = P.build_negative(cfg, row)
            self.assertIn(cfg["negative"], negative)
            if kind == "monster":
                self.assertIn("non-human creature", positive)
                self.assertNotIn("1girl", positive)
                self.assertNotIn("1boy", positive)
                self.assertIn("monster girl", negative)
            else:
                self.assertNotIn("monster girl", negative)
        cfg["type_prompts"] = {"monster": {"positive": "armored beast", "negative": "human face"}}
        self.assertIn("armored beast", P.build_prompt(cfg, self.row, ""))
        self.assertNotIn("distinct creature anatomy", P.build_prompt(cfg, self.row, ""))
        self.assertEqual(P.build_negative(cfg, self.row), cfg["negative"] + ", human face")
        cfg["type_prompts"]["monster"] = {"positive": "", "negative": ""}
        self.assertEqual(P.build_negative(cfg, self.row), cfg["negative"])
        self.assertNotIn("non-human creature", P.build_prompt(cfg, self.row, ""))

    def test_actual_graph_contains_type_positive_and_negative_once(self):
        self.pipe.run_character(self.row, base_only=True)
        graph = self.client.run.call_args.args[0]
        encoded = [n["inputs"]["text"] for n in graph.values() if n["class_type"] == "CLIPTextEncode"]
        self.assertEqual(sum("distinct creature anatomy" in value for value in encoded), 1)
        self.assertEqual(sum("monster girl" in value for value in encoded), 1)
        negative = next(value for value in encoded if "monster girl" in value)
        self.assertEqual(negative.count("monster girl"), 1)
        twice = IT.effective_config(IT.effective_config(self.cfg, self.row), self.row)
        self.assertEqual(P.build_negative(twice, self.row), negative)

    def test_type_prompt_config_roundtrip_and_validation(self):
        path = self.root / "config.json"
        cfg = copy.deepcopy(self.cfg)
        cfg.pop("type_prompts", None)
        cfg.update(width=832, height=1216)
        path.write_text(json.dumps(cfg), encoding="utf-8")
        with patch.object(server.Paths, "config", path), TestClient(server.app) as api:
            before = path.read_bytes()
            loaded = api.get("/api/config").json()
            self.assertEqual(set(loaded["type_prompts"]), set(IT.TYPES))
            self.assertEqual(path.read_bytes(), before)
            loaded["type_prompts"]["monster"] = {"positive": "stone golem", "negative": "human face"}
            response = api.put("/api/config", json=loaded)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(api.get("/api/config").json()["type_prompts"]["monster"]["positive"], "stone golem")
            for value in ([], {"monster": "bad"}, {"monster": {"positive": 3}}, {"wrong": {}}):
                bad = {**loaded, "type_prompts": value}
                response = api.put("/api/config", json=bad)
                self.assertEqual(response.status_code, 400, response.text)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.cfg.update(width=128, height=192, output_dir=str(self.root / "out"), remove_background=False)
        self.cfg["headroom"]["enabled"] = True
        self.cfg["type_poses"] = {"character": "upper_front.png", "npc": "upper_front.png"}
        self.cfg["controlnet"].update(pose_dir=str(ROOT / "poses"), model="controlnet-union-sdxl-promax.safetensors")
        self.cfg["comfy_url"] = os.environ.get("FAKE_COMFY_URL", "http://127.0.0.1:18001")
        self.ex = json.loads((ROOT / "expressions.json").read_text(encoding="utf-8"))
        self.client = Mock()
        self.client.run.return_value = Image.new("RGB", (128, 192), "white")
        self.client.upload_image.return_value = "pose.png"
        self.row = {"id": "creature", "name": "슬라임", "image_type": "monster", "prompt": "green slime, wide shot"}
        self.pipe = P.Pipeline(self.client, self.cfg, self.ex, rows=[self.row])

    def test_monster_has_no_human_stages_and_finishes(self):
        with patch.object(P, "detect_face", side_effect=AssertionError("face detector called")), \
             patch.object(self.pipe, "_upload_pose", wraps=self.pipe._upload_pose) as pose, \
             patch.object(self.pipe, "_ensure_headroom", side_effect=AssertionError("headroom called")):
            self.pipe.run_character(self.row, base_only=True)
        graph = self.client.run.call_args.args[0]
        self.assertNotIn("ControlNetLoader", [n["class_type"] for n in graph.values()])
        self.client.upload_image.assert_not_called()
        meta = P.read_json(self.root / "out/creature/meta.json")
        self.assertEqual(meta["expressions"], [])
        self.assertEqual(meta["image_type"], "monster")
        prompt = P.build_prompt(self.cfg, self.row, "closed mouth")
        self.assertIn("green slime, wide shot", prompt)
        self.assertNotIn("1girl", prompt)
        self.assertNotIn("upper body", prompt)
        self.assertNotIn("closed mouth", prompt)
        self.assertTrue(self.cfg["controlnet"]["enabled"])

    def test_npc_uses_type_pose_without_face_or_expression(self):
        row = {"id": "resident", "image_type": "npc"}
        cfg = copy.deepcopy(self.cfg)
        cfg["headroom"]["enabled"] = False
        pipe = P.Pipeline(self.client, cfg, self.ex, rows=[row])
        with patch.object(P, "detect_face", side_effect=AssertionError("face detector called")):
            pipe.run_character(row)
        self.assertEqual(P.read_json(self.root / "out/resident/meta.json")["expressions"], [])
        graph = self.client.run.call_args.args[0]
        self.assertIn("ControlNetLoader", [n["class_type"] for n in graph.values()])
        with self.assertRaises(ValueError):
            pipe.generate_expression("resident", "smile")

    def test_layers_keep_pixels_and_do_not_detect_face(self):
        row = {"id": "resident", "image_type": "npc", "name": "주민"}
        d = self.root / "out/resident"
        d.mkdir(parents=True)
        image = Image.new("RGB", (87, 143), "red")
        image.putpixel((3, 7), (0, 20, 40))
        image.save(d / "base_src.png")
        P.write_json(d / "work.json", {"seed": 3, "image_type": "npc"})
        pipe = P.Pipeline(self.client, self.cfg, self.ex)
        spec = P.read_json(ROOT / "parts.json")

        def split(client, spec, source, **kwargs):
            dest = PT.out_root(spec) / source["id"]
            copied = Image.open(dest / "source.png")
            self.assertEqual(copied.size, image.size)
            self.assertEqual(copied.tobytes(), image.tobytes())
            P.write_json(dest / "layers.json", {"width": 87, "height": 143, "layers": []})

        with patch.object(P, "detect_face", side_effect=AssertionError("face detector called")), \
             patch.object(PT, "check_seethrough"), patch.object(PT, "split_source", side_effect=split) as run:
            self.assertEqual(pipe.generate_layers(row, spec), "done")
            self.assertEqual(pipe.generate_layers(row, spec), "skipped")
            image.putpixel((3, 7), (40, 20, 0))
            image.save(d / "base_src.png")
            self.assertEqual(pipe.generate_layers(row, spec), "done")
            self.assertEqual(run.call_count, 2)
        self.assertNotIn("_layers", spec["out_dir"])

    def test_type_change_preserves_files_but_excludes_completion(self):
        self.pipe.run_character(self.row)
        changed = {**self.row, "image_type": "character"}
        original = (self.root / "out/creature/meta.json").read_bytes()
        with patch.object(server, "out_root", return_value=self.root / "out"):
            info = server.portrait_summary("creature", changed)
        self.assertTrue(info["stale"])
        self.assertEqual(info["status"], "base")
        self.assertFalse(info["stages"]["image"])
        self.assertEqual(original, (self.root / "out/creature/meta.json").read_bytes())

    def test_reusing_image_only_base_keeps_approval(self):
        self.pipe.run_character(self.row)
        path = self.root / "out/creature/meta.json"
        meta = P.read_json(path)
        meta["approved"] = True
        P.write_json(path, meta)
        self.pipe.run_character(self.row, base_only=True)
        self.assertTrue(P.read_json(path)["approved"])

    def test_api_rejects_unsupported_jobs_and_legacy_read_is_not_a_write(self):
        csv_path = self.root / "characters.csv"
        csv_path.write_text("id,name,image_type,prompt\nold,기존,,\ncreature,슬라임,monster,slime\nresident,주민,npc,\n", encoding="utf-8")
        before = csv_path.read_bytes()
        with patch.object(server.Paths, "characters", csv_path), \
             patch.object(server, "load_config", return_value=self.cfg), TestClient(server.app) as api:
            response = api.get("/api/characters")
            self.assertEqual(response.json()[0]["image_type"], "character")
            self.assertEqual(csv_path.read_bytes(), before)
            for cid, mode in (("creature", "layers"), ("creature", "expression"), ("resident", "expressions")):
                r = api.post("/api/jobs", json={"ids": [cid], "mode": mode, "expression": "smile"})
                self.assertEqual(r.status_code, 400, r.text)
            with patch.object(server.CG, "generate_monsters", return_value=[{"name": "슬라임", "prompt": "slime"}]):
                r = api.post("/api/characters/generate", json={"description": "슬라임", "count": 1, "image_type": "monster"})
                self.assertEqual(r.json()["rows"][0]["image_type"], "monster")
                self.assertEqual(csv_path.read_bytes(), before)

    def test_full_failure_stops_before_layers(self):
        row = {"id": "hero", "image_type": "character"}
        job = server.Job("hero", "주인공", "full")
        with patch.object(server, "load_config", return_value=self.cfg), \
             patch.object(server, "load_rows", return_value=[row]), \
             patch.object(server.P, "ComfyClient"), patch.object(server.P, "Pipeline") as pipe:
            pipe.return_value.run_character.side_effect = RuntimeError("expression failed")
            with self.assertRaisesRegex(RuntimeError, "expression failed"):
                server.QUEUE._execute(job)
            pipe.return_value.generate_layers.assert_not_called()

    def test_retry_of_failed_layers_keeps_successful_base_and_expressions(self):
        job = server.Job("hero", "주인공", "full", new_seed=True, force=True)
        job.retry_stage = "layers"
        self.assertEqual(server.retry_spec(job, {"id": "hero"})[1:], ("layers", None, None, False, False))

    def test_failed_layer_replacement_preserves_previous_layers(self):
        row = {"id": "hero", "image_type": "character"}
        d = self.root / "out/hero"
        d.mkdir(parents=True)
        Image.new("RGB", (50, 70), "white").save(d / "base_src.png")
        previous = self.root / "out/_layers/hero"
        previous.mkdir(parents=True)
        (previous / "layers.json").write_text('{"old": true}', encoding="utf-8")
        with patch.object(PT, "check_seethrough"), \
             patch.object(PT, "split_source", side_effect=RuntimeError("split failed")):
            with self.assertRaisesRegex(RuntimeError, "split failed"):
                self.pipe.generate_layers(row, P.read_json(ROOT / "parts.json"), force=True)
        self.assertEqual(P.read_json(previous / "layers.json"), {"old": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
