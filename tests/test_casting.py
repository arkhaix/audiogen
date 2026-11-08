"""Tests for the Breeze voice design path of casting, with a fake breeze-server and a fake recognizer."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serial_audio import asr, breeze, casting
from serial_audio import library as lib
from serial_audio.comfy import ComfyError

SLUG = "design-test"
SAMPLE = "I have waited a hundred and fifty years for this."
BRIEF = {"id": "meredith", "instruct": "A woman in her late twenties with a low alto voice.", "sample_text": SAMPLE}


class FakeServer:
    """breeze-server that returns a second of silence per request and records what it was sent."""

    instances: list["FakeServer"] = []
    fail_text = None

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
        return b"\x00\x00" * self.sample_rate


def heard_for(good_seed: int):
    """A fake recognizer that hears the sample line in the take of good_seed and a loop in every other take."""
    def transcripts(files, *, engine, comfy_url):
        for i, f in enumerate(files):
            yield i, ("I have waited 150 years for this." if f"_s{good_seed}" in Path(f).name else "This this this.")
    return transcripts


class BreezeCastTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        patches = [mock.patch.object(lib, "LIBRARY", root / "library"),
                   mock.patch.object(casting, "BREEZE_CAST_OUTPUT", root / "output" / "breeze" / "cast"),
                   mock.patch.object(breeze, "CppServer", FakeServer),
                   mock.patch.object(breeze, "wait_comfy_idle", lambda *a, **k: None),
                   mock.patch.object(breeze, "free_comfy", lambda *a, **k: None),
                   mock.patch.object(asr, "check_ready", lambda *a, **k: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        FakeServer.instances.clear()
        FakeServer.fail_text = None
        lib.save_serial(SLUG, {"title": "Design Test", "narration": {"point_of_view": "third_person"},
                               "narrator_voice_id": "narrator"})
        character = {"gender": "female", "age_group": "young adult", "importance": "major", "role": "x",
                     "description": "x", "voice_brief": "x", "facts": [], "voice_id": ""}
        lib.save_bible(SLUG, {"characters": {"meredith": {"name": "Meredith", **character},
                                             "ann": {"name": "Ann", **character}}, "pronunciations": {}})
        lib.save_yaml(lib.serial_dir(SLUG) / "voices" / "_briefs.yaml",
                      {"meredith": BRIEF, "ann": {"id": "ann", "instruct": "A low alto.", "sample_text": "Ann speaks."}})

    def cast(self, good_seed: int, seed0: int = 1000, **kwargs):
        with mock.patch.object(asr, "transcripts", heard_for(good_seed)):
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

    def test_a_failed_design_files_the_voices_before_it(self):
        # Design: ann's design fails after meredith's three succeed, and the server still stops.
        FakeServer.fail_text = "Ann speaks."
        with mock.patch.object(asr, "transcripts", heard_for(1001)), self.assertRaises(ComfyError) as e:
            casting.cast(SLUG, only=["meredith", "ann"], candidates=3, engine="breeze", asr="parakeet",
                         log=lambda *_: None)
        self.assertIn("designing ann seed 1000 failed", str(e.exception))
        self.assertFalse(FakeServer.instances[0].running)

        # File: meredith is filed before the error is raised, and ann stays uncast for a rerun.
        chars = lib.load_bible(SLUG)["characters"]
        self.assertEqual((chars["meredith"]["voice_id"], chars["ann"]["voice_id"]), ("meredith", ""))
        self.assertTrue((lib.serial_dir(SLUG) / "voices" / "meredith.yaml").is_file())
        self.assertFalse((lib.serial_dir(SLUG) / "voices" / "ann.yaml").exists())

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


if __name__ == "__main__":
    unittest.main()
