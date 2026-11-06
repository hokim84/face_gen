"""파츠 조합용 소스 생성 + SeeThrough 레이어 분리 (실험용, 두 단계로 나눠 실행)

  python parts_tool.py gen   [id ...] [--force]   1단계: 구도를 맞춘 소스 초상 생성 (SDXL, 기본 노드)
  python parts_tool.py split [id ...] [--force]   2단계: SeeThrough 로 레이어 분리 (ComfyUI-See-through 커스텀 노드 필요)
  python parts_tool.py preview [id ...]           GPU 없이 check.png / sheet.png 만 다시 그림
  python parts_tool.py psd [id ...]               레이어를 PSD 로 저장 (<id>.psd, <id>_depth.psd)
  python parts_tool.py status                     진행 상황

웹 UI(server.py)의 [파츠] 탭도 이 모듈의 gen_source / split_source / compose 를 씁니다.
소스에 "from": "<캐릭터 id>" 가 있으면 새로 그리지 않고 파이프라인이 만든 base_src.png 를 가져옵니다
("align": true 면 얼굴 기준으로 잘라 다른 소스와 구도를 맞춤). 웹 UI 의 [생성 이미지에서 가져오기]가 이 항목을 만듭니다.

설정은 parts.json, 생성 설정(체크포인트·VAE·샘플러)은 config.json 을 그대로 씁니다.
결과는 parts_out/<id>/ 에 저장합니다.
  source.png, source.json                  1단계
  layers/<이름>.png, layers.json            2단계 (좌표는 source.png 기준)
  layers/<이름>_depth.png                   2단계 깊이 (회색조, 밝을수록 멂)
  check.png (원본 | 레이어 재합성), sheet.png (레이어 목록)

두 단계는 VRAM 을 많이 쓰는 모델이 달라서(SDXL ↔ SeeThrough) 각 단계 시작 때 ComfyUI 의 모델을 내립니다(/free).
이미 결과가 있는 소스는 건너뜁니다. 다시 만들려면 --force.
"""
import argparse
import io
import json
import re
import sys
import threading
import struct
import time
import urllib.error
import urllib.parse
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw

import pipeline as P

ROOT = Path(__file__).resolve().parent
SEETHROUGH_NODE = "SeeThrough_GenerateLayers"
SOURCE_ID_RE = re.compile(r"^[\w\-]+$")


class SpecError(ValueError):
    pass


def validate_sources(sources):
    """[{id, seed, tags}] 검사. 문제 목록을 반환 (없으면 [])"""
    errs, seen = [], set()
    if not isinstance(sources, list):
        return ["sources 는 목록이어야 합니다"]
    for i, s in enumerate(sources, 1):
        if not isinstance(s, dict):
            errs.append(f"{i}행: 형식이 잘못됐습니다")
            continue
        sid = str(s.get("id", "")).strip()
        if not SOURCE_ID_RE.match(sid):
            errs.append(f"{i}행: id 는 영문·숫자·_·- 만 쓸 수 있습니다 ({sid!r})")
        elif sid in seen:
            errs.append(f"{i}행: 중복된 id {sid}")
        seen.add(sid)
        seed = s.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2 ** 32:
            errs.append(f"{i}행({sid}): seed 는 0 이상의 정수여야 합니다")
        if not str(s.get("tags", "")).strip():
            errs.append(f"{i}행({sid}): 태그가 비어 있습니다")
        if "from" in s and (not isinstance(s["from"], str) or not s["from"].strip()):
            errs.append(f"{i}행({sid}): from 은 캐릭터 id 여야 합니다")
        if "align" in s and not isinstance(s["align"], bool):
            errs.append(f"{i}행({sid}): align 은 true / false 여야 합니다")
    return errs


def load_spec(path):
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    errs = validate_sources(spec.get("sources"))
    if errs:
        raise SpecError("parts.json: " + "; ".join(errs))
    return spec


def out_root(spec):
    p = Path(spec.get("out_dir", "parts_out"))
    return p if p.is_absolute() else ROOT / p


def pick(spec, ids):
    if not ids:
        return spec["sources"]
    by_id = {s["id"]: s for s in spec["sources"]}
    miss = [i for i in ids if i not in by_id]
    if miss:
        raise SpecError(f"parts.json 에 없는 id: {miss}")
    return [by_id[i] for i in ids]


def source_state(root, s):
    """{'source': bool, 'layers': int|None, 'groups': [이 소스에 있는 조합 그룹]}"""
    d = root / s["id"]
    lj = P.read_json(d / "layers.json")
    groups = sorted({g for g in (group_of(e["name"]) for e in lj["layers"]) if g}) if lj else []
    issues = sorted(layer_issues(lj["layers"])) if lj else []
    return {"source": (d / "source.png").exists(), "layers": len(lj["layers"]) if lj else None, "groups": groups,
            "issues": [f"{n} 레이어가 비정상적으로 커서 정상 위치만 남김 (SeeThrough 오분류)" for n in issues]}


def free_vram(client, warn=print):
    """다른 단계의 모델을 VRAM 에서 내림 (SDXL 과 SeeThrough 가 동시에 올라가면 16GB 가 빠듯함)"""
    try:
        client._post_json("/free", {"unload_models": True, "free_memory": True})
    except Exception as e:
        warn(f"모델 내리기 실패(무시): {e}")


def run_graph(client, graph, timeout, should_stop=None):
    """이미지 출력이 없는 워크플로우용: 끝날 때까지 기다리고 history 항목을 반환.
    should_stop() 이 True 가 되면 ComfyUI 를 중단시키고 P.Cancelled"""
    res = json.loads(client._req("/prompt", json.dumps({"prompt": graph, "client_id": client.client_id}).encode(),
                                 {"Content-Type": "application/json"}))
    pid, t0 = res["prompt_id"], time.time()
    try:
        while True:
            if should_stop and should_stop():
                client.interrupt(pid)
                raise P.Cancelled()
            hist = json.loads(client._req(f"/history/{pid}"))
            st = hist.get(pid, {}).get("status", {})
            if st.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI 실행 실패: {json.dumps(st.get('messages', []), ensure_ascii=False)[:2000]}")
            if st.get("completed"):
                return hist[pid]
            if time.time() - t0 > timeout:
                client.interrupt(pid)
                raise TimeoutError(f"ComfyUI 응답 시간 초과 ({timeout}s)")
            time.sleep(0.5)
    except KeyboardInterrupt:
        client.interrupt(pid)
        raise


def view(client, filename, subfolder=""):
    q = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": "output"})
    return client._req("/view?" + q)


# --------------------------------------------------------------------------- #
# 1단계: 소스 생성
# --------------------------------------------------------------------------- #
def graph_img2img(cfg, image_name, pos, neg, seed, denoise):
    g = P.Graph()
    model, clip, vae = P._model(g, cfg)
    li = g.add("LoadImage", image=image_name)
    enc = g.add("VAEEncode", pixels=[li, 0], vae=vae)
    p = g.add("CLIPTextEncode", text=pos, clip=clip)
    n = g.add("CLIPTextEncode", text=neg, clip=clip)
    s = g.add("KSampler", model=model, seed=seed, steps=cfg["steps"], cfg=cfg["cfg"],
              sampler_name=cfg["sampler"], scheduler=cfg["scheduler"],
              positive=[p, 0], negative=[n, 0], latent_image=[enc, 0], denoise=denoise)
    d = g.add("VAEDecode", samples=[s, 0], vae=vae)
    g.add("SaveImage", images=[d, 0], filename_prefix="npc_parts/src")
    return g.nodes


def align_crop(img, face, W, H, face_height, face_top):
    """얼굴 높이가 H*face_height, 얼굴 위쪽이 H*face_top 에 오도록 잘라 W×H 로 키움.
    원본 밖으로 나가는 부분은 위쪽 양 모서리의 배경색 (흰색으로 채우면 회색 배경과 띠가 생김)"""
    x0, y0, x1, y1 = face
    ch = (y1 - y0) / face_height
    cw = ch * W / H
    cx, cy0 = (x0 + x1) / 2, y0 - face_top * ch
    box = (round(cx - cw / 2), round(cy0), round(cx + cw / 2), round(cy0 + ch))
    k = max(8, img.width // 20)
    corners = [img.crop((0, 0, k, k)), img.crop((img.width - k, 0, img.width, k))]
    bg = tuple(sum(c.resize((1, 1), Image.BOX).getpixel((0, 0))[i] for c in corners) // 2 for i in range(3))
    canvas = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), bg)
    canvas.paste(img, (-box[0], -box[1]))
    return canvas.resize((W, H), Image.LANCZOS), box


def gen_source(client, cfg, spec, s, should_stop=None, on_image=None, warn=print):
    """소스 1개 생성 → parts_out/<id>/source.png. on_image(초) 는 이미지 1장마다"""
    al = spec["align"]
    W, H = cfg["width"], cfg["height"]
    neg = ", ".join(t for t in (cfg["negative"], spec.get("negative_extra", "")) if t)
    d = out_root(spec) / s["id"]
    d.mkdir(parents=True, exist_ok=True)
    # 멀리서(cowboy shot) 찍고 얼굴 기준으로 잘라 키운 뒤 다시 다듬기 → 모든 소스의 얼굴 위치·크기가 같아짐
    t0 = time.time()
    wide = f"{cfg['quality_prefix']}, {s['tags']}, cowboy shot, {spec['prompt']}"
    g = P.graph_txt2img(cfg, wide, neg, s["seed"])
    for n in g.values():
        if n["class_type"] == "SaveImage":
            n["inputs"]["filename_prefix"] = "npc_parts/wide"
    img = client.run(g, should_stop=should_stop)
    if on_image:
        on_image(time.time() - t0)
    face = P.detect_face(img, cfg, warn=warn)
    crop, box = align_crop(img, face, W, H, al["face_height"], al["face_top"])
    name = client.upload_image(crop, f"parts_{s['id']}_crop.png")
    pos = f"{cfg['quality_prefix']}, {s['tags']}, upper body, {spec['prompt']}"
    t0 = time.time()
    src = client.run(graph_img2img(cfg, name, pos, neg, s["seed"], al["refine_denoise"]), should_stop=should_stop)
    if on_image:
        on_image(time.time() - t0)
    if src.size != (W, H):
        src = src.resize((W, H), Image.LANCZOS)
    # 소스가 바뀌면 예전 분리 결과는 맞지 않으므로 지움
    for old in ("layers.json", "check.png", "sheet.png"):
        (d / old).unlink(missing_ok=True)
    src.save(d / "source.png")
    img.save(d / "wide.png")
    P.write_json(d / "source.json", {
        "id": s["id"], "seed": s["seed"], "tags": s["tags"], "prompt": pos, "negative": neg,
        "wide_face": [round(v) for v in face], "crop_box": box,
        "face": [round(v) for v in P.detect_face(src, cfg, warn=warn)],
    })


def import_source(cfg, spec, s, char_dir, warn=print):
    """파이프라인이 만든 캐릭터 이미지(<output>/<from>/base_src.png)를 소스로 가져옴. GPU 를 쓰지 않음.
    s["align"] 이면 gen 소스처럼 얼굴 기준으로 잘라 키움 → 다른 소스와 파츠를 섞을 수 있음 (img2img 다듬기는 안 함)"""
    src_path = char_dir / "base_src.png"
    if not src_path.exists():
        raise RuntimeError(f"{s['from']}: 생성된 base 이미지가 없습니다. 먼저 캐릭터 base 를 생성하세요.")
    img = Image.open(src_path).convert("RGB")
    # 얼굴 위치는 work.json 의 것을 씀 (base 가 그 뒤에 바뀌었으면 다시 검출)
    work = P.read_json(char_dir / "work.json", {})
    face = work.get("face") if work.get("base_src_mtime") == src_path.stat().st_mtime_ns else None
    if not face and s.get("align", True):
        face = P.detect_face(img, cfg, warn=warn)
    box = None
    if s.get("align", True):
        al = spec["align"]
        img, box = align_crop(img, face, cfg["width"], cfg["height"], al["face_height"], al["face_top"])
        k = img.width / (box[2] - box[0])
        face = [(face[0] - box[0]) * k, (face[1] - box[1]) * k, (face[2] - box[0]) * k, (face[3] - box[1]) * k]
    d = out_root(spec) / s["id"]
    d.mkdir(parents=True, exist_ok=True)
    for old in ("layers.json", "check.png", "sheet.png", "wide.png"):
        (d / old).unlink(missing_ok=True)
    img.save(d / "source.png")
    P.write_json(d / "source.json", {
        "id": s["id"], "from": s["from"], "seed": s["seed"], "tags": s["tags"], "align": bool(box),
        "crop_box": box, "face": [round(v) for v in face] if face else None,
    })


# --------------------------------------------------------------------------- #
# 2단계: SeeThrough 레이어 분리
# --------------------------------------------------------------------------- #
def graph_seethrough(st, image_name, seed, prefix):
    g = P.Graph()
    li = g.add("LoadImage", image=image_name)
    # group_offload 는 끔: NF4 + group offload 조합은 CUDA illegal memory access 로 ComfyUI 가 죽음 (2026-10-14)
    ld = g.add("SeeThrough_LoadLayerDiffModel", model=st["layerdiff_model"], vae_ckpt="", unet_ckpt="",
               quant_mode=st.get("quant_mode", "nf4"), cache_tag_embeds=True, group_offload=False, auto_download=True)
    dm = g.add("SeeThrough_LoadDepthModel", model=st["depth_model"], quant_mode="none",
               cache_tag_embeds=True, group_offload=False, auto_download=True)
    gl = g.add("SeeThrough_GenerateLayers", image=[li, 0], layerdiff_model=[ld, 0], seed=seed,
               resolution=st.get("resolution", 1280), num_inference_steps=st.get("steps", 30))
    gd = g.add("SeeThrough_GenerateDepth", layers=[gl, 0], depth_model=[dm, 0], seed=seed,
               resolution_depth=st.get("depth_resolution", -1))
    pp = g.add("SeeThrough_PostProcess", layers_depth=[gd, 0], tblr_split=st.get("tblr_split", True),
               use_lama=st.get("use_lama", True))
    g.add("SeeThrough_SavePSD", parts=[pp, 0], filename_prefix=prefix)
    return g.nodes


# 그리는 순서(먼 것부터)는 고정. SeeThrough 의 깊이 중앙값은 순서 판단에 못 믿음:
#   - 흰자가 홍채보다 앞으로 나와 눈동자가 가려짐
#   - 어깨 앞으로 내려온 트윈테일 때문에 back hair 가 얼굴보다 앞으로 나와 얼굴을 덮음
# 다른 소스의 파츠를 섞을 때도 깊이값끼리는 비교할 수 없으므로 이 순서를 씀
DRAW_ORDER = ["back hair", "neck", "bottomwear", "legwear", "topwear", "handwear", "neckwear", "ears", "face",
              "nose", "mouth", "eyewhite", "irides", "eyelash", "eyebrow", "front hair", "headwear", "eyewear"]


def part_base(name):
    """'eyes-l' → 'eyes', 'front hair' → 'front hair'"""
    return name.rsplit("-", 1)[0] if name.endswith(("-l", "-r")) else name


def draw_order(entries, name=lambda e: e["name"], depth=lambda e: e["depth_median"]):
    """그리는 순서(먼 것부터). 목록에 없는 파츠는 깊이가 가장 가까운 아는 파츠 바로 뒤"""
    known = [(depth(e), DRAW_ORDER.index(part_base(name(e)))) for e in entries if part_base(name(e)) in DRAW_ORDER]

    def key(e):
        b = part_base(name(e))
        if b in DRAW_ORDER:
            return (DRAW_ORDER.index(b), 0, 0)
        if not known:
            return (0, 1, -depth(e))
        return (min(known, key=lambda k: abs(k[0] - depth(e)))[1], 1, -depth(e))
    return sorted(entries, key=key)


def layers_to_source(client, info, size):
    """SeeThrough 결과(정사각형으로 패딩·리사이즈된 좌표)를 source.png 좌표로 되돌림.
    [(이름, RGBA 이미지, (left, top), depth_median, 깊이 이미지(L) 또는 None)] — 그리는 순서"""
    w, h = size
    sz = max(w, h)
    px, py = (sz - w) // 2, (sz - h) // 2   # center_square_pad_resize 와 같은 계산
    R = info["width"]

    def to_source(l, key, mode, bg):
        im = Image.open(io.BytesIO(view(client, l[key]))).convert(mode)
        canvas = Image.new(mode, (R, R), bg)
        canvas.paste(im, (l["left"], l["top"]))
        if R != sz:
            if mode == "RGBA":
                canvas = canvas.convert("RGBa").resize((sz, sz), Image.LANCZOS).convert("RGBA")
            else:
                canvas = canvas.resize((sz, sz), Image.LANCZOS)
        return canvas.crop((px, py, px + w, py + h))

    out = []
    for l in info["layers"]:
        canvas = to_source(l, "filename", "RGBA", (0, 0, 0, 0))
        bb = canvas.getchannel("A").point(lambda v: 255 if v > 2 else 0).getbbox()
        if not bb:
            continue
        depth = to_source(l, "depth_filename", "L", 255).crop(bb) if l.get("depth_filename") else None
        out.append((l["name"], canvas.crop(bb), bb[:2], l["depth_median"], depth))
    return draw_order(out, name=lambda t: t[0], depth=lambda t: t[3])


# 이목구비 레이어의 정상 범위 (두 홍채 중심 사이 거리 d 기준, 눈높이 y_e, 양 눈 가운데 x_m).
# SeeThrough 가 가끔 머리카락 덩어리를 nose 등으로 잘못 분류함 (예: 은발 소스의 nose 가 336×350)
#   이름: (최대 너비/d, 최대 높이/d, 정상 위치 상자 (x_m 기준 좌우 /d, y_e 기준 위아래 /d))
FEATURE_LIMITS = {
    "nose": (0.45, 0.45, (-0.35, 0.35, 0.15, 0.95)),
    "mouth": (0.8, 0.4, (-0.6, 0.6, 0.55, 1.45)),
}


def _feature_frame(entries):
    """(x_m, y_e, d) — 두 홍채(없으면 흰자) 중심으로 계산. 못 구하면 None"""
    def center(name):
        e = next((e for e in entries if e["name"] == name), None)
        return ((e["left"] + e["right"]) / 2, (e["top"] + e["bottom"]) / 2) if e else None
    for a, b in (("irides-l", "irides-r"), ("eyewhite-l", "eyewhite-r")):
        ca, cb = center(a), center(b)
        if ca and cb and abs(ca[0] - cb[0]) > 10:
            return (ca[0] + cb[0]) / 2, (ca[1] + cb[1]) / 2, abs(ca[0] - cb[0])
    return None


def layer_issues(entries):
    """비정상적으로 큰 이목구비 레이어 → {이름: 잘라 낼 상자(캔버스 좌표)}"""
    fr = _feature_frame(entries)
    if not fr:
        return {}
    xm, ye, d = fr
    out = {}
    for e in entries:
        lim = FEATURE_LIMITS.get(part_base(e["name"]))
        if not lim:
            continue
        mw, mh, (bx0, bx1, by0, by1) = lim
        if e["right"] - e["left"] > mw * d or e["bottom"] - e["top"] > mh * d:
            out[e["name"]] = (round(xm + bx0 * d), round(ye + by0 * d), round(xm + bx1 * d), round(ye + by1 * d))
    return out


def load_layers(d):
    """저장된 layers.json 을 읽어 [(이름, RGBA, (left, top), depth)] — 그리는 순서.
    비정상적으로 큰 이목구비 레이어는 정상 위치 상자만 남김 (layer_issues)"""
    lj = P.read_json(d / "layers.json")
    if not lj:
        return None
    fix = layer_issues(lj["layers"])
    out = []
    for e in draw_order(lj["layers"]):
        im, pos = Image.open(d / e["file"]).convert("RGBA"), (e["left"], e["top"])
        box = fix.get(e["name"])
        if box:
            x0, y0 = max(box[0], pos[0]), max(box[1], pos[1])
            x1, y1 = min(box[2], pos[0] + im.width), min(box[3], pos[1] + im.height)
            if x1 <= x0 or y1 <= y0:
                continue
            im, pos = im.crop((x0 - pos[0], y0 - pos[1], x1 - pos[0], y1 - pos[1])), (x0, y0)
        out.append((e["name"], im, pos, e["depth_median"]))
    return out


# 목과 상의 깃: 뒷깃·후드 안쪽은 목 뒤에, 앞깃은 목 앞에 있어서 레이어 순서 하나로는 표현할 수 없음.
# SeeThrough 깊이를 픽셀마다 비교해 '목 뒤로 가야 할 상의 부분'을 구하고, 목을 상의 바로 앞에 그리되
# 상의가 있는 곳에서는 그 부분에서만 보이게 마스크를 씌움 (레이어를 새로 나누지 않음)
COLLAR_MARGIN = 3   # 깊이 PNG(0~255) 차이가 이보다 커야 확실한 앞/뒤


def _layer_arrays(d, e, box):
    """레이어 e 의 (알파 0~1, 깊이 0~255) 를 box 영역 배열로"""
    size, off = (box[2] - box[0], box[3] - box[1]), (e["left"] - box[0], e["top"] - box[1])
    a, z = Image.new("L", size, 0), Image.new("L", size, 255)
    a.paste(Image.open(d / e["file"]).convert("RGBA").getchannel("A"), off)
    z.paste(Image.open(d / e["depth_file"]).convert("L"), off)
    return np.asarray(a, np.float32) / 255, np.asarray(z, np.float32)


def collar_mask(d):
    """목 뒤로 가야 할 상의 부분 → (L 이미지, (left, top)) — 목 레이어 영역 기준. 없거나 깊이가 없으면 None.
    판정은 둘 다 확실히 불투명한 안쪽(가장자리에서 4px 이상)에서만 함 (가장자리 깊이는 부정확).
    나머지는 가장 가까운 확실한 판정을 따르고, 확실한 '앞'에서 8px 넘게 떨어지면 '뒤'로 봄
    (뒷깃 윤곽선 같은 얇은 띠가 목 위에 선으로 남지 않게)"""
    from PIL import ImageFilter
    from scipy.ndimage import distance_transform_edt
    lj = P.read_json(d / "layers.json")
    by = {e["name"]: e for e in lj["layers"]} if lj else {}
    ne, te = by.get("neck"), by.get("topwear")
    if not (ne and te and ne.get("depth_file") and te.get("depth_file")):
        return None
    box = (ne["left"], ne["top"], ne["right"], ne["bottom"])
    na, nz = _layer_arrays(d, ne, box)
    ta, tz = _layer_arrays(d, te, box)
    img = lambda x: Image.fromarray(x.astype(np.uint8) * 255)
    opened = lambda x: np.asarray(img(x).filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))) > 0
    inner = np.asarray(img((ta > 0.5) & (na > 0.5)).filter(ImageFilter.MinFilter(9))) > 0
    back = opened(inner & (tz - nz > COLLAR_MARGIN))
    if not back.any():
        return None
    front = opened(inner & (nz - tz > COLLAR_MARGIN))
    b = na > 0.03
    if front.any():
        df = distance_transform_edt(~front)
        b &= (distance_transform_edt(~back) < df) | (df > 8)
    return img(b).filter(ImageFilter.GaussianBlur(1)), box[:2]


_collar_cache = {}  # 폴더 → (layers.json mtime, collar_mask)


def cached_collar(d):
    try:
        mt = (d / "layers.json").stat().st_mtime_ns
    except FileNotFoundError:
        return None
    with _cache_lock:
        hit = _collar_cache.get(str(d))
        if hit and hit[0] == mt:
            return hit[1]
    m = collar_mask(d)
    with _cache_lock:
        _collar_cache[str(d)] = (mt, m)
    return m


def neck_mask(neck, topwear, collar):
    """목 레이어 크기의 마스크(L): 상의가 없는 곳과 목 뒤로 가는 상의 부분에서만 목이 보임.
    neck, topwear: (이름, RGBA, (left, top), …). collar 는 상의 소스의 collar_mask (목 소스와 달라도 됨)"""
    x, y = neck[2]
    box = (x, y, x + neck[1].width, y + neck[1].height)
    ta = _canvas_arr(topwear, box)[..., 3] / 255
    b = Image.new("L", neck[1].size, 0)
    b.paste(collar[0], (collar[1][0] - x, collar[1][1] - y))
    return Image.fromarray(np.round(np.maximum(1 - ta, np.asarray(b) / 255) * 255).astype(np.uint8), "L")


def neck_over_collar(layers, collar):
    """그리는 순서의 [(이름, RGBA, pos, depth)] → 목을 상의 바로 앞으로 옮기고 neck_mask 를 알파에 곱함"""
    names = [l[0] for l in layers]
    if collar is None or "neck" not in names or "topwear" not in names:
        return layers
    neck = layers[names.index("neck")]
    im = neck[1].copy()
    im.putalpha(ImageChops.multiply(im.getchannel("A"), neck_mask(neck, layers[names.index("topwear")], collar)))
    out = [l for l, n in zip(layers, names) if n != "neck"]
    out.insert([l[0] for l in out].index("topwear") + 1, (neck[0], im, neck[2], neck[3]))
    return out


def checker(size, s=16):
    im = Image.new("RGB", size, (205, 205, 205))
    d = ImageDraw.Draw(im)
    for y in range(0, size[1], s):
        for x in range((y // s) % 2 * s, size[0], 2 * s):
            d.rectangle((x, y, x + s - 1, y + s - 1), fill=(240, 240, 240))
    return im


def write_previews(d, source, layers):
    layers = neck_over_collar(layers, collar_mask(d))
    w, h = source.size
    comp = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    for _, im, pos, _ in layers:
        comp.alpha_composite(im, pos)
    check = Image.new("RGB", (w * 2, h), "white")
    check.paste(source, (0, 0))
    check.paste(comp.convert("RGB"), (w, 0))
    check.resize((w, h // 2)).save(d / "check.png")

    tw, th = w // 4, h // 4
    cols = 6
    rows = max(1, (len(layers) + cols - 1) // cols)
    sheet = Image.new("RGB", (cols * tw, rows * th), "white")
    for i, (name, im, pos, _) in enumerate(layers):
        c = checker((w, h)).convert("RGBA")
        c.alpha_composite(im, pos)
        c = c.convert("RGB").resize((tw, th))
        ImageDraw.Draw(c).text((4, 4), name, fill=(220, 0, 0))
        sheet.paste(c, ((i % cols) * tw, (i // cols) * th))
    sheet.save(d / "sheet.png")


def check_seethrough(client):
    if not client.object_info(SEETHROUGH_NODE):
        raise RuntimeError("ComfyUI 에 SeeThrough 노드가 없습니다. ComfyUI-See-through 커스텀 노드를 설치하세요.")


def split_source(client, spec, s, should_stop=None):
    """소스 1개를 레이어로 분리 → parts_out/<id>/layers/. 레이어 수를 반환"""
    d = out_root(spec) / s["id"]
    if not (d / "source.png").exists():
        raise RuntimeError(f"{s['id']}: 소스 이미지가 없습니다. 먼저 소스를 생성하세요.")
    st = spec["seethrough"]
    source = Image.open(d / "source.png").convert("RGB")
    name = client.upload_image(source, f"parts_{s['id']}_source.png")
    prefix = f"npcparts_{s['id']}_{int(time.time())}"
    # 첫 실행은 모델 다운로드·로드 때문에 오래 걸릴 수 있음
    run_graph(client, graph_seethrough(st, name, s["seed"], prefix), timeout=3600, should_stop=should_stop)
    # SavePSD 는 이미지 출력을 history 에 남기지 않음 → 노드가 쓰는 로그 파일에서 정보 파일 이름을 읽음
    info_name = view(client, "seethrough_psd_info.log").decode("utf-8").strip()
    if not info_name.startswith(prefix):
        raise RuntimeError(f"SeeThrough 결과 파일을 찾지 못했습니다 (로그: {info_name!r})")
    info = json.loads(view(client, info_name))
    layers = layers_to_source(client, info, source.size)

    ldir = d / "layers"
    if ldir.exists():
        for f in ldir.glob("*.png"):
            f.unlink()
    ldir.mkdir(exist_ok=True)
    entries = []
    for nm, im, (x, y), dm, depth in layers:
        fn = nm.replace(" ", "_") + ".png"
        im.save(ldir / fn)
        e = {"name": nm, "file": f"layers/{fn}", "left": x, "top": y,
             "right": x + im.width, "bottom": y + im.height, "depth_median": dm}
        if depth is not None:
            depth.save(ldir / (fn[:-4] + "_depth.png"))
            e["depth_file"] = f"layers/{fn[:-4]}_depth.png"
        entries.append(e)
    P.write_json(d / "layers.json", {"id": s["id"], "width": source.width, "height": source.height,
                                     "comfy_info": info_name, "seethrough": st, "layers": entries})
    write_previews(d, source, [t[:4] for t in layers])   # collar_mask 가 layers.json 을 읽으므로 그 뒤에
    return len(entries)


# --------------------------------------------------------------------------- #
# PSD 내보내기. SeeThrough 의 [Download PSD] / [Download Depth PSD] 버튼(브라우저에서 만듦)과 같은 2종.
# 좌표는 source.png 기준, 레이어 순서는 DRAW_ORDER. layer_issues 보정은 하지 않음(원래 레이어 그대로)
# 목은 상의 바로 위에 두고 neck_mask 를 레이어 마스크로 붙임 (Photoshop 에서 고칠 수 있게 픽셀에는 굽지 않음)
# --------------------------------------------------------------------------- #
def psd_bytes(size, layers, gray=False):
    """8bit PSD (압축 없음). layers: [(이름, 이미지, (left, top), 숨김[, 레이어 마스크 L])] — 아래 레이어부터.
    gray=True 면 회색조(이미지는 LA), 아니면 RGB(이미지는 RGBA). 합쳐진 이미지는 흰 바탕에 겹친 것.
    레이어 마스크는 레이어와 같은 크기·위치"""
    w, h = size
    mode, ids = ("LA", [-1, 0]) if gray else ("RGBA", [-1, 0, 1, 2])
    comp = Image.new("RGBA", size, (255, 255, 255, 255))
    recs, chans, n = [], [], 0
    for name, im, (x, y), hidden, *mask in layers:
        im, mask = im.convert(mode), (mask[0] if mask else None)
        if not im.width or not im.height:
            continue
        if not hidden:
            vis = im.convert("RGBA")
            if mask:
                vis.putalpha(ImageChops.multiply(vis.getchannel("A"), mask))
            comp.alpha_composite(vis, (x, y))
        bands = im.split()
        planes = [bands[-1]] + list(bands[:-1])   # ids 순서: 투명도(-1), 색 채널[, 레이어 마스크(-2)]
        cids = ids + [-2] if mask else ids
        if mask:
            planes.append(mask)
        nm = name.encode("ascii", "replace")[:255]
        pascal = bytes([len(nm)]) + nm
        pascal += b"\0" * (-len(pascal) % 4)
        # 레이어 마스크(사각형 = 레이어, 바깥 색 0, 플래그 0) 또는 없음, 블렌딩 범위 없음, 이름
        mdata = struct.pack(">I4iBB2x", 20, y, x, y + im.height, x + im.width, 0, 0) if mask else struct.pack(">I", 0)
        extra = mdata + struct.pack(">I", 0) + pascal
        recs.append(struct.pack(">4iH", y, x, y + im.height, x + im.width, len(cids))
                    + b"".join(struct.pack(">hI", c, 2 + im.width * im.height) for c in cids)
                    + b"8BIMnorm" + struct.pack(">BBBB", 255, 0, 0x02 if hidden else 0, 0)
                    + struct.pack(">I", len(extra)) + extra)
        chans += [b"\0\0" + p.tobytes() for p in planes]   # 압축 0 (raw)
        n += 1
    info = struct.pack(">h", n) + b"".join(recs) + b"".join(chans)
    info += b"\0" * (len(info) % 2)
    lmi = struct.pack(">I", len(info)) + info + struct.pack(">I", 0)
    merged = comp.convert("L" if gray else "RGB")
    return (b"8BPS" + struct.pack(">H6xHIIHH", 1, len(ids) - 1, h, w, 8, 1 if gray else 3)
            + struct.pack(">II", 0, 0)                       # 색상표, 이미지 리소스 없음
            + struct.pack(">I", len(lmi)) + lmi
            + b"\0\0" + b"".join(b.tobytes() for b in merged.split()))


def fetch_depth_layers(client, d):
    """깊이 레이어를 저장하기 전에 분리한 소스: ComfyUI output 에 남은 원본에서 깊이 레이어를 받아 저장"""
    lj = P.read_json(d / "layers.json")
    if not lj.get("comfy_info"):
        raise RuntimeError("깊이 레이어가 없습니다. 레이어를 다시 분리하세요.")
    try:
        info = json.loads(view(client, lj["comfy_info"]))
    except RuntimeError:   # 404: ComfyUI output 에서 지워짐
        raise RuntimeError("ComfyUI output 폴더에 SeeThrough 원본이 없어 깊이 레이어를 만들 수 없습니다. "
                           "레이어를 다시 분리하세요.") from None
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        raise P.ComfyConnectionError("깊이 레이어를 받으려면 ComfyUI 가 켜져 있어야 합니다 "
                                     "(깊이 레이어를 저장하기 전에 분리한 소스)") from None
    got = {t[0]: t for t in layers_to_source(client, info, (lj["width"], lj["height"]))}
    for e in lj["layers"]:
        t = got.get(e["name"])
        if not t or t[4] is None:
            continue
        if t[2] != (e["left"], e["top"]) or t[4].size != (e["right"] - e["left"], e["bottom"] - e["top"]):
            raise RuntimeError(f"{e['name']}: 저장된 레이어와 ComfyUI 원본의 위치가 다릅니다. 레이어를 다시 분리하세요.")
        e["depth_file"] = e["file"][:-4] + "_depth.png"
        t[4].save(d / e["depth_file"])
    P.write_json(d / "layers.json", lj)
    return lj


def make_psd(d, depth=False, client=None):
    """parts_out/<id> 의 레이어 → PSD bytes. depth=True 면 깊이 PSD(회색조, 밝을수록 멂).
    예전에 분리해서 깊이 레이어가 없으면 client 로 ComfyUI 에서 받아 옴 (목/깃 마스크에도 필요)"""
    lj = P.read_json(d / "layers.json")
    if not lj:
        raise FileNotFoundError(f"{d.name}: 레이어를 아직 분리하지 않았습니다")
    size = (lj["width"], lj["height"])
    if not any(e.get("depth_file") for e in lj["layers"]):
        try:
            if client is None:
                raise RuntimeError("깊이 레이어가 없습니다. 레이어를 다시 분리하세요.")
            lj = fetch_depth_layers(client, d)
        except (RuntimeError, P.ComfyConnectionError):
            if depth:
                raise
            # 레이어 PSD 는 깊이 없이도 만들 수 있음 (목/깃 마스크만 빠짐)
    order = draw_order(lj["layers"])
    rgba = lambda e: Image.open(d / e["file"]).convert("RGBA")
    masks = {}
    collar = collar_mask(d)
    by = {e["name"]: e for e in order}
    if collar and "neck" in by and "topwear" in by:
        ne, te = by["neck"], by["topwear"]
        masks["neck"] = neck_mask(("neck", rgba(ne), (ne["left"], ne["top"])),
                                  ("topwear", rgba(te), (te["left"], te["top"])), collar)
        order.remove(ne)
        order.insert(order.index(te) + 1, ne)
    if not depth:
        layers = [("source", Image.open(d / "source.png"), (0, 0), True)]
        layers += [(e["name"], rgba(e), (e["left"], e["top"]), False, masks.get(e["name"])) for e in order]
        return psd_bytes(size, layers)
    layers = []
    for e in order:
        if not e.get("depth_file"):
            continue
        im = Image.merge("LA", (Image.open(d / e["depth_file"]).convert("L"), rgba(e).getchannel("A")))
        layers.append((e["name"], im, (e["left"], e["top"]), False, masks.get(e["name"])))
    return psd_bytes(size, layers, gray=True)


# --------------------------------------------------------------------------- #
# 조합 (CPU). 소스들은 gen 단계에서 얼굴 위치를 맞춰 두었으므로 좌표 변환 없이 겹침
# --------------------------------------------------------------------------- #
# 귀는 머리 그룹: 귀가 보이는지 여부가 머리 모양에 달려 있음
# (얼굴 소스의 귀를 쓰면 다른 머리의 옆머리 위로 귀가 튀어나옴)
GROUPS = {
    "skin": ["face", "neck", "nose", "mouth"],
    "eyes": ["eyewhite", "irides", "eyelash", "eyebrow"],
    "hair": ["back hair", "front hair", "headwear", "ears"],
    "outfit": ["topwear", "handwear", "bottomwear", "neckwear", "legwear"],
    "glasses": ["eyewear"],
}
GROUP_LABEL = {"skin": "얼굴", "eyes": "눈", "hair": "머리", "outfit": "옷", "glasses": "안경"}

_layer_cache = {}   # 폴더 → (layers.json mtime, 레이어)
_cache_lock = threading.Lock()


def group_of(name):
    b = part_base(name)
    return next((g for g, names in GROUPS.items() if b in names), None)


def cached_layers(d):
    p = d / "layers.json"
    try:
        mt = p.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    with _cache_lock:
        hit = _layer_cache.get(str(d))
        if hit and hit[0] == mt:
            return hit[1]
    layers = load_layers(d)
    with _cache_lock:
        _layer_cache[str(d)] = (mt, layers)
    return layers


def _canvas_arr(layer, box):
    """(이름, RGBA, pos, 깊이) 를 box(캔버스 좌표) 영역의 float 배열 (H, W, 4) 로"""
    c = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
    c.paste(layer[1], (layer[2][0] - box[0], layer[2][1] - box[1]))
    return np.asarray(c, dtype=np.float32)


def _shading(arr, interior):
    """얼굴 레이어의 음영 비율 (1 = 기본 피부색, 그림자일수록 작음). 기본 피부 밝기는 밝은 쪽 분위수"""
    y = arr[..., 0] * .299 + arr[..., 1] * .587 + arr[..., 2] * .114
    vals = y[interior]
    base = np.percentile(vals, 85) if vals.size else 255.0
    return np.where(interior, np.clip(y / max(base, 1.0), 0, 1), 1.0)


def adapt_face(face, hair_face, top_frac=0.6, ramp=40):
    """얼굴 소스의 face 레이어를 머리 소스에 맞게 고침.
    - 음영: SeeThrough 의 face 레이어에는 원래 앞머리가 드리운 그림자가 남아 있어서, 다른 머리를 얹으면
      예전 앞머리 모양이 비쳐 보임 → 얼굴 소스의 음영을 걷어 내고 머리 소스 얼굴의 음영을 입힘
    - 윗부분 윤곽: 머리 모양은 머리 소스의 얼굴 윤곽에 맞춰 그려져 있음. 얼굴이 더 넓으면 뒷머리(옆머리)를
      덮어 피부색 띠가 생김 → 눈높이 위쪽은 머리 소스 얼굴 윤곽 안으로 자름 (턱선은 얼굴 소스 그대로)
    반환: (RGBA, (left, top)) — 두 얼굴을 합친 영역만 계산 (전체 캔버스로 하면 조합 1장에 1초 가까이 걸림)"""
    from PIL import ImageFilter
    box = (min(face[2][0], hair_face[2][0]), min(face[2][1], hair_face[2][1]),
           max(face[2][0] + face[1].width, hair_face[2][0] + hair_face[1].width),
           max(face[2][1] + face[1].height, hair_face[2][1] + hair_face[1].height))
    size = (box[2] - box[0], box[3] - box[1])
    F, G = _canvas_arr(face, box), _canvas_arr(hair_face, box)
    erode = lambda a: np.asarray(Image.fromarray((a > 250).astype(np.uint8) * 255).filter(ImageFilter.MinFilter(17))) > 0
    in_f, in_g = erode(F[..., 3]), erode(G[..., 3])
    s_f, s_g = _shading(F, in_f), _shading(G, in_g)
    # 윤곽선처럼 아주 어두운 곳(비율 < 0.45)은 음영이 아니라 선이므로 그대로 둠
    keep = (s_f < 0.45) | ~in_f
    albedo = np.where(keep[..., None], F[..., :3], F[..., :3] / np.maximum(s_f, 0.45)[..., None])
    rgb = np.where(keep[..., None], F[..., :3], albedo * np.where(in_g, s_g, 1.0)[..., None])

    # 윗부분 윤곽 자르기 (눈높이 근처에서 서서히)
    ys = np.nonzero(G[..., 3].max(axis=1) > 0)[0]
    a = F[..., 3]
    if ys.size:
        cut = ys[0] + top_frac * (ys[-1] - ys[0])
        w = np.clip((cut - np.arange(size[1], dtype=np.float32)) / ramp + 0.5, 0, 1)[:, None]   # 위쪽 1 → 아래쪽 0
        g_alpha = np.asarray(Image.fromarray(G[..., 3].astype(np.uint8)).filter(ImageFilter.MaxFilter(5)), dtype=np.float32)
        a = a * (1 - w) + np.minimum(a, g_alpha) * w
    out = np.dstack([np.clip(rgb, 0, 255), a]).astype(np.uint8)
    return Image.fromarray(out, "RGBA"), box[:2]


_face_cache = {}    # (얼굴 소스, 머리 소스, mtime들) → 고친 face 레이어


def compose(root, picks, size=None):
    """picks: {그룹: 소스 id 또는 None} → RGBA 이미지 (배경 투명).
    얼굴과 머리 소스가 다르면 face 레이어를 머리 소스에 맞게 고치고(adapt_face),
    머리 소스의 얼굴을 밑에 깔아 윤곽 차이로 생기는 틈을 메움"""
    chosen = []
    for g, sid in picks.items():
        if g not in GROUPS:
            raise ValueError(f"알 수 없는 그룹: {g}")
        if not sid:
            continue
        layers = cached_layers(root / sid)
        if layers is None:
            raise FileNotFoundError(f"{sid}: 분리된 레이어가 없습니다")
        chosen += [l for l in layers if group_of(l[0]) == g]
        if size is None:
            lj = P.read_json(root / sid / "layers.json")
            size = (lj["width"], lj["height"])
    size = size or (832, 1216)
    skin, hair = picks.get("skin"), picks.get("hair")
    if skin and hair and skin != hair:
        face = next((l for l in chosen if l[0] == "face"), None)
        hair_face = next((l for l in cached_layers(root / hair) if l[0] == "face"), None)
        if face and hair_face:
            key = (skin, hair, (root / skin / "layers.json").stat().st_mtime_ns,
                   (root / hair / "layers.json").stat().st_mtime_ns, size)
            with _cache_lock:
                fixed = _face_cache.get(key)
            if fixed is None:
                fixed = adapt_face(face, hair_face)
                with _cache_lock:
                    if len(_face_cache) > 64:
                        _face_cache.clear()
                    _face_cache[key] = fixed
            i = chosen.index(face)
            # 같은 이름은 넣은 순서대로 그려짐 (정렬이 안정적) → 머리 소스 얼굴이 밑, 고친 얼굴이 위
            chosen[i:i + 1] = [hair_face, ("face", fixed[0], fixed[1], face[3])]
    chosen = draw_order(chosen, name=lambda t: t[0], depth=lambda t: t[3])
    # 목/깃 마스크는 상의 소스에서 구함 (목이 다른 소스여도 얼굴 위치가 같으므로 그대로 씀)
    outfit = picks.get("outfit")
    chosen = neck_over_collar(chosen, cached_collar(root / outfit) if outfit else None)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    for _, im, pos, _ in chosen:
        canvas.alpha_composite(im, pos)
    return canvas


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_gen(client, cfg, spec, sources, force):
    root = out_root(spec)
    todo = [s for s in sources if force or not (root / s["id"] / "source.png").exists()]
    print(f"[1단계] 소스 생성: {len(todo)}개 (건너뜀 {len(sources) - len(todo)}개)")
    if not todo:
        return
    if any(not s.get("from") for s in todo):
        free_vram(client)
    for i, s in enumerate(todo, 1):
        t0 = time.time()
        if s.get("from"):   # 생성 이미지에서 가져온 소스는 새로 그리지 않고 다시 가져옴
            import_source(cfg, spec, s, P.output_root(cfg) / s["from"], warn=P._warn)
        else:
            gen_source(client, cfg, spec, s, warn=P._warn)
        print(f"  ({i}/{len(todo)}) {s['id']} 완료 {time.time() - t0:.0f}s")


def cmd_split(client, spec, sources, force):
    root = out_root(spec)
    check_seethrough(client)
    ready = [s for s in sources if (root / s["id"] / "source.png").exists()]
    missing = [s["id"] for s in sources if s not in ready]
    todo = [s for s in ready if force or not (root / s["id"] / "layers.json").exists()]
    print(f"[2단계] 레이어 분리: {len(todo)}개 (건너뜀 {len(ready) - len(todo)}개)")
    if missing:
        print(f"  소스가 없어 제외: {missing} → 먼저 gen 을 실행하세요")
    if not todo:
        return
    free_vram(client)
    for i, s in enumerate(todo, 1):
        t0 = time.time()
        n = split_source(client, spec, s)
        print(f"  ({i}/{len(todo)}) {s['id']} 완료: 레이어 {n}개, {time.time() - t0:.0f}s")


def cmd_preview(spec, sources):
    """GPU 없이 저장된 레이어로 check.png / sheet.png 만 다시 그림"""
    root = out_root(spec)
    for s in sources:
        d = root / s["id"]
        layers = load_layers(d)
        if layers is None:
            continue
        write_previews(d, Image.open(d / "source.png").convert("RGB"), layers)
        print(f"  {s['id']} 미리보기 갱신")


def cmd_psd(spec, sources, client):
    """parts_out/<id>/<id>.psd, <id>_depth.psd"""
    root = out_root(spec)
    for s in sources:
        d = root / s["id"]
        if not (d / "layers.json").exists():
            continue
        (d / f"{s['id']}.psd").write_bytes(make_psd(d))
        (d / f"{s['id']}_depth.psd").write_bytes(make_psd(d, depth=True, client=client))
        print(f"  {s['id']} → {d / (s['id'] + '.psd')}, {s['id']}_depth.psd")


def cmd_status(spec):
    root = out_root(spec)
    print(f"{'id':<8}{'소스':<6}{'레이어':<8}태그")
    for s in spec["sources"]:
        st = source_state(root, s)
        lay = str(st["layers"]) if st["layers"] is not None else "-"
        print(f"{s['id']:<8}{'O' if st['source'] else '-':<6}{lay:<8}{s['tags']}")


def main():
    ap = argparse.ArgumentParser(description="파츠 소스 생성 / SeeThrough 레이어 분리")
    ap.add_argument("cmd", choices=["gen", "split", "preview", "psd", "status"])
    ap.add_argument("ids", nargs="*", help="처리할 소스 id (생략하면 전부)")
    ap.add_argument("--force", action="store_true", help="결과가 있어도 다시 만들기")
    ap.add_argument("--spec", default=str(ROOT / "parts.json"))
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    a = ap.parse_args()

    spec = load_spec(a.spec)
    if a.cmd == "status":
        return cmd_status(spec)
    if a.cmd == "preview":
        return cmd_preview(spec, pick(spec, a.ids))
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    client = P.ComfyClient(cfg.get("comfy_url", "http://127.0.0.1:8000"))
    if a.cmd == "psd":   # ComfyUI 는 깊이 레이어가 없는 예전 결과에만 필요
        return cmd_psd(spec, pick(spec, a.ids), client)
    client.check()
    sources = pick(spec, a.ids)
    if a.cmd == "gen":
        cmd_gen(client, cfg, spec, sources, a.force)
    else:
        cmd_split(client, spec, sources, a.force)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단했습니다.")
    except (P.ComfyConnectionError, P.ComfyValidationError, SpecError, RuntimeError) as e:
        sys.exit(str(e))
