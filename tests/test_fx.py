"""Tests for the click repair, edge fades and voice leveling in fx.py and breeze_join.py. They need the audio Python, which has numpy,
scipy, librosa and soundfile, and are skipped elsewhere. Run: <audio python> -I tests/test_fx.py"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "serial_audio"))
try:
    import numpy as np
    import soundfile as sf

    import breeze_join
    import fx
except ImportError:
    np = None

SR = 24000


def voice(seconds: float, seed: int = 1) -> "np.ndarray":
    """A steady voiced sound at about -29 dB RMS with a little noise, as float32."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    x = sum(np.sin(2 * np.pi * 140 * k * t + k) / k for k in range(1, 16)) * 0.04
    return (x + rng.normal(0, 0.001, t.size)).astype(np.float32)


@unittest.skipIf(np is None, "needs the audio Python")
class ClickTest(unittest.TestCase):
    def test_a_clean_voice_has_no_clicks(self):
        self.assertEqual(fx.find_clicks(voice(2.0), SR), [])
        x = voice(2.0)
        y, count = fx.declick(x, SR)
        self.assertEqual(count, 0)
        self.assertIs(y, x)

    def test_a_click_in_speech_is_found_and_filled(self):
        clean = voice(2.0)
        x = clean.copy()
        x[24000:24003] += np.array([0.4, -0.3, 0.2], dtype=np.float32)
        found = fx.find_clicks(x, SR)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0][0] <= 24000 and found[0][1] >= 24003)
        self.assertLess(found[0][1] - found[0][0], 4 * 24)
        y, count = fx.declick(x, SR)
        self.assertEqual(count, 1)
        self.assertLess(np.abs(y[23900:24100] - clean[23900:24100]).max(), 0.1)
        self.assertEqual(fx.find_clicks(y, SR), [])
        self.assertEqual(np.abs(y[:23900] - x[:23900]).max(), 0.0)

    def test_two_clicks_10_ms_apart_are_both_filled(self):
        clean = voice(2.0)
        x = clean.copy()
        for at in (24000, 24240):
            x[at:at + 3] += np.array([0.4, -0.3, 0.2], dtype=np.float32)
        y, count = fx.declick(x, SR)
        self.assertEqual(count, 2)
        self.assertLess(np.abs(y[23900:24400] - clean[23900:24400]).max(), 0.1)

    def test_a_rate_too_low_for_the_band_finds_nothing(self):
        x = np.zeros(8000, dtype=np.float32)
        x[4000] = 0.5
        self.assertEqual(fx.find_clicks(x, 8000), [])

    def test_a_quiet_click_in_a_pause_is_found(self):
        x = np.random.default_rng(2).normal(0, 0.0002, SR).astype(np.float32)
        x[12000] += 0.05
        self.assertEqual(len(fx.find_clicks(x, SR)), 1)

    def test_a_consonant_length_burst_is_not_a_click(self):
        x = voice(2.0)
        burst = np.random.default_rng(3).normal(0, 0.05, 240).astype(np.float32)
        x[24000:24240] += burst * np.hanning(240).astype(np.float32)
        self.assertEqual(fx.find_clicks(x, SR), [])


@unittest.skipIf(np is None, "needs the audio Python")
class FillAndFadeTest(unittest.TestCase):
    def test_fill_gap_rebuilds_a_tone(self):
        t = np.arange(4800) / SR
        clean = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        x = clean.copy()
        x[2400:2448] = 0.9
        fx.fill_gap(x, 2400, 2448, SR)
        self.assertLess(np.abs(x[2400:2448] - clean[2400:2448]).max(), 1e-3)

    def test_fill_gap_leaves_a_gap_without_context(self):
        x = voice(0.1)
        before = x.copy()
        fx.fill_gap(x, 10, 58, SR)
        self.assertEqual(np.abs(x - before).max(), 0.0)

    def test_fade_edges_starts_and_ends_at_zero(self):
        x = np.full(2400, 0.3, dtype=np.float32)
        y = fx.fade_edges(x, SR)
        self.assertEqual(y[0], 0.0)
        self.assertEqual(y[-1], 0.0)
        n = int(fx.EDGE_FADE * SR)
        self.assertTrue(np.all(y[n:-n] == x[n:-n]))
        self.assertEqual(x[0], np.float32(0.3))


@unittest.skipIf(np is None, "needs the audio Python")
class JoinTest(unittest.TestCase):
    def test_join_fills_clicks_and_fades_each_take(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A take with a click, and a take whose first sample is already loud
            a = voice(1.0, seed=4)
            a[12000:12003] += np.array([0.4, -0.3, 0.2], dtype=np.float32)
            b = voice(1.0, seed=5)
            files = []
            for i, take in enumerate((a, b)):
                files.append(str(Path(tmp) / f"l{i:04d}.wav"))
                sf.write(files[-1], take, SR)
            job = {"output": str(Path(tmp) / "out.wav"), "cap_seconds": 120,
                   "takes": [{"line": i, "file": f, "effect": None} for i, f in enumerate(files)]}

            # The summary counts the click, and the joined audio starts at zero
            summary = breeze_join.join(json.loads(json.dumps(job)))
            self.assertEqual(summary["clicks"], 1)
            out, sr = sf.read(job["output"], dtype="float32")
            self.assertEqual(sr, SR)
            self.assertLess(abs(out[0]), 1e-3)

            # The second take comes out of the gap near zero, where unfaded it would step to about 0.16
            silent = np.abs(out) < 1e-6
            ends = [i for i in range(1000, len(out)) if silent[i - 1] and not silent[i] and silent[i - 1000:i].all()]
            self.assertEqual(len(ends), 1)
            self.assertLess(abs(out[ends[0]]), 0.01)

    def test_join_levels_each_voice_and_keeps_its_dynamics(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A loud voice with one line 6 dB over its median, then a quiet voice 12 dB below that median
            scales = [("loud", 4.0), ("loud", 2.0), ("loud", 2.0), ("quiet", 0.5), ("quiet", 0.5), ("quiet", 0.5)]
            takes = []
            for i, (name, scale) in enumerate(scales):
                path = str(Path(tmp) / f"l{i:04d}.wav")
                sf.write(path, voice(1.0, seed=10 + i) * np.float32(scale), SR)
                takes.append({"line": i, "file": path, "voice": name, "effect": None})
            job = {"output": str(Path(tmp) / "out.wav"), "cap_seconds": 120, "takes": takes}

            # The summary gives the quiet voice 12 dB more gain than the loud one
            summary = breeze_join.join(json.loads(json.dumps(job)))
            gains = summary["voice_gains"]
            self.assertAlmostEqual(gains["quiet"] - gains["loud"], 12.0, delta=0.3)

            # Both voices' median lines come out at one level, and the loud voice's loud line stays 6 dB over them
            out, sr = sf.read(job["output"], dtype="float32")
            sound = np.nonzero(np.abs(out) > 1e-6)[0]
            starts = [sound[0]] + [b for a, b in zip(sound, sound[1:]) if b - a > 1000]
            self.assertEqual(len(starts), 6)
            db = [20 * np.log10(np.sqrt((out[s + SR // 4: s + 3 * SR // 4] ** 2).mean())) for s in starts]
            for i in (2, 3, 4, 5):
                self.assertAlmostEqual(db[i], db[1], delta=0.5)
            self.assertAlmostEqual(db[0] - db[1], 6.0, delta=0.5)

    def test_a_voice_gain_does_not_change_which_pauses_are_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A loud voice whose 2 s soft line is above the silence threshold before its -7 dB gain and below it after
            takes = []
            for i, (seconds, scale) in enumerate(((1.0, 8.0), (2.0, 0.1), (1.0, 8.0), (1.0, 8.0))):
                path = str(Path(tmp) / f"l{i:04d}.wav")
                sf.write(path, voice(seconds, seed=20 + i) * np.float32(scale), SR, subtype="FLOAT")
                takes.append({"line": i, "file": path, "voice": "loud", "effect": None})
            job = {"output": str(Path(tmp) / "out.wav"), "cap_seconds": 120, "takes": takes}
            soft, _ = sf.read(takes[1]["file"], dtype="float32")
            self.assertGreater(np.sqrt((soft ** 2).mean()), fx.SILENCE_RMS)
            summary = breeze_join.join(json.loads(json.dumps(job)))
            self.assertLess(summary["voice_gains"]["loud"], -5.0)
            self.assertLess(np.sqrt((soft ** 2).mean()) * 10 ** (summary["voice_gains"]["loud"] / 20), fx.SILENCE_RMS)

            # The soft line is kept whole: 5 s of takes and three 0.3 s gaps
            self.assertAlmostEqual(summary["seconds"], 5.9, delta=0.1)


if __name__ == "__main__":
    unittest.main()
