"""Tests for the Breeze voice design path of casting, with a fake breeze-server and a fake recognizer, and for the
casting bios, with a fake LLM."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serial_audio import asr, breeze, casting, machine, pronounce
from serial_audio import library as lib
from serial_audio.comfy import ComfyError
from serial_audio.llm import LLMError, PassResult
from serial_audio.web.exchange import mirror_rel

SLUG = "design-test"
SAMPLE = "I have waited a hundred and fifty years for this."
BRIEF = {"id": "meredith", "instruct": "A woman in her late twenties with a low alto voice.", "sample_text": SAMPLE}


def setUpModule():
    # The fake recognizers here give a transcript only, so no option is screened, whatever models this machine has.
    patcher = mock.patch.object(asr, "extra_blocks", lambda: set())
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class FakeLLM:
    """run_pass that answers the casting pass and the bio pass for the ids in its input and records each call.

    A bio names the call that wrote it. An id in drop doesn't get a bio.
    """

    def __init__(self):
        self.calls: list[tuple[str, list[str], str]] = []
        self.drop: set[str] = set()

    def __call__(self, system_prompt, user_text, schema, **kwargs):
        ids = re.findall(r"^## (\S+)$", user_text, re.M)
        kind = "bios" if "bios" in schema["properties"] else "voices"
        self.calls.append((kind, ids, user_text))
        if kind == "bios":
            data = {"bios": [{"id": i, "bio": f"Bio {len(self.calls)} of {i}."} for i in ids if i not in self.drop]}
        else:
            data = {"voices": [{"id": i, "instruct": f"The voice of {i}.", "sample_text": SAMPLE} for i in ids]}
        return PassResult(data=data, backend="fake", model="fake", cost_estimate_usd=0.0)


REAL_LEVEL = casting.level_clips


class FakeServer:
    """breeze-server that returns a second of silence per request and records what it was sent.

    A request whose seed is in clipped_seeds gets a second at full scale instead.
    """

    instances: list["FakeServer"] = []
    fail_text = None
    clipped_seeds: frozenset[int] = frozenset()

    def __init__(self, log_path, log):
        self.sample_rate = 24000
        self.requests: list[tuple[dict, bytes | None]] = []
        self.running = False
        FakeServer.instances.append(self)

    def refuse_if_busy(self):
        pass

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def take(self, fields, wav, label, log):
        assert self.running
        self.requests.append((dict(fields), wav))
        if fields["text"] == FakeServer.fail_text:
            raise ComfyError(f"breeze-server answered 500 for {label}")
        return (b"\xff\x7f" if fields["seed"] in FakeServer.clipped_seeds else b"\x00\x00") * self.sample_rate


def heard_for(good_seed: int):
    """A fake recognizer that hears the sample line in the take of good_seed and a loop in every other take."""
    def records(files, *, engine, comfy_url):
        for i, f in enumerate(files):
            yield i, {"text": "I have waited 150 years for this." if f"_s{good_seed}" in Path(f).name
                      else "This this this."}
    return records


class BreezeCastTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        patches = [mock.patch.object(lib, "LIBRARY", root / "library"),
                   mock.patch.object(casting, "BREEZE_CAST_OUTPUT", root / "output" / "breeze" / "cast"),
                   mock.patch.object(breeze, "CppServer", FakeServer),
                   mock.patch.object(breeze, "wait_comfy_idle", lambda *a, **k: None),
                   mock.patch.object(breeze, "free_comfy", lambda *a, **k: None),
                   mock.patch.object(asr, "check_ready", lambda *a, **k: None),
                   mock.patch.object(casting, "level_clips", self.level)]
        self.leveled: list[tuple[str, bool]] = []
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        FakeServer.instances.clear()
        FakeServer.fail_text = None
        FakeServer.clipped_seeds = frozenset()
        lib.save_serial(SLUG, {"title": "Design Test", "narration": {"point_of_view": "third_person"},
                               "narrator_voice_id": "narrator"})
        character = {"gender": "female", "age_group": "young adult", "importance": "major", "role": "x",
                     "description": "x", "voice_brief": "x", "facts": [], "voice_id": ""}
        lib.save_bible(SLUG, {"characters": {"meredith": {"name": "Meredith", **character},
                                             "ann": {"name": "Ann", **character}}, "pronunciations": {}})
        lib.save_yaml(lib.serial_dir(SLUG) / "voices" / "_briefs.yaml",
                      {"meredith": BRIEF, "ann": {"id": "ann", "instruct": "A low alto.", "sample_text": "Ann speaks."}})

    def level(self, candidates, log=print):
        """Stands in for the leveler: notes each clip and whether it was scored yet, and marks the clip's file."""
        for c in candidates:
            self.leveled.append((Path(c["clip"]).name, "score" in c))
            with open(c["clip"], "ab") as f:
                f.write(b"leveled")
            c["lufs"] = -18.0

    def cast(self, good_seed: int, seed0: int = 1000, **kwargs):
        with mock.patch.object(asr, "records", heard_for(good_seed)):
            return casting.cast(SLUG, only=["meredith"], candidates=3, seed0=seed0, engine="breeze", asr="parakeet",
                                log=lambda *_: None, **kwargs)

    def test_engine_choice(self):
        # The flag wins, then serial.yaml cast_engine, then breeze. An unknown engine is refused.
        self.assertEqual(casting.plan_cast_engine({}), "breeze")
        self.assertEqual(casting.plan_cast_engine({"cast_engine": "qwen"}), "qwen")
        self.assertEqual(casting.plan_cast_engine({"cast_engine": "breeze"}), "breeze")
        self.assertEqual(casting.plan_cast_engine({"cast_engine": "breeze"}, "qwen"), "qwen")
        with self.assertRaises(ComfyError):
            casting.plan_cast_engine({}, "drama3")

    def test_breeze_cast_designs_scores_and_files(self):
        # Design: one server session, one request per seed, each a design request with no reference clip.
        [record] = self.cast(good_seed=1001)
        [server] = FakeServer.instances
        self.assertFalse(server.running)
        self.assertEqual([f["seed"] for f, _ in server.requests], [1000, 1001, 1002])
        for fields, wav in server.requests:
            self.assertIsNone(wav)
            self.assertEqual(fields["text"], SAMPLE)
            self.assertEqual(fields["instruction"], BRIEF["instruct"])
            self.assertEqual(fields["cfg_scale"], casting.DESIGN_CFG)

        # Score: the take that says the line wins, with its numbers written as digits.
        self.assertEqual(record["chosen"], 1)
        self.assertEqual(record["seed"], 1001)
        self.assertEqual(record["candidates"][1]["score"], 1.0)
        self.assertEqual(record["status"], "auto")

        # File: a Breeze record with the clip and no Drama 3 voice, and the bible points at it.
        self.assertEqual((record["engine"], record["asr"], record["source"]),
                         ("breeze", "parakeet", "breeze-tts-2-voicedesign"))
        self.assertNotIn("drama3_voice", record)
        self.assertNotIn("voice_file", record["candidates"][0])
        voices = lib.serial_dir(SLUG) / "voices"
        self.assertTrue((voices / "meredith.wav").is_file())
        self.assertFalse((voices / "meredith.safetensors").exists())
        self.assertEqual(lib.load_yaml(voices / "meredith.yaml")["engine"], "breeze")
        self.assertEqual(lib.load_bible(SLUG)["characters"]["meredith"]["voice_id"], "meredith")

        # Level: every candidate's clip is leveled after it was scored, and the filed clip is the leveled one.
        self.assertEqual(self.leveled, [("meredith_s1000.wav", True), ("meredith_s1001.wav", True),
                                        ("meredith_s1002.wav", True)])
        self.assertTrue((voices / "meredith.wav").read_bytes().endswith(b"leveled"))
        self.assertEqual([c["lufs"] for c in record["candidates"]], [-18.0, -18.0, -18.0])

    def test_recast_and_pick_keep_the_engine(self):
        # A recast moves the first batch to history with its engine and recognizer.
        self.cast(good_seed=1001)
        first_clip = lib.load_yaml(lib.serial_dir(SLUG) / "voices" / "meredith.yaml")["candidates"][1]["clip"]
        [record] = self.cast(good_seed=2002, seed0=2000, recast=True, note="higher")
        self.assertEqual(record["seed"], 2002)
        [old] = record["history"]
        self.assertEqual((old["engine"], old["asr"], old["seed"], old["why_replaced"]),
                         ("breeze", "parakeet", 1001, "higher"))

        # pick-voice re-files the history batch as a Breeze voice, with no Drama 3 voice file needed.
        picked = casting.pick_voice(SLUG, "meredith", seed=1001)
        self.assertEqual((picked["engine"], picked["seed"], picked["status"]), ("breeze", 1001, "approved"))
        self.assertNotIn("drama3_voice", picked)
        self.assertEqual([(h["seed"], h["engine"]) for h in picked["history"]], [(2002, "breeze")])

        # A recast that reuses seeds writes new clip files, so the clips the history names stay on disk.
        [again] = self.cast(good_seed=1001, recast=True)
        self.assertTrue(again["candidates"][1]["clip"].endswith("meredith_s1001_2.wav"), again["candidates"][1])
        self.assertTrue(Path(first_clip).is_file())
        self.assertEqual([h["seed"] for h in again["history"]], [2002, 1001])

    def test_the_leveler_runs_the_join_script_and_never_stops_a_cast(self):
        clips = [{"clip": "a.wav"}, {"clip": "b.wav"}]
        logs: list[str] = []
        answer = json.dumps([{"file": "a.wav", "before": -27.3, "after": -18.0, "peak": -4.0},
                             {"file": "b.wav", "before": None, "after": None, "peak": None}])

        # The audio Python gets the join script and the clips, and each clip with a level gets it recorded.
        with mock.patch.object(casting.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "noise\n" + answer, "")) as run:
            REAL_LEVEL(clips, logs.append)
        args = run.call_args[0][0]
        self.assertEqual(args[1:], ["-I", str(casting.LEVEL_SCRIPT), "--level", "a.wav", "b.wav"])
        self.assertEqual(clips, [{"clip": "a.wav", "lufs": -18.0}, {"clip": "b.wav"}])
        self.assertIn("-27.3 to -18.0", logs[-1])

        # A failure of the audio Python, a missing one and an answer that isn't the list leave the clips alone.
        for outcome in (subprocess.CompletedProcess([], 1, "", "Traceback: no soundfile"),
                        subprocess.CompletedProcess([], 0, "{}", ""), subprocess.CompletedProcess([], 0, "", ""),
                        OSError("no such file")):
            clips = [{"clip": "a.wav"}]
            patch = (mock.patch.object(casting.subprocess, "run", side_effect=outcome) if isinstance(outcome, OSError)
                     else mock.patch.object(casting.subprocess, "run", return_value=outcome))
            with patch:
                REAL_LEVEL(clips, logs.append)
            self.assertEqual(clips, [{"clip": "a.wav"}], outcome)
            self.assertIn("were not leveled", logs[-1])

    def test_a_clipped_candidate_is_chosen_last(self):
        # The best match is clipped, so the next candidate is filed and the clipped one is marked.
        FakeServer.clipped_seeds = frozenset({1001})
        [record] = self.cast(good_seed=1001)
        self.assertNotEqual(record["seed"], 1001)
        self.assertEqual(record["status"], "review")
        self.assertEqual([bool(c.get("clipped")) for c in record["candidates"]], [False, True, False])

        # A clean candidate that matches still files as auto beside a clipped one.
        FakeServer.clipped_seeds = frozenset({2000})
        [record] = self.cast(good_seed=2001, seed0=2000, recast=True)
        self.assertEqual((record["seed"], record["status"]), (2001, "auto"))

        # When every candidate is clipped the best match is filed for review.
        FakeServer.clipped_seeds = frozenset({3000, 3001, 3002})
        [record] = self.cast(good_seed=3001, seed0=3000, recast=True)
        self.assertEqual((record["seed"], record["status"]), (3001, "review"))

    def test_a_failed_design_files_the_voices_before_it(self):
        # Design: ann's design fails after meredith's three succeed, and the server still stops.
        FakeServer.fail_text = "Ann speaks."
        with mock.patch.object(asr, "records", heard_for(1001)), self.assertRaises(ComfyError) as e:
            casting.cast(SLUG, only=["meredith", "ann"], candidates=3, engine="breeze", asr="parakeet",
                         log=lambda *_: None)
        self.assertIn("designing ann seed 1000 failed", str(e.exception))
        self.assertFalse(FakeServer.instances[0].running)

        # File: meredith is filed before the error is raised, and ann stays uncast for a rerun.
        chars = lib.load_bible(SLUG)["characters"]
        self.assertEqual((chars["meredith"]["voice_id"], chars["ann"]["voice_id"]), ("meredith", ""))
        self.assertTrue((lib.serial_dir(SLUG) / "voices" / "meredith.yaml").is_file())
        self.assertFalse((lib.serial_dir(SLUG) / "voices" / "ann.yaml").exists())

    def test_the_casting_pass_brings_a_bio_and_a_cached_brief_does_not_call_the_llm(self):
        llm = FakeLLM()
        with mock.patch.object(casting, "run_pass", llm):
            # Cached brief: the cast designs from it. It doesn't call the LLM or write a bio.
            self.cast(good_seed=1001)
            self.assertEqual(llm.calls, [])
            self.assertNotIn("bio", lib.load_bible(SLUG)["characters"]["meredith"])

            # Briefs stage of a recast with a fresh brief: the casting pass runs, then the bio pass. The bio is saved,
            # and the bible on disk keeps the voice the recast has not replaced yet.
            casting.cast(SLUG, only=["meredith"], recast=True, redo_brief=True, dry_run=True, log=lambda *_: None)
            self.assertEqual([(kind, ids) for kind, ids, _ in llm.calls],
                             [("voices", ["meredith"]), ("bios", ["meredith"])])
            saved = lib.load_bible(SLUG)["characters"]["meredith"]
            self.assertEqual((saved["bio"], saved["voice_id"]), ("Bio 2 of meredith.", "meredith"))

            # Voices stage: the brief is cached, so the LLM isn't called, and filing the voice keeps the bio.
            self.cast(good_seed=2001, seed0=2000, recast=True)
            self.assertEqual(len(llm.calls), 2)
            self.assertEqual(lib.load_bible(SLUG)["characters"]["meredith"]["bio"], "Bio 2 of meredith.")

            # Both stages in one process: the bible that filing saves holds the new bio.
            self.cast(good_seed=3001, seed0=3000, recast=True, redo_brief=True)
            self.assertEqual([kind for kind, _, _ in llm.calls[2:]], ["voices", "bios"])
            saved = lib.load_bible(SLUG)["characters"]["meredith"]
            self.assertEqual((saved["bio"], saved["voice_id"]), ("Bio 4 of meredith.", "meredith"))

            # A bio pass that leaves the character out is logged and doesn't stop the cast. The voice is filed and
            # the bio stays as it was.
            llm.drop = {"meredith"}
            lines = []
            with mock.patch.object(asr, "records", heard_for(4001)):
                [record] = casting.cast(SLUG, only=["meredith"], candidates=3, seed0=4000, engine="breeze",
                                        asr="parakeet", recast=True, redo_brief=True, log=lines.append)
            self.assertEqual(record["seed"], 4001)
            self.assertTrue(any("the bio pass failed" in line for line in lines), lines)
            self.assertEqual(lib.load_bible(SLUG)["characters"]["meredith"]["bio"], "Bio 4 of meredith.")

    def test_old_qwen_history_files_as_qwen(self):
        # A batch with no engine key comes from before Breeze design, so pick-voice files it as a Qwen voice.
        out = Path(self.tmp.name) / "output"
        out.mkdir(parents=True, exist_ok=True)
        clip, voice = out / "m_00001_.flac", out / "m_00001_.safetensors"
        clip.write_bytes(b"flac")
        voice.write_bytes(b"voice")
        self.cast(good_seed=1001)
        path = lib.serial_dir(SLUG) / "voices" / "meredith.yaml"
        rec = lib.load_yaml(path)
        rec["history"] = [{"instruct": "old", "sample_text": SAMPLE, "seed": 9000, "status": "auto",
                           "candidates": [{"seed": 9000, "clip": str(clip), "voice_file": str(voice), "score": 1.0}]}]
        lib.save_yaml(path, rec)
        with mock.patch.object(casting, "VOICES_OUTPUT", out / "drama3_voices"):
            picked = casting.pick_voice(SLUG, "meredith", seed=9000)
        self.assertEqual((picked["engine"], picked["drama3_voice"]), ("qwen", "meredith.safetensors"))
        self.assertTrue((lib.serial_dir(SLUG) / "voices" / "meredith.safetensors").is_file())
        # On a machine with ComfyUI's output folder, Load Drama 3 Voice gets the file too.
        self.assertEqual((out / "drama3_voices" / SLUG / "meredith.safetensors").read_bytes(), b"voice")

    def send_to_mirror(self, mirror: Path, batch: dict, folder: str) -> None:
        """Moves a batch's candidate files into the mirror, as if another node had made them in folder there."""
        sep = "\\" if "\\" in folder else "/"
        for c in batch["candidates"]:
            for field in ("clip", "voice_file"):
                if c.get(field):
                    raw = folder + sep + Path(c[field]).name
                    copy = mirror / mirror_rel(raw)
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    Path(c[field]).replace(copy)
                    c[field] = raw

    def test_a_pick_files_a_candidate_another_node_made_from_the_app_s_mirror(self):
        # Setup: the first cast's candidates are on a Windows node and the recast's on a Mac. This machine holds
        # neither, and the mirror holds both.
        self.cast(good_seed=1001)
        self.cast(good_seed=2002, seed0=2000, recast=True)
        voices = lib.serial_dir(SLUG) / "voices"
        mirror = Path(self.tmp.name) / "mirror"
        rec = lib.load_yaml(voices / "meredith.yaml")
        self.send_to_mirror(mirror, rec, "/Users/nobody/serial-audio/output/breeze/cast/" + SLUG)
        self.send_to_mirror(mirror, rec["history"][0], "D:\\nowhere\\output\\breeze\\cast\\" + SLUG)
        lib.save_yaml(voices / "meredith.yaml", rec)
        (voices / "meredith.wav").unlink()

        # Without the mirror the pick fails and files nothing.
        with self.assertRaises(FileNotFoundError):
            casting.pick_voice(SLUG, "meredith", seed=1001)
        self.assertFalse((voices / "meredith.wav").exists())

        with mock.patch.object(machine, "MEDIA_MIRROR", mirror):
            # A pick by seed files that candidate, from the batch the Windows node made and from the Mac's.
            picked = casting.pick_voice(SLUG, "meredith", seed=1001)
            self.assertEqual((picked["seed"], picked["status"], picked["engine"]), (1001, "approved", "breeze"))
            self.assertTrue((voices / "meredith.wav").is_file())
            (voices / "meredith.wav").unlink()
            picked = casting.pick_voice(SLUG, "meredith", seed=2001)
            self.assertEqual((picked["seed"], picked["chosen"]), (2001, 1))
            self.assertTrue((voices / "meredith.wav").is_file())
            # The record keeps the candidates' paths on their nodes, which the app plays from the mirror.
            self.assertTrue(picked["candidates"][1]["clip"].startswith("/Users/nobody/"), picked["candidates"][1])

            # An approval of the current voice files its chosen candidate again.
            (voices / "meredith.wav").unlink()
            picked = casting.pick_voice(SLUG, "meredith")
            self.assertEqual((picked["seed"], picked["status"]), (2001, "approved"))
            self.assertTrue((voices / "meredith.wav").is_file())

            # A seed that no candidate has is refused, and the voice stays as it was.
            with self.assertRaises(ValueError):
                casting.pick_voice(SLUG, "meredith", seed=9)
            self.assertEqual(lib.load_yaml(voices / "meredith.yaml")["seed"], 2001)

    def test_a_qwen_pick_on_the_app_s_machine_files_what_the_mirror_holds(self):
        # Setup: a Qwen batch whose clip and Drama 3 voice file are only in the mirror, on a machine with no
        # ComfyUI output folder.
        out = Path(self.tmp.name) / "output"
        out.mkdir(parents=True, exist_ok=True)
        clip, voice = out / "m_00001_.flac", out / "m_00001_.safetensors"
        clip.write_bytes(b"flac")
        voice.write_bytes(b"voice")
        self.cast(good_seed=1001)
        voices = lib.serial_dir(SLUG) / "voices"
        rec = lib.load_yaml(voices / "meredith.yaml")
        rec["history"] = [{"instruct": "old", "sample_text": SAMPLE, "seed": 9000, "status": "auto",
                           "candidates": [{"seed": 9000, "clip": str(clip), "voice_file": str(voice), "score": 1.0}]}]
        mirror = Path(self.tmp.name) / "mirror"
        self.send_to_mirror(mirror, rec["history"][0], "D:\\nowhere\\output\\drama3\\cast\\" + SLUG)
        lib.save_yaml(voices / "meredith.yaml", rec)
        no_comfy = Path(self.tmp.name) / "no-comfy"

        with mock.patch.object(machine, "MEDIA_MIRROR", mirror), \
                mock.patch.object(casting, "VOICES_OUTPUT", no_comfy / "drama3_voices"):
            # The clip and the voice file are filed in the library, and no ComfyUI folder is made here.
            picked = casting.pick_voice(SLUG, "meredith", seed=9000)
            self.assertEqual((picked["engine"], picked["drama3_voice"], picked["comfy_voice"]),
                             ("qwen", "meredith.safetensors", f"{SLUG}/meredith.safetensors"))
            self.assertEqual((voices / "meredith.flac").read_bytes(), b"flac")
            self.assertEqual((voices / "meredith.safetensors").read_bytes(), b"voice")
            self.assertFalse(no_comfy.exists())

            # A candidate whose voice file the mirror lacks is filed with its clip alone, and the record names
            # no Drama 3 voice, so a Drama 3 render refuses the voice instead of using an old file.
            (mirror / mirror_rel(picked["candidates"][0]["voice_file"])).unlink()
            picked = casting.pick_voice(SLUG, "meredith", seed=9000)
            self.assertEqual((picked["engine"], picked["seed"]), ("qwen", 9000))
            self.assertNotIn("drama3_voice", picked)
            self.assertNotIn("drama3_voice", lib.load_yaml(voices / "meredith.yaml"))

    def test_a_drama_3_render_copies_a_voice_the_app_filed_to_comfyui(self):
        from serial_audio import render
        voices = lib.serial_dir(SLUG) / "voices"
        lib.save_yaml(voices / "meredith.yaml", {"id": "meredith", "drama3_voice": "meredith.safetensors"})
        lib.save_yaml(voices / "ann.yaml", {"id": "ann", "engine": "breeze"})
        (voices / "meredith.safetensors").write_bytes(b"picked on the app")
        comfy = Path(self.tmp.name) / "comfy"
        staged = comfy / "drama3_voices" / SLUG / "meredith.safetensors"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"the voice before the pick")
        with mock.patch.object(render, "COMFY_OUTPUT", comfy):
            # The voice whose file differs in ComfyUI's folder is replaced. A Breeze voice and an unknown voice
            # are skipped.
            self.assertEqual(render.stage_drama3_voices(SLUG, {"meredith", "ann", "nobody"}), ["meredith"])
            self.assertEqual(staged.read_bytes(), b"picked on the app")
            # A voice that is already there is left alone, and a missing one is copied.
            self.assertEqual(render.stage_drama3_voices(SLUG, {"meredith"}), [])
            staged.unlink()
            self.assertEqual(render.stage_drama3_voices(SLUG, {"meredith"}), ["meredith"])
            self.assertTrue(staged.is_file())


OPTIONS_SCRIPT = [{"speaker": "narrator", "text": "Chapter One.", "direction": ""},
                  {"speaker": "narrator", "text": "Eole!", "direction": ""},
                  {"speaker": "narrator", "text": "The guard told Louis that Eole had not come back.", "direction": "flatly"},
                  {"speaker": "narrator", "text": "Nobody had seen the Eolean hills.", "direction": ""}]


class SayOptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "ref.wav").write_bytes(b"ref")
        self.answers: list[dict] = []
        self.inputs: list[str] = []

        def run_pass(system_prompt, user_text, schema, **kwargs):
            self.inputs.append(user_text)
            return PassResult(data=self.answers.pop(0), backend="fake", model="fake", cost_estimate_usd=0.0)

        def stage(slug, voices, runner):
            return {v: {"ref": str(root / "ref.wav"), "transcript": "t", "sha1": "s" * 40} for v in voices}

        def hears(files, *, engine, comfy_url):
            for i, f in enumerate(files):
                yield i, {"text": f"heard {Path(f).name[-5]}"}

        class Server(FakeServer):
            """A take of four seconds, a length the options' line may have, so each candidate is one request."""

            def take(self, fields, wav, label, log):
                return super().take(fields, wav, label, log) * 4

        for p in (mock.patch.object(lib, "LIBRARY", root / "library"),
                  mock.patch.object(pronounce, "SAY_OUTPUT", root / "output" / "breeze" / "say"),
                  mock.patch.object(pronounce, "run_pass", run_pass),
                  mock.patch.object(breeze, "CppServer", Server),
                  mock.patch.object(breeze, "wait_comfy_idle", lambda *a, **k: None),
                  mock.patch.object(breeze, "free_comfy", lambda *a, **k: None),
                  mock.patch.object(breeze, "stage_references", stage),
                  mock.patch.object(asr, "choose", lambda *a, **k: "parakeet"),
                  mock.patch.object(asr, "records", hears)):
            p.start()
            self.addCleanup(p.stop)
        FakeServer.instances.clear()
        FakeServer.fail_text = None
        FakeServer.clipped_seeds = frozenset()
        lib.save_serial(SLUG, {"title": "Design Test", "author": "A. Writer", "narrator_voice_id": "narrator",
                               "narration": {"point_of_view": "third_person"}})
        lib.save_bible(SLUG, {"characters": {"eole": {"name": "Eole", "description": "A winged singer."}},
                              "pronunciations": {"Eole": {"respelling": "ay-OHL", "note": "French."},
                                                 "Louis": {"say": "Lewis"}}})
        lib.save_yaml(lib.serial_dir(SLUG) / "voices" / "narrator.yaml", {"clip": "narrator.wav", "sample_text": "t"})
        folder = lib.chapter_dir(SLUG, 1)
        folder.mkdir(parents=True)
        (folder / "text.md").write_text("x", encoding="utf-8")
        lib.save_chapter_meta(SLUG, 1, {"number": 1, "render": {"engine": "breeze", "seed": 5}})
        lib.save_json(folder / "script.json", {"lines": OPTIONS_SCRIPT})

    def entry(self) -> dict:
        return lib.load_yaml(lib.serial_dir(SLUG) / "bible" / "pronunciations.yaml")["Eole"]

    def test_the_pass_stores_the_respellings_beside_the_term_as_written(self):
        # The answer repeats the term, repeats a respelling in another case, has one without a letter and has more
        # than the limit.
        self.answers.append({"options": [{"say": "Ay-ole", "why": "Two beats."}, {"say": "Eole", "why": "x"},
                                         {"say": "ay-ole", "why": "x"}, {"say": "123", "why": "x"}]
                                        + [{"say": f"Option {c}", "why": "x"} for c in "abcdefg"]})
        options = pronounce.options_pass(SLUG, "Eole", log=lambda *_: None)

        # The term as written is first, then six respellings, each once. The line is the first of a sayable length,
        # found as a say-as would find it, so "Eolean" is not the term.
        self.assertEqual([c["say"] for c in options["candidates"]],
                         ["Eole", "Ay-ole", "Option a", "Option b", "Option c", "Option d", "Option e"])
        self.assertEqual(options["candidates"][0]["why"], "as written")
        self.assertEqual(options["line"], {"chapter": 1, "index": 2, "speaker": "narrator",
                                           "text": OPTIONS_SCRIPT[2]["text"]})
        self.assertEqual([i for _, i, _ in pronounce.lines_naming(SLUG, "Eole")], [1, 2])

        # The pass read the term, what the bible says about its sound, who it is and the lines that have it.
        for part in ("## Term\n\nEole", "respelling: ay-OHL", "note: French.", "Eole: A winged singer.",
                     "- chapter 1, narrator: Eole!"):
            self.assertIn(part, self.inputs[0])

        # The entry keeps its respelling, gets the options, and doesn't get a say-as. Another entry is as it was.
        saved = lib.load_yaml(lib.serial_dir(SLUG) / "bible" / "pronunciations.yaml")
        self.assertEqual((saved["Eole"]["respelling"], saved["Eole"]["options"], "say" in saved["Eole"]),
                         ("ay-OHL", options, False))
        self.assertEqual(saved["Louis"], {"say": "Lewis"})

        # The pass also gets the respelling of a longer term that has the word, and a line of a chapter with a
        # Breeze render is chosen before an earlier line of a chapter without one.
        bible = lib.load_bible(SLUG)
        bible["pronunciations"]["Eole Ashmodai"] = {"respelling": "ay-OHL ash-moh-DYE"}
        self.assertIn("in Eole Ashmodai: ay-OHL ash-moh-DYE",
                      pronounce._options_input({"title": "t"}, bible, "Eole", pronounce.lines_naming(SLUG, "Eole")))
        early = lib.chapter_dir(SLUG, 0)
        early.mkdir(parents=True)
        (early / "text.md").write_text("x", encoding="utf-8")
        lib.save_chapter_meta(SLUG, 0, {"number": 0, "render": {"codes_file": "x"}})
        lib.save_json(early / "script.json", {"lines": [{"speaker": "narrator", "direction": "",
                                                         "text": "Nobody in the harbour had ever heard of Eole."}]})
        self.answers.append({"options": [{"say": "Ay-ole", "why": "x"}]})
        self.assertEqual(pronounce.options_pass(SLUG, "Eole", log=lambda *_: None)["line"]["chapter"], 1)
        lib.save_yaml(lib.serial_dir(SLUG) / "bible" / "pronunciations.yaml",
                      {**lib.load_yaml(lib.serial_dir(SLUG) / "bible" / "pronunciations.yaml"), "Eole": saved["Eole"]})

        # Removing a say-as leaves an entry that has options, and removes one that has neither a respelling nor
        # options.
        pronounce.set_say_as(SLUG, "Eole", "Ay-ole")
        pronounce.set_say_as(SLUG, "Eole", None, clear=True)
        self.assertEqual(self.entry()["options"], options)
        pronounce.set_say_as(SLUG, "Guard", "Gard")
        self.assertIsNone(pronounce.set_say_as(SLUG, "Guard", None, clear=True))

        # A term no line has, and an answer without a usable respelling, change nothing.
        with self.assertRaises(ValueError):
            pronounce.options_pass(SLUG, "Vouivre", log=lambda *_: None)
        self.answers.append({"options": [{"say": "Eole", "why": "x"}]})
        with self.assertRaises(LLMError):
            pronounce.options_pass(SLUG, "Eole", log=lambda *_: None)
        self.assertEqual(self.entry()["options"], options)

    def test_each_option_is_said_in_its_line_by_the_lines_voice(self):
        # A term without options has nothing to say.
        with self.assertRaises(ValueError):
            pronounce.render_options(SLUG, "Eole", log=lambda *_: None)
        self.answers.append({"options": [{"say": "Ay-ole", "why": "x"}, {"say": "Yole", "why": "x"}]})
        pronounce.options_pass(SLUG, "Eole", log=lambda *_: None)
        options = pronounce.render_options(SLUG, "Eole", log=lambda *_: None)

        # One server session says the line three times, with the term as each candidate spells it, and with the
        # other say-as of the line, the line's direction, one seed and the voice's reference clip every time.
        [server] = FakeServer.instances
        self.assertFalse(server.running)
        self.assertEqual([f["text"] for f, _ in server.requests],
                         ["The guard told Lewis that Eole had not come back.",
                          "The guard told Lewis that Ay-ole had not come back.",
                          "The guard told Lewis that Yole had not come back."])
        self.assertEqual(len({f["seed"] for f, _ in server.requests}), 1)
        self.assertEqual({c["seed"] for c in options["candidates"]}, {f["seed"] for f, _ in server.requests})
        self.assertTrue(all("flatly" in f["instruction"] and wav == b"ref" for f, wav in server.requests))

        # Each candidate has its clip, its length, its text and what the recognizer heard, and the options name
        # the voice. The term still has no say-as.
        clips = [Path(c["clip"]) for c in options["candidates"]]
        self.assertTrue(all(c.is_file() and c.parent == pronounce.SAY_OUTPUT / SLUG for c in clips))
        self.assertEqual(len(set(clips)), 3)
        self.assertEqual([(c["seconds"], c["heard"]) for c in options["candidates"]],
                         [(4.0, "heard 0"), (4.0, "heard 1"), (4.0, "heard 2")])
        self.assertEqual((options["voice"], self.entry()["options"]), ("narrator", options))
        self.assertNotIn("say", self.entry())

        # A script whose line changed since the options were made is refused.
        lib.save_json(lib.chapter_dir(SLUG, 1) / "script.json", {"lines": OPTIONS_SCRIPT[:2]})
        with self.assertRaises(ValueError):
            pronounce.render_options(SLUG, "Eole", log=lambda *_: None)


SCREEN_SCRIPT = OPTIONS_SCRIPT + [
    {"speaker": "narrator", "text": f"{before} Eole {after}", "direction": ""}
    for before, after in (("Nobody had told", "about the open gate."), ("The captain asked", "to sing it again."),
                          ("In the morning", "was gone from the hall."), ("They found", "asleep beside the fire."),
                          ("Then", "laughed at all of them."), ("Only", "knew the way down there."),
                          ("At the gate", "turned and waved once."), ("Nobody but", "had kept the old key."))]
# how the fake voice says each spelling, as (the word Parakeet writes, the phones): "Yole" gets a syllable more in
# every other take
SOUNDS = {"Eole": [("Eole", "iol")], "Ay-ole": [("Ay-ole", "eɪoʊl")], "Yole": [("Yole", "jol"), ("Yoley", "joli")]}


class ScreenTest(SayOptionsTest):
    """The options of a term on a machine whose take check has the phone model, while pronounce.SCREENS is on."""

    test_the_pass_stores_the_respellings_beside_the_term_as_written = None
    test_each_option_is_said_in_its_line_by_the_lines_voice = None

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(pronounce, "SCREENS", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        lib.save_json(lib.chapter_dir(SLUG, 1) / "script.json", {"lines": SCREEN_SCRIPT})
        self.blocks: list[tuple] = []

        def hears(files, *, engine, comfy_url, blocks=("asr", "signal")):
            # Hears the text the server was sent for each file, a word every 0.3 s, with the name as SOUNDS has it
            # for the file and "tə" at every other word.
            self.blocks.append(tuple(blocks))
            texts = [f["text"] for f, _ in FakeServer.instances[-1].requests]
            for i, _ in enumerate(files):
                said = texts[i].split()
                symbols, starts = "", []
                for n, word in enumerate(said):
                    spelling = next((s for s in SOUNDS if word.strip(".,!") == s), None)
                    written, sound = SOUNDS[spelling][i % len(SOUNDS[spelling])] if spelling else (word, "tə")
                    said[n] = word.replace(spelling, written) if spelling else word
                    symbols += sound
                    starts += [round(0.3 * n + 0.04 * j, 2) for j in range(len(sound))]
                yield i, {"text": " ".join(said), "tokens": [f" {w}" for w in said],
                          "starts": [round(0.3 * n, 2) for n in range(len(said))], "durations": [0.24] * len(said),
                          "phone": {"v": 1, "engine": "zipa", "phones": symbols, "starts": starts}}

        for p in (mock.patch.object(asr, "extra_blocks", lambda: {"phone"}), mock.patch.object(asr, "records", hears)):
            p.start()
            self.addCleanup(p.stop)

    def test_each_option_is_said_in_eight_lines_and_only_a_steady_one_is_held_to(self):
        self.answers.append({"options": [{"say": "Ay-ole", "why": "x"}, {"say": "Yole", "why": "x"}]})
        pronounce.options_pass(SLUG, "Eole", log=lambda *_: None)
        logs: list[str] = []
        options = pronounce.render_options(SLUG, "Eole", log=logs.append)

        # Each of the three options is said in its line, as before, and in seven other lines that have the term, in
        # the same voice without a direction. The recognizer is asked for the words and the phones.
        [server] = FakeServer.instances
        texts = [f["text"] for f, _ in server.requests]
        self.assertEqual(len(texts), 3 * pronounce.SCREEN_TAKES)
        self.assertEqual(texts[0], "The guard told Lewis that Eole had not come back.")
        self.assertEqual(texts[8], "The guard told Lewis that Ay-ole had not come back.")
        self.assertEqual(len(set(texts[:8])), 8)
        self.assertTrue(all("Ay-ole" in t for t in texts[8:16]) and all("Yole" in t for t in texts[16:]))
        self.assertTrue(all("instruction" in f for f, _ in server.requests[::8]))
        self.assertFalse(any("instruction" in f for n, (f, _) in enumerate(server.requests) if n % 8))
        self.assertEqual(self.blocks, [("asr", "phone")])

        # A spelling that every take says without an added sound gets its usual saying and counts as steady. One
        # that half of the takes add a syllable to is not steady. It is offered all the same, because its clip is
        # a usual take. Each saying is kept with the sounds both recognizers heard added.
        got = {c["say"]: (c.get("said"), c.get("steady"), c.get("typical")) for c in options["candidates"]}
        self.assertEqual(got["Eole"], ("iol", [8, 8], True))
        self.assertEqual(got["Ay-ole"], ("eɪoʊl", [8, 8], True))
        self.assertEqual(got["Yole"][1:], ([4, 8], True))
        self.assertEqual([c.get("screen") for c in options["candidates"]], [pronounce.SCREEN_V] * 3)
        self.assertEqual([pronounce.steady(c) for c in options["candidates"]], [True, True, False])
        self.assertTrue(all(pronounce.offered(c) for c in options["candidates"]))
        self.assertFalse([line for line in logs if "not offered" in line], logs)
        self.assertEqual([spans[0][2] for spans in options["candidates"][2]["sayings"]], ["", "V@3"] * 4)

        # Only the clip AJ hears is kept of each option, and it holds what the recognizer heard.
        left = sorted(p.name for p in (pronounce.SAY_OUTPUT / SLUG).glob("*.wav"))
        self.assertEqual(left, sorted(Path(c["clip"]).name for c in options["candidates"]))
        self.assertEqual(options["candidates"][1]["heard"], texts[8])

        # The term is read as written, so its entry gets the usual saying of that option. A pick moves the usual
        # saying with the spelling, and a spelling that is not steady, or was never screened, has none.
        self.assertEqual(self.entry()["said"], "iol")
        self.assertEqual(pronounce.set_say_as(SLUG, "Eole", "Ay-ole")["said"], "eɪoʊl")
        self.assertNotIn("said", pronounce.set_say_as(SLUG, "Eole", "Yole"))
        self.assertNotIn("said", pronounce.set_say_as(SLUG, "Eole", "Eeyore"))
        self.assertEqual(pronounce.set_say_as(SLUG, "Eole", None, clear=True)["said"], "iol")

        # Nothing is said again while every option is screened, and a typed spelling is screened alone.
        pronounce.render_options(SLUG, "Eole", log=lambda *_: None, missing=True)
        self.assertEqual(len(FakeServer.instances), 1)
        pronounce.add_option(SLUG, "Eole", "Ay-ole")
        self.assertEqual(self.entry()["said"], "iol")

    def test_an_option_gets_one_take_and_no_count_while_the_screen_is_off(self):
        # With the switch off, an option is said once in its line, as on a machine without the phone model, and
        # gets no count.
        with mock.patch.object(pronounce, "SCREENS", False):
            pronounce.add_option(SLUG, "Eole", "Ay-ole")
            options = pronounce.render_options(SLUG, "Eole", log=lambda *_: None)
        self.assertEqual(len(FakeServer.instances[-1].requests), 2)
        self.assertEqual(self.blocks, [("asr", "signal")])
        self.assertFalse(any("steady" in c or "said" in c for c in options["candidates"]))
        self.assertNotIn("said", self.entry())

    def test_a_second_screen_uses_new_seeds_and_counts_with_the_first(self):
        pronounce.add_option(SLUG, "Eole", "Ay-ole")
        pronounce.render_options(SLUG, "Eole", log=lambda *_: None)
        first = FakeServer.instances[-1].requests
        self.assertEqual([c["steady"] for c in self.entry()["options"]["candidates"]], [[8, 8], [8, 8]])

        # A spelling that is typed again is screened again, alone. Its clip is the same take, the seven other
        # lines get seeds the first screen didn't use, and the count is over all fifteen takes.
        pronounce.render_options(SLUG, "Eole", log=lambda *_: None, missing=True, again="Ay-ole")
        second = FakeServer.instances[-1].requests
        self.assertEqual(len(FakeServer.instances), 2)
        self.assertEqual(len(second), pronounce.SCREEN_TAKES)
        self.assertTrue(all("Ay-ole" in f["text"] for f, _ in second))
        self.assertEqual(second[0][0]["seed"], first[8][0]["seed"])
        self.assertFalse({f["seed"] for f, _ in second[1:]} & {f["seed"] for f, _ in first})
        written, typed = self.entry()["options"]["candidates"]
        self.assertEqual((written["steady"], typed["steady"], len(typed["sayings"])), ([8, 8], [15, 15], 15))

        # Every option is said again when the line's voice changed, and the earlier sayings don't count then.
        bible = lib.load_bible(SLUG)
        bible["pronunciations"]["Louis"]["say"] = "Lou-ee"
        lib.save_bible(SLUG, bible)
        pronounce.render_options(SLUG, "Eole", log=lambda *_: None, missing=True)
        self.assertEqual([c["steady"] for c in self.entry()["options"]["candidates"]], [[8, 8], [8, 8]])

    def test_a_count_of_added_sounds_brings_no_further_round_and_holds_no_option_back(self):
        # "Yole" adds a syllable in two screening takes of eight, and the name as written in one. The count is
        # shown and decides nothing: neither is said again, and both are offered, because each clip says its
        # option the usual way.
        pronounce.add_option(SLUG, "Eole", "Yole")
        sounds = [("Yole", "jol")] * 8
        sounds[3] = sounds[6] = ("Yoley", "joli")
        written = [("Eole", "iol")] * 8
        written[3] = ("Eoley", "ioli")
        logs: list[str] = []
        with mock.patch.dict(SOUNDS, {"Yole": sounds, "Eole": written}):
            options = pronounce.render_options(SLUG, "Eole", log=logs.append)
        self.assertEqual([len(server.requests) for server in FakeServer.instances], [16])
        self.assertFalse([line for line in logs if "not offered" in line or "another seed" in line], logs)
        asis, typed = options["candidates"]
        self.assertEqual((asis["steady"], typed["steady"], typed["alike"], typed["typical"]),
                         ([7, 8], [6, 8], [6, 8], True))
        self.assertTrue(pronounce.offered(typed))
        self.assertFalse(pronounce.unsure(typed))

        # hold() still asks for a share of clean takes before it keeps an option's usual saying, and six of eight
        # is under it.
        self.assertFalse(pronounce.steady(typed))
        self.assertNotIn("said", pronounce.set_say_as(SLUG, "Eole", "Yole"))

    def test_a_clip_that_is_not_said_the_usual_way_is_said_again_from_another_seed(self):
        # The voice says "Ay-ole" as "eye-ole" in every take but the first one of the line AJ hears, which says
        # "ay-ole". No take adds a sound, so the old count was eight of eight with a clip that sounds right.
        eye, ay = ("Aiol", "aɪoʊl"), ("Aol", "eɪoʊl")
        pronounce.add_option(SLUG, "Eole", "Ay-ole")
        logs: list[str] = []
        written = [("Aiol", "aɪol")] + [("Eole", "iol")] * 15
        with mock.patch.dict(SOUNDS, {"Ay-ole": [eye] * 8 + [ay] + [eye] * 7, "Eole": written}):
            options = pronounce.render_options(SLUG, "Eole", log=logs.append)
        first, second = [[f for f, _ in server.requests] for server in FakeServer.instances]

        # The first round finds the clip is not how the option is usually said: seven takes of eight have the
        # usual vowels, by both recognizers. So a second round says the clip's line from another seed, and the
        # seven other lines again.
        self.assertTrue(any("'Ay-ole'" in line and "8 of 8" in line and "7 of them the usual way" in line
                            and "the clip is not one of those" in line for line in logs), logs)
        self.assertEqual((len(first), len(second)), (16, 8))
        self.assertTrue(all("Ay-ole" in f["text"] for f in second))
        self.assertEqual(second[0]["text"], first[8]["text"])
        self.assertNotEqual(second[0]["seed"], first[8]["seed"])
        seeds = [f["seed"] for f in first[9:]] + [f["seed"] for f in second[1:]]
        self.assertEqual((len(seeds), len(set(seeds))), (14, 14))
        # The name as written has a clip that is not usual too, and it is not said again: its count is not shown,
        # and it is offered whatever a screen says.
        self.assertEqual((options["candidates"][0]["typical"], options["candidates"][0]["clips"]), (False, 1))

        # The new clip says it the usual way, so the option is offered with a clip of "eye-ole" for AJ to hear.
        # The first clip counts as one of its sixteen takes.
        typed = options["candidates"][1]
        self.assertEqual((typed["steady"], typed["alike"], typed["typical"], typed["clips"], len(typed["sayings"])),
                         ([16, 16], [15, 16], True, 2, 16))
        self.assertEqual((typed["seed"], typed["heard"]), (second[0]["seed"], second[0]["text"].replace("Ay-ole", "Aiol")))
        self.assertTrue(pronounce.steady(typed))

        # A later screen of the option says the clip from the seed it has now.
        with mock.patch.dict(SOUNDS, {"Ay-ole": [eye]}):
            pronounce.render_options(SLUG, "Eole", log=lambda *_: None, missing=True, again="Ay-ole")
        third = [f for f, _ in FakeServer.instances[-1].requests]
        self.assertEqual((len(FakeServer.instances), third[0]["seed"]), (3, second[0]["seed"]))
        self.assertEqual(self.entry()["options"]["candidates"][1]["clips"], 2)

    def test_an_option_is_offered_by_its_clip_and_its_usual_saying_is_kept_by_its_count(self):
        # An option is offered when it was screened and its clip says it the usual way, whatever share of its
        # takes add a sound. An option without a count of this screen over eight takes or more is not offered.
        v = pronounce.SCREEN_V
        for count, typical, want in (([8, 8], True, True), ([3, 8], True, True), ([0, 8], True, True),
                                     ([8, 8], False, False), ([22, 22], False, False), ([3, 4], True, False),
                                     ([9, 8], True, False)):
            self.assertEqual(pronounce.offered({"screen": v, "steady": count, "typical": typical}), want, count)
        self.assertFalse(pronounce.offered({"steady": [8, 8], "typical": True}))
        self.assertFalse(pronounce.offered("Yole"))

        # Another round is for the clip alone: an option whose clip is not said the usual way gets one, whatever
        # its count, and an option whose clip is usual gets none.
        for count, typical, want in (([8, 8], False, True), ([4, 8], False, True), ([22, 22], False, True),
                                     ([7, 8], True, False), ([4, 8], True, False), ([3, 4], False, False)):
            self.assertEqual(pronounce.unsure({"screen": v, "steady": count, "typical": typical}), want, count)
        self.assertFalse(pronounce.unsure({"steady": [7, 8], "typical": False}))
        self.assertFalse(pronounce.unsure("Yole"))

        # hold() keeps an option's usual saying when it is more likely than not that STEADY_SHARE of its takes add
        # no sound. With every share as likely as another beforehand, a count of k in n leaves that chance at the
        # chance of k successes or fewer in n + 1 tries at STEADY_SHARE.
        share = pronounce.STEADY_SHARE
        self.assertEqual(share, 0.75)
        self.assertAlmostEqual(pronounce.likely(8, 8), 1 - share ** 9)
        self.assertAlmostEqual(pronounce.likely(0, 0), 1 - share)
        self.assertAlmostEqual(pronounce.likely(7, 8), 1 - share ** 9 - 9 * share ** 8 * (1 - share))
        # That is 7 of 8, 12 of 15 and 17 of 22. The second count doesn't enter it, and a count far over what a
        # screen makes gives a chance too, so the function doesn't raise on it.
        for count, want in (([7, 8], True), ([6, 8], False), ([12, 15], True), ([11, 15], False), ([17, 22], True),
                            ([16, 22], False), ([5000, 5000], True)):
            self.assertEqual(pronounce.steady({"screen": v, "steady": count, "typical": True}), want, count)
        self.assertTrue(pronounce.steady({"screen": v, "steady": [22, 22], "alike": [9, 22], "typical": True}))

    def test_a_screen_that_could_not_count_its_takes_still_counts_with_the_next(self):
        # No take of the spelling has a word in the name's place, so the screen gives no count. Its sayings are
        # kept all the same, with the version of the screen.
        pronounce.add_option(SLUG, "Eole", "Ay-ole")
        with mock.patch.dict(SOUNDS, {"Ay-ole": [("", "")]}):
            pronounce.render_options(SLUG, "Eole", log=lambda *_: None)
        first = FakeServer.instances[-1].requests
        typed = self.entry()["options"]["candidates"][1]
        self.assertEqual((typed.get("steady"), typed["sayings"], typed["screen"]), (None, [[]] * 8, pronounce.SCREEN_V))
        self.assertFalse(pronounce.steady(typed))

        # The next screen of it gets new seeds for the seven other lines, and counts with those eight takes.
        pronounce.render_options(SLUG, "Eole", log=lambda *_: None, missing=True, again="Ay-ole")
        second = FakeServer.instances[-1].requests
        self.assertFalse({f["seed"] for f, _ in second[1:]} & {f["seed"] for f, _ in first})
        self.assertEqual(self.entry()["options"]["candidates"][1]["steady"], [8, 15])

    def test_a_usual_saying_stays_until_its_spelling_is_screened_again_or_changed(self):
        self.answers.append({"options": [{"say": "Ay-ole", "why": "x"}]})
        pronounce.options_pass(SLUG, "Eole", log=lambda *_: None)
        pronounce.render_options(SLUG, "Eole", log=lambda *_: None)
        self.assertEqual(pronounce.set_say_as(SLUG, "Eole", "Ay-ole")["said"], "eɪoʊl")

        # New options without the spelling in use, a typed spelling and a changed note leave the usual saying.
        self.answers.append({"options": [{"say": "Yole", "why": "x"}]})
        pronounce.options_pass(SLUG, "Eole", log=lambda *_: None)
        self.assertEqual((self.entry()["say"], self.entry()["said"]), ("Ay-ole", "eɪoʊl"))
        pronounce.add_option(SLUG, "Eole", "Ayohl")
        pronounce.set_say_as(SLUG, "Eole", "Ay-ole", note="French.")
        self.assertEqual(self.entry()["said"], "eɪoʊl")

        # A screen of other spellings leaves it too, and so does a screen whose takes could not be heard.
        pronounce.render_options(SLUG, "Eole", log=lambda *_: None)
        self.assertEqual(self.entry()["said"], "eɪoʊl")
        pronounce.add_option(SLUG, "Eole", "Ay-ole")
        with mock.patch.object(asr, "records", mock.Mock(side_effect=ComfyError("Parakeet stopped"))):
            pronounce.render_options(SLUG, "Eole", log=lambda *_: None, missing=True)
        self.assertEqual(self.entry()["said"], "eɪoʊl")

        # A screen that finds half the takes of the spelling in use add a syllable removes the usual saying.
        with mock.patch.dict(SOUNDS, {"Ay-ole": [("Ay-ole", "eɪoʊl"), ("Ay-oley", "eɪoʊli")]}):
            pronounce.render_options(SLUG, "Eole", log=lambda *_: None)
        self.assertNotIn("said", self.entry())

    def test_a_term_with_one_line_is_screened_in_that_line_and_a_failed_take_leaves_no_screening_file(self):
        # The only line with the term is said eight times, the seven others without its direction.
        lib.save_json(lib.chapter_dir(SLUG, 1) / "script.json", {"lines": OPTIONS_SCRIPT[:1] + OPTIONS_SCRIPT[2:3]})
        pronounce.add_option(SLUG, "Eole", "Ay-ole")
        options = pronounce.render_options(SLUG, "Eole", log=lambda *_: None)
        texts = [f["text"] for f, _ in FakeServer.instances[-1].requests]
        self.assertEqual((len(texts), len(set(texts[:8]))), (16, 1))
        self.assertEqual([c["steady"] for c in options["candidates"]], [[8, 8], [8, 8]])

        # A take that fails part way stops the job, and the screening takes made before it are removed.
        FakeServer.fail_text = "The guard told Lewis that Ay-ole had not come back."
        before = {p.name for p in (pronounce.SAY_OUTPUT / SLUG).glob("*.wav")}
        with self.assertRaises(ComfyError):
            pronounce.render_options(SLUG, "Eole", log=lambda *_: None)
        left = {p.name for p in (pronounce.SAY_OUTPUT / SLUG).glob("*.wav")} - before
        self.assertFalse([name for name in left if "_s" in name], left)

    def test_an_option_is_steady_when_most_of_its_takes_add_no_sound(self):
        # Seven of eight is steady and six is not. The clip AJ hears has to be one of the takes that add none,
        # and fewer takes than a screen makes prove nothing.
        v = pronounce.SCREEN_V
        for count, typical, want in (([8, 8], True, True), ([7, 8], True, True), ([6, 8], True, False),
                                     ([7, 8], False, False), ([0, 0], True, False), ([4, 4], True, False),
                                     ([14, 16], True, True), ([13, 16], True, True), ([12, 16], True, False)):
            self.assertEqual(pronounce.steady({"screen": v, "steady": count, "typical": typical}), want, count)
        # A count from a screen of another version proves nothing either, and neither does a candidate without one.
        self.assertFalse(pronounce.steady({"steady": [8, 8], "typical": True}))
        self.assertFalse(pronounce.steady({"screen": v, "said": "eɪoʊl"}))

        # The takes of an option give how many of them add no sound, whether the first one is one of those, and
        # the option's usual saying.
        usual, odd = [["eɪoʊl", "deɪoʊls", ""]], [["eɪoʊli", "deɪoʊlis", "V@2"]]
        self.assertEqual(pronounce.screened([usual] * 7 + [odd]),
                         {"said": "eɪoʊl", "steady": [7, 8], "alike": [7, 8], "typical": True})
        self.assertEqual(pronounce.screened([odd] + [usual] * 7),
                         {"said": "eɪoʊl", "steady": [7, 8], "alike": [7, 8], "typical": False})
        # A take that says the name twice has to say it right both times. A take without a span is a take that
        # doesn't say it as spelled, so four good takes of eight are not four of four.
        self.assertEqual(pronounce.screened([usual] * 4 + [usual + odd, []])["steady"], [4, 6])
        self.assertEqual(pronounce.screened([usual] * 4 + [[]] * 4)["steady"], [4, 8])
        self.assertEqual(pronounce.screened([[]] + [usual] * 7)["typical"], False)
        self.assertEqual(pronounce.screened([usual, [], [], []]), {})
        # A saying that doesn't hold what was added, as an older screen left it, is not a clean one.
        self.assertEqual(pronounce.screened([[["eɪoʊl", "deɪoʊls"]]] * 8)["steady"], [0, 8])

    def test_the_other_lines_of_an_option_are_spread_over_the_lines_that_have_the_term(self):
        found = pronounce.lines_naming(SLUG, "Eole")
        others = pronounce.screening_lines(found, (1, 2), 7)
        # The line AJ hears and the line of one word are left out, and the seven come from across the rest.
        self.assertEqual(len(others), 7)
        self.assertNotIn(OPTIONS_SCRIPT[1], others)
        self.assertNotIn(OPTIONS_SCRIPT[2], others)
        self.assertEqual(len({line["text"] for line in others}), 7)
        # With fewer lines of a sayable length than takes they repeat. A term whose other lines are all too short
        # is said in those, and a term with no other line has none.
        self.assertEqual([line["text"] for line in pronounce.screening_lines(found[:3], (1, 2), 3)],
                         [SCREEN_SCRIPT[4]["text"]] * 3)
        self.assertEqual([line["text"] for line in pronounce.screening_lines(found[:2], (1, 2), 2)], ["Eole!"] * 2)
        self.assertEqual(pronounce.screening_lines(found[1:2], (1, 2), 7), [])

        # A line whose words on both sides of the term are new comes before one that shares a word with a line
        # already chosen, so a neighbour's sound doesn't pass for a part of the name.
        texts = ["Then Eole replied to all of them.", "Then Eole replied once more to them.",
                 "Soon Eole replied with a short laugh.", "The guard told Eole about the gate.",
                 "Nobody but Eole knew the way down.", "At last Eole turned and waved once."]
        beside = [(1, i, {"text": text}) for i, text in enumerate(texts)]
        self.assertEqual([line["text"] for line in pronounce.screening_lines(beside, (0, 0), 4, "Eole")],
                         [texts[0], texts[3], texts[4], texts[5]])
        self.assertEqual([line["text"] for line in pronounce.screening_lines(beside, (0, 0), 4)], texts[:4])

        # A later round leaves out the lines an earlier round used while enough others remain. With too few
        # others, the used lines are said again.
        first = pronounce.screening_lines(beside, (0, 0), 2, "Eole")
        second = pronounce.screening_lines(beside, (0, 0), 2, "Eole", first)
        self.assertEqual((len(second), [line for line in second if line in first]), (2, []))
        third = pronounce.screening_lines(beside, (0, 0), 5, "Eole", first)
        self.assertEqual(len(third), 5)
        self.assertTrue([line for line in third if line in first])

    def test_a_screening_line_is_said_in_its_own_speakers_voice(self):
        serial, bible = lib.load_serial(SLUG), lib.load_bible(SLUG)
        bible["characters"]["eole"]["voice_id"] = "eole"
        line = {"speaker": "eole", "text": "x"}
        # A speaker whose voice has no record is said by the fallback, and so is a speaker without a voice. The
        # narrator's lines get the narrator's voice.
        self.assertEqual(pronounce.line_voice(SLUG, serial, bible, line, "narrator"), "narrator")
        lib.save_yaml(lib.serial_dir(SLUG) / "voices" / "eole.yaml", {"clip": "eole.wav"})
        self.assertEqual(pronounce.line_voice(SLUG, serial, bible, line, "narrator"), "eole")
        self.assertEqual(pronounce.line_voice(SLUG, serial, bible, {"speaker": "guard", "text": "x"}, "eole"), "narrator")
        self.assertEqual(pronounce.line_voice(SLUG, serial, bible, {"speaker": "narrator", "text": "x"}, "eole"),
                         "narrator")


class HearSpellingTest(SayOptionsTest):
    """The options of a term that gets one spelling at a time, without a Claude pass."""

    test_the_pass_stores_the_respellings_beside_the_term_as_written = None
    test_each_option_is_said_in_its_line_by_the_lines_voice = None

    def test_a_typed_spelling_joins_the_options_and_only_new_options_are_said(self):
        # A term without options gets them: the term as written and the typed spelling, in the line a pass would
        # choose. No pass is asked.
        options = pronounce.add_option(SLUG, "Eole", " Ay-ohl ")
        self.assertEqual([c["say"] for c in options["candidates"]], ["Eole", "Ay-ohl"])
        self.assertEqual((options["line"]["index"], self.inputs, self.entry()["options"]), (2, [], options))

        # Both are said, because neither has a take yet.
        said = pronounce.render_options(SLUG, "Eole", log=lambda *_: None, missing=True)
        [server] = FakeServer.instances
        self.assertEqual([f["text"] for f, _ in server.requests],
                         ["The guard told Lewis that Eole had not come back.",
                          "The guard told Lewis that Ay-ohl had not come back."])
        self.assertEqual([c["heard"] for c in said["candidates"]], ["heard 0", "heard 1"])

        # A second spelling is said by itself, and the first two keep their clips. A spelling the options have
        # is not added again, and then nothing is said and no server starts.
        first = [c["clip"] for c in said["candidates"]]
        pronounce.add_option(SLUG, "Eole", "Yole")
        pronounce.add_option(SLUG, "Eole", " Yole ")
        said = pronounce.render_options(SLUG, "Eole", log=lambda *_: None, missing=True)
        self.assertEqual([c["say"] for c in said["candidates"]], ["Eole", "Ay-ohl", "Yole"])
        self.assertEqual([f["text"] for f, _ in FakeServer.instances[1].requests],
                         ["The guard told Lewis that Yole had not come back."])
        self.assertEqual(([c["clip"] for c in said["candidates"]][:2], Path(said["candidates"][2]["clip"]).is_file()),
                         (first, True))
        pronounce.render_options(SLUG, "Eole", log=lambda *_: None, missing=True)
        self.assertEqual(len(FakeServer.instances), 2)

        # Saying every option again takes all three, and the term still has no say-as.
        pronounce.render_options(SLUG, "Eole", log=lambda *_: None)
        self.assertEqual(len(FakeServer.instances[2].requests), 3)
        self.assertNotIn("say", self.entry())

        # A spelling without a letter and a term no line has are refused, and change nothing.
        for term, say in (("Eole", "123"), ("Eole", ""), ("Vouivre", "Voo-eevr")):
            with self.assertRaises(ValueError):
                pronounce.add_option(SLUG, term, say)
        self.assertEqual([c["say"] for c in self.entry()["options"]["candidates"]], ["Eole", "Ay-ohl", "Yole"])

    def test_a_spelling_in_another_case_is_an_option_of_its_own(self):
        # The bible writes stress in capitals, so the voice may read "ay-OHL" and "Ay-ohl" differently.
        pronounce.add_option(SLUG, "Eole", "Ay-ohl")
        options = pronounce.add_option(SLUG, "Eole", "ay-OHL")
        self.assertEqual([c["say"] for c in options["candidates"]], ["Eole", "Ay-ohl", "ay-OHL"])

    def test_every_option_is_said_again_when_its_clip_is_not_from_the_line_as_it_is_now(self):
        def hear(spelling):
            pronounce.add_option(SLUG, "Eole", spelling)
            said = pronounce.render_options(SLUG, "Eole", log=lambda *_: None, missing=True)
            return said, [f["text"] for f, _ in FakeServer.instances[-1].requests]

        said, texts = hear("Ay-ohl")
        self.assertEqual((len(texts), said["voice"]), (2, "narrator"))
        seed = said["candidates"][0]["seed"]

        # Another seed for the chapter's render: the two old clips don't stay beside the new spelling's.
        lib.save_chapter_meta(SLUG, 1, {"number": 1, "render": {"engine": "breeze", "seed": 6}})
        said, texts = hear("Yole")
        self.assertEqual(len(texts), 3)
        self.assertEqual(len({c["seed"] for c in said["candidates"]} - {seed}), 1)

        # Another say-as in the line: the same.
        pronounce.set_say_as(SLUG, "Louis", "Loo-ee")
        said, texts = hear("Ee-ole")
        self.assertEqual(texts, [f"The guard told Loo-ee that {c['say']} had not come back." for c in said["candidates"]])

        # Another voice for the line: the same, and the options name the voice that said them.
        lib.save_yaml(lib.serial_dir(SLUG) / "voices" / "other.yaml", {"clip": "other.wav", "sample_text": "t"})
        lib.save_serial(SLUG, {**lib.load_serial(SLUG), "narrator_voice_id": "other"})
        said, texts = hear("Ay-ole")
        self.assertEqual((len(texts), said["voice"]), (5, "other"))

        # With nothing changed, one more spelling is said by itself.
        said, texts = hear("Yoll")
        self.assertEqual(texts, ["The guard told Loo-ee that Yoll had not come back."])

        # The script's line is rewritten. The options can't be said in a line that is gone, so the next spelling
        # moves them to the line as it is now. They keep every spelling, and each is said in that line.
        script = [dict(line) for line in OPTIONS_SCRIPT]
        script[2]["text"] = "The guard said that Eole was still away."
        lib.save_json(lib.chapter_dir(SLUG, 1) / "script.json", {"lines": script})
        with self.assertRaises(ValueError):
            pronounce.render_options(SLUG, "Eole", log=lambda *_: None, missing=True)
        said, texts = hear("Ay-oll")
        self.assertEqual([c["say"] for c in said["candidates"]],
                         ["Eole", "Ay-ohl", "Yole", "Ee-ole", "Ay-ole", "Yoll", "Ay-oll"])
        self.assertEqual(said["line"]["text"], script[2]["text"])
        self.assertEqual(texts, [f"The guard said that {c['say']} was still away." for c in said["candidates"]])
        self.assertEqual([c["why"] for c in said["candidates"]][:2], ["as written", "typed on the Names tab"])


class BioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.llm = FakeLLM()
        for p in (mock.patch.object(lib, "LIBRARY", Path(self.tmp.name) / "library"),
                  mock.patch.object(casting, "run_pass", self.llm)):
            p.start()
            self.addCleanup(p.stop)
        lib.save_serial(SLUG, {"title": "Design Test"})
        character = {"gender": "female", "age_group": "adult", "importance": "major", "role": "x",
                     "description": "x", "voice_brief": "x", "facts": [], "voice_id": ""}
        lib.save_bible(SLUG, {"characters": {
            "meredith": {"name": "Meredith", **character, "voice_id": "meredith",
                         "facts": [{"chapter": 3, "text": "She is an elf."}]},
            "ann": {"name": "Ann", **character, "voice_id": "ann", "bio": "An old bio."},
            "tom": {"name": "Tom", **character}}, "pronunciations": {}})

    def write(self, **kwargs):
        return casting.write_bios(SLUG, log=lambda *_: None, **kwargs)

    def bios(self):
        return {cid: c.get("bio") for cid, c in lib.load_bible(SLUG)["characters"].items()}

    def test_bios_go_to_the_voiced_characters_without_one(self):
        # Default: only the voiced character with no bio is sent, with its facts, and the bible gets its bio.
        self.assertEqual(self.write(), {"meredith": "Bio 1 of meredith."})
        [(kind, ids, text)] = self.llm.calls
        self.assertEqual((kind, ids), ("bios", ["meredith"]))
        self.assertIn("- chapter 3: She is an elf.", text)
        self.assertEqual(self.bios(), {"meredith": "Bio 1 of meredith.", "ann": "An old bio.", "tom": None})

        # With every voiced character written, a second run doesn't call the LLM.
        self.assertEqual(self.write(), {})
        self.assertEqual(len(self.llm.calls), 1)

        # Named ids with redo: a bio is written again, an uncast character gets one, and an id that is not in the
        # bible is skipped.
        self.assertEqual(self.write(only=["ann", "tom", "narrator"], redo=True),
                         {"ann": "Bio 2 of ann.", "tom": "Bio 2 of tom."})
        self.assertEqual(self.bios(), {"meredith": "Bio 1 of meredith.", "ann": "Bio 2 of ann.", "tom": "Bio 2 of tom."})

    def test_many_bios_go_out_in_batches(self):
        with mock.patch.object(casting, "BIO_BATCH", 2):
            written = self.write(only=["meredith", "ann", "tom"], redo=True)
        self.assertEqual([ids for _, ids, _ in self.llm.calls], [["meredith", "ann"], ["tom"]])
        self.assertEqual(written, {"meredith": "Bio 1 of meredith.", "ann": "Bio 1 of ann.", "tom": "Bio 2 of tom."})

    def test_a_pass_that_leaves_a_character_out_does_not_save(self):
        self.llm.drop = {"ann"}
        with self.assertRaises(LLMError) as e:
            self.write(only=["meredith", "ann"], redo=True)
        self.assertIn("didn't return a bio for ann", str(e.exception))
        self.assertEqual(self.bios(), {"meredith": None, "ann": "An old bio.", "tom": None})


if __name__ == "__main__":
    unittest.main()
