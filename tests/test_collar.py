"""목/깃 깊이 마스크(parts_tool.collar_mask) 테스트. 서버·GPU 없이 합성 레이어로 확인.
python tests/test_collar.py"""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import parts_tool as PT  # noqa: E402

W, H = 200, 200


def save_layer(d, name, box, depth_fn, color):
    """box 영역을 채운 RGBA 레이어 + 깊이(L) 레이어. depth_fn(y) → 0~255"""
    w, h = box[2] - box[0], box[3] - box[1]
    Image.new("RGBA", (w, h), color + (255,)).save(d / "layers" / f"{name}.png")
    z = np.array([[depth_fn(box[1] + y)] * w for y in range(h)], np.uint8)
    Image.fromarray(z, "L").save(d / "layers" / f"{name}_depth.png")
    return {"name": name, "file": f"layers/{name}.png", "depth_file": f"layers/{name}_depth.png",
            "left": box[0], "top": box[1], "right": box[2], "bottom": box[3], "depth_median": 0.5}


def make_source(d, topwear_depth):
    (d / "layers").mkdir(parents=True)
    Image.new("RGB", (W, H), "white").save(d / "source.png")
    layers = [save_layer(d, "neck", (80, 40, 120, 160), lambda y: 100, (230, 180, 160)),
              save_layer(d, "topwear", (40, 100, 160, 200), topwear_depth, (30, 40, 60))]
    (d / "layers.json").write_text(json.dumps({"id": d.name, "width": W, "height": H, "layers": layers}),
                                   encoding="utf-8")


def sailor(y):
    """뒷깃(y 100~124)은 목보다 멀고, 그 아래 앞깃은 가까움. 맨 위 2줄은 윤곽선처럼 깊이가 애매함"""
    if y < 102:
        return 97
    return 140 if y < 125 else 60


class CollarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_back_collar_behind_neck(self):
        d = self.root / "a"
        make_source(d, sailor)
        m, pos = PT.collar_mask(d)
        self.assertEqual(pos, (80, 40))
        m = np.asarray(m)
        at = lambda x, y: m[y - 40, x - 80]
        self.assertEqual(at(100, 112), 255)      # 뒷깃 → 목이 앞
        self.assertEqual(at(100, 100), 255)      # 애매한 윤곽선 띠도 뒤로 (목 위에 선이 남지 않게)
        self.assertEqual(at(100, 145), 0)        # 앞깃 → 상의가 앞

        layers = PT.neck_over_collar(PT.load_layers(d), (Image.fromarray(m), pos))
        names = [l[0] for l in layers]
        self.assertEqual(names.index("neck"), names.index("topwear") + 1)
        canvas = Image.new("RGBA", (W, H), "white")
        for _, im, p, _ in layers:
            canvas.alpha_composite(im, p)
        self.assertEqual(canvas.getpixel((100, 112))[:3], (230, 180, 160))   # 뒷깃 위로 목
        self.assertEqual(canvas.getpixel((100, 100))[:3], (230, 180, 160))
        self.assertEqual(canvas.getpixel((100, 145))[:3], (30, 40, 60))      # 앞깃이 목을 덮음
        self.assertEqual(canvas.getpixel((60, 112))[:3], (30, 40, 60))       # 목 밖의 상의는 그대로

    def test_collar_in_front(self):
        """높은 옷깃처럼 상의가 전부 목 앞이면 마스크 없음 (원래 순서 그대로)"""
        d = self.root / "b"
        make_source(d, lambda y: 60)
        self.assertIsNone(PT.collar_mask(d))
        layers = PT.load_layers(d)
        self.assertIs(PT.neck_over_collar(layers, None), layers)

    def test_no_depth(self):
        d = self.root / "c"
        make_source(d, sailor)
        lj = json.loads((d / "layers.json").read_text(encoding="utf-8"))
        for e in lj["layers"]:
            del e["depth_file"]
        (d / "layers.json").write_text(json.dumps(lj), encoding="utf-8")
        self.assertIsNone(PT.collar_mask(d))

    def test_psd_layer_mask(self):
        d = self.root / "a"
        make_source(d, sailor)
        for depth, nch in ((False, 5), (True, 3)):         # 투명도 + 색 + 레이어 마스크(-2)
            psd = PT.make_psd(d, depth=depth)
            pos = 26 + 8 + 4 + 4
            n = struct.unpack(">h", psd[pos:pos + 2])[0]
            pos += 2
            recs = []   # (이름, 채널 id, 마스크 데이터 길이)
            for _ in range(n):
                c = struct.unpack(">H", psd[pos + 16:pos + 18])[0]
                ids = [struct.unpack(">h", psd[pos + 18 + 6 * i:pos + 20 + 6 * i])[0] for i in range(c)]
                pos += 18 + 6 * c + 12                              # 채널 목록, 8BIM norm, 불투명도 등
                extra = struct.unpack(">I", psd[pos:pos + 4])[0]
                x = pos + 4
                mlen = struct.unpack(">I", psd[x:x + 4])[0]
                x += 4 + mlen
                x += 4 + struct.unpack(">I", psd[x:x + 4])[0]       # 블렌딩 범위
                recs.append((psd[x + 1:x + 1 + psd[x]].decode(), ids, mlen))
                pos += 4 + extra
            names = [r[0] for r in recs]
            self.assertEqual(names.index("neck"), names.index("topwear") + 1, depth)
            neck = recs[names.index("neck")]
            self.assertEqual((len(neck[1]), neck[1][-1], neck[2]), (nch, -2, 20), depth)
            self.assertNotIn(-2, recs[names.index("topwear")][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
