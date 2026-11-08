"""Unit tests for the cluster exchange and the Nomad scheduling rules. Run: python -m unittest tests.test_cluster"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from serial_audio.web import exchange
from serial_audio.web.nomad import conflicts, job_stages, placement_wait, worker_summary


def make_root(base: Path) -> Path:
    for rel, text in {"serial_audio/__init__.py": "", "serial_audio/web/static/app.js": "ui", "prompts/p.md": "p",
                      "schemas/s.json": "{}", "library/demo/serial.yaml": "title: Demo\n",
                      "library/demo/chapters/0001/chapter.yaml": "status: scripted\n",
                      "library/demo/audio/ch0001.opus": "old audio"}.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return base


class ExchangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = make_root(base / "app")
        self.work = base / "work"
        data = exchange.build_bundle("demo", {"job": "j1", "stage": 0, "steps": [["chapters", "demo"]]}, self.root)
        z = zipfile.ZipFile(io.BytesIO(data))
        self.names = set(z.namelist())
        z.extractall(self.work)
        self.info = json.loads((self.work / exchange.BUNDLE_FILE).read_text(encoding="utf-8"))
        self.folder = self.work / "library" / "demo"
        self.conflicts = base / "conflicts"

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, success=True):
        changed = exchange.changes(self.folder, self.info["manifest"])
        data = exchange.build_result(self.folder, {"job": "j1", "exit_code": 0 if success else 1}, changed)
        info, z = exchange.read_result(data)
        return exchange.apply_result("demo", info, z, success=success, conflicts_dir=self.conflicts, root=self.root)

    def lib(self, rel):
        return (self.root / "library" / "demo" / rel).read_text(encoding="utf-8")

    def test_bundle_ships_code_and_serial_without_audio(self):
        self.assertIn("serial_audio/__init__.py", self.names)
        self.assertIn("prompts/p.md", self.names)
        self.assertIn("library/demo/chapters/0001/chapter.yaml", self.names)
        self.assertNotIn("library/demo/audio/ch0001.opus", self.names)
        self.assertNotIn("serial_audio/web/static/app.js", self.names)
        self.assertFalse(self.info["manifest"]["audio/ch0001.opus"]["shipped"])
        self.assertEqual(self.info["steps"], [["chapters", "demo"]])

    def test_changed_and_new_files_are_written_back(self):
        (self.folder / "chapters/0001/chapter.yaml").write_text("status: rendered\n", encoding="utf-8")
        (self.folder / "audio").mkdir()
        (self.folder / "audio/ch0001.opus").write_text("new audio", encoding="utf-8")
        report = self.result()
        self.assertEqual(sorted(report["applied"]), ["audio/ch0001.opus", "chapters/0001/chapter.yaml"])
        self.assertEqual(self.lib("chapters/0001/chapter.yaml"), "status: rendered\n")
        self.assertEqual(self.lib("audio/ch0001.opus"), "new audio")

    def test_a_file_changed_in_the_library_meanwhile_is_kept_aside(self):
        (self.folder / "chapters/0001/chapter.yaml").write_text("status: rendered\n", encoding="utf-8")
        (self.root / "library/demo/chapters/0001/chapter.yaml").write_text("status: edited\n", encoding="utf-8")
        report = self.result()
        self.assertEqual(report["conflicts"], ["chapters/0001/chapter.yaml"])
        self.assertEqual(self.lib("chapters/0001/chapter.yaml"), "status: edited\n")
        self.assertEqual((self.conflicts / "chapters/0001/chapter.yaml").read_text(encoding="utf-8"), "status: rendered\n")

    def test_a_failed_job_writes_back_only_resumable_files(self):
        (self.folder / "chapters/0001/chapter.yaml").write_text("status: broken\n", encoding="utf-8")
        (self.folder / "chapters/0001/breeze_takes.json").write_text("{}", encoding="utf-8")
        report = self.result(success=False)
        self.assertEqual(report["applied"], ["chapters/0001/breeze_takes.json"])
        self.assertEqual(report["held"], ["chapters/0001/chapter.yaml"])
        self.assertEqual(self.lib("chapters/0001/chapter.yaml"), "status: scripted\n")

    def test_unchanged_files_are_not_reported(self):
        self.assertEqual(exchange.changes(self.folder, self.info["manifest"]), {})

    def test_bad_paths_and_bad_hashes_write_nothing(self):
        for rel in ("../escape.txt", "/abs.txt", "a/../b.txt", "c:/x", "a\\b"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr(f"files/{rel}", "x")
                z.writestr(exchange.RESULT_FILE, json.dumps({"changes": {rel: {"base": None, "sha256": "0"}}}))
            info, z = exchange.read_result(buf.getvalue())
            with self.assertRaises(exchange.ExchangeError):
                exchange.apply_result("demo", info, z, success=True, conflicts_dir=self.conflicts, root=self.root)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("files/ok.txt", "x")
            z.writestr(exchange.RESULT_FILE, json.dumps({"changes": {"ok.txt": {"base": None, "sha256": "f" * 64}}}))
        info, z = exchange.read_result(buf.getvalue())
        with self.assertRaises(exchange.ExchangeError):
            exchange.apply_result("demo", info, z, success=True, conflicts_dir=self.conflicts, root=self.root)
        self.assertFalse((self.root / "library/demo/ok.txt").exists())

    def test_media_paths_map_into_the_mirror(self):
        self.assertEqual(exchange.mirror_rel("D:\\comfy\\out\\cast\\a_00001.flac"), "D/comfy/out/cast/a_00001.flac")
        self.assertEqual(exchange.mirror_rel("/Users/aj/out/a.wav"), "Users/aj/out/a.wav")
        self.assertIsNone(exchange.mirror_rel("D:\\comfy\\..\\secret.flac"))
        self.assertIsNone(exchange.mirror_rel(""))

    def test_media_named_by_node_paths_lands_in_the_mirror_and_views_serve_it(self):
        from serial_audio.web import views
        clip = Path(self.tmp.name) / "node-out" / "cast" / "hero_00001.flac"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"fLaC fake")
        mirror = Path(self.tmp.name) / "mirror"
        data = exchange.build_result(self.folder, {"job": "j1", "exit_code": 0}, {}, [str(clip), str(clip) + ".gone"])
        info, z = exchange.read_result(data)
        report = exchange.apply_result("demo", info, z, success=False, conflicts_dir=self.conflicts,
                                       media_dir=mirror, root=self.root)
        self.assertEqual(report["media"], 1)
        rel = exchange.mirror_rel(str(clip))
        self.assertEqual((mirror / rel).read_bytes(), b"fLaC fake")
        clip.unlink()
        old = views.MEDIA_MIRROR
        views.MEDIA_MIRROR = mirror
        try:
            self.assertTrue(views.comfy_media(str(clip))["url"].startswith("/media/mirror/"))
            self.assertIsNone(views.comfy_media(str(clip) + ".other"))
        finally:
            views.MEDIA_MIRROR = old

    def test_a_replaced_media_file_of_the_same_size_is_updated(self):
        mirror = Path(self.tmp.name) / "mirror"
        clip = Path(self.tmp.name) / "node-out" / "take.wav"
        clip.parent.mkdir(parents=True)
        for content in (b"AAAA", b"BBBB"):
            clip.write_bytes(content)
            info, z = exchange.read_result(exchange.build_result(self.folder, {"exit_code": 0}, {}, [str(clip)]))
            exchange.apply_result("demo", info, z, success=True, conflicts_dir=self.conflicts, media_dir=mirror,
                                  root=self.root)
        self.assertEqual((mirror / exchange.mirror_rel(str(clip))).read_bytes(), b"BBBB")

    def test_damaged_upload_is_refused(self):
        with self.assertRaises(exchange.ExchangeError):
            exchange.read_result(b"not a zip")
        for change in (1, None, "x", {"base": None}, {"sha256": 5, "base": None}):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr(exchange.RESULT_FILE, json.dumps({"changes": {"a.txt": change}}))
            with self.assertRaises(exchange.ExchangeError):
                exchange.read_result(buf.getvalue())


def job(kind, slug="demo", chapters=None, args=None, comfy=False):
    return {"kind": kind, "slug": slug, "meta": {"chapters": chapters or []}, "args": args or [kind], "comfy": comfy}


class SchedulingTest(unittest.TestCase):
    def test_conflicts(self):
        self.assertFalse(conflicts(job("render", chapters=[7]), job("render", chapters=[8])))
        self.assertTrue(conflicts(job("render", chapters=[7]), job("screenplay", chapters=[7])))
        self.assertTrue(conflicts(job("render", chapters=[7]), job("character-pass", chapters=[1])))
        self.assertFalse(conflicts(job("render", chapters=[7]), job("cast", slug="other")))
        self.assertFalse(conflicts(job("add-epub", slug=None), job("add-epub", slug=None)))

    def test_stages(self):
        self.assertEqual(job_stages(job("render", args=["render", "demo", "7"]))[0]["class"], "breeze")
        self.assertEqual(job_stages(job("render", args=["render"], comfy=True))[0]["class"], "comfyui")
        self.assertEqual(job_stages(job("screenplay"))[0]["class"], "llm")
        self.assertIsNone(job_stages(job("fetch")))
        self.assertIsNone(job_stages(job("reset-bible")))
        cast = job_stages(job("cast", args=["cast", "demo", "--redo-brief", "--candidates", "3"]))
        self.assertEqual([s["class"] for s in cast], ["llm", "comfyui"])
        self.assertIn("--dry-run", cast[0]["args"])
        self.assertIn("--redo-brief", cast[0]["args"])
        self.assertNotIn("--redo-brief", cast[1]["args"])
        # A serial the app can't read gets no --engine, so the CLI picks it. A readable one gets it explicitly.
        self.assertNotIn("--engine", cast[1]["args"])
        from unittest import mock
        with mock.patch("serial_audio.library.load_serial", return_value={}):
            qwen = job_stages(job("cast", args=["cast", "demo"]))
        self.assertEqual(qwen[1]["args"][-2:], ["--engine", "qwen"])
        self.assertEqual(qwen[1]["class"], "comfyui")
        breeze = job_stages(job("cast", args=["cast", "demo", "--engine", "breeze"]))
        self.assertEqual([s["class"] for s in breeze], ["llm", "breeze"])
        self.assertEqual(breeze[1]["args"].count("--engine"), 1)
        self.assertEqual(len(job_stages(job("cast", args=["cast", "demo", "--dry-run"]))), 1)

    def test_an_unknown_cast_engine_reaches_the_cli_before_the_casting_pass(self):
        from unittest import mock
        with mock.patch("serial_audio.library.load_serial", return_value={"cast_engine": "breze"}):
            stages = job_stages(job("cast", args=["cast", "demo"]))
        self.assertEqual(stages[0]["args"], ["cast", "demo", "--engine", "breze", "--dry-run"])
        self.assertEqual(stages[1]["args"][-2:], ["--engine", "breze"])
        with mock.patch("serial_audio.library.load_serial", return_value={"cast_engine": "breeze"}):
            self.assertEqual(job_stages(job("cast", args=["cast", "demo"]))[1]["class"], "breeze")

    def test_worker_summary(self):
        n = worker_summary({"Name": "box", "Status": "ready", "SchedulingEligibility": "eligible",
                            "Meta": {"serial_audio": "breeze, comfyui", "busy": "false"}})
        self.assertEqual(n["classes"], ["breeze", "comfyui"])
        self.assertTrue(n["ready"])
        busy = worker_summary({"Name": "box", "Status": "ready", "SchedulingEligibility": "eligible",
                               "Meta": {"serial_audio": "llm", "busy": "true"}})
        self.assertFalse(busy["ready"])

    def test_placement_wait(self):
        evals = [{"CreateIndex": 2, "FailedTGAllocs": {"work": {"DimensionExhausted":
                                                                {"network: reserved port collision gpu=19137": 1}}}}]
        self.assertIn("another GPU job", placement_wait(evals))
        self.assertIsNone(placement_wait([{"CreateIndex": 1}]))


def worker(name, classes, os_name, gpu="none", ready=True):
    return {"name": name, "classes": classes, "os": os_name, "gpu": gpu, "ready": ready, "status": "ready",
            "eligible": True, "busy": False}


class NodeChoiceTest(unittest.TestCase):
    def setUp(self):
        from serial_audio.web.nomad import NomadClient, NomadJobManager
        self.tmp = tempfile.TemporaryDirectory()
        self.m = NomadJobManager(NomadClient("http://127.0.0.1:9", "token"), state_dir=Path(self.tmp.name),
                                 autostart=False)
        self.m.workers = [worker("laptop", ["breeze", "comfyui"], "windows", "nvidia"),
                          worker("mac", ["breeze"], "darwin", "metal")]

    def tearDown(self):
        self.m._lock_file.close()
        self.tmp.cleanup()

    def running_on(self, node, kind="render"):
        return {"kind": kind, "slug": "other", "meta": {"chapters": [9]}, "args": [kind], "comfy": False,
                "nomad": {"target": node, "stage": 0, "stages": [{"class": "breeze", "args": []}]}}

    def test_a_gpu_job_takes_the_nvidia_node_first_and_one_job_per_node(self):
        render = job("render", chapters=[1], args=["render", "demo", "1"])
        self.assertEqual(self.m._pick_node(render, "breeze", [])[:2], ("laptop", "breeze"))
        self.assertEqual(self.m._pick_node(render, "breeze", [self.running_on("laptop")])[:2], ("mac", "breeze"))
        node, cls, why = self.m._pick_node(render, "breeze", [self.running_on("laptop"), self.running_on("mac")])
        self.assertIsNone(node)
        self.assertIn("free breeze node", why)

    def test_a_job_that_needs_a_node_waits_for_it_and_may_switch_class(self):
        self.m.placement = {"chapters": {"demo": {"1": "mac"}}}
        check = job("check-takes", chapters=[1], args=["check-takes", "demo", "1"])
        self.assertEqual(self.m._pick_node(check, "comfyui", [])[:2], ("mac", "breeze"))
        node, cls, why = self.m._pick_node(check, "comfyui", [self.running_on("mac")])
        self.assertIsNone(node)
        self.assertIn("mac, which holds the files", why)
        render = job("render", chapters=[1], args=["render", "demo", "1"])
        self.assertEqual(self.m._pick_node(render, "breeze", [])[:2], ("mac", "breeze"))

    def test_paths_name_their_node_when_one_node_runs_that_os(self):
        self.assertEqual(self.m._node_for_paths(["D:\\comfy\\out\\a.wav"]), "laptop")
        self.assertEqual(self.m._node_for_paths(["/Users/aj/serial-audio/output/a.wav"]), "mac")
        self.assertIsNone(self.m._node_for_paths(["/home/aj/a.wav"]))
        self.m.workers.append(worker("laptop2", ["breeze"], "windows"))
        self.assertIsNone(self.m._node_for_paths(["D:\\comfy\\out\\a.wav"]))

    def test_a_restart_during_a_dispatch_dispatches_again_with_the_same_token(self):
        from serial_audio.web.nomad import NomadClient, NomadJobManager

        class Client(NomadClient):
            calls = []

            def nodes(self):
                return [{"ID": "n1", "Name": "dev", "Status": "ready", "SchedulingEligibility": "eligible",
                         "ModifyIndex": 1}]

            def node(self, node_id):
                return {"ID": "n1", "Name": "dev", "Meta": {"serial_audio": "llm"}, "Attributes": {}}

            def dispatch(self, job_id, meta, token):
                self.calls.append((job_id, token))
                return {"DispatchedJobID": f"{job_id}/dispatch-1"}

            def allocations(self, job_id):
                return []

            def evaluations(self, job_id):
                return []

        j = self.m.submit([{"kind": "screenplay", "params": {"slug": "twenty-thousand-leagues", "chapters": "1"}}])[0]
        self.m._mark_dispatch(j)
        self.m._save()
        self.m._lock_file.close()
        again = NomadJobManager(Client("http://127.0.0.1:9", "token"), state_dir=Path(self.tmp.name), autostart=False)
        self.m = again
        again.tick()
        self.assertEqual(Client.calls, [("serial-audio-llm", f"{j['id']}-0")])
        self.assertEqual(again.jobs[j["id"]]["nomad"]["dispatched"], "serial-audio-llm/dispatch-1")


class KeyAndLockTest(unittest.TestCase):
    def test_a_strange_key_is_refused_without_raising(self):
        from serial_audio.web.nomad import _key_ok
        self.assertTrue(_key_ok({"key": "abc"}, "abc"))
        self.assertFalse(_key_ok({"key": "abc"}, "\u00e9\u00e9"))
        self.assertFalse(_key_ok({"key": "abc"}, ""))

    def test_the_gpu_lock_holds_one_job_at_a_time(self):
        import threading
        from serial_audio import worker as w

        class Log:
            lines = []

            def line(self, text):
                self.lines.append(text)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpu.lock"
            first = w.gpu_lock(Log(), threading.Event(), path)
            self.assertIsNotNone(first)
            stop = threading.Event()
            threading.Timer(0.5, stop.set).start()
            log = Log()
            self.assertIsNone(w.gpu_lock(log, stop, path))
            self.assertTrue(any("another serial_audio GPU job" in t for t in log.lines))
            first.close()
            again = w.gpu_lock(Log(), threading.Event(), path)
            self.assertIsNotNone(again)
            again.close()


if __name__ == "__main__":
    unittest.main()
