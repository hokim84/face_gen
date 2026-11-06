"""머리 위 여백(아웃페인트) 테스트 (ComfyUI 모의 서버 사용)

먼저 모의 서버를 띄웁니다:   python tests/fake_comfy.py
그 다음:                    python tests/test_headroom.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pipeline as P  # noqa: E402

FAKE = os.environ.get("FAKE_COMFY_URL", "http://127.0.0.1:8000")
W, H = 832, 1216


def portrait(head_top):
    """흰 배경 위 인물. head_top 이 0 이하면 정수리가 잘린 그림"""
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.ellipse((W * .25, head_top, W * .75, head_top + H * .4), fill=(90, 60, 40))   # 머리카락
    d.rectangle((W * .15, H * .5, W * .85, H), fill=(60, 80, 160))                 # 몸
    return im


class HeadroomTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"checkpoint": "waiIllustriousSDXL_v150.safetensors", "steps": 20, "cfg": 5, "sampler": "euler", "scheduler": "normal",
                    "negative": "", "expression": {"steps": 10},
                    "headroom": {"max_passes": 2}}
        self.events = []
        self.tmp = tempfile.TemporaryDirectory()
        self.pipe = P.Pipeline(P.ComfyClient(FAKE), self.cfg, {}, self.tmp.name, on_progress=self.events.append)
        # 색 기준으로 판정 (rembg 는 합성 그림에서 결과가 들쭉날쭉)
        self._orig = P.foreground_alpha
        P.foreground_alpha = lambda img, cfg, warn=None: (self._orig_color(img), False)

    def tearDown(self):
        P.foreground_alpha = self._orig
        self.tmp.cleanup()

    @staticmethod
    def _orig_color(img):
        from PIL import ImageChops
        r, g, b = img.convert("RGB").split()
        return ImageChops.darker(ImageChops.darker(r, g), b).point(lambda v: 255 if v < 220 else 0)

    def steps(self):
        return [e for e in self.events if e["type"] == "step" and e["name"] == "headroom"]

    def warns(self):
        return [e["msg"] for e in self.events if e["type"] == "log" and e["level"] == "warn"]

    def test_shift_and_mask(self):
        im = portrait(-100)
        shifted, mask = P.shift_down_for_outpaint(im, 120, (240, 240, 240), ramp=64)
        self.assertEqual(shifted.size, im.size)
        self.assertEqual(shifted.getpixel((W // 2, 130)), im.getpixel((W // 2, 10)))  # 아래로 120px 이동
        self.assertEqual(shifted.getpixel((W // 2, 50)), (240, 240, 240))           # 빈칸은 배경색
        self.assertEqual(mask.getpixel((0, 0)), 255)
        self.assertEqual(mask.getpixel((0, 119)), 255)
        self.assertTrue(0 < mask.getpixel((0, 150)) < 255)                         # 이음매는 점점 옅어짐
        self.assertEqual(mask.getpixel((0, 120 + 64)), 0)

    def test_background_color(self):
        im = portrait(100)
        self.assertEqual(P.background_color(im, self._orig_color(im)), (255, 255, 255))
        gray = Image.new("RGB", (W, H), (230, 228, 225))
        self.assertEqual(P.background_color(gray, Image.new("L", (W, H), 0)), (230, 228, 225))

    def test_top_coverage(self):
        self.assertEqual(P.top_coverage(self._orig_color(portrait(100)), 36), 0)
        self.assertGreater(P.top_coverage(self._orig_color(portrait(-100)), 36), 0.2)

    def test_enough_headroom_untouched(self):
        im = portrait(100)
        out = self.pipe._ensure_headroom("npc_x", im, "1girl", 1)
        self.assertIs(out, im)
        self.assertEqual(self.steps(), [])

    def test_disabled(self):
        self.cfg["headroom"]["enabled"] = False
        im = portrait(-100)
        self.assertIs(self.pipe._ensure_headroom("npc_x", im, "1girl", 1), im)
        self.assertEqual(self.steps(), [])

    def test_cut_head_outpainted(self):
        # 모의 서버는 받은 그림을 거의 그대로 돌려주므로 머리가 위에 닿은 채로 남게 만든다 (머리를 아주 길게)
        # → 최대 반복 후 경고. 실제 모델이라면 머리를 이어 그린다.
        im = portrait(-100)
        ImageDraw.Draw(im).rectangle((W * .3, 0, W * .7, H * .3), fill=(90, 60, 40))
        orig = P.shift_down_for_outpaint

        def hair_top(img, pad, bg, ramp=None):
            shifted, mask = orig(img, pad, bg, ramp)
            ImageDraw.Draw(shifted).rectangle((W * .3, 0, W * .7, pad), fill=(90, 60, 40))
            return shifted, mask
        P.shift_down_for_outpaint = hair_top
        self.addCleanup(setattr, P, "shift_down_for_outpaint", orig)
        out = self.pipe._ensure_headroom("npc_x", im, "1girl", 1)
        self.assertEqual(out.size, (W, H))
        self.assertEqual(len(self.steps()), 2)
        self.assertTrue(any("다시 뽑기" in w for w in self.warns()))
        # 원래 그림은 아래로 2 * pad 만큼 내려가 있다
        pad = int(H * 0.22) // 8 * 8
        self.assertEqual(out.getpixel((W // 2, H - 1)), im.getpixel((W // 2, H - 1 - 2 * pad)))

    def test_outpaint_fixes_when_model_fills_background(self):
        # 모의 서버는 받은 그림(위쪽은 배경색)을 그대로 돌려줌 = 모델이 위쪽을 배경으로 채운 경우 → 한 번에 해결
        im = portrait(-20)
        out = self.pipe._ensure_headroom("npc_x", im, "1girl", 1)
        self.assertEqual(len(self.steps()), 1)
        self.assertEqual(self.warns(), [])
        self.assertEqual(P.top_coverage(self._orig_color(out), int(H * 0.03)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
