"""Unit tests for the cluster exchange and the Nomad scheduling rules. Run: python -m unittest tests.test_cluster"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.parse
import wave
import zipfile
from pathlib import Path

from serial_audio.web import exchange
from serial_audio.web.nomad import conflicts, job_stages, placement_wait, worker_summary


def make_root(base: Path) -> Path:
    for rel, text in {"serial_audio/__init__.py": "", "serial_audio/web/static/app.js": "ui", "prompts/p.md": "p",
                      "serial_audio/._cli.py": "AppleDouble", "prompts/._p.md": "AppleDouble",
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
        # The ._ files a tar from a Mac leaves beside the code are not code.
        self.assertFalse([n for n in self.names if "/._" in n], self.names)
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

    def test_a_take_s_path_on_another_node_maps_under_this_node_s_output_folder(self):
        root = Path(self.tmp.name) / "out"
        anchor = "breeze/demo/ch0007"
        here = root / "breeze" / "demo" / "ch0007"
        # A Windows path and a POSIX path both map, whichever system asks.
        self.assertEqual(exchange.local_path("D:\\comfy\\ComfyUI\\output\\breeze\\demo\\ch0007\\cpp\\l0003.wav", root, anchor),
                         str(here / "cpp" / "l0003.wav"))
        self.assertEqual(exchange.local_path("/Users/aj/serial-audio/output/breeze/demo/ch0007/l0003_00001_.flac", root,
                                             anchor), str(here / "l0003_00001_.flac"))
        # The last anchor counts, so an output folder whose own path holds the same folder names maps right.
        self.assertEqual(exchange.local_path("/data/breeze/demo/ch0007/output/breeze/demo/ch0007/cpp/l0001.wav", root,
                                             anchor), str(here / "cpp" / "l0001.wav"))
        # A path of another chapter, a path that climbs out, and no path at all do not map.
        self.assertIsNone(exchange.local_path("D:\\comfy\\out\\breeze\\demo\\ch0008\\cpp\\l0003.wav", root, anchor))
        self.assertIsNone(exchange.local_path("/out/breeze/demo/ch0007/../../x.wav", root, anchor))
        self.assertIsNone(exchange.local_path(None, root, anchor))
        self.assertTrue(exchange.same_path(here / "cpp" / "l0003.wav", str(here / "cpp" / ".." / "cpp" / "l0003.wav")))
        self.assertFalse(exchange.same_path("D:\\comfy\\out\\a.wav", "/Users/aj/out/a.wav"))

    def test_take_files_reads_a_chapter_s_takes_and_tolerates_a_bad_file(self):
        chapter = self.folder / "chapters/0001"
        self.assertEqual(exchange.take_files(chapter), {})
        (chapter / exchange.TAKES_FILE).write_text(json.dumps({"takes": {
            "0": {"key": "a", "file": "D:\\out\\l0000.wav"}, "1": {"key": "b"}, "2": "junk", "3": {"file": ""}}}),
            encoding="utf-8")
        self.assertEqual(exchange.take_files(chapter), {"0": "D:\\out\\l0000.wav"})
        (chapter / exchange.TAKES_FILE).write_text("not json", encoding="utf-8")
        self.assertEqual(exchange.take_files(chapter), {})

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
        # A render waits for a cast, a pick, a say-as or a voice effect on its serial, which change what it reads.
        # None of them waits for a render, a screenplay or a take check, and they still wait for each other and for
        # a character pass.
        self.assertTrue(conflicts(job("render", chapters=[7]), job("cast")))
        self.assertTrue(conflicts(job("render", chapters=[7]), job("pick-voice")))
        self.assertFalse(conflicts(job("cast"), job("render", chapters=[7])))
        self.assertFalse(conflicts(job("pick-voice"), job("screenplay", chapters=[7])))
        self.assertFalse(conflicts(job("cast"), job("check-takes", chapters=[7])))
        self.assertTrue(conflicts(job("cast"), job("pick-voice")))
        self.assertTrue(conflicts(job("cast"), job("character-pass", chapters=[1])))
        self.assertFalse(conflicts(job("say-as"), job("render", chapters=[7])))
        self.assertFalse(conflicts(job("voice-effect"), job("render", chapters=[7])))
        self.assertTrue(conflicts(job("render", chapters=[7]), job("say-as")))
        self.assertTrue(conflicts(job("voice-effect"), job("cast")))
        self.assertTrue(conflicts(job("cast"), job("add-epub")))

    def test_stages(self):
        self.assertEqual(job_stages(job("render", args=["render", "demo", "7"]))[0]["class"], "breeze")
        self.assertEqual(job_stages(job("render", args=["render"], comfy=True))[0]["class"], "comfyui")
        self.assertEqual(job_stages(job("screenplay"))[0]["class"], "llm")
        self.assertIsNone(job_stages(job("fetch")))
        self.assertIsNone(job_stages(job("reset-bible")))
        cast = job_stages(job("cast", args=["cast", "demo", "--redo-brief", "--candidates", "3"]))
        self.assertEqual([s["class"] for s in cast], ["llm", "breeze"])
        self.assertIn("--dry-run", cast[0]["args"])
        self.assertIn("--redo-brief", cast[0]["args"])
        self.assertNotIn("--redo-brief", cast[1]["args"])
        # A serial the app can't read gets no --engine, so the CLI picks its default, breeze, on a breeze node.
        # A readable one gets the engine explicitly, breeze by default, and only a qwen cast takes a comfyui node.
        self.assertNotIn("--engine", cast[1]["args"])
        from unittest import mock
        with mock.patch("serial_audio.library.load_serial", return_value={}):
            default = job_stages(job("cast", args=["cast", "demo"]))
        self.assertEqual(default[1]["args"][-2:], ["--engine", "breeze"])
        self.assertEqual(default[1]["class"], "breeze")
        with mock.patch("serial_audio.library.load_serial", return_value={"cast_engine": "qwen"}):
            qwen = job_stages(job("cast", args=["cast", "demo"]))
        self.assertEqual(qwen[1]["args"][-2:], ["--engine", "qwen"])
        self.assertEqual(qwen[1]["class"], "comfyui")
        breeze = job_stages(job("cast", args=["cast", "demo", "--engine", "breeze"]))
        self.assertEqual([s["class"] for s in breeze], ["llm", "breeze"])
        self.assertEqual(breeze[1]["args"].count("--engine"), 1)
        self.assertEqual(len(job_stages(job("cast", args=["cast", "demo", "--dry-run"]))), 1)
        # A take check runs on a breeze node, and a voice pick on the app's machine.
        self.assertEqual(job_stages(job("check-takes", args=["check-takes", "demo", "7"]))[0]["class"], "breeze")
        self.assertIsNone(job_stages(job("pick-voice")))

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
        # With the laptop busy, a render waits for it rather than take the Mac, which is six times slower.
        node, cls, why = self.m._pick_node(render, "breeze", [self.running_on("laptop")])
        self.assertIsNone(node)
        self.assertTrue(why.startswith("waiting for laptop, about"), why)
        self.assertIn("on mac now", why)
        node, cls, why = self.m._pick_node(render, "breeze", [self.running_on("laptop"), self.running_on("mac")])
        self.assertIsNone(node)
        self.assertTrue(why.startswith("waiting for laptop, about"), why)
        self.assertIn("busy", why)

    def test_a_job_starts_when_its_bundle_is_asked_for(self):
        from unittest import mock
        from serial_audio.web import nomad

        # The bundle is read between two looks at the clock, and the job starts at the first.
        clock = iter(["asked", "built", "later"])
        self.m.jobs["j1"] = {"id": "j1", "kind": "render", "slug": "demo", "status": "running", "started": None,
                             "title": "Render · ch 1", "meta": {"chapters": [1]}, "args": ["render", "demo", "1"],
                             "nomad": {"key": "k", "stage": 0,
                                       "stages": [{"class": "breeze", "args": ["render", "demo", "1"]}]}}
        for p in (mock.patch.object(nomad, "now_iso", lambda: next(clock)),
                  mock.patch.object(nomad.exchange, "build_bundle", lambda slug, info: b"zip"),
                  mock.patch.object(self.m, "_append_log", lambda *a: None),
                  mock.patch.object(self.m, "_save", lambda: None)):
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(self.m.worker_bundle("j1", "k", "laptop"), b"zip")
        self.assertEqual((self.m.jobs["j1"]["started"], self.m.jobs["j1"]["nomad"]["contact"]), ("asked", "built"))

    def test_a_cast_starts_beside_a_render_of_its_serial_and_a_render_waits_for_a_cast(self):
        self.m.workers.append(worker("dev", ["llm"], "linux"))
        self.m.nomad_state["up"] = True
        render = {"kind": "render", "slug": "demo", "meta": {"chapters": [12]}, "args": ["render", "demo", "12"],
                  "title": "Render · ch 12",
                  "nomad": {"target": "mac", "stage": 0, "stages": [{"class": "breeze", "args": []}], "contact": "t"}}
        cast = {"kind": "cast", "slug": "demo", "meta": {"characters": ["eole"]}, "args": ["cast", "demo"],
                "title": "Cast · eole"}
        # Running: the render does not hold the cast once it has its bundle, and the cast holds a render.
        self.assertIsNone(self.m._blocker_for(cast, "llm", [render]))
        unfetched = {**render, "nomad": {**render["nomad"], "contact": None}}
        self.assertEqual(self.m._blocker_for(cast, "llm", [unfetched]),
                         "waiting for Render · ch 12 to fetch its bundle")
        self.assertEqual(self.m._blocker_for(render, "breeze", [cast]),
                         "waiting for Cast · eole, which changes the whole serial")
        # Queued ahead: the same, in queue order.
        self.m.jobs = {"r": {**render, "id": "r", "seq": 1, "status": "queued"},
                       "c": {**cast, "id": "c", "seq": 2, "status": "queued"}}
        self.assertIsNone(self.m._blocker_for(self.m.jobs["c"], "llm", []))
        self.m.jobs["c"]["seq"], self.m.jobs["r"]["seq"] = 1, 2
        self.assertEqual(self.m._blocker_for(self.m.jobs["r"], "breeze", []),
                         "waiting for Cast · eole, queued ahead of it on the same serial")

    def test_a_cast_passes_a_chapter_job_that_waits_ahead_of_it_in_its_lane(self):
        self.m.nomad_state["up"] = True
        stage = {"class": "breeze", "args": []}
        running = {"id": "r1", "seq": 1, "status": "running", "kind": "render", "slug": "demo",
                   "meta": {"chapters": [12]}, "args": ["render", "demo", "12"], "title": "Render · ch 12",
                   "nomad": {"target": "mac", "node": "mac", "stage": 0, "stages": [stage], "contact": "t"}}
        again = {**running, "id": "r2", "seq": 2, "status": "queued", "title": "Render · ch 12 again", "nomad": None}
        cast = {"id": "c", "seq": 3, "status": "queued", "kind": "cast", "slug": "demo", "heavy": True,
                "meta": {"characters": ["eole"]}, "args": ["cast", "demo"], "title": "Cast · eole",
                "nomad": {"key": "k", "stage": 1, "stages": [{"class": "llm", "args": []}, dict(stage)]}}
        self.m.jobs = {j["id"]: j for j in (running, again, cast)}
        dispatch = []
        # The second render waits for the running one and holds the breeze lane. The cast's voices stage passes it
        # to the free laptop.
        self.assertTrue(self.m._schedule(0.0, dispatch))
        self.assertIn("Render · ch 12", self.m.wait_reasons["r2"])
        self.assertEqual(dispatch, ["c"])
        self.assertEqual((cast["status"], cast["nomad"]["target"]), ("running", "laptop"))
        # A cast queued after it still waits for it.
        self.m.jobs["c2"] = {**cast, "id": "c2", "seq": 4, "status": "queued", "title": "Cast · lorimor-ancitif",
                             "nomad": {"key": "k2", "stage": 1, "stages": [{"class": "llm", "args": []}, dict(stage)]}}
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["c"])
        self.assertIn("Cast · eole", self.m.wait_reasons["c2"])

    def test_casting_bios_start_beside_a_render_and_the_fetch_that_waits_for_it(self):
        self.m.workers.append(worker("dev", ["llm"], "linux"))
        self.m.nomad_state["up"] = True
        render = {"id": "r", "seq": 1, "status": "running", "kind": "render", "slug": "demo", "heavy": True,
                  "meta": {"chapters": [38]}, "args": ["render", "demo", "38"], "title": "Render · ch 38",
                  "nomad": {"target": "mac", "node": "mac", "stage": 0, "stages": [{"class": "breeze", "args": []}],
                            "contact": "t"}}
        fetch = {"id": "f", "seq": 2, "status": "queued", "kind": "fetch", "slug": "demo", "heavy": False,
                 "meta": {}, "args": ["fetch", "demo", "new"], "title": "Fetch from site · new"}
        bios = {"id": "b", "seq": 3, "status": "queued", "kind": "bios", "slug": "demo", "heavy": True,
                "meta": {"characters": []}, "args": ["bios", "demo"], "title": "Casting bios · every voice without one"}
        self.m.jobs = {j["id"]: j for j in (render, fetch, bios)}
        dispatch = []
        # The fetch from the site waits for the render. The bios job, queued after the fetch, goes to the Claude
        # node at once.
        self.m._schedule(0.0, dispatch)
        self.assertEqual(self.m.wait_reasons["f"], "waiting for Render · ch 38, which changes the whole serial")
        self.assertEqual(dispatch, ["b"])
        self.assertEqual((bios["status"], fetch["status"]), ("running", "queued"))

    def test_an_import_waits_only_for_its_own_chapter(self):
        from unittest import mock

        def imp(n, **more):
            return {"kind": "fetch", "slug": "demo", "heavy": False, "meta": {"chapters": [n], "file": "p.html"},
                    "args": ["fetch", "demo", str(n), "--html", "p.html"], "title": f"Import saved page · ch {n}",
                    **more}

        # An import and a chapter job of another chapter don't wait for each other, and neither do two imports.
        # On one chapter they do, in both orders.
        self.assertFalse(conflicts(imp(39), job("render", chapters=[38])))
        self.assertFalse(conflicts(job("render", chapters=[38]), imp(39)))
        self.assertFalse(conflicts(job("screenplay", chapters=[38]), imp(39)))
        self.assertFalse(conflicts(imp(39), imp(40)))
        self.assertTrue(conflicts(imp(38), job("render", chapters=[38])))
        self.assertTrue(conflicts(job("render", chapters=[38]), imp(38)))
        # An import and a job that writes only voices, the bible or serial.yaml don't wait for each other. A
        # character pass and an import wait for each other only on one chapter. A fetch from the site still changes
        # the whole serial.
        for kind in ("cast", "bios", "pick-voice", "say-as", "voice-effect"):
            self.assertFalse(conflicts(imp(39), job(kind)), kind)
            self.assertFalse(conflicts(job(kind), imp(39)), kind)
        self.assertTrue(conflicts(imp(39), job("reset-bible")))
        self.assertTrue(conflicts(imp(39), job("character-pass", chapters=[39])))
        self.assertTrue(conflicts(job("character-pass", chapters=[39]), imp(39)))
        self.assertFalse(conflicts(imp(39), job("character-pass", chapters=[1])))
        self.assertFalse(conflicts(job("character-pass", chapters=[40]), imp(39)))
        self.assertTrue(conflicts(job("fetch"), job("render", chapters=[38])))
        self.assertTrue(conflicts(imp(39), job("fetch")))

        # Scheduling: the first import starts beside a render of another chapter. The second waits for the first.
        self.m.nomad_state["up"] = True
        render = {"id": "r", "seq": 1, "status": "running", "kind": "render", "slug": "demo", "heavy": True,
                  "meta": {"chapters": [38]}, "args": ["render", "demo", "38"], "title": "Render · ch 38",
                  "nomad": {"target": "mac", "node": "mac", "stage": 0, "stages": [{"class": "breeze", "args": []}],
                            "contact": "t"}}
        first, second = imp(39, id="i39", seq=2, status="queued"), imp(40, id="i40", seq=3, status="queued")
        self.m.jobs = {j["id"]: j for j in (render, first, second)}
        with mock.patch.object(self.m, "_launch_local", side_effect=lambda j: j.update(status="running")):
            self.m._schedule(0.0, [])
        self.assertEqual((first["status"], second["status"]), ("running", "queued"))
        self.assertEqual(self.m.wait_reasons["i40"], "waiting for Import saved page · ch 39")

        # Bundles: an import waits until a running job of its serial has fetched its bundle, and a job that gets a
        # bundle waits for a running import. On the render's own chapter the import waits for the render.
        self.m.jobs = {}
        unfetched = {**render, "nomad": {**render["nomad"], "contact": None}}
        self.assertEqual(self.m._blocker_for(imp(41), "local", [unfetched]),
                         "waiting for Render · ch 38 to fetch its bundle")
        other = job("render", chapters=[12], args=["render", "demo", "12"])
        self.assertEqual(self.m._blocker_for(other, "breeze", [first]), "waiting for Import saved page · ch 39")
        self.assertEqual(self.m._blocker_for(imp(38), "local", [render]),
                         "waiting for Render · ch 38, which works on the same chapter")

        # Queue order: an import starts while a batch's cast waits ahead of it for a character pass, and while an
        # import of the render's chapter waits ahead of it in its lane.
        stage = {"stage": 0, "stages": [{"class": "llm", "args": []}], "contact": "t"}
        passing = {"id": "c39", "seq": 2, "status": "running", "kind": "character-pass", "slug": "demo",
                   "heavy": True, "meta": {"chapters": [39]}, "args": ["character-pass", "demo", "39"],
                   "title": "Character pass · ch 39", "nomad": {"target": "dev", "node": "dev", **stage}}
        cast = {"id": "cast", "seq": 3, "status": "queued", "kind": "cast", "slug": "demo", "heavy": True,
                "meta": {"characters": []}, "args": ["cast", "demo"], "title": "Cast · everyone who needs a voice"}
        held, free = imp(38, id="i38", seq=4, status="queued"), imp(44, id="i44", seq=5, status="queued")
        self.m.jobs = {j["id"]: j for j in (render, passing, cast, held, free)}
        with mock.patch.object(self.m, "_launch_local", side_effect=lambda j: j.update(status="running")):
            self.m._schedule(0.0, [])
        self.assertEqual((cast["status"], held["status"], free["status"]), ("queued", "queued", "running"))
        self.assertEqual(self.m.wait_reasons["cast"], "waiting for Character pass · ch 39, which changes the whole serial")
        self.assertEqual(self.m.wait_reasons["i38"], "waiting for Render · ch 38, which works on the same chapter")

    def test_a_character_pass_starts_beside_a_render_of_another_chapter(self):
        # A character pass waits for a chapter job only on its own chapter. A chapter job reads the bible, so it
        # waits for a character pass of any chapter, and character passes wait for each other.
        self.assertFalse(conflicts(job("character-pass", chapters=[39]), job("render", chapters=[38])))
        self.assertFalse(conflicts(job("character-pass", chapters=[39]), job("screenplay", chapters=[38])))
        self.assertFalse(conflicts(job("character-pass", chapters=[39]), job("check-takes", chapters=[38])))
        self.assertTrue(conflicts(job("character-pass", chapters=[38]), job("render", chapters=[38])))
        self.assertTrue(conflicts(job("character-pass", chapters=[37, 38]), job("screenplay", chapters=[38])))
        self.assertTrue(conflicts(job("render", chapters=[38]), job("character-pass", chapters=[39])))
        self.assertTrue(conflicts(job("screenplay", chapters=[40]), job("character-pass", chapters=[39])))
        self.assertTrue(conflicts(job("character-pass", chapters=[40]), job("character-pass", chapters=[39])))
        self.assertTrue(conflicts(job("character-pass", chapters=[39]), job("cast")))

        # Scheduling: a batch queued behind another batch's running render. Its first character pass starts beside
        # the render, and the rest of the batch waits for the character pass.
        self.m.workers.append(worker("dev", ["llm"], "linux"))
        self.m.nomad_state["up"] = True

        def queued(jid, seq, kind, n=None):
            titles = {"character-pass": "Character pass", "screenplay": "Screenplay", "render": "Render"}
            return {"id": jid, "seq": seq, "status": "queued", "kind": kind, "slug": "demo", "heavy": True,
                    "batch": "b2", "created": "2026-11-11T11:05:00", "meta": {"chapters": [n] if n else []},
                    "title": f"{titles[kind]} · ch {n}" if n else "Cast · everyone who needs a voice",
                    "args": [kind, "demo"] + ([str(n)] if n else [])}

        render = {**queued("r38", 1, "render", 38), "status": "running", "batch": "b1",
                  "nomad": {"target": "mac", "node": "mac", "stage": 0, "stages": [{"class": "breeze", "args": []}],
                            "contact": "t"}}
        batch = [queued("c39", 2, "character-pass", 39), queued("c40", 3, "character-pass", 40),
                 {**queued("cast", 4, "cast"), "title": "Cast · everyone who needs a voice"},
                 queued("s39", 5, "screenplay", 39), queued("r39", 6, "render", 39)]
        self.m.jobs = {j["id"]: j for j in [render] + batch}
        dispatch = []
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["c39"])
        self.assertEqual(self.m.wait_reasons["c40"], "waiting for Character pass · ch 39, which changes the whole serial")
        self.assertEqual(self.m.wait_reasons["r39"],
                         "waiting for Character pass · ch 40, queued ahead of it on the same serial")
        self.assertEqual({j["id"]: j["status"] for j in batch},
                         {"c39": "running", "c40": "queued", "cast": "queued", "s39": "queued", "r39": "queued"})

        # Bundles and its own chapter: a character pass waits until a running render has fetched its bundle, and a
        # character pass on the render's chapter waits for the render.
        self.m.jobs = {}
        unfetched = {**render, "nomad": {**render["nomad"], "contact": None}}
        self.assertEqual(self.m._blocker_for(queued("c41", 9, "character-pass", 41), "llm", [unfetched]),
                         "waiting for Render · ch 38 to fetch its bundle")
        self.assertEqual(self.m._blocker_for(queued("c38", 9, "character-pass", 38), "llm", [render]),
                         "waiting for Render · ch 38, which changes the whole serial")

    def test_a_batch_job_waits_only_for_the_jobs_it_needs(self):
        self.m.workers.append(worker("dev", ["llm"], "linux"))
        self.m.nomad_state["up"] = True

        def queued(jid, seq, kind, n):
            label = "Screenplay" if kind == "screenplay" else "Render"
            return {"id": jid, "seq": seq, "status": "queued", "kind": kind, "slug": "demo", "heavy": True,
                    "batch": "b1", "created": "2026-11-09T23:08:32", "title": f"{label} · ch {n}",
                    "meta": {"chapters": [n], "lines": 200}, "args": [kind, "demo", str(n)]}

        first = {**queued("s18", 1, "screenplay", 18), "status": "running",
                 "nomad": {"stage": 0, "stages": [{"class": "llm", "args": []}], "contact": "t"}}
        self.m.jobs = {j["id"]: j for j in (first, queued("s19", 2, "screenplay", 19),
                                            queued("s20", 3, "screenplay", 20), queued("s21", 4, "screenplay", 21),
                                            queued("r18", 5, "render", 18), queued("r19", 6, "render", 19),
                                            queued("r21", 7, "render", 21))}
        dispatch = []
        # While chapter 18's screenplay runs, its render waits for it. The screenplays of chapters 19 and 20 start
        # beside it, and chapter 21's jobs wait their turn, because three Claude jobs run at once.
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["s19", "s20"])
        self.assertEqual(self.m.wait_reasons["r18"], "waiting for Screenplay · ch 18, which works on the same chapter")
        self.assertEqual(self.m.wait_reasons["r19"], "waiting for Screenplay · ch 19, which works on the same chapter")
        self.assertEqual(self.m.wait_reasons["s21"], "waiting for a running Claude job to finish, 3 run at once")
        self.assertEqual(self.m.wait_reasons["r21"],
                         "waiting for Screenplay · ch 21, queued ahead of it on the same serial")
        # Once it is written, chapter 18 renders beside the screenplays of chapters 19 and 20, which are earlier in
        # the batch, and chapter 21's screenplay starts in its place.
        first["status"] = "done"
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["s19", "s20", "s21", "r18"])
        self.assertEqual(self.m.jobs["r18"]["nomad"]["target"], "laptop")
        self.assertEqual(self.m.wait_reasons["r19"], "waiting for Screenplay · ch 19, which works on the same chapter")
        self.assertEqual(self.m.wait_reasons["r21"], "waiting for Screenplay · ch 21, which works on the same chapter")

    def test_a_screenplay_passes_a_claude_job_that_waits_ahead_of_it(self):
        self.m.workers.append(worker("dev", ["llm"], "linux"))
        self.m.nomad_state["up"] = True
        render = {"id": "r38", "seq": 1, "status": "running", "kind": "render", "slug": "demo", "heavy": True,
                  "meta": {"chapters": [38]}, "args": ["render", "demo", "38"], "title": "Render · ch 38",
                  "nomad": {"target": "mac", "node": "mac", "stage": 0, "stages": [{"class": "breeze", "args": []}],
                            "contact": "t"}}

        def script(jid, seq, n, slug="demo"):
            return {"id": jid, "seq": seq, "status": "queued", "kind": "screenplay", "slug": slug, "heavy": True,
                    "meta": {"chapters": [n]}, "args": ["screenplay", slug, str(n)], "title": f"Screenplay · ch {n}"}

        self.m.jobs = {j["id"]: j for j in (render, script("s38", 2, 38), script("s39", 3, 39),
                                            script("o1", 4, 1, slug="other"))}
        dispatch = []
        # Chapter 38's screenplay waits for the chapter's render. The screenplays queued after it, of another
        # chapter and of another serial, start without it.
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["s39", "o1"])
        self.assertEqual(self.m.wait_reasons, {"s38": "waiting for Render · ch 38, which works on the same chapter"})
        # A cast's briefs stage waits until the serial's running screenplay has fetched its bundle, and a screenplay
        # queued after it takes the third place meanwhile. Once the bundle is fetched and a place is free, the briefs
        # stage starts beside the screenplay. A fourth Claude job waits for one of the three.
        self.m.jobs["cast"] = {"id": "cast", "seq": 5, "status": "queued", "kind": "cast", "slug": "demo",
                               "heavy": True, "meta": {"characters": ["eole"]}, "args": ["cast", "demo"],
                               "title": "Cast · eole"}
        self.m.jobs["o2"] = script("o2", 6, 2, slug="other")
        self.m._schedule(0.0, dispatch)
        self.assertEqual(self.m.wait_reasons["cast"], "waiting for Screenplay · ch 39 to fetch its bundle")
        self.assertEqual(dispatch, ["s39", "o1", "o2"])
        self.m.jobs["o2"]["status"] = "done"
        self.m.jobs["s39"]["nomad"]["contact"] = "t"
        self.m.jobs["o3"] = script("o3", 7, 3, slug="other")
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["s39", "o1", "o2", "cast"])
        self.assertEqual(self.m.jobs["cast"]["nomad"]["stages"][0]["class"], "llm")
        self.assertEqual(self.m.wait_reasons["o3"], "waiting for a running Claude job to finish, 3 run at once")

    def test_a_batch_that_imports_chapters_and_processes_them_runs_in_order(self):
        from unittest import mock
        from serial_audio import library as lib
        from serial_audio.web import jobs

        base = Path(self.tmp.name)
        for patch in (mock.patch.object(lib, "LIBRARY", base / "library"),
                      mock.patch.object(jobs, "INCOMING", base / "incoming")):
            patch.start()
            self.addCleanup(patch.stop)
        lib.save_serial("demo", {"title": "Demo"})
        lib.chapter_dir("demo", 1).mkdir(parents=True)
        (base / "incoming" / "demo").mkdir(parents=True)
        for n in (7, 8):
            (base / "incoming" / "demo" / f"ch{n}.html").write_text("<html>", encoding="utf-8")
        self.m.workers.append(worker("dev", ["llm"], "linux"))
        self.m.nomad_state["up"] = True

        def requests(*numbers):
            def each(kind, params):
                return [{"kind": kind, "params": {"slug": "demo", **params(n)}} for n in numbers]
            return (each("fetch", lambda n: {"chapter": n, "html": f"ch{n}.html"})
                    + each("character-pass", lambda n: {"chapters": str(n)})
                    + [{"kind": "cast", "params": {"slug": "demo"}}]
                    + each("screenplay", lambda n: {"chapters": str(n)})
                    + each("render", lambda n: {"chapter": n}))

        def schedule():
            dispatch = []
            with mock.patch.object(self.m, "_launch_local", side_effect=lambda j: j.update(status="running")):
                self.m._schedule(0.0, dispatch)
            return dispatch

        # Queued behind the import of its chapter, a character pass or a screenplay names the chapter before it is
        # on disk. Queued alone, it names only chapters on disk.
        batch = self.m.submit(requests(7))
        imp, chars, cast, script, render = batch
        self.assertEqual([j["meta"].get("chapters") for j in batch], [[7], [7], None, [7], [7]])
        self.assertEqual(len({j["batch"] for j in batch}), 1)
        self.assertEqual(jobs.build("screenplay", {"slug": "demo", "chapters": "7"})["meta"]["chapters"], [])
        self.assertEqual(jobs.build("character-pass", {"slug": "demo", "chapters": "all"}, coming={"demo": {7}})
                         ["meta"]["chapters"], [1, 7])

        # The import starts on the app's machine, and the rest of the batch waits for it in order.
        self.assertEqual(schedule(), [])
        self.assertEqual(imp["status"], "running")
        self.assertIn("Import saved page · ch 7", self.m.wait_reasons[chars["id"]])
        self.assertEqual(self.m.wait_reasons[render["id"]],
                         "waiting for Character pass · ch 7, queued ahead of it on the same serial")
        # The character pass follows the import, and the cast follows the character pass.
        imp["status"] = "done"
        self.assertEqual(schedule(), [chars["id"]])
        chars["status"] = "done"
        self.assertEqual(schedule(), [cast["id"]])
        self.assertEqual(self.m.wait_reasons[render["id"]],
                         "waiting for Screenplay · ch 7, queued ahead of it on the same serial")
        # The render waits for its chapter's screenplay, which follows the cast.
        cast["status"] = "done"
        self.assertEqual(schedule(), [script["id"]])
        self.assertEqual(self.m.wait_reasons[render["id"]],
                         "waiting for Screenplay · ch 7, which works on the same chapter")

        # A failed import skips its chapter's character pass and the jobs that wait for that one: the cast, and
        # with it every screenplay and render. The other import and the earlier chapter's character pass go on.
        self.m.jobs = {}
        batch = self.m.submit(requests(7, 8))
        self.m._finish(batch[1], "failed", "exit 1", 1)
        self.assertEqual([j["status"] for j in batch], ["queued", "failed", "queued"] + ["skipped"] * 6)
        self.assertEqual([j["title"] for j in batch[:4]], ["Import saved page · ch 7", "Import saved page · ch 8",
                                                           "Character pass · ch 7", "Character pass · ch 8"])

    def test_a_failed_batch_job_skips_only_the_jobs_that_wait_for_it(self):
        def queued(jid, seq, kind, n=None, batch="b1"):
            return {"id": jid, "seq": seq, "status": "queued", "kind": kind, "slug": "demo", "batch": batch,
                    "created": "2026-11-09T23:08:32", "title": jid, "meta": {"chapters": [n] if n else []},
                    "args": [kind, "demo"]}

        def batch():
            jobs = [queued("s20", 1, "screenplay", 20), queued("s21", 2, "screenplay", 21),
                    queued("r18", 3, "render", 18), queued("r20", 4, "render", 20), queued("r21", 5, "render", 21),
                    queued("other", 6, "render", 20, batch="b2")]
            self.m.jobs = {j["id"]: j for j in jobs}
            return self.m.jobs

        def statuses():
            return {jid: j["status"] for jid, j in self.m.jobs.items()}

        # A failed screenplay skips its chapter's render. The other chapters and the other batch go on.
        jobs = batch()
        self.m._finish(jobs["s20"], "failed", "exit 1 on dev", 1)
        self.assertEqual(statuses(), {"s20": "failed", "s21": "queued", "r18": "queued", "r20": "skipped",
                                      "r21": "queued", "other": "queued"})
        self.assertEqual(jobs["r20"]["note"], "an earlier job in the batch failed")
        # A lost render skips nothing, because no other job of the batch waits for it.
        jobs = batch()
        self.m._finish(jobs["r18"], "lost", "the Nomad allocation on laptop ended lost without a result")
        self.assertEqual([jid for jid, status in statuses().items() if status == "skipped"], [])
        # A render that was moved ahead of its screenplay in the queue waited for it too, so it is skipped.
        jobs = batch()
        jobs["r20"]["seq"] = 0.5
        self.m._finish(jobs["s20"], "failed", "exit 1 on dev", 1)
        self.assertEqual(jobs["r20"]["status"], "skipped")
        # A failed job that changes the whole serial skips every queued job of the batch.
        jobs = batch()
        jobs["cast"] = queued("cast", 0.5, "cast")
        self.m._finish(jobs["cast"], "failed", "exit 1 on laptop", 1)
        self.assertEqual(statuses(), {"s20": "skipped", "s21": "skipped", "r18": "skipped", "r20": "skipped",
                                      "r21": "skipped", "other": "queued", "cast": "failed"})
        # A job skipped with the failed one skips the later jobs that wait for it, and not the earlier ones. The
        # character pass is on the failed render's chapter, because one on another chapter doesn't wait for a render.
        jobs = batch()
        jobs["s20"]["status"] = "done"
        jobs["c20"] = queued("c20", 4.5, "character-pass", 20)
        self.m._finish(jobs["r20"], "failed", "exit 1 on laptop", 1)
        self.assertEqual(statuses(), {"s20": "done", "s21": "queued", "r18": "queued", "r20": "failed",
                                      "r21": "skipped", "other": "queued", "c20": "skipped"})
        # A stopped job still skips every queued job of the batch.
        jobs = batch()
        self.m._finish(jobs["r18"], "stopped", "stopped from the web app")
        self.assertEqual(statuses(), {"s20": "skipped", "s21": "skipped", "r18": "stopped", "r20": "skipped",
                                      "r21": "skipped", "other": "queued"})

    def test_a_stop_request_skips_the_batch_s_queued_jobs_before_the_worker_confirms(self):
        self.m.workers.append(worker("dev", ["llm"], "linux"))
        self.m.nomad_state["up"] = True

        def queued(jid, seq, kind, n):
            return {"id": jid, "seq": seq, "status": "queued", "kind": kind, "slug": "demo", "heavy": True,
                    "batch": "b1", "created": "2026-11-09T23:08:32", "title": jid,
                    "meta": {"chapters": [n], "lines": 200}, "args": [kind, "demo", str(n)]}

        render = {**queued("r18", 1, "render", 18), "status": "running",
                  "nomad": {"key": "k", "target": "laptop", "node": "laptop", "stage": 0,
                            "stages": [{"class": "breeze", "args": []}], "contact": "t"}}
        self.m.jobs = {j["id"]: j for j in (render, queued("s19", 2, "screenplay", 19), queued("r19", 3, "render", 19))}
        # The request skips the queued jobs at once, so the next scheduling pass starts none of them while the
        # worker still runs.
        self.m.cancel("r18")
        self.assertEqual({jid: j["status"] for jid, j in self.m.jobs.items()},
                         {"r18": "running", "s19": "skipped", "r19": "skipped"})
        dispatch = []
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, [])
        # A worker that is lost before it confirms the stop changes nothing for the batch.
        self.m._finish(render, "lost", "the Nomad allocation on laptop ended lost without a result")
        self.assertEqual({jid: j["status"] for jid, j in self.m.jobs.items()},
                         {"r18": "lost", "s19": "skipped", "r19": "skipped"})

    def test_a_breeze_cast_takes_the_nvidia_node_first(self):
        cast = job("cast", args=["cast", "demo", "--engine", "breeze"])
        self.assertEqual(self.m._pick_node(cast, "breeze", [])[:2], ("laptop", "breeze"))
        self.assertEqual(self.m._pick_node(cast, "breeze", [self.running_on("laptop")])[:2], ("mac", "breeze"))
        self.assertEqual(self.m._pick_node(job("cast", args=["cast", "demo"]), "comfyui", [])[:2],
                         ("laptop", "comfyui"))

    def test_a_job_needs_the_node_that_holds_its_takes_only_while_the_app_lacks_them(self):
        from unittest import mock
        self.m.placement = {"chapters": {"demo": {"1": "mac"}}}
        check = job("check-takes", chapters=[1], args=["check-takes", "demo", "1"])
        join = job("render", chapters=[1], args=["render", "demo", "1", "--decode-only"])
        join["meta"].update(decode_only=True, engine="breeze")
        decode = job("render", chapters=[1], args=["render", "demo", "1", "--decode-only"], comfy=True)
        decode["meta"].update(decode_only=True, engine="drama3")
        # A take check is a breeze job, so the Mac runs it without a change of class.
        self.assertEqual(job_stages(check)[0]["class"], "breeze")
        with mock.patch.object(self.m, "_mirrored", return_value=False):
            # A Drama 3 decode reads no Breeze takes, so it goes to the ComfyUI node whatever node holds them.
            self.assertEqual(self.m.holder(decode), ("mac", False))
            self.assertEqual(self.m._pick_node(decode, "comfyui", [])[:2], ("laptop", "comfyui"))
            # The app has no copy of the takes: the check and the join get the Mac, and wait while it is busy
            # or down.
            self.assertEqual(self.m._pick_node(check, "breeze", [])[:2], ("mac", "breeze"))
            self.assertEqual(self.m._pick_node(join, "breeze", [])[:2], ("mac", "breeze"))
            node, cls, why = self.m._pick_node(check, "breeze", [self.running_on("mac")])
            self.assertIsNone(node)
            self.assertIn("mac, which holds the files", why)
            # A node that does not offer the job's class cannot run it.
            node, cls, why = self.m._pick_node(check, "comfyui", [])
            self.assertIsNone(node)
            self.assertEqual(why, "mac, which holds the files this job needs, does not offer comfyui jobs")
            self.m.workers[1]["ready"], self.m.workers[1]["status"] = False, "down"
            node, cls, why = self.m._pick_node(join, "breeze", [])
            self.assertIsNone(node)
            self.assertEqual(why, "waiting for mac, which holds the files this job needs: mac is down")
        # a scheduling pass starts with no job waiting for a node
        self.m._plan = {}
        with mock.patch.object(self.m, "_mirrored", return_value=True):
            # The app holds every take: with the Mac down, the check and the join run on the laptop.
            self.assertEqual(self.m._pick_node(check, "breeze", [])[:2], ("laptop", "breeze"))
            self.assertEqual(self.m._pick_node(join, "breeze", [])[:2], ("laptop", "breeze"))
            # With both nodes ready they stay on the Mac, which need not fetch the takes.
            self.m.workers[1]["ready"], self.m.workers[1]["status"] = True, "ready"
            self.assertEqual(self.m._pick_node(check, "breeze", [])[:2], ("mac", "breeze"))
            self.assertEqual(self.m._pick_node(join, "breeze", [])[:2], ("mac", "breeze"))
            # With the Mac busy they take the laptop instead of waiting for it.
            self.assertEqual(self.m._pick_node(check, "breeze", [self.running_on("mac")])[:2], ("laptop", "breeze"))
        render = job("render", chapters=[1], args=["render", "demo", "1"])
        render["meta"]["engine"] = "breeze"
        with mock.patch("serial_audio.web.nomad.reusable_takes", return_value=(200, 200)):
            self.assertEqual(self.m._pick_node(render, "breeze", [])[:2], ("mac", "breeze"))

    def test_a_render_goes_where_it_finishes_first_counting_the_takes_a_node_holds(self):
        from unittest import mock
        self.m.placement = {"chapters": {"demo": {"1": "mac"}}}
        render = {"kind": "render", "slug": "demo", "args": ["render", "demo", "1", "--seed", "2"],
                  "meta": {"chapters": [1], "engine": "breeze", "runner": "cpp", "lines": 255}}
        with mock.patch.object(self.m, "_mirrored", return_value=False):
            # The app has no copy of the takes, so only the Mac can reuse them. 25 takes on the Mac beat 255 on
            # the laptop, and 252 on the Mac do not.
            with mock.patch("serial_audio.web.nomad.reusable_takes", return_value=(230, 255)) as reuse:
                self.assertEqual(self.m._pick_node(render, "breeze", [])[:2], ("mac", "breeze"))
            reuse.assert_called_once_with("demo", 1, 2, "cpp")
            with mock.patch("serial_audio.web.nomad.reusable_takes", return_value=(3, 255)):
                self.assertEqual(self.m._pick_node(render, "breeze", [])[:2], ("laptop", "breeze"))
                # With the laptop busy, waiting for it still beats a full render on the Mac.
                node, cls, why = self.m._pick_node(render, "breeze", [self.running_on("laptop")])
                self.assertIsNone(node)
                self.assertTrue(why.startswith("waiting for laptop, about"), why)
        # a scheduling pass starts with no job waiting for a node
        self.m._plan = {}
        with mock.patch.object(self.m, "_mirrored", return_value=True), \
                mock.patch("serial_audio.web.nomad.reusable_takes", return_value=(230, 255)):
            # The app holds the takes, so the laptop reuses them too: 25 takes there, plus fetching the rest,
            # beat 25 on the Mac. With the laptop down the Mac makes the 25.
            self.assertEqual(self.m._pick_node(render, "breeze", [])[:2], ("laptop", "breeze"))
            self.assertLess(self.m._duration(render, "laptop", "mac"), 255 * self.m._rate("laptop"))
            self.m.workers[0]["ready"] = False
            self.assertEqual(self.m._pick_node(render, "breeze", [])[:2], ("mac", "breeze"))
            self.m.workers[0]["ready"] = True
        # When the reuse cannot be worked out, the render is costed at all its takes, so it goes to the fast node.
        with mock.patch("serial_audio.web.nomad.reusable_takes", side_effect=FileNotFoundError("no script")):
            self.assertEqual(self.m._pick_node(render, "breeze", [])[:2], ("laptop", "breeze"))

    def test_a_job_waits_for_a_busy_node_only_when_that_saves_more_than_the_margin(self):
        from unittest import mock
        render = job("render", chapters=[2], args=["render", "demo", "2"])
        render["meta"]["lines"] = 200
        busy = {**self.running_on("laptop"), "status": "running"}
        rates = {"laptop": 2.0, "mac": 2.9}
        with mock.patch.object(self.m, "progress", return_value={"phase": "checking", "eta": 30}), \
                mock.patch.object(self.m, "_rate", side_effect=rates.get):
            # The laptop frees in 30 s and renders in 590 s. The Mac renders now in 770 s. Waiting saves 150 s.
            node, cls, why = self.m._pick_node(render, "breeze", [busy])
            self.assertIsNone(node)
            self.assertEqual(why, "waiting for laptop, about 10 min, instead of about 13 min on mac now")
            # At 2.7 s per take the Mac renders now in 730 s. Waiting saves 110 s, under the margin, so the Mac starts.
            rates["mac"] = 2.7
            self.assertEqual(self.m._pick_node(render, "breeze", [busy])[:2], ("mac", "breeze"))

    def test_queued_jobs_fill_the_fast_node_before_the_slow_one_takes_any(self):
        from unittest import mock
        self.m.nomad_state["up"] = True
        running = {**self.running_on("laptop"), "id": "r0", "seq": 0, "status": "running", "title": "Render · ch 9"}
        queued = [{"id": f"r{n}", "seq": n, "status": "queued", "kind": "render", "slug": "demo", "heavy": True,
                   "created": "2026-11-08T15:00:00", "title": f"Render · ch {n}",
                   "meta": {"chapters": [n], "lines": 100}, "args": ["render", "demo", str(n)]}
                  for n in (1, 2)]
        self.m.jobs = {j["id"]: j for j in [running, *queued]}
        dispatch = []
        with mock.patch.object(self.m, "progress", return_value={"phase": "checking", "eta": 30}), \
                mock.patch.object(self.m, "_rate", side_effect={"laptop": 3.0, "mac": 5.0}.get):
            self.m._schedule(0.0, dispatch)
        # Chapter 1 waits for the laptop: 30 s plus 440 s against 640 s on the Mac. Chapter 2 would wait behind it
        # as well, 910 s, so it takes the Mac now.
        self.assertEqual(self.m.wait_reasons["r1"], "waiting for laptop, about 8 min, instead of about 11 min on mac now")
        self.assertEqual(dispatch, ["r2"])
        self.assertEqual(self.m.jobs["r2"]["nomad"]["target"], "mac")

    def test_backlog_work_starts_only_when_nothing_else_runs_or_waits(self):
        from unittest import mock
        from serial_audio.web.jobs import start_order
        self.m.nomad_state["up"] = True

        def queued(jid, seq, n, backlog=False):
            return {"id": jid, "seq": seq, "status": "queued", "kind": "render", "slug": "demo", "heavy": True,
                    "created": "2026-11-12T15:00:00", "title": f"Render · ch {n}", "backlog": backlog,
                    "meta": {"chapters": [n], "lines": 100}, "args": ["render", "demo", str(n)]}

        def schedule() -> list[str]:
            dispatch = []
            with mock.patch("serial_audio.web.nomad.reusable_takes", return_value=(0, 100)):
                self.m._schedule(0.0, dispatch)
            return dispatch

        # Backlog renders of chapters 5 and 6 were queued ahead of a render of chapter 5.
        self.m.jobs = {j["id"]: j for j in (queued("b5", 1, 5, True), queued("b6", 2, 6, True), queued("r5", 3, 5))}
        self.assertEqual([j["id"] for j in sorted(self.m.jobs.values(), key=start_order)], ["r5", "b5", "b6"])

        # The render starts first. It doesn't wait for the backlog work queued ahead of it on its chapter, and
        # the backlog work waits for it.
        self.assertEqual(schedule(), ["r5"])
        self.assertEqual((self.m.jobs["r5"]["status"], self.m.wait_reasons),
                         ("running", {"b5": "backlog work, waiting for Render · ch 5 to finish",
                                      "b6": "backlog work, waiting for Render · ch 5 to finish"}))

        # With the render done, one backlog job starts and the other waits for it, though the Mac is free.
        self.m.jobs["r5"]["status"] = "done"
        self.assertEqual(schedule(), ["b5"])
        self.assertEqual(self.m.wait_reasons, {"b6": "backlog work, waiting for Render · ch 5 to finish"})

        # A render queued while backlog work runs is placed by the usual rules, and the backlog work keeps running.
        self.m.jobs["r7"] = queued("r7", 4, 7)
        with mock.patch.object(self.m, "progress", return_value={"phase": "generating", "eta": 3000}):
            self.assertEqual(schedule(), ["r7"])
        self.assertEqual((self.m.jobs["b5"]["status"], self.m.jobs["r7"]["nomad"]["target"]), ("running", "mac"))

        # Backlog work also waits while a job that is not backlog work is queued and can't start.
        self.m.jobs["b5"]["status"] = self.m.jobs["r7"]["status"] = "done"
        self.m.jobs["r8"] = queued("r8", 5, 8)
        self.m.paused = True
        self.assertEqual(schedule(), [])
        self.assertEqual(self.m.wait_reasons, {"r8": "the queue is on hold",
                                               "b6": "backlog work, waiting for the queue to empty"})

        # A backlog cast that has run its briefs waits for its voices stage as a queued job. It counts as running:
        # it starts before a character pass queued after it, the pass waits for it, and other backlog work does too.
        self.m.paused = False
        self.m.workers.append(worker("dev", ["llm"], "linux"))
        self.m.jobs = {"c": {"id": "c", "seq": 6, "status": "queued", "kind": "cast", "slug": "demo", "heavy": True,
                             "backlog": True, "created": "2026-11-12T15:00:00", "title": "Cast · eole",
                             "meta": {"characters": ["eole"]}, "args": ["cast", "demo"],
                             "nomad": {"key": "k", "stage": 1, "stages": [{"class": "llm", "args": []},
                                                                          {"class": "breeze", "args": []}]}},
                       "p": {"id": "p", "seq": 7, "status": "queued", "kind": "character-pass", "slug": "demo",
                             "heavy": True, "created": "2026-11-12T15:00:00", "title": "Character pass · ch 5",
                             "meta": {"chapters": [5]}, "args": ["character-pass", "demo", "5"]},
                       "b6": queued("b6", 8, 6, True)}
        self.assertEqual([j["id"] for j in sorted(self.m.jobs.values(), key=start_order)], ["c", "p", "b6"])
        self.assertEqual(schedule(), ["c"])
        self.assertEqual(self.m.wait_reasons, {"p": "waiting for Cast · eole, which changes the whole serial",
                                               "b6": "backlog work, waiting for Cast · eole to finish"})

    def test_a_node_s_take_rate_comes_from_the_renders_it_finished(self):
        from unittest import mock
        from serial_audio import library as lib
        with mock.patch.object(lib, "LIBRARY", Path(self.tmp.name) / "library"):
            def finish(n, node, takes, secs, success=True, lines=None):
                lib.save_yaml(lib.chapter_dir("demo", n) / "chapter.yaml",
                              {"render": {"engine": "breeze", "runner": "cpp", "generated_takes": takes,
                                          "generate_seconds": secs},
                               **({"script": {"lines": lines}} if lines else {})})
                render = {**job("render", chapters=[n], args=["render", "demo", str(n)]), "id": f"r{n}"}
                self.m._record_placement(render, node, {"applied": [f"chapters/{n:04d}/chapter.yaml"]}, success)

            finish(1, "mac", 255, 4468)
            finish(2, "mac", 3, 60)
            finish(3, "laptop", 246, 689)
            finish(4, "laptop", 231, 2000, success=False)
            # A record of fewer than 20 takes and a failed render are skipped, and a node without records gets its
            # GPU type's default.
            self.assertAlmostEqual(self.m._rate("mac"), 4468 / 255, places=2)
            self.assertAlmostEqual(self.m._rate("laptop"), 689 / 246, places=2)
            self.m.workers.append(worker("box", ["breeze"], "linux", "none"))
            self.assertEqual(self.m._rate("box"), 60.0)
            # Only the last three records of a node count.
            for n in (5, 6, 7):
                finish(n, "laptop", 100, 100 * n)
            self.assertAlmostEqual(self.m._rate("laptop"), 6.0, places=2)
            # A touch-up render that made under half of its chapter's lines is skipped, and one that made half
            # or more counts.
            finish(8, "laptop", 21, 22, lines=231)
            self.assertAlmostEqual(self.m._rate("laptop"), 6.0, places=2)
            finish(9, "laptop", 120, 240, lines=231)
            self.assertAlmostEqual(self.m._rate("laptop"), 5.0, places=2)

    def test_paths_name_their_node_when_one_node_runs_that_os(self):
        self.assertEqual(self.m._node_for_paths(["D:\\comfy\\out\\a.wav"]), "laptop")
        self.assertEqual(self.m._node_for_paths(["/Users/aj/serial-audio/output/a.wav"]), "mac")
        self.assertIsNone(self.m._node_for_paths(["/home/aj/a.wav"]))
        self.m.workers.append(worker("laptop2", ["breeze"], "windows"))
        self.assertIsNone(self.m._node_for_paths(["D:\\comfy\\out\\a.wav"]))

    def test_a_voice_pick_runs_on_the_app_s_machine_whichever_node_made_its_candidate(self):
        # tests.test_casting covers the filing: a pick by seed, an approval of the current voice and a seed no
        # candidate has, each with candidates that the laptop and the Mac made.
        def pick(*extra):
            return {"kind": "pick-voice", "slug": "demo", "meta": {"characters": ["keeper"]}, "heavy": False,
                    "args": ["pick-voice", "demo", "keeper", *extra]}

        self.m.placement = {"voices": {"demo": {"keeper": "mac"}}}
        for p in (pick("--seed", "5002"), pick("--seed", "13000"), pick(), pick("--seed", "9")):
            # No node holds a pick, and it is not a cluster job, so it runs with both GPU nodes down.
            self.assertIsNone(job_stages(p))
            self.assertEqual(self.m.lane(p), "local")
            self.assertEqual(self.m.holder(p), (None, False))
        # The pick's CLI is told where the app keeps the candidates the nodes sent it.
        import os
        import subprocess
        from unittest import mock
        from serial_audio import machine
        with mock.patch.dict(os.environ), mock.patch.object(subprocess, "Popen") as popen:
            popen.return_value.pid = 1
            self.m._launch_local({**pick(), "id": "p1"})
            self.assertEqual(popen.call_args.kwargs["env"][machine.MEDIA_MIRROR_ENV], str(self.m.media_dir))

    def test_a_sync_job_runs_on_the_node_it_names_and_waits_for_no_other_job(self):
        # Build: the job names a GPU node, has no CLI step, and is refused for a node that is not one.
        from unittest import mock
        with mock.patch("serial_audio.web.nomad._slug", return_value="demo"):
            built = self.m._build("sync-media", {"slug": "demo", "node": "mac"}, {})
            with self.assertRaises(ValueError):
                self.m._build("sync-media", {"slug": "demo", "node": "dev"}, {})
        self.assertEqual((built["args"], built["meta"], built["title"]),
                         ([], {"node": "mac"}, "Send audio to the app · mac"))
        sync = {"kind": "sync-media", "slug": "demo", **built}

        # Place: one breeze dispatch pinned to that node, which waits while the node is busy.
        self.assertEqual(job_stages(sync), [{"class": "breeze", "args": []}])
        self.assertEqual(self.m._pick_node(sync, "breeze", [])[:2], ("mac", "breeze"))
        node, cls, why = self.m._pick_node(sync, "breeze", [self.running_on("mac")])
        self.assertIsNone(node)
        self.assertIn("mac, which holds the files", why)

        # Wait: it writes no library file, so it and a cast or a render of its serial do not wait for each other.
        for other in (job("cast"), job("render", chapters=[1]), job("character-pass", chapters=[1])):
            self.assertFalse(conflicts(sync, other))
            self.assertFalse(conflicts(other, sync))

    def test_the_mirror_holds_a_chapter_once_every_take_has_a_file_there(self):
        from unittest import mock
        from serial_audio import library as lib
        with mock.patch.object(lib, "LIBRARY", Path(self.tmp.name) / "library"):
            # A chapter without saved takes needs nothing from a node.
            self.assertTrue(self.m._mirrored("demo", 1))
            self.assertTrue(self.m._mirrored(None, 1))
            files = ["D:\\comfy\\out\\breeze\\demo\\ch0001\\cpp\\l0000.wav",
                     "D:\\comfy\\out\\breeze\\demo\\ch0001\\cpp\\l0001.wav"]
            lib.save_json(lib.chapter_dir("demo", 1) / "breeze_takes.json",
                          {"takes": {str(i): {"key": "k", "file": f} for i, f in enumerate(files)}})
            self.assertFalse(self.m._mirrored("demo", 1))
            check = job("check-takes", chapters=[1], args=["check-takes", "demo", "1"])
            self.assertEqual(self.m.holder(check), ("laptop", True))

            # One take in the mirror is not enough, and the answer follows the worker's uploads.
            for i, raw in enumerate(files):
                target = self.m.media_dir / exchange.mirror_rel(raw)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"RIFF")
                self.m.worker_media_stored()
                self.assertEqual(self.m._mirrored("demo", 1), i == 1)
            self.assertEqual(self.m.holder(check), ("laptop", False))

    def test_a_restart_during_a_dispatch_dispatches_again_with_the_same_token(self):
        from unittest import mock
        from serial_audio import library as lib
        from serial_audio.web.nomad import NomadClient, NomadJobManager

        patch = mock.patch.object(lib, "LIBRARY", Path(self.tmp.name) / "library")
        patch.start()
        self.addCleanup(patch.stop)
        lib.save_serial("demo", {"title": "Demo"})
        lib.chapter_dir("demo", 1).mkdir(parents=True)

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

        j = self.m.submit([{"kind": "screenplay", "params": {"slug": "demo", "chapters": "1"}}])[0]
        self.m._mark_dispatch(j)
        self.m._save()
        self.m._lock_file.close()
        again = NomadJobManager(Client("http://127.0.0.1:9", "token"), state_dir=Path(self.tmp.name), autostart=False)
        self.m = again
        again.tick()
        self.assertEqual(Client.calls, [("serial-audio-llm", f"{j['id']}-0")])
        self.assertEqual(again.jobs[j["id"]]["nomad"]["dispatched"], "serial-audio-llm/dispatch-1")


def wav_bytes(samples: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(samples)
    return buf.getvalue()


def other_node(name: str) -> str:
    """A take's path on a node of the other system than the one the test runs on."""
    if os.name == "nt":
        return "/Users/nobody/serial-audio/output/breeze/demo/ch0001/cpp/" + name
    return "D:\\comfy\\ComfyUI\\output\\breeze\\demo\\ch0001\\cpp\\" + name


class Lines:
    def __init__(self):
        self.lines = []

    def line(self, text):
        self.lines.append(text)


class FakeApp:
    """The worker's view of the app, answered by a NomadJobManager's media methods without HTTP."""

    def __init__(self, manager, jid="j1", key="k"):
        self.m, self.jid, self.key = manager, jid, key
        self.sent, self.fetched = [], []

    def request(self, method, path, data=None, timeout=60):
        from serial_audio.web.nomad import WorkerError
        from serial_audio.worker import AppError
        try:
            if path == "media/have":
                return self.m.worker_media_have(self.jid, self.key, json.loads(data)["paths"])
            rel = urllib.parse.unquote(path.split("path=", 1)[1])
            target = self.m.worker_media_target(self.jid, self.key, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.m.worker_media_stored()
            self.sent.append(rel)
            return {"bytes": len(data)}
        except WorkerError as e:
            raise AppError(e.status, str(e)) from None

    def fetch(self, path, timeout=120):
        from serial_audio.worker import AppError
        rel = urllib.parse.unquote(path[len("/media/mirror/"):])
        self.fetched.append(rel)
        try:
            return (self.m.media_dir / rel).read_bytes()
        except OSError:
            raise AppError(404, f"no file {rel}") from None


class MediaTransferTest(unittest.TestCase):
    def setUp(self):
        from serial_audio.web.nomad import NomadClient, NomadJobManager
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.m = NomadJobManager(NomadClient("http://127.0.0.1:9", "token"), state_dir=base / "state", autostart=False)
        self.m.jobs["j1"] = {"id": "j1", "status": "running", "nomad": {"key": "k"}}
        self.app = FakeApp(self.m)
        self.log = Lines()
        self.root = base / "output"
        self.folder = base / "work" / "library" / "demo"
        self.takes_dir = self.root / "breeze" / "demo" / "ch0001" / "cpp"
        self.takes_dir.mkdir(parents=True)
        (self.folder / "chapters" / "0001").mkdir(parents=True)

    def tearDown(self):
        self.m._lock_file.close()
        self.tmp.cleanup()

    def mirror(self, raw: str, data: bytes) -> None:
        path = self.m.media_dir / exchange.mirror_rel(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def save_takes(self, takes: dict) -> Path:
        path = self.folder / "chapters" / "0001" / exchange.TAKES_FILE
        path.write_text(json.dumps({"engine": "breeze", "runner": "cpp", "takes": takes}), encoding="utf-8")
        return path

    def test_a_worker_fetches_the_takes_another_node_made(self):
        from serial_audio import worker

        # Setup: lines 0 and 1 come from another node and are in the mirror. The mirror's copy of line 2 holds other
        # audio than its entry names, and the mirror lacks line 3. Line 4 is this node's own take, and the mirror
        # holds an older copy of it.
        pcm = [bytes([i + 1]) * 400 for i in range(5)]
        wavs = [wav_bytes(p) for p in pcm]
        ids = [hashlib.sha1(p).hexdigest()[:16] for p in pcm]
        own = self.takes_dir / "l0004.wav"
        own.write_bytes(wavs[4])
        takes = {"0": {"key": "k0", "file": other_node("l0000.wav"), "audio": ids[0], "retake_rounds": 2},
                 "1": {"key": "k1", "file": other_node("l0001.wav")},
                 "2": {"key": "k2", "file": other_node("l0002.wav"), "audio": ids[2]},
                 "3": {"key": "k3", "file": other_node("l0003.wav"), "audio": ids[3]},
                 "4": {"key": "k4", "file": str(own), "audio": ids[4]}}
        path = self.save_takes(takes)
        self.mirror(other_node("l0000.wav"), wavs[0])
        self.mirror(other_node("l0001.wav"), wavs[1])
        self.mirror(other_node("l0002.wav"), wavs[3])
        self.mirror(str(own), wavs[0])

        # Fetch: the two takes land under this node's output folder, and their entries name them and keep their
        # other fields.
        self.assertEqual(worker.fetch_takes(self.app, self.folder, "demo", [1], self.root, self.log), 2)
        new = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((new["engine"], new["runner"]), ("breeze", "cpp"))
        for i in (0, 1):
            here = self.takes_dir / f"l000{i}.wav"
            self.assertEqual(new["takes"][str(i)]["file"], str(here))
            self.assertEqual(here.read_bytes(), wavs[i])
        self.assertEqual(new["takes"]["0"], {**takes["0"], "file": str(self.takes_dir / "l0000.wav")})

        # Leave: the take whose mirror copy is other audio, the take the mirror lacks and this node's own take stay
        # as they were. The mirror's older copy does not replace the own take's file.
        for i in (2, 3, 4):
            self.assertEqual(new["takes"][str(i)], takes[str(i)])
        self.assertFalse((self.takes_dir / "l0002.wav").exists())
        self.assertEqual(own.read_bytes(), wavs[4])
        self.assertNotIn(exchange.mirror_rel(str(own)), self.app.fetched)
        self.assertIn("2 of the 4 takes this node lacked come from the app", self.log.lines[-1])

        # Again: the fetched takes are now this node's, so a second run asks for neither.
        self.app.fetched.clear()
        self.assertEqual(worker.fetch_takes(self.app, self.folder, "demo", [1], self.root, self.log), 0)
        self.assertEqual(self.app.fetched, [exchange.mirror_rel(other_node("l0002.wav"))])

        # Back on the first node: a take whose file under the output folder already holds the mirror's audio is
        # not fetched, and one that holds older audio is.
        self.save_takes({"0": takes["0"], "1": takes["1"]})
        (self.takes_dir / "l0001.wav").write_bytes(wavs[2])
        self.app.fetched.clear()
        self.assertEqual(worker.fetch_takes(self.app, self.folder, "demo", [1], self.root, self.log), 2)
        self.assertEqual(self.app.fetched, [exchange.mirror_rel(other_node("l0001.wav"))])
        self.assertEqual((self.takes_dir / "l0001.wav").read_bytes(), wavs[1])

        # Gone: this node's own take whose file is gone comes back from the mirror at its own path.
        self.save_takes({"4": takes["4"]})
        self.mirror(str(own), wavs[4])
        own.unlink()
        self.assertEqual(worker.fetch_takes(self.app, self.folder, "demo", [1], self.root, self.log), 1)
        self.assertEqual(own.read_bytes(), wavs[4])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["takes"]["4"], takes["4"])

        # An app that cannot be asked leaves the takes file alone and does not raise.
        self.m.jobs["j1"]["status"] = "failed"
        before = self.save_takes(takes).read_bytes()
        self.assertEqual(worker.fetch_takes(self.app, self.folder, "demo", [1], self.root, self.log), 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("could not fetch the takes of chapter 1", self.log.lines[-1])

    def test_a_fetch_that_fails_never_stops_the_job(self):
        from unittest import mock
        from serial_audio import worker
        pcm = b"\x01" * 400
        takes = {"0": {"key": "k0", "file": other_node("l0000.wav"), "audio": hashlib.sha1(pcm).hexdigest()[:16]}}
        path = self.save_takes(takes)
        self.mirror(other_node("l0000.wav"), wav_bytes(pcm))
        before = path.read_bytes()
        answers = []

        def cut_short(url, timeout=120):
            answers.append(url)
            raise worker.AppError(None, "the app is not reachable: IncompleteRead(10 bytes read, 990 more expected)")

        with mock.patch.object(worker, "MEDIA_RETRY", 0), mock.patch.object(worker, "MEDIA_TRIES", 3):
            # A download the app cuts short is tried again, then given up, and the take stays as it was.
            with mock.patch.object(self.app, "fetch", cut_short):
                self.assertEqual(worker.fetch_takes(self.app, self.folder, "demo", [1], self.root, self.log), 0)
            self.assertEqual(len(answers), 3)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((self.takes_dir / "l0000.wav").exists())

            # A download that succeeds on a later try brings the take.
            real, calls = self.app.fetch, []

            def flaky(url, timeout=120):
                calls.append(url)
                if len(calls) < 3:
                    raise worker.AppError(503, "the app answered 503")
                return real(url, timeout)

            with mock.patch.object(self.app, "fetch", flaky):
                self.assertEqual(worker.fetch_takes(self.app, self.folder, "demo", [1], self.root, self.log), 1)
            self.assertEqual((self.takes_dir / "l0000.wav").read_bytes(), wav_bytes(pcm))

        # A takes file of another shape, and an error nobody planned for, are logged and do not raise.
        for body in ('{"takes": ["junk"]}', '["junk"]', "not json"):
            path.write_text(body, encoding="utf-8")
            self.assertEqual(worker.fetch_takes(self.app, self.folder, "demo", [1], self.root, self.log), 0)
        self.save_takes(takes)
        (self.takes_dir / "l0000.wav").unlink()
        with mock.patch.object(worker, "app_copies", side_effect=RuntimeError("boom")):
            self.assertEqual(worker.fetch_takes(self.app, self.folder, "demo", [1, 2], self.root, self.log), 0)
        self.assertIn("RuntimeError: boom", self.log.lines[-1])

    def test_the_http_client_reports_a_cut_answer_as_the_app_being_unreachable(self):
        import http.client
        from unittest import mock
        from serial_audio import worker
        app = worker.App("http://127.0.0.1:9", "j1", "k", "node", 0)

        class Cut:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                raise http.client.IncompleteRead(b"x" * 10, 990)

        with mock.patch("urllib.request.urlopen", return_value=Cut()):
            for call in (lambda: app.fetch("/media/mirror/a.wav"), lambda: app.request("POST", "media/have", b"{}")):
                with self.assertRaises(worker.AppError) as e:
                    call()
                self.assertIsNone(e.exception.status)

    def test_a_worker_sends_the_app_the_audio_it_lacks(self):
        from serial_audio import worker
        files = [self.takes_dir / "l0000.wav", self.takes_dir / "l0001.wav", self.root / "hero_00001_.safetensors"]
        for i, f in enumerate(files):
            f.write_bytes(bytes([i]) * 100)
        paths = [str(f) for f in files]

        # Send: the takes and the Drama 3 voice file the mirror lacks all arrive.
        self.assertEqual(worker.send_media(self.app, paths, self.log), {"sent": 3, "bytes": 300, "kept": 0, "failed": 0})
        for f in files:
            self.assertEqual((self.m.media_dir / exchange.mirror_rel(str(f))).read_bytes(), f.read_bytes())
        self.assertIn("sent the app 3 audio file(s)", self.log.lines[-1])

        # Keep: a second run sends nothing, and says nothing.
        said = len(self.log.lines)
        self.assertEqual(worker.send_media(self.app, paths, self.log), {"sent": 0, "bytes": 0, "kept": 3, "failed": 0})
        self.assertEqual(len(self.log.lines), said)

        # Replace: a take with new contents of the same size is sent again.
        files[1].write_bytes(b"\x09" * 100)
        self.app.sent.clear()
        self.assertEqual(worker.send_media(self.app, paths, self.log)["sent"], 1)
        self.assertEqual(self.app.sent, [exchange.mirror_rel(paths[1])])

        # Refuse: a file the app refuses fails alone, and the files beside it still arrive.
        note = self.root / "note.txt"
        note.write_text("x", encoding="utf-8")
        more = [self.takes_dir / f"l001{i}.wav" for i in range(6)]
        for i, f in enumerate(more):
            f.write_bytes(bytes([20 + i]) * 50)
        got = worker.send_media(self.app, [str(note)] + [str(f) for f in more], self.log)
        self.assertEqual((got["sent"], got["failed"]), (6, 1))
        self.assertFalse((self.m.media_dir / exchange.mirror_rel(str(note))).exists())
        self.assertIn("1 did not arrive", self.log.lines[-1])

        # Outage: while the app is unreachable each request is tried again, and the audio arrives once it answers.
        from unittest import mock
        real, calls = self.app.request, []

        def down_twice(method, path, data=None, timeout=60):
            calls.append(path)
            if len(calls) <= 2:
                raise worker.AppError(None, "the app is not reachable: connection refused")
            return real(method, path, data, timeout)

        files[2].write_bytes(b"\x07" * 100)
        with mock.patch.object(worker, "MEDIA_RETRY", 0), mock.patch.object(self.app, "request", down_twice):
            self.assertEqual(worker.send_media(self.app, paths, self.log)["sent"], 1)
        self.assertEqual((self.m.media_dir / exchange.mirror_rel(paths[2])).read_bytes(), b"\x07" * 100)

        # An outage that lasts costs one round of tries, not one per file, and nothing raises.
        calls.clear()

        def down(method, path, data=None, timeout=60):
            calls.append(path)
            raise worker.AppError(None, "the app is not reachable: connection refused")

        for f in more:
            f.write_bytes(b"\x06" * 50)
        with mock.patch.object(worker, "MEDIA_RETRY", 0), mock.patch.object(worker, "MEDIA_TRIES", 3), \
                mock.patch.object(self.app, "request", down):
            self.assertEqual(worker.send_media(self.app, [str(f) for f in more], self.log)["failed"], 6)
        self.assertEqual(calls, ["media/have"] * 3)

        # A job that no longer runs gets nothing into the mirror, and the worker does not raise.
        self.m.jobs["j1"]["status"] = "failed"
        files[0].write_bytes(b"\x08" * 100)
        self.assertEqual(worker.send_media(self.app, paths, self.log)["failed"], 3)
        self.assertNotEqual((self.m.media_dir / exchange.mirror_rel(paths[0])).read_bytes(), files[0].read_bytes())

    def test_the_audio_a_job_sends_covers_every_take_of_a_chapter_it_changed(self):
        from serial_audio import worker
        import yaml

        # Setup: chapter 1 has two takes on this node and one on another, chapter 2 has one, and the hero's voice
        # has a candidate from this node with a Drama 3 voice file and an older one from another node.
        t1, t2, t3 = self.takes_dir / "l0000.wav", self.takes_dir / "l0001.wav", self.root / "ch2.wav"
        clip, voice = self.root / "hero_00001_.flac", self.root / "hero_00001_.safetensors"
        for f in (t1, t2, t3, clip, voice):
            f.write_bytes(b"x")
        self.save_takes({"0": {"file": str(t1)}, "1": {"file": str(t2)}, "2": {"file": other_node("l0002.wav")}})
        (self.folder / "chapters" / "0002").mkdir()
        (self.folder / "chapters" / "0002" / exchange.TAKES_FILE).write_text(
            json.dumps({"takes": {"0": {"file": str(t3)}}}), encoding="utf-8")
        (self.folder / "voices").mkdir()
        (self.folder / "voices" / "hero.yaml").write_text(yaml.safe_dump({
            "candidates": [{"seed": 1, "clip": str(clip), "voice_file": str(voice)}],
            "history": [{"candidates": [{"seed": 0, "clip": other_node("hero_s0.wav")}]}]}), encoding="utf-8")
        (self.folder / "voices" / "_briefs.yaml").write_text("hero: {}\n", encoding="utf-8")

        # Any changed file of a chapter brings every take of that chapter this node holds, flagged or not.
        self.assertEqual(worker.media_files(self.folder, {"chapters/0001/chapter.yaml": {}}), sorted([str(t1), str(t2)]))
        self.assertEqual(worker.media_files(self.folder, {"chapters/0002/take_check.json": {}}), [str(t3)])
        # A changed voice record brings its candidates on this node, with their Drama 3 voice files.
        self.assertEqual(worker.media_files(self.folder, {"voices/hero.yaml": {}}), sorted([str(clip), str(voice)]))
        # The briefs and the serial's own files bring nothing.
        self.assertEqual(worker.media_files(self.folder, {"voices/_briefs.yaml": {}, "serial.yaml": {}}), [])
        # A sync job covers the whole serial.
        self.assertEqual(worker.media_files(self.folder, None), sorted(str(f) for f in (t1, t2, t3, clip, voice)))

        # Changed pronunciations bring the options this node said, and a sync job brings them too.
        said = self.root / "Eole_1_0.wav"
        said.write_bytes(b"x")
        (self.folder / "bible").mkdir(exist_ok=True)
        (self.folder / "bible" / "pronunciations.yaml").write_text(yaml.safe_dump({
            "Aaa": "text", "Bbb": {"options": ["a list"]}, "Ccc": {"options": {"candidates": "text"}},
            "Eole": {"respelling": "ay-OHL", "options": {"candidates": [
                {"say": "Eole", "clip": str(said)}, {"say": "Ay-ole", "clip": other_node("Eole_1_1.wav")}, "junk"]}},
            "Louis": {"say": "Lewis"}}), encoding="utf-8")
        self.assertEqual(worker.media_files(self.folder, {"bible/pronunciations.yaml": {}}), [str(said)])
        self.assertEqual(worker.media_files(self.folder, {"bible/characters.yaml": {}}), [])
        self.assertEqual(worker.media_files(self.folder, None), sorted(str(f) for f in (t1, t2, t3, clip, voice, said)))
        said.unlink()
        # An empty file is no take, and a record with a field of another shape is read for the rest.
        t2.write_bytes(b"")
        (self.folder / "voices" / "hero.yaml").write_text(yaml.safe_dump({
            "candidates": [{"seed": 1, "clip": str(clip), "voice_file": [str(voice)]}, "junk"]}), encoding="utf-8")
        self.assertEqual(worker.media_files(self.folder, None), sorted(str(f) for f in (t1, t3, clip)))

    def test_the_app_takes_media_only_for_a_running_job_and_only_at_mirror_paths(self):
        from serial_audio.web.nomad import WorkerError
        self.mirror("D:\\out\\a.wav", b"a")

        # Have: the answer names the mirror's files by hash and leaves out what is not a mirror path.
        got = self.m.worker_media_have("j1", "k", ["D/out/a.wav", "D/out/b.wav", "../state/jobs.json", "C:/x.wav"])
        self.assertEqual(got, {"have": {"D/out/a.wav": hashlib.sha256(b"a").hexdigest()}})

        # Target: a mirror path with a kept file type maps into the mirror, and anything else is refused.
        self.assertEqual(self.m.worker_media_target("j1", "k", "D/out/v.safetensors"),
                         self.m.media_dir / "D/out/v.safetensors")
        for bad in ("../state/jobs.json", "D/out/../../x.wav", "D/out/run.py", "/etc/x.wav", "C:\\x.wav", ""):
            with self.assertRaises(WorkerError) as e:
                self.m.worker_media_target("j1", "k", bad)
            self.assertEqual(e.exception.status, 400, bad)

        # Job: a wrong key, an unknown job and a finished job are refused. A job the app stopped while its worker
        # still ran may send the takes it made.
        for jid, key, status in (("j1", "wrong", 403), ("j9", "k", 404)):
            with self.assertRaises(WorkerError) as e:
                self.m.worker_media_have(jid, key, [])
            self.assertEqual(e.exception.status, status)
        self.m.jobs["j1"]["status"] = "done"
        with self.assertRaises(WorkerError) as e:
            self.m.worker_media_target("j1", "k", "D/out/a.wav")
        self.assertEqual(e.exception.status, 410)
        self.m.jobs["j1"].update(status="stopped")
        self.m.jobs["j1"]["nomad"]["cancel"] = True
        self.assertEqual(self.m.worker_media_have("j1", "k", [])["have"], {})


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

    def test_a_job_that_must_use_the_claude_token_is_refused_without_it(self):
        from serial_audio import worker as w
        # A job that does not ask for a token runs with whatever sign-in the node has, and logs nothing.
        self.assertEqual(w.claude_sign_in({}), (True, ""))
        self.assertEqual(w.claude_sign_in({"CLAUDE_CODE_OAUTH_TOKEN": "t"}), (True, ""))
        # A job that asks for a token runs when it has one, and its log line does not hold the token.
        ok, line = w.claude_sign_in({"SERIAL_AUDIO_CLAUDE_AUTH": "token", "CLAUDE_CODE_OAUTH_TOKEN": "sk-secret"})
        self.assertTrue(ok)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", line)
        self.assertNotIn("sk-secret", line)
        # Without a token, or with a blank one, it is refused.
        for env in ({"SERIAL_AUDIO_CLAUDE_AUTH": "token"},
                    {"SERIAL_AUDIO_CLAUDE_AUTH": "token", "CLAUDE_CODE_OAUTH_TOKEN": " \n"}):
            ok, line = w.claude_sign_in(env)
            self.assertFalse(ok)
            self.assertIn("is not set", line)
        # A misspelled setting is refused, with or without a token, so it can't turn the check off.
        for value in ("Token", " token", "tokn"):
            ok, line = w.claude_sign_in({"SERIAL_AUDIO_CLAUDE_AUTH": value, "CLAUDE_CODE_OAUTH_TOKEN": "sk-secret"})
            self.assertFalse(ok)
            self.assertIn("only value it takes", line)
            self.assertNotIn("sk-secret", line)


class VoiceEffectTest(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        from serial_audio import library as lib
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lib = lib
        patch = mock.patch.object(lib, "LIBRARY", Path(self.tmp.name) / "library")
        patch.start()
        self.addCleanup(patch.stop)
        folder = lib.serial_dir("demo")
        (folder / "voices").mkdir(parents=True)
        (folder / "voices" / "keeper.yaml").write_text("id: keeper\n", encoding="utf-8")
        lib.save_serial("demo", {"title": "Demo", "voice_effects": {"keeper": {"effect": "whisper", "voice": 0.35}}})

    def test_set_and_remove_an_effect(self):
        from serial_audio.render import set_voice_effect

        # Removing the last effect drops voice_effects, and removing it again leaves the file as it was.
        self.assertIsNone(set_voice_effect("demo", "keeper", None))
        self.assertNotIn("voice_effects", self.lib.load_serial("demo"))
        self.assertIsNone(set_voice_effect("demo", "keeper", None))

        # Setting one writes the spec, with the level only when given.
        self.assertEqual(set_voice_effect("demo", "keeper", "whisper", 0.5), {"effect": "whisper", "voice": 0.5})
        self.assertEqual(set_voice_effect("demo", "keeper", "whisper"), {"effect": "whisper"})
        self.assertEqual(self.lib.load_serial("demo")["voice_effects"], {"keeper": {"effect": "whisper"}})

        # An unknown effect, a level out of range, a voice with no cast record and a malformed id are refused.
        for effect, level, voice in (("echo", None, "keeper"), ("whisper", 1.5, "keeper"), ("whisper", None, "kepper"),
                                     ("whisper", None, "../voices/keeper")):
            with self.assertRaises(ValueError):
                set_voice_effect("demo", voice, effect, level)
        self.assertEqual(self.lib.load_serial("demo")["voice_effects"], {"keeper": {"effect": "whisper"}})

    def test_the_job_builds_cli_args_and_waits_for_the_serial(self):
        from serial_audio.web.jobs import JobManager, build

        # The job passes --clear, or the effect and its level, to the CLI as a quick job.
        clear = build("voice-effect", {"slug": "demo", "voice": "keeper", "clear": True})
        self.assertEqual(clear["args"], ["voice-effect", "demo", "keeper", "--clear"])
        self.assertFalse(clear["heavy"])
        whisper = build("voice-effect", {"slug": "demo", "voice": "keeper", "effect": "whisper", "level": 0.4})
        self.assertEqual(whisper["args"], ["voice-effect", "demo", "keeper", "whisper", "--level", "0.4"])

        # A flag in place of an effect, a missing effect, a malformed id and a level out of range are refused.
        for bad in ({"voice": "keeper", "effect": "--clear"}, {"voice": "keeper"}, {"voice": "Bad Id", "clear": True},
                    {"voice": "keeper", "effect": "whisper", "level": 2}):
            with self.assertRaises(ValueError):
                build("voice-effect", {"slug": "demo", **bad})

        # On the cluster it starts beside a running render of its serial, which took its copy of serial.yaml in its
        # bundle, and waits for a cast. On one machine it waits for a running job on its serial and not for another
        # serial's.
        effect = {"kind": "voice-effect", "slug": "demo", "meta": {}, "heavy": False}
        self.assertFalse(conflicts(effect, job("render", chapters=[3])))
        self.assertTrue(conflicts(effect, job("cast")))
        m = JobManager(state_dir=Path(self.tmp.name) / "state", autostart=False)
        self.addCleanup(m._lock_file.close)
        render = {**job("render", chapters=[3]), "heavy": True, "title": "Render · ch 3"}
        self.assertIn("Render · ch 3", m._blocker(effect, [render], [], False, 0.0))
        self.assertIsNone(m._blocker(effect, [{**render, "slug": "other"}], [], False, 0.0))


class RenderSeedTest(unittest.TestCase):
    """A chapter that was rendered with seed 3, as chapter 13 of The Hundred Reigns was."""

    LINES = [{"speaker": "narrator", "text": "The harbour was quiet that morning."},
             {"speaker": "narrator", "text": "Nobody answered the door."}]

    def setUp(self):
        from unittest import mock
        from serial_audio import breeze
        from serial_audio import library as lib
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(lib, "LIBRARY", Path(self.tmp.name) / "library")
        patch.start()
        self.addCleanup(patch.stop)
        folder = lib.serial_dir("demo")
        (folder / "voices").mkdir(parents=True)
        lib.save_serial("demo", {"title": "Demo", "narrator_voice_id": "narrator"})
        (folder / "voices" / "narrator.flac").write_bytes(b"clip")
        lib.save_yaml(folder / "voices" / "narrator.yaml", {"clip": "narrator.flac", "sample_text": "t"})
        chapter = lib.chapter_dir("demo", 13)
        lib.save_chapter_meta("demo", 13, {"number": 13, "status": "rendered", "script": {"lines": 2},
                                           "render": {"engine": "breeze", "runner": "cpp", "seed": 3}})
        (chapter / "text.md").write_text("x", encoding="utf-8")
        lib.save_json(chapter / "script.json", {"lines": self.LINES})
        plan = breeze.plan_takes(self.LINES, {"narrator": 0}, {0: "narrator"}, {}, 3,
                                 {"narrator": hashlib.sha1(b"clip").hexdigest()}, "cpp")
        breeze.save_takes(chapter / breeze.TAKES_FILE, {str(p[0]): {"key": p[5], "file": "gone.wav"} for p in plan},
                          "cpp")

    def test_a_render_queued_without_a_seed_plans_to_reuse_the_chapters_takes(self):
        from serial_audio import breeze
        from serial_audio.web.jobs import build
        from serial_audio.web.nomad import NomadClient, NomadJobManager

        # The seed a render uses: the one the request names, else the last Breeze render's, else 1.
        self.assertEqual(breeze.render_seed({"engine": "breeze", "seed": 3}), 3)
        self.assertEqual(breeze.render_seed({"engine": "breeze", "seed": 3}, 0), 0)
        for last in (None, {}, {"seed": 3}, {"engine": "breeze"}, {"engine": "breeze", "seed": "3"},
                     {"engine": "breeze", "seed": True}):
            self.assertEqual(breeze.render_seed(last), 1, last)

        # A request with no seed gives a job with no --seed, and a request that names one passes it on.
        queued = build("render", {"slug": "demo", "chapter": 13}, comfy_url="http://c")
        self.assertEqual(queued["args"], ["render", "demo", "13", "--comfy-url", "http://c", "--engine", "breeze",
                                          "--runner", "cpp"])
        named = build("render", {"slug": "demo", "chapter": 13, "seed": 1}, comfy_url="http://c")
        self.assertEqual(named["args"][:5], ["render", "demo", "13", "--seed", "1"])

        # The plan of the job without a seed reuses both takes. With seed 1 it would make both again.
        self.assertEqual(breeze.reusable_takes("demo", 13), (2, 2))
        self.assertEqual(breeze.reusable_takes("demo", 13, 1), (0, 2))

        # The scheduler's estimate counts the same takes for each job.
        m = NomadJobManager(NomadClient("http://127.0.0.1:9", "token"), state_dir=Path(self.tmp.name) / "state",
                            autostart=False)
        self.addCleanup(m._lock_file.close)
        record = {"kind": "render", "slug": "demo", "meta": queued["meta"]}
        self.assertEqual(m._takes_needed({**record, "id": "a", "args": queued["args"]}, 2), 0)
        self.assertEqual(m._takes_needed({**record, "id": "b", "args": named["args"]}, 2), 2)


class BacklogTest(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        from serial_audio import library as lib
        from serial_audio.web import jobs
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(lib, "LIBRARY", Path(self.tmp.name) / "library"),
                  mock.patch.object(jobs, "external_activity", lambda *a, **k: []),
                  mock.patch.object(jobs.procs, "snapshot", lambda: {})):
            p.start()
            self.addCleanup(p.stop)
        lib.save_serial("demo", {"title": "Demo"})
        self.m = jobs.JobManager(state_dir=Path(self.tmp.name) / "state", autostart=False)
        self.addCleanup(self.m._lock_file.close)
        self.started: list[str] = []

        def launch(job):
            self.started.append(job["id"])
            job["status"] = "running"
        for p in (mock.patch.object(self.m, "_launch", launch),
                  mock.patch.object(self.m, "_poll_comfy", lambda: {"up": True}),
                  mock.patch.object(self.m, "_reconcile", lambda job, snap: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_a_request_marks_its_job_as_backlog_work(self):
        from serial_audio.web.jobs import backlog_wait

        # Only "backlog": true in the params makes a job backlog work, and a job queued again keeps it.
        marked, plain, odd = self.m.submit([{"kind": "check-takes", "params": {"slug": "demo", "chapter": n, **extra}}
                                            for n, extra in ((1, {"backlog": True}), (2, {}), (3, {"backlog": "yes"}))],
                                           stop_on_failure=False)
        self.assertEqual((marked["backlog"], plain["backlog"], odd["backlog"]), (True, False, False))
        self.assertTrue(self.m.retry(marked["id"])["backlog"])

        # It waits for any running job and for a queued job that is not backlog work, and for nothing else.
        def job(status, backlog=False):
            return {"status": status, "backlog": backlog, "title": "Render · ch 9"}
        mine = job("queued", True)
        self.assertIsNone(backlog_wait(mine, [mine, job("queued", True), job("done"), job("failed")]))
        self.assertEqual(backlog_wait(mine, [mine, job("queued")]), "backlog work, waiting for the queue to empty")
        self.assertEqual(backlog_wait(mine, [mine, job("running", True)]),
                         "backlog work, waiting for Render · ch 9 to finish")

    def test_one_machine_runs_backlog_work_after_everything_else(self):
        # Two backlog jobs are queued ahead of a job that is not backlog work.
        first, second, plain = self.m.submit(
            [{"kind": "check-takes", "params": {"slug": "demo", "chapter": n, **extra}}
             for n, extra in ((1, {"backlog": True}), (2, {"backlog": True}), (3, {}))], stop_on_failure=False)
        self.m.last_busy = -1000.0

        # The other job starts first, and both backlog jobs wait for it.
        self.m.tick()
        self.assertEqual(self.started, [plain["id"]])
        self.assertEqual(self.m.wait_reasons[first["id"]], f"backlog work, waiting for {plain['title']} to finish")

        # Then the backlog jobs run one at a time, in the order they were queued.
        plain["status"] = "done"
        self.m.tick()
        self.assertEqual(self.started, [plain["id"], first["id"]])
        self.assertEqual(self.m.wait_reasons[second["id"]], f"backlog work, waiting for {first['title']} to finish")
        first["status"] = "done"
        self.m.tick()
        self.assertEqual(self.started, [plain["id"], first["id"], second["id"]])

    @staticmethod
    def render(n, **extra):
        return {"kind": "render", "params": {"slug": "demo", "chapter": n, "backlog": True, **extra}}

    def test_a_backlog_render_is_not_queued_while_the_same_render_waits(self):
        # Chapters 1 and 2 wait as backlog work. A request for 2 and 3 queues 3 only, and names the job that
        # stands in for 2.
        one, two = self.m.submit([self.render(1), self.render(2)], stop_on_failure=False)
        covered: list[dict] = []
        [three] = self.m.submit([self.render(2), self.render(3)], stop_on_failure=False, covered=covered)
        self.assertEqual((three["title"], covered), ("Render · ch 3", [two]))
        self.assertEqual(self.m.submit([self.render(1), self.render(2), self.render(3)]), [])

        # A render that has started doesn't stand in for a new one.
        two["status"] = "running"
        [later] = self.m.submit([self.render(2)])
        self.assertEqual((later["status"], self.m.submit([self.render(2)])), ("queued", []))

        # A render that is not backlog work and a backlog render with another seed are not the same render.
        plain, seeded = self.m.submit([{"kind": "render", "params": {"slug": "demo", "chapter": 2}},
                                       self.render(2, seed=7)])
        self.assertEqual((plain["backlog"], seeded["backlog"], len(self.m.jobs)), (False, True, 6))

        # Another kind of backlog job is queued after the render of chapter 1. A new render of chapter 1 has to
        # read what that job writes, so it is queued, and it then stands in for the next one.
        self.m.submit([{"kind": "check-takes", "params": {"slug": "demo", "chapter": 1, "backlog": True}}])
        covered = []
        [again] = self.m.submit([self.render(1)], covered=covered)
        self.assertEqual((again["seq"] > one["seq"], covered), (True, []))
        self.assertEqual(self.m.submit([self.render(1)], covered=covered), [])
        self.assertEqual(covered, [again])

        # Queueing a job again gives the job that stands in for it.
        one["status"] = "failed"
        self.assertIs(self.m.retry(one["id"]), again)

        # A render in a batch doesn't stand in, because a failure ahead of it in the batch would skip it.
        self.m.submit([self.render(8), self.render(9)])
        self.assertEqual(len(self.m.submit([self.render(9)])), 1)

    def test_a_render_that_stands_in_for_a_later_request_counts_as_queued_then(self):
        import datetime
        from serial_audio import library as lib

        def ago(minutes):
            then = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes)
            return then.isoformat(timespec="seconds")

        def rendered(minutes):
            lib.save_yaml(lib.chapter_dir("demo", 1) / "chapter.yaml",
                          {"number": 1, "render": {"engine": "breeze", "runner": "cpp", "at": ago(minutes)}})

        # A backlog render was queued an hour ago, and a run the app did not start rendered the chapter half an
        # hour ago. The job no longer needs to run.
        lib.chapter_dir("demo", 1).mkdir(parents=True)
        rendered(120)
        [waiting] = self.m.submit([self.render(1)])
        waiting["created"] = ago(60)
        rendered(30)
        self.assertIn("rendered outside the app", self.m._already_done(waiting))

        # The same render is asked for now. The job stands in for the request, so it has to run.
        covered: list[dict] = []
        self.assertEqual((self.m.submit([self.render(1)], covered=covered), covered), ([], [waiting]))
        self.assertGreater(waiting["asked"], waiting["created"])
        self.assertIsNone(self.m._already_done(waiting))

    def test_a_backlog_render_that_the_same_render_did_already_is_skipped(self):
        from unittest import mock
        nothing = {"spoken": 9, "make": 0, "retake": 0, "unheard": 0, "join": False, "engine": "breeze"}

        def at(clock):
            return f"2026-11-12T{clock}-05:00"

        def ran(kind, began, ended, **more):
            return {"id": f"{kind}-{began}", "kind": kind, "status": "done", "args": [kind], "started": at(began),
                    "finished": at(ended), "created": at(began), "seq": 0, **more}

        def stale(changes=nothing):
            with mock.patch("serial_audio.breeze.render_changes", return_value=changes):
                return self.m._already_done(second)

        # Chapter 1's render was queued twice, and a name was changed before the second was queued. The first
        # started after both and is done.
        [first] = self.m.submit([self.render(1)])
        second = {**first, "id": "second", "seq": first["seq"] + 1, "created": at("20:09:17")}
        self.m.jobs["second"] = second
        self.m.jobs["name"] = ran("say-as", "20:09:03", "20:09:04")
        first.update(status="done", created=at("20:05:20"), started=at("20:09:55"), finished=at("20:20:00"))

        # The second has nothing left to do, and it is skipped when its turn comes.
        self.assertIn("Render · ch 1 started at 2026-11-12T20:09:55-05:00", stale())
        self.m.last_busy = -1000.0
        with mock.patch("serial_audio.breeze.render_changes", return_value=nothing):
            self.m.tick()
        self.assertEqual((second["status"], self.started), ("skipped", []))
        self.assertIn("left it nothing to do", second["note"])
        second.update(status="queued", finished=None, note=None)

        # It runs when a render would still make, take again, hear or join something, when the chapter's last
        # render was not Breeze, and when the chapter cannot be planned.
        for change in ({"make": 1}, {"retake": 1}, {"unheard": 1}, {"join": True}, {"engine": "drama3"}):
            self.assertIsNone(stale({**nothing, **change}))
        with mock.patch("serial_audio.breeze.render_changes", side_effect=FileNotFoundError("no script")):
            self.assertIsNone(self.m._already_done(second))

        # It runs when a job that changes what a render reads has run since the first started, or is between two
        # stages. Options for a name, and a job that was cancelled before it started, don't count.
        for kind, more, skipped in (("say-as", {}, False), ("check-takes", {}, False), ("say-options", {}, True),
                                    ("cast", {"status": "queued", "finished": None}, False),
                                    ("check-takes", {"status": "cancelled", "started": None}, True)):
            self.m.jobs["other"] = ran(kind, "20:15:00", "20:15:30", **more)
            self.assertEqual(bool(stale()), skipped, (kind, more))
        del self.m.jobs["other"]

        # It runs when the first started before it was queued, when the first failed, when the first had another
        # seed, and when it is not backlog work.
        for target, change in ((first, {"started": at("20:09:10")}), (first, {"status": "failed"}),
                               (first, {"args": first["args"] + ["--seed", "7"]}), (second, {"backlog": False})):
            before = dict(target)
            target.update(change)
            self.assertIsNone(stale(), change)
            target.clear()
            target.update(before)
        self.assertTrue(stale())


class SayOptionsJobTest(unittest.TestCase):
    def test_the_job_builds_cli_args_and_runs_as_a_claude_stage_and_a_breeze_stage(self):
        from unittest import mock
        from serial_audio import library as lib
        from serial_audio.web.jobs import HEAVY, build

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(lib, "LIBRARY", Path(tmp) / "library"):
            lib.save_serial("demo", {"title": "Demo"})

            # The term goes last, after "--", so a term that starts with a dash is still a term.
            one = build("say-options", {"slug": "demo", "term": " Eole ", "model": "sonnet"}, comfy_url="http://c:1")
            self.assertEqual(one["args"], ["say-options", "demo", "--comfy-url", "http://c:1", "--backend", "auto",
                                           "--model", "sonnet", "--on-limit", "api", "--", "Eole"])
            self.assertEqual((one["heavy"], one["gpu"], one["comfy"], one["title"], one["meta"]),
                             (True, True, False, "Options for Eole", {"term": "Eole"}))
            self.assertIn("say-options", HEAVY)
            build_again = build("say-options", {"slug": "demo", "term": "Eole", "render_only": True})
            self.assertEqual(build_again["title"], "Say the options for Eole again")
            build_hear = build("say-options", {"slug": "demo", "term": "Eole", "say": " -Ay ohl "})
            self.assertEqual((build_hear["title"], build_hear["gpu"]), ("Hear Eole as -Ay ohl", True))
            with self.assertRaises(ValueError):
                build("say-options", {"slug": "demo", "term": "Eole", "say": "Ay-ohl", "render_only": True})
            # A spelling without a letter is refused here, so it doesn't become a pass that proposes options.
            for bad in ("", "   ", "123"):
                with self.assertRaises(ValueError):
                    build("say-options", {"slug": "demo", "term": "Eole", "say": bad})

            # A missing term and an unknown serial are refused.
            for bad in ({"slug": "demo"}, {"slug": "demo", "term": "  "}, {"slug": "nope", "term": "Eole"}):
                with self.assertRaises(ValueError):
                    build("say-options", bad)

        # On the cluster the respellings come from a Claude node and the takes from a breeze node, and each stage's
        # flag stands before the "--".
        stages = job_stages(job("say-options", args=one["args"]))
        self.assertEqual([(s["class"], s["label"]) for s in stages], [("llm", "respellings"), ("breeze", "takes")])
        self.assertEqual(stages[0]["args"][-3:], ["--dry-run", "--", "Eole"])
        self.assertEqual(stages[1]["args"][-3:], ["--render-only", "--", "Eole"])
        self.assertEqual(stages[0]["args"][:-3], one["args"][:-2])

        # A stored set of options is said again in one breeze stage.
        again = build_again["args"]
        self.assertEqual(again[-3:], ["--render-only", "--", "Eole"])
        self.assertEqual(job_stages(job("say-options", args=again)), [{"class": "breeze", "args": again}])

        # A spelling to hear is added and said in one breeze stage too, and it can't come with render_only.
        hear = build_hear["args"]
        self.assertEqual(hear[-3:], ["--add=-Ay ohl", "--", "Eole"])
        self.assertEqual(job_stages(job("say-options", args=hear)), [{"class": "breeze", "args": hear}])

        # It writes only the options of a term, which no chapter job reads, so it and a chapter job of its serial
        # don't wait for each other. It waits for the other jobs that write the serial's settings.
        self.assertFalse(conflicts(job("say-options"), job("render", chapters=[7])))
        self.assertFalse(conflicts(job("say-options"), job("check-takes", chapters=[7])))
        self.assertFalse(conflicts(job("render", chapters=[7]), job("say-options")))
        self.assertFalse(conflicts(job("screenplay", chapters=[7]), job("say-options")))
        self.assertTrue(conflicts(job("render", chapters=[7]), job("say-as")))
        self.assertTrue(conflicts(job("say-options"), job("say-as")))
        self.assertTrue(conflicts(job("say-as"), job("say-options")))
        self.assertTrue(conflicts(job("say-options"), job("character-pass", chapters=[1])))
        self.assertTrue(conflicts(job("say-options"), job("say-options")))
        self.assertFalse(conflicts(job("say-options"), job("say-as", slug="other")))


class BiosJobTest(unittest.TestCase):
    def test_the_job_builds_cli_args_and_runs_as_one_claude_stage(self):
        from unittest import mock
        from serial_audio import library as lib
        from serial_audio.web.jobs import build

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(lib, "LIBRARY", Path(tmp) / "library"):
            lib.save_serial("demo", {"title": "Demo"})

            # Default: every voice without a bio, as a heavy job off the GPU, with the LLM settings.
            every = build("bios", {"slug": "demo", "model": "sonnet"})
            self.assertEqual(every["args"], ["bios", "demo", "--backend", "auto", "--model", "sonnet", "--on-limit", "api"])
            self.assertEqual((every["heavy"], every["gpu"], every["comfy"]), (True, False, False))
            self.assertEqual(every["title"], "Casting bios · every voice without one")

            # Named characters with redo. The title names up to three ids and counts more.
            one = build("bios", {"slug": "demo", "characters": ["eole"], "redo": True})
            self.assertEqual(one["args"][-3:], ["--characters", "eole", "--redo"])
            self.assertEqual((one["title"], one["meta"]), ("Casting bios · eole", {"characters": ["eole"]}))
            four = build("bios", {"slug": "demo", "characters": ["a", "b", "c", "d"]})
            self.assertEqual((four["args"][-2:], four["title"]), (["--characters", "a,b,c,d"], "Casting bios · 4 characters"))

            # A malformed id and an unknown serial are refused.
            for bad in ({"slug": "demo", "characters": ["../x"]}, {"slug": "demo", "characters": "eole"},
                        {"slug": "nope"}):
                with self.assertRaises(ValueError):
                    build("bios", bad)

        # On the cluster it is one dispatch to a Claude node with the args as queued.
        self.assertEqual(job_stages(job("bios", args=every["args"])), [{"class": "llm", "args": every["args"]}])

        # It writes only the bible, so it does not wait for a chapter job of its serial. A render waits for it, and
        # it waits for a cast, a character pass and another bios job, which write the bible too.
        self.assertFalse(conflicts(job("bios"), job("render", chapters=[7])))
        self.assertFalse(conflicts(job("bios"), job("screenplay", chapters=[7])))
        self.assertTrue(conflicts(job("render", chapters=[7]), job("bios")))
        self.assertTrue(conflicts(job("bios"), job("cast")))
        self.assertTrue(conflicts(job("cast"), job("bios")))
        self.assertTrue(conflicts(job("bios"), job("character-pass", chapters=[1])))
        self.assertTrue(conflicts(job("bios"), job("bios")))
        self.assertFalse(conflicts(job("bios"), job("cast", slug="other")))

        # An import writes chapter files, the index and serial.yaml, and never the bible, so bios and an import
        # don't wait for each other. A cast still waits for an import.
        self.assertFalse(conflicts(job("bios"), job("fetch")))
        self.assertFalse(conflicts(job("fetch"), job("bios")))
        self.assertTrue(conflicts(job("cast"), job("fetch")))


if __name__ == "__main__":
    unittest.main()
