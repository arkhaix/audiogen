"""Tests for serial_audio.asr: the word comparison, the take check verdicts that use it, the Parakeet pipe, and the
retakes of flagged takes before the join."""
from __future__ import annotations

import hashlib
import io
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from serial_audio import asr, breeze, machine, parakeet_asr, takecheck
from serial_audio import library as lib
from serial_audio.asr import words
from serial_audio.comfy import ComfyError
from serial_audio.takecheck import check_after_render, classify
from serial_audio.web.jobs import render_progress


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


LOOP = "Nobody nobody nobody nobody answered the door."
LINES = [{"speaker": "narrator", "text": "The harbour was quiet that morning."},
         {"speaker": "narrator", "text": "Nobody answered the door."}]


def heard_file_text(files, *, engine, comfy_url):
    """A fake recognizer that hears the text each take file holds."""
    for i, f in enumerate(files):
        yield i, Path(f).read_text(encoding="utf-8")


class RetakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.logs: list[str] = []
        self.saves: list[dict] = []
        self.asked: list[tuple[str, int]] = []
        self.engine = "parakeet"
        for p in (mock.patch.object(takecheck, "transcripts", heard_file_text),
                  mock.patch.object(takecheck, "check_ready", lambda *a, **k: None),
                  mock.patch.object(breeze, "choose_asr", lambda *a, **k: self.engine)):
            p.start()
            self.addCleanup(p.stop)
        self.saved = {"0": self.take(0, LINES[0]["text"], "a0"), "1": self.take(1, LOOP, "a1")}

    def take(self, line: int, heard: str, audio: str) -> dict:
        file = self.folder / f"l{line:04d}.wav"
        file.write_text(heard, encoding="utf-8")
        return {"key": f"k{line}", "file": str(file), "voice": "narrator", "seed": 5, "audio": audio}

    def again(self, heard: str):
        """An again() that writes heard as the new take of a line and records each call."""
        def make(k: str, round_: int) -> dict:
            self.asked.append((k, round_))
            file = self.folder / f"l{int(k):04d}.retake.wav"
            file.write_text(heard, encoding="utf-8")
            return {"key": f"k{k}", "file": str(file), "seed": 90 + round_, "audio": f"b{round_}", "takes_tried": 2}
        return make

    def retake(self, again) -> list[int]:
        return breeze.retake_flagged(self.folder, LINES, {}, self.saved, ["0", "1"], again, comfy_url="",
                                     log=self.logs.append,
                                     save=lambda: self.saves.append(json.loads(json.dumps(self.saved))))

    def checked(self) -> dict:
        return lib.load_json(self.folder / takecheck.CHECK_FILE)["takes"]

    def test_a_flagged_take_is_replaced_by_one_that_says_the_line(self):
        # Only the flagged line is taken again, once, and its new take becomes the saved one.
        self.assertEqual(self.retake(self.again(LINES[1]["text"])), [1])
        self.assertEqual(self.asked, [("1", 1)])
        self.assertEqual(Path(self.saved["1"]["file"]).read_text(encoding="utf-8"), LINES[1]["text"])
        self.assertFalse((self.folder / "l0001.retake.wav").exists())
        self.assertEqual({k: self.saved["1"][k] for k in ("key", "seed", "audio", "retaken", "takes_tried")},
                         {"key": "k1", "seed": 91, "audio": "b1", "retaken": 1, "takes_tried": 3})
        self.assertEqual(self.saved["0"]["seed"], 5)

        # The takes were written to disk as soon as the take was replaced, and again as they ended up.
        self.assertEqual(self.saves[0]["1"]["audio"], "b1")
        self.assertEqual(self.saves[-1], self.saved)

        # take_check.json holds the verdict of both new takes, each tied to the audio it was heard in.
        checked = self.checked()
        self.assertEqual({k: (r["verdict"], r["audio"]) for k, r in checked.items()}, {"0": ("ok", "a0"), "1": ("ok", "b1")})
        self.assertTrue(takecheck.reusable(checked["1"], self.saved["1"], "parakeet"))

        # The summary of the check after the join lists the replaced line.
        self.assertEqual(takecheck.summarize(checked, self.saved)["retaken"], [1])

    def test_a_new_take_that_is_no_closer_is_dropped(self):
        # Every round hears the same loop, so the first take stays and the line stays flagged.
        self.assertEqual(self.retake(self.again(LOOP)), [])
        self.assertEqual(self.asked, [("1", 1), ("1", 2)])
        self.assertEqual((self.saved["1"]["seed"], self.saved["1"]["audio"]), (5, "a1"))
        self.assertNotIn("retaken", self.saved["1"])
        self.assertFalse((self.folder / "l0001.retake.wav").exists())
        self.assertEqual(self.checked()["1"]["verdict"], "flagged")
        self.assertTrue(any("still flagged" in line for line in self.logs), self.logs)

    def test_a_closer_take_that_is_still_flagged_replaces_the_first(self):
        # Three repeats are closer to the line than five, so the first new take is kept though it is still flagged.
        # The second round's take is no closer than that one and is dropped.
        self.saved["1"] = self.take(1, "Nobody nobody nobody nobody nobody nobody answered the door.", "a1")
        self.assertEqual(self.retake(self.again(LOOP)), [1])
        self.assertEqual(self.asked, [("1", 1), ("1", 2)])
        self.assertEqual((self.saved["1"]["audio"], self.saved["1"]["retaken"]), ("b1", 1))
        self.assertEqual(self.checked()["1"]["verdict"], "flagged")

    def test_a_failure_leaves_the_takes_as_they_are(self):
        # A retake that fails is logged and never raised, and the saved takes are unchanged.
        def broken(k, round_):
            raise ComfyError("breeze-server answered 500")
        before = json.loads(json.dumps(self.saved))
        self.assertEqual(self.retake(broken), [])
        self.assertEqual(self.saved, before)
        self.assertTrue(any("could not check the new takes" in line for line in self.logs), self.logs)

    def test_a_failure_removes_the_new_takes_it_did_not_judge(self):
        # Both lines are flagged. The first is taken again, the second fails, and the first's new take is removed.
        self.saved["0"] = self.take(0, "The the the the harbour was quiet that morning.", "a0")

        def half(k, round_):
            if k == "1":
                raise ComfyError("breeze-server answered 500")
            return self.again(LINES[0]["text"])(k, round_)
        before = json.loads(json.dumps(self.saved))
        self.assertEqual(self.retake(half), [])
        self.assertEqual(self.saved, before)
        self.assertEqual(list(self.folder.glob("*.retake.wav")), [])

    def test_a_failure_in_a_later_round_keeps_the_earlier_replacement(self):
        # Round one replaces the take with a closer one that is still flagged, and round two fails.
        self.saved["1"] = self.take(1, "Nobody nobody nobody nobody nobody nobody answered the door.", "a1")

        def once(k, round_):
            if round_ == 2:
                raise ComfyError("breeze-server answered 500")
            return self.again(LOOP)(k, round_)
        self.assertEqual(self.retake(once), [1])

        # The replacement is on record in the takes, on disk and in take_check.json, tied to the new audio.
        self.assertEqual((self.saved["1"]["audio"], self.saved["1"]["retaken"]), ("b1", 1))
        self.assertEqual(self.saves[-1], self.saved)
        self.assertEqual((self.checked()["1"]["audio"], self.checked()["1"]["verdict"]), ("b1", "flagged"))
        self.assertTrue(any("could not check the new takes" in line for line in self.logs), self.logs)

    def test_every_try_is_counted(self):
        # Two rounds of two tries each that are both dropped still count: 1 + 2 + 2.
        self.retake(self.again(LOOP))
        self.assertEqual(self.saved["1"]["takes_tried"], 5)

    def test_a_line_taken_again_records_its_round(self):
        # Both rounds are dropped, so the line keeps its take and records the last round. The other line records none.
        self.retake(self.again(LOOP))
        self.assertEqual((self.saved["1"]["retake_rounds"], self.saved["1"]["audio"]), (2, "a1"))
        self.assertNotIn("retake_rounds", self.saved["0"])

    def flag_saved(self) -> None:
        """Writes take_check.json for the saved takes, as the check of an earlier render left it."""
        entries = takecheck.check_entries(self.saved, LINES, {}, engine="parakeet", comfy_url="", log=self.logs.append)
        takecheck.save_entries(self.folder, entries, "parakeet")

    def test_a_saved_take_that_was_flagged_is_taken_again_once(self):
        # An earlier check flagged line 1 and passed line 0, so only line 1's saved take is one to take again.
        self.flag_saved()
        self.assertEqual(self.checked()["1"]["verdict"], "flagged")
        self.assertEqual(breeze.flagged_before(self.folder, self.saved), {"1"})

        # A verdict says nothing about a take with other audio, another key, or a line with no saved take.
        self.assertEqual(breeze.flagged_before(self.folder, {**self.saved, "1": {**self.saved["1"], "audio": "b9"}}), set())
        self.assertEqual(breeze.flagged_before(self.folder, {**self.saved, "1": {**self.saved["1"], "key": "k9"}}), set())
        self.assertEqual(breeze.flagged_before(self.folder, {"0": self.saved["0"]}), set())

        # After one round the line may still be taken again. After every round its take stands, though still flagged.
        self.assertEqual(breeze.flagged_before(self.folder, {**self.saved, "1": {**self.saved["1"], "retake_rounds": 1}}),
                         {"1"})
        self.retake(self.again(LOOP))
        self.assertEqual((self.checked()["1"]["verdict"], self.saved["1"]["retake_rounds"]),
                         ("flagged", breeze.CHECK_RETAKES))
        self.assertEqual(breeze.flagged_before(self.folder, self.saved), set())

    def test_a_flagged_take_from_before_takes_recorded_their_audio_is_taken_again(self):
        # Neither the take nor its verdict holds an audio hash, so they match on the key.
        for take in self.saved.values():
            del take["audio"]
        self.flag_saved()
        self.assertNotIn("audio", self.checked()["1"])
        self.assertEqual(breeze.flagged_before(self.folder, self.saved), {"1"})

    def test_a_missing_or_damaged_check_file_flags_nothing(self):
        self.assertEqual(breeze.flagged_before(self.folder, self.saved), set())
        for damaged in (b"{", b'{"takes": {"1": "\xff"}}', b'{"takes": {"1": "flagged"}}', b'{"takes": []}', b"[]"):
            (self.folder / takecheck.CHECK_FILE).write_bytes(damaged)
            self.assertEqual(breeze.flagged_before(self.folder, self.saved), set(), damaged)

        # A take whose record is damaged does not hide the verdict of another line.
        (self.folder / takecheck.CHECK_FILE).unlink()
        self.saved["0"] = self.take(0, "The the the the harbour was quiet that morning.", "a0")
        self.flag_saved()
        self.assertEqual(breeze.flagged_before(self.folder, {**self.saved, "1": {**self.saved["1"], "retake_rounds": "2"}}),
                         {"0", "1"})
        self.assertEqual(breeze.flagged_before(self.folder, {**self.saved, "1": "l0001.wav"}), {"0"})

    def test_only_a_machine_whose_parakeet_runs_takes_flagged_takes_again(self):
        self.assertTrue(breeze.retakes_flagged_here(""))
        self.engine = "qwen3"
        self.assertFalse(breeze.retakes_flagged_here(""))

        # Parakeet that cannot run, or a recognizer name this code does not know, is not a reason to fail a render.
        self.engine = "parakeet"
        with mock.patch.object(takecheck, "check_ready", side_effect=ComfyError("sherpa-onnx is missing")):
            self.assertFalse(breeze.retakes_flagged_here(""))
        with mock.patch.object(breeze, "choose_asr", side_effect=ComfyError("unknown speech recognizer 'x'")):
            self.assertFalse(breeze.retakes_flagged_here(""))

    def test_only_parakeet_checks_before_the_join(self):
        # Qwen3-ASR needs the card that breeze-server holds, so nothing is checked or taken again.
        self.engine = "qwen3"
        self.assertEqual(self.retake(self.again(LINES[1]["text"])), [])
        self.assertEqual(self.asked, [])
        self.assertFalse((self.folder / takecheck.CHECK_FILE).exists())

    def test_a_saved_transcript_is_reused_only_for_the_same_audio(self):
        old = {"key": "k1", "asr": "parakeet", "heard": "x", "audio": "a1"}
        self.assertTrue(takecheck.reusable(old, {"key": "k1", "audio": "a1"}, "parakeet"))
        self.assertFalse(takecheck.reusable(old, {"key": "k1", "audio": "b1"}, "parakeet"))
        self.assertFalse(takecheck.reusable(old, {"key": "k1", "audio": "a1"}, "qwen3"))
        # A take and an entry from before takes recorded their audio still match on the key.
        self.assertTrue(takecheck.reusable({"key": "k1", "asr": "parakeet", "heard": "x"}, {"key": "k1"}, "parakeet"))
        self.assertFalse(takecheck.reusable({"key": "k1", "asr": "parakeet", "heard": "x"},
                                            {"key": "k1", "audio": "a1"}, "parakeet"))


SAVED_TAKE = 2_000_000_001
LONGER_LOOP = "Nobody nobody nobody nobody nobody nobody nobody answered the door."


class RenderFlaggedTest(unittest.TestCase):
    """render_breeze on a rendered chapter whose saved take of line 1 an earlier check flagged, with a fake
    breeze-server, recognizer and join. Each take's audio holds a number: the seed that made it, or SAVED_TAKE for
    the saved take of line 1. The recognizer hears LOOP in that saved take and new_takes_say in any other take of
    line 1."""

    RATE = 24000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.engine, self.parakeet_runs, self.new_takes_say = "parakeet", True, LINES[1]["text"]
        self.asked: list[int] = []
        self.joined: dict[int, int] = {}
        self.logs: list[str] = []
        clip, test, real_run = b"clip", self, subprocess.run
        (root / "ref.wav").write_bytes(b"ref")

        class Server:
            sample_rate = self.RATE

            def __init__(self, log_path, log):
                pass

            def refuse_if_busy(self):
                pass

            def start(self):
                pass

            def take(self, fields, wav, label, log):
                test.asked.append(fields["seed"])
                return test.pcm(fields["seed"])

            def __exit__(self, *exc):
                pass

        def stage(slug, voices, runner):
            return {"narrator": {"ref": str(root / "ref.wav"), "transcript": "t", "sha1": hashlib.sha1(clip).hexdigest()}}

        def hears(files, *, engine, comfy_url):
            for i, f in enumerate(files):
                line = int(Path(f).name[1:5])
                said = LOOP if self.number_in(f) == SAVED_TAKE else self.new_takes_say
                yield i, said if line == 1 else LINES[line]["text"]

        def ready(engine, comfy_url):
            if not self.parakeet_runs:
                raise ComfyError("sherpa-onnx is missing from the audio Python")

        def run(args, *a, **k):
            if len(args) < 4 or str(args[2]) != str(breeze.JOIN_SCRIPT):
                return real_run(args, *a, **k)
            job = json.loads(Path(args[3]).read_text(encoding="utf-8"))
            self.joined = {t["line"]: self.number_in(t["file"]) for t in job["takes"]}
            Path(job["output"]).write_bytes(b"opus")
            summary = {"takes": len(job["takes"]), "seconds": 4.0, "sample_rate": self.RATE}
            return subprocess.CompletedProcess(args, 0, json.dumps(summary), "")

        for p in (mock.patch.object(lib, "LIBRARY", root / "library"),
                  mock.patch.object(breeze, "COMFY_OUTPUT", root / "out"),
                  mock.patch.object(breeze, "CppServer", Server),
                  mock.patch.object(breeze, "wait_comfy_idle", lambda *a, **k: None),
                  mock.patch.object(breeze, "free_comfy", lambda *a, **k: None),
                  mock.patch.object(breeze, "stage_references", stage),
                  mock.patch.object(breeze, "choose_asr", lambda *a, **k: self.engine),
                  mock.patch.object(takecheck, "choose", lambda *a, **k: self.engine),
                  mock.patch.object(takecheck, "check_ready", ready),
                  mock.patch.object(takecheck, "transcripts", hears),
                  mock.patch.object(subprocess, "run", run)):
            p.start()
            self.addCleanup(p.stop)

        # A serial with one voice and a rendered chapter of two lines
        serial = root / "library" / "s"
        (serial / "voices").mkdir(parents=True)
        lib.save_yaml(serial / "serial.yaml", {"title": "s", "narrator_voice_id": "narrator"})
        (serial / "voices" / "narrator.flac").write_bytes(clip)
        lib.save_yaml(serial / "voices" / "narrator.yaml", {"clip": "narrator.flac", "sample_text": "t"})
        self.chapter = serial / "chapters" / "0001"
        self.chapter.mkdir(parents=True)
        lib.save_yaml(self.chapter / "chapter.yaml", {"number": 1, "status": "rendered", "render": {
            "engine": "breeze", "runner": "cpp", "seed": 1, "check": {"checked": 2, "flagged": [1]}}})
        (self.chapter / "text.md").write_text("x", encoding="utf-8")
        lib.save_json(self.chapter / "script.json", {"lines": LINES})

        # Its saved takes, and the check that flagged line 1
        plan = breeze.plan_takes(LINES, {"narrator": 0}, {0: "narrator"}, {}, 1,
                                 {"narrator": hashlib.sha1(clip).hexdigest()}, "cpp")
        self.voice_seed = plan[0][4]
        take_dir = root / "out" / "breeze" / "s" / "ch0001" / "cpp"
        take_dir.mkdir(parents=True)
        saved = {}
        for i, voice, text, direction, vseed, key in plan:
            pcm = self.pcm(SAVED_TAKE if i == 1 else vseed)
            breeze.write_wav(take_dir / f"l{i:04d}.wav", pcm, self.RATE)
            saved[str(i)] = {"key": key, "file": str(take_dir / f"l{i:04d}.wav"), "voice": voice, "direction": direction,
                             "seconds": 1.0, "seed": vseed, "audio": hashlib.sha1(pcm).hexdigest()[:16]}
        breeze.save_takes(self.chapter / breeze.TAKES_FILE, saved, "cpp")
        entries = takecheck.check_entries(saved, LINES, {}, engine="parakeet", comfy_url="", log=self.logs.append)
        takecheck.save_entries(self.chapter, entries, "parakeet")

    def pcm(self, number: int) -> bytes:
        """Two seconds of audio that holds number."""
        return struct.pack("<i", number) * self.RATE

    def number_in(self, file) -> int:
        with wave.open(str(file), "rb") as w:
            return struct.unpack("<i", w.readframes(2))[0]

    def seed_of_try(self, n: int) -> int:
        """The seed cpp_take gives a line's n-th try, counted from 0 across rounds."""
        return 1 + (self.voice_seed - 1 + n * breeze.RETAKE_SEED_STEP) % (2**31 - 2)

    def render(self, **kw) -> dict:
        self.asked.clear()
        self.joined = {}
        return breeze.render_breeze("s", 1, seed=1, comfy_url="", log=self.logs.append, **kw)

    def take(self, line: int) -> dict:
        return lib.load_json(self.chapter / breeze.TAKES_FILE)["takes"][str(line)]

    def verdict(self, line: int) -> str:
        return lib.load_json(self.chapter / takecheck.CHECK_FILE)["takes"][str(line)]["verdict"]

    def test_a_render_takes_a_flagged_saved_take_again(self):
        # The fixture holds what an earlier render left: line 1 flagged, and a render would take it again.
        self.assertEqual((self.verdict(0), self.verdict(1)), ("ok", "flagged"))
        self.assertEqual(breeze.reusable_takes("s", 1, 1, "cpp"), (1, 2))

        # The render makes no line again from its own seed. It asks only for round one of line 1, which says the
        # line, so that take is joined in place of the saved one and line 0 keeps its take.
        record = self.render()
        self.assertEqual(self.asked, [self.seed_of_try(breeze.RETAKES + 1)])
        self.assertEqual(self.joined, {0: self.voice_seed, 1: self.asked[0]})
        self.assertEqual(record["generated_takes"], 0)

        # The take, its verdict and the chapter's check record all say so.
        self.assertEqual({k: self.take(1).get(k) for k in ("seed", "retaken", "retake_rounds")},
                         {"seed": self.asked[0], "retaken": 1, "retake_rounds": 1})
        self.assertEqual(self.verdict(1), "ok")
        self.assertEqual((record["check"]["flagged"], record["check"]["retaken"]), ([], [1]))
        self.assertEqual(lib.load_chapter("s", 1)[0]["render"]["check"], record["check"])
        self.assertFalse(list((self.chapter.parents[3] / "out").rglob("*.retake.wav")))

        # A later render has nothing to take and joins the same takes.
        self.render()
        self.assertEqual((self.asked, self.joined[1]), ([], self.seed_of_try(breeze.RETAKES + 1)))
        self.assertEqual(breeze.reusable_takes("s", 1, 1, "cpp"), (2, 2))

    def test_the_saved_take_stays_when_no_new_take_is_closer(self):
        # Every new take loops more than the saved one, so both rounds are asked for and the saved take is joined.
        self.new_takes_say = LONGER_LOOP
        record = self.render()
        self.assertEqual(self.asked, [self.seed_of_try(n * (breeze.RETAKES + 1)) for n in (1, 2)])
        self.assertEqual(self.joined[1], SAVED_TAKE)
        self.assertEqual(self.number_in(self.take(1)["file"]), SAVED_TAKE)
        self.assertEqual((self.take(1).get("retaken"), self.take(1)["retake_rounds"], self.verdict(1)),
                         (None, breeze.CHECK_RETAKES, "flagged"))
        self.assertEqual(record["check"]["flagged"], [1])

        # The line has been through every round, so a later render leaves it alone and counts its take as reusable.
        self.render()
        self.assertEqual((self.asked, self.joined[1]), ([], SAVED_TAKE))
        self.assertEqual(breeze.reusable_takes("s", 1, 1, "cpp"), (2, 2))

    def test_a_render_that_cannot_check_takes_nothing_again(self):
        # A join-only run, a machine with Qwen3-ASR, and a machine whose Parakeet cannot run all join the saved take,
        # start no breeze-server take and keep the chapter's last check record.
        before = lib.load_chapter("s", 1)[0]["render"]["check"]
        for change in ({"join_only": True}, {"engine": "qwen3"}, {"parakeet_runs": False}):
            self.engine, self.parakeet_runs = change.get("engine", "parakeet"), change.get("parakeet_runs", True)
            record = self.render(join_only=change.get("join_only", False))
            self.assertEqual((self.asked, self.joined[1], record["check"]), ([], SAVED_TAKE, before), change)
            self.assertNotIn("retake_rounds", self.take(1), change)

        # With Parakeet running again, the same chapter is taken again, so the cases above had something to take.
        self.engine, self.parakeet_runs = "parakeet", True
        self.render()
        self.assertEqual(len(self.asked), 1)


class FakeTakes:
    """A breeze-server whose take lasts the seconds listed for its turn, and that records each seed."""

    sample_rate = 24000

    def __init__(self, seconds: list[float]):
        self.seconds, self.seeds = list(seconds), []

    def take(self, fields, wav, label, log):
        self.seeds.append(fields["seed"])
        return b"\x00\x00" * int(self.sample_rate * self.seconds[len(self.seeds) - 1])


class CppTakeTest(unittest.TestCase):
    def take(self, seconds: list[float], first: int = 0):
        server = FakeTakes(seconds)
        got = breeze.cpp_take(server, b"", "ref", "Nobody answered the door.", "", 1000, 7, lambda *_: None, first=first)
        return server, got

    def test_a_take_that_fits_is_kept_at_once(self):
        server, got = self.take([2.0])
        self.assertEqual((server.seeds, got["seed"], got["tries"], got["fits"]), ([1000], 1000, 1, True))
        self.assertEqual(breeze.take_fields(got), {"seed": 1000, "audio": breeze.take_fields(got)["audio"]})

    def test_the_closest_take_is_kept_when_none_fits(self):
        # Four words fit 0.48 to 5.25 s. Three tries all run long, and the shortest is kept and marked.
        server, got = self.take([30.0, 9.0, 12.0])
        self.assertEqual(len(server.seeds), breeze.RETAKES + 1)
        self.assertEqual((got["seed"], got["tries"], got["fits"]), (server.seeds[1], 3, False))
        self.assertEqual({k: v for k, v in breeze.take_fields(got).items() if k != "audio"},
                         {"seed": server.seeds[1], "takes_tried": 3, "out_of_range": True})

    def test_a_later_round_uses_seeds_no_earlier_round_used(self):
        first, _ = self.take([30.0, 30.0, 30.0])
        second, _ = self.take([30.0, 30.0, 30.0], first=breeze.RETAKES + 1)
        self.assertEqual(len(set(first.seeds) | set(second.seeds)), 2 * (breeze.RETAKES + 1))


class RenderProgressTest(unittest.TestCase):
    HEAD = ("render: s chapter 1: 10 lines, 1 voices {0: 'narrator'} with Breeze TTS 2 via cpp\n"
            "   generating takes with Breeze TTS 2 via breeze-server (this is the slow part): 10 of 10 lines, 0 reused\n"
            "   [1 min] take 10/10, 3000 ms/take, ~0 min left\n")

    def test_the_check_before_the_join_is_its_own_phase(self):
        # While the new takes are checked, the estimate is the rest of the check and the join.
        text = self.HEAD + "   checking the 10 new takes with Parakeet before the join...\n"
        p = render_progress(text, None, 0.0, None)
        self.assertEqual((p["phase"], p["prechecked"], p["eta"]), ("checking", True, 10 * 0.5 + 60))

        # The join after it still says the takes were checked, so no check time is added to its estimate.
        p = render_progress(text + "   joining takes...\n", None, 0.0, None)
        self.assertEqual((p["phase"], p.get("prechecked")), ("joining", True))

        # A render that made one take words its line in the singular, and it reads the same way.
        p = render_progress(self.HEAD + "   checking the 1 new take with Parakeet before the join...\n", None, 0.0, None)
        self.assertEqual((p["phase"], p["prechecked"]), ("checking", True))

        # The check after the join and the end of the render read as before.
        p = render_progress(text + "   joining takes...\n   checking 0 takes with Parakeet, 10 already checked...\n",
                            None, 0.0, None)
        self.assertEqual(p["phase"], "checking")
        p = render_progress(text + "   joining takes...\n   take check: no takes flagged in 10\n   audio: x.opus\n",
                            None, 0.0, None)
        self.assertEqual(p["phase"], "done")

    def test_a_render_without_the_check_reads_as_before(self):
        p = render_progress(self.HEAD, None, 0.0, None)
        self.assertEqual((p["phase"], p.get("prechecked")), ("generating", None))

    def test_a_check_that_failed_before_the_join_does_not_count(self):
        # The check after the join then transcribes every take, so its time stays in the estimate.
        text = (self.HEAD + "   checking the 10 new takes with Parakeet before the join...\n"
                "   could not check the new takes before the join, so the join uses them as they are: ComfyError: x\n")
        self.assertEqual(render_progress(text, None, 0.0, None).get("prechecked"), None)
        p = render_progress(text + "   joining takes...\n", None, 0.0, None)
        self.assertEqual((p["phase"], p.get("prechecked")), ("joining", None))


if __name__ == "__main__":
    unittest.main()
