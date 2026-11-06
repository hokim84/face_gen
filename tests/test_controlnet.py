"""ControlNet 자세 고정 테스트 (ComfyUI 모의 서버 사용)

먼저 모의 서버를 띄웁니다:   python tests/fake_comfy.py
그 다음:                    python tests/test_controlnet.py
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pipeline as P  # noqa: E402
import server  # noqa: E402

FAKE = os.environ.get("FAKE_COMFY_URL", "http://127.0.0.1:8000")
W, H = 832, 1216
CN_MODEL = "controlnet-union-sdxl-promax.safetensors"


def fake_state():
    return json.loads(urllib.request.urlopen(FAKE + "/_fake/state").read())


def classes(g):
    return [n["class_type"] for n in g.values()]


class ControlNetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.poses = Path(self.tmp.name) / "poses"
        self.poses.mkdir()
        Image.new("RGB", (W, H), "black").save(self.poses / "upper_front.png")
        self.cfg = {"checkpoint": "waiIllustriousSDXL_v150.safetensors", "width": W, "height": H, "steps": 20, "cfg": 5,
                    "sampler": "euler", "scheduler": "normal", "negative": "", "quality_prefix": "", "framing": "",
                    "background": "", "expression": {"steps": 10}, "headroom": {"enabled": False},
                    "controlnet": {"enabled": True, "model": CN_MODEL, "pose_dir": str(self.poses),
                                   "strength": 0.55, "end": 0.4}}
        self.events = []
        self.pipe = P.Pipeline(P.ComfyClient(FAKE), self.cfg, {"base_expression": "light smile"},
                               Path(self.tmp.name) / "out", on_progress=self.events.append)

    def tearDown(self):
        self.tmp.cleanup()

    def warnings(self):
        return [e["msg"] for e in self.events if e["type"] == "log" and e.get("level") == "warn"]

    def base(self, **row):
        before = fake_state()["controlnet"]
        self.pipe.generate_base({"id": "npc_t", **row}, force=True)
        st = fake_state()
        return st["controlnet"] - before, st["last_control"]

    # ---- 그래프 ---- #
    def test_graph_without_pose_unchanged(self):
        """포즈를 안 주면 (parts_tool 의 cowboy shot 등) ControlNet 노드가 없음"""
        g = P.graph_txt2img(self.cfg, "a", "b", 1)
        self.assertFalse({"ControlNetLoader", "ControlNetApplyAdvanced", "LoadImage"} & set(classes(g)))

    def test_graph_wires_conditioning(self):
        g = P.graph_txt2img(self.cfg, "a", "b", 1, "pose.png")
        ks = next(n for n in g.values() if n["class_type"] == "KSampler")["inputs"]
        apply_id = next(k for k, n in g.items() if n["class_type"] == "ControlNetApplyAdvanced")
        self.assertEqual((ks["positive"], ks["negative"]), ([apply_id, 0], [apply_id, 1]))
        a = g[apply_id]["inputs"]
        self.assertEqual((a["strength"], a["start_percent"], a["end_percent"]), (0.55, 0.0, 0.4))
        self.assertIn("SetUnionControlNetType", classes(g))

    def test_not_union_model_skips_type_node(self):
        self.cfg["controlnet"]["union_type"] = ""
        self.assertNotIn("SetUnionControlNetType", classes(P.graph_txt2img(self.cfg, "a", "b", 1, "pose.png")))
        n, last = self.base()
        self.assertEqual(n, 1)
        self.assertIsNone(last["type"])

    # ---- base 생성 ---- #
    def test_base_uses_default_pose(self):
        n, last = self.base()
        self.assertEqual(n, 1)
        self.assertEqual(last, {"image": "pose_upper_front.png", "strength": 0.55, "start": 0.0, "end": 0.4,
                                "type": "openpose"})
        self.assertEqual(self.warnings(), [])

    def test_disabled_sends_no_controlnet(self):
        self.cfg["controlnet"]["enabled"] = False
        n, _ = self.base()
        self.assertEqual(n, 0)

    def test_row_pose_override(self):
        Image.new("RGB", (W, H), "black").save(self.poses / "tilt.png")
        _, last = self.base(pose="tilt")
        self.assertEqual(last["image"], "pose_tilt.png")

    def test_missing_row_pose_falls_back(self):
        _, last = self.base(pose="nope")
        self.assertEqual(last["image"], "pose_upper_front.png")
        self.assertTrue(any("포즈 'nope'" in w for w in self.warnings()))

    def test_row_pose_cannot_leave_pose_dir(self):
        self.assertEqual(P.pose_path(self.cfg, "../../config").parent, self.poses)

    def test_missing_default_pose_is_config_error(self):
        (self.poses / "upper_front.png").unlink()
        with self.assertRaises(P.ConfigError):
            self.pipe.generate_base({"id": "npc_t"}, force=True)

    def test_aspect_mismatch_warns(self):
        Image.new("RGB", (512, 512), "black").save(self.poses / "upper_front.png")
        self.base()
        self.assertTrue(any("비율" in w for w in self.warnings()))

    # ---- 포즈 템플릿 만들기 ---- #
    def test_make_pose(self):
        src = Image.new("RGB", (W, H), "white")
        path = P.make_pose(P.ComfyClient(FAKE), self.cfg, src, "from_npc")
        self.assertEqual(path, self.poses / "from_npc.png")
        with Image.open(path) as im:
            self.assertEqual(im.size, (W, H))
        _, last = self.base(pose="from_npc")
        self.assertEqual(last["image"], "pose_from_npc.png")

    def test_make_pose_refuses_blank(self):
        """인물을 못 찾아 검은 그림만 나오면 저장하지 않음 (빈 템플릿은 구도를 오히려 망침)"""
        client = P.ComfyClient(FAKE)
        client.run = lambda g, **kw: Image.new("RGB", (W, H), "black")
        with self.assertRaises(RuntimeError):
            P.make_pose(client, self.cfg, Image.new("RGB", (W, H), "white"), "blank")
        self.assertFalse((self.poses / "blank.png").exists())

    # ---- 웹 서버 검증 ---- #
    def test_validate_config(self):
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        cfg["controlnet"] = {**self.cfg["controlnet"]}
        self.assertEqual([e for e in server.validate_config(cfg) if e.startswith("controlnet")], [])
        for bad in ({"model": ""}, {"start": 0.5, "end": 0.4}, {"union_type": "pose"}, {"pose": "a/b.png"},
                    {"strength": 3}):
            cfg["controlnet"] = {**self.cfg["controlnet"], **bad}
            self.assertTrue([e for e in server.validate_config(cfg) if e.startswith("controlnet")], bad)
        cfg["controlnet"] = {"enabled": False, "model": ""}   # 꺼져 있으면 모델 비어도 됨
        cfg.pop("type_poses", None)  # Legacy CLI configuration without type-specific fixed poses.
        self.assertEqual([e for e in server.validate_config(cfg) if e.startswith("controlnet")], [])

    def test_validate_rows_pose(self):
        rows = [{"id": "a", "pose": "upper_front"}, {"id": "b", "pose": "nope"}, {"id": "c", "pose": "../x"}]
        errs, warns = server.validate_rows(rows, {"personalities": {}, "expressions": {}}, self.cfg)
        self.assertEqual(len(errs), 1)
        self.assertIn("3행", errs[0])
        self.assertEqual(len(warns), 1)
        self.assertIn("'nope'", warns[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
