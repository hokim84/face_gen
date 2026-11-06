"""ComfyUI HTTP API 흉내 서버 (테스트용). 그래프 유효성 검사 + 더미 이미지 생성.

python tests/fake_comfy.py                # 포트 8000, 즉시 완료
python tests/fake_comfy.py --delay 0.5    # 이미지 1장에 0.5초 걸리는 것처럼 (일시정지/취소 테스트용)
실행 중에 POST /_fake/config {"delay": 0.3} 으로 지연을 바꿀 수 있고, GET /_fake/state 로 호출 기록을 볼 수 있다.
"""
import argparse, io, json, re, threading, time, uuid
from email.parser import BytesParser
from email.policy import default as POL
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from PIL import Image, ImageDraw

UP, OUT, HIST = {}, {}, {}
PENDING = {}            # prompt_id -> 완료 예정 시각 (delay > 0 일 때)
STATE = {"delay": 0.0, "prompts": 0, "interrupts": 0, "last_ready": 0.0, "frees": 0, "seethrough": 0,
         "controlnet": 0, "last_control": None}
LOCK = threading.Lock()
KNOWN = {
 "CheckpointLoaderSimple": {"ckpt_name"}, "LoraLoader": {"model","clip","lora_name","strength_model","strength_clip"},
 "CLIPSetLastLayer": {"clip","stop_at_clip_layer"}, "CLIPTextEncode": {"text","clip"},
 "EmptyLatentImage": {"width","height","batch_size"},
 "KSampler": {"model","seed","steps","cfg","sampler_name","scheduler","positive","negative","latent_image","denoise"},
 "VAEDecode": {"samples","vae"}, "SaveImage": {"images","filename_prefix"}, "LoadImage": {"image"},
 "LoadImageMask": {"image","channel"}, "VAEEncode": {"pixels","vae"}, "SetLatentNoiseMask": {"samples","mask"},
 "VAELoader": {"vae_name"}, "DifferentialDiffusion": {"model"},
 "ControlNetLoader": {"control_net_name"}, "SetUnionControlNetType": {"control_net","type"},
 "ControlNetApplyAdvanced": {"positive","negative","control_net","image","strength","start_percent","end_percent"},
 # comfyui_controlnet_aux 커스텀 노드 (pipeline --make-pose)
 "DWPreprocessor": {"image","detect_hand","detect_body","detect_face","resolution","bbox_detector","pose_estimator",
                    "scale_stick_for_xinsr_cn"},
 # SeeThrough 커스텀 노드 (parts_tool split)
 "SeeThrough_LoadLayerDiffModel": {"model","vae_ckpt","unet_ckpt","quant_mode","cache_tag_embeds","group_offload","auto_download"},
 "SeeThrough_LoadDepthModel": {"model","quant_mode","cache_tag_embeds","group_offload","auto_download"},
 "SeeThrough_GenerateLayers": {"image","layerdiff_model","seed","resolution","num_inference_steps"},
 "SeeThrough_GenerateDepth": {"layers","depth_model","seed","resolution_depth"},
 "SeeThrough_PostProcess": {"layers_depth","tblr_split","use_lama"},
 "SeeThrough_SavePSD": {"parts","filename_prefix"},
}
# /object_info 응답 (드롭다운 테스트용). 실제 ComfyUI 의 구형 형식 [[목록], {...}] 을 흉내낸다
OBJECT_INFO = {
 "CheckpointLoaderSimple": {"ckpt_name": ["waiIllustriousSDXL_v150.safetensors", "illustriousXL_v01.safetensors"]},
 "LoraLoader": {"lora_name": ["my_style.safetensors", "manhwa_style.safetensors"]},
 "VAELoader": {"vae_name": ["sdxl_vae_fp16_fix.safetensors", "pixel_space"]},
 "ControlNetLoader": {"control_net_name": ["controlnet-union-sdxl-promax.safetensors", "control_v11p_sd15_openpose.pth"]},
 "SetUnionControlNetType": {"type": ["auto", "openpose", "depth", "hed/pidi/scribble/ted",
                                     "canny/lineart/anime_lineart/mlsd", "normal", "segment", "tile", "repaint"]},
 "KSampler": {"sampler_name": ["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde"],
              "scheduler": ["normal", "karras", "exponential", "sgm_uniform"]},
 "SeeThrough_GenerateLayers": {"resolution": "INT"},
}

def validate(g):
    for k, n in g.items():
        ct = n["class_type"]; assert ct in KNOWN, ct
        assert set(n["inputs"]) == KNOWN[ct], (ct, set(n["inputs"]) ^ KNOWN[ct])
        for v in n["inputs"].values():
            if isinstance(v, list): assert v[0] in g, f"bad link {v}"
    for n in g.values():
        if n["class_type"] in ("LoadImage", "LoadImageMask"): assert n["inputs"]["image"] in UP, n["inputs"]["image"]

COMBO_INPUTS = {("CheckpointLoaderSimple", "ckpt_name"), ("LoraLoader", "lora_name"), ("VAELoader", "vae_name"),
                ("KSampler", "sampler_name"), ("KSampler", "scheduler"), ("ControlNetLoader", "control_net_name"),
                ("SetUnionControlNetType", "type")}

def node_errors(g):
    """실제 ComfyUI 처럼 목록에 없는 모델/샘플러 이름이면 value_not_in_list 오류 (응답 400)"""
    errs = {}
    for k, n in g.items():
        for cls, inp in COMBO_INPUTS:
            if n["class_type"] != cls: continue
            options, v = OBJECT_INFO[cls][inp], n["inputs"][inp]
            if v not in options:
                errs[k] = {"class_type": cls, "dependent_outputs": [], "errors": [{
                    "type": "value_not_in_list", "message": "Value not in list",
                    "details": f"{inp}: '{v}' not in {options}",
                    "extra_info": {"input_name": inp, "input_config": [options, {}], "received_value": v}}]}
    return errs

# 가짜 레이어: (이름, 정사각형 캔버스 기준 상대 박스, 깊이). 실제처럼 흰자가 홍채보다 앞(깊이 작음)
FAKE_LAYERS = [("back hair", (.30, .04, .70, .45), .95), ("neck", (.45, .33, .55, .50), .70),
               ("topwear", (.20, .42, .80, 1.0), .80),
               ("face", (.38, .12, .62, .38), .53), ("eyewhite-l", (.51, .21, .57, .26), .40),
               ("irides-l", (.52, .22, .56, .25), .41), ("front hair", (.35, .08, .65, .20), .05),
               ("eyewear", (.40, .20, .60, .27), .20)]

def png_bytes(im):
    buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()

def fake_seethrough(by):
    """SavePSD 처럼 출력 폴더에 레이어 PNG + layers.json + seethrough_psd_info.log 를 만든다"""
    src = UP[by["LoadImage"]["inputs"]["image"]].convert("RGBA")
    R = by["SeeThrough_GenerateLayers"]["inputs"]["resolution"]
    prefix = by["SeeThrough_SavePSD"]["inputs"]["filename_prefix"]
    w, h = src.size; sz = max(w, h)
    sq = Image.new("RGBA", (sz, sz), (0, 0, 0, 0)); sq.paste(src, ((sz - w) // 2, (sz - h) // 2)); sq = sq.resize((R, R))
    ts = uuid.uuid4().hex[:8]; layers = []
    for name, rel, depth in FAKE_LAYERS:
        b = tuple(int(v * R) for v in rel)
        fn = f"{prefix}_{ts}_{name}.png"; OUT[fn] = png_bytes(sq.crop(b))
        dfn = f"{prefix}_{ts}_{name}_depth.png"   # 실제 노드처럼 회색조(L), 밝을수록 멂
        OUT[dfn] = png_bytes(Image.new("L", (b[2] - b[0], b[3] - b[1]), int(depth * 255)))
        layers.append({"name": name, "filename": fn, "depth_filename": dfn,
                       "left": b[0], "top": b[1], "right": b[2], "bottom": b[3], "depth_median": depth})
    info = f"{prefix}_{ts}_layers.json"
    OUT[info] = json.dumps({"prefix": prefix, "layers": layers, "width": R, "height": R}).encode()
    OUT["seethrough_psd_info.log"] = info.encode()
    STATE["seethrough"] += 1

def render(g):
    by = {n["class_type"]: n for n in g.values()}
    if "SeeThrough_SavePSD" in by:
        return fake_seethrough(by)
    if "DWPreprocessor" in by:   # 검은 바탕에 뼈대 (실제처럼 짧은 변 = resolution)
        src = UP[by["LoadImage"]["inputs"]["image"]]; r = by["DWPreprocessor"]["inputs"]["resolution"]
        w, h = (r, round(src.height * r / src.width)) if src.width <= src.height else (round(src.width * r / src.height), r)
        im = Image.new("RGB", (w, h), "black"); d = ImageDraw.Draw(im)
        d.line((w*.5, h*.25, w*.5, h*.45), fill=(255, 0, 0), width=8); d.line((w*.2, h*.45, w*.8, h*.45), fill=(255, 85, 0), width=8)
        return im
    if "ControlNetApplyAdvanced" in by:
        a = by["ControlNetApplyAdvanced"]["inputs"]; li = g[a["image"][0]]
        STATE["controlnet"] += 1
        STATE["last_control"] = {"image": li["inputs"]["image"], "strength": a["strength"],
                                 "start": a["start_percent"], "end": a["end_percent"],
                                 "type": by["SetUnionControlNetType"]["inputs"]["type"] if "SetUnionControlNetType" in by else None}
    if "EmptyLatentImage" in by:
        w, h = by["EmptyLatentImage"]["inputs"]["width"], by["EmptyLatentImage"]["inputs"]["height"]
        im = Image.new("RGB", (w, h), "white"); d = ImageDraw.Draw(im)
        d.rectangle((w*.2, h*.42, w*.8, h), fill=(60, 80, 160))           # body
        d.ellipse((w*.34, h*.13, w*.66, h*.36), fill=(250, 220, 200))     # face
        d.ellipse((w*.42, h*.21, w*.46, h*.24), fill="black"); d.ellipse((w*.54, h*.21, w*.58, h*.24), fill="black")
        d.line((w*.46, h*.30, w*.54, h*.30), fill="black", width=4)
        return im
    src = UP[by["LoadImage"]["inputs"]["image"]].convert("RGB"); d = ImageDraw.Draw(src); s = src.width
    txt = [n["inputs"]["text"] for n in g.values() if n["class_type"] == "CLIPTextEncode"][0]
    col = (220, 30, 30) if "smile" in txt or "laugh" in txt or "grin" in txt else (30, 30, 220)
    d.ellipse((s*.40, s*.55, s*.60, s*.65), fill=col)  # 바뀐 입
    return src

def success(fn):
    return {"status": {"status_str": "success", "completed": True},
            "outputs": {"9": {"images": [{"filename": fn, "subfolder": "npc_face", "type": "output"}]}}}

def interrupted():
    return {"status": {"status_str": "error", "completed": False,
                       "messages": [["execution_interrupted", {"node_type": "KSampler"}]]}, "outputs": {}}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _j(self, o, code=200):
        b = json.dumps(o).encode(); self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0); return self.rfile.read(n) if n else b""
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/system_stats": return self._j({"ok": 1})
        if u.path == "/_fake/state":
            with LOCK: return self._j({**STATE, "pending": len(PENDING)})
        m = re.match(r"/object_info/(\w+)$", u.path)
        if m:
            req = OBJECT_INFO.get(m[1])
            if req is None: return self._j({})
            return self._j({m[1]: {"input": {"required": {k: [v, {}] for k, v in req.items()}}}})
        m = re.match(r"/history/(.+)", u.path)
        if m:
            pid = m[1]
            with LOCK:
                if pid in PENDING and time.time() >= PENDING[pid]:
                    del PENDING[pid]; HIST[pid] = success(pid + ".png")
                return self._j({pid: HIST[pid]} if pid in HIST else {})
        if u.path == "/view":
            fn = parse_qs(u.query)["filename"][0]
            if fn not in OUT: return self._j({"error": "not found"}, 404)
            b = OUT[fn] if isinstance(OUT[fn], bytes) else png_bytes(OUT[fn])
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        self._j({"error": "not found"}, 404)
    def do_POST(self):
        body = self._body()
        if self.path == "/upload/image":
            msg = BytesParser(policy=POL).parsebytes(b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            parts = {pt.get_param("name", header="content-disposition"): pt for pt in msg.iter_parts()}
            fn = parts["image"].get_filename(); UP[fn] = Image.open(io.BytesIO(parts["image"].get_payload(decode=True)))
            assert parts["overwrite"].get_content().strip() == "true"
            return self._j({"name": fn, "subfolder": "", "type": "input"})
        if self.path == "/prompt":
            g = json.loads(body)["prompt"]
            try: validate(g)
            except AssertionError as e: return self._j({"error": str(e)}, 400)
            ne = node_errors(g)
            if ne:
                return self._j({"error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation",
                                          "details": "", "extra_info": {}}, "node_errors": ne}, 400)
            pid = uuid.uuid4().hex; fn = pid + ".png"; OUT[fn] = render(g)
            with LOCK:
                STATE["prompts"] += 1
                if STATE["delay"] > 0:   # 앞 프롬프트가 끝난 뒤 delay 초 걸려 완료 (ComfyUI 는 하나씩 실행)
                    ready = max(time.time(), STATE["last_ready"]) + STATE["delay"]
                    STATE["last_ready"] = PENDING[pid] = ready
                else:
                    HIST[pid] = success(fn)
            return self._j({"prompt_id": pid})
        if self.path == "/interrupt":
            req = json.loads(body or b"{}")
            with LOCK:
                STATE["interrupts"] += 1
                targets = [req["prompt_id"]] if req.get("prompt_id") else sorted(PENDING, key=PENDING.get)[:1]
                for pid in targets:
                    if PENDING.pop(pid, None) is not None: HIST[pid] = interrupted()
                STATE["last_ready"] = max(PENDING.values(), default=0.0)
            return self._j({})
        if self.path == "/queue":
            req = json.loads(body or b"{}")
            with LOCK:
                for pid in req.get("delete", []): PENDING.pop(pid, None)
            return self._j({})
        if self.path == "/free":
            with LOCK: STATE["frees"] += 1
            return self._j({})
        if self.path == "/_fake/config":
            with LOCK: STATE.update({k: float(v) for k, v in json.loads(body).items() if k == "delay"})
            return self._j(STATE)
        self._j({"error": "not found"}, 404)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--delay", type=float, default=0.0, help="이미지 1장당 걸리는 시간(초)")
    a = ap.parse_args()
    STATE["delay"] = a.delay
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
