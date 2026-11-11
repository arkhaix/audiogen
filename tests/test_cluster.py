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
        self.m.jobs = {j["id"]: j for j in (first, queued("s19", 2, "screenplay", 19), queued("r18", 3, "render", 18),
                                            queued("r19", 4, "render", 19))}
        dispatch = []
        # While chapter 18's screenplay runs, its render waits for it, and chapter 19's jobs wait their turn.
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, [])
        self.assertEqual(self.m.wait_reasons["r18"], "waiting for Screenplay · ch 18, which works on the same chapter")
        self.assertEqual(self.m.wait_reasons["s19"], "waiting for Screenplay · ch 18")
        self.assertEqual(self.m.wait_reasons["r19"],
                         "waiting for Screenplay · ch 19, queued ahead of it on the same serial")
        # Once it is written, chapter 18 renders beside chapter 19's screenplay, which is earlier in the batch.
        first["status"] = "done"
        self.m._schedule(0.0, dispatch)
        self.assertEqual(dispatch, ["s19", "r18"])
        self.assertEqual(self.m.jobs["r18"]["nomad"]["target"], "laptop")
        self.assertEqual(self.m.wait_reasons["r19"], "waiting for Screenplay · ch 19, which works on the same chapter")

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

    def test_a_job_that_needs_a_node_waits_for_it_and_may_switch_class(self):
        from unittest import mock
        self.m.placement = {"chapters": {"demo": {"1": "mac"}}}
        check = job("check-takes", chapters=[1], args=["check-takes", "demo", "1"])
        self.assertEqual(self.m._pick_node(check, "comfyui", [])[:2], ("mac", "breeze"))
        node, cls, why = self.m._pick_node(check, "comfyui", [self.running_on("mac")])
        self.assertIsNone(node)
        self.assertIn("mac, which holds the files", why)
        render = job("render", chapters=[1], args=["render", "demo", "1"])
        render["meta"]["engine"] = "breeze"
        with mock.patch("serial_audio.web.nomad.reusable_takes", return_value=(200, 200)):
            self.assertEqual(self.m._pick_node(render, "breeze", [])[:2], ("mac", "breeze"))

    def test_a_render_goes_where_it_finishes_first_counting_the_takes_a_node_holds(self):
        from unittest import mock
        self.m.placement = {"chapters": {"demo": {"1": "mac"}}}
        render = {"kind": "render", "slug": "demo", "args": ["render", "demo", "1", "--seed", "2"],
                  "meta": {"chapters": [1], "engine": "breeze", "runner": "cpp", "lines": 255}}
        # 25 takes on the Mac beat 255 on the laptop, and 252 on the Mac do not.
        with mock.patch("serial_audio.web.nomad.reusable_takes", return_value=(230, 255)) as reuse:
            self.assertEqual(self.m._pick_node(render, "breeze", [])[:2], ("mac", "breeze"))
        reuse.assert_called_once_with("demo", 1, 2, "cpp")
        with mock.patch("serial_audio.web.nomad.reusable_takes", return_value=(3, 255)):
            self.assertEqual(self.m._pick_node(render, "breeze", [])[:2], ("laptop", "breeze"))
            # With the laptop busy, waiting for it still beats a full render on the Mac.
            node, cls, why = self.m._pick_node(render, "breeze", [self.running_on("laptop")])
            self.assertIsNone(node)
            self.assertTrue(why.startswith("waiting for laptop, about"), why)
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

    def test_a_voice_pick_runs_where_its_candidate_was_made(self):
        from unittest import mock
        rec = {"chosen": 0, "candidates": [{"seed": 13000, "clip": "/Users/aj/serial-audio/output/k_s13000.wav"}],
               "history": [{"candidates": [{"seed": 5002, "clip": "D:\\comfy\\out\\k_00006.flac"}]}]}
        self.m.placement = {"voices": {"demo": {"keeper": "mac"}}}

        def pick(*extra):
            return {"kind": "pick-voice", "slug": "demo", "meta": {"characters": ["keeper"]},
                    "args": ["pick-voice", "demo", "keeper", *extra]}

        with mock.patch("serial_audio.library.load_yaml", return_value=rec):
            # A pick by seed goes to the node that wrote that candidate, even after a recast on another node.
            self.assertEqual(self.m.holder(pick("--seed", "5002")), ("laptop", True))
            self.assertEqual(self.m.holder(pick("--seed", "13000")), ("mac", True))
            # An approval of the current voice goes to the node that wrote its chosen candidate.
            self.assertEqual(self.m.holder(pick()), ("mac", True))
            # A seed that no candidate has falls back to the node that last wrote the voice.
            self.assertEqual(self.m.holder(pick("--seed", "9")), ("mac", True))

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
