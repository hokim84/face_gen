"""Permanent gallery deletion uses disposable temporary output only."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
import pipeline as P
import server

ROOT = Path(__file__).resolve().parent.parent


class GalleryDeleteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out, self.parts = self.root / "out", self.root / "parts_out"
        cfg = P.read_json(ROOT / "config.json")
        cfg["output_dir"] = str(self.out)
        spec = P.read_json(ROOT / "parts.json")
        spec.update(out_dir=str(self.parts), sources=[
            {"id": "hero-src", "from": "hero", "seed": 1, "tags": "hero", "align": False},
            {"id": "keep-src", "from": "keep", "seed": 2, "tags": "keep", "align": False}])
        P.write_json(self.root / "config.json", cfg)
        P.write_json(self.root / "parts.json", spec)
        self.csv = self.root / "characters.csv"
        self.csv.write_text("id,name,image_type,prompt\nhero,주인공,character,\nbeast,괴물,monster,beast\nkeep,유지,npc,\n", encoding="utf-8")
        for name, value in (("config", self.root / "config.json"), ("characters", self.csv),
                            ("parts", self.root / "parts.json")):
            p = patch.object(server.Paths, name, value)
            p.start()
            self.addCleanup(p.stop)
        bus = server.EventBus()
        for name, value in (("BUS", bus), ("QUEUE", server.JobQueue(bus)), ("EXPORTER", server.UnityExporter(bus))):
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.api = TestClient(server.app)
        self.addCleanup(self.api.close)
        for cid in ("hero", "beast", "keep", "orphan"):
            self.image(self.out / cid / "base_src.png")
        for path, cid in ((self.out / "_layers/hero", "hero"),
                          (self.out / "_layers/.history/hero-old", "hero"),
                          (self.out / "_layers/.history/hero-extra-old", "hero-extra"),
                          (self.out / "_layers/.staging/batch/hero", "hero")):
            self.image(path / "source.png")
            P.write_json(path / "layers.json", {"id": cid, "width": 8, "height": 8, "layers": []})
        for sid, cid in (("hero-src", "hero"), ("keep-src", "keep"), (".deleted/hero-src-old", "hero")):
            self.image(self.parts / sid / "source.png")
            P.write_json(self.parts / sid / "source.json", {"id": sid.split("/")[-1], "from": cid})

    def image(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), "red").save(path)

    def delete(self, ids, **kwargs):
        return self.api.post("/api/portraits/delete", json={"ids": ids}, **kwargs)

    def test_batch_removes_records_and_all_managed_images(self):
        r = self.delete(["hero", "beast", "hero"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"deleted": ["hero", "beast"], "failed": []})
        self.assertEqual([r["id"] for r in server.load_rows()], ["keep"])
        self.assertFalse((self.out / "hero").exists())
        self.assertFalse((self.out / "beast").exists())
        self.assertFalse((self.out / "_layers/hero").exists())
        self.assertFalse((self.out / "_layers/.history/hero-old").exists())
        self.assertFalse((self.out / "_layers/.staging/batch/hero").exists())
        self.assertFalse((self.parts / "hero-src").exists())
        self.assertFalse((self.parts / ".deleted/hero-src-old").exists())
        self.assertEqual([s["id"] for s in server.load_parts()["sources"]], ["keep-src"])
        self.assertTrue((self.out / "_layers/.history/hero-extra-old/source.png").is_file())
        self.assertTrue((self.parts / "keep-src/source.png").is_file())
        self.assertEqual(self.api.get("/output/hero/base_src.png").status_code, 404)
        self.assertNotIn("hero", [p["id"] for p in self.api.get("/api/portraits").json()])
        self.assertFalse(list(self.root.rglob(".purge-*")))

    def test_orphan_outputs_are_deleted_without_changing_other_rows(self):
        before = self.csv.read_bytes()
        r = self.delete(["orphan"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse((self.out / "orphan").exists())
        self.assertEqual(self.csv.read_bytes(), before)

    def test_invalid_ids_and_mixed_missing_batch_do_not_delete(self):
        for ids, status in (([], 400), (["../hero"], 400), (["hero", "missing"], 404)):
            r = self.delete(ids)
            self.assertEqual(r.status_code, status, r.text)
            self.assertTrue((self.out / "hero/base_src.png").is_file())

    def test_busy_character_or_linked_parts_and_export_are_rejected(self):
        for cid in ("hero", "hero-src"):
            job = server.Job(cid, cid, "base")
            server.QUEUE.jobs[job.id] = job
            self.assertEqual(self.delete(["hero"]).status_code, 409)
            server.QUEUE.jobs.clear()
        server.EXPORTER.state["running"] = True
        self.assertEqual(self.delete(["hero"]).status_code, 409)
        self.assertTrue((self.out / "hero/base_src.png").is_file())

    def test_stale_csv_version_rejected(self):
        self.assertEqual(self.delete(["hero"], headers={"X-Base-Version": "old"}).status_code, 409)
        self.assertTrue((self.out / "hero/base_src.png").is_file())

    def test_metadata_failure_restores_all_staged_directories(self):
        before = self.csv.read_bytes()
        with patch.object(server, "save_parts_sources", side_effect=HTTPException(409, "locked")):
            r = self.delete(["hero"])
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual([r["id"] for r in server.load_rows()], ["hero", "beast", "keep"])
        self.assertTrue((self.out / "hero/base_src.png").is_file())
        self.assertTrue((self.parts / "hero-src/source.png").is_file())
        self.assertFalse(list(self.root.rglob(".purge-*")))

    def test_purge_failure_is_reported_and_keeps_failed_records(self):
        (self.out / "hero/locked.marker").write_text("locked")
        real_remove = shutil.rmtree

        def remove(path, *args, **kwargs):
            if (Path(path) / "locked.marker").exists():
                raise PermissionError("locked")
            return real_remove(path, *args, **kwargs)

        with patch.object(server.shutil, "rmtree", side_effect=remove):
            r = self.delete(["hero", "beast"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["deleted"], ["beast"])
        self.assertEqual(r.json()["failed"][0]["id"], "hero")
        self.assertEqual([r["id"] for r in server.load_rows()], ["hero", "keep"])
        self.assertTrue((self.out / "hero/base_src.png").is_file())
        self.assertTrue((self.parts / "hero-src/source.png").is_file())
        self.assertIn("hero-src", [s["id"] for s in server.load_parts()["sources"]])

    def test_unmanaged_content_and_outside_paths_are_rejected(self):
        with self.assertRaises(HTTPException):
            server.checked_delete_dir(self.out, self.root / "outside")
        (self.out / "hero/base_src.png").unlink()
        (self.out / "hero/unrelated.txt").write_text("preserve")
        self.assertEqual(self.delete(["hero"]).status_code, 400)
        self.assertTrue((self.out / "hero/unrelated.txt").is_file())

    def test_partial_delete_remains_retryable_after_metadata_files_are_gone(self):
        real_remove = shutil.rmtree

        def remove(path, *args, **kwargs):
            path = Path(path)
            if (path / "base_src.png").exists():
                (path / "base_src.png").unlink()
                self.image(path / "expr/locked.png")
                raise PermissionError("expression image locked")
            return real_remove(path, *args, **kwargs)

        with patch.object(server.shutil, "rmtree", side_effect=remove):
            r = self.delete(["hero"])
        self.assertEqual(r.json()["failed"][0]["id"], "hero")
        self.assertTrue((self.out / "hero/.delete.json").is_file())
        self.assertTrue((self.out / "hero/expr/locked.png").is_file())
        r = self.delete(["hero"])
        self.assertEqual(r.json(), {"deleted": ["hero"], "failed": []})
        self.assertFalse((self.out / "hero").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
