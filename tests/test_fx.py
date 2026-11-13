"""Tests for the click repair and edge fades in fx.py, the leveling, cuts, compressor, limiter and delivery of the
join in breeze_join.py, and the take and chapter measures in take_measure.py. They need the audio Python, which has
numpy, scipy, librosa and soundfile, and are skipped elsewhere. Run: <audio python> -I tests/test_fx.py"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "serial_audio"))
try:
    import numpy as np
    import soundfile as sf

    import breeze_join
    import fx
    import take_measure as tm
except ImportError:
    np = None

SR = 24000
# the take check's model folders, found the way machine.py finds them
import machine  # noqa: E402

MODELS = {"vad": machine.VAD_MODEL, "speaker": machine.SPEAKER_MODEL}


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

            # Both voices' median lines come out at one level. The loud voice's loud line has a second of speech,
            # so it comes two thirds of its 6 dB toward them and ends 2 dB over.
            def levels() -> list[float]:
                out, sr = sf.read(job["output"], dtype="float32")
                sound = np.nonzero(np.abs(out) > 1e-6)[0]
                starts = [sound[0]] + [b for a, b in zip(sound, sound[1:]) if b - a > 1000]
                self.assertEqual(len(starts), 6)
                return [20 * np.log10(np.sqrt((out[s + SR // 4: s + 3 * SR // 4] ** 2).mean())) for s in starts]

            db = levels()
            for i in (2, 3, 4, 5):
                self.assertAlmostEqual(db[i], db[1], delta=0.5)
            self.assertAlmostEqual(db[0] - db[1], 2.0, delta=0.5)

            # A line keeps its dynamics when its offset says it belongs there: 6 dB over stays 6 dB over.
            job["takes"][0]["offset"] = 6.0
            breeze_join.join(json.loads(json.dumps(job)))
            db = levels()
            self.assertAlmostEqual(db[0] - db[1], 6.0, delta=0.5)
            self.assertAlmostEqual(db[2], db[1], delta=0.5)

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


    def test_the_join_cuts_the_spans_a_take_names_and_leaves_each_takes_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A take with 0.7 s of silence in its middle, and two more takes
            paused = np.concatenate([voice(1.0, 30), np.zeros(int(0.7 * SR), dtype=np.float32), voice(1.0, 31)])
            takes = []
            for i, x in enumerate((paused, voice(1.0, 32), voice(1.0, 33))):
                path = str(Path(tmp) / f"l{i:04d}.wav")
                sf.write(path, x, SR, subtype="FLOAT")
                takes.append({"line": i, "file": path, "voice": "v", "effect": None})
            job = {"output": str(Path(tmp) / "out.wav"), "cap_seconds": 120, "takes": takes}

            # Without a cut the pause stays, and each take is followed by the old gap of 0.3 s.
            summary = breeze_join.join(json.loads(json.dumps(job)))
            self.assertEqual((summary["cuts"], summary["cut_seconds"]), (0, 0.0))
            self.assertAlmostEqual(summary["seconds"], 2.7 + 1.0 + 1.0 + 2 * 0.3, delta=0.06)

            # The cut takes 0.4 s out of the pause, and the gaps are the ones the takes name.
            takes[0].update(cuts=[[1.15, 1.55]], gap=0.15)
            takes[1].update(gap=0.5)
            takes[2].update(gap=9.0)
            summary = breeze_join.join(json.loads(json.dumps(job)))
            self.assertEqual((summary["cuts"], summary["cut_seconds"]), (1, 0.4))
            self.assertAlmostEqual(summary["seconds"], 2.3 + 1.0 + 1.0 + 0.15 + 0.5, delta=0.06)
            self.assertAlmostEqual(summary["raw_seconds"], 2.7 + 1.0 + 1.0 + 0.15 + 0.5, delta=0.01)

            # A gap, an offset or cuts that aren't finite numbers count as the old gap, no offset and no cut.
            takes[0].update(cuts=5, gap=float("inf"), offset=float("nan"))
            takes[1].update(cuts=[[float("nan"), 0.5]], gap=-1, offset="loud")
            summary = breeze_join.join(json.loads(json.dumps(job)))
            self.assertEqual((summary["cuts"], summary["loudness"]), (0, breeze_join.TARGET_LUFS))
            self.assertAlmostEqual(summary["seconds"], 2.7 + 1.0 + 1.0 + 2 * 0.3, delta=0.06)

    def test_the_chapter_comes_out_at_the_target_loudness_under_the_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Ten quiet takes of one voice, one of them with a twentieth of a second 20 dB over its level
            takes = []
            for i in range(10):
                x = voice(2.0, seed=40 + i)
                if i == 4:
                    x[SR: SR + SR // 20] *= np.float32(10.0)
                path = str(Path(tmp) / f"l{i:04d}.wav")
                sf.write(path, x, SR, subtype="FLOAT")
                takes.append({"line": i, "file": path, "voice": "v", "effect": None})

            # The file plays at -18 LUFS with its true peak under -1 dB, as Opus and as a WAV, and the summary
            # reports what the file measures.
            for name in ("out.opus", "out.wav"):
                job = {"output": str(Path(tmp) / name), "cap_seconds": 120, "takes": takes}
                summary = breeze_join.join(json.loads(json.dumps(job)))
                out, sr = sf.read(job["output"], dtype="float32", always_2d=True)
                self.assertAlmostEqual(tm.loudness(out.mean(1), sr), breeze_join.TARGET_LUFS, delta=0.15, msg=name)
                self.assertLessEqual(tm.true_peak(out.mean(1), sr), breeze_join.CEILING_DB + 0.05, name)
                self.assertAlmostEqual(summary["loudness"], tm.loudness(out.mean(1), sr), delta=0.06, msg=name)
                self.assertAlmostEqual(summary["true_peak"], tm.true_peak(out.mean(1), sr), delta=0.06, msg=name)
                self.assertGreater(summary["limited"], 3.0, name)


@unittest.skipIf(np is None, "needs the audio Python")
class MasterTest(unittest.TestCase):
    def test_a_cut_takes_its_span_out_and_fades_across_it(self):
        x = np.arange(SR, dtype=np.float32) / SR
        fade = int(breeze_join.CUT_FADE * SR)

        # The half second from 0.25 s is gone. The samples before the fade and after the cut are the old ones.
        y, count = breeze_join.cut(x, SR, [[0.25, 0.75]])
        self.assertEqual((count, len(y)), (1, SR // 2))
        self.assertTrue(np.array_equal(y[: SR // 4 - fade], x[: SR // 4 - fade]))
        self.assertTrue(np.array_equal(y[SR // 4:], x[3 * SR // 4:]))
        mixed = y[SR // 4 - fade: SR // 4]
        self.assertTrue(np.all((mixed >= x[SR // 4 - fade]) & (mixed <= x[3 * SR // 4])))

        # Two cuts are both made, in order, whatever order they come in.
        y, count = breeze_join.cut(x, SR, [[0.6, 0.7], [0.2, 0.3]])
        self.assertEqual((count, len(y)), (2, SR - SR // 5))

        # A cut that reaches outside the audio, runs backwards, overlaps the cut before it or isn't two numbers is
        # left out, and audio without a cut comes back as it is.
        for cuts in ([[0.9, 1.5]], [[0.5, 0.4]], [[0.5]], [["a", "b"]], [None], [], 5, None, [[float("nan"), 0.5]],
                     [[0.2, float("inf")]], [[True, 0.5]]):
            y, count = breeze_join.cut(x, SR, cuts)
            self.assertIs(y, x, cuts)
            self.assertEqual(count, 0, cuts)
        y, count = breeze_join.cut(x, SR, [[0.2, 0.4], [0.3, 0.5]])
        self.assertEqual((count, len(y)), (1, SR - SR // 5))

        # A cut that ends less than CUT_FADE from the end of the audio, before or past it, ends the audio there with
        # a fade out, and the samples before the fade are the old ones.
        for end in (1.0, 1.0 + breeze_join.CUT_FADE / 2, 1.0 - breeze_join.CUT_FADE / 2):
            y, count = breeze_join.cut(x, SR, [[0.2, 0.4], [0.8, end]])
            self.assertEqual((count, len(y)), (2, SR * 3 // 5), end)
            self.assertTrue(np.array_equal(y[-SR // 5: -fade], x[3 * SR // 5: 4 * SR // 5 - fade]))
            self.assertLess(abs(float(y[-1])), 1e-3)

    def test_each_take_gets_its_voices_gain_and_a_bounded_correction(self):
        t = breeze_join.TARGET_LUFS
        measured = [("a", -30.0, 0.0, 3.0),    # the voice's ordinary take
                    ("a", -30.0, 0.0, 3.0),
                    ("a", -30.0, 0.0, 3.0),
                    ("a", -24.0, 0.0, 3.0),    # 6 dB over
                    ("a", -36.0, -6.0, 3.0),   # a whisper, 6 dB under as its offset says
                    ("a", -24.0, 0.0, 0.75),   # 6 dB over with half the speech a full correction needs
                    ("a", -10.0, 0.0, 3.0),    # 20 dB over
                    ("a", None, 0.0, 0.0),     # no speech
                    (None, -30.0, 0.0, 3.0),   # no voice
                    ("b", -12.0, 3.0, 3.0)]    # the only take of a voice, a shout
        gains, voices = breeze_join.take_gains(measured)
        self.assertEqual(voices, {"a": t + 30.0, "b": t + 15.0})
        self.assertEqual(gains, [t + 30.0, t + 30.0, t + 30.0, t + 24.0, t + 30.0, t + 27.0, t + 24.0, 0.0, 0.0,
                                 t + 15.0])

        # The whisper ends 6 dB under the target and the shout 3 dB over it.
        self.assertEqual((measured[4][1] + gains[4], measured[9][1] + gains[9]), (t - 6.0, t + 3.0))

    def test_the_compressor_turns_down_only_what_is_far_over_the_normal_level(self):
        normal = breeze_join.TARGET_LUFS
        peak = 10 ** ((normal + 3.01) / 20)

        # Speech at the normal level, and 3 dB over it, comes back as it is.
        for over in (0.0, 3.0):
            x = sine(6.0, SR, peak=peak * 10 ** (over / 20))
            y, most = breeze_join.compress(x, SR)
            self.assertIs(y, x, over)
            self.assertEqual(most, 0.0, over)

        # Two seconds 9 dB over the normal level are 3 dB over the threshold, and they come down by two thirds
        # of that. The audio a second away from them is unchanged.
        x = sine(8.0, SR, peak=peak)
        x[3 * SR: 5 * SR] *= np.float32(10 ** (9 / 20))
        y, most = breeze_join.compress(x, SR)
        self.assertAlmostEqual(most, 2.0, delta=0.15)
        middle = slice(int(3.8 * SR), int(4.2 * SR))
        self.assertAlmostEqual(20 * np.log10(np.abs(y[middle]).max() / np.abs(x[middle]).max()), -2.0, delta=0.15)
        self.assertTrue(np.allclose(y[: 2 * SR], x[: 2 * SR], atol=1e-7))
        self.assertTrue(np.allclose(y[6 * SR:], x[6 * SR:], atol=1e-7))

    def test_the_limiter_holds_the_true_peak_under_the_ceiling(self):
        ceiling = 10 ** (breeze_join.CEILING_DB / 20)

        # Audio under the ceiling comes back as it is.
        quiet = sine(1.0, SR, peak=0.5)
        y, most = breeze_join.limit(quiet, SR)
        self.assertIs(y, quiet)
        self.assertEqual(most, 0.0)

        # A burst at 1.5 comes down by its 4.5 dB over the ceiling and the margin. The audio before the gain comes
        # down, and after it is back, is unchanged.
        x = sine(2.0, SR, hz=220.0, peak=0.5)
        x[SR: SR + 240] *= np.float32(3.0)
        y, most = breeze_join.limit(x, SR)
        self.assertAlmostEqual(most, 20 * np.log10(1.5 / ceiling) + breeze_join.LIMIT_MARGIN, delta=0.2)
        self.assertLessEqual(tm.true_peak(y, SR), breeze_join.CEILING_DB + 1e-6)
        ahead, hold = int(breeze_join.LOOKAHEAD * SR), int(breeze_join.RELEASE * SR)
        self.assertTrue(np.array_equal(y[: SR - 2 * ahead - 2], x[: SR - 2 * ahead - 2]))
        self.assertTrue(np.array_equal(y[SR + 240 + hold + 2 * ahead + 2:], x[SR + 240 + hold + 2 * ahead + 2:]))
        self.assertLess(np.abs(y[SR + 240 + hold // 2]), np.abs(x[SR + 240 + hold // 2]) + 1e-9)

        # A peak between the samples counts: a sine at a quarter of the rate, 45 degrees off the samples, has
        # sample peaks 3 dB under its true peak, and it is limited on the true one.
        between = (np.sin(2 * np.pi * np.arange(SR) / 4 + np.pi / 4)).astype(np.float32)
        self.assertLess(np.abs(between).max(), ceiling)
        y, most = breeze_join.limit(between, SR)
        self.assertGreater(most, 0.9)
        self.assertLessEqual(tm.true_peak(y, SR), breeze_join.CEILING_DB + 1e-6)

    def test_a_clip_is_leveled_in_place_and_keeps_its_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A quiet 16-bit WAV and a FLAC with a twentieth of a second 20 dB over its level
            quiet, peaky = Path(tmp) / "quiet.wav", Path(tmp) / "peaky.flac"
            sf.write(str(quiet), voice(4.0, 70), SR, subtype="PCM_16")
            burst = voice(4.0, 71)
            burst[SR: SR + SR // 20] *= np.float32(10.0)
            sf.write(str(peaky), burst, SR, subtype="PCM_16")

            # Both come out at the target with their true peak under the ceiling, in the format they had.
            for path, kind in ((quiet, "WAV"), (peaky, "FLAC")):
                before = tm.loudness(sf.read(str(path), dtype="float32")[0], SR)
                got = breeze_join.level_file(path)
                back, sr = sf.read(str(path), dtype="float32")
                info = sf.info(str(path))
                self.assertEqual((info.format, info.subtype, sr, len(back)), (kind, "PCM_16", SR, 4 * SR))
                self.assertAlmostEqual(tm.loudness(back, sr), breeze_join.TARGET_LUFS, delta=0.1, msg=kind)
                self.assertLessEqual(tm.true_peak(back, sr), breeze_join.CEILING_DB + 0.05, kind)
                self.assertAlmostEqual(got["before"], before, delta=0.01, msg=kind)
                self.assertAlmostEqual(got["after"], tm.loudness(back, sr), delta=0.05, msg=kind)

            # A silent clip and a stereo clip are left as they are.
            silent, stereo = Path(tmp) / "silent.wav", Path(tmp) / "stereo.wav"
            sf.write(str(silent), np.zeros(SR, dtype=np.float32), SR, subtype="PCM_16")
            sf.write(str(stereo), np.stack([voice(2.0, 72), voice(2.0, 73)], axis=1), SR, subtype="PCM_16")
            for path in (silent, stereo):
                was = path.read_bytes()
                got = breeze_join.level_file(path)
                self.assertEqual(path.read_bytes(), was, path.name)
                self.assertEqual(got["after"], got["before"], path.name)

    def test_an_opus_file_is_written_at_the_bitrate_asked_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            x = np.concatenate([voice(1.0, seed=50 + i) for i in range(20)])
            sizes = {}
            for kbps in (24, 48, 96):
                path = Path(tmp) / f"{kbps}.opus"
                fx.write_audio(str(path), x, SR, kbps=kbps)
                sizes[kbps] = path.stat().st_size * 8 / 20 / 1000
                back, sr = sf.read(str(path), dtype="float32")
                self.assertEqual((sr, len(back)), (SR, len(x)))
            for kbps, got in sizes.items():
                self.assertAlmostEqual(got, kbps, delta=0.25 * kbps, msg=sizes)

            # Without a bitrate the encoder picks its own, and another format doesn't use the argument.
            fx.write_audio(str(Path(tmp) / "own.opus"), x, SR)
            fx.write_audio(str(Path(tmp) / "plain.wav"), x, SR, kbps=24)
            back, sr = sf.read(str(Path(tmp) / "plain.wav"), dtype="float32")
            self.assertEqual((sr, len(back)), (SR, len(x)))


def sine(seconds: float, sr: int, hz: float = 997.0, peak: float = 1.0) -> "np.ndarray":
    return (peak * np.sin(2 * np.pi * hz * np.arange(int(seconds * sr)) / sr)).astype(np.float32)


def hush(seconds: float) -> "np.ndarray":
    """Room noise 70 dB under full scale, far below the voice() level."""
    return np.random.default_rng(9).normal(0, 0.0003, int(seconds * SR)).astype(np.float32)


@unittest.skipIf(np is None, "needs the audio Python")
class ChildTest(unittest.TestCase):
    def run_child(self, job: dict) -> tuple[list[dict], str]:
        import subprocess
        script = Path(__file__).resolve().parents[1] / "serial_audio" / "parakeet_asr.py"
        done = subprocess.run([sys.executable, "-I", str(script)], input=json.dumps(job), capture_output=True,
                              text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr)
        return [json.loads(line) for line in done.stdout.splitlines()], done.stderr

    def test_a_model_that_does_not_load_leaves_only_its_block_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            take = Path(tmp) / "take.wav"
            sf.write(take, voice(1.0), SR)
            broken = Path(tmp) / "vad"
            broken.mkdir()
            (broken / "silero_vad_v5.onnx").write_bytes(b"not a model")

            # The signal block is still measured, and the child names the block it left out.
            out, err = self.run_child({"model": str(Path(tmp) / "none"), "files": [str(take)],
                                       "blocks": ["signal", "vad"], "vad": str(broken)})
            self.assertEqual(sorted(out[0]), ["index", "signal"])
            self.assertIn("the vad block is left out", err)

            # An empty list of blocks gathers nothing, and no Parakeet folder is needed for it.
            out, _ = self.run_child({"model": str(Path(tmp) / "none"), "files": [str(take)], "blocks": []})
            self.assertEqual(out[0], {"index": 0})

            # A check job loads the models and hears no file. It names the block that didn't load.
            out, _ = self.run_child({"check": True, "model": str(Path(tmp) / "none"), "blocks": ["vad"],
                                     "vad": str(broken)})
            self.assertEqual((out[0]["loaded"], sorted(out[0]["errors"])), ([], ["vad"]))

    def test_an_embedding_the_child_packs_reads_back_in_the_take_check(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from serial_audio import takecheck
        import parakeet_asr

        # The int8 packing keeps the direction of the embedding, whatever its length and sign.
        rng = np.random.default_rng(3)
        for scale in (1.0, 37.0, 0.002):
            v = rng.normal(0, 1, 192) * scale
            back = takecheck.embedding({"v": takecheck.SPEAKER_V, **parakeet_asr.packed(v)})
            self.assertGreater(float(np.dot(v / np.linalg.norm(v), back)), 0.9999, scale)

    @unittest.skipUnless(MODELS["vad"].is_dir(), "needs the Silero VAD folder")
    def test_the_speech_detector_hears_no_speech_in_silence_or_hiss(self):
        import parakeet_asr
        config = parakeet_asr.vad_config(MODELS["vad"])
        rng = np.random.default_rng(1)
        self.assertEqual(parakeet_asr.speech_segments(config, np.zeros(32000, np.float32)), [])
        hiss = (rng.normal(0, 1, 32000) * 10 ** (-50 / 20)).astype(np.float32)
        self.assertEqual(parakeet_asr.speech_segments(config, hiss), [])

    @unittest.skipUnless(MODELS["speaker"].is_dir(), "needs the TitaNet small folder")
    def test_the_speaker_block_of_one_file_is_the_same_twice(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from serial_audio import takecheck
        with tempfile.TemporaryDirectory() as tmp:
            take = Path(tmp) / "take.wav"
            sf.write(take, voice(3.0), SR)
            job = {"model": str(Path(tmp) / "none"), "files": [str(take), str(take)], "blocks": ["speaker"],
                   "speaker": str(MODELS["speaker"])}
            out, _ = self.run_child(job)
        a, b = (takecheck.embedding(r["speaker"]) for r in out[:2])
        self.assertEqual((out[0]["speaker"]["engine"], len(a)), ("titanet-small", 192))
        self.assertGreater(takecheck.similarity(a, b), 0.9999)


@unittest.skipIf(np is None, "needs the audio Python")
class MeasureTest(unittest.TestCase):
    def test_the_k_weighting_at_48_khz_is_the_one_in_bs_1770(self):
        shelf, highpass = tm.k_sos(48000)
        for got, want in zip(shelf, (1.53512485958697, -2.69169618940638, 1.19839281085285,
                                     1.0, -1.69065929318241, 0.73248077421585)):
            self.assertAlmostEqual(got, want, places=7)
        for got, want in zip(highpass, (1.0, -2.0, 1.0, 1.0, -1.99004745483398, 0.99007225036621)):
            self.assertAlmostEqual(got, want, places=7)

    def test_a_full_scale_997_hz_sine_reads_minus_3_01_lkfs(self):
        # The known answer of BS.1770, at the rate of the takes and at the rate of the standard's table.
        for sr in (24000, 48000):
            self.assertAlmostEqual(tm.level(sine(3.0, sr), sr), -3.01, delta=0.02, msg=sr)
        block = tm.measure(sine(3.0, SR), SR)
        self.assertEqual((block["v"], block["seconds"], block["level"], block["peak"]), (tm.SIGNAL_V, 3.0, -3.0, 0.0))

        # Half the amplitude reads 6.02 dB lower.
        self.assertAlmostEqual(tm.level(sine(3.0, SR, peak=0.5), SR), -9.03, delta=0.02)

    def test_silence_has_no_level(self):
        block = tm.measure(np.zeros(SR, dtype=np.float32), SR)
        self.assertEqual((block["level"], block["peak"], block["speech"], block["active"], block["silences"]),
                         (None, None, None, 0.0, []))
        self.assertEqual(tm.measure(np.zeros(0, dtype=np.float32), SR)["seconds"], 0.0)

    def test_a_run_of_full_scale_samples_is_counted(self):
        x = voice(1.0)
        self.assertEqual(tm.clip_run(x), 0)
        x[1000:1005] = 1.0
        x[2000:2003] = -1.0
        x[3000] = 32767 / 32768
        self.assertEqual(tm.clip_run(x), 5)

        # A 16-bit file read as floats keeps its full-scale samples.
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "clipped.wav")
            sf.write(path, x, SR, subtype="PCM_16")
            back, _ = sf.read(path, dtype="float32")
            self.assertEqual(tm.clip_run(back), 5)

    def test_silent_runs_inside_the_speech_are_listed(self):
        # Half a second of room noise before the voice, a 0.5 s pause, a 0.1 s pause and 0.3 s after the voice
        x = np.concatenate([hush(0.5), voice(1.0), hush(0.5), voice(1.0, 2), hush(0.1), voice(1.0, 3), hush(0.3)])
        block = tm.measure(x, SR)

        # Only the 0.5 s pause is a silent run. The edges and the short pause are not.
        self.assertEqual(len(block["silences"]), 1)
        self.assertAlmostEqual(block["silences"][0][0], 1.5, delta=0.02)
        self.assertAlmostEqual(block["silences"][0][1], 2.0, delta=0.02)
        self.assertAlmostEqual(block["speech"][0], 0.5, delta=0.02)
        self.assertAlmostEqual(block["speech"][1], 4.1, delta=0.02)
        self.assertAlmostEqual(block["active"], 3.0, delta=0.04)

        # A copy 12 dB quieter has the same runs and reads 12 dB lower, because the gate follows the take's level.
        quiet = tm.measure(x * np.float32(0.25), SR)
        self.assertEqual(quiet["silences"], block["silences"])
        self.assertAlmostEqual(quiet["level"], block["level"] - 12.0, delta=0.15)

    def test_an_ending_that_fades_reads_as_a_tail_drop(self):
        steady = tm.measure(voice(4.0), SR)
        self.assertAlmostEqual(steady["tail_drop"], 0.0, delta=0.3)
        fading = voice(4.0)
        fading[3 * SR:] *= np.float32(10 ** (-8 / 20))
        self.assertAlmostEqual(tm.measure(fading, SR)["tail_drop"], -8.0, delta=0.3)

        # A take with under a second of speech before its last second has no tail drop.
        self.assertIsNone(tm.measure(voice(1.5), SR)["tail_drop"])

    def test_a_level_step_inside_a_take_reads_as_swing_and_jump(self):
        steady = tm.measure(voice(4.0), SR)
        self.assertLess(steady["swing"], 0.5)
        self.assertLess(steady["jump"], 0.5)
        stepped = voice(4.0)
        stepped[2 * SR:] *= np.float32(10 ** (10 / 20))
        block = tm.measure(stepped, SR)
        self.assertAlmostEqual(block["jump"], 10.0, delta=0.5)
        self.assertAlmostEqual(block["swing"], 10.0, delta=0.5)

        # A take of 2.5 s has seven windows of a second, which is too few for either.
        short = tm.measure(voice(2.5), SR)
        self.assertEqual((short["swing"], short["jump"]), (None, None))

    def test_the_gated_loudness_of_a_programme_has_the_answers_of_bs_1770(self):
        # A sine of one channel at -23 dB reads 3.01 under its peak, at both rates.
        for sr in (24000, 48000):
            self.assertAlmostEqual(tm.loudness(sine(20.0, sr, peak=10 ** (-23 / 20)), sr), -26.01, delta=0.1, msg=sr)

        # The relative gate leaves out a stretch more than 10 LU under the rest: ten seconds at -36 dB on each side
        # of a minute at -23 dB read as the minute alone, as in EBU Tech 3341.
        low, high = sine(10.0, SR, peak=10 ** (-36 / 20)), sine(60.0, SR, peak=10 ** (-23 / 20))
        self.assertAlmostEqual(tm.loudness(np.concatenate([low, high, low]), SR), -26.01, delta=0.1)

        # A stretch 6 LU under the rest is inside the gate, so it counts: half at -20 dB and half at -26 dB.
        halves = np.concatenate([sine(20.0, SR, peak=0.1), sine(20.0, SR, peak=0.05)])
        self.assertAlmostEqual(tm.loudness(halves, SR), -3.01 + 10 * np.log10((0.01 + 0.0025) / 2), delta=0.1)

        # Silence between the sounds is under the absolute gate and changes nothing.
        gap = np.zeros(10 * SR, dtype=np.float32)
        self.assertAlmostEqual(tm.loudness(np.concatenate([high, gap, high]), SR), -26.01, delta=0.1)

        # Digital silence and no audio have no loudness, and audio shorter than a block is one block.
        self.assertIsNone(tm.loudness(np.zeros(SR, dtype=np.float32), SR))
        self.assertIsNone(tm.loudness(np.zeros(0, dtype=np.float32), SR))
        self.assertAlmostEqual(tm.loudness(sine(0.2, SR, peak=0.1), SR), -23.01, delta=0.3)

    def test_the_power_of_a_chapter_is_the_same_a_stretch_at_a_time(self):
        x = np.concatenate([voice(1.3, 60), hush(0.4), voice(1.9, 61)])
        whole = tm.frame_power(tm.k_weighted(x, SR), SR)
        with mock.patch.object(tm, "STRETCH", 0.5):
            pieces = tm.weighted_power(x, SR)
        self.assertEqual(len(pieces), len(whole))
        self.assertTrue(np.allclose(pieces, whole, rtol=1e-9, atol=1e-15))
        self.assertEqual(len(tm.weighted_power(np.zeros(0, dtype=np.float32), SR)), 0)

    def test_the_true_peak_sees_between_the_samples(self):
        # A sine at a quarter of the rate, 45 degrees off the samples: every sample is 3.01 dB under the peak.
        x = (np.sin(2 * np.pi * np.arange(SR) / 4 + np.pi / 4)).astype(np.float32)
        self.assertAlmostEqual(20 * np.log10(np.abs(x).max()), -3.01, delta=0.01)
        envelope = tm.peak_envelope(x, SR)
        self.assertEqual((len(envelope), envelope.dtype), (len(x), np.float32))
        self.assertAlmostEqual(20 * np.log10(envelope[1000:-1000].max()), 0.0, delta=0.02)
        self.assertAlmostEqual(20 * np.log10(envelope[1000:-1000].min()), 0.0, delta=0.02)

        # A sine well under the rate peaks on its samples, and silence doesn't have a peak.
        self.assertAlmostEqual(tm.true_peak(sine(1.0, SR, hz=100.0, peak=0.5)[:-7], SR), -6.02, delta=0.02)
        self.assertIsNone(tm.true_peak(np.zeros(SR, dtype=np.float32), SR))
        self.assertIsNone(tm.true_peak(np.zeros(0, dtype=np.float32), SR))

        # Looking only near the loudest samples finds the same peak, and gives every other sample its own size.
        y = (np.random.default_rng(7).standard_normal(4 * SR) * 0.05).astype(np.float32)
        y[2 * SR: 2 * SR + 400] *= np.float32(8.0)
        full = tm.peak_envelope(y, SR)
        near = tm.peak_envelope(y, SR, 0.5 * float(np.abs(y).max()))
        self.assertEqual(float(near.max()), float(full.max()))
        self.assertAlmostEqual(tm.true_peak(y, SR), 20 * np.log10(float(full.max())), places=5)
        self.assertTrue(np.array_equal(near[:SR], np.abs(y[:SR])))
        self.assertTrue(np.all(near <= full + 1e-7))


if __name__ == "__main__":
    unittest.main()
