"""웹 UI 서버 종단 간 테스트 (ComfyUI 모의 서버 사용)

먼저 모의 서버를 띄웁니다:   python tests/fake_comfy.py
그 다음:                    python tests/test_web.py      (또는 python -m unittest tests.test_web -v)

임시 폴더에 config/expressions/characters 를 복사해 쓰므로 프로젝트의 output/ 은 건드리지 않습니다.
"""
import json
import csv
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = os.environ.get("FAKE_COMFY_URL", "http://127.0.0.1:8000")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method, url, body=None, timeout=10, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    hdrs = {"Content-Type": "application/json"} if data is not None and raw is None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
            return r.status, (json.loads(raw) if "json" in ctype else raw), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except ValueError:
            return e.code, raw, dict(e.headers)


class SSEReader(threading.Thread):
    def __init__(self, url):
        super().__init__(daemon=True)
        self.url, self.events, self.ok = url, [], threading.Event()

    def run(self):
        try:
            with urllib.request.urlopen(self.url, timeout=60) as r:
                self.ok.set()
                for line in r:
                    line = line.decode("utf-8").rstrip("\n")
                    if line.startswith("data: "):
                        self.events.append(json.loads(line[6:]))
        except Exception:
            pass

    def types(self):
        return [e["type"] for e in self.events]


class WebUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            http("GET", FAKE + "/system_stats", timeout=2)
        except Exception:
            raise unittest.SkipTest(f"모의 ComfyUI 가 꺼져 있습니다 ({FAKE}). 먼저 python tests/fake_comfy.py 를 실행하세요.")
        http("POST", FAKE + "/_fake/config", {"delay": 0})
        cls.tmp = Path(tempfile.mkdtemp(prefix="npcui_test_"))
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        # 모델 이름은 사용자의 실제 config 와 무관하게 모의 서버가 아는 것으로 고정
        cfg.update(comfy_url=FAKE, output_dir=str(cls.tmp / "out"), remove_background=False,
                   checkpoint="waiIllustriousSDXL_v150.safetensors", loras=[],
                   sampler="euler_ancestral", scheduler="normal")
        cfg["controlnet"].update(model="controlnet-union-sdxl-promax.safetensors", pose_dir=str(ROOT / "poses"))
        (cls.tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copy(ROOT / "expressions.json", cls.tmp / "expressions.json")
        shutil.copy(ROOT / "characters.csv", cls.tmp / "characters.csv")
        # This suite's scenarios use a stable five-character fixture.
        with (ROOT / "characters.csv").open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields, rows = reader.fieldnames, list(reader)[:5]
        with (cls.tmp / "characters.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        parts = json.loads((ROOT / "parts.json").read_text(encoding="utf-8"))
        parts.update(out_dir=str(cls.tmp / "parts_out"),
                     sources=[{"id": "p1", "seed": 11, "tags": "1girl, black hair"},
                              {"id": "p2", "seed": 22, "tags": "1girl, red hair, glasses"}])
        parts["seethrough"]["resolution"] = 512
        (cls.tmp / "parts.json").write_text(json.dumps(parts, ensure_ascii=False, indent=2), encoding="utf-8")
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        cls.log = open(cls.tmp / "server.log", "w", encoding="utf-8")
        cls.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py"), "--no-browser", "--port", str(cls.port),
             "--config", str(cls.tmp / "config.json"), "--expressions", str(cls.tmp / "expressions.json"),
             "--characters", str(cls.tmp / "characters.csv"), "--parts", str(cls.tmp / "parts.json")],
            cwd=ROOT, env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                if http("GET", cls.base + "/api/status", timeout=1)[0] == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("서버가 시작되지 않았습니다")
        cls.sse = SSEReader(cls.base + "/api/events")
        cls.sse.start()
        cls.sse.ok.wait(5)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "proc"):
            http("POST", FAKE + "/_fake/config", {"delay": 0})
            cls.proc.terminate()
            cls.proc.wait(10)
            cls.log.close()
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- 도우미 ---- #
    def api(self, method, path, body=None, expect=200):
        code, data, _ = http(method, self.base + path, body)
        self.assertEqual(code, expect, f"{method} {path} → {code} {data}")
        return data

    def wait_idle(self, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            q = self.api("GET", "/api/status")["queue"]
            if not q["running"] and q["counts"]["pending"] == 0:
                return q
            time.sleep(0.1)
        self.fail("큐가 끝나지 않았습니다")

    def wait_for(self, cond, timeout=10, msg="조건"):
        t0 = time.time()
        while time.time() - t0 < timeout:
            v = cond()
            if v:
                return v
            time.sleep(0.05)
        self.fail(f"{msg} 대기 시간 초과")

    def jobs(self, ids):
        by = {j["id"]: j for j in self.api("GET", "/api/jobs")["jobs"]}
        return [by[i] for i in ids]

    def out(self, cid):
        return self.tmp / "out" / cid

    # ---- 테스트 (이름 순서대로 실행) ---- #
    def test_01_status_and_static(self):
        st = self.api("GET", "/api/status")
        self.assertTrue(st["comfy"]["ok"])
        code, body, headers = http("GET", self.base + "/")
        self.assertEqual(code, 200)
        self.assertIn("게임 이미지 제작실".encode(), body)
        for f in ("app.js", "style.css"):
            self.assertEqual(http("GET", f"{self.base}/static/{f}")[0], 200)

    def test_02_config_roundtrip_and_validation(self):
        cfg = self.api("GET", "/api/config")
        bad = {**cfg, "width": 833, "steps": 0, "expression": {**cfg["expression"], "denoise": 1.5}}
        err = self.api("PUT", "/api/config", bad, expect=400)
        self.assertEqual(len(err["errors"]), 3, err)
        cfg["steps"] = 30
        cfg["loras"] = [{"name": "my_style.safetensors", "strength": 0.6}]
        self.api("PUT", "/api/config", cfg)
        saved = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["steps"], 30)
        self.assertEqual(saved["loras"][0]["name"], "my_style.safetensors")
        self.assertTrue((self.tmp / "config.json.bak").exists())

    def test_03_expressions_validation(self):
        ex = self.api("GET", "/api/expressions")
        bad = json.loads(json.dumps(ex))
        bad["personalities"]["명랑"].append("no_such_expr")
        err = self.api("PUT", "/api/expressions", bad, expect=400)
        self.assertTrue(any("no_such_expr" in e for e in err["errors"]))
        bad = json.loads(json.dumps(ex))
        bad["default_personality"] = "없는성격"
        self.api("PUT", "/api/expressions", bad, expect=400)
        ex["expressions"]["wink"] = {"tags": "one eye closed, smile", "denoise": 0.6}
        self.api("PUT", "/api/expressions", ex)
        self.assertIn("wink", self.api("GET", "/api/expressions")["expressions"])

    def test_04_models(self):
        m = self.api("GET", "/api/comfy/models")
        self.assertTrue(m["ok"])
        self.assertIn("waiIllustriousSDXL_v150.safetensors", m["checkpoints"])
        self.assertIn("my_style.safetensors", m["loras"])
        self.assertIn("euler_ancestral", m["samplers"])
        t = self.api("POST", "/api/comfy/test", {"url": "http://127.0.0.1:1"})
        self.assertFalse(t["ok"])

    def test_05_base_job(self):
        r = self.api("POST", "/api/jobs", {"ids": ["npc_0001", "npc_0002"], "mode": "base"})
        self.assertEqual(len(r["added"]), 2)
        self.wait_idle()
        jobs = self.jobs(r["added"])
        self.assertEqual([j["status"] for j in jobs], ["done", "done"])
        self.assertTrue((self.out("npc_0001") / "base_src.png").exists())
        self.assertFalse((self.out("npc_0001") / "meta.json").exists())
        p = self.api("GET", "/api/portraits/npc_0001")
        self.assertEqual(p["status"], "base")
        self.assertEqual(p["size"], [832, 1216])
        code, img, headers = http("GET", self.base + p["thumb"])
        self.assertEqual(code, 200)
        self.assertEqual(headers["content-type"], "image/webp")
        code, _, _ = http("GET", self.base + p["base_url"])
        self.assertEqual(code, 200)

    def test_06_full_job_and_sse(self):
        n0 = len(self.sse.events)
        r = self.api("POST", "/api/jobs", {"ids": ["npc_0001", "npc_0003"], "mode": "full"})
        q = self.wait_idle()
        j1, j3 = self.jobs(r["added"])
        self.assertEqual((j1["status"], j3["status"]), ("done", "done"))
        self.assertEqual(j1["done_images"], 7)   # base 재사용 + 표정 6 + 레이어
        self.assertEqual(j3["done_images"], 7)   # base + 표정 5 + 레이어
        self.assertGreater(q["avg_sec"], 0)

        meta = json.loads((self.out("npc_0001") / "meta.json").read_text(encoding="utf-8"))
        for k in ("id", "name", "width", "height", "face", "expressions"):   # Unity 가 쓰는 필드
            self.assertIn(k, meta)
        self.assertEqual(meta["expressions"][0], {"name": "neutral", "file": "", "x": 0, "y": 0, "w": 0, "h": 0})
        for e in meta["expressions"][1:]:
            self.assertTrue((self.out("npc_0001") / e["file"]).exists())
            for k in ("name", "file", "x", "y", "w", "h"):
                self.assertIn(k, e)
        work = json.loads((self.out("npc_0001") / "work.json").read_text(encoding="utf-8"))
        self.assertIn("crop", work)
        self.assertEqual(work["seed"], meta["seed"])

        p = self.api("GET", "/api/portraits/npc_0001")
        self.assertEqual(p["status"], "done")
        self.assertIn("base.png", p["base_url"])
        self.assertTrue(all(e["url"] for e in p["expressions"][1:]))
        self.assertEqual(http("GET", self.base + p["expressions"][1]["url"])[0], 200)

        summary = {x["id"]: x for x in self.api("GET", "/api/portraits")}
        self.assertEqual(summary["npc_0003"]["status"], "done")
        self.assertEqual(summary["npc_0004"]["status"], "none")

        self.wait_for(lambda: "portrait" in self.sse.types()[n0:], msg="SSE portrait")
        types = self.sse.types()[n0:]
        for t in ("job", "queue", "log", "image", "portrait"):
            self.assertIn(t, types)
        imgs = [e for e in self.sse.events[n0:] if e["type"] == "image"]
        self.assertTrue(imgs[0]["url"].startswith("/output/"))

    def test_07_skip_completed_and_duplicates(self):
        r = self.api("POST", "/api/jobs", {"ids": ["npc_0001"], "mode": "full"})
        self.wait_idle()
        (j,) = self.jobs(r["added"])
        self.assertEqual((j["status"], j["result"], j["done_images"]), ("done", "skipped", 0))
        err = self.api("POST", "/api/jobs", {"ids": ["nobody"], "mode": "full"}, expect=400)
        self.assertIn("nobody", err["message"])
        self.api("POST", "/api/jobs", {"ids": ["npc_0001"], "mode": "dance"}, expect=400)

    def test_08_pause_resume_cancel(self):
        http("POST", FAKE + "/_fake/config", {"delay": 0.25})
        try:
            interrupts0 = http("GET", FAKE + "/_fake/state")[1]["interrupts"]
            self.api("POST", "/api/jobs/pause")
            r = self.api("POST", "/api/jobs", {"ids": ["npc_0004", "npc_0005"], "mode": "full"})
            dup = self.api("POST", "/api/jobs", {"ids": ["npc_0004"], "mode": "full"})
            self.assertEqual(dup["duplicates"], 1)
            time.sleep(0.5)
            st = self.api("GET", "/api/status")["queue"]
            self.assertTrue(st["paused"])
            self.assertIsNone(st["running"])
            self.assertEqual(st["counts"]["pending"], 2)
            self.assertGreater(st["images_total"], 0)

            self.api("POST", "/api/jobs/resume")
            self.wait_for(lambda: self.jobs(r["added"])[0]["done_images"] >= 1, msg="첫 이미지")
            # 진행 중 일시정지: 현재 이미지까지만 만들고 멈춤
            self.api("POST", "/api/jobs/pause")
            time.sleep(0.8)
            n1 = self.jobs(r["added"])[0]["done_images"]
            time.sleep(0.8)
            self.assertEqual(self.jobs(r["added"])[0]["done_images"], n1, "일시정지 중에 이미지가 계속 생성됨")
            self.api("POST", "/api/jobs/resume")
            self.wait_for(lambda: self.jobs(r["added"])[0]["done_images"] > n1, msg="재개 후 진행")

            # 진행 중 취소 → ComfyUI /interrupt 호출
            res = self.api("POST", "/api/jobs/cancel", {})
            self.assertEqual(res["cancelled"], 2)
            self.wait_idle()
            j4, j5 = self.jobs(r["added"])
            self.assertEqual((j4["status"], j5["status"]), ("cancelled", "cancelled"))
            self.assertGreater(http("GET", FAKE + "/_fake/state")[1]["interrupts"], interrupts0)
            self.assertFalse((self.out("npc_0004") / "meta.json").exists())  # 중간에 끊긴 건 완료로 보이면 안 됨
            self.assertEqual(self.api("GET", "/api/portraits/npc_0004")["status"], "base")
        finally:
            http("POST", FAKE + "/_fake/config", {"delay": 0})
            self.api("POST", "/api/jobs/resume")

        # 다시 시도하면 이어서 완료
        retried = self.api("POST", "/api/jobs/retry", {"job_ids": r["added"]})
        self.assertEqual(len(retried["added"]), 2)
        self.wait_idle()
        self.assertEqual([j["status"] for j in self.jobs(retried["added"])], ["done", "done"])
        self.assertTrue((self.out("npc_0005") / "meta.json").exists())

    def test_09_single_expression(self):
        before = json.loads((self.out("npc_0001") / "meta.json").read_text(encoding="utf-8"))
        r = self.api("POST", "/api/jobs", {"ids": ["npc_0001"], "mode": "expression", "expression": "smile",
                                           "denoise": 0.7, "new_seed": True})
        self.wait_idle()
        (j,) = self.jobs(r["added"])
        self.assertEqual(j["status"], "done", j["error"])
        after = json.loads((self.out("npc_0001") / "meta.json").read_text(encoding="utf-8"))
        b = {e["name"]: e for e in before["expressions"]}
        a = {e["name"]: e for e in after["expressions"]}
        self.assertEqual(list(a), list(b))                 # 순서 유지
        self.assertEqual(a["smile"]["denoise"], 0.7)
        self.assertNotEqual(a["smile"]["seed"], before["seed"])
        self.assertEqual(a["grin"], b["grin"])             # 다른 표정은 그대로
        self.assertEqual((a["smile"]["x"], a["smile"]["y"]), (b["smile"]["x"], b["smile"]["y"]))

    def test_10_comfy_down_pauses_queue(self):
        cfg = self.api("GET", "/api/config")
        good = cfg["comfy_url"]
        cfg["comfy_url"] = "http://127.0.0.1:1"
        self.api("PUT", "/api/config", cfg)
        try:
            r = self.api("POST", "/api/jobs", {"ids": ["npc_0002"], "mode": "full"})
            self.wait_for(lambda: self.api("GET", "/api/status")["queue"]["paused"], msg="자동 일시정지")
            (j,) = self.jobs(r["added"])
            self.assertEqual(j["status"], "pending")
        finally:
            cfg["comfy_url"] = good
            self.api("PUT", "/api/config", cfg)
        self.api("POST", "/api/jobs/resume")
        self.wait_idle()
        self.assertEqual(self.jobs(r["added"])[0]["status"], "done")

    def test_10b_missing_checkpoint_pauses_queue(self):
        """config 의 체크포인트가 ComfyUI 에 없으면: 미리 경고 → 생성 시 실패시키지 않고 큐를 멈춤"""
        cfg = self.api("GET", "/api/config")
        good = cfg["checkpoint"]
        cfg["checkpoint"] = "not_installed.safetensors"
        self.api("PUT", "/api/config", cfg)
        try:
            issues = self.api("GET", "/api/status")["config_issues"]
            self.assertEqual(len(issues), 1, issues)
            self.assertIn("not_installed.safetensors", issues[0])
            r = self.api("POST", "/api/jobs", {"ids": ["npc_0002", "npc_0003"], "mode": "full", "force": True})
            q = self.wait_for(lambda: (lambda q: q if q["paused"] else None)(self.api("GET", "/api/status")["queue"]),
                              msg="자동 일시정지")
            self.assertIn("not_installed.safetensors", q["pause_reason"])
            self.assertIn("ComfyUI 에 없습니다", q["pause_reason"])
            self.assertEqual([j["status"] for j in self.jobs(r["added"])], ["pending", "pending"])
        finally:
            cfg["checkpoint"] = good
            self.api("PUT", "/api/config", cfg)
        self.assertEqual(self.api("GET", "/api/status")["config_issues"], [])
        self.api("POST", "/api/jobs/resume")
        self.wait_idle()
        self.assertEqual([j["status"] for j in self.jobs(r["added"])], ["done", "done"])
        self.assertIsNone(self.api("GET", "/api/status")["queue"]["pause_reason"])

    def test_10c_prompt_error_message(self):
        sys.path.insert(0, str(ROOT))
        import pipeline
        # 실제 ComfyUI 0.39 가 돌려준 응답
        body = json.dumps({"error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation",
                                     "details": "", "extra_info": {}},
                           "node_errors": {"1": {"errors": [{"type": "value_not_in_list", "message": "Value not in list",
                               "details": "ckpt_name: 'waiIllustriousSDXL_v150.safetensors' not in [...]",
                               "extra_info": {"input_name": "ckpt_name",
                                              "input_config": [["NetaYumev35_pretrained_all_in_one.safetensors",
                                                                "boleromixWAINSFW_v10.safetensors"], {"tooltip": "..."}],
                                              "received_value": "waiIllustriousSDXL_v150.safetensors"}}],
                               "dependent_outputs": ["8"], "class_type": "CheckpointLoaderSimple"}}})
        msg = pipeline.describe_prompt_error(body)
        self.assertIn("CheckpointLoaderSimple.ckpt_name: 'waiIllustriousSDXL_v150.safetensors' 이(가) ComfyUI 에 없습니다", msg)
        self.assertIn("boleromixWAINSFW_v10.safetensors", msg)
        self.assertIsNone(pipeline.describe_prompt_error("not json"))

    def test_11_path_safety(self):
        for path in ("/output/../config.json", "/output/..%2Fconfig.json", "/output/npc_0001/../../config.json"):
            self.assertEqual(http("GET", self.base + path)[0], 404, path)
        self.assertIn(http("GET", self.base + "/api/portraits/..%2F..")[0], (400, 404))

    def test_12_clear_finished(self):
        self.api("POST", "/api/jobs/clear")
        d = self.api("GET", "/api/jobs")
        self.assertEqual(d["jobs"], [])
        self.assertEqual(d["queue"]["images_total"], 0)

    def test_13_cli_still_works(self):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        p = subprocess.run([sys.executable, str(ROOT / "pipeline.py"), "--config", str(self.tmp / "config.json"),
                            "--expressions", str(self.tmp / "expressions.json"),
                            "--characters", str(self.tmp / "characters.csv")],
                           cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("[skip] npc_0001", p.stdout)
        self.assertNotIn("[실패]", p.stdout)
        for cid in ("npc_0001", "npc_0002", "npc_0003", "npc_0004", "npc_0005"):
            self.assertTrue((self.out(cid) / "meta.json").exists(), cid)


    def test_14_cli_stops_on_config_error(self):
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        cfg["checkpoint"] = "not_installed.safetensors"
        bad = self.tmp / "config_bad.json"
        bad.write_text(json.dumps(cfg), encoding="utf-8")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        p = subprocess.run([sys.executable, str(ROOT / "pipeline.py"), "--config", str(bad), "--force",
                            "--characters", str(self.tmp / "characters.csv")],
                           cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertIn("not_installed.safetensors' 이(가) ComfyUI 에 없습니다", p.stdout)
        self.assertIn("(1/5)", p.stdout)
        self.assertNotIn("(2/5)", p.stdout)   # 첫 캐릭터에서 멈춤


    # ---- Phase 2 ---- #
    def get_chars(self):
        code, rows, headers = http("GET", self.base + "/api/characters")
        self.assertEqual(code, 200)
        return rows, headers["x-version"], json.loads(headers["x-columns"])

    def put_chars(self, rows, version, expect=200):
        code, data, _ = http("PUT", self.base + "/api/characters", rows, headers={"X-Base-Version": version})
        self.assertEqual(code, expect, data)
        return data

    def test_15_characters_edit_roundtrip(self):
        rows, ver, cols = self.get_chars()
        self.assertEqual(cols[:4], ["id", "name", "image_type", "prompt"])
        n0 = len(rows)
        rows[3]["hair"] = "short hair, pink hair"
        rows.append({"id": "npc_0006", "name": "한글이름", "subject": "1girl", "personality": "명랑", "memo": "추가 열"})
        res = self.put_chars(rows, ver)
        self.assertEqual(res["count"], n0 + 1)
        raw = (self.tmp / "characters.csv").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))           # 엑셀용 UTF-8 BOM
        self.assertTrue((self.tmp / "characters.csv.bak").exists())
        rows2, ver2, cols2 = self.get_chars()
        self.assertEqual(rows2[-1]["name"], "한글이름")
        self.assertEqual(rows2[3]["hair"], "short hair, pink hair")
        self.assertIn("memo", cols2)                                # 모르는 열도 보존
        self.assertEqual(rows2[-1]["memo"], "추가 열")
        self.assertNotEqual(ver, ver2)
        # 옛 버전으로 저장하면 충돌
        self.put_chars(rows2, ver, expect=409)
        # 검증 오류
        bad = [dict(r) for r in rows2]
        bad.append({"id": "NPC_0001"})                              # 대소문자만 다른 중복 (Windows 폴더)
        bad.append({"id": "has space"})
        bad.append({"id": "npc_0099", "seed": "abc", "expressions": "smile|웃음 표정"})
        err = self.put_chars(bad, ver2, expect=400)
        joined = " ".join(err["errors"])
        for frag in ("겹칩니다", "has space", "seed", "웃음 표정"):
            self.assertIn(frag, joined)
        # 경고는 저장을 막지 않음
        warn = [dict(r) for r in rows2]
        warn[-1]["personality"] = "없는성격"
        res = self.put_chars(warn, ver2)
        self.assertTrue(any("없는성격" in w for w in res["warnings"]))
        # 원래대로 (뒤 테스트용)
        _, ver3, _ = self.get_chars()
        self.put_chars([r for r in rows2 if r["id"] != "npc_0006"], ver3)

    def test_16_import_encodings(self):
        csv_text = "id,name,personality,hair\r\nnpc_0101,가나다,소심,\"long hair, black hair\"\r\n,빈id,,\r\n"
        for enc, label in (("cp949", "CP949"), ("utf-8-sig", "UTF-8"), ("utf-8", "UTF-8")):
            code, data, _ = http("POST", self.base + "/api/characters/import", raw=csv_text.encode(enc),
                                 headers={"Content-Type": "text/csv"})
            self.assertEqual(code, 200, data)
            self.assertEqual(data["encoding"], label, enc)
            self.assertEqual(len(data["rows"]), 1)                  # id 없는 행은 버림
            self.assertEqual(data["rows"][0]["name"], "가나다")
            self.assertEqual(data["rows"][0]["hair"], "long hair, black hair")
        code, data, _ = http("POST", self.base + "/api/characters/import", raw="name,hair\r\n가,나\r\n".encode(),
                             headers={"Content-Type": "text/csv"})
        self.assertEqual(code, 400)
        self.assertIn("id", data["message"])
        self.assertEqual(len(self.get_chars()[0]), 5)              # 가져오기는 저장하지 않음

    def test_17_export(self):
        code, body, headers = http("GET", self.base + "/api/characters/export")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["content-disposition"])
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        text = body.decode("utf-8-sig")
        self.assertTrue(text.startswith("id,name,image_type,prompt"))
        self.assertIn("리나", text)

    def test_18_approve(self):
        r = self.api("POST", "/api/portraits/npc_0001/approve", {"approved": True})
        self.assertEqual(r["status"], "approved")
        meta = json.loads((self.out("npc_0001") / "meta.json").read_text(encoding="utf-8"))
        self.assertIs(meta["approved"], True)
        self.assertIn("approved_at", meta)
        self.assertEqual(self.api("GET", "/api/portraits/npc_0001")["status"], "approved")
        # 표정을 다시 만들면 승인이 풀림
        j = self.api("POST", "/api/jobs", {"ids": ["npc_0001"], "mode": "expression", "expression": "grin"})
        self.wait_idle()
        self.assertEqual(self.jobs(j["added"])[0]["status"], "done")
        self.assertEqual(self.api("GET", "/api/portraits/npc_0001")["status"], "done")
        # 승인 → 취소
        self.api("POST", "/api/portraits/npc_0001/approve", {})
        r = self.api("POST", "/api/portraits/npc_0001/approve", {"approved": False})
        self.assertEqual(r["status"], "done")
        meta = json.loads((self.out("npc_0001") / "meta.json").read_text(encoding="utf-8"))
        self.assertIs(meta["approved"], False)
        self.assertNotIn("approved_at", meta)
        # meta 가 없는 캐릭터는 승인 불가
        self.api("POST", "/api/jobs", {"ids": ["npc_0004"], "mode": "base", "new_seed": True})
        self.wait_idle()
        self.api("POST", "/api/portraits/npc_0004/approve", {"approved": True}, expect=400)

    def test_19_reroll_base_records_seed(self):
        before = json.loads((self.out("npc_0003") / "meta.json").read_text(encoding="utf-8"))
        self.api("POST", "/api/portraits/npc_0003/approve", {"approved": True})
        n0 = len(self.sse.events)
        r = self.api("POST", "/api/jobs", {"ids": ["npc_0003"], "mode": "base", "new_seed": True})
        self.wait_idle()
        self.assertEqual(self.jobs(r["added"])[0]["status"], "done")
        work = json.loads((self.out("npc_0003") / "work.json").read_text(encoding="utf-8"))
        self.assertNotEqual(work["seed"], before["seed"])
        rows, _, _ = self.get_chars()
        self.assertEqual(next(x for x in rows if x["id"] == "npc_0003")["seed"], str(work["seed"]))
        # 기존 표정·승인은 무효화
        self.assertFalse((self.out("npc_0003") / "meta.json").exists())
        self.assertEqual(list((self.out("npc_0003") / "expr").glob("*.png")), [])
        self.assertEqual(self.api("GET", "/api/portraits/npc_0003")["status"], "base")
        ev = self.wait_for(lambda: next((e for e in self.sse.events[n0:] if e["type"] == "characters"), None), msg="SSE characters")
        self.assertEqual(ev["changes"], [{"id": "npc_0003", "seed": str(work["seed"])}])
        # 새 seed 로 표정까지 이어서 → meta.seed 와 CSV seed 일치, CLI 로 다시 해도 같은 seed
        r = self.api("POST", "/api/jobs", {"ids": ["npc_0003"], "mode": "full", "new_seed": True})
        self.wait_idle()
        meta = json.loads((self.out("npc_0003") / "meta.json").read_text(encoding="utf-8"))
        rows, _, _ = self.get_chars()
        self.assertEqual(next(x for x in rows if x["id"] == "npc_0003")["seed"], str(meta["seed"]))
        self.assertTrue(all(e["seed"] == meta["seed"] for e in meta["expressions"][1:]))

    def test_20_portrait_detail_defaults(self):
        ex = self.api("GET", "/api/expressions")
        ex["expressions"]["laugh"] = {"tags": "laughing", "denoise": 0.62}
        ex["expressions"]["smile"] = "smile"
        self.api("PUT", "/api/expressions", ex)
        default = self.api("GET", "/api/config")["expression"]["denoise"]
        p = self.api("GET", "/api/portraits/npc_0001")
        self.assertEqual(p["denoise_defaults"]["laugh"], 0.62)    # 표정별 지정값
        self.assertEqual(p["denoise_defaults"]["smile"], default) # 설정 기본값
        self.assertNotIn("neutral", p["denoise_defaults"])


    def test_21_bg_removal_safety(self):
        """배경 제거가 얼굴까지 지우면 배경 제거를 쓰지 않음 (캐릭터가 통째로 투명해지는 사고 방지)"""
        sys.path.insert(0, str(ROOT))
        import pipeline as P
        from PIL import Image
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        cfg["remove_background"] = True
        out = self.tmp / "unit"
        (out / "x" / "expr").mkdir(parents=True)
        Image.new("RGB", (832, 1216), "white").save(out / "x" / "base_src.png")
        warns = []
        orig = (P.remove_bg, P.detect_face)
        try:
            P.detect_face = lambda img, cfg, warn=None: (300, 150, 530, 430)
            for alpha_value, expect_cut in ((0, False), (255, True)):
                P.remove_bg = lambda img, cfg, warn=None, a=alpha_value: Image.new("RGBA", img.size, (0, 0, 0, a))
                pipe = P.Pipeline(None, cfg, {}, out, on_progress=lambda e: warns.append(e.get("msg", "")))
                pipe.prepare({"id": "x"}, seed=1)
                lo, hi = Image.open(out / "x" / "base.png").getchannel("A").getextrema()
                if expect_cut:
                    self.assertEqual((lo, hi), (255, 255))        # 배경 제거 결과(여기선 전부 불투명)를 사용
                else:
                    self.assertEqual(lo, 255)                     # 투명해지지 않고 원본 유지
                    self.assertTrue(any("배경 제거 생략" in w for w in warns))
        finally:
            P.remove_bg, P.detect_face = orig


    # ---- Phase 3 ---- #
    def test_22_debug_mask(self):
        code, png, headers = http("GET", self.base + "/api/portraits/npc_0001/mask.png")
        self.assertEqual(code, 200)
        self.assertEqual(headers["content-type"], "image/png")
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(png))
        self.assertEqual(im.size, (832, 1216))
        lo, hi = im.getchannel("A").getextrema()
        self.assertEqual(lo, 0)
        self.assertGreater(hi, 80)
        p = self.api("GET", "/api/portraits/npc_0001")
        self.assertEqual(set(p["geometry_cfg"]), {"inpaint_size", "crop_scale", "mask_expand", "feather"})
        self.assertIn("ellipse", p["work"])
        self.assertEqual(http("GET", self.base + "/api/portraits/npc_0004/mask.png")[0], 404)  # base 만 있음

    def wait_export(self):
        return self.wait_for(lambda: (lambda s: s if not s["running"] else None)(self.api("GET", "/api/export/unity")["state"]),
                             timeout=30, msg="내보내기")

    def test_23_unity_export(self):
        dest = self.tmp / "UnityProj" / "Assets" / "Resources" / "Portraits"
        dest.parent.mkdir(parents=True)
        # 잘못된 대상
        self.api("POST", "/api/export/unity", {"dest": "relative/path"}, expect=400)
        self.api("POST", "/api/export/unity", {"dest": str(self.tmp / "out" / "sub")}, expect=400)       # output 안
        self.api("POST", "/api/export/unity", {"dest": str(self.tmp / "nope" / "a" / "b")}, expect=400)  # 상위 폴더 없음
        # 승인 상태: npc_0001, npc_0005 만 승인
        for cid in ("npc_0001", "npc_0002", "npc_0003", "npc_0005"):
            if (self.out(cid) / "meta.json").exists():
                self.api("POST", f"/api/portraits/{cid}/approve", {"approved": cid in ("npc_0001", "npc_0005")})
        info = self.api("GET", "/api/export/unity")
        self.assertEqual(info["approved"], 2)

        # 이전에 내보냈던 다른 캐릭터 폴더 (승인 취소됨) + 우리가 만든 게 아닌 폴더
        stale = dest / "npc_0002"
        (stale / "expr").mkdir(parents=True)
        (stale / "meta.json").write_text(json.dumps({"id": "npc_0002"}), encoding="utf-8")
        (dest / "npc_0002.meta").write_text("unity", encoding="utf-8")
        foreign = dest / "SomethingElse"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("x", encoding="utf-8")

        self.api("POST", "/api/export/unity", {"dest": str(dest), "prune": False})
        st = self.wait_export()
        self.assertIsNone(st["error"])
        self.assertEqual(st["total"], 2)
        meta = json.loads((self.out("npc_0001") / "meta.json").read_text(encoding="utf-8"))
        n_files = 2 + len([e for e in meta["expressions"] if e["file"]])
        files = sorted(str(p.relative_to(dest / "npc_0001")).replace("\\", "/") for p in (dest / "npc_0001").rglob("*") if p.is_file())
        self.assertEqual(len(files), n_files)
        self.assertNotIn("base_src.png", files)
        self.assertNotIn("work.json", files)
        self.assertNotIn("preview.jpg", files)
        self.assertTrue(stale.exists())                                   # prune 안 했으므로 남음
        saved = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["unity_export_dir"], str(dest.resolve()))   # 다음에도 쓰도록 설정에 저장

        # Unity 가 만든 .meta 흉내 + 오래된 표정 파일 → 다시 내보내기
        (dest / "npc_0001" / "base.png.meta").write_text("guid: 123", encoding="utf-8")
        old = dest / "npc_0001" / "expr" / "old_expr.png"
        old.write_bytes(b"x")
        old.with_name("old_expr.png.meta").write_text("guid: 9", encoding="utf-8")
        self.api("POST", "/api/export/unity", {"dest": str(dest), "prune": True})
        st = self.wait_export()
        self.assertEqual(st["copied"], 0)                                 # 변경 없음 → 전부 건너뜀
        self.assertEqual(st["skipped"], n_files + 2 + len(json.loads(
            (self.out("npc_0005") / "meta.json").read_text(encoding="utf-8"))["expressions"]) - 1)
        self.assertEqual((dest / "npc_0001" / "base.png.meta").read_text(encoding="utf-8"), "guid: 123")  # GUID 유지
        self.assertFalse(old.exists())
        self.assertFalse(old.with_name("old_expr.png.meta").exists())
        self.assertFalse(stale.exists())                                  # 승인 취소된 캐릭터 정리
        self.assertFalse((dest / "npc_0002.meta").exists())
        self.assertTrue((foreign / "keep.txt").exists())                  # 우리 것이 아니면 건드리지 않음

        # 표정을 다시 만들면 승인이 풀려 다음 내보내기에서 빠짐
        j = self.api("POST", "/api/jobs", {"ids": ["npc_0005"], "mode": "expression", "expression": "smile"})
        self.wait_idle()
        self.api("POST", "/api/export/unity", {"dest": str(dest), "prune": True})
        st = self.wait_export()
        self.assertEqual(st["total"], 1)
        self.assertFalse((dest / "npc_0005").exists())

    def test_24_parts(self):
        P_OUT = self.tmp / "parts_out"
        d = self.api("GET", "/api/parts")
        self.assertEqual([s["id"] for s in d["sources"]], ["p1", "p2"])
        self.assertEqual({g["key"] for g in d["groups"]}, {"skin", "eyes", "hair", "outfit", "glasses"})
        self.assertFalse(d["sources"][0]["source"])
        self.assertIsNone(d["sources"][0]["layers"])

        # 소스 목록 편집: 잘못된 값은 거부, 파일은 그대로
        before = (self.tmp / "parts.json").read_text(encoding="utf-8")
        bad = [{"id": "p1", "seed": 1, "tags": "a"}, {"id": "p1", "seed": 2, "tags": "b"}]
        err = self.api("PUT", "/api/parts/sources", {"sources": bad}, expect=400)
        self.assertTrue(any("중복" in e for e in err["errors"]))
        for b in ([{"id": "a/b", "seed": 1, "tags": "x"}], [{"id": "p9", "seed": -1, "tags": "x"}],
                  [{"id": "p9", "seed": 1, "tags": " "}], [{"id": "p9", "seed": "12", "tags": "x"}]):
            self.api("PUT", "/api/parts/sources", {"sources": b}, expect=400)
        self.assertEqual((self.tmp / "parts.json").read_text(encoding="utf-8"), before)
        srcs = [{"id": s["id"], "seed": s["seed"], "tags": s["tags"]} for s in d["sources"]]
        srcs.append({"id": "p3", "seed": 33, "tags": "  1boy, armor  "})
        d = self.api("PUT", "/api/parts/sources", {"sources": srcs})
        self.assertEqual(d["sources"][2]["tags"], "1boy, armor")
        saved = json.loads((self.tmp / "parts.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["sources"]), 3)
        self.assertIn("seethrough", saved)                     # 다른 설정은 유지
        self.assertTrue((self.tmp / "parts.json.bak").exists())

        # 캐릭터용 /api/jobs 로는 파츠 작업을 넣을 수 없음
        self.api("POST", "/api/jobs", {"ids": ["p1"], "mode": "parts_gen"}, expect=400)
        self.api("POST", "/api/parts/jobs", {"ids": ["p1"], "mode": "dance"}, expect=400)
        self.api("POST", "/api/parts/jobs", {"ids": ["nobody"], "mode": "gen"}, expect=400)

        # 소스 없이 분리 → 실패, 다시 시도는 나중에
        r = self.api("POST", "/api/parts/jobs", {"ids": ["p3"], "mode": "split"})
        self.wait_idle()
        (j,) = self.jobs(r["added"])
        self.assertEqual(j["status"], "failed")
        self.assertIn("소스", j["error"])

        # ① 소스 생성 → ② 분리
        n0 = len(self.sse.events)
        frees0 = http("GET", FAKE + "/_fake/state")[1]["frees"]
        r = self.api("POST", "/api/parts/jobs", {"ids": ["p1", "p2", "p3"], "mode": "gen"})
        self.wait_idle(60)
        self.assertEqual([j["status"] for j in self.jobs(r["added"])], ["done"] * 3)
        self.assertEqual(self.jobs(r["added"])[0]["done_images"], 2)
        for sid in ("p1", "p2", "p3"):
            self.assertTrue((P_OUT / sid / "source.png").exists())
            self.assertIn("face", json.loads((P_OUT / sid / "source.json").read_text(encoding="utf-8")))
        r = self.api("POST", "/api/parts/jobs", {"ids": ["p1", "p2"], "mode": "split"})
        self.wait_idle(60)
        self.assertEqual([j["status"] for j in self.jobs(r["added"])], ["done"] * 2)
        st = http("GET", FAKE + "/_fake/state")[1]
        self.assertGreater(st["frees"], frees0)                  # SDXL → SeeThrough 로 바뀔 때 VRAM 비움

        lj = json.loads((P_OUT / "p1" / "layers.json").read_text(encoding="utf-8"))
        names = [l["name"] for l in lj["layers"]]
        self.assertLess(names.index("eyewhite-l"), names.index("irides-l"))   # 깊이와 상관없이 흰자 → 홍채 순
        self.assertLess(names.index("back hair"), names.index("face"))
        for l in lj["layers"]:                                                  # source.png 좌표 안
            self.assertTrue(0 <= l["left"] < l["right"] <= 832 and 0 <= l["top"] < l["bottom"] <= 1216, l)
            self.assertTrue((P_OUT / "p1" / l["file"]).exists())
        for f in ("check.png", "sheet.png"):
            self.assertTrue((P_OUT / "p1" / f).exists())

        d = self.api("GET", "/api/parts")
        p1 = d["sources"][0]
        self.assertEqual(p1["layers"], len(lj["layers"]))
        self.assertEqual(p1["groups"], ["eyes", "glasses", "hair", "outfit", "skin"])
        self.assertIsNone(d["sources"][2]["layers"])
        for k in ("source", "check", "sheet"):
            code, _, hd = http("GET", self.base + p1["files"][k])
            self.assertEqual(code, 200, k)
        self.assertEqual(http("GET", self.base + p1["thumb"])[2]["content-type"], "image/webp")
        self.assertEqual(http("GET", self.base + "/parts_out/../config.json")[0], 404)
        self.assertEqual(http("GET", self.base + "/parts_out/p1/nope.png")[0], 404)

        # 조합
        code, img, hd = http("GET", self.base + "/api/parts/compose?skin=p1&eyes=p2&hair=p2&outfit=p1&w=300")
        self.assertEqual((code, hd["content-type"]), (200, "image/webp"))
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(img))
        self.assertEqual(im.size, (300, round(1216 * 300 / 832)))
        self.assertEqual(im.mode, "RGBA")
        # 얼굴·머리 소스가 다르면 머리 소스 얼굴을 밑에 깔고 고친 얼굴을 얹음 → 같은 소스일 때와 결과가 달라야 함
        same = http("GET", self.base + "/api/parts/compose?skin=p2&hair=p2")[1]
        mixed = http("GET", self.base + "/api/parts/compose?skin=p1&hair=p2")[1]
        self.assertNotEqual(same, mixed)
        self.assertEqual(d["sources"][0]["issues"], [])
        self.api("GET", "/api/parts/compose?skin=nobody", expect=400)
        self.api("GET", "/api/parts/compose", expect=400)
        self.api("GET", "/api/parts/compose?skin=p3", expect=404)              # 분리 안 한 소스

        # PSD 2종 (레이어 / 깊이). 깊이 레이어는 분리할 때 저장됨
        for l in lj["layers"]:
            self.assertTrue((P_OUT / "p1" / l["depth_file"]).exists(), l)
        import struct
        for kind, n, mode in (("rgba", len(lj["layers"]) + 1, 3), ("depth", len(lj["layers"]), 1)):
            code, psd, hd = http("GET", self.base + f"/api/parts/psd/p1?kind={kind}")
            self.assertEqual(code, 200, kind)
            self.assertIn("p1_depth.psd" if kind == "depth" else "p1.psd", hd["content-disposition"])
            self.assertEqual(psd[:4], b"8BPS")
            self.assertEqual(struct.unpack(">IIHH", psd[14:26]), (1216, 832, 8, mode))
            # 헤더(26) + 색상표·리소스 길이(8) + 레이어·마스크 길이(4) + 레이어 정보 길이(4) → 레이어 수
            self.assertEqual(struct.unpack(">h", psd[42:44])[0], n, kind)
        self.api("GET", "/api/parts/psd/p1?kind=nope", expect=400)
        self.api("GET", "/api/parts/psd/p3", expect=404)                       # 분리 안 한 소스
        # 깊이 레이어를 저장하기 전에 분리한 소스 → ComfyUI output 에서 받아 채움
        lj2 = json.loads((P_OUT / "p2" / "layers.json").read_text(encoding="utf-8"))
        for l in lj2["layers"]:
            (P_OUT / "p2" / l.pop("depth_file")).unlink()
        (P_OUT / "p2" / "layers.json").write_text(json.dumps(lj2), encoding="utf-8")
        self.assertEqual(http("GET", self.base + "/api/parts/psd/p2?kind=depth")[0], 200)
        lj2 = json.loads((P_OUT / "p2" / "layers.json").read_text(encoding="utf-8"))
        self.assertTrue(all((P_OUT / "p2" / l["depth_file"]).exists() for l in lj2["layers"]))

        # 이미 있으면 건너뜀, force 면 다시
        r = self.api("POST", "/api/parts/jobs", {"ids": ["p1"], "mode": "split"})
        self.wait_idle()
        (j,) = self.jobs(r["added"])
        self.assertEqual((j["result"], j["done_images"]), ("skipped", 0))
        r = self.api("POST", "/api/parts/jobs", {"ids": ["p1"], "mode": "gen", "force": True})
        self.wait_idle(60)
        self.assertFalse((P_OUT / "p1" / "layers.json").exists())              # 소스가 바뀌면 예전 레이어는 무효

        # 실패한 작업 다시 시도 (p3 은 이제 소스가 있으므로 성공)
        r = self.api("POST", "/api/jobs/retry", {})
        self.assertGreaterEqual(len(r["added"]), 1)
        self.wait_idle(60)
        self.assertTrue((P_OUT / "p3" / "layers.json").exists())

        self.wait_for(lambda: "parts" in self.sse.types()[n0:], msg="SSE parts")
        logs = [e["msg"] for e in self.sse.events[n0:] if e["type"] == "log"]
        self.assertTrue(any("레이어" in m and "분리" in m for m in logs))

    def test_25_parts_import(self):
        from PIL import Image, ImageChops
        P_OUT = self.tmp / "parts_out"
        r = self.api("POST", "/api/jobs", {"ids": ["npc_0001", "npc_0002"], "mode": "base"})
        self.wait_idle(60)
        self.api("POST", "/api/parts/import", {"ids": ["npc_9999"]}, expect=400)       # base 없음
        self.api("POST", "/api/parts/import", {"ids": []}, expect=400)
        self.api("POST", "/api/parts/import", {"ids": ["../x"]}, expect=400)

        # 가져오기 + 바로 분리. 얼굴 맞춤이면 gen 소스와 같은 크기
        r = self.api("POST", "/api/parts/import", {"ids": ["npc_0001"], "align": True, "split": True})
        self.assertEqual(r["sources"], ["npc_0001"])
        self.assertEqual(len(r["added"]), 2)
        self.wait_idle(60)
        jobs = self.jobs(r["added"])
        self.assertEqual([(j["mode"], j["status"]) for j in jobs], [("parts_gen", "done"), ("parts_split", "done")])
        self.assertEqual(jobs[0]["done_images"], 1)
        src = next(s for s in self.api("GET", "/api/parts")["sources"] if s["id"] == "npc_0001")
        self.assertEqual((src["from"], src["align"]), ("npc_0001", True))
        self.assertTrue(src["tags"])
        self.assertIsNotNone(src["layers"])
        with Image.open(P_OUT / "npc_0001" / "source.png") as im:
            self.assertEqual(im.size, (832, 1216))
        sj = json.loads((P_OUT / "npc_0001" / "source.json").read_text(encoding="utf-8"))
        self.assertEqual((sj["from"], sj["align"]), ("npc_0001", True))

        # id 가 다른 소스와 겹치면 _2. 얼굴 맞춤을 끄면 원래 그림 그대로
        d = self.api("GET", "/api/parts")
        srcs = [{k: s[k] for k in ("id", "seed", "tags", "from", "align") if k in s} for s in d["sources"]]
        srcs.append({"id": "npc_0002", "seed": 5, "tags": "1girl"})
        self.api("PUT", "/api/parts/sources", {"sources": srcs})
        saved = json.loads((self.tmp / "parts.json").read_text(encoding="utf-8"))["sources"]
        self.assertEqual(next(s for s in saved if s["id"] == "npc_0001")["from"], "npc_0001")   # 표 저장 후에도 유지
        r = self.api("POST", "/api/parts/import", {"ids": ["npc_0002"], "align": False})
        self.assertEqual(r["sources"], ["npc_0002_2"])
        self.wait_idle(60)
        with Image.open(P_OUT / "npc_0002_2" / "source.png") as a, Image.open(self.out("npc_0002") / "base_src.png") as b:
            self.assertIsNone(ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox())

        # 다시 가져오면 같은 소스를 갱신하고 예전 레이어는 지움
        n = len(json.loads((self.tmp / "parts.json").read_text(encoding="utf-8"))["sources"])
        r = self.api("POST", "/api/parts/import", {"ids": ["npc_0001"]})
        self.assertEqual(r["sources"], ["npc_0001"])
        self.wait_idle(60)
        self.assertEqual(len(json.loads((self.tmp / "parts.json").read_text(encoding="utf-8"))["sources"]), n)
        self.assertFalse((P_OUT / "npc_0001" / "layers.json").exists())

        # ① 소스 생성도 가져온 소스는 새로 그리지 않고 다시 가져옴 (ComfyUI 로 그림을 보내지 않음)
        before = http("GET", FAKE + "/_fake/state")[1]["prompts"]
        r = self.api("POST", "/api/parts/jobs", {"ids": ["npc_0002_2"], "mode": "gen", "force": True})
        self.wait_idle(60)
        (j,) = self.jobs(r["added"])
        self.assertEqual((j["status"], j["done_images"]), ("done", 1))
        self.assertEqual(http("GET", FAKE + "/_fake/state")[1]["prompts"], before)

    def test_26_parts_delete_moves_results(self):
        from PIL import Image
        P_OUT = self.tmp / "parts_out"
        cur = lambda: [{k: s[k] for k in ("id", "seed", "tags", "from", "align") if k in s}
                       for s in self.api("GET", "/api/parts")["sources"]]
        srcs = cur() + [{"id": "x1", "seed": 1, "tags": "a"}]
        self.api("PUT", "/api/parts/sources", {"sources": srcs})
        (P_OUT / "x1").mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8)).save(P_OUT / "x1" / "source.png")
        # 목록에서 빼면 결과 폴더는 .deleted 로 (지우지 않음)
        self.api("PUT", "/api/parts/sources", {"sources": [s for s in srcs if s["id"] != "x1"]})
        self.assertFalse((P_OUT / "x1").exists())
        self.assertEqual(len(list((P_OUT / ".deleted").glob("x1-*/source.png"))), 1)
        # 같은 id 로 새로 만들면 빈 상태 (예전 결과가 남아 있던 폴더도 옮김)
        (P_OUT / "x1").mkdir()
        Image.new("RGB", (8, 8)).save(P_OUT / "x1" / "source.png")
        d = self.api("PUT", "/api/parts/sources", {"sources": cur() + [{"id": "x1", "seed": 2, "tags": "b"}]})
        x1 = next(s for s in d["sources"] if s["id"] == "x1")
        self.assertFalse(x1["source"])
        self.assertIsNone(x1["thumb"])
        # 남아 있는 소스의 결과는 그대로
        self.assertTrue((P_OUT / "p1" / "source.png").exists())


    def test_27_type_workflows_and_psd(self):
        from PIL import Image
        rows, _, _ = self.get_chars()
        rows += [{"id": "resident_type", "name": "주민", "image_type": "npc", "subject": "1boy"},
                 {"id": "slime_type", "name": "슬라임", "image_type": "monster", "prompt": "green slime, wide shot"}]
        self.api("PUT", "/api/characters", rows)
        for cid in ("resident_type", "slime_type"):
            r = self.api("POST", "/api/jobs", {"ids": [cid], "mode": "base"})
            self.wait_idle()
            self.assertEqual(self.jobs(r["added"])[0]["status"], "done")
            p = self.api("GET", f"/api/portraits/{cid}")
            self.assertEqual(p["expressions"], [])
            self.assertEqual(p["planned"], [])
            self.assertTrue(p["stages"]["image"])
            self.assertNotIn("face", p["work"])
        self.api("POST", "/api/portraits/resident_type/approve", {}, expect=400)
        self.api("POST", "/api/portraits/slime_type/approve", {})
        for mode in ("expressions", "layers"):
            self.api("POST", "/api/jobs", {"ids": ["slime_type"], "mode": mode}, expect=400)
        r = self.api("POST", "/api/jobs", {"ids": ["resident_type"], "mode": "full"})
        self.wait_idle()
        self.assertEqual(self.jobs(r["added"])[0]["status"], "done")
        p = self.api("GET", "/api/portraits/resident_type")
        self.assertEqual(p["status"], "done")
        self.assertTrue(p["stages"]["layers"])
        self.assertEqual(http("GET", self.base + p["layers"]["preview"])[0], 200)
        code, psd, _ = http("GET", self.base + p["layers"]["psd_url"])
        self.assertEqual(code, 200)
        self.assertEqual(psd[:4], b"8BPS")
        with Image.open(self.out("resident_type") / "base_src.png") as base, \
             Image.open(self.tmp / "out/_layers/resident_type/source.png") as source:
            self.assertEqual(base.size, source.size)
            self.assertEqual(base.convert("RGB").tobytes(), source.tobytes())
        self.api("POST", "/api/portraits/resident_type/approve", {})
        r = self.api("POST", "/api/jobs", {"ids": ["resident_type"], "mode": "base", "new_seed": True})
        self.wait_idle()
        self.assertEqual(self.jobs(r["added"])[0]["status"], "done")
        # Even identical fake image bytes must invalidate layers after regeneration.
        p = self.api("GET", "/api/portraits/resident_type")
        self.assertEqual(p["status"], "base")
        self.assertFalse(p["layers"]["current"])
        self.assertTrue(p["layers"]["available"])
        self.api("POST", "/api/portraits/resident_type/approve", {}, expect=400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
