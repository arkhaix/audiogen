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

    def test_a_file_replaced_while_the_bundle_is_built_ships_with_its_own_hash(self):
        # A result lands between the listing of the serial's files and the read of one of them. The manifest names
        # the bytes that were shipped, so the worker doesn't report the file as changed, and its own change to
        # another file is still written back.
        from unittest import mock
        rel = "chapters/0001/chapter.yaml"
        target = self.root / "library" / "demo" / rel
        listed = exchange.manifest

        def then_replaced(folder):
            files = listed(folder)
            target.write_bytes(b"status: scripted\n")
            return files

        with mock.patch.object(exchange, "manifest", then_replaced):
            data = exchange.build_bundle("demo", {"job": "j2", "stage": 0, "steps": []}, self.root)
        z = zipfile.ZipFile(io.BytesIO(data))
        info = json.loads(z.read(exchange.BUNDLE_FILE).decode("utf-8"))
        self.assertEqual(z.read(f"library/demo/{rel}"), b"status: scripted\n")
        self.assertEqual(info["manifest"][rel]["sha256"], hashlib.sha256(b"status: scripted\n").hexdigest())
        folder = Path(self.tmp.name) / "work2" / "library" / "demo"
        z.extractall(folder.parent.parent)
        self.assertEqual(exchange.changes(folder, info["manifest"]), {})
        (folder / "serial.yaml").write_bytes(b"title: Demo, again\n")
        changed = exchange.changes(folder, info["manifest"])
        result, archive = exchange.read_result(exchange.build_result(folder, {"job": "j2", "exit_code": 0}, changed))
        report = exchange.apply_result("demo", result, archive, success=True, conflicts_dir=self.conflicts,
                                       root=self.root)
        self.assertEqual((report["applied"], report["conflicts"]), (["serial.yaml"], []))

    def test_a_file_gone_while_the_bundle_is_built_is_left_out(self):
        # A result's temporary file is listed and then renamed before it is read. The bundle is built without it.
        from unittest import mock
        rel = "chapters/0001/chapter.yaml.incoming"
        temporary = self.root / "library" / "demo" / rel
        temporary.write_bytes(b"status: scripted\n")
        listed = exchange.manifest

        def then_renamed(folder):
            files = listed(folder)
            temporary.unlink()
            return files

        with mock.patch.object(exchange, "manifest", then_renamed):
            data = exchange.build_bundle("demo", {"job": "j3", "stage": 0, "steps": []}, self.root)
        z = zipfile.ZipFile(io.BytesIO(data))
        info = json.loads(z.read(exchange.BUNDLE_FILE).decode("utf-8"))
        self.assertNotIn(rel, info["manifest"])
        self.assertNotIn(f"library/demo/{rel}", z.namelist())
        self.assertIn("library/demo/chapters/0001/chapter.yaml", z.namelist())

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
        self.assertTrue(conflicts(job("render", chapters=[7]), job("character-pass", chapters=[7])))
        self.assertFalse(conflicts(job("render", chapters=[7]), job("character-pass", chapters=[1])))
        self.assertFalse(conflicts(job("render", chapters=[7]), job("cast", slug="other")))
        self.assertFalse(conflicts(job("add-epub", slug=None), job("add-epub", slug=None)))
        # A render waits for a cast, a pick, a say-as or a voice effect on its serial, which change what it reads.
        # None of them waits for a render, a screenplay or a take check, and they still wait for each other and for
        # a character pass. A screenplay reads nothing that a cast, casting bios, a pick or a voice effect writes,
        # so of these it waits only for a say-as.
        self.assertTrue(conflicts(job("render", chapters=[7]), job("cast")))
        self.assertTrue(conflicts(job("check-takes", chapters=[7]), job("cast")))
        for kind in ("cast", "bios", "pick-voice", "voice-effect"):
            self.assertFalse(conflicts(job("screenplay", chapters=[7]), job(kind)), kind)
            self.assertTrue(conflicts(job("render", chapters=[7]), job(kind)), kind)
        self.assertTrue(conflicts(job("screenplay", chapters=[7]), job("say-as")))
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

    def test_an_options_job_does_not_wait_for_a_chapter_job_to_fetch_its_bundle(self):
        # A screenplay that was just dispatched has no bundle yet. A cast waits for it, because the bundle takes
        # the voices a cast writes. An options job writes only the options of a term, which no chapter job reads,
        # so it doesn't wait, and a render behind it in the queue can't take the node it was first for.
        self.m.workers.append(worker("dev", ["llm"], "linux"))
        self.m.nomad_state["up"] = True
        fresh = {"kind": "screenplay", "slug": "demo", "meta": {"chapters": [12]}, "args": ["screenplay", "demo", "12"],
                 "title": "Screenplay · ch 12",
                 "nomad": {"target": "dev", "stage": 0, "stages": [{"class": "llm", "args": []}], "contact": None}}
        cast = {"kind": "cast", "slug": "demo", "meta": {"characters": ["eole"]}, "args": ["cast", "demo"],
                "title": "Cast · eole"}
        options = {"kind": "say-options", "slug": "demo", "meta": {"term": "Eole"},
                   "args": ["say-options", "demo", "--", "Eole"], "title": "Options for Eole"}
        self.assertEqual(self.m._blocker_for(cast, "llm", [fresh]),
                         "waiting for Screenplay · ch 12 to fetch its bundle")
        self.assertIsNone(self.m._blocker_for(options, "llm", [fresh]))

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
        # A character pass and a chapter job wait for each other only on a chapter that both work on. Character
        # passes wait for each other and for a cast.
        self.assertFalse(conflicts(job("character-pass", chapters=[39]), job("render", chapters=[38])))
        self.assertFalse(conflicts(job("character-pass", chapters=[39]), job("screenplay", chapters=[38])))
        self.assertFalse(conflicts(job("character-pass", chapters=[39]), job("check-takes", chapters=[38])))
        self.assertTrue(conflicts(job("character-pass", chapters=[38]), job("render", chapters=[38])))
        self.assertTrue(conflicts(job("character-pass", chapters=[37, 38]), job("screenplay", chapters=[38])))
        self.assertFalse(conflicts(job("render", chapters=[38]), job("character-pass", chapters=[39])))
        self.assertFalse(conflicts(job("screenplay", chapters=[40]), job("character-pass", chapters=[39])))
        self.assertFalse(conflicts(job("check-takes", chapters=[38]), job("character-pass", chapters=[39])))
        self.assertTrue(conflicts(job("render", chapters=[38]), job("character-pass", chapters=[38])))
        self.assertTrue(conflicts(job("screenplay", chapters=[38]), job("character-pass", chapters=[37, 38])))
        self.assertTrue(conflicts(job("check-takes", chapters=[38]), job("character-pass", chapters=[38])))
        self.assertTrue(conflicts(job("character-pass", chapters=[40]), job("character-pass", chapters=[39])))
        self.assertTrue(conflicts(job("character-pass", chapters=[39]), job("cast")))
        # A job whose record names no chapter, because its chapter was not on disk when it was queued, may work on
        # any chapter, so it and a character pass wait for each other.
        self.assertTrue(conflicts(job("screenplay"), job("character-pass", chapters=[39])))
        self.assertTrue(conflicts(job("screenplay", chapters=[39]), job("character-pass")))
        self.assertTrue(conflicts(job("character-pass"), job("render", chapters=[38])))
        self.assertTrue(conflicts(job("character-pass", chapters=[39]), job("screenplay")))

        # Scheduling: a batch queued behind another batch's running render. Its first character pass starts beside
        # the render. The next pass and chapter 39's screenplay wait for that pass, and the render waits for the
        # cast, which gives its speakers their voices.
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
        self.assertEqual(self.m.wait_reasons["s39"], "waiting for Character pass · ch 39, which works on the same chapter")
        self.assertEqual(self.m.wait_reasons["r39"],
                         "waiting for Cast · everyone who needs a voice, queued ahead of it on the same serial")
        self.assertEqual({j["id"]: j["status"] for j in batch},
                         {"c39": "running", "c40": "queued", "cast": "queued", "s39": "queued", "r39": "queued"})
        # Once chapter 39's pass is done, its screenplay starts beside chapter 40's pass and passes the cast, which
        # waits for that pass. The render still waits for the cast.
        batch[0]["status"] = "done"
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["c39", "c40", "s39"])
        self.assertEqual(self.m.wait_reasons["cast"], "waiting for Character pass · ch 40, which changes the whole serial")
        self.assertEqual(self.m.wait_reasons["r39"],
                         "waiting for Cast · everyone who needs a voice, queued ahead of it on the same serial")

        # Bundles and its own chapter: a character pass waits until a running render has fetched its bundle, and a
        # character pass on the render's chapter waits for the render.
        self.m.jobs = {}
        unfetched = {**render, "nomad": {**render["nomad"], "contact": None}}
        self.assertEqual(self.m._blocker_for(queued("c41", 9, "character-pass", 41), "llm", [unfetched]),
                         "waiting for Render · ch 38 to fetch its bundle")
        self.assertEqual(self.m._blocker_for(queued("c38", 9, "character-pass", 38), "llm", [render]),
                         "waiting for Render · ch 38, which works on the same chapter")

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
        ann = {"name": "Ann", "importance": "major", "speaks": True}
        lib.save_bible("demo", {"characters": {"ann": ann}, "pronunciations": {}})
        lib.chapter_dir("demo", 1).mkdir(parents=True)
        (base / "incoming" / "demo").mkdir(parents=True)
        for n in (7, 8):
            (base / "incoming" / "demo" / f"ch{n}.html").write_text("<html>", encoding="utf-8")
        self.m.workers.append(worker("dev", ["llm"], "linux"))
        self.m.nomad_state["up"] = True

        def requests(*numbers):
            def each(kind, params):
                return [{"kind": kind, "params": {"slug": "demo", **params(n)}} for n in numbers]
            # as the app queues them: a cast after each character pass, the first one for everyone
            chain = []
            for i, n in enumerate(numbers):
                chain += [{"kind": "character-pass", "params": {"slug": "demo", "chapters": str(n)}},
                          {"kind": "cast", "params": {"slug": "demo", **({"from_chapter": n} if i else {})}}]
            return (each("fetch", lambda n: {"chapter": n, "html": f"ch{n}.html"}) + chain
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
        # The character pass follows the import. The cast and the chapter's screenplay follow the character pass
        # together, because a screenplay doesn't wait for a cast.
        imp["status"] = "done"
        self.assertEqual(schedule(), [chars["id"]])
        chars["status"] = "done"
        self.assertEqual(schedule(), [cast["id"], script["id"]])
        self.assertEqual(self.m.wait_reasons[render["id"]],
                         "waiting for Cast · everyone who needs a voice, which changes the whole serial")
        # The render waits for the cast and for its chapter's screenplay.
        cast["status"] = "done"
        self.assertEqual(schedule(), [])
        self.assertEqual(self.m.wait_reasons[render["id"]],
                         "waiting for Screenplay · ch 7, which works on the same chapter")

        # A cast that has nobody to cast when its turn comes is skipped, and the render then waits only for its
        # chapter's screenplay.
        lib.save_bible("demo", {"characters": {"ann": {**ann, "voice_id": "ann"}}, "pronunciations": {}})
        self.m.jobs = {}
        imp, chars, cast, script, render = self.m.submit(requests(7))
        imp["status"] = chars["status"] = "done"
        self.assertEqual(schedule(), [script["id"]])
        self.assertEqual((cast["status"], cast["note"]), ("skipped", "nobody needs a voice, so there is nothing to cast"))
        self.assertEqual(self.m.wait_reasons[render["id"]],
                         "waiting for Screenplay · ch 7, which works on the same chapter")

        # A failed import skips its chapter's character pass and the jobs that wait for that one: its chapter's
        # cast, screenplay and render. The other import goes on, and so does every job of the earlier chapter,
        # which waits for none of them.
        self.m.jobs = {}
        batch = self.m.submit(requests(7, 8))
        self.m._finish(batch[1], "failed", "exit 1", 1)
        self.assertEqual([j["title"] for j in batch[:6]], ["Import saved page · ch 7", "Import saved page · ch 8",
                                                           "Character pass · ch 7", "Cast · everyone who needs a voice",
                                                           "Character pass · ch 8", "Cast · ch 8"])
        self.assertEqual([j["status"] for j in batch], ["queued", "failed", "queued", "queued", "skipped", "skipped",
                                                        "queued", "skipped", "queued", "skipped"])

    def test_a_batch_casts_after_each_character_pass(self):
        from unittest import mock
        from serial_audio import library as lib
        from serial_audio.web import jobs

        patch = mock.patch.object(lib, "LIBRARY", Path(self.tmp.name) / "library")
        patch.start()
        self.addCleanup(patch.stop)
        lib.save_serial("demo", {"title": "Demo"})
        ann = {"name": "Ann", "importance": "major", "speaks": True}
        lib.save_bible("demo", {"characters": {"ann": ann}, "pronunciations": {}})

        # The cast that follows a chapter's character pass names the chapter. A render or a take check of earlier
        # chapters only doesn't wait for it. Every other job waits for it as for any cast.
        built = jobs.build("cast", {"slug": "demo", "from_chapter": 55})
        self.assertEqual((built["title"], built["meta"]["from_chapter"]), ("Cast · ch 55", 55))
        self.assertNotIn("from_chapter", jobs.build("cast", {"slug": "demo"})["meta"])
        with self.assertRaises(ValueError):
            jobs.build("cast", {"slug": "demo", "from_chapter": 55, "characters": ["ann"]})
        late = {**job("cast"), "meta": {"characters": [], "from_chapter": 55}}
        self.assertFalse(conflicts(job("render", chapters=[54]), late))
        self.assertFalse(conflicts(job("check-takes", chapters=[54]), late))
        self.assertTrue(conflicts(job("render", chapters=[55]), late))
        self.assertTrue(conflicts(job("render", chapters=[60]), late))
        self.assertTrue(conflicts(job("render", chapters=[54, 55]), late))
        self.assertTrue(conflicts(job("render"), late))
        self.assertTrue(conflicts(job("render", chapters=[54]), job("cast")))
        self.assertTrue(conflicts(job("character-pass", chapters=[54]), late))
        self.assertTrue(conflicts(late, job("character-pass", chapters=[54])))
        self.assertTrue(conflicts(job("pick-voice"), late))

        # A cast is skipped only when it is for everyone and nobody needs a voice.
        everyone = {"kind": "cast", "slug": "demo", "args": ["cast", "demo"], "meta": {"characters": []}}
        self.assertIsNone(self.m._already_done(everyone))
        self.assertIsNone(self.m._already_done({**everyone, "slug": "missing"}))
        lib.save_bible("demo", {"characters": {"ann": {**ann, "voice_id": "ann"}}, "pronunciations": {}})
        self.assertEqual(self.m._already_done(everyone), "nobody needs a voice, so there is nothing to cast")
        self.assertIsNone(self.m._already_done({**everyone, "meta": {"characters": ["ann"]}}))
        self.assertIsNone(self.m._already_done({**everyone, "meta": {"characters": [], "dry_run": True}}))
        lib.save_bible("demo", {"characters": {"ann": ann}, "pronunciations": {}})

        # Scheduling: the batch of chapters 54 and 55. Chapter 54's pass starts alone.
        self.m.workers.append(worker("dev", ["llm"], "linux"))
        self.m.nomad_state["up"] = True

        def queued(jid, seq, kind, n=None, first=None):
            titles = {"character-pass": "Character pass", "screenplay": "Screenplay", "render": "Render", "cast": "Cast"}
            meta = {"characters": [], **({"from_chapter": first} if first else {})} if kind == "cast" else {"chapters": [n]}
            who = f"ch {n or first}" if n or first else "everyone who needs a voice"
            return {"id": jid, "seq": seq, "status": "queued", "kind": kind, "slug": "demo", "heavy": True,
                    "batch": "b1", "created": "2026-11-13T22:30:00", "meta": meta, "title": f"{titles[kind]} · {who}",
                    "args": [kind, "demo"] + ([str(n)] if n else [])}

        def batch():
            jobs_ = [queued("c54", 1, "character-pass", 54), queued("k54", 2, "cast"),
                     queued("c55", 3, "character-pass", 55), queued("k55", 4, "cast", first=55),
                     queued("s54", 5, "screenplay", 54), queued("s55", 6, "screenplay", 55),
                     queued("r54", 7, "render", 54), queued("r55", 8, "render", 55)]
            self.m.jobs = {j["id"]: j for j in jobs_}
            return self.m.jobs

        b = batch()
        dispatch = []
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["c54"])
        self.assertEqual(self.m.wait_reasons["k54"], "waiting for Character pass · ch 54, which changes the whole serial")
        self.assertEqual(self.m.wait_reasons["r54"],
                         "waiting for Cast · everyone who needs a voice, queued ahead of it on the same serial")
        # After the pass, the first cast and chapter 54's screenplay start together, and chapter 55's pass waits
        # for the cast.
        b["c54"]["status"] = "done"
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["c54", "k54", "s54"])
        self.assertEqual(self.m.wait_reasons["c55"],
                         "waiting for Cast · everyone who needs a voice, which changes the whole serial")
        # After the cast, chapter 55's pass starts once the running screenplay has fetched its bundle. Chapter
        # 54's render waits only for its screenplay.
        b["k54"]["status"] = "done"
        self.m._schedule(0.0, dispatch)
        self.assertEqual(self.m.wait_reasons["c55"], "waiting for Screenplay · ch 54 to fetch its bundle")
        b["s54"]["nomad"]["contact"] = "t"
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["c54", "k54", "s54", "c55"])
        self.assertEqual(self.m.wait_reasons["r54"], "waiting for Screenplay · ch 54, which works on the same chapter")
        # Chapter 55's cast starts when its pass ends. Once chapter 54's screenplay ends, chapter 54 renders beside
        # that cast, and chapter 55's render waits for it.
        b["c55"]["status"] = "done"
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["c54", "k54", "s54", "c55", "k55", "s55"])
        b["s54"]["status"] = "done"
        b["k55"]["nomad"]["contact"] = b["s55"]["nomad"]["contact"] = "t"
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["c54", "k54", "s54", "c55", "k55", "s55", "r54"])
        self.assertEqual(b["k55"]["status"], "running")
        self.assertEqual(self.m.wait_reasons["r55"], "waiting for Cast · ch 55, which changes the whole serial")

        # A failed cast skips the casts after it and the renders from its chapter on. The next chapter's pass
        # goes on, because it reads nothing a cast files, and so do the screenplays and the render of the chapter
        # before.
        b = batch()
        b["c56"] = queued("c56", 4.5, "character-pass", 56)
        b["k56"] = queued("k56", 4.7, "cast", first=56)
        b["s56"] = queued("s56", 6.5, "screenplay", 56)
        b["r56"] = queued("r56", 9, "render", 56)
        for jid in ("c54", "k54", "c55"):
            b[jid]["status"] = "done"
        self.m._finish(b["k55"], "failed", "exit 1 on laptop", 1)
        self.assertEqual({jid: j["status"] for jid, j in b.items() if j["status"] != "done"},
                         {"k55": "failed", "c56": "queued", "k56": "skipped", "s54": "queued", "s55": "queued",
                          "s56": "queued", "r54": "queued", "r55": "skipped", "r56": "skipped"})
        # A failed pass still skips the cast that follows it.
        b = batch()
        self.m._finish(b["c54"], "failed", "exit 1 on dev", 1)
        self.assertEqual((b["k54"]["status"], b["c55"]["status"], b["s55"]["status"]), ("skipped",) * 3)

        # A cast for everyone is costed at three characters, and one that follows a chapter's pass at one.
        from serial_audio.web.nomad import _cast_units
        self.assertEqual(_cast_units({"candidates": 3, "characters": []}), 9)
        self.assertEqual(_cast_units({"candidates": 3, "characters": [], "from_chapter": 55}), 3)
        self.assertEqual(_cast_units({"candidates": 4, "characters": ["ann", "bob"], "from_chapter": None}), 8)

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
        # A failed job that changes the whole serial skips every queued job of the batch that waits for it. A fetch
        # from the site skips them all. A cast skips the renders and not the screenplays, which don't wait for it.
        jobs = batch()
        jobs["fetch"] = queued("fetch", 0.5, "fetch")
        self.m._finish(jobs["fetch"], "failed", "exit 1", 1)
        self.assertEqual(statuses(), {"s20": "skipped", "s21": "skipped", "r18": "skipped", "r20": "skipped",
                                      "r21": "skipped", "other": "queued", "fetch": "failed"})
        jobs = batch()
        jobs["cast"] = queued("cast", 0.5, "cast")
        self.m._finish(jobs["cast"], "failed", "exit 1 on laptop", 1)
        self.assertEqual(statuses(), {"s20": "queued", "s21": "queued", "r18": "skipped", "r20": "skipped",
                                      "r21": "skipped", "other": "queued", "cast": "failed"})
        # A job skipped with the failed one skips the later jobs that wait for it, and not the earlier ones. The
        # character pass is on the failed render's chapter, because one on another chapter doesn't wait for a render.
        # The passes of chapters 19 and 22 wait for it as any two passes do. Chapter 21's render doesn't, because it
        # waits for a character pass only on its own chapter.
        jobs = batch()
        jobs["s20"]["status"] = "done"
        jobs["c19"] = queued("c19", 2.5, "character-pass", 19)
        jobs["c20"] = queued("c20", 4.5, "character-pass", 20)
        jobs["c22"] = queued("c22", 4.7, "character-pass", 22)
        self.m._finish(jobs["r20"], "failed", "exit 1 on laptop", 1)
        self.assertEqual(statuses(), {"s20": "done", "s21": "queued", "r18": "queued", "r20": "failed",
                                      "r21": "queued", "other": "queued", "c19": "queued", "c20": "skipped",
                                      "c22": "skipped"})
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

    def test_backlog_work_starts_only_when_no_other_work_runs_or_waits(self):
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

        # With the render done, one backlog job starts on the laptop. The other doesn't wait for it as backlog work:
        # it is placed by the usual rules, and a render of 100 takes ends sooner behind it on the laptop than on
        # the free Mac.
        self.m.jobs["r5"]["status"] = "done"
        self.assertEqual(schedule(), ["b5"])
        self.assertEqual((self.m.jobs["b5"]["nomad"]["target"], list(self.m.wait_reasons)), ("laptop", ["b6"]))
        self.assertRegex(self.m.wait_reasons["b6"],
                         r"^waiting for laptop, about \d+ min, instead of about \d+ min on mac now$")

        # A backlog Breeze render that makes two takes, with its saved takes in the app's mirror, ends sooner on the
        # free Mac, so two backlog renders run at once.
        self.m.jobs["b6"]["meta"]["engine"] = "breeze"
        self.m.placement = {"chapters": {"demo": {"6": "laptop"}}}
        self.m._takes_cache.clear()
        with mock.patch("serial_audio.web.nomad.reusable_takes", return_value=(98, 100)), \
                mock.patch.object(self.m, "_mirrored", return_value=True):
            dispatch: list[str] = []
            self.m._schedule(0.0, dispatch)
        self.assertEqual((dispatch, self.m.jobs["b6"]["status"], self.m.jobs["b6"]["nomad"]["target"]),
                         (["b6"], "running", "mac"))
        self.assertEqual((self.m.jobs["b5"]["status"], self.m.wait_reasons), ("running", {}))

        # With backlog renders on both nodes, a render that is not backlog work has no free node. It waits for the
        # node where it ends soonest, and no more backlog work starts while it waits.
        self.m.jobs["r9"] = queued("r9", 9, 9)
        self.m.jobs["b10"] = queued("b10", 10, 10, True)
        self.assertEqual(schedule(), [])
        self.assertRegex(self.m.wait_reasons["r9"], r"^waiting for (laptop|mac), about \d+ min \((laptop|mac) busy too\)$")
        self.assertEqual(self.m.wait_reasons["b10"], "backlog work, waiting for the queue to empty")
        del self.m.jobs["r9"], self.m.jobs["b10"]
        self.m.jobs["b6"] = queued("b6", 2, 6, True)
        self.m.placement = {}

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
            self.assertEqual(self.m._rate("box"), 25.0)
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


class EstimateTest(unittest.TestCase):
    """How long jobs take: what a node's own jobs teach, the cost of a render, and the forecast of the queue."""

    def setUp(self):
        from unittest import mock
        from serial_audio import library as lib
        from serial_audio.web.nomad import NomadClient, NomadJobManager
        self.tmp = tempfile.TemporaryDirectory()
        patch = mock.patch.object(lib, "LIBRARY", Path(self.tmp.name) / "library")
        patch.start()
        self.addCleanup(patch.stop)
        self.m = NomadJobManager(NomadClient("http://127.0.0.1:9", "token"), state_dir=Path(self.tmp.name) / "state",
                                 autostart=False)
        self.m.workers = [worker("laptop", ["breeze", "comfyui"], "windows", "nvidia"),
                          worker("mac", ["breeze"], "darwin", "metal"), worker("dev", ["llm"], "linux")]
        self.m.nomad_state["up"] = True

    def tearDown(self):
        self.m._lock_file.close()
        self.tmp.cleanup()

    def queued(self, jid, seq, kind, n, **more):
        title = {"render": "Render", "screenplay": "Screenplay", "check-takes": "Check takes"}.get(kind, kind)
        job = {"id": jid, "seq": seq, "status": "queued", "kind": kind, "slug": "demo", "heavy": True, "backlog": False,
               "batch": None, "created": "2026-11-13T19:00:00", "started": None, "finished": None,
               "title": f"{title} · ch {n}", "args": [kind, "demo", str(n)], "meta": {"chapters": [n]}, **more}
        self.m.jobs[jid] = job
        return job

    def render(self, jid, seq, n, takes=None, lines=200, unheard=0, **more):
        """A queued Breeze render of chapter n.

        takes is what the app last counted it would make, or None when nobody counted. unheard is the saved takes
        it would only hear.
        """
        job = self.queued(jid, seq, "render", n, **more)
        job["meta"].update({"lines": lines, "engine": "breeze", "runner": "cpp"})
        if takes is not None:
            self.m._work[jid] = (0.0, takes, unheard)
        return job

    def order(self):
        from serial_audio.web.jobs import start_order
        return [j["id"] for j in sorted((j for j in self.m.jobs.values() if j["status"] == "queued"), key=start_order)]

    def test_a_node_s_timings_scale_the_default(self):
        from serial_audio.web.nomad import TIMING_MIN, expected
        self.assertEqual(TIMING_MIN, 3)

        # Two timings are too few, so the default line stands: 20 s and 2 s a unit.
        self.assertEqual(expected([[10, 80], [20, 120]], 30, 20.0, 2.0), 80.0)
        # Three scale it by their median ratio to it. This node is twice as slow as the default, and the one job
        # that ran fast doesn't count.
        self.assertEqual(expected([[10, 80], [20, 120], [30, 40]], 30, 20.0, 2.0), 160.0)
        # Neither does one job that ran long among five that took one and a half times the default.
        many = [[10, 60], [20, 90], [30, 120], [40, 150], [50, 180], [60, 2000]]
        self.assertAlmostEqual(expected(many, 100, 20.0, 2.0), 330.0)

        # Work without a size keeps its usual time, and what is not a timing is passed over.
        mixed = [[0, 18], [0, 21], [0, 18], "x", [1], [0, -5], None, [True, 5]]
        self.assertEqual(expected(mixed, 0, 20.0, 0.0), 18.0)
        # A timing of a size that the default costs at nothing teaches nothing.
        self.assertEqual(expected([[0, 5], [0, 6], [0, 7]], 10, 0.0, 4.0), 40.0)
        # Whatever a hand edit of placement.json leaves there, the default stands and nothing is raised.
        for odd in (None, 5, 2.5, "x", {"a": 1}, [[1, float("inf")]] * 3, [[float("nan"), 1]] * 3):
            self.assertEqual(expected(odd, 10, 20.0, 2.0), 40.0)

    def test_a_breeze_render_is_costed_part_by_part(self):
        from serial_audio.web import nomad
        from serial_audio.web.jobs import QUICK_SETUP_SECONDS, RENDER_COST, WORDS_PER_LINE

        def default(part, units=0.0):
            return RENDER_COST[part][0] + RENDER_COST[part][1] * units

        job = self.render("r1", 1, 1, lines=200)
        words, rate = 200 * WORDS_PER_LINE, self.m._rate("laptop")
        around = nomad.PRE_STEP + default("setup") + default("join", words)

        # A touch-up makes under half of its chapter's lines, and its takes run longer than a whole chapter's.
        # The setup runs to the end of the first take, so the takes after it are costed.
        self.assertAlmostEqual(self.m._duration(job, "laptop", "laptop", 20),
                               around + 19 * rate * nomad.TOUCH_RATIO + default("check", 20) + default("finish", 20))
        # A whole chapter is costed at the node's take rate, with the check of a whole chapter.
        check = nomad.WHOLE_CHECK[0] + nomad.WHOLE_CHECK[1] * 200
        self.assertAlmostEqual(self.m._duration(job, "laptop", "laptop", 200),
                               around + 199 * rate + check + default("finish", 200))
        # A render that makes no take joins and sends back, with no breeze-server to start.
        nothing = nomad.PRE_STEP + QUICK_SETUP_SECONDS + default("join", words) + default("finish")
        self.assertAlmostEqual(self.m._duration(job, "laptop", "laptop", 0), nothing)
        # On another node than the one that holds the takes, it also fetches them from the app.
        self.assertAlmostEqual(self.m._duration(job, "mac", "laptop", 0), nothing + nomad.MOVE_PER_TAKE * 200)
        # A render that makes nothing and hears 150 saved takes again, as after a new version of the check, is
        # no quick join: breeze-server starts and the check hears them, at the cost of a whole chapter's check.
        self.m._work["r1"] = (0.0, 0, 150)
        heard = nomad.WHOLE_CHECK[0] + nomad.WHOLE_CHECK[1] * 150
        self.assertAlmostEqual(self.m._duration(job, "laptop", "laptop", 0), around + heard + default("finish"))
        # One that makes 20 takes and hears 30 more checks all 50.
        self.m._work["r1"] = (0.0, 20, 30)
        self.assertAlmostEqual(self.m._duration(job, "laptop", "laptop", 20),
                               around + 19 * rate * nomad.TOUCH_RATIO + default("check", 50) + default("finish", 20))
        del self.m._work["r1"]
        # A render that makes exactly half of its lines is a whole chapter, for its takes as for its timings.
        self.assertAlmostEqual(self.m._duration(job, "laptop", "laptop", 100),
                               around + 99 * rate + nomad.WHOLE_CHECK[0] + nomad.WHOLE_CHECK[1] * 100
                               + default("finish", 100))

        # The laptop's own timings scale each default: 30 s to set up, 10 s a touch-up take, and a check, a
        # join and a send-back that each take twice the default. The Mac has none and keeps the defaults.
        before = self.m._duration(job, "mac", "mac", 20)
        self.m.placement["timing"] = {"laptop": {
            "setup": [[0, 30]] * 3, "touch": [[10, 100]] * 3, "check": [[10, 2 * default("check", 10)]] * 3,
            "join": [[words, 96]] * 3, "finish": [[10, 2 * default("finish", 10)]] * 3}}
        self.assertAlmostEqual(self.m._duration(job, "laptop", "laptop", 20),
                               nomad.PRE_STEP + 30 + 190 + 2 * default("check", 20) + 96 + 2 * default("finish", 20))
        self.assertEqual(self.m._duration(job, "mac", "mac", 20), before)
        # Timings that a hand edit of placement.json broke count as none, and the next result starts them again.
        for broken in ({"laptop": {"setup": 5}}, {"laptop": []}, 7):
            self.m.placement["timing"] = broken
            self.assertAlmostEqual(self.m._duration(job, "laptop", "laptop", 20),
                                   around + 19 * rate * nomad.TOUCH_RATIO + default("check", 20) + default("finish", 20))
        self.m.placement.pop("timing")

        # The chapter list's figure for a chapter without audio is a whole chapter on the fastest breeze node.
        probe = {"kind": "render", "meta": {"engine": "breeze", "lines": nomad.DEFAULT_LINES}}
        self.assertAlmostEqual(self.m.minutes_per_line() * 60 * nomad.DEFAULT_LINES,
                               self.m._render_seconds(probe, "laptop", nomad.DEFAULT_LINES))

    def test_the_app_times_each_part_of_a_job_as_its_log_arrives(self):
        from unittest import mock
        from serial_audio.web import nomad
        clock = [0.0]

        def start(job, node, cls):
            job.update({"status": "running", "started": "2026-11-13T19:00:00"})
            job.pop("nomad", None)
            self.m._mark_dispatch(job, node, cls)
            job["nomad"]["node"] = node
            return [0]

        def say(job, sent, at, text):
            clock[0] = at
            sent[0] = self.m.worker_log(job["id"], job["nomad"]["key"], "0", sent[0], text.encode())["offset"]

        def run(job, make, first=1000.0):
            sent = start(job, "laptop", "breeze")
            say(job, sent, first + 6, "[worker] Render on laptop, stage 0\n$ python -m serial_audio.cli render demo 1\n")
            say(job, sent, first + 9, "render: demo chapter 1: 100 lines, 1 voices {0: 'narrator'} with Breeze TTS 2 via cpp\n")
            say(job, sent, first + 26, "   breeze-server started at http://127.0.0.1:8137\n   generating takes with Breeze "
                f"TTS 2 via breeze-server (this is the slow part): {make} of 100 lines, {100 - make} reused\n")
            say(job, sent, first + 31, "   first take done in 5 s\n")
            # the worker's log arrives in pieces, and this line is cut in two
            say(job, sent, first + 66, f"   checking the {make} takes with Parakeet bef")
            say(job, sent, first + 68, "ore the join...\n")
            say(job, sent, first + 90, "   joining takes...\n")
            say(job, sent, first + 140, "   joined 100 takes into 600.0 s at 24000 Hz\n   checking 0 takes with Parakeet, "
                "100 already checked...\n   take check: no takes flagged in 100\n   audio: x.opus\n")
            self.m._record_timing(job, "laptop", first + 146)

        with mock.patch.object(nomad.time, "time", lambda: clock[0]):
            # A touch-up of 10 takes: each part's first line is timed when it arrives, also the line that came in
            # two pieces.
            job = self.render("r1", 1, 1, lines=100)
            run(job, 10)
            self.assertEqual(job["nomad"]["marks"], {"step": 1006.0, "takes": 1026.0, "first": 1031.0, "check": 1068.0,
                                                     "join": 1090.0, "joined": 1140.0, "audio": 1140.0})
            # Its result adds one timing to each part of the node: seconds against takes, takes heard or words.
            # The setup runs to the end of the first take, and the takes are the nine after it.
            self.assertEqual(self.m.placement["timing"], {"laptop": {
                "setup": [[0, 25.0]], "touch": [[9, 37.0]], "check": [[10, 22.0]], "join": [[1600, 50.0]],
                "finish": [[10, 6.0]]}})

            # A whole chapter's check is kept apart, and its takes are left to the node's take rate.
            run(self.render("r2", 2, 1, lines=100), 80, first=2000.0)
            kept = self.m.placement["timing"]["laptop"]
            self.assertEqual((kept["check-whole"], kept["touch"]), ([[80, 22.0]], [[9, 37.0]]))
            self.assertEqual(len(kept["setup"]), 2)

            # A stage of another kind adds one timing, from its step to its result, by the words of its chapters.
            script = self.queued("s1", 3, "screenplay", 5)
            sent = start(script, "dev", None)
            say(script, sent, 3010.0, "$ python -m serial_audio.cli screenplay demo 5\n")
            self.m._record_timing(script, "dev", 3130.0)
            self.assertEqual(self.m.placement["timing"]["dev"], {"screenplay/llm": [[3200, 120.0]]})

            # A stage whose step the app did not see start teaches nothing.
            late = self.queued("s2", 4, "screenplay", 6)
            start(late, "dev", None)
            self.m._record_timing(late, "dev", 4000.0)
            self.assertEqual(len(self.m.placement["timing"]["dev"]["screenplay/llm"]), 1)

            # Only the last TIMING_KEEP timings of a kind stay.
            for i in range(nomad.TIMING_KEEP + 3):
                self.m._record_timing(script, "dev", 3130.0 + i)
            got = self.m.placement["timing"]["dev"]["screenplay/llm"]
            self.assertEqual((len(got), got[-1]), (nomad.TIMING_KEEP, [3200, 120.0 + nomad.TIMING_KEEP + 2]))
            # A step whose result came with its first log line took no time. Its mark is rounded to a tenth of
            # a second, which can put it just after the result.
            self.m._record_timing(script, "dev", 3009.96)
            self.assertEqual(self.m.placement["timing"]["dev"]["screenplay/llm"][-1], [3200, 0.0])
            # Timings that a hand edit of placement.json broke are started again.
            for broken in ({"dev": {"screenplay/llm": 5}}, {"dev": []}, 7):
                self.m.placement["timing"] = broken
                self.m._record_timing(script, "dev", 3130.0)
                self.assertEqual(self.m.placement["timing"]["dev"]["screenplay/llm"], [[3200, 120.0]])

    def test_the_forecast_places_each_queued_job_as_the_scheduler_would(self):
        from unittest import mock
        seconds = {"laptop": 200.0, "mac": 300.0, "dev": 60.0}

        def forecast():
            with mock.patch.object(self.m, "_stage_seconds", lambda job, st, node: seconds[node]), \
                    mock.patch.object(self.m, "_remaining", lambda job, count=True: 100.0):
                got = self.m._forecast(0.0)
            return {jid: (o.get("on"), o.get("start_in"), o.get("end_in")) for jid, o in got.items()}

        # The laptop has 100 s left of chapter 9. A queued render takes 200 s there and 300 s on the Mac.
        stage = {"class": "breeze", "args": []}
        running = self.render("r0", 0, 9, takes=10, nomad={"target": "laptop", "node": "laptop", "stage": 0,
                                                           "stages": [stage]})
        running["status"] = "running"
        for i in (1, 2, 3):
            self.render(f"r{i}", i, i, takes=10)
        self.render("b1", 4, 11, takes=10, backlog=True)
        self.render("b2", 5, 12, takes=10, backlog=True)
        got = forecast()
        # Each render goes where it ends first: the first waits for the laptop, the second takes the idle Mac,
        # and the third waits for the laptop again.
        self.assertEqual((got["r0"], got["r1"], got["r2"], got["r3"]),
                         (("laptop", None, 100.0), ("laptop", 100.0, 300.0), ("mac", 0.0, 300.0),
                          ("laptop", 300.0, 500.0)))
        # Backlog work starts when the last other job has ended, and two backlog renders run on two nodes.
        self.assertEqual((got["b1"], got["b2"]), (("laptop", 500.0, 700.0), ("mac", 500.0, 800.0)))

        # A running cast has 100 s left of its briefs on the Claude node. Its voices follow on a GPU node, so it
        # ends after both, and a render of its serial waits for that.
        briefs = {"class": "llm", "args": [], "label": "casting briefs"}
        cast = self.queued("c0", 0, "cast", 0, meta={"characters": ["eole"]},
                           nomad={"node": "dev", "stage": 0, "stages": [briefs, dict(stage, label="voices")]})
        cast["status"] = "running"
        self.m.jobs = {"c0": cast}
        self.render("r1", 1, 1, takes=10)
        got = forecast()
        self.assertEqual((got["c0"], got["r1"]), (("dev", None, 300.0), ("laptop", 300.0, 500.0)))

        # A render waits for the screenplay of its chapter that is queued ahead of it, and no other render does.
        # A cast is its briefs on the Claude node and then its voices on a GPU node, and the renders queued after
        # it wait for it.
        self.m.jobs = {"r0": running}
        self.queued("s5", 1, "screenplay", 5)
        self.render("r5", 2, 5, takes=10)
        self.render("r6", 3, 6, takes=10)
        cast = self.queued("c", 4, "cast", 0, meta={"characters": ["eole"]})
        cast["args"] = ["cast", "demo", "--engine", "breeze"]
        self.render("r7", 5, 7, takes=10)
        got = forecast()
        self.assertEqual((got["s5"], got["r5"], got["r6"]),
                         ((None, 0.0, 60.0), ("laptop", 100.0, 300.0), ("mac", 0.0, 300.0)))
        self.assertEqual((got["c"], got["r7"]), (("laptop", 0.0, 500.0), ("laptop", 500.0, 700.0)))

        # A render whose takes nobody has counted has no times yet, and neither has backlog work, which waits
        # for it. The next render is placed as if it were not there.
        self.m.jobs = {"r0": running}
        self.render("r1", 1, 1)
        del self.m._work["r1"]
        self.render("r2", 2, 2, takes=10)
        self.render("b1", 3, 11, takes=10, backlog=True)
        got = forecast()
        self.assertEqual((got["r1"], got["r2"], got["b1"]),
                         ((None, None, None), ("laptop", 100.0, 300.0), (None, None, None)))
        # With the queue on hold no job has a time, and each keeps its length.
        self.m.paused = True
        with mock.patch.object(self.m, "_stage_seconds", lambda job, st, node: seconds[node]), \
                mock.patch.object(self.m, "_remaining", lambda job, count=True: 100.0):
            held = self.m._forecast(0.0)
        self.assertEqual((held["r2"].get("start_in"), held["r2"].get("end_in"), held["r2"]["estimate"]),
                         (None, None, 200.0))

        # The forecast counts no take itself. A running render that nobody has counted and whose log gives no
        # time yet has no end, and neither has the render that would wait for its node.
        self.m.paused = False
        del self.m._work["r0"]
        running.update({"started": "2026-11-13T19:00:00"})
        running["nomad"].update({"contact": "t", "marks": {}})
        self.m.jobs = {"r0": running}
        self.m.workers = [w for w in self.m.workers if w["name"] != "mac"]
        self.render("r2", 2, 2, takes=10)
        with mock.patch("serial_audio.web.nomad.reusable_takes") as reuse, \
                mock.patch("serial_audio.web.nomad.render_changes") as changes:
            got = self.m._forecast(0.0)
        self.assertEqual((reuse.call_count, changes.call_count), (0, 0))
        self.assertEqual((got["r0"].get("end_in"), got["r2"].get("end_in")), (None, None))

    def test_the_snapshot_gives_each_waiting_job_its_place_in_the_forecast(self):
        import time
        self.render("r1", 1, 1, takes=10)
        staged = self.render("r2", 2, 2, takes=10)
        staged["nomad"] = {"key": "secret", "stage": 0, "stages": [{"class": "breeze", "args": []}], "marks": {"step": 1.0}}
        self.m._outlook = {"r1": {"estimate": 200.0, "start_in": 100.0, "end_in": 300.0, "on": "laptop"},
                           "r2": {"estimate": 200.0, "start_in": 300.0, "end_in": 500.0, "on": "laptop"}}

        # The forecast is 40 s old, so its times are 40 s nearer. The length and the node stand.
        self.m._outlook_at = time.time() - 40
        snap = self.m.snapshot()
        r1, r2 = (next(j for j in snap["jobs"] if j["id"] == jid) for jid in ("r1", "r2"))
        self.assertEqual((r1["estimate"], r1["on"]), (200.0, "laptop"))
        self.assertAlmostEqual(r1["start_in"], 60.0, delta=2)
        self.assertAlmostEqual(r1["end_in"], 260.0, delta=2)
        # The queue ends when its last job does.
        self.assertAlmostEqual(snap["queue_end_in"], 460.0, delta=2)
        # A job's key and the times of its log lines stay in the app.
        self.assertEqual(sorted(r2["nomad"]), ["stage", "stages"])

        # A job the forecast could not place has a length and no times, and then the queue has no end either.
        self.m._outlook["r2"] = {"estimate": 200.0, "start_in": None, "end_in": None, "on": None}
        snap = self.m.snapshot()
        r2 = next(j for j in snap["jobs"] if j["id"] == "r2")
        self.assertEqual((r2["estimate"], "start_in" in r2, "end_in" in r2, snap["queue_end_in"]),
                         (200.0, False, False, None))
        # A job the forecast has not seen yet shows no figure of any kind.
        self.render("r3", 3, 3)
        r3 = next(j for j in self.m.snapshot()["jobs"] if j["id"] == "r3")
        self.assertFalse({"estimate", "start_in", "end_in"} & set(r3))

    def test_a_running_job_shows_the_time_to_the_end_of_its_last_stage(self):
        import datetime
        from unittest import mock
        from serial_audio.web import nomad

        T0 = 1_800_000_000.0

        def iso(t):
            return datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")

        # A cast writes its briefs on the Claude node, 20 s into its step here, and designs its voices after that.
        cast = self.queued("c", 1, "cast", 0, meta={"characters": ["eole"], "candidates": 3})
        cast.update({"status": "running", "started": iso(T0 - 10)})
        cast["nomad"] = {"key": "k", "stage": 0, "node": "dev", "contact": "t", "marks": {"step": T0},
                         "stages": [{"class": "llm", "args": [], "label": "casting briefs"},
                                    {"class": "breeze", "args": [], "label": "voices"}]}
        briefs = nomad.LLM_STAGE
        voices = 60 + 3 * nomad.CAST_TAKES * self.m._rate("laptop")
        p = self.m.progress(cast, T0 + 20)
        # The time left is the rest of the briefs and the voices on the fastest node, and the bar is the share
        # of the whole job since it started. The way to the node is behind a step that has begun.
        self.assertAlmostEqual(p["eta"], briefs - 20 + voices)
        self.assertAlmostEqual(p["fraction"], 30 / (30 + briefs - 20 + voices))
        # The scheduler asks how long the job keeps its node, which is the rest of the briefs alone.
        with mock.patch.object(nomad.time, "time", lambda: T0 + 20):
            self.assertAlmostEqual(self.m._remaining(cast), briefs - 20)
        # A stage that outlasts its estimate keeps a share of it, so the time left stays above the stages to come.
        p = self.m.progress(cast, T0 + 4000)
        self.assertAlmostEqual(p["eta"], nomad.OVERRUN_SHARE * briefs + voices)

        # While the worker waits for the machine there is no time left to show.
        self.m.log_path(cast).write_text("[worker] waiting: ComfyUI is running a prompt\n", encoding="utf-8")
        p = self.m.progress(cast, T0 + 20)
        self.assertEqual((p["phase"], p["eta"], p["fraction"]), ("waiting", None, None))

        # A Breeze render shows no time left until its takes are counted, and then the whole job.
        render = self.render("r1", 2, 1)
        render.update({"status": "running", "started": iso(T0)})
        render["nomad"] = {"key": "k", "stage": 0, "node": "laptop", "contact": "t", "marks": {},
                           "stages": [{"class": "breeze", "args": []}]}
        self.assertIsNone(self.m.progress(render, T0 + 1)["eta"])
        self.m._work["r1"] = (0.0, 10, 0)
        self.assertAlmostEqual(self.m.progress(render, T0 + 1)["eta"],
                               self.m._render_seconds(render, "laptop", 10) - nomad.PRE_STEP)
        # The saved takes it only hears are in its time left from the start, as in its estimate.
        self.m._work["r1"] = (0.0, 10, 40)
        self.assertAlmostEqual(self.m.progress(render, T0 + 1)["eta"],
                               self.m._render_seconds(render, "laptop", 10, 40) - nomad.PRE_STEP)
        # A render that makes nothing and hears nothing is a join, before the join and during it.
        self.m._work["r1"] = (0.0, 0, 0)
        before = self.m.progress(render, T0 + 1)["eta"]
        self.assertAlmostEqual(before, self.m._render_seconds(render, "laptop", 0) - nomad.PRE_STEP)
        self.m.log_path(render).write_text(
            "$ python -m serial_audio.cli render demo 1\nrender: demo chapter 1: 200 lines, 1 voices {0: 'narrator'} "
            "with Breeze TTS 2 via cpp\n   joining takes...\n", encoding="utf-8")
        render["nomad"]["marks"] = {"step": T0, "join": T0 + 1}
        self.assertLessEqual(self.m.progress(render, T0 + 2)["eta"], before)

        # The newest count of a render's takes is the one that stands: a count that was marked for another loses
        # to the scheduler's own, and a later recount wins over it.
        self.m._work["r1"] = (0.0, 7, 0)
        self.m._takes_cache["r1"] = (T0, 9)
        self.assertEqual(self.m._known_takes(render), 9)
        self.m._work["r1"] = (T0 + 5, 11, 0)
        self.assertEqual(self.m._known_takes(render), 11)

    def test_the_takes_of_queued_renders_are_counted_a_few_at_a_time(self):
        import time
        from unittest import mock
        from serial_audio.web import nomad
        for i in range(1, 6):
            self.render(f"r{i}", i, i)
        counts = {"spoken": 200, "make": 8, "retake": 2, "unheard": 5, "join": False, "engine": "breeze"}
        with mock.patch("serial_audio.web.nomad.render_changes", return_value=counts) as count:
            # One pass counts WORK_PER_TICK renders, the first of the queue first, from the stored verdicts.
            self.m._poll_work(time.time())
            self.assertEqual((count.call_count, sorted(self.m._work)), (nomad.WORK_PER_TICK, ["r1", "r2"]))
            count.assert_called_with("demo", 2, None, "cpp", stored=True)
            self.m._poll_work(time.time())
            self.m._poll_work(time.time())
            # A render's takes are the ones it makes and the flagged ones it takes again. The saved takes it only
            # hears are counted beside them.
            self.assertEqual({jid: got[1:] for jid, got in self.m._work.items()},
                             {f"r{i}": (10, 5) for i in range(1, 6)})
            self.assertEqual((self.m._known_takes(self.m.jobs["r3"]), self.m._known_unheard(self.m.jobs["r3"])),
                             (10, 5))
            # A counted render is left alone.
            count.reset_mock()
            self.m._poll_work(time.time())
            count.assert_not_called()

            # A job that changes what the serial's renders read marks them for another count, and each keeps its
            # count until then.
            count.return_value = {**counts, "make": 48}
            self.m._stale_work("demo")
            self.assertEqual(self.m._known_takes(self.m.jobs["r5"]), 10)
            self.m._poll_work(time.time())
            self.assertEqual((count.call_count, self.m._known_takes(self.m.jobs["r1"]),
                              self.m._known_takes(self.m.jobs["r5"])), (nomad.WORK_PER_TICK, 50, 10))

            # A count that is marked while it is read stays marked, so the next pass reads it again.
            count.reset_mock()
            count.side_effect = lambda *a, **k: self.m._stale_work("demo") or counts
            self.m._poll_work(time.time())
            self.assertEqual([jid for jid, got in sorted(self.m._work.items()) if got[0] == 0.0],
                             ["r1", "r2", "r3", "r4", "r5"])
            count.side_effect = None

            # The count of a job that left the queue is forgotten.
            self.m.jobs["r1"]["status"] = "done"
            self.m._poll_work(time.time())
            self.assertNotIn("r1", self.m._work)
        # A chapter that can't be planned yet counts as all its lines.
        self.render("r9", 9, 9, lines=120)
        self.m._work = {}
        with mock.patch("serial_audio.web.nomad.render_changes", side_effect=FileNotFoundError("no script")):
            for _ in range(4):
                self.m._poll_work(time.time())
        self.assertEqual((self.m._known_takes(self.m.jobs["r9"]), self.m._known_unheard(self.m.jobs["r9"])), (120, 0))

    def test_queued_jobs_move_to_the_top_the_bottom_or_ahead_of_another(self):
        for i, jid in enumerate("abcd", 1):
            self.render(jid, i, i)
        for i, jid in enumerate("xyz", 5):
            self.render(jid, i, i, backlog=True)
        start = {jid: j["seq"] for jid, j in self.m.jobs.items()}

        def moved(ids, to, before=None):
            for jid, seq in start.items():
                self.m.jobs[jid]["seq"] = seq
            self.m.reorder(list(ids), to, before)
            return "".join(self.order())

        # One job or several go to the top or the bottom, and several keep the order they had.
        self.assertEqual((moved("c", "top"), moved("b", "bottom")), ("cabdxyz", "acdbxyz"))
        self.assertEqual((moved("db", "top"), moved("ca", "bottom")), ("bdacxyz", "bdacxyz"))
        # Up and down move by one place and stop at the ends.
        self.assertEqual((moved("c", "up"), moved("a", "up"), moved("b", "down"), moved("d", "down")),
                         ("acbdxyz", "abcdxyz", "acbdxyz", "abcdxyz"))
        self.assertEqual((moved("ab", "down"), moved("cd", "up")), ("cabdxyz", "acdbxyz"))
        # A job goes ahead of the job it is dropped on.
        self.assertEqual((moved("d", "before", "b"), moved("a", "before", "d"), moved("ac", "before", "c")),
                         ("adbcxyz", "bcadxyz", "bacdxyz"))

        # Backlog work stays behind every other job, so each job moves among its own kind of work.
        self.assertEqual((moved("z", "top"), moved("az", "bottom")), ("abcdzxy", "bcdaxyz"))
        self.assertEqual((moved("y", "before", "a"), moved("b", "before", "y")), ("abcdyxz", "acdbxyz"))
        # The queue keeps the numbers it had, so a job queued later still goes behind every one of them.
        self.assertEqual(sorted(j["seq"] for j in self.m.jobs.values()), sorted(start.values()))

        # A job that is not queued can't be moved, and neither can a job be moved an unknown way.
        self.assertRaises(KeyError, self.m.reorder, ["nope"], "top")
        self.assertRaises(KeyError, self.m.reorder, ["a"], "before", "nope")
        self.assertRaises(ValueError, self.m.reorder, ["a"], "sideways")
        self.m.jobs["a"]["status"] = "running"
        self.assertRaises(KeyError, self.m.reorder, ["a"], "top")
        # A selection in which one job has started meanwhile still moves the others. The queue stood at c, d, b.
        self.m.reorder(["a", "d"], "top")
        self.assertEqual("".join(self.order()), "dcbxyz")

    def test_a_selection_is_cancelled_in_one_go(self):
        from unittest import mock
        for i, jid in enumerate("abc", 1):
            self.render(jid, i, i)
        self.m.jobs["b"]["status"] = "running"
        # The queued jobs are cancelled, the one that started meanwhile keeps running, an id nobody knows is
        # passed over, and the job list is written once.
        with mock.patch.object(self.m, "_save") as save:
            self.assertEqual(self.m.cancel_queued(["a", "b", "c", "nope"]), 2)
        self.assertEqual([self.m.jobs[j]["status"] for j in "abc"], ["cancelled", "running", "cancelled"])
        self.assertTrue(self.m.jobs["a"]["finished"])
        save.assert_called_once_with()
        # Nothing to cancel writes nothing.
        with mock.patch.object(self.m, "_save") as save:
            self.assertEqual(self.m.cancel_queued(["b", "nope"]), 0)
        save.assert_not_called()

    def test_a_moved_job_stays_behind_the_jobs_of_its_batch_it_waits_for(self):
        from serial_audio.web.jobs import JobManager

        def batch():
            self.m.jobs = {}
            self.render("a", 1, 1)
            self.queued("s2", 2, "screenplay", 2, batch="B")
            self.render("r2", 3, 2, batch="B")
            self.render("r9", 4, 9, batch="B")

        # Chapter 2's render was queued with its screenplay. Moved to the top it stops behind the screenplay,
        # and chapter 9's render, which waits for neither, goes all the way.
        batch()
        self.m.reorder(["r2", "r9"], "top")
        self.assertEqual(self.order(), ["r9", "a", "s2", "r2"])
        # The screenplay moved to the bottom takes its render with it.
        batch()
        self.m.reorder(["s2"], "bottom")
        self.assertEqual(self.order(), ["a", "r9", "s2", "r2"])
        # Two jobs that were not queued together move past each other freely.
        batch()
        self.m.jobs["r2"]["batch"] = None
        self.m.reorder(["r2"], "top")
        self.assertEqual(self.order(), ["r2", "a", "s2", "r9"])

        # On one machine every job of a batch keeps its place among them.
        one = JobManager(state_dir=Path(self.tmp.name) / "one", autostart=False)
        try:
            batch()
            one.jobs = self.m.jobs
            one.reorder(["r9"], "top")
            self.assertEqual(self.order(), ["a", "s2", "r2", "r9"])
            # The old way to move one job by one place still works.
            one.move("a", "down")
            self.assertEqual(self.order(), ["s2", "a", "r2", "r9"])
        finally:
            one._lock_file.close()


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

        # A job of a kind that changes more than its chapter waits for any running job and for a queued job that
        # is not backlog work, and for nothing else.
        def job(status, backlog=False, kind="cast", **extra):
            return {"status": status, "backlog": backlog, "kind": kind, "title": "Render · ch 9", **extra}
        mine = job("queued", True)
        self.assertIsNone(backlog_wait(mine, [mine, job("queued", True), job("done"), job("failed")]))
        self.assertEqual(backlog_wait(mine, [mine, job("queued")]), "backlog work, waiting for the queue to empty")
        self.assertEqual(backlog_wait(mine, [mine, job("running", True)]),
                         "backlog work, waiting for Render · ch 9 to finish")

        # A backlog render or take check doesn't wait for a running backlog render or take check, so two of them
        # run on two nodes. It waits for running backlog work of any other kind, and that waits for it.
        held = "backlog work, waiting for Render · ch 9 to finish"
        for mine_kind, other_kind, reason in (("render", "render", None), ("check-takes", "render", None),
                                              ("render", "check-takes", None), ("render", "cast", held),
                                              ("cast", "render", held)):
            mine = job("queued", True, mine_kind)
            self.assertEqual(backlog_wait(mine, [mine, job("running", True, other_kind)]), reason, (mine_kind, other_kind))

        # It still waits for a job that is not backlog work, running or queued, and for a backlog job that is
        # between two stages on the cluster.
        mine = job("queued", True, "render")
        self.assertEqual(backlog_wait(mine, [mine, job("running", False, "render")]), held)
        self.assertEqual(backlog_wait(mine, [mine, job("running", True, "render"), job("queued", False, "render")]),
                         "backlog work, waiting for the queue to empty")
        self.assertEqual(backlog_wait(mine, [mine, job("queued", True, "render", nomad={"stage": 1})]), held)

        # It doesn't start beside a running backlog render while a backlog job of another kind is queued ahead of
        # it, or that job would wait for as long as renders keep coming. One queued after it doesn't hold it.
        mine = job("queued", True, "render", seq=5)
        running = job("running", True, "render", seq=1)
        self.assertEqual(backlog_wait(mine, [running, job("queued", True, "cast", seq=3), mine]), held)
        self.assertIsNone(backlog_wait(mine, [running, mine, job("queued", True, "cast", seq=7)]))
        self.assertIsNone(backlog_wait(mine, [running, job("queued", True, "check-takes", seq=3), mine]))

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

        # Then the backlog jobs run one at a time, in the order they were queued. The second one doesn't wait as
        # backlog work, since two take checks may run beside each other: it waits because one machine runs one
        # heavy job at a time.
        plain["status"] = "done"
        self.m.tick()
        self.assertEqual(self.started, [plain["id"], first["id"]])
        self.assertEqual(self.m.wait_reasons[second["id"]], f"waiting for {first['title']}")
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
