"""Tests for serial_audio.asr: the word comparison, the take check verdicts that use it, and the Parakeet pipe."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from serial_audio import asr, machine, parakeet_asr
from serial_audio import library as lib
from serial_audio.asr import words
from serial_audio.comfy import ComfyError
from serial_audio.takecheck import check_after_render, classify


class WordsTest(unittest.TestCase):
    def same(self, spoken: str, written: str) -> None:
        self.assertEqual(words(spoken), words(written), f"{spoken!r} vs {written!r}")

    def test_plain_words(self):
        self.assertEqual(words("Don’t stop, Simon!"), ["don't", "stop", "simon"])
        self.assertEqual(words("the dogs' bowls"), ["the", "dogs", "bowls"])

    def test_cardinals(self):
        self.same("a hundred and fifty", "150")
        self.same("one hundred fifty", "150")
        self.same("twenty-five", "25")
        self.same("two thousand three hundred and twelve", "2,312")
        self.same("two hundred and fifty thousand", "250,000")
        self.same("a thousand leagues", "1000 leagues")
        self.same("one million two hundred thousand", "1,200,000")
        self.same("twenty thousand leagues under the sea", "20,000 leagues under the sea")

    def test_years_and_times(self):
        self.same("in eighteen sixty-six", "in 1866")
        self.same("at ten thirty", "at 10:30")
        self.same("nineteen hundred", "1900")

    def test_ordinals(self):
        self.same("the twenty-first of May", "the 21st of May")
        self.same("the first time", "the 1st time")
        self.same("his hundredth day", "his 100th day")
        self.same("the third", "the 3rd")

    def test_symbols(self):
        self.same("thirty degrees fifteen minutes", "30° 15'")
        self.same("fifty percent", "50%")
        self.same("five dollars", "$5")
        self.same("three point five", "3.5")

    def test_separate_numbers_stay_apart(self):
        self.assertEqual(words("one, two, three"), ["1", "2", "3"])
        self.assertEqual(words("in 1866 two ships"), ["in", "1866", "2", "ships"])
        self.assertEqual(words("two and three"), ["2", "and", "3"])
        self.assertEqual(words("a man"), ["a", "man"])

    def test_number_formatting_is_not_flagged(self):
        expected = words("The Nautilus lay at a depth of a hundred and fifty metres, thirty degrees south.")
        verdict = classify(expected, "The Nautilus lay at a depth of 150 metres, 30° south.")
        self.assertEqual(verdict["verdict"], "ok", verdict)
        self.assertEqual(verdict["wer"], 0.0)

    def test_a_loop_is_still_flagged(self):
        expected = words("Yeah, right.")
        verdict = classify(expected, "Right, right, right, right, right.")
        self.assertEqual(verdict["verdict"], "flagged", verdict)


class ParakeetPipeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def child(self, body: str):
        """Runs _parakeet against a stand-in child script with this Python as the audio Python."""
        script = self.root / "child.py"
        script.write_text(body, encoding="utf-8")
        return [mock.patch.object(asr, "PARAKEET_SCRIPT", script),
                mock.patch.object(machine, "AUDIO_PYTHON", Path(sys.executable))]

    def run_child(self, body: str, files: list[Path]) -> list[tuple[int, str]]:
        patches = self.child(body)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return list(asr.transcripts(files, engine="parakeet", comfy_url=""))

    def test_emit_writes_ascii_that_round_trips(self):
        # The child's lines are ASCII, so a cp1252 stdout on Windows cannot break the UTF-8 parent.
        out = io.StringIO()
        with redirect_stdout(out):
            parakeet_asr.emit({"index": 0, "text": "Thalas’ fiancée paid £5"})
        line = out.getvalue()
        line.encode("ascii")
        self.assertEqual(json.loads(line)["text"], "Thalas’ fiancée paid £5")

    def test_transcripts_arrive_in_order(self):
        # Every file gets its transcript, in order. A line that is not a JSON object is skipped.
        body = ("import json, sys\njob = json.loads(sys.stdin.read())\nprint(42)\n"
                "for i, f in enumerate(job['files']):\n    print(json.dumps({'index': i, 'text': 'heard ' + f[-5:]}))\n"
                "print(json.dumps({'done': len(job['files'])}))\n")
        got = self.run_child(body, [Path("a.wav"), Path("b.wav")])
        self.assertEqual(got, [(0, "heard a.wav"), (1, "heard b.wav")])

    def test_a_child_that_exits_early_raises_comfy_error(self):
        # A child that dies before reading a large job gives a ComfyError with its stderr, not a broken pipe.
        files = [self.root / f"take_{i:04d}_{'x' * 60}.wav" for i in range(2000)]
        with self.assertRaises(ComfyError) as e:
            self.run_child("import sys\nsys.stderr.write('no sherpa here')\nsys.exit(3)\n", files)
        self.assertIn("no sherpa here", str(e.exception))
        self.assertIn("stopped after 0 of 2000", str(e.exception))

    def test_a_file_error_raises_comfy_error(self):
        # A per-file error from the child names the file.
        body = ("import json, sys\nsys.stdin.read()\n"
                "print(json.dumps({'index': 0, 'error': 'LibsndfileError: bad file'}))\n")
        with self.assertRaises(ComfyError) as e:
            self.run_child(body, [Path("bad.wav")])
        self.assertIn("bad.wav", str(e.exception))

    def test_check_after_render_never_raises(self):
        # Any failure inside the check is recorded as skipped in chapter.yaml instead of failing the render.
        with mock.patch.object(lib, "LIBRARY", self.root / "library"):
            lib.save_chapter_meta("s", 1, {"number": 1, "render": {"engine": "breeze"}})
            (lib.chapter_dir("s", 1) / "text.md").write_text("# 1\n", encoding="utf-8")
            with mock.patch("serial_audio.takecheck.check_takes", side_effect=BrokenPipeError(32, "Broken pipe")):
                summary = check_after_render("s", 1, comfy_url="", log=lambda *_: None)
            self.assertIn("BrokenPipeError", summary["skipped"])
            self.assertEqual(lib.load_chapter("s", 1)[0]["render"]["check"], summary)


if __name__ == "__main__":
    unittest.main()
