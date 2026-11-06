#!/usr/bin/env python3
"""
NPC 초상화 생성기 웹 UI 서버 (로컬 전용)

  python server.py                 # http://127.0.0.1:7870 을 열고 브라우저 실행
  python server.py --port 7871 --no-browser

브라우저 ─(REST + SSE)─ 이 서버 ─(HTTP)─ ComfyUI
생성 작업은 백그라운드 스레드 1개가 큐에서 하나씩 꺼내 처리합니다 (GPU 1개).
"""
import argparse
import asyncio
import csv
import io
import json
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
import uuid
import os
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, StrictStr, conint

import character_generator as CG
import parts_tool as PT
import pipeline as P
from image_types import TYPES, image_type, effective_config, fingerprint, prompt_profiles

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
APP_NAME = "npc-portrait-webui"


class Paths:
    config = ROOT / "config.json"
    expressions = ROOT / "expressions.json"
    characters = ROOT / "characters.csv"
    parts = ROOT / "parts.json"


# --------------------------------------------------------------------------- #
# 파일 읽기/쓰기
# --------------------------------------------------------------------------- #
def load_config():
    cfg = json.loads(Paths.config.read_text(encoding="utf-8"))
    defaults = {k: cfg.get("controlnet", {}).get("pose", "upper_front.png") for k in ("character", "npc")}
    cfg["type_poses"] = {**defaults, **cfg.get("type_poses", {})}
    cfg["type_prompts"] = prompt_profiles(cfg)
    return cfg


def load_expressions():
    return json.loads(Paths.expressions.read_text(encoding="utf-8"))


def load_rows():
    return P.load_characters(Paths.characters) if Paths.characters.exists() else []


def out_root():
    return P.output_root(load_config())


# --------------------------------------------------------------------------- #
# characters.csv 읽기/쓰기
# --------------------------------------------------------------------------- #
STD_COLUMNS = ["id", "name", "image_type", "prompt", "subject", "age", "hair", "eyes", "outfit", "extra", "personality", "expressions", "pose",
               "seed"]
CSV_LOCK = threading.RLock()
ROW_ID_RE = re.compile(r"^[\w\-]+$")
EXPR_NAME_RE = re.compile(r"^[\w\-]+$")


def decode_csv(raw):
    """UTF-8(BOM 포함) 우선, 안 되면 CP949 (엑셀 기본 저장). (텍스트, 인코딩 이름) 반환"""
    for enc, label in (("utf-8-sig", "UTF-8"), ("cp949", "CP949")):
        try:
            return raw.decode(enc), label
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV 인코딩을 읽을 수 없습니다 (UTF-8 / CP949 가 아님)")


def parse_csv(text):
    """(열 목록, 행 목록). id 가 빈 행은 버리고 값은 앞뒤 공백 제거"""
    reader = csv.DictReader(io.StringIO(text, newline=""))
    cols = [c.strip() for c in (reader.fieldnames or []) if c and c.strip()]
    if "id" not in cols:
        raise ValueError("CSV 첫 줄(헤더)에 id 열이 없습니다")
    rows = []
    for r in reader:
        row = {c: (r.get(c) or "").strip() for c in cols}
        if row["id"]:
            rows.append(row)
    return cols, rows


def csv_version():
    try:
        return str(Paths.characters.stat().st_mtime_ns)
    except FileNotFoundError:
        return "0"


def csv_columns():
    """현재 파일의 열 순서 (표준 열 + 파일에 있던 그 밖의 열)"""
    cols = list(STD_COLUMNS)
    if Paths.characters.exists():
        try:
            file_cols, _ = parse_csv(decode_csv(Paths.characters.read_bytes())[0])
            cols += [c for c in file_cols if c not in cols]
        except ValueError:
            pass
    return cols


def csv_text(rows, columns):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in columns})
    return buf.getvalue()


def save_rows(rows, columns=None):
    """characters.csv 저장 (엑셀에서 한글이 깨지지 않게 UTF-8 BOM). 이전 파일은 .bak 으로"""
    columns = columns or csv_columns()
    for r in rows:  # 행에만 있는 새 열도 보존
        columns += [k for k in r if k not in columns]
    with CSV_LOCK:
        path = Paths.characters
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(csv_text(rows, columns), encoding="utf-8-sig", newline="")
        try:
            tmp.replace(path)
        except PermissionError:
            tmp.unlink(missing_ok=True)
            raise HTTPException(409, "characters.csv 를 저장할 수 없습니다. 엑셀 등에서 열려 있으면 닫고 다시 저장하세요.")
    return csv_version()


def validate_rows(rows, ex, cfg=None):
    """(오류 목록, 경고 목록). cfg 를 주면 pose 칸의 포즈 템플릿 파일이 있는지도 확인"""
    errs, warns = [], []
    seen = {}
    pers = ex.get("personalities", {})
    for i, r in enumerate(rows, 1):
        try:
            kind = image_type(r)
        except ValueError as exc:
            errs.append(f"{i}행: {exc}")
            continue
        if kind == "monster" and not (r.get("prompt") or "").strip():
            errs.append(f"{i}행: 몬스터 설명 태그를 입력하세요")
        cid = (r.get("id") or "").strip()
        label = f"{i}행({cid or '빈 id'})"
        if not cid:
            errs.append(f"{label}: id 가 비어 있습니다")
            continue
        if not ROW_ID_RE.match(cid):
            errs.append(f"{label}: id 에는 글자/숫자/_/- 만 쓸 수 있습니다 (폴더 이름이 됩니다)")
        key = cid.lower()  # Windows 폴더는 대소문자를 구분하지 않음
        if key in seen:
            errs.append(f"{label}: id 가 {seen[key]}행과 겹칩니다")
        seen.setdefault(key, i)
        seed = (r.get("seed") or "").strip()
        if seed and not (seed.isdigit() and int(seed) < 2 ** 63):
            errs.append(f"{label}: seed 는 0 이상의 정수여야 합니다")
        for p in [x.strip() for x in (r.get("personality") or "").split("|") if x.strip() and kind == "character"]:
            if p not in pers:
                warns.append(f"{label}: 성격 '{p}' 이(가) 없어 기본 성격을 씁니다")
        for n in [x.strip() for x in (r.get("expressions") or "").split("|") if x.strip() and kind == "character"]:
            if not EXPR_NAME_RE.match(n):
                errs.append(f"{label}: 표정 이름 '{n}' 에는 영문/숫자/_/- 만 쓸 수 있습니다")
            elif n not in ex.get("expressions", {}):
                warns.append(f"{label}: 표정 '{n}' 이(가) expressions.json 에 없어 이름을 그대로 태그로 씁니다")
        pose = (r.get("pose") or "").strip()
        if pose and kind != "monster":
            if Path(pose).name != pose:
                errs.append(f"{label}: pose 에는 파일 이름만 쓸 수 있습니다 (폴더 없이)")
            elif cfg and not P.pose_path(cfg, pose).is_file():
                warns.append(f"{label}: 포즈 '{pose}' 파일이 {P.pose_dir(cfg)} 에 없어 기본 포즈를 씁니다")
    return errs, warns


def update_csv_seed(cid, seed):
    """base 를 새 seed 로 다시 뽑았을 때 CSV 의 seed 칸에도 기록 (CLI 로 --force 해도 같은 그림이 나오게)"""
    with CSV_LOCK:
        rows = load_rows()
        hit = next((r for r in rows if r["id"].strip() == cid), None)
        if hit is None:
            return None
        hit["seed"] = str(seed)
        return save_rows(rows)


def pretty_json(o, ind=0):
    """사람이 손으로 고치기 쉬운 JSON: 값이 단순한 리스트/작은 객체는 한 줄로"""
    sp = "  " * ind
    simple = lambda v: not isinstance(v, (dict, list))
    if isinstance(o, list) and all(simple(v) for v in o):
        return json.dumps(o, ensure_ascii=False)
    if isinstance(o, dict) and o and all(simple(v) for v in o.values()) and len(json.dumps(o, ensure_ascii=False)) < 90 and ind > 1:
        return json.dumps(o, ensure_ascii=False)
    if isinstance(o, dict):
        if not o:
            return "{}"
        body = ",\n".join(f"{sp}  {json.dumps(k, ensure_ascii=False)}: {pretty_json(v, ind + 1)}" for k, v in o.items())
        return "{\n" + body + "\n" + sp + "}"
    if isinstance(o, list):
        if not o:
            return "[]"
        return "[\n" + ",\n".join(f"{sp}  {pretty_json(v, ind + 1)}" for v in o) + "\n" + sp + "]"
    return json.dumps(o, ensure_ascii=False)


def save_json_with_backup(path, obj):
    path = Path(path)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(pretty_json(obj) + "\n", encoding="utf-8")
    tmp.replace(path)


ID_RE = re.compile(r"^[\w\-][\w\-. ]*$")


def check_id(cid):
    if not ID_RE.match(cid) or ".." in cid:
        raise HTTPException(400, f"잘못된 id: {cid}")
    return cid


def file_url(cid, rel):
    p = out_root() / cid / rel
    try:
        return f"/output/{cid}/{rel}?v={p.stat().st_mtime_ns}"
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------- #
# 캐릭터 상태
# --------------------------------------------------------------------------- #
_meta_cache = {}  # path -> (mtime_ns, meta)


def read_meta(path):
    try:
        mt = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    hit = _meta_cache.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    _meta_cache[path] = (mt, meta)
    return meta


STATUS_LABEL = {"none": "미생성", "base": "후처리 대기", "done": "완료", "approved": "승인됨"}


def portrait_status(out):
    meta = read_meta(out / "meta.json")
    if meta is not None:
        return ("approved" if meta.get("approved") else "done"), meta
    if (out / "base_src.png").exists():
        return "base", None
    return "none", None


def portrait_summary(cid, row=None):
    out = out_root() / cid
    status, meta = portrait_status(out)
    kind = image_type(row or meta or {})
    stale = bool(meta and meta.get("image_type", "character") != kind)
    work = P.read_json(out / "work.json", {})
    image_ok = (out / "base_src.png").exists() and work.get("image_type", "character") == kind
    if stale:
        meta = None
    layers = character_layers(cid, kind)
    complete = image_ok and bool(meta) and (kind == "monster" or layers["current"])
    status = ("approved" if meta.get("approved") else "done") if complete else ("base" if (out / "base_src.png").exists() else "none")
    thumb_src = out / "base.png" if (out / "base.png").exists() else out / "base_src.png"
    thumb = None
    if status != "none" and thumb_src.exists():
        thumb = f"/api/thumb/{cid}?v={thumb_src.stat().st_mtime_ns}"
    name = (row or {}).get("name") or (meta or {}).get("name", "")
    return {
        "id": cid, "name": name, "status": status,
        "image_type": kind, "stale": stale or (not image_ok and status != "none"),
        "stages": {"image": image_ok, "expressions": bool(meta) if kind == "character" else None,
                   "layers": layers["current"] if kind != "monster" else None},
        "layers": layers,
        "personality": ((row or {}).get("personality") or "").strip(),
        "thumb": thumb, "in_csv": row is not None,
        "expr_count": sum(1 for e in (meta or {}).get("expressions", []) if e.get("file")),
    }


def character_layers(cid, kind):
    d = out_root() / "_layers" / cid
    provenance = P.read_json(d / "provenance.json", {})
    src = out_root() / cid / "base_src.png"
    current = kind != "monster" and (d / "layers.json").is_file() and provenance == {
        "source": fingerprint(src), "image_type": kind,
        "generation": P.read_json(out_root() / cid / "work.json", {}).get("generation")}
    return {"current": current, "available": (d / "layers.json").is_file(),
            "preview": file_url("_layers", f"{cid}/check.png") if (d / "check.png").exists() else None,
            "psd_url": f"/api/portraits/{cid}/layers.psd" if current else None}


def all_portraits():
    rows = load_rows()
    seen = set()
    res = []
    for r in rows:
        cid = r["id"].strip()
        seen.add(cid)
        res.append(portrait_summary(cid, r))
    root = out_root()
    if root.exists():  # CSV 에는 없지만 결과물이 남아 있는 캐릭터
        for d in sorted(root.iterdir()):
            if d.is_dir() and d.name not in seen and (d / "base_src.png").exists():
                res.append(portrait_summary(d.name))
    return res


# --------------------------------------------------------------------------- #
# 이벤트 버스 (작업 스레드 → SSE 구독자)
# --------------------------------------------------------------------------- #
class EventBus:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs = set()
        self.logs = deque(maxlen=400)
        self.images = deque(maxlen=24)
        self.seq = 0

    def publish(self, ev):
        with self.lock:
            self.seq += 1
            ev = {**ev, "seq": self.seq, "ts": time.time()}
            if ev["type"] == "log":
                self.logs.append(ev)
            elif ev["type"] == "image":
                self.images.append(ev)
            subs = list(self.subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._put, q, ev)
            except RuntimeError:  # 이벤트 루프가 이미 닫힘
                pass

    @staticmethod
    def _put(q, ev):
        if q.full():  # 느린 구독자: 오래된 것부터 버림
            q.get_nowait()
        q.put_nowait(ev)

    def subscribe(self, loop):
        q = asyncio.Queue(maxsize=2000)
        with self.lock:
            self.subs.add((loop, q))
            snapshot = {"logs": list(self.logs), "images": list(self.images)}
        return q, snapshot

    def unsubscribe(self, loop, q):
        with self.lock:
            self.subs.discard((loop, q))

    def log(self, msg, level="info", cid=None, job=None):
        print(f"[웹] {msg}")
        self.publish({"type": "log", "level": level, "msg": msg, "id": cid, "job": job})


BUS = EventBus()


# --------------------------------------------------------------------------- #
# 작업 큐 (스레드 1개)
# --------------------------------------------------------------------------- #
MODE_LABEL = {"base": "base 생성", "full": "전체 생성", "expression": "표정 재생성",
              "parts_gen": "파츠 소스 생성", "parts_split": "파츠 레이어 분리"}
CHAR_MODES = ("base", "full", "expression", "expressions", "layers")
MODE_LABEL.update(expressions="표정 생성", layers="레이어 분리")
PARTS_MODES = ("parts_gen", "parts_split")
FINISHED = ("done", "failed", "cancelled")
CONNECTION_ERRORS = (P.ComfyConnectionError, urllib.error.URLError, ConnectionError)


class Job:
    _next = 1

    def __init__(self, cid, name, mode, expression=None, denoise=None, new_seed=False, force=False):
        self.id = Job._next
        Job._next += 1
        self.cid, self.name, self.mode = cid, name, mode
        self.expression, self.denoise, self.new_seed, self.force = expression, denoise, new_seed, force
        self.status = "pending"
        self.error = None
        self.result = None
        self.step = None
        self.created = time.time()
        self.started = self.finished = None
        self.est_images = 0
        self.done_images = 0
        self.seed_used = None
        self.cancel_requested = False
        self.image_type = "character"
        self.retry_stage = None

    def public(self):
        return {k: getattr(self, k) for k in (
            "id", "cid", "name", "mode", "expression", "denoise", "new_seed", "force", "status", "error",
            "result", "step", "created", "started", "finished", "est_images", "done_images", "image_type")}


class JobControl:
    """Pipeline 이 이미지 1장마다 호출: 일시정지면 대기, 취소면 Cancelled"""

    def __init__(self, queue, job):
        self.q, self.job = queue, job

    def cancelled(self):
        return self.job.cancel_requested

    def wait(self):
        with self.q.cond:
            if self.q.paused and not self.job.cancel_requested:
                self.job.step = "일시정지됨"
                self.q.publish_job(self.job)
            while self.q.paused and not self.job.cancel_requested:
                self.q.cond.wait(0.5)
        if self.job.cancel_requested:
            raise P.Cancelled()


class JobQueue:
    def __init__(self, bus):
        self.bus = bus
        self.cond = threading.Condition(threading.RLock())
        self.jobs = OrderedDict()   # id -> Job (표시용, 완료 포함)
        self.pending = deque()      # 대기 중인 job id
        self.current = None
        self.paused = False
        self.pause_reason = None  # 자동 일시정지 이유 (설정 오류, 연결 끊김)
        self.durations = deque(maxlen=50)  # 최근 이미지 1장 생성 시간
        self.last_engine = None     # 마지막으로 GPU 에 올린 모델 종류 (sdxl / seethrough)
        self.thread = threading.Thread(target=self._worker, name="job-worker", daemon=True)

    def start(self):
        self.thread.start()

    # ---- 상태 보고 ---- #
    def summary(self):
        with self.cond:
            jobs = list(self.jobs.values())
            counts = {s: 0 for s in ("pending", "running", "done", "failed", "cancelled")}
            for j in jobs:
                counts[j.status] += 1
            total = sum(j.est_images for j in jobs)
            done = sum(j.done_images for j in jobs)
            avg = sum(self.durations) / len(self.durations) if self.durations else None
            remaining = max(total - done, 0)
            return {
                "paused": self.paused,
                "pause_reason": self.pause_reason if self.paused else None,
                "running": self.current.public() if self.current else None,
                "counts": counts,
                "images_total": total, "images_done": done,
                "avg_sec": avg,
                "eta_sec": remaining * avg if avg is not None else None,
            }

    def publish_job(self, job):
        self.bus.publish({"type": "job", "job": job.public()})

    def publish_queue(self):
        self.bus.publish({"type": "queue", "queue": self.summary()})

    def list(self):
        with self.cond:
            return [j.public() for j in self.jobs.values()]

    # ---- 제어 ---- #
    def submit(self, specs):
        """specs: [(row, mode, expression, denoise, new_seed, force)] → (추가된 Job 목록, 중복으로 건너뛴 수)"""
        cfg, ex = load_config(), load_expressions()
        root = P.output_root(cfg)
        added, dup = [], 0
        with self.cond:
            waiting = {(self.jobs[i].cid, self.jobs[i].mode, self.jobs[i].expression) for i in self.pending}
            for row, mode, expression, denoise, new_seed, force in specs:
                cid = row["id"].strip()
                if (cid, mode, expression) in waiting:
                    dup += 1
                    continue
                job = Job(cid, row.get("name", ""), mode, expression, denoise, new_seed, force)
                job.image_type = image_type(row)
                job.est_images = estimate_images(row, job, ex, root)
                self.jobs[job.id] = job
                self.pending.append(job.id)
                waiting.add((cid, mode, expression))
                added.append(job)
            self.cond.notify_all()
        for j in added:
            self.publish_job(j)
        self.publish_queue()
        return added, dup

    def set_paused(self, paused):
        with self.cond:
            self.paused = paused
            self.pause_reason = None
            if not paused and self.current and self.current.step == "일시정지됨":
                self.current.step = None
            self.cond.notify_all()
        self.bus.log("큐를 일시정지했습니다 (진행 중인 이미지는 끝까지 생성)" if paused else "큐를 재개했습니다")
        if self.current:
            self.publish_job(self.current)
        self.publish_queue()

    def cancel(self, job_ids=None):
        """job_ids 가 없으면 대기·진행 중인 작업 전부 취소"""
        changed = []
        with self.cond:
            targets = set(job_ids) if job_ids else None
            for jid in list(self.pending):
                if targets is None or jid in targets:
                    self.pending.remove(jid)
                    j = self.jobs[jid]
                    j.status, j.finished, j.est_images = "cancelled", time.time(), 0
                    changed.append(j)
            cur = self.current
            if cur and (targets is None or cur.id in targets):
                cur.cancel_requested = True
                cur.step = "취소 중…"
                changed.append(cur)
            self.cond.notify_all()
        for j in changed:
            self.publish_job(j)
        if changed:
            self.bus.log(f"작업 {len(changed)}건 취소")
        self.publish_queue()
        return len(changed)

    def clear_finished(self):
        with self.cond:
            for jid in [i for i, j in self.jobs.items() if j.status in FINISHED]:
                del self.jobs[jid]
        self.bus.publish({"type": "jobs_reset"})
        self.publish_queue()

    # ---- 작업 스레드 ---- #
    def _worker(self):
        while True:
            with self.cond:
                while self.paused or not self.pending:
                    self.cond.wait()
                job = self.jobs[self.pending.popleft()]
                job.status, job.started = "running", time.time()
                self.current = job
            self.publish_job(job)
            self.publish_queue()
            self.bus.log(f"▶ {job.name or job.cid} — {MODE_LABEL.get(job.mode, job.mode)}"
                         + (f" ({job.expression})" if job.expression else ""), cid=job.cid, job=job.id)
            requeued = False
            try:
                job.result = self._execute(job)
                job.status = "done"
            except P.Cancelled:
                job.status = "cancelled"
                self.bus.log(f"{job.cid}: 취소됨", "warn", job.cid, job.id)
            except CONNECTION_ERRORS as e:
                # ComfyUI 가 꺼져 있으면 남은 작업이 줄줄이 실패하지 않게 되돌려 놓고 큐를 멈춘다
                requeued = self._requeue_and_pause(job, f"ComfyUI 에 연결할 수 없습니다. ComfyUI 를 켠 뒤 [재개]를 누르세요. ({e})")
            except P.ComfyValidationError as e:
                # 없는 모델 이름 같은 설정 문제도 모든 캐릭터에서 똑같이 실패하므로 같은 방식으로 멈춘다
                requeued = self._requeue_and_pause(job, f"{e}\n설정 탭에서 고친 뒤 [재개]를 누르세요.")
            except Exception as e:
                job.status, job.error = "failed", str(e)
                self.bus.log(f"{job.cid}: 실패 — {e}", "error", job.cid, job.id)
                traceback.print_exc()
            finally:
                with self.cond:
                    self.current = None
                    if not requeued:
                        job.finished = time.time()
                        job.est_images = job.done_images
                        job.step = None
            self.publish_job(job)
            self.publish_queue()

    def _requeue_and_pause(self, job, reason):
        with self.cond:
            if job.mode == "full" and job.retry_stage in ("layers", "expressions"):
                job.mode = "layers" if job.retry_stage == "layers" else "full"
                job.new_seed, job.force = False, False
            job.status, job.started, job.step = "pending", None, None
            job.done_images = 0
            self.pending.appendleft(job.id)
            self.paused = True
            self.pause_reason = reason
        self.bus.log(f"큐를 일시정지했습니다 — {reason}", "error", job.cid, job.id)
        return True

    def _execute(self, job):
        if job.mode in PARTS_MODES:
            return self._execute_parts(job)
        cfg, ex = load_config(), load_expressions()
        rows = load_rows()
        row = next((r for r in rows if r["id"].strip() == job.cid), None)
        if row is None:
            raise RuntimeError(f"characters.csv 에 '{job.cid}' 가 없습니다")
        if image_type(row) != job.image_type:
            raise ValueError("대기 중 이미지 유형이 변경되었습니다. 새 유형으로 다시 실행하세요")
        validate_type_job(row, job.mode)
        cfg = effective_config(cfg, row)
        client = P.ComfyClient(cfg["comfy_url"])
        client.check()
        if job.mode != "layers":
            self._switch_engine(client, "sdxl", job)
        pipe = P.Pipeline(client, cfg, ex, rows=rows, control=JobControl(self, job),
                          on_progress=lambda ev: self._on_progress(job, ev))
        seed = secrets.randbelow(2 ** 32) if job.new_seed else None
        job.seed_used = seed
        if job.mode == "layers":
            result = pipe.generate_layers(row, load_parts(), force=job.force,
                        before_split=lambda: self._switch_engine(client, "seethrough", job))
            if result == "done":
                self._parts_image(job, None)
            return result
        if job.mode == "base":
            pipe.run_character(row, base_only=True, force=job.force, seed=seed)
        elif job.mode in ("full", "expressions"):
            result = pipe.run_character(row, force=job.force, seed=seed)
            if job.mode == "full" and image_type(row) != "monster":
                job.retry_stage = "layers"
                job.step = "레이어 분리"
                self.publish_job(job)
                layered = pipe.generate_layers(row, load_parts(), force=job.force,
                            before_split=lambda: self._switch_engine(client, "seethrough", job))
                if layered == "done":
                    self._parts_image(job, None)
            elif result == "skipped":
                return "skipped"
        elif job.mode == "expression":
            pipe.generate_expression(job.cid, job.expression, job.denoise, seed)
        else:
            raise ValueError(f"알 수 없는 mode: {job.mode}")
        return "done" if job.done_images else "skipped"

    def _switch_engine(self, client, engine, job):
        """SDXL <-> SeeThrough 를 오갈 때 ComfyUI 의 모델을 내림. 둘이 같이 올라가면 16GB 가 빠듯함.
        (SeeThrough 노드는 ComfyUI 의 모델 관리 밖에서 diffusers 로 모델을 올리므로 자동으로 비켜 주지 않음)"""
        prev, self.last_engine = self.last_engine, engine
        if engine == prev or (prev is None and engine != "seethrough"):
            return
        self.bus.log("다른 종류의 모델을 쓰므로 ComfyUI 의 VRAM 을 비웁니다", cid=job.cid, job=job.id)
        PT.free_vram(client, warn=lambda m: self.bus.log(m, "warn", job.cid, job.id))

    def _execute_parts(self, job):
        cfg, spec = load_config(), load_parts()
        s = next((x for x in spec["sources"] if x["id"] == job.cid), None)
        if s is None:
            raise RuntimeError(f"parts.json 에 '{job.cid}' 가 없습니다")
        state = PT.source_state(PT.out_root(spec), s)
        ctl = JobControl(self, job)
        warn = lambda m: self.bus.log(m, "warn", job.cid, job.id)
        if job.mode == "parts_gen":
            if state["source"] and not job.force:
                return "skipped"
        else:
            if not state["source"]:
                raise RuntimeError("소스 이미지가 없습니다. 먼저 [1. 소스 생성]을 하세요.")
            if state["layers"] is not None and not job.force:
                return "skipped"
        ctl.wait()
        if job.mode == "parts_gen" and s.get("from"):   # 생성 이미지에서 가져온 소스: ComfyUI 를 쓰지 않음
            job.step = "생성 이미지 가져오기"
            self.publish_job(job)
            PT.import_source(cfg, spec, s, out_root() / s["from"], warn=warn)
            self._parts_image(job, None)
            self.bus.log(f"{job.cid}: {s['from']} 의 생성 이미지를 소스로 가져왔습니다", cid=job.cid, job=job.id)
            self.bus.publish({"type": "parts", "id": job.cid})
            return "done"
        client = P.ComfyClient(cfg["comfy_url"])
        client.check()
        if job.mode == "parts_gen":
            self._switch_engine(client, "sdxl", job)
            job.step = "소스 생성"
            self.publish_job(job)
            PT.gen_source(client, cfg, spec, s, should_stop=ctl.cancelled, warn=warn,
                          on_image=lambda sec: self._parts_image(job, sec))
        else:
            PT.check_seethrough(client)
            self._switch_engine(client, "seethrough", job)
            job.step = "SeeThrough 레이어 분리 (장당 약 4~5분)"
            self.publish_job(job)
            n = PT.split_source(client, spec, s, should_stop=ctl.cancelled)
            # 분리는 장당 몇 분이라 '장당 평균'에 넣으면 남은 시간 계산이 크게 틀어짐 → 시간은 기록하지 않음
            self._parts_image(job, None)
            self.bus.log(f"{job.cid}: 레이어 {n}개로 분리했습니다", cid=job.cid, job=job.id)
        self.bus.publish({"type": "parts", "id": job.cid})
        return "done"

    def _parts_image(self, job, sec):
        with self.cond:
            job.done_images += 1
            job.est_images = max(job.est_images, job.done_images)
            if sec is not None:
                self.durations.append(sec)
        self.publish_job(job)
        self.publish_queue()

    def _on_progress(self, job, ev):
        t = ev["type"]
        if t == "log":
            self.bus.publish({"type": "log", "level": ev["level"], "msg": ev["msg"], "id": ev.get("id"), "job": job.id})
        elif t == "step":
            job.retry_stage = "expressions" if ev.get("stage") == "expression" else "base"
            job.step = "base" if ev["stage"] == "base" else ev["name"]
            self.publish_job(job)
        elif t == "image":
            with self.cond:
                job.done_images += 1
                job.est_images = max(job.est_images, job.done_images)
                self.durations.append(ev["elapsed"])
            self.bus.publish({"type": "image", "id": ev["id"], "kind": ev["kind"], "name": ev["name"],
                              "url": file_url(ev["id"], ev["file"]), "job": job.id})
            if ev["kind"] == "base" and job.mode != "expression" and job.seed_used is not None:
                self._record_seed(job)
            self.publish_job(job)
            self.publish_queue()
        elif t == "portrait":
            self.bus.publish({"type": "portrait", "id": ev["id"], "status": portrait_status(out_root() / ev["id"])[0]})


    def _record_seed(self, job):
        """새 seed 로 뽑은 base 를 CSV 에도 기록. 엑셀이 파일을 잡고 있어도 생성은 계속한다"""
        try:
            version = update_csv_seed(job.cid, job.seed_used)
        except HTTPException as e:
            self.bus.log(f"새 seed {job.seed_used} 를 characters.csv 에 기록하지 못했습니다 ({e.detail}). "
                         "work.json/meta.json 에는 남아 있습니다.", "warn", job.cid, job.id)
            return
        if version:
            self.bus.log(f"새 seed {job.seed_used} 를 characters.csv 에 기록했습니다", cid=job.cid, job=job.id)
            self.bus.publish({"type": "characters", "version": version,
                              "changes": [{"id": job.cid, "seed": str(job.seed_used)}]})


def estimate_images(row, job, ex, root):
    """이 작업이 생성할 이미지 수 (진행률·남은 시간 계산용)"""
    if job.mode == "layers":
        return 1
    if job.mode in PARTS_MODES:
        st = PT.source_state(parts_root(), row)
        if job.mode == "parts_gen":
            return 0 if st["source"] and not job.force else 1 if row.get("from") else 2
        return 0 if st["layers"] is not None and not job.force else 1
    out = root / job.cid
    if job.mode == "expression":
        return 1
    new_base = job.force or job.new_seed or not (out / "base_src.png").exists()
    if job.mode == "base":
        return 1 if new_base else 0
    extra = int(job.mode == "full" and image_type(row) != "monster")
    if (out / "meta.json").exists() and not job.force and not job.new_seed:
        return extra
    if image_type(row) != "character":
        return int(new_base) + extra
    names = P.expression_list(row, ex, warn=lambda m: None)
    return int(new_base) + sum(1 for n in names if n != "neutral") + extra


QUEUE = JobQueue(BUS)


# --------------------------------------------------------------------------- #
# ComfyUI 상태
# --------------------------------------------------------------------------- #
_comfy_cache = {"t": 0.0, "url": None, "res": None}


def comfy_status(url=None, use_cache=True):
    url = url or load_config()["comfy_url"]
    c = _comfy_cache
    if use_cache and c["url"] == url and time.time() - c["t"] < 3:
        return c["res"]
    t0 = time.time()
    try:
        stats = P.ComfyClient(url).check()
        res = {"ok": True, "url": url, "latency_ms": int((time.time() - t0) * 1000), "error": None}
        sysinfo = stats.get("system", {}) if isinstance(stats, dict) else {}
        res["version"] = sysinfo.get("comfyui_version")
        devs = stats.get("devices") or [] if isinstance(stats, dict) else []
        if devs:
            d = devs[0]
            res["device"] = d.get("name")
            if d.get("vram_total"):
                res["vram_gb"] = round(d["vram_total"] / 1024 ** 3, 1)
    except Exception as e:
        res = {"ok": False, "url": url, "error": str(e).split("\n")[-1].strip() or str(e)}
    c.update(t=time.time(), url=url, res=res)
    return res


_models_cache = {}  # url -> (시각, 목록)


def fetch_models(url, use_cache=True):
    """ComfyUI 에 설치된 체크포인트/LoRA/샘플러/스케줄러 목록 (30초 캐시)"""
    hit = _models_cache.get(url)
    if use_cache and hit and time.time() - hit[0] < 30:
        return hit[1]
    try:
        client = P.ComfyClient(url)
        ck = client.object_info("CheckpointLoaderSimple")
        lo = client.object_info("LoraLoader")
        ks = client.object_info("KSampler")
        va = client.object_info("VAELoader")
        cn = client.object_info("ControlNetLoader")
    except Exception as e:
        return {"ok": False, "error": str(e).split("\n")[-1] or str(e),
                "checkpoints": [], "loras": [], "samplers": [], "schedulers": [], "vaes": [], "controlnets": []}
    res = {"ok": True, "checkpoints": combo_options(ck, "ckpt_name"), "loras": combo_options(lo, "lora_name"),
           "vaes": [v for v in combo_options(va, "vae_name") if v != "pixel_space"],
           "controlnets": combo_options(cn, "control_net_name"),
           "samplers": combo_options(ks, "sampler_name"), "schedulers": combo_options(ks, "scheduler")}
    _models_cache[url] = (time.time(), res)
    return res


def config_issues(cfg):
    """config.json 의 모델/샘플러 이름 중 ComfyUI 에 없는 것 (생성 전에 미리 경고)"""
    m = fetch_models(cfg["comfy_url"])
    if not m["ok"]:
        return []
    issues = []

    def check(value, names, label):
        if names and value and value not in names:
            shown = ", ".join(names[:6]) + (" …" if len(names) > 6 else "")
            issues.append(f"{label} '{value}' 이(가) ComfyUI 에 없습니다 (설치된 것: {shown})")

    check(cfg.get("checkpoint"), m["checkpoints"], "체크포인트")
    check((cfg.get("vae") or "").strip(), m.get("vaes", []), "VAE")
    for l in cfg.get("loras", []):
        check(l.get("name"), m["loras"], "LoRA")
    check(cfg.get("sampler"), m["samplers"], "샘플러")
    check(cfg.get("scheduler"), m["schedulers"], "스케줄러")
    cc = P.controlnet_cfg(cfg)
    if cc["enabled"] or cfg.get("type_poses"):
        check(cc["model"], m.get("controlnets", []), "ControlNet 모델")
        if not P.pose_path(cfg, cc["pose"]).is_file():
            issues.append(f"기본 포즈 템플릿 '{cc['pose']}' 이(가) {P.pose_dir(cfg)} 에 없습니다 "
                          f"(python pipeline.py --make-pose <캐릭터 id> 로 만드세요)")
        for kind, name in cfg.get("type_poses", {}).items():
            if not P.pose_path(cfg, name).is_file():
                issues.append(f"{TYPES.get(kind, kind)} 기본 포즈 '{name}' 파일이 없습니다")
    return issues


def combo_options(info, name):
    """object_info 의 콤보 입력 목록. 구형 [[...], {}] 와 신형 ["COMBO", {"options": [...]}] 둘 다 지원"""
    inputs = info.get("input", {})
    spec = inputs.get("required", {}).get(name) or inputs.get("optional", {}).get(name)
    if not spec:
        return []
    if isinstance(spec[0], list):
        return spec[0]
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("options", [])
    return []


# --------------------------------------------------------------------------- #
# 검증
# --------------------------------------------------------------------------- #
def _num(v, lo=None, hi=None, integer=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    if integer and int(v) != v:
        return False
    return (lo is None or v >= lo) and (hi is None or v <= hi)


def validate_config(c):
    errs = []

    def need(cond, msg):
        if not cond:
            errs.append(msg)

    if not isinstance(c, dict):
        return ["설정은 JSON 객체여야 합니다"]
    prompts = c.get("type_prompts", {})
    need(isinstance(prompts, dict), "type_prompts: 객체여야 합니다")
    if isinstance(prompts, dict):
        for kind, profile in prompts.items():
            need(kind in TYPES and isinstance(profile, dict), "type_prompts: 유형별 프롬프트 객체여야 합니다")
            if isinstance(profile, dict):
                for key, value in profile.items():
                    need(key in ("positive", "negative") and isinstance(value, str) and len(value) <= 10000,
                         "type_prompts: positive / negative에 10000자 이하의 문자열을 입력하세요")
    poses = c.get("type_poses", {})
    need(isinstance(poses, dict), "type_poses: 객체여야 합니다")
    if isinstance(poses, dict):
        for kind, pose in poses.items():
            need(kind in ("character", "npc") and isinstance(pose, str) and bool(pose.strip()) and Path(pose).name == pose,
                 "type_poses: 주인공/조연·NPC 포즈 파일 이름을 입력하세요")
    for k in ("comfy_url", "checkpoint", "sampler", "scheduler", "output_dir"):
        need(isinstance(c.get(k), str) and c.get(k).strip(), f"{k}: 비어 있으면 안 됩니다")
    need(isinstance(c.get("vae", ""), str), "vae: 문자열이어야 합니다 (비우면 체크포인트 내장 VAE)")
    for k in ("quality_prefix", "framing", "closeup_framing", "background", "style_suffix", "negative"):
        need(isinstance(c.get(k, ""), str), f"{k}: 문자열이어야 합니다")
    need(isinstance(c.get("comfy_url"), str) and c["comfy_url"].startswith(("http://", "https://")),
         "comfy_url: http:// 로 시작해야 합니다")
    for k in ("width", "height"):
        need(_num(c.get(k), 256, 4096, True) and c[k] % 8 == 0, f"{k}: 256~4096 사이 8의 배수여야 합니다")
    need(_num(c.get("steps"), 1, 150, True), "steps: 1~150 정수")
    need(_num(c.get("cfg"), 0, 30), "cfg: 0~30")
    need(_num(c.get("clip_skip", 2), 1, 12, True), "clip_skip: 1~12 정수")
    loras = c.get("loras", [])
    if not isinstance(loras, list):
        errs.append("loras: 목록이어야 합니다")
    else:
        for i, l in enumerate(loras, 1):
            if not isinstance(l, dict) or not isinstance(l.get("name"), str) or not l["name"].strip():
                errs.append(f"LoRA {i}: 파일 이름이 필요합니다")
            elif not _num(l.get("strength", 0.7), -5, 5) or not _num(l.get("strength_clip", 0), -5, 5):
                errs.append(f"LoRA {i}: 강도는 -5~5 숫자")
    e = c.get("expression")
    if not isinstance(e, dict):
        errs.append("expression: 객체여야 합니다")
    else:
        need(_num(e.get("denoise"), 0.01, 1), "expression.denoise: 0.01~1")
        need(_num(e.get("steps"), 1, 150, True), "expression.steps: 1~150 정수")
        need(_num(e.get("inpaint_size"), 256, 2048, True) and e["inpaint_size"] % 8 == 0,
             "expression.inpaint_size: 256~2048 사이 8의 배수")
        need(_num(e.get("crop_scale"), 1, 6), "expression.crop_scale: 1~6")
        need(_num(e.get("mask_expand"), 0.5, 3), "expression.mask_expand: 0.5~3")
        need(_num(e.get("feather"), 0, 0.5), "expression.feather: 0~0.5")
    hr = c.get("headroom", {})
    if not isinstance(hr, dict):
        errs.append("headroom: 객체여야 합니다")
    else:
        need(isinstance(hr.get("enabled", True), bool), "headroom.enabled: 참/거짓")
        need(_num(hr.get("min_margin", 0.03), 0.005, 0.2), "headroom.min_margin: 0.005~0.2")
        need(_num(hr.get("pad", 0.1), 0.02, 0.4), "headroom.pad: 0.02~0.4")
        need(_num(hr.get("max_passes", 2), 1, 5, True), "headroom.max_passes: 1~5 정수")
        need(_num(hr.get("denoise", 1.0), 0.3, 1), "headroom.denoise: 0.3~1")
    cn = c.get("controlnet", {})
    if not isinstance(cn, dict):
        errs.append("controlnet: 객체여야 합니다")
    else:
        cc = {**P.CONTROLNET_DEFAULTS, **cn}
        need(isinstance(cc["enabled"], bool), "controlnet.enabled: 참/거짓")
        need(isinstance(cc["model"], str) and (cc["model"].strip() or (cc["enabled"] is not True and not c.get("type_poses"))),
             "controlnet.model: 사용하려면 ControlNet 모델을 골라야 합니다")
        need(cc["union_type"] in P.UNION_TYPES, "controlnet.union_type: " + ", ".join(t or '""' for t in P.UNION_TYPES))
        need(isinstance(cc["pose_dir"], str) and cc["pose_dir"].strip(), "controlnet.pose_dir: 비어 있으면 안 됩니다")
        need(isinstance(cc["pose"], str) and cc["pose"].strip() and Path(cc["pose"]).name == cc["pose"].strip(),
             "controlnet.pose: 포즈 파일 이름 (폴더 없이)")
        need(_num(cc["strength"], 0, 2), "controlnet.strength: 0~2")
        need(_num(cc["start"], 0, 1) and _num(cc["end"], 0, 1) and cc["start"] < cc["end"],
             "controlnet.start / end: 0~1, start < end")
    fd = c.get("face_detector")
    if not isinstance(fd, dict) or not fd.get("repo") or not fd.get("file"):
        errs.append("face_detector: repo 와 file 이 필요합니다")
    elif not _num(fd.get("conf", 0.3), 0.01, 1):
        errs.append("face_detector.conf: 0.01~1")
    need(isinstance(c.get("remove_background", False), bool), "remove_background: 참/거짓")
    ued = c.get("unity_export_dir", "")
    need(isinstance(ued, str) and (not ued.strip() or Path(ued).is_absolute()),
         "unity_export_dir: 비우거나 절대 경로 (예: D:\\MyGame\\Assets\\Resources\\Portraits)")
    return errs


def validate_expressions(x):
    if not isinstance(x, dict):
        return ["JSON 객체여야 합니다"]
    errs = []
    if not isinstance(x.get("base_expression"), str):
        errs.append("base_expression: 문자열이어야 합니다")
    exprs = x.get("expressions")
    if not isinstance(exprs, dict) or not exprs:
        return errs + ["expressions: 표정이 하나 이상 있어야 합니다"]
    for name, v in exprs.items():
        if not re.match(r"^[\w\-]+$", name):
            errs.append(f"표정 이름 '{name}': 영문/숫자/_/- 만 쓸 수 있습니다 (파일 이름이 됩니다)")
        if isinstance(v, str):
            continue
        if not isinstance(v, dict) or not isinstance(v.get("tags"), str):
            errs.append(f"표정 '{name}': 태그 문자열이 필요합니다")
        elif "denoise" in v and not _num(v["denoise"], 0.01, 1):
            errs.append(f"표정 '{name}': denoise 는 0.01~1")
    pers = x.get("personalities")
    if not isinstance(pers, dict) or not pers:
        return errs + ["personalities: 성격이 하나 이상 있어야 합니다"]
    for p, names in pers.items():
        if not p.strip() or "|" in p:
            errs.append(f"성격 이름 '{p}': 비어 있거나 | 를 포함할 수 없습니다")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            errs.append(f"성격 '{p}': 표정 이름 목록이어야 합니다")
            continue
        unknown = [n for n in names if n not in exprs]
        if unknown:
            errs.append(f"성격 '{p}': 정의되지 않은 표정 {', '.join(unknown)}")
    if x.get("default_personality") not in pers:
        errs.append("default_personality: personalities 중 하나여야 합니다")
    return errs


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
_shutdown = threading.Event()


@asynccontextmanager
async def lifespan(_app):
    if not QUEUE.thread.is_alive():
        QUEUE.start()
    yield
    _shutdown.set()


app = FastAPI(title="NPC 초상화 생성기", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"  # 수정한 JS/CSS 가 바로 반영되도록 (재검증)
    return resp


@app.get("/api/status")
def api_status():
    comfy = comfy_status()
    return {"app": APP_NAME, "comfy": comfy, "queue": QUEUE.summary(),
            "config_issues": config_issues(load_config()) if comfy["ok"] else [],
            "paths": {"config": str(Paths.config), "expressions": str(Paths.expressions),
                      "characters": str(Paths.characters), "output": str(out_root())}}


@app.get("/api/config")
def get_config():
    return load_config()


@app.put("/api/config")
def put_config(cfg: dict = Body(...)):
    errs = validate_config(cfg)
    if errs:
        raise HTTPException(400, {"message": "설정 값을 확인하세요", "errors": errs})
    save_json_with_backup(Paths.config, cfg)
    BUS.log("config.json 저장 (다음 작업부터 적용)")
    return {"ok": True}


@app.get("/api/expressions")
def get_expressions():
    return load_expressions()


@app.put("/api/expressions")
def put_expressions(x: dict = Body(...)):
    errs = validate_expressions(x)
    if errs:
        raise HTTPException(400, {"message": "표정 설정을 확인하세요", "errors": errs})
    save_json_with_backup(Paths.expressions, x)
    BUS.log("expressions.json 저장 (다음 작업부터 적용)")
    return {"ok": True}


@app.get("/api/characters")
def get_characters():
    """행 배열. 헤더 X-Version(파일 수정 시각)과 X-Columns(열 순서)를 같이 보냄"""
    with CSV_LOCK:
        try:
            rows = load_rows()
        except ValueError as e:
            raise HTTPException(400, str(e))
        version = csv_version()
    cols = csv_columns()
    clean = [{c: (r.get(c) or "").strip() for c in cols} for r in rows]
    for r in clean:
        r["image_type"] = image_type(r)
    return JSONResponse(clean, headers={"X-Version": version, "X-Columns": json.dumps(cols)})


@app.put("/api/characters")
def put_characters(request: Request, rows: list = Body(...)):
    """표 전체 저장. X-Base-Version 이 지금 파일과 다르면(밖에서 고쳐짐) 409"""
    base = request.headers.get("X-Base-Version")
    if not all(isinstance(r, dict) for r in rows):
        raise HTTPException(400, "행은 객체여야 합니다")
    rows = [{str(k): ("" if v is None else str(v)).strip() for k, v in r.items() if not str(k).startswith("_")}
            for r in rows]
    errs, warns = validate_rows(rows, load_expressions(), load_config())
    if errs:
        raise HTTPException(400, {"message": "캐릭터 표를 확인하세요", "errors": errs[:50], "warnings": warns[:50]})
    for row in rows:
        row["image_type"] = image_type(row)
    with CSV_LOCK:
        if base and base != csv_version():
            raise HTTPException(409, "그 사이에 characters.csv 가 바뀌었습니다 (다른 창, 엑셀, seed 기록 등). "
                                     "[다시 불러오기] 후 고쳐 주세요.")
        version = save_rows(rows)
    BUS.log(f"characters.csv 저장 ({len(rows)}명)")
    BUS.publish({"type": "characters", "version": version, "changes": None})
    return {"ok": True, "version": version, "warnings": warns[:50], "count": len(rows)}


class CharacterGenerationRequest(BaseModel):
    description: StrictStr
    count: conint(strict=True, ge=1, le=50) = 5
    image_type: StrictStr = "character"


CHARACTER_GENERATION_LOCK = threading.Lock()


@app.post("/api/characters/generate")
def generate_characters(body: CharacterGenerationRequest):
    if body.image_type not in TYPES:
        raise HTTPException(400, "이미지 유형을 확인하세요")
    description = body.description.strip()
    if not description or len(description) > 10000:
        raise HTTPException(400, "캐릭터 설명을 1~10000자로 입력하세요.")
    if not CHARACTER_GENERATION_LOCK.acquire(blocking=False):
        raise HTTPException(409, "이미 캐릭터 설정을 생성 중입니다. 완료 후 다시 시도하세요.")
    try:
        if body.image_type == "monster":
            rows = CG.generate_monsters(description, body.count)
        else:
            rows = CG.generate_characters(description, body.count, list(load_expressions().get("personalities", {})))
        for row in rows:
            row["image_type"] = body.image_type
        return {"rows": rows}
    except CG.GenerationError as exc:
        raise HTTPException(exc.status, str(exc))
    finally:
        CHARACTER_GENERATION_LOCK.release()


@app.post("/api/characters/import")
async def import_characters(request: Request):
    """CSV 파일 내용(바이트)을 그대로 받아 행으로 바꿔 돌려줌. 저장은 하지 않음 (화면에서 확인 후 PUT)"""
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "빈 파일입니다")
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(400, "파일이 너무 큽니다 (20MB 초과)")
    try:
        text, enc = decode_csv(raw)
        cols, rows = parse_csv(text)
    except (ValueError, csv.Error) as e:
        raise HTTPException(400, str(e))
    errs, warns = validate_rows(rows, load_expressions(), load_config())
    unknown_cols = [c for c in cols if c not in STD_COLUMNS]
    return {"rows": rows, "columns": cols, "encoding": enc, "errors": errs[:50], "warnings": warns[:50],
            "unknown_columns": unknown_cols}


@app.get("/api/characters/export")
def export_characters():
    """저장된 표를 엑셀에서 바로 열리는 CSV(UTF-8 BOM)로 내려받기"""
    with CSV_LOCK:
        rows = load_rows()
    rows = [{**r, "image_type": image_type(r)} for r in rows]
    cols = csv_columns()
    data = csv_text([{c: (r.get(c) or "").strip() for c in cols} for r in rows], cols).encode("utf-8-sig")
    name = time.strftime("characters_%Y%m%d_%H%M.csv")
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/comfy/models")
def comfy_models(url: Optional[str] = None):
    """url 을 주면 (아직 저장하지 않은) 그 주소의 ComfyUI 에서 목록을 받음. poses 는 이 PC 의 포즈 템플릿 폴더"""
    cfg = load_config()
    return {**fetch_models(url or cfg["comfy_url"], use_cache=False), "poses": pose_names(cfg)}


def pose_names(cfg):
    d = P.pose_dir(cfg)
    return sorted(p.name for p in d.glob("*.png")) if d.is_dir() else []


@app.get("/api/poses")
def list_poses():
    """ControlNet 포즈 템플릿 목록 (캐릭터 표 pose 칸 자동 완성용)"""
    cfg = load_config()
    cc = P.controlnet_cfg(cfg)
    return {"enabled": cc["enabled"], "default": cc["pose"], "dir": str(P.pose_dir(cfg)), "poses": pose_names(cfg)}


@app.post("/api/comfy/test")
def comfy_test(body: dict = Body(default={})):
    url = (body or {}).get("url") or None
    return comfy_status(url, use_cache=False)


class JobRequest(BaseModel):
    ids: List[str]
    mode: str
    expression: Optional[str] = None
    denoise: Optional[float] = None
    new_seed: bool = False
    force: bool = False


@app.post("/api/jobs")
def create_jobs(req: JobRequest):
    if req.mode not in CHAR_MODES:
        raise HTTPException(400, "mode 는 base / full / expressions / expression / layers 중 하나여야 합니다")
    if req.mode == "expression" and not req.expression:
        raise HTTPException(400, "expression 모드에는 표정 이름이 필요합니다")
    if req.denoise is not None and not 0 < req.denoise <= 1:
        raise HTTPException(400, "denoise 는 0~1 사이여야 합니다")
    if not req.ids:
        raise HTTPException(400, "캐릭터를 하나 이상 선택하세요")
    rows = {r["id"].strip(): r for r in load_rows()}
    missing = [i for i in req.ids if i not in rows]
    if missing:
        raise HTTPException(400, f"characters.csv 에 없는 id: {', '.join(missing[:10])}")
    for cid in req.ids:
        validate_type_job(rows[cid], req.mode)
        if req.mode in ("expressions", "layers") and not (out_root() / cid / "base_src.png").exists():
            raise HTTPException(400, "먼저 이미지를 생성하고 검수하세요")
        if req.mode in ("expressions", "expression", "layers") and portrait_summary(cid, rows[cid])["stale"]:
            raise HTTPException(400, "이미지 유형이 변경되었습니다. 먼저 이미지를 다시 생성하세요")
        cfg = effective_config(load_config(), rows[cid])
        if req.mode in ("base", "full") and P.controlnet_cfg(cfg)["enabled"]:
            try:
                chosen = (rows[cid].get("pose") or "").strip()
                if chosen and not P.pose_path(cfg, chosen).is_file():
                    raise P.ConfigError(f"선택한 포즈 '{chosen}' 파일이 없습니다")
                P.pose_file(cfg, rows[cid])
                models = fetch_models(cfg["comfy_url"])
                if models["ok"] and P.controlnet_cfg(cfg)["model"] not in models.get("controlnets", []):
                    raise P.ConfigError("ControlNet 모델이 없습니다. 설정에서 모델을 선택하세요")
            except P.ConfigError as exc:
                raise HTTPException(400, str(exc))
    specs = [(rows[i], req.mode, req.expression, req.denoise, req.new_seed, req.force) for i in dict.fromkeys(req.ids)]
    added, dup = QUEUE.submit(specs)
    if added:
        BUS.log(f"{MODE_LABEL[req.mode]} {len(added)}건을 큐에 추가" + (f" (이미 대기 중 {dup}건 제외)" if dup else ""))
    return {"added": [j.id for j in added], "duplicates": dup}


def validate_type_job(row, mode):
    kind = image_type(row)
    if mode in ("expression", "expressions") and kind != "character":
        raise HTTPException(400, "주인공/조연만 표정을 생성할 수 있습니다")
    if mode == "layers" and kind == "monster":
        raise HTTPException(400, "몬스터는 레이어 분리를 지원하지 않습니다")
    if kind == "monster" and not (row.get("prompt") or "").strip():
        raise HTTPException(400, "몬스터 설명 태그를 입력하세요")


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": QUEUE.list(), "queue": QUEUE.summary()}


@app.post("/api/jobs/pause")
def pause_jobs():
    QUEUE.set_paused(True)
    return QUEUE.summary()


@app.post("/api/jobs/resume")
def resume_jobs():
    QUEUE.set_paused(False)
    return QUEUE.summary()


@app.post("/api/jobs/cancel")
def cancel_jobs(body: dict = Body(default={})):
    ids = (body or {}).get("job_ids") or None
    return {"cancelled": QUEUE.cancel(ids)}


@app.post("/api/jobs/clear")
def clear_jobs():
    QUEUE.clear_finished()
    return {"ok": True}


@app.post("/api/jobs/retry")
def retry_jobs(body: dict = Body(default={})):
    """실패/취소된 작업을 같은 조건으로 다시 큐에 넣음"""
    want = set((body or {}).get("job_ids") or [])
    rows = {r["id"].strip(): r for r in load_rows()}
    try:
        srcs = {s["id"]: parts_row(s) for s in load_parts()["sources"]}
    except HTTPException:
        srcs = {}
    row_for = lambda j: (srcs if j.mode in PARTS_MODES else rows).get(j.cid)
    with QUEUE.cond:
        old = [j for j in QUEUE.jobs.values()
               if j.status in ("failed", "cancelled") and (not want or j.id in want) and row_for(j)]
    specs = [retry_spec(j, row_for(j)) for j in old]
    added, _ = QUEUE.submit(specs)
    with QUEUE.cond:
        for j in old:
            QUEUE.jobs.pop(j.id, None)
    BUS.publish({"type": "jobs_reset"})
    return {"added": [j.id for j in added]}


def retry_spec(job, row):
    mode, new_seed, force = job.mode, job.new_seed, job.force
    if mode == "full" and job.retry_stage in ("layers", "expressions"):
        mode = "layers" if job.retry_stage == "layers" else "full"
        new_seed, force = False, False
    return row, mode, job.expression, job.denoise, new_seed, force


def sse(ev):
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


@app.get("/api/events")
async def events(request: Request):
    loop = asyncio.get_running_loop()
    q, snap = BUS.subscribe(loop)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            yield sse({"type": "snapshot", **snap, "queue": QUEUE.summary()})
            while not _shutdown.is_set():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=10)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": ping\n\n"
                    continue
                yield sse(ev)
        finally:
            BUS.unsubscribe(loop, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/portraits")
def list_portraits():
    try:
        return all_portraits()
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/portraits/{cid}")
def get_portrait(cid: str):
    check_id(cid)
    rows = {r["id"].strip(): r for r in load_rows()}
    row = rows.get(cid)
    out = out_root() / cid
    if row is None and not out.exists():
        raise HTTPException(404, f"'{cid}' 캐릭터가 없습니다")
    summary = portrait_summary(cid, row)
    meta = read_meta(out / "meta.json")
    if summary["stale"]:
        meta = None
    work = P.read_json(out / "work.json")
    base_rel = "base.png" if (out / "base.png").exists() and meta else "base_src.png"
    size = None
    if meta:
        size = [meta["width"], meta["height"]]
    elif (out / base_rel).exists():
        with Image.open(out / base_rel) as im:
            size = list(im.size)
    exprs = []
    for e in (meta or {}).get("expressions", []):
        exprs.append({**e, "url": file_url(cid, e["file"]) if e.get("file") else None})
    planned = []
    cfg, ex = load_config(), load_expressions()
    if row is not None and image_type(row) == "character":
        planned = P.expression_list(row, ex, warn=lambda m: None)
    # 표정별 기본 denoise (재생성 슬라이더 초기값)
    names = dict.fromkeys([e["name"] for e in exprs] + planned)
    defaults = {n: P.expression_spec(n, ex, cfg, warn=lambda m: None)[1] for n in names if n != "neutral"}
    return {**summary, "status_label": STATUS_LABEL[summary["status"]], "row": row, "meta": meta,
            "work": work, "size": size, "base_url": file_url(cid, base_rel), "expressions": exprs,
            "planned": planned, "denoise_defaults": defaults,
            "geometry_cfg": {k: cfg["expression"].get(k) for k in P.EXPR_GEOMETRY_KEYS},
            "seed": (meta or {}).get("seed", (work or {}).get("seed")),
            "approved_at": (meta or {}).get("approved_at")}


@app.post("/api/portraits/{cid}/approve")
def approve_portrait(cid: str, body: dict = Body(default={})):
    """meta.json 에 "approved": true 기록 ({"approved": false} 면 승인 취소)"""
    check_id(cid)
    approved = bool((body or {}).get("approved", True))
    meta_path = out_root() / cid / "meta.json"
    with QUEUE.cond:
        busy = (QUEUE.current and QUEUE.current.cid == cid) or any(QUEUE.jobs[i].cid == cid for i in QUEUE.pending)
    if busy and approved:
        raise HTTPException(409, "이 캐릭터는 생성 중이거나 대기 중입니다. 끝난 뒤 승인하세요.")
    meta = P.read_json(meta_path)
    row = next((r for r in load_rows() if r["id"].strip() == cid), None)
    summary = portrait_summary(cid, row)
    if approved and summary["status"] not in ("done", "approved"):
        raise HTTPException(400, "현재 유형의 필수 작업을 완료한 뒤 승인하세요")
    if meta is None:
        raise HTTPException(400, "완료된 이미지만 승인할 수 있습니다")
    meta["approved"] = approved
    if approved:
        meta["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        meta.pop("approved_at", None)
    P.write_json(meta_path, meta)
    status = portrait_summary(cid, row)["status"]
    BUS.log(f"{cid}: {'승인' if approved else '승인 취소'}", cid=cid)
    BUS.publish({"type": "portrait", "id": cid, "status": status})
    return {"ok": True, "status": status}


@app.get("/api/portraits/{cid}/layers.psd")
def character_psd(cid: str):
    check_id(cid)
    row = next((r for r in load_rows() if r["id"].strip() == cid), None)
    if not row or not character_layers(cid, image_type(row))["current"]:
        raise HTTPException(400, "현재 이미지의 레이어를 먼저 분리하세요")
    data = PT.make_psd(out_root() / "_layers" / cid)
    return Response(data, media_type="image/vnd.adobe.photoshop",
                    headers={"Content-Disposition": f'attachment; filename="layers.psd"'})


class PortraitDeleteRequest(BaseModel):
    ids: List[StrictStr]


def checked_delete_dir(root, target):
    """Validate every directory before recursive deletion; never follow reparse points."""
    root, target = Path(root).absolute(), Path(target).absolute()
    if root.resolve() == target.resolve() or root.resolve() not in target.resolve().parents:
        raise HTTPException(400, "삭제 경로가 결과 폴더 범위를 벗어납니다")
    for path in (root, *target.relative_to(root).parents):
        candidate = path if path == root else root / path
        if candidate.exists() and (candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)()):
            raise HTTPException(400, "링크된 폴더는 삭제할 수 없습니다")
    if target.is_symlink() or getattr(target, "is_junction", lambda: False)():
        raise HTTPException(400, "링크된 폴더는 삭제할 수 없습니다")
    if not target.exists():
        return None
    if not target.is_dir():
        raise HTTPException(400, "결과 경로가 폴더가 아닙니다")
    for base, dirs, files in os.walk(target, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise HTTPException(400, "결과 폴더 안의 링크를 먼저 제거하세요")
    return target


def portrait_delete_paths(cid, sources, spec):
    """Only collect managed output, including previous layer/import generations."""
    root, proot = out_root(), PT.out_root(spec)
    paths = []

    def add(parent, path, sentinels):
        path = checked_delete_dir(parent, path)
        if path is not None:
            retry = P.read_json(path / ".delete.json", {}).get("portrait_id") == cid
            if not retry and not any((path / name).is_file() for name in sentinels):
                if any(path.iterdir()):
                    raise HTTPException(400, f"생성 결과로 확인할 수 없는 폴더입니다: {path.name}")
            paths.append(path)

    add(root, root / cid, ("base_src.png", "meta.json", "work.json"))
    add(root, root / "_layers" / cid, ("source.png", "layers.json", "provenance.json"))
    history = root / "_layers" / ".history"
    if history.exists():
        checked_delete_dir(root, history)
        for d in history.iterdir():
            if d.is_dir() and (P.read_json(d / "layers.json", {}).get("id") == cid or
                               P.read_json(d / ".delete.json", {}).get("portrait_id") == cid):
                add(root, d, ("layers.json",))
    staging = root / "_layers" / ".staging"
    if staging.exists():
        checked_delete_dir(root, staging)
        for batch in staging.iterdir():
            if batch.is_dir():
                add(root, batch / cid, ("source.png", "source.json", "layers.json"))
    linked = {s["id"] for s in sources if s.get("from") == cid}
    if proot.exists():
        checked_delete_dir(proot.parent, proot)
        for d in proot.iterdir():
            if d.name.startswith(".") or not d.is_dir():
                continue
            info = P.read_json(d / "source.json", {})
            if d.name in linked or info.get("from") == cid or P.read_json(d / ".delete.json", {}).get("portrait_id") == cid:
                linked.add(d.name)
                add(proot, d, ("source.png", "source.json", "layers.json"))
        retired = proot / ".deleted"
        if retired.exists():
            checked_delete_dir(proot, retired)
            for d in retired.iterdir():
                if not d.is_dir():
                    continue
                info = P.read_json(d / "source.json", {})
                layer_id = P.read_json(d / "layers.json", {}).get("id")
                if info.get("from") == cid or info.get("id") in linked or layer_id in linked or P.read_json(d / ".delete.json", {}).get("portrait_id") == cid:
                    add(proot, d, ("source.png", "source.json", "layers.json"))
    return list(dict.fromkeys(paths)), linked


@app.post("/api/portraits/delete")
def delete_portraits(body: PortraitDeleteRequest, request: Request):
    ids = list(dict.fromkeys(check_id(cid) for cid in body.ids))
    if not ids or len(ids) > 1000:
        raise HTTPException(400, "삭제할 항목을 1~1000개 선택하세요")
    # Keep producers and the exporter from acquiring files during the transaction.
    with EXPORTER.lock, QUEUE.cond, CSV_LOCK, PARTS_LOCK:
        if EXPORTER.state.get("running"):
            raise HTTPException(409, "Unity 내보내기가 끝난 뒤 삭제하세요")
        base = request.headers.get("X-Base-Version")
        if base and base != csv_version():
            raise HTTPException(409, "캐릭터 데이터가 변경되었습니다. 다시 불러온 뒤 삭제하세요")
        rows, spec = load_rows(), load_parts()
        sources = spec.get("sources", [])
        known = {p["id"] for p in all_portraits()}
        if any(cid not in known for cid in ids):
            raise HTTPException(404, "이미 삭제되었거나 없는 항목이 포함되어 있습니다. 목록을 새로 불러오세요")
        plans = {cid: portrait_delete_paths(cid, sources, spec) for cid in ids}
        affected = set(ids) | {sid for _, linked in plans.values() for sid in linked}
        busy = [j for j in QUEUE.jobs.values() if j.cid in affected and j.status in ("pending", "running")]
        if busy:
            raise HTTPException(409, "선택한 항목 또는 연결된 파츠의 생성 작업이 대기·진행 중입니다. 작업을 끝내거나 취소한 뒤 삭제하세요")
        staged = {cid: [] for cid in ids}

        def restore(cid, retry=False):
            for original, temporary in reversed(staged[cid]):
                if temporary.exists():
                    temporary.rename(original)
                    if retry:
                        P.write_json(original / ".delete.json", {"portrait_id": cid})

        def write_data(deleted):
            deleted = set(deleted)
            linked = {sid for cid in deleted for sid in plans[cid][1]}
            remaining_rows = [r for r in rows if r["id"].strip() not in deleted]
            remaining_sources = [s for s in sources if s["id"] not in linked and s.get("from") not in deleted]
            if load_rows() != remaining_rows:
                save_rows(remaining_rows)
            if load_parts().get("sources", []) != remaining_sources:
                save_parts_sources(lambda _: remaining_sources)

        try:
            for cid in ids:
                for path in plans[cid][0]:
                    temporary = path.with_name(f".purge-{uuid.uuid4().hex}")
                    path.rename(temporary)
                    staged[cid].append((path, temporary))
            write_data(ids)
        except (OSError, HTTPException) as exc:
            for cid in ids:
                restore(cid)
            # Restore metadata if one of the two metadata saves failed.
            if load_rows() != rows:
                save_rows(rows)
            if load_parts().get("sources", []) != sources:
                save_parts_sources(lambda _: sources)
            raise HTTPException(409, f"삭제하지 못했습니다. 열린 파일을 닫고 다시 시도하세요: {exc}")
        deleted, failed = [], []
        for cid in ids:
            try:
                for _, temporary in staged[cid]:
                    # The original tree was fully checked before the rename.
                    checked_delete_dir(temporary.parent, temporary)
                    shutil.rmtree(temporary)
                deleted.append(cid)
            except (OSError, HTTPException) as exc:
                restore(cid, retry=True)
                failed.append({"id": cid, "error": f"일부 파일 삭제 실패: {exc}"})
        if failed:
            write_data(deleted)
        removed_jobs = {cid for cid in deleted} | {sid for cid in deleted for sid in plans[cid][1]}
        QUEUE.jobs = {jid: j for jid, j in QUEUE.jobs.items() if j.cid not in removed_jobs}
        _meta_cache.clear()
        _thumbs.clear()
    with BUS.lock:
        BUS.images = deque((ev for ev in BUS.images if ev.get("id") not in removed_jobs), maxlen=BUS.images.maxlen)
    BUS.publish({"type": "characters", "version": csv_version(), "changes": None})
    BUS.publish({"type": "portraits_deleted", "ids": deleted})
    BUS.publish({"type": "parts", "id": None})
    BUS.publish({"type": "jobs_reset"})
    QUEUE.publish_queue()
    BUS.log(f"검수 항목 {len(deleted)}개와 생성 파일을 영구 삭제했습니다")
    return {"deleted": deleted, "failed": failed}


_masks = OrderedDict()  # (cid, work.json mtime) -> png bytes


@app.get("/api/portraits/{cid}/mask.png")
def debug_mask(cid: str):
    """디버그 오버레이용: 표정을 합성할 때 쓰는 페더 마스크를 반투명 빨강으로 (work.json 으로 다시 계산)"""
    check_id(cid)
    path = out_root() / cid / "work.json"
    work = P.read_json(path)
    if not work or "ellipse" not in work:
        raise HTTPException(404, "얼굴 정보(work.json)가 없습니다. 전체 생성을 한 번 해야 합니다.")
    key = (cid, path.stat().st_mtime_ns)
    data = _masks.get(key)
    if data is None:
        size = (work["width"], work["height"])
        soft, _ = P.make_masks(size, tuple(work["ellipse"]), tuple(work["crop"]), work["expression_cfg"])
        layer = Image.new("RGBA", size, (255, 40, 90, 0))
        layer.putalpha(soft.point(lambda v: v * 115 // 255))
        buf = io.BytesIO()
        layer.save(buf, "PNG", optimize=True)
        data = buf.getvalue()
        _masks[key] = data
        while len(_masks) > 50:
            _masks.popitem(last=False)
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------------- #
# 승인된 것만 Unity 폴더로 내보내기
# --------------------------------------------------------------------------- #
class UnityExporter:
    """output/<id>/ 의 base.png, expr/*.png(meta 에 있는 것), meta.json 만 복사.
    - 바뀌지 않은 파일은 건너뜀 (크기+수정 시각) → 다시 내보내기가 빠름
    - 파일을 지웠다 새로 만들지 않고 덮어써서 Unity 의 .meta(GUID)를 유지"""

    def __init__(self, bus):
        self.bus = bus
        self.lock = threading.Lock()
        self.state = {"running": False}

    def publish(self, **kw):
        self.state.update(kw)
        self.bus.publish({"type": "export", **self.state})

    def start(self, dest, prune):
        with self.lock:
            if self.state.get("running"):
                raise HTTPException(409, "이미 내보내는 중입니다")
            self.state = {"running": True, "dest": str(dest), "done": 0, "total": 0,
                          "copied": 0, "skipped": 0, "removed": 0, "error": None}
        threading.Thread(target=self._run, args=(Path(dest), prune), daemon=True).start()

    @staticmethod
    def _same(src, dst):
        try:
            a, b = src.stat(), dst.stat()
        except FileNotFoundError:
            return False
        return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)

    @staticmethod
    def _remove(path):
        """파일과 Unity 가 만든 짝 .meta 를 함께 삭제"""
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        path.with_name(path.name + ".meta").unlink(missing_ok=True)

    def _run(self, dest, prune):
        try:
            root = out_root()
            approved = []
            rows = {r["id"].strip(): r for r in load_rows()}
            if root.exists():
                for d in sorted(root.iterdir()):
                    meta = read_meta(d / "meta.json") if d.is_dir() else None
                    if meta and meta.get("approved") and (d / "base.png").exists() and portrait_summary(d.name, rows.get(d.name))["status"] == "approved":
                        approved.append((d, meta))
            self.publish(total=len(approved))
            dest.mkdir(parents=True, exist_ok=True)
            copied = skipped = removed = 0
            for i, (src_dir, meta) in enumerate(approved, 1):
                dst_dir = dest / src_dir.name
                (dst_dir / "expr").mkdir(parents=True, exist_ok=True)
                files = ["base.png", "meta.json"] + [e["file"] for e in meta["expressions"] if e.get("file")]
                for rel in files:
                    src, dst = src_dir / rel, dst_dir / rel
                    if not src.exists():
                        continue
                    if self._same(src, dst):
                        skipped += 1
                    else:
                        shutil.copy2(src, dst)
                        copied += 1
                # 표정 목록이 바뀌어 더는 meta 에 없는 오버레이 정리
                keep = {Path(f).name for f in files if f.startswith("expr/")}
                for p in (dst_dir / "expr").glob("*.png"):
                    if p.name not in keep:
                        self._remove(p)
                        removed += 1
                if i % 10 == 0 or i == len(approved):
                    self.publish(done=i, copied=copied, skipped=skipped, removed=removed)
            if prune:  # 승인이 풀렸거나 없어진 캐릭터 폴더 (우리가 내보낸 것만: meta.json 의 id 가 폴더 이름과 같음)
                ids = {d.name for d, _ in approved}
                for d in dest.iterdir():
                    if d.is_dir() and d.name not in ids:
                        m = P.read_json(d / "meta.json") if (d / "meta.json").exists() else None
                        if isinstance(m, dict) and m.get("id") == d.name:
                            self._remove(d)
                            removed += 1
            self.publish(running=False, done=len(approved), copied=copied, skipped=skipped, removed=removed)
            self.bus.log(f"Unity 내보내기 완료 → {dest}  (캐릭터 {len(approved)}명, 복사 {copied}, "
                         f"변경 없음 {skipped}, 삭제 {removed})")
        except Exception as e:
            traceback.print_exc()
            self.publish(running=False, error=str(e))
            self.bus.log(f"Unity 내보내기 실패: {e}", "error")


EXPORTER = UnityExporter(BUS)


def check_export_dest(dest):
    if not dest or not dest.strip():
        raise HTTPException(400, "내보낼 폴더를 입력하세요")
    p = Path(dest.strip())
    if not p.is_absolute():
        raise HTTPException(400, "절대 경로를 입력하세요 (예: D:\\MyGame\\Assets\\Resources\\Portraits)")
    p = p.resolve()
    root = out_root().resolve()
    if p == root or root in p.parents or p in root.parents:
        raise HTTPException(400, "출력 폴더(output)와 겹치는 위치로는 내보낼 수 없습니다")
    if not p.exists() and not p.parent.exists():
        raise HTTPException(400, f"상위 폴더가 없습니다: {p.parent}")
    return p


@app.get("/api/export/unity")
def export_info():
    cfg = load_config()
    approved = sum(p["status"] == "approved" for p in all_portraits())
    dest = cfg.get("unity_export_dir", "")
    warn = None
    if dest and "resources" not in [x.lower() for x in Path(dest).parts]:
        warn = "NpcPortrait.cs 는 Resources.Load 로 읽으므로 Assets/Resources 아래 폴더여야 합니다 (기본 Resources/Portraits)."
    return {"dest": dest, "approved": approved, "warning": warn, "state": EXPORTER.state}


@app.post("/api/export/unity")
def export_unity(body: dict = Body(default={})):
    dest = check_export_dest((body or {}).get("dest") or load_config().get("unity_export_dir", ""))
    cfg = load_config()
    if cfg.get("unity_export_dir") != str(dest):  # 다음에도 쓰도록 설정에 저장
        cfg["unity_export_dir"] = str(dest)
        save_json_with_backup(Paths.config, cfg)
    EXPORTER.start(dest, bool((body or {}).get("prune")))
    return {"ok": True, "dest": str(dest)}


# --------------------------------------------------------------------------- #
# 파츠 조합 (parts.json / parts_out). 생성은 같은 작업 큐에서 처리
# --------------------------------------------------------------------------- #
PARTS_LOCK = threading.RLock()


def load_parts():
    try:
        return PT.load_spec(Paths.parts)
    except FileNotFoundError:
        raise HTTPException(500, f"{Paths.parts.name} 이 없습니다")
    except (ValueError, KeyError) as e:   # JSON 문법 오류, SpecError
        raise HTTPException(500, f"{Paths.parts.name} 오류: {e}")


def parts_root():
    return PT.out_root(load_parts())


def parts_row(s):
    """작업 큐에 넣을 때 쓰는 행 (캐릭터 행과 같은 모양)"""
    tags = s["tags"]
    row = {"id": s["id"], "name": tags if len(tags) <= 40 else tags[:39] + "…"}
    if s.get("from"):
        row["from"] = s["from"]
    return row


def parts_file_url(sid, rel, root):
    p = root / sid / rel
    try:
        return f"/parts_out/{sid}/{rel}?v={p.stat().st_mtime_ns}"
    except FileNotFoundError:
        return None


@app.get("/api/parts")
def get_parts():
    spec = load_parts()
    root = PT.out_root(spec)
    sources = []
    for s in spec["sources"]:
        st = PT.source_state(root, s)
        src = parts_file_url(s["id"], "source.png", root)
        sources.append({**s, **st,
                        "thumb": f"/api/parts/thumb/{s['id']}?v={src.rsplit('=', 1)[1]}" if src else None,
                        "files": {"source": src, "check": parts_file_url(s["id"], "check.png", root),
                                  "sheet": parts_file_url(s["id"], "sheet.png", root)}})
    return {"sources": sources,
            "groups": [{"key": k, "label": PT.GROUP_LABEL[k]} for k in PT.GROUPS],
            "spec_file": str(Paths.parts), "out_dir": str(root)}


def clean_source(s):
    """표에서 받은 소스 1개 → parts.json 에 쓸 모양 (생성 이미지에서 가져온 소스는 from / align 유지)"""
    if not isinstance(s, dict):
        return s
    out = {"id": str(s.get("id", "")).strip(), "seed": s.get("seed"), "tags": str(s.get("tags", "")).strip()}
    if s.get("from") is not None:
        out["from"] = s["from"].strip() if isinstance(s["from"], str) else s["from"]
        out["align"] = s.get("align", True)
    return out


def save_parts_sources(update):
    """parts.json 의 sources 만 바꿔 저장. update(지금 sources) → 새 sources. 다른 설정은 그대로"""
    with PARTS_LOCK:
        try:
            raw = json.loads(Paths.parts.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(500, f"{Paths.parts.name} 을 읽지 못했습니다: {e}")
        sources = update(raw.get("sources") or [])
        errs = PT.validate_sources(sources)
        if errs:
            raise HTTPException(400, {"message": "저장하지 못했습니다: " + errs[0], "errors": errs})
        raw["sources"] = sources
        try:
            save_json_with_backup(Paths.parts, raw)
        except PermissionError:
            raise HTTPException(409, f"{Paths.parts.name} 이 다른 프로그램에서 열려 있어 저장하지 못했습니다")
    return sources


def retire_parts_dirs(ids):
    """소스 결과 폴더를 parts_out/.deleted/<id>-<시각> 으로 옮김 (지우지 않음, 필요하면 거기서 되살림).
    목록에서 뺀 소스와, 새 소스 id 에 남아 있던 예전 결과가 대상 → 같은 id 로 다시 만들어도 예전 그림이 나오지 않음"""
    root = parts_root()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for sid in ids:
        d = root / sid
        if not d.is_dir():
            continue
        dst, n = root / ".deleted" / f"{sid}-{stamp}", 2
        while dst.exists():
            dst, n = root / ".deleted" / f"{sid}-{stamp}-{n}", n + 1
        try:
            dst.parent.mkdir(exist_ok=True)
            d.rename(dst)
            moved.append(sid)
        except OSError as e:   # 작업 중이거나 다른 프로그램이 파일을 잡고 있음
            BUS.log(f"{sid}: 예전 결과 폴더를 옮기지 못했습니다 ({e})", "warn")
    if moved:
        BUS.log(f"결과 폴더 {len(moved)}개를 {root.name}/.deleted 로 옮겼습니다: {', '.join(moved)}")
    return moved


@app.put("/api/parts/sources")
def put_parts_sources(body: dict = Body(...)):
    sources = body.get("sources")
    if isinstance(sources, list):
        sources = [clean_source(s) for s in sources]
    errs = PT.validate_sources(sources)
    if errs:
        raise HTTPException(400, {"message": "저장하지 못했습니다: " + errs[0], "errors": errs})
    old = set()

    def update(prev):
        old.update(s.get("id") for s in prev if isinstance(s, dict))
        return sources

    with PARTS_LOCK:
        save_parts_sources(update)
        new = {s["id"] for s in sources}
        retire_parts_dirs(sorted((old - new) | (new - old)))
    BUS.log(f"파츠 소스 목록을 저장했습니다 ({len(sources)}개)")
    BUS.publish({"type": "parts", "id": None})
    return get_parts()


class PartsImportRequest(BaseModel):
    ids: List[str]          # 캐릭터 id
    align: bool = True      # 얼굴 위치·크기를 다른 소스와 맞춤
    split: bool = False     # 가져온 뒤 바로 레이어 분리까지 큐에 넣음


def import_tags(row, cid):
    """가져온 소스의 태그 칸 (표시용. 생성에는 쓰지 않음)"""
    tags = ", ".join(v for v in ((row or {}).get(c, "").strip() for c in ("subject", "hair", "eyes", "outfit", "extra")) if v)
    return tags or (row or {}).get("name", "").strip() or cid


@app.post("/api/parts/import")
def import_parts(req: PartsImportRequest):
    """캐릭터 생성 결과(base)를 파츠 소스로 등록하고 가져오기(+분리) 작업을 큐에 넣음.
    소스 id 는 캐릭터 id. 같은 캐릭터를 다시 가져오면 기존 소스를 갱신 (다른 소스와 id 가 겹치면 _2, _3 …)"""
    ids = list(dict.fromkeys(check_id(c) for c in req.ids))
    if not ids:
        raise HTTPException(400, "가져올 캐릭터를 하나 이상 고르세요")
    root = out_root()
    missing = [c for c in ids if not (root / c / "base_src.png").exists()]
    if missing:
        raise HTTPException(400, f"base 를 아직 생성하지 않은 캐릭터: {', '.join(missing[:10])}")
    rows = {r["id"].strip(): r for r in load_rows()}
    if any(image_type(rows.get(c, {})) == "monster" for c in ids):
        raise HTTPException(400, "몬스터는 레이어 분리를 지원하지 않습니다")
    seeds = {c: (P.read_json(root / c / "work.json", {}) or {}).get("seed") for c in ids}
    sid_of, created = {}, []

    def update(sources):
        by_from = {s.get("from"): s for s in sources if s.get("from")}
        used = {s["id"] for s in sources}
        for c in ids:
            s = by_from.get(c)
            if s is None:
                base = re.sub(r"[^\w\-]", "_", c)
                sid, n = base, 2
                while sid in used:
                    sid, n = f"{base}_{n}", n + 1
                used.add(sid)
                seed = seeds[c]
                s = {"id": sid, "seed": seed if isinstance(seed, int) and 0 <= seed < 2 ** 32 else 0, "from": c}
                sources.append(s)
                created.append(sid)
            s["tags"] = import_tags(rows.get(c), c)
            s["align"] = req.align
            sid_of[c] = s["id"]
        return sources

    with PARTS_LOCK:
        sources = save_parts_sources(update)
        retire_parts_dirs(created)   # 예전에 지운 같은 id 소스의 레이어 등이 섞이지 않게
    by_id = {s["id"]: s for s in sources}
    picked = [by_id[sid_of[c]] for c in ids]
    # 가져오기는 GPU 를 쓰지 않으므로 먼저 전부 끝내고, 분리는 그 뒤에 몰아서 (SeeThrough 모델을 한 번만 올림)
    specs = [(parts_row(s), "parts_gen", None, None, False, True) for s in picked]
    if req.split:
        specs += [(parts_row(s), "parts_split", None, None, False, True) for s in picked]
    added, dup = QUEUE.submit(specs)
    BUS.log(f"생성 이미지 {len(ids)}명을 파츠 소스로 가져옵니다" + (" (레이어 분리까지)" if req.split else ""))
    BUS.publish({"type": "parts", "id": None})
    return {"sources": [s["id"] for s in picked], "added": [j.id for j in added], "duplicates": dup}


class PartsJobRequest(BaseModel):
    ids: List[str]
    mode: str
    force: bool = False


@app.post("/api/parts/jobs")
def create_parts_jobs(req: PartsJobRequest):
    mode = {"gen": "parts_gen", "split": "parts_split"}.get(req.mode)
    if not mode:
        raise HTTPException(400, "mode 는 gen / split 중 하나여야 합니다")
    if not req.ids:
        raise HTTPException(400, "소스를 하나 이상 선택하세요")
    srcs = {s["id"]: s for s in load_parts()["sources"]}
    missing = [i for i in req.ids if i not in srcs]
    if missing:
        raise HTTPException(400, f"parts.json 에 없는 id: {', '.join(missing[:10])}")
    rows = {r["id"].strip(): r for r in load_rows()}
    if any(image_type(rows.get(srcs[i].get("from"), {})) == "monster" for i in req.ids):
        raise HTTPException(400, "몬스터는 레이어 분리를 지원하지 않습니다")
    specs = [(parts_row(srcs[i]), mode, None, None, False, req.force) for i in dict.fromkeys(req.ids)]
    added, dup = QUEUE.submit(specs)
    if added:
        BUS.log(f"{MODE_LABEL[mode]} {len(added)}건을 큐에 추가" + (f" (이미 대기 중 {dup}건 제외)" if dup else ""))
    return {"added": [j.id for j in added], "duplicates": dup}


@app.get("/api/parts/compose")
def parts_compose(request: Request, w: int = 600):
    """?skin=s01&eyes=s03&hair=s02&outfit=s05&glasses= → 투명 배경 WEBP"""
    spec = load_parts()
    root = PT.out_root(spec)
    ids = {s["id"] for s in spec["sources"]}
    picks = {}
    for g in PT.GROUPS:
        v = (request.query_params.get(g) or "").strip()
        if v and v not in ids:
            raise HTTPException(400, f"{PT.GROUP_LABEL[g]}: parts.json 에 없는 id {v}")
        picks[g] = v or None
    if not any(picks.values()):
        raise HTTPException(400, "조합할 파츠를 하나 이상 고르세요")
    try:
        im = PT.compose(root, picks)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    w = max(64, min(w, im.width))
    if w != im.width:
        im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=90, method=4)
    return Response(buf.getvalue(), media_type="image/webp", headers={"Cache-Control": "no-cache"})


@app.get("/api/parts/psd/{sid}")
def parts_psd(sid: str, kind: str = "rgba"):
    """SeeThrough 레이어 → PSD. kind=rgba(색 레이어) / depth(깊이, 회색조)"""
    check_id(sid)
    if kind not in ("rgba", "depth"):
        raise HTTPException(400, "kind 는 rgba / depth 중 하나여야 합니다")
    d = parts_root() / sid
    try:
        # 깊이 레이어를 저장하기 전에 분리한 소스는 ComfyUI output 에서 받아 옴 (생성은 하지 않음)
        data = PT.make_psd(d, depth=kind == "depth", client=P.ComfyClient(load_config()["comfy_url"]))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except CONNECTION_ERRORS as e:
        raise HTTPException(503, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    fn = f"{sid}_depth.psd" if kind == "depth" else f"{sid}.psd"
    return Response(data, media_type="image/vnd.adobe.photoshop",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"', "Cache-Control": "no-cache"})


@app.get("/api/parts/thumb/{sid}")
def parts_thumb(sid: str, w: int = 160):
    check_id(sid)
    return webp_thumb(parts_root() / sid / "source.png", ("parts", sid), w)


@app.get("/parts_out/{path:path}")
def parts_file(path: str, v: Optional[str] = None):
    root = parts_root().resolve()
    p = (root / path).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404, "파일이 없습니다")
    cache = "public, max-age=31536000, immutable" if v else "no-cache"
    return FileResponse(p, headers={"Cache-Control": cache})


_thumbs = OrderedDict()  # (종류, w, mtime) -> webp bytes


def webp_thumb(src, kind, w):
    w = max(64, min(w, 832))
    if not src.exists():
        raise HTTPException(404, "이미지가 없습니다")
    key = (kind, w, src.stat().st_mtime_ns)
    data = _thumbs.get(key)
    if data is None:
        with Image.open(src) as im:
            im = im.convert("RGBA")
            im.thumbnail((w, w * 4), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=82, method=4)
            data = buf.getvalue()
        _thumbs[key] = data
        while len(_thumbs) > 3000:
            _thumbs.popitem(last=False)
    else:
        _thumbs.move_to_end(key)
    return Response(data, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/thumb/{cid}")
def thumb(cid: str, w: int = 320):
    check_id(cid)
    out = out_root() / cid
    src = out / "base.png" if (out / "base.png").exists() else out / "base_src.png"
    return webp_thumb(src, ("portrait", cid), w)


@app.get("/output/{path:path}")
def output_file(path: str, v: Optional[str] = None):
    root = out_root().resolve()
    p = (root / path).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404, "파일이 없습니다")
    cache = "public, max-age=31536000, immutable" if v else "no-cache"
    return FileResponse(p, headers={"Cache-Control": cache})


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    d = exc.detail
    body = d if isinstance(d, dict) else {"message": str(d)}
    return JSONResponse(body, status_code=exc.status_code)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --------------------------------------------------------------------------- #
def already_running(url):
    try:
        with urllib.request.urlopen(url + "/api/status", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == APP_NAME
    except Exception:
        return False


def open_browser_when_ready(url, port, host):
    def run():
        import socket
        for _ in range(100):
            try:
                with socket.create_connection((host, port), timeout=0.3):
                    break
            except OSError:
                time.sleep(0.1)
        webbrowser.open(url)
    threading.Thread(target=run, daemon=True).start()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="NPC 초상화 생성기 웹 UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7870)
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    ap.add_argument("--config", default=str(Paths.config))
    ap.add_argument("--expressions", default=str(Paths.expressions))
    ap.add_argument("--characters", default=str(Paths.characters))
    ap.add_argument("--parts", default=str(Paths.parts))
    a = ap.parse_args()
    Paths.config, Paths.expressions, Paths.characters = Path(a.config), Path(a.expressions), Path(a.characters)
    Paths.parts = Path(a.parts)

    url = f"http://{a.host}:{a.port}"
    if already_running(url):
        print(f"이미 실행 중입니다 → {url}")
        if not a.no_browser:
            webbrowser.open(url)
        return

    import uvicorn
    print(f"NPC 초상화 생성기 웹 UI: {url}   (종료: Ctrl+C)")
    print(f"  ComfyUI: {load_config()['comfy_url']}   출력 폴더: {out_root()}")
    if not a.no_browser:
        open_browser_when_ready(url, a.port, a.host)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning", timeout_graceful_shutdown=1)


if __name__ == "__main__":
    main()
