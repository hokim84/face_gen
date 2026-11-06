#!/usr/bin/env python3
"""
NPC 페이스 스프라이트 자동 생성 파이프라인 (ComfyUI HTTP API 사용)

흐름 (캐릭터 1명당):
  1) characters.csv 한 줄 -> 프롬프트 조립 -> ComfyUI txt2img 로 상반신 base 생성
     (선택: ControlNet 포즈 템플릿으로 자세·구도 고정, config controlnet)
     (머리가 위쪽 가장자리에 닿으면 그림을 아래로 내리고 위쪽을 아웃페인트해 여백 확보)
  2) 얼굴 검출 (YOLO, Bingsu/adetailer face_yolov8m)
  3) 표정마다: 얼굴 주변을 잘라 확대 -> 얼굴만 마스크 인페인트(같은 seed) -> 원래 크기로 합성
  4) 표정 오버레이 = 마스크 영역만 잘라낸 투명 PNG + 좌표(meta.json)
  5) (선택) 배경 제거 -> Unity 에서 base 위에 오버레이 한 장만 덮어 표정 전환

사용 예:
  python pipeline.py                      # characters.csv 전체
  python pipeline.py --only npc_0001      # 특정 캐릭터만
  python pipeline.py --base-only          # base 만 먼저 뽑아 검수
  python pipeline.py --force              # 이미 만든 것도 다시
  python pipeline.py --export-workflows   # ComfyUI 에서 열어볼 워크플로우 JSON 저장
  python pipeline.py --make-pose npc_0001 # 이 base 의 자세를 ControlNet 포즈 템플릿(poses/)으로 저장
"""
import argparse
import csv
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace
from image_types import image_type, effective_config, fingerprint, prompt_profiles

from PIL import Image, ImageDraw, ImageFilter, ImageChops

ROOT = Path(__file__).resolve().parent


class Cancelled(Exception):
    """사용자가 작업을 취소함"""


class ComfyConnectionError(RuntimeError):
    """ComfyUI 에 연결할 수 없음"""


class ComfyValidationError(RuntimeError):
    """ComfyUI 가 워크플로우를 거부함 (없는 모델 이름 등 → 설정 문제라 모든 캐릭터에서 똑같이 실패)"""


class ConfigError(ComfyValidationError):
    """보내기 전에 알 수 있는 설정 문제 (포즈 템플릿 파일 없음 등). 처리는 ComfyValidationError 와 같음"""


def describe_prompt_error(body):
    """/prompt 400 응답(JSON)을 읽기 쉬운 한국어 메시지로. 해석할 수 없으면 None"""
    try:
        res = json.loads(body)
    except ValueError:
        return None
    if not isinstance(res, dict) or "error" not in res:
        return None
    lines = []
    for node in (res.get("node_errors") or {}).values():
        ct = node.get("class_type", "?")
        for err in node.get("errors", []):
            info = err.get("extra_info") or {}
            name = info.get("input_name", "")
            if err.get("type") == "value_not_in_list":
                cfg = info.get("input_config") or [[]]
                choices = cfg[0] if cfg and isinstance(cfg[0], list) else []
                if not choices and len(cfg) > 1 and isinstance(cfg[1], dict):  # 신형 ["COMBO", {"options": [...]}]
                    choices = cfg[1].get("options", [])
                msg = f"{ct}.{name}: '{info.get('received_value')}' 이(가) ComfyUI 에 없습니다"
                if choices:
                    shown = ", ".join(map(str, choices[:8])) + (" …" if len(choices) > 8 else "")
                    msg += f" (설치된 것: {shown})"
                lines.append(msg)
            else:
                detail = err.get("details") or ""
                lines.append(f"{ct}{'.' + name if name else ''}: {err.get('message', '')}" + (f" — {detail}" if detail else ""))
    if not lines:
        e = res["error"]
        e = e if isinstance(e, dict) else {"message": str(e)}
        lines.append(e.get("message", "") + (f" — {e['details']}" if e.get("details") else ""))
    return "ComfyUI 가 워크플로우를 거부했습니다. 설정을 확인하세요.\n  " + "\n  ".join(lines)


# --------------------------------------------------------------------------- #
# ComfyUI 클라이언트
# --------------------------------------------------------------------------- #
class ComfyClient:
    def __init__(self, url):
        self.base = url.rstrip("/")
        self.client_id = uuid.uuid4().hex

    def _req(self, path, data=None, headers=None, timeout=600):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if path == "/prompt" and e.code == 400:
                msg = describe_prompt_error(body)
                if msg:
                    raise ComfyValidationError(msg) from None
            raise RuntimeError(f"ComfyUI 오류 {e.code} ({path}):\n{body}") from None

    def _post_json(self, path, obj, timeout=10):
        return self._req(path, json.dumps(obj).encode(), {"Content-Type": "application/json"}, timeout)

    def check(self):
        """연결 확인. 실패하면 ComfyConnectionError"""
        try:
            return json.loads(self._req("/system_stats", timeout=5))
        except Exception as e:
            raise ComfyConnectionError(
                f"ComfyUI 에 연결할 수 없습니다 ({self.base}). ComfyUI 를 먼저 실행하세요.\n    {e}") from None

    def object_info(self, node_class):
        return json.loads(self._req(f"/object_info/{node_class}", timeout=10)).get(node_class, {})

    def interrupt(self, prompt_id=None):
        """실행 중인 프롬프트 중단 + 대기열에서 제거 (실패해도 무시)"""
        try:
            self._post_json("/interrupt", {"prompt_id": prompt_id} if prompt_id else {})
        except Exception:
            pass
        if prompt_id:
            try:
                self._post_json("/queue", {"delete": [prompt_id]})
            except Exception:
                pass

    def upload_image(self, img, name):
        buf = io.BytesIO()
        img.save(buf, "PNG")
        b = uuid.uuid4().hex
        body = (
            f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode() + buf.getvalue() + (
            f'\r\n--{b}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n--{b}--\r\n'
        ).encode()
        res = json.loads(self._req("/upload/image", body, {"Content-Type": f"multipart/form-data; boundary={b}"}))
        return f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]

    def run(self, graph, timeout=900, should_stop=None):
        """워크플로우 실행 후 첫 번째 출력 이미지를 PIL 로 반환.
        should_stop() 이 True 가 되면 ComfyUI 를 중단시키고 Cancelled 를 던짐"""
        payload = json.dumps({"prompt": graph, "client_id": self.client_id}).encode()
        res = json.loads(self._req("/prompt", payload, {"Content-Type": "application/json"}))
        pid = res["prompt_id"]
        t0 = time.time()
        while True:
            if should_stop and should_stop():
                self.interrupt(pid)
                raise Cancelled()
            hist = json.loads(self._req(f"/history/{pid}"))
            if pid in hist:
                entry = hist[pid]
                st = entry.get("status", {})
                if st.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI 실행 실패: {json.dumps(st.get('messages', []), ensure_ascii=False)[:2000]}")
                for out in entry.get("outputs", {}).values():
                    for im in out.get("images", []):
                        q = urllib.parse.urlencode(
                            {"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": im.get("type", "output")}
                        )
                        return Image.open(io.BytesIO(self._req("/view?" + q))).convert("RGB")
                if st.get("completed"):
                    raise RuntimeError("실행은 끝났지만 출력 이미지가 없습니다.")
            if time.time() - t0 > timeout:
                raise TimeoutError(f"ComfyUI 응답 시간 초과 ({timeout}s)")
            time.sleep(0.4)


# --------------------------------------------------------------------------- #
# 워크플로우 그래프 (ComfyUI 기본 노드만 사용 → 커스텀 노드 설치 불필요)
# --------------------------------------------------------------------------- #
class Graph:
    def __init__(self):
        self.nodes, self.n = {}, 0

    def add(self, cls, **inputs):
        self.n += 1
        k = str(self.n)
        self.nodes[k] = {"class_type": cls, "inputs": inputs}
        return k


def _model(g, cfg):
    ck = g.add("CheckpointLoaderSimple", ckpt_name=cfg["checkpoint"])
    model, clip, vae = [ck, 0], [ck, 1], [ck, 2]
    for l in cfg.get("loras", []):
        s = l.get("strength", 0.7)
        k = g.add("LoraLoader", model=model, clip=clip, lora_name=l["name"],
                  strength_model=s, strength_clip=l.get("strength_clip", s))
        model, clip = [k, 0], [k, 1]
    cs = g.add("CLIPSetLastLayer", clip=clip, stop_at_clip_layer=-int(cfg.get("clip_skip", 2)))
    if (cfg.get("vae") or "").strip():
        # 체크포인트에 든 VAE 가 망가진 경우가 있음 (인코드 → 디코드만 해도 그림이 뿌옇게 바뀜 →
        # 인페인트/아웃페인트 영역이 회색 막처럼 보임). 그럴 때 표준 SDXL VAE 를 따로 지정
        vae = [g.add("VAELoader", vae_name=cfg["vae"].strip()), 0]
    return model, [cs, 0], vae


def _apply_controlnet(g, cfg, pos, neg, pose_image):
    """포즈 템플릿(OpenPose 뼈대 그림)으로 어깨선·머리 위치·얼굴 방향을 고정.
    초반 스텝에만 약하게 걸어 머리 모양·옷 같은 세부는 모델에 맡긴다"""
    cc = controlnet_cfg(cfg)
    cn = [g.add("ControlNetLoader", control_net_name=cc["model"]), 0]
    if cc["union_type"]:  # union(promax) 모델은 종류를 지정. 단일 용도 모델이면 "" (노드 생략)
        cn = [g.add("SetUnionControlNetType", control_net=cn, type=cc["union_type"]), 0]
    li = g.add("LoadImage", image=pose_image)
    a = g.add("ControlNetApplyAdvanced", positive=pos, negative=neg, control_net=cn, image=[li, 0],
              strength=cc["strength"], start_percent=cc["start"], end_percent=cc["end"])
    return [a, 0], [a, 1]


def graph_txt2img(cfg, pos, neg, seed, pose_image=None):
    """pose_image: ComfyUI 에 올린 포즈 템플릿 이름. 주면 ControlNet 으로 자세를 고정"""
    g = Graph()
    model, clip, vae = _model(g, cfg)
    p = g.add("CLIPTextEncode", text=pos, clip=clip)
    n = g.add("CLIPTextEncode", text=neg, clip=clip)
    pc, nc = [p, 0], [n, 0]
    if pose_image:
        pc, nc = _apply_controlnet(g, cfg, pc, nc, pose_image)
    lat = g.add("EmptyLatentImage", width=cfg["width"], height=cfg["height"], batch_size=1)
    s = g.add("KSampler", model=model, seed=seed, steps=cfg["steps"], cfg=cfg["cfg"],
              sampler_name=cfg["sampler"], scheduler=cfg["scheduler"],
              positive=pc, negative=nc, latent_image=[lat, 0], denoise=1.0)
    d = g.add("VAEDecode", samples=[s, 0], vae=vae)
    g.add("SaveImage", images=[d, 0], filename_prefix="npc_face/base")
    return g.nodes


def graph_pose_extract(image_name, resolution):
    """그림에서 OpenPose 뼈대 추출 (포즈 템플릿 만들 때 한 번만 씀).
    이 그래프만 커스텀 노드(comfyui_controlnet_aux 의 DWPreprocessor)가 필요하다"""
    g = Graph()
    li = g.add("LoadImage", image=image_name)
    # 손은 상반신 구도에서 자주 잘리고 위치가 제각각이라 고정하지 않음. 얼굴 점은 얼굴 방향을 잡아 줌.
    # 인물 검출(bbox_detector)은 끄고 그림 전체를 한 사람으로 봄: yolox_l 은 이 환경에서 애니 그림(전신 포함)의
    # 사람을 하나도 못 찾아 빈 뼈대가 나옴. 우리 base 는 항상 solo 라 전체 그림으로 충분
    dw = g.add("DWPreprocessor", image=[li, 0], detect_hand="disable", detect_body="enable", detect_face="enable",
               resolution=resolution, bbox_detector="None",
               pose_estimator="dw-ll_ucoco_384_bs5.torchscript.pt",
               scale_stick_for_xinsr_cn="enable")  # xinsir(union promax) 은 굵은 뼈대로 학습됨
    g.add("SaveImage", images=[dw, 0], filename_prefix="npc_face/pose")
    return g.nodes


def graph_inpaint(cfg, image_name, mask_name, pos, neg, seed, denoise, steps=None, prefix="npc_face/expr",
                  differential=False):
    """differential: 마스크 값(0~1)을 픽셀별 denoise 세기로 씀 (DifferentialDiffusion, 기본 노드).
    부드러운 마스크 경계에서 이음매 없이 이어 그릴 때 사용"""
    g = Graph()
    model, clip, vae = _model(g, cfg)
    if differential:
        model = [g.add("DifferentialDiffusion", model=model), 0]
    li = g.add("LoadImage", image=image_name)
    lm = g.add("LoadImageMask", image=mask_name, channel="red")
    enc = g.add("VAEEncode", pixels=[li, 0], vae=vae)
    nm = g.add("SetLatentNoiseMask", samples=[enc, 0], mask=[lm, 0])
    p = g.add("CLIPTextEncode", text=pos, clip=clip)
    n = g.add("CLIPTextEncode", text=neg, clip=clip)
    s = g.add("KSampler", model=model, seed=seed, steps=steps or cfg["expression"]["steps"], cfg=cfg["cfg"],
              sampler_name=cfg["sampler"], scheduler=cfg["scheduler"],
              positive=[p, 0], negative=[n, 0], latent_image=[nm, 0], denoise=denoise)
    d = g.add("VAEDecode", samples=[s, 0], vae=vae)
    g.add("SaveImage", images=[d, 0], filename_prefix=prefix)
    return g.nodes


# --------------------------------------------------------------------------- #
# 얼굴 검출
# --------------------------------------------------------------------------- #
_yolo = None


def _warn(msg):
    print(f"    [경고] {msg}")


def detect_face(img, cfg, warn=_warn):
    """(x0, y0, x1, y1) 반환. 검출 실패 시 상반신 구도 기준 추정 박스."""
    global _yolo
    try:
        if _yolo is None:
            from huggingface_hub import hf_hub_download
            from ultralytics import YOLO
            fd = cfg["face_detector"]
            _yolo = YOLO(hf_hub_download(fd["repo"], fd["file"]))
        r = _yolo(img, conf=cfg["face_detector"].get("conf", 0.3), verbose=False)[0]
        boxes = r.boxes.xyxy.cpu().numpy().tolist()
        if boxes:
            return tuple(max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])))
        warn("얼굴 검출 실패 → 추정 위치 사용")
    except ImportError:
        warn("ultralytics 미설치 → 추정 얼굴 위치 사용 (pip install ultralytics huggingface_hub)")
    except Exception as e:
        warn(f"얼굴 검출 오류({e}) → 추정 위치 사용")
    W, H = img.size
    return (W * 0.32, H * 0.10, W * 0.68, H * 0.38)


# --------------------------------------------------------------------------- #
# 프롬프트 / 표정
# --------------------------------------------------------------------------- #
def load_characters(path):
    for enc in ("utf-8-sig", "cp949"):  # 엑셀 한글 CSV 대응
        try:
            with open(path, encoding=enc, newline="") as f:
                return [r for r in csv.DictReader(f) if (r.get("id") or "").strip()]
        except UnicodeDecodeError:
            continue
    raise ValueError(f"CSV 인코딩을 읽을 수 없습니다: {path}")


def build_prompt(cfg, row, expr_tags, closeup=False):
    default = prompt_profiles(cfg)[image_type(row)]["positive"]
    if image_type(row) == "monster":
        return ", ".join(v for v in (cfg.get("quality_prefix", ""), default, row.get("prompt", ""),
                                    cfg.get("background", ""), cfg.get("style_suffix", "")) if v)
    parts = [cfg["quality_prefix"], default, (row.get("subject") or "1girl").strip(), "solo",
             cfg["closeup_framing"] if closeup else cfg["framing"]]
    for col in ("age", "hair", "eyes", "outfit", "extra"):
        v = (row.get(col) or "").strip()
        if v:
            parts.append(v)
    parts.append(expr_tags)
    if not closeup:
        parts.append(cfg["background"])
    if cfg.get("style_suffix"):
        parts.append(cfg["style_suffix"])
    return ", ".join(p for p in parts if p)


def build_negative(cfg, row):
    return ", ".join(v for v in (cfg.get("negative", ""), prompt_profiles(cfg)[image_type(row)]["negative"]) if v)


def expression_list(row, ex, warn=_warn):
    if image_type(row) != "character":
        return []
    custom = (row.get("expressions") or "").strip()
    if custom:
        names = [x.strip() for x in custom.split("|") if x.strip()]
    else:
        names = []
        pers = (row.get("personality") or "").strip() or ex["default_personality"]
        for p in pers.split("|"):
            p = p.strip()
            if p not in ex["personalities"]:
                warn(f"성격 '{p}' 프리셋 없음 → '{ex['default_personality']}' 사용")
                p = ex["default_personality"]
            names += [n for n in ex["personalities"][p] if n not in names]
    if "neutral" not in names:
        names.insert(0, "neutral")
    return names


def expression_spec(name, ex, cfg, warn=_warn):
    v = ex["expressions"].get(name)
    if v is None:
        warn(f"표정 '{name}' 정의 없음 → 이름을 그대로 태그로 사용")
        v = name
    if isinstance(v, str):
        return v, cfg["expression"]["denoise"]
    return v["tags"], v.get("denoise", cfg["expression"]["denoise"])


def seed_of(row):
    s = (row.get("seed") or "").strip()
    if s:
        return int(s)
    return int(hashlib.md5(row["id"].encode()).hexdigest()[:8], 16)


# --------------------------------------------------------------------------- #
# 이미지 처리
# --------------------------------------------------------------------------- #
def face_regions(size, face, ecfg):
    """확대 크롭 영역(정사각형)과 마스크 타원(전체 이미지 좌표) 계산"""
    W, H = size
    x0, y0, x1, y1 = face
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    fw, fh = x1 - x0, y1 - y0
    side = int(min(max(fw, fh) * ecfg["crop_scale"], W, H))
    cx0 = int(min(max(cx - side / 2, 0), W - side))
    cy0 = int(min(max(cy - side / 2, 0), H - side))
    m = ecfg["mask_expand"]
    ell = (cx - fw * m / 2, cy - fh * m / 2, cx + fw * m / 2, cy + fh * m / 2)
    return (cx0, cy0, cx0 + side, cy0 + side), ell


def make_masks(size, ell, crop, ecfg):
    """(전체 이미지용 페더 마스크, 크롭용 인페인트 마스크) 반환"""
    W, H = size
    hard = Image.new("L", (W, H), 0)
    ImageDraw.Draw(hard).ellipse(ell, fill=255)
    fpx = max(2, int((ell[2] - ell[0]) * ecfg["feather"]))
    # 페더: 타원을 안쪽으로 줄여 그린 뒤 블러 → 인페인트 영역 안쪽에서 경계가 부드럽게 사라짐
    soft = Image.new("L", (W, H), 0)
    ImageDraw.Draw(soft).ellipse((ell[0] + fpx, ell[1] + fpx, ell[2] - fpx, ell[3] - fpx), fill=255)
    soft = soft.filter(ImageFilter.GaussianBlur(fpx / 2))
    inpaint = hard.crop(crop).filter(ImageFilter.GaussianBlur(2))
    return soft, inpaint


def remove_bg(img, cfg, warn=_warn):
    try:
        from rembg import new_session, remove
    except ImportError:
        warn("rembg 미설치 → 배경 제거 생략 (pip install rembg)")
        return img.convert("RGBA")
    global _rembg_session
    if "_rembg_session" not in globals():
        _rembg_session = new_session(cfg.get("rembg_model", "isnet-anime"))
    return remove(img, session=_rembg_session).convert("RGBA")


def foreground_alpha(img, cfg, warn=_warn):
    """(인물 영역 L 이미지 255=인물, rembg 결과인지). rembg 가 없으면 '흰 배경' 프롬프트를 믿고 색으로 추정"""
    try:
        import rembg  # noqa: F401
    except ImportError:
        r, g, b = img.convert("RGB").split()
        return ImageChops.darker(ImageChops.darker(r, g), b).point(lambda v: 255 if v < 220 else 0), False
    return remove_bg(img, cfg, warn).getchannel("A"), True


def top_coverage(alpha, band):
    """위쪽 band 픽셀 줄에서 인물이 차지하는 비율 (0~1). 머리가 위에 닿았거나 잘렸으면 큼"""
    hist = alpha.crop((0, 0, alpha.width, max(1, band))).histogram()
    return sum(hist[128:]) / max(sum(hist), 1)


HEADROOM_DEFAULTS = {"enabled": True, "min_margin": 0.03, "pad": 0.22, "max_passes": 2, "denoise": 1.0}


def headroom_cfg(cfg):
    return {**HEADROOM_DEFAULTS, **(cfg.get("headroom") or {})}


# union_type: "" 이면 union 모델이 아님 (SetUnionControlNetType 생략)
UNION_TYPES = ["", "auto", "openpose", "depth", "hed/pidi/scribble/ted", "canny/lineart/anime_lineart/mlsd",
               "normal", "segment", "tile", "repaint"]
CONTROLNET_DEFAULTS = {"enabled": False, "model": "", "union_type": "openpose", "pose_dir": "poses",
                       "pose": "upper_front.png", "strength": 0.8, "start": 0.0, "end": 0.6}


def controlnet_cfg(cfg):
    return {**CONTROLNET_DEFAULTS, **(cfg.get("controlnet") or {})}


def pose_dir(cfg):
    p = Path(controlnet_cfg(cfg)["pose_dir"])
    return p if p.is_absolute() else ROOT / p


def pose_path(cfg, name):
    """포즈 이름(확장자 생략 가능) → 파일 경로. 폴더 부분은 버림 (CSV 값으로 다른 폴더를 가리키지 못하게)"""
    name = Path(name.strip()).name
    if not Path(name).suffix:
        name += ".png"
    return pose_dir(cfg) / name


def pose_file(cfg, row, warn=_warn):
    """이 캐릭터가 쓸 포즈 템플릿. CSV pose 칸 → 없으면 config 기본 포즈.
    기본 포즈 파일까지 없으면 ConfigError (모든 캐릭터에서 똑같이 실패하므로)"""
    default = pose_path(cfg, controlnet_cfg(cfg)["pose"])
    want = (row.get("pose") or "").strip()
    if want:
        p = pose_path(cfg, want)
        if p.is_file():
            return p
        warn(f"포즈 '{want}' 파일이 없음 ({p}) → 기본 포즈 사용")
    if not default.is_file():
        raise ConfigError(f"ControlNet 포즈 템플릿이 없습니다: {default}\n"
                          f"    python pipeline.py --make-pose <캐릭터 id> 로 만들거나 controlnet 을 끄세요.")
    return default


def background_color(img, alpha):
    """배경(알파 거의 0) 픽셀의 중앙값 색. 배경이 거의 없으면 흰색"""
    small = img.convert("RGB").resize((img.width // 4, img.height // 4))
    hist = small.histogram(mask=alpha.resize(small.size).point(lambda v: 255 if v < 10 else 0))
    n = sum(hist[:256])
    if n < 200:
        return (255, 255, 255)
    color = []
    for ch in range(3):
        acc = 0
        for v, cnt in enumerate(hist[ch * 256:(ch + 1) * 256]):
            acc += cnt
            if acc * 2 >= n:
                color.append(v)
                break
    return tuple(color)


def shift_down_for_outpaint(img, pad, bg, ramp=None):
    """그림을 pad 만큼 아래로 내리고(아래쪽은 잘림) 위쪽 빈칸은 배경색으로 채운 이미지와 마스크 반환.
    마스크: 빈칸 255, 그 아래 ramp 줄은 255→0 으로 옅어짐 → DifferentialDiffusion 이 잘린 정수리 부근을
    조금씩 고쳐 그리며 이어 붙인다. (빈칸을 원본 윗줄을 늘려 채우면 그 줄무늬가 결과에 비쳐 보임)"""
    W, H = img.size
    ramp = int(pad * 0.8) if ramp is None else ramp
    shifted = Image.new("RGB", (W, H), bg)
    shifted.paste(img.crop((0, 0, W, H - pad)), (0, pad))
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    d.rectangle((0, 0, W, pad), fill=255)
    for i in range(ramp):
        d.line((0, pad + i, W, pad + i), fill=int(255 * (1 - (i + 1) / (ramp + 1))))
    return shifted, mask


def contact_sheet(items, height=360):
    thumbs = []
    for name, im in items:
        t = im.convert("RGBA")
        t = t.resize((int(t.width * height / t.height), height), Image.LANCZOS)
        bg = Image.new("RGBA", t.size, (235, 235, 235, 255))
        bg.alpha_composite(t)
        d = ImageDraw.Draw(bg)
        d.rectangle((0, 0, bg.width, 22), fill=(0, 0, 0, 160))
        d.text((6, 4), name, fill=(255, 255, 255, 255))
        thumbs.append(bg)
    sheet = Image.new("RGBA", (sum(t.width for t in thumbs), height), (255, 255, 255, 255))
    x = 0
    for t in thumbs:
        sheet.paste(t, (x, 0))
        x += t.width
    return sheet.convert("RGB")


# --------------------------------------------------------------------------- #
# 파일 유틸
# --------------------------------------------------------------------------- #
def output_root(cfg):
    p = Path(cfg["output_dir"])
    return p if p.is_absolute() else ROOT / p


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, obj):
    """임시 파일에 쓴 뒤 교체 (쓰는 도중에 읽어도 깨진 JSON 이 보이지 않게)"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    for i in range(20):  # Windows: 다른 쪽에서 파일을 잠깐 열고 있으면 교체가 실패할 수 있음
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.05)


def write_preview(out, meta):
    """meta.json 과 오버레이 파일로 검수용 preview.jpg 생성"""
    base_rgba = Image.open(out / "base.png").convert("RGBA")
    previews = [("neutral", base_rgba)]
    for e in meta["expressions"]:
        if not e["file"] or not (out / e["file"]).exists():
            continue
        prev = base_rgba.copy()
        prev.alpha_composite(Image.open(out / e["file"]).convert("RGBA"), (e["x"], e["y"]))
        previews.append((e["name"], prev))
    contact_sheet(previews).save(out / "preview.jpg", quality=88)


# base 가 바뀌면 의미가 없어지는 파일 (base 를 새로 만들 때 삭제)
DERIVED_FILES = ("base.png", "meta.json", "work.json", "preview.jpg")
EXPR_GEOMETRY_KEYS = ("inpaint_size", "crop_scale", "mask_expand", "feather")


# --------------------------------------------------------------------------- #
# 캐릭터 처리 (CLI / 웹 서버 공용)
# --------------------------------------------------------------------------- #
class Pipeline:
    """캐릭터 단위 생성 작업.

    on_progress(event: dict): 진행 이벤트 콜백. event["type"] 은
        log      {level, msg, id}                    로그 한 줄
        step     {id, stage, name}                   이미지 1장 생성 시작 (stage: base | expression)
        image    {id, kind, name, file, elapsed}     이미지 1장 완료 (file: output/<id>/ 기준 경로)
        portrait {id}                                캐릭터 결과물(상태)이 바뀜
        skip     {id}                                이미 완료되어 건너뜀
    control: wait() (일시정지 중이면 대기, 취소되면 Cancelled) 와 cancelled() 를 가진 객체. 없으면 무시.
    """

    def __init__(self, client, cfg, ex, out_root=None, rows=None, on_progress=None, control=None):
        self.client, self.cfg, self.ex = client, cfg, ex
        self.out_root = Path(out_root) if out_root else output_root(cfg)
        self.rows = {r["id"].strip(): r for r in (rows or [])}
        self.on_progress = on_progress
        self.control = control
        self._uploads = {}  # cid -> (crop 이미지 이름, 마스크 이름)
        self._alpha = {}    # cid -> (base 이미지, 인물 알파). 머리 여백 검사에서 구한 배경 제거 결과 재사용

    # ---- 진행 보고 / 제어 ------------------------------------------------ #
    def emit(self, type_, **kw):
        if self.on_progress:
            self.on_progress({"type": type_, **kw})

    def log(self, msg, cid=None, level="info", indent=True):
        print(("    " if indent else "") + msg)
        self.emit("log", level=level, msg=msg, id=cid)

    def _warn(self, cid):
        return lambda m: self.log(f"[경고] {m}", cid, "warn")

    def _run(self, graph):
        if self.control:
            self.control.wait()
        return self.client.run(graph, should_stop=self.control.cancelled if self.control else None)

    # ---- 경로 / 데이터 ---------------------------------------------------- #
    def out_dir(self, cid):
        return self.out_root / cid

    def row_of(self, cid):
        if cid not in self.rows:
            raise KeyError(f"캐릭터 '{cid}' 가 characters.csv 에 없습니다")
        return self.rows[cid]

    def current_seed(self, row):
        """지금 base_src.png 를 만든 seed (work.json 기록 우선, 없으면 CSV/id 기준)"""
        work = read_json(self.out_dir(row["id"].strip()) / "work.json", {})
        return work.get("seed", seed_of(row))

    def _invalidate(self, out):
        """base 가 새로 만들어졌으므로 이전 base 기준의 결과물 삭제"""
        for fn in DERIVED_FILES:
            (out / fn).unlink(missing_ok=True)
        for p in (out / "expr").glob("*.png"):
            p.unlink()

    # ---- 1) base --------------------------------------------------------- #
    def generate_base(self, row, seed=None, force=False):
        """base_src.png 생성. 이미 있으면 재사용.
        seed 를 주거나 force 면 새로 만들고, 기존 표정·meta 는 무효화(삭제)한다.
        반환: (base RGB 이미지, 사용한 seed)"""
        cid = row["id"].strip()
        out = self.out_dir(cid)
        (out / "expr").mkdir(parents=True, exist_ok=True)
        src_path = out / "base_src.png"
        if src_path.exists() and not force and seed is None:
            self.log("base_src.png 재사용", cid)
            return Image.open(src_path).convert("RGB"), self.current_seed(row)

        if seed is None:
            seed = seed_of(row)
        self.emit("step", id=cid, stage="base", name="base")
        t0 = time.time()
        cfg = effective_config(self.cfg, row)
        pos = build_prompt(cfg, row, self.ex["base_expression"])
        pose = self._upload_pose(row)
        base = self._run(graph_txt2img(cfg, pos, build_negative(cfg, row), seed, pose))
        if image_type(row) == "character":
            base = self._ensure_headroom(cid, base, pos, seed, negative=build_negative(cfg, row))
        base.save(src_path)
        self._invalidate(out)
        self._uploads.pop(cid, None)
        write_json(out / "work.json", {"seed": seed, "image_type": image_type(row), "generation": uuid.uuid4().hex})
        if image_type(row) != "character":
            self.finish_image(row, base, seed)
        self.log("base 생성 완료", cid)
        self.emit("image", id=cid, kind="base", name="base", file="base_src.png", elapsed=time.time() - t0)
        self.emit("portrait", id=cid)
        return base, seed

    def _upload_pose(self, row):
        """ControlNet 이 켜져 있으면 이 캐릭터의 포즈 템플릿을 ComfyUI 에 올리고 그 이름을 반환"""
        cfg = effective_config(self.cfg, row)
        if not controlnet_cfg(cfg)["enabled"]:
            return None
        cid = row["id"].strip()
        if cfg.get("type_poses") and (row.get("pose") or "").strip() and not pose_path(cfg, row["pose"].strip()).is_file():
            raise ConfigError(f"선택한 포즈 '{row['pose']}' 파일이 없습니다")
        path = pose_file(cfg, row, self._warn(cid))
        img = Image.open(path).convert("RGB")
        W, H = self.cfg["width"], self.cfg["height"]
        if abs(img.width / img.height - W / H) > 0.02:
            # ComfyUI 는 생성 크기에 맞춰 가운데를 잘라 씀 → 머리·어깨가 잘려 나갈 수 있음
            self._warn(cid)(f"포즈 '{path.name}' 비율({img.width}×{img.height})이 생성 크기({W}×{H})와 달라 가운데만 쓰입니다")
        return self.client.upload_image(img, f"pose_{path.stem}.png")

    def _ensure_headroom(self, cid, base, pos, seed, negative=None):
        """머리가 위쪽 가장자리에 닿았으면(잘렸으면) 그림을 아래로 내리고 위쪽을 아웃페인트.
        대화창에서 정수리가 잘려 보이지 않도록 위쪽 min_margin 만큼은 비어 있게 만든다"""
        hc = headroom_cfg(self.cfg)
        if not hc["enabled"]:
            return base
        warn = self._warn(cid)
        W, H = base.size
        band = max(2, int(H * hc["min_margin"]))
        pad = max(8, int(H * hc["pad"]) // 8 * 8)
        for i in range(int(hc["max_passes"]) + 1):
            alpha, from_rembg = foreground_alpha(base, self.cfg, warn)
            cov = top_coverage(alpha, band)
            if cov <= 0.02:
                if from_rembg:
                    self._alpha[cid] = (base, alpha)
                return base
            if i == int(hc["max_passes"]):
                break
            self.log(f"머리가 위쪽에 닿음 (위 {band}px 중 {cov:.0%}) → 위쪽 {pad}px 아웃페인트", cid)
            self.emit("step", id=cid, stage="base", name="headroom")
            shifted, mask = shift_down_for_outpaint(base, pad, background_color(base, alpha))
            img_name = self.client.upload_image(shifted, f"{cid}_outpaint.png")
            mask_name = self.client.upload_image(mask.convert("RGB"), f"{cid}_outpaint_mask.png")
            res = self._run(graph_inpaint(self.cfg, img_name, mask_name, pos, self.cfg["negative"] if negative is None else negative,
                                          seed + i + 1, hc["denoise"], steps=self.cfg["steps"],
                                          prefix="npc_face/outpaint", differential=True))
            if res.size != (W, H):
                res = res.resize((W, H), Image.LANCZOS)
            # 마스크가 0 인 곳은 원본 그대로 (VAE 를 거치며 생기는 미세한 변화도 막음). 경계만 살짝 흐리게
            keep = mask.point(lambda v: 255 if v > 0 else 0).filter(ImageFilter.GaussianBlur(3))
            base = Image.composite(res, shifted, keep)
        warn(f"아웃페인트 {hc['max_passes']}번 뒤에도 머리가 위쪽에 닿아 있습니다 → base 다시 뽑기 권장")
        return base

    # ---- 2) 얼굴 위치 / 마스크 -------------------------------------------- #
    def prepare(self, row, base=None, seed=None):
        """배경 제거(base.png) + 얼굴 검출. 결과를 work.json 에 저장하고 표정 생성용 컨텍스트 반환"""
        cid = row["id"].strip()
        out = self.out_dir(cid)
        src_path = out / "base_src.png"
        if base is None:
            base = Image.open(src_path).convert("RGB")
        if seed is None:
            seed = self.current_seed(row)
        warn = self._warn(cid)
        W, H = base.size
        face = detect_face(base, self.cfg, warn)
        if (face[3] - face[1]) / H > 0.42:
            warn(f"얼굴이 화면 세로의 {(face[3] - face[1]) / H:.0%} 를 차지합니다 (너무 가까움) → base 다시 뽑기 권장")
        alpha = Image.new("L", (W, H), 255)
        cached = self._alpha.pop(cid, None)
        if self.cfg.get("remove_background"):
            cut = cached[1] if cached and cached[0] is base else remove_bg(base, self.cfg, warn).getchannel("A")
            # 배경 제거가 얼굴까지 지워 버리면 (드물게 인물을 배경으로 오인) 캐릭터가 통째로 투명해지므로 쓰지 않음
            fx0, fy0, fx1, fy1 = (int(v) for v in face)
            hist = cut.crop((fx0, fy0, max(fx1, fx0 + 1), max(fy1, fy0 + 1))).histogram()
            if sum(hist[128:]) / max(sum(hist), 1) < 0.5:
                warn("배경 제거 결과 얼굴 부분이 대부분 투명해짐 → 배경 제거 생략 (base_src.png 확인 필요)")
            else:
                alpha = cut
        base_rgba = base.convert("RGBA")
        base_rgba.putalpha(alpha)
        base_rgba.save(out / "base.png")

        ecfg = {k: self.cfg["expression"][k] for k in EXPR_GEOMETRY_KEYS}
        crop, ell = face_regions(base.size, face, ecfg)
        work = {"seed": seed, "width": W, "height": H,
                "face": list(face), "crop": list(crop), "ellipse": list(ell), "expression_cfg": ecfg,
                "base_src_mtime": src_path.stat().st_mtime_ns}
        work["image_type"] = image_type(row)
        work["generation"] = read_json(out / "work.json", {}).get("generation")
        write_json(out / "work.json", work)
        self._uploads.pop(cid, None)
        return self._context(cid, base, alpha, work)

    def _context(self, cid, base, alpha, work):
        ecfg = work["expression_cfg"]
        crop, ell = tuple(work["crop"]), tuple(work["ellipse"])
        soft, inpaint_mask = make_masks(base.size, ell, crop, ecfg)
        return SimpleNamespace(
            cid=cid, base=base, alpha=alpha, W=base.width, H=base.height, seed=work["seed"],
            face=work["face"], crop=crop, ecfg=ecfg, soft=soft, inpaint_mask=inpaint_mask,
            bbox=soft.getbbox() or tuple(int(v) for v in ell), side=crop[2] - crop[0])

    def load_context(self, row):
        """work.json 으로 컨텍스트 복원 (얼굴 재검출 없음). 없거나 base 가 바뀌었으면 prepare"""
        cid = row["id"].strip()
        out = self.out_dir(cid)
        src_path = out / "base_src.png"
        if not src_path.exists():
            raise RuntimeError(f"{cid}: base_src.png 가 없습니다. base 를 먼저 생성하세요.")
        work = read_json(out / "work.json", {})
        if ("crop" in work and (out / "base.png").exists()
                and work.get("base_src_mtime") == src_path.stat().st_mtime_ns):
            base = Image.open(src_path).convert("RGB")
            alpha = Image.open(out / "base.png").getchannel("A")
            return self._context(cid, base, alpha, work)
        self.log("얼굴 정보(work.json)가 없거나 base 가 바뀜 → 다시 검출", cid)
        return self.prepare(row, seed=work.get("seed"))

    def _ensure_uploaded(self, ctx):
        if ctx.cid not in self._uploads:
            size = ctx.ecfg["inpaint_size"]
            crop_img = ctx.base.crop(ctx.crop).resize((size, size), Image.LANCZOS)
            img_name = self.client.upload_image(crop_img, f"{ctx.cid}_crop.png")
            mask_name = self.client.upload_image(
                ctx.inpaint_mask.resize((size, size), Image.LANCZOS).convert("RGB"), f"{ctx.cid}_mask.png")
            self._uploads[ctx.cid] = (img_name, mask_name)
        return self._uploads[ctx.cid]

    # ---- 3) 표정 --------------------------------------------------------- #
    def _render_expression(self, ctx, row, name, denoise=None, seed=None):
        """표정 1개 인페인트 → expr/<name>.png 저장, meta 항목 반환"""
        tags, default_denoise = expression_spec(name, self.ex, self.cfg, self._warn(ctx.cid))
        denoise = default_denoise if denoise is None else float(denoise)
        seed = ctx.seed if seed is None else int(seed)
        img_name, mask_name = self._ensure_uploaded(ctx)
        pos = build_prompt(self.cfg, row, tags, closeup=True)
        self.emit("step", id=ctx.cid, stage="expression", name=name)
        t0 = time.time()
        res = self._run(graph_inpaint(self.cfg, img_name, mask_name, pos, build_negative(self.cfg, row), seed, denoise))
        res = res.resize((ctx.side, ctx.side), Image.LANCZOS)

        layer = Image.new("RGB", (ctx.W, ctx.H))
        layer.paste(res, ctx.crop[:2])
        full = Image.composite(layer, ctx.base.copy(), ctx.soft)

        # 오버레이: 마스크 bbox 만 잘라 알파 = 페더마스크 × 배경알파
        bbox = ctx.bbox
        ov = full.crop(bbox).convert("RGBA")
        ov.putalpha(ImageChops.multiply(ctx.soft.crop(bbox), ctx.alpha.crop(bbox)))
        ov.save(self.out_dir(ctx.cid) / "expr" / f"{name}.png")
        elapsed = time.time() - t0
        self.log(f"{name:<12} {elapsed:5.1f}s", ctx.cid)
        self.emit("image", id=ctx.cid, kind="expression", name=name, file=f"expr/{name}.png", elapsed=elapsed)
        return {"name": name, "file": f"expr/{name}.png",
                "x": bbox[0], "y": bbox[1], "w": bbox[2] - bbox[0], "h": bbox[3] - bbox[1],
                "seed": seed, "denoise": denoise}

    def _new_meta(self, row, ctx, entries):
        return {"id": ctx.cid, "name": row.get("name", ""), "width": ctx.W, "height": ctx.H,
                "face": [int(v) for v in ctx.face], "seed": ctx.seed, "expressions": entries,
                "image_type": image_type(row)}

    def generate_expression(self, cid, name, denoise=None, seed=None):
        """완료된 캐릭터의 표정 1개만 다시 생성 (work.json 의 얼굴 정보 재사용).
        denoise/seed 를 생략하면 설정값/base seed 사용. 반환: meta 항목"""
        row = self.row_of(cid)
        if image_type(row) != "character":
            raise ValueError("주인공/조연만 표정을 생성할 수 있습니다")
        out = self.out_dir(cid)
        meta = read_json(out / "meta.json")
        if meta is None:
            raise RuntimeError(f"{cid}: meta.json 이 없습니다. 먼저 전체 생성을 하세요.")
        if name == "neutral":
            raise ValueError("neutral 은 base 그대로라 다시 만들 표정이 없습니다. base 를 다시 뽑으세요.")
        ctx = self.load_context(row)
        entry = self._render_expression(ctx, row, name, denoise, seed)
        exprs = meta["expressions"]
        idx = next((i for i, e in enumerate(exprs) if e["name"] == name), None)
        if idx is None:
            exprs.append(entry)
        else:
            exprs[idx] = entry
        if meta.get("approved"):
            meta["approved"] = False
            self.log("표정이 바뀌어 승인이 해제되었습니다", cid)
        write_json(out / "meta.json", meta)
        write_preview(out, meta)
        self.emit("portrait", id=cid)
        return entry

    # ---- 전체 흐름 -------------------------------------------------------- #
    def run_character(self, row, base_only=False, force=False, seed=None):
        """base → (base_only 가 아니면) 얼굴 검출 → 표정 전체 → meta.json.
        seed 를 주면 그 seed 로 base 를 새로 뽑는다. 반환: "skipped" | "base" | "done" """
        cid = row["id"].strip()
        out = self.out_dir(cid)
        meta_path = out / "meta.json"
        old = read_json(meta_path, {})
        if old.get("image_type", "character") != image_type(row):
            # Preserve previous results until an explicit new generation replaces them.
            force = True
        if meta_path.exists() and not force and not base_only and seed is None:
            self.log(f"[skip] {cid} (이미 완료, 다시 하려면 --force)", cid, indent=False)
            self.emit("skip", id=cid)
            return "skipped"
        (out / "expr").mkdir(parents=True, exist_ok=True)
        regen = force or seed is not None or not (out / "base_src.png").exists()
        shown = seed if seed is not None else (seed_of(row) if regen else self.current_seed(row))
        self.log(f"[{cid}] {row.get('name', '')}  seed={shown}", cid, indent=False)

        base, seed = self.generate_base(row, seed=seed, force=force)
        if image_type(row) != "character":
            if not meta_path.exists():
                self.finish_image(row, base, seed)
            return "done"
        if base_only:
            return "base"

        ctx = self.prepare(row, base, seed)
        entries = []
        for name in expression_list(row, self.ex, self._warn(cid)):
            if name == "neutral":
                entries.append({"name": "neutral", "file": "", "x": 0, "y": 0, "w": 0, "h": 0})
                continue
            entries.append(self._render_expression(ctx, row, name))

        # meta.json 은 마지막에 한 번에 기록 (= 완료 표시. 중간에 끊기면 다음 실행에서 이어서 처리)
        meta = self._new_meta(row, ctx, entries)
        write_json(meta_path, meta)
        write_preview(out, meta)
        self.emit("portrait", id=cid)
        return "done"

    def finish_image(self, row, base, seed):
        """Finalize image-only types without a face detector."""
        out = self.out_dir(row["id"].strip())
        result = remove_bg(base, self.cfg, self._warn(row["id"])) if self.cfg.get("remove_background") else base
        result.save(out / "base.png")
        write_json(out / "meta.json", {"id": row["id"], "name": row.get("name", ""),
                   "width": base.width, "height": base.height, "seed": seed,
                   "image_type": image_type(row), "face": [], "expressions": []})
        self.emit("portrait", id=row["id"])

    def generate_layers(self, row, spec=None, force=False, before_split=None):
        """Reuse SeeThrough, preserving the source image's coordinate system."""
        import copy
        import parts_tool as PT
        if image_type(row) == "monster":
            raise ValueError("몬스터는 레이어 분리를 지원하지 않습니다")
        cid = row["id"].strip()
        src = self.out_dir(cid) / "base_src.png"
        if not src.exists():
            raise ValueError("먼저 이미지를 생성하세요")
        if read_json(self.out_dir(cid) / "work.json", {}).get("image_type", "character") != image_type(row):
            raise ValueError("이미지 유형이 변경되었습니다. 먼저 이미지를 다시 생성하세요")
        spec = copy.deepcopy(spec if spec is not None else read_json(ROOT / "parts.json"))
        spec["out_dir"] = str(self.out_root / "_layers")
        source = {"id": cid, "from": cid, "align": False, "seed": self.current_seed(row),
                  "tags": row.get("name") or cid}
        d = PT.out_root(spec) / cid
        provenance = {"source": fingerprint(src), "image_type": image_type(row),
                      "generation": read_json(self.out_dir(cid) / "work.json", {}).get("generation")}
        if not force and read_json(d / "provenance.json") == provenance and (d / "layers.json").exists():
            return "skipped"
        if before_split:
            before_split()
        PT.check_seethrough(self.client)
        # Keep the last successful layer set until its replacement is complete.
        stage_root = self.out_root / "_layers" / ".staging" / uuid.uuid4().hex
        spec["out_dir"] = str(stage_root)
        staged = stage_root / cid
        PT.import_source(self.cfg, spec, source, self.out_dir(cid), warn=self._warn(cid))
        if self.control:
            self.control.wait()
        PT.split_source(self.client, spec, source,
                        should_stop=self.control.cancelled if self.control else None)
        if fingerprint(src) != provenance["source"]:
            raise RuntimeError("레이어 분리 중 원본 이미지가 변경되었습니다. 다시 분리하세요")
        write_json(staged / "provenance.json", provenance)
        if d.exists():
            history = d.parent / ".history"
            history.mkdir(exist_ok=True)
            d.rename(history / f"{cid}-{uuid.uuid4().hex}")
        staged.rename(d)
        stage_root.rmdir()
        self.emit("portrait", id=cid)
        return "done"


def process_character(client, cfg, ex, row, out_root, base_only=False, force=False):
    """(구버전 호환) 캐릭터 1명 처리"""
    return Pipeline(client, cfg, ex, out_root, rows=[row]).run_character(row, base_only, force)


def make_pose(client, cfg, base, name):
    """base 그림에서 OpenPose 뼈대를 뽑아 포즈 템플릿(<pose_dir>/<name>.png)으로 저장. 저장한 경로 반환"""
    W, H = cfg["width"], cfg["height"]
    img_name = client.upload_image(base.convert("RGB"), "pose_src.png")
    pose = client.run(graph_pose_extract(img_name, max(64, min(W, H) // 64 * 64)))
    if pose.convert("L").point(lambda v: 255 if v > 20 else 0).getbbox() is None:
        # 빈(검은) 템플릿을 쓰면 ControlNet 이 '사람 없음'으로 이끌어 오히려 구도가 망가짐
        raise RuntimeError("그림에서 자세(뼈대)를 찾지 못했습니다. 인물이 잘 보이는 다른 base 로 해 보세요.")
    if pose.size != (W, H):
        pose = pose.resize((W, H), Image.LANCZOS)
    path = pose_path(cfg, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    pose.save(path)
    return path


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="NPC 페이스 스프라이트 생성")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--expressions", default=str(ROOT / "expressions.json"))
    ap.add_argument("--characters", default=str(ROOT / "characters.csv"))
    ap.add_argument("--only", help="쉼표로 구분한 id 목록")
    ap.add_argument("--base-only", action="store_true", help="base 만 생성 (검수용)")
    ap.add_argument("--layers", action="store_true", help="기존 기본 이미지의 레이어만 분리")
    ap.add_argument("--expressions-only", action="store_true", help="주인공/조연 표정까지 생성 (레이어 제외)")
    ap.add_argument("--force", action="store_true", help="기존 결과 무시하고 다시 생성")
    ap.add_argument("--export-workflows", action="store_true", help="샘플 워크플로우 JSON 저장 후 종료")
    ap.add_argument("--make-pose", metavar="ID",
                    help="이 캐릭터의 base 자세를 ControlNet 포즈 템플릿으로 저장 (comfyui_controlnet_aux 필요)")
    ap.add_argument("--pose-name", help="--make-pose 로 저장할 이름 (기본: config controlnet.pose)")
    a = ap.parse_args()

    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    ex = json.loads(Path(a.expressions).read_text(encoding="utf-8"))
    try:
        rows = load_characters(a.characters)
    except ValueError as e:
        sys.exit(f"[!] {e}")
    if a.only:
        want = {x.strip() for x in a.only.split(",")}
        rows = [r for r in rows if r["id"].strip() in want]

    if a.make_pose:
        src = output_root(cfg) / a.make_pose / "base_src.png"
        if not src.exists():
            sys.exit(f"[!] {src} 가 없습니다. 먼저 base 를 만드세요: python pipeline.py --only {a.make_pose} --base-only")
        name = a.pose_name or controlnet_cfg(cfg)["pose"]
        dst = pose_path(cfg, name)
        if dst.exists() and not a.force:
            sys.exit(f"[!] {dst} 가 이미 있습니다. 덮어쓰려면 --force")
        client = ComfyClient(cfg["comfy_url"])
        try:
            client.check()
            print(f"포즈 저장: {make_pose(client, cfg, Image.open(src), name)}")
        except (ComfyConnectionError, ComfyValidationError, RuntimeError) as e:
            sys.exit(f"[!] {e}")
        return

    if a.export_workflows:
        d = ROOT / "workflows"
        d.mkdir(exist_ok=True)
        r = rows[0] if rows else {"id": "sample"}
        (d / "base_txt2img_api.json").write_text(json.dumps(
            graph_txt2img(cfg, build_prompt(cfg, r, ex["base_expression"]), build_negative(cfg, r), seed_of(r),
                          "pose.png" if controlnet_cfg(effective_config(cfg, r))["enabled"] else None), indent=2), "utf-8")
        (d / "expression_inpaint_api.json").write_text(json.dumps(
            graph_inpaint(cfg, "crop.png", "mask.png", build_prompt(cfg, r, expression_spec("smile", ex, cfg)[0], True),
                          build_negative(cfg, r), seed_of(r), cfg["expression"]["denoise"]), indent=2), "utf-8")
        print(f"저장: {d}")
        return

    out_root = output_root(cfg)
    client = ComfyClient(cfg["comfy_url"])
    try:
        client.check()
    except ComfyConnectionError as e:
        sys.exit(f"[!] {e}")
    if sum((a.layers, a.expressions_only, a.base_only)) > 1:
        ap.error("--layers / --expressions-only / --base-only 중 하나만 선택하세요")
    print(f"캐릭터 {len(rows)}명 처리 시작 → {out_root}")
    fails = []
    for i, row in enumerate(rows, 1):
        print(f"({i}/{len(rows)}) ", end="")
        try:
            pipe = Pipeline(client, effective_config(cfg, row), ex, out_root, rows=rows)
            if a.layers:
                pipe.generate_layers(row, force=a.force)
            else:
                # SeeThrough owns models outside ComfyUI's regular SDXL lifecycle.
                __import__("parts_tool").free_vram(client)
                if a.expressions_only and image_type(row) != "character":
                    raise ValueError("주인공/조연만 표정을 생성할 수 있습니다")
                if a.expressions_only and not (out_root / row["id"].strip() / "base_src.png").exists():
                    raise ValueError("먼저 이미지를 생성하고 검수하세요")
                pipe.run_character(row, a.base_only, a.force)
                if not a.base_only and not a.expressions_only and image_type(row) != "monster":
                    pipe.generate_layers(row, force=a.force,
                                         before_split=lambda: __import__("parts_tool").free_vram(client))
        except KeyboardInterrupt:
            print("\n중단됨. 다시 실행하면 완료된 캐릭터는 건너뜁니다.")
            break
        except ComfyValidationError as e:  # 설정 문제는 다음 캐릭터에서도 똑같이 실패하므로 바로 멈춤
            print(f"\n[!] {e}\n    config.json 을 고친 뒤 다시 실행하세요.")
            fails.append(row["id"])
            break
        except Exception as e:
            print(f"    [실패] {e}")
            fails.append(row["id"])
    if fails:
        print(f"\n실패 {len(fails)}건: {','.join(fails)}\n  → python pipeline.py --only {','.join(fails)}")


if __name__ == "__main__":
    main()
