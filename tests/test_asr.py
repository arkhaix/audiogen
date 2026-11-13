"""Tests for serial_audio.asr: the word comparison, the take check verdicts that use it, the Parakeet pipe, and the
retakes of flagged takes before the join."""
from __future__ import annotations

import hashlib
import io
import json
import random
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from fractions import Fraction
from pathlib import Path
from unittest import mock

from serial_audio import asr, breeze, machine, parakeet_asr, phones, screenplay, takecheck
from serial_audio import library as lib
from serial_audio.asr import words
from serial_audio.comfy import ComfyError
from serial_audio.takecheck import check_after_render, classify
from serial_audio.web.jobs import render_progress

REAL_EXTRA_BLOCKS = asr.extra_blocks


def setUpModule():
    # The fakes here give Parakeet's two blocks only, whatever take check models this machine has.
    for target in (asr, takecheck):
        patcher = mock.patch.object(target, "extra_blocks", lambda: set())
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


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

    def test_titles_accents_possessives_and_stutters(self):
        # The recognizer writes "Mr." where the script says "Mister".
        self.same("Mr. Adrissant and Mrs Hale saw Dr. Voss.", "Mister Adrissant and Missus Hale saw Doctor Voss.")

        # An accented letter doesn't split its word.
        self.assertEqual(words("Thalas’ fiancée paid."), ["thalas", "fiancee", "paid"])

        # The possessive of a word that ends in s is written three ways and said one way. Another possessive stays.
        self.same("Thalas's men", "Thalas’ men")
        self.assertEqual(words("the Class's rules, the dogs' bowls, Simon's hat"),
                         ["the", "class", "rules", "the", "dogs", "bowls", "simon's", "hat"])

        # The fragment of a stutter is not a word. A prefix and a letter that names something are.
        self.assertEqual(words("Y-yes, master. Th-the door? I-I see."), ["yes", "master", "the", "door", "i", "see"])
        self.assertEqual(words("He re-read the e-mail in his T-shirt."),
                         ["he", "re", "read", "the", "e", "mail", "in", "his", "t", "shirt"])

    def test_each_word_knows_what_follows_it(self):
        # Every mark between two words counts, and a number made of several words keeps the mark after its last one.
        self.assertEqual(asr.word_map("“Wait\u2014no,” he said… (a hundred and fifty, at most)."),
                         [("wait", "\u2014"), ("no", ",”"), ("he", ""), ("said", "…("), ("150", ","), ("at", ""),
                          ("most", ").")])
        self.assertEqual(asr.word_map("At 10:30, Thalas’ men left."),
                         [("at", ""), ("1030", ","), ("thalas", ""), ("men", ""), ("left", ".")])
        self.assertEqual(asr.word_map("…"), [])
        for text in ("in eighteen sixty-six", "The 21st of May, 1866: two ships.", "Don’t stop, Simon!"):
            self.assertEqual([w for w, _ in asr.word_map(text)], words(text))


BIBLE = {"characters": {"simon": {"name": "Simon", "aliases": ["the bastard"]},
                        "lauriane": {"name": "Lauriane", "aliases": []},
                        "thalas": {"name": "Thalas Magnos", "aliases": ["Prince Thalas"]}},
         "pronunciations": {"Lauriane": {"respelling": "LOR-ee-ann", "say": "Laurie Anne"},
                            "Louis": {"respelling": "LOO-iss", "say": "Lewis"},
                            "Thalas": {"respelling": "THAH-lahs"},
                            "Balzam Magnos": {"respelling": "BAHL-zahm MAG-nohss"},
                            "demonbarrows": {"respelling": "DEE-mun-bar-ohz"}}}
SCRIPT = [
    {"speaker": "narrator", "text": "Chapter 12: In His Highness’s Service, Part 1", "direction": ""},
    {"speaker": "narrator", "text": "Lauriane met Louis at 10:30, with 150 men and 2,312 horses.", "direction": ""},
    {"speaker": "simon", "text": "Thalas’ fiancée (laugh) paid £5 for it… didn’t she?", "direction": "  dry,   amused "},
    {"speaker": "narrator", "text": "Balzam Magnos (the late emperor) had ruled the demonbarrows.", "direction": ""},
    {"speaker": "lauriane", "text": "No!", "direction": "shouting"},
    {"speaker": "narrator", "text": "…", "direction": ""},
]
SCRIPT_KEYS = {
    "cpp": ["801780cc4c68099ebf0b82ffc1b06232488806d8", "00fb8170d001979a22fcd49d85def8be50ad0e46",
            "a7007bfe96553c635489547d3cc8ac2d9ae4ec1a", "e40d8c6f631ebce0a09772569335c2a32c13da73",
            "a1bec2bc83f50adef6cc2f035eb01261a2b61f58", "3a72f87c38e9c466e659d1f3e5dd89ee4b78be3e"],
    "comfyui": ["d0228c3f972202ae1c6754efff79d5085325c9ac", "791a62c1ee85febee74e1f5cd805b09d7072d289",
                "17f3327168a53d62eae4f64aa3f9ca4c87480091", "00fb4444eafcbec3d8b794be1575bb44d2218466",
                "b0bd73e45db6b471dacc76eb3065cc5b294b3cf1", "bd25e792ab8608384e06743b6c9c2a92b6ed5c16"],
}


class VoiceTextTest(unittest.TestCase):
    def test_the_keys_of_a_fixed_script_stay_as_they_were(self):
        # Each line of the script gets the key that the library's takes of 2026-11-12 were saved under.
        slots, voices = {"narrator": 0, "simon": 1, "lauriane": 2}, {0: "narrator", 1: "simon", 2: "lauriane"}
        clips = {"narrator": "a" * 40, "simon": "b" * 40, "lauriane": "c" * 40}
        for runner, keys in SCRIPT_KEYS.items():
            plan = breeze.plan_takes(SCRIPT, slots, voices, BIBLE, 3, clips, runner)
            self.assertEqual([p[5] for p in plan], keys, runner)

        # The text is the say-as text with parentheses read aloud, and the direction loses its extra spaces.
        plan = breeze.plan_takes(SCRIPT, slots, voices, BIBLE, 3, clips, "cpp")
        self.assertEqual([p[2] for p in plan][1:4],
                         ["Laurie Anne met Lewis at 10:30, with 150 men and 2,312 horses.",
                          "Thalas’ fiancée (laugh) paid £5 for it… didn’t she?",
                          "Balzam Magnos, the late emperor, had ruled the demonbarrows."])
        self.assertEqual(plan[2][3], "dry, amused")

    def test_the_token_map_places_the_names_and_the_punctuation(self):
        def names(text: str) -> list[tuple[str, str, str]]:
            return [(t["w"], t["name"], t["after"]) for t in breeze.voice_text({"text": text}, BIBLE)[1]]

        # A say-as term is tagged on each word of its spoken spelling, and a possessive keeps its name.
        self.assertEqual(names("Simon’s men met Lauriane’s."),
                         [("simon's", "Simon", ""), ("men", "", ""), ("met", "", ""), ("laurie", "Lauriane", ""),
                          ("anne's", "Lauriane", ".")])

        # Each word of a pronunciation term and of a character's name is a name of its own. An alias is not.
        self.assertEqual(names("Balzam Magnos (the late emperor) called Thalas the bastard, twice."),
                         [("balzam", "Balzam", ""), ("magnos", "Magnos", ","), ("the", "", ""), ("late", "", ""),
                          ("emperor", "", ","), ("called", "", ""), ("thalas", "Thalas", ""), ("the", "", ""),
                          ("bastard", "", ","), ("twice", "", ".")])

        # The words are always the ones the check compares, for every line of the script: the words of the text
        # without its vocal events, which the voice performs and doesn't say.
        for line in SCRIPT:
            text, tokens = breeze.voice_text(line, BIBLE)
            self.assertEqual([t["w"] for t in tokens], words(breeze.without_events(text)), text)
            self.assertEqual(takecheck.expected_words(line, BIBLE), words(breeze.without_events(text)))
        self.assertEqual(names("(sigh) Fine."), [("fine", "", ".")])

        # A name that is also a number word is a name where its spoken word stands by itself, and the other
        # names of its line keep theirs.
        bible = {"characters": {"seven": {"name": "Seven"}, "simon": {"name": "Simon"}}, "pronunciations": {}}
        text, tokens = breeze.voice_text({"text": "Seven hundred men followed Seven and Simon."}, bible)
        self.assertEqual([(t["w"], t["name"]) for t in tokens],
                         [("700", ""), ("men", ""), ("followed", ""), ("7", "Seven"), ("and", ""), ("simon", "Simon")])

        # The possessive of a name that ends in s, and a stutter on a name, leave the line's names in place.
        self.assertEqual(names("Thalas's men met Simon."),
                         [("thalas", "Thalas", ""), ("men", "", ""), ("met", "", ""), ("simon", "Simon", ".")])
        self.assertEqual(names("T-Thalas met Simon's men."),
                         [("thalas", "Thalas", ""), ("met", "", ""), ("simon's", "Simon", ""), ("men", "", ".")])

        # A pronunciation term of one word is a name with its first letter in either case. A word of a longer
        # term is a name only as the term writes it.
        self.assertEqual(names("Demonbarrows and balzam."),
                         [("demonbarrows", "demonbarrows", ""), ("and", "", ""), ("balzam", "", ".")])

        # A say-as term wins over a longer pronunciation term at the same place, as it does in the text.
        bible = {"pronunciations": {**BIBLE["pronunciations"], "Lauriane's": {"respelling": "LOR-ee-annz"}}}
        text, tokens = breeze.voice_text({"text": "Lauriane's men"}, bible)
        self.assertEqual((text, [(t["w"], t["name"]) for t in tokens]),
                         ("Laurie Anne's men", [("laurie", "Lauriane"), ("anne's", "Lauriane"), ("men", "")]))

        # A bible without names gives every word no name.
        self.assertEqual(breeze.voice_text({"text": "Nobody answered."}, {})[1],
                         [{"w": "nobody", "name": "", "after": ""}, {"w": "answered", "name": "", "after": "."}])

    def test_the_names_are_those_of_the_bible_as_it_is_at_each_call(self):
        def mapped(bible: dict) -> tuple[str, list[tuple[str, str]]]:
            text, tokens = breeze.voice_text({"text": "Eole met Simon."}, bible)
            return text, [(t["w"], t["name"]) for t in tokens]

        # A say-as that is set, changed or cleared in the same bible shows in the next line's text and names.
        bible = {"characters": {}, "pronunciations": {"Eole": {"note": "a siren"}}}
        self.assertEqual(mapped(bible), ("Eole met Simon.", [("eole", "Eole"), ("met", ""), ("simon", "")]))
        bible["pronunciations"]["Eole"]["say"] = "Eh-ole"
        self.assertEqual(mapped(bible), ("Eh-ole met Simon.", [("eh", "Eole"), ("ole", "Eole"), ("met", ""), ("simon", "")]))
        bible["pronunciations"]["Eole"]["say"] = "Ay-ole"
        self.assertEqual(mapped(bible)[1][:2], [("ay", "Eole"), ("ole", "Eole")])
        del bible["pronunciations"]["Eole"]["say"]
        self.assertEqual(mapped(bible)[1][0], ("eole", "Eole"))
        # So does a character who is added, and a term that is taken out.
        bible["characters"]["simon"] = {"name": "Simon"}
        self.assertEqual(mapped(bible)[1], [("eole", "Eole"), ("met", ""), ("simon", "Simon")])
        del bible["pronunciations"]["Eole"]
        self.assertEqual(mapped(bible)[1], [("eole", ""), ("met", ""), ("simon", "Simon")])

        # A say-as that is not text leaves a line without its term as it is.
        odd = {"pronunciations": {"Vouivre": {"say": ["Vweevra"]}}}
        self.assertEqual(mapped(odd), ("Eole met Simon.", [("eole", ""), ("met", ""), ("simon", "")]))


class GapTest(unittest.TestCase):
    def gap(self, text: str, then: str | None = "Nobody answered.", speakers=("narrator", "narrator")) -> float:
        following = None if then is None else {"speaker": speakers[1], "text": then}
        return breeze.gap_after({"speaker": speakers[0], "text": text}, following)

    def test_the_gap_after_a_take_follows_the_script(self):
        # A finished sentence gets the longest gap before another speaker, and a shorter one before the same speaker.
        for text in ("The harbour was quiet.", "Was it quiet?", "Quiet!", "Quiet" + chr(0x2026)):
            self.assertEqual(self.gap(text, speakers=("narrator", "mira")), breeze.GAP_CHANGE, text)
            self.assertEqual(self.gap(text), breeze.GAP_SAME, text)

        # A sentence that goes on into the next line gets the shortest gap, whoever speaks next: a line that ends
        # in a comma, a colon, a dash or a word, and a next line that starts in lower case.
        for text in ("Run,", "He said:", "I thought" + chr(0x2014), "Oh, I will have to ask the waiter for more"):
            self.assertEqual(self.gap(text, speakers=("mira", "narrator")), breeze.GAP_CONTINUES, text)
            self.assertEqual(self.gap(text), breeze.GAP_CONTINUES, text)
        self.assertEqual(self.gap("Run!", "he said.", speakers=("mira", "narrator")), breeze.GAP_CONTINUES)
        self.assertEqual(self.gap("Run!", "He said it twice.", speakers=("mira", "narrator")), breeze.GAP_CHANGE)

        # Quote marks, brackets and an aside in parentheses at either end don't count.
        close = chr(0x201D)
        self.assertEqual(self.gap(f"He left.{close}", speakers=("narrator", "mira")), breeze.GAP_CHANGE)
        self.assertEqual(self.gap("I know. (sigh)", speakers=("mira", "narrator")), breeze.GAP_CHANGE)
        self.assertEqual(self.gap("Run!", "(laugh) he said.", speakers=("mira", "narrator")), breeze.GAP_CONTINUES)
        self.assertEqual(self.gap("(sigh)", speakers=("mira", "narrator")), breeze.GAP_CHANGE)

        # The last take of a chapter has nothing after it.
        self.assertEqual(self.gap("The end.", None), breeze.GAP_SAME)
        self.assertEqual((breeze.GAP_CONTINUES < breeze.GAP_SAME < breeze.GAP_CHANGE), True)


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

    def run_child(self, body: str, files: list[Path]) -> list[tuple[int, dict]]:
        patches = self.child(body)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return list(asr.records(files, engine="parakeet", comfy_url=""))

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
        self.assertEqual(got, [(0, {"text": "heard a.wav"}), (1, {"text": "heard b.wav"})])

    def test_a_record_keeps_the_tokens_and_the_signal(self):
        # What the child says about a file beside its text reaches the caller as it was sent, without the index.
        body = ("import json, sys\nsys.stdin.read()\n"
                "print(json.dumps({'index': 0, 'text': 'Hi there', 'tokens': [' Hi', ' there'], 'starts': [0.0, 0.24],"
                " 'durations': [0.16, 0.24], 'logprobs': [-0.1, 0.0], 'signal': {'v': 1, 'level': -26.0}}))\n"
                "print(json.dumps({'index': 1}))\nprint(json.dumps({'done': 2}))\n")
        got = self.run_child(body, [Path("a.wav"), Path("b.wav")])
        self.assertEqual(got[0], (0, {"text": "Hi there", "tokens": [" Hi", " there"], "starts": [0.0, 0.24],
                                      "durations": [0.16, 0.24], "logprobs": [-0.1, 0.0],
                                      "signal": {"v": 1, "level": -26.0}}))
        self.assertEqual(got[1], (1, {"text": ""}))

    def test_heard_reads_a_recognizer_result(self):
        # Times and log-probabilities are rounded, and a list that does not match the tokens is left out.
        class Result:
            text = " Hi there "
            tokens = [" Hi", " there"]
            timestamps = [0.0, 0.2399999946]
            durations = [0.1599999964]
            ys_log_probs = [-0.12046051, -0.00008392]
        self.assertEqual(parakeet_asr.heard(Result()), {"text": "Hi there", "tokens": [" Hi", " there"],
                                                        "starts": [0.0, 0.24], "logprobs": [-0.12, -0.0]})

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


def record(text: str, **signal) -> dict:
    """What Parakeet gives for a take that says text: one token per word, 0.3 s apart, and a signal block."""
    said = text.split()
    return {"text": text, "tokens": [f" {w}" for w in said], "starts": [round(0.3 * n, 2) for n in range(len(said))],
            "durations": [0.24] * len(said), "logprobs": [0.0] * len(said),
            "signal": {"v": takecheck.SIGNAL_V, "seconds": 0.3 * len(said) + 0.2, "level": -26.0, "peak": -8.0,
                       "clip_run": 0, "speech": [0.0, 0.3 * len(said)], "active": round(0.3 * len(said), 2),
                       "silences": [], "tail_drop": None, "swing": None, "jump": None, **signal}}


def heard_file_text(files, *, engine, comfy_url):
    """A fake recognizer that hears the text each take file holds. A file that starts with "#clipped " is a take
    with a long run of full-scale samples."""
    for i, f in enumerate(files):
        text = Path(f).read_text(encoding="utf-8")
        yield i, record(text.removeprefix("#clipped "), clip_run=40 if text.startswith("#clipped ") else 0)


def every_cell(ref: list[str], hyp: list[str]) -> list[tuple]:
    """The steps takecheck.align has to give, from the costs of every pair of a line's word and a heard word."""
    rows = [[((j, 0, 0, j, 0.0), "ins") for j in range(len(hyp) + 1)]]
    for i in range(1, len(ref) + 1):
        row = [((i, 0, i, 0, 0.0), "del")]
        for j in range(1, len(hyp) + 1):
            if ref[i - 1] == hyp[j - 1]:
                row.append((rows[i - 1][j - 1][0], "="))
                continue
            s, d, n = rows[i - 1][j - 1][0], rows[i - 1][j][0], row[j - 1][0]
            apart = takecheck.spelling_distance(ref[i - 1], hyp[j - 1])
            row.append(min(((s[0] + 1, s[1] + 1, s[2], s[3], s[4] + apart), "sub"),
                           ((d[0] + 1, d[1], d[2] + 1, d[3], d[4]), "del"),
                           ((n[0] + 1, n[1], n[2], n[3] + 1, n[4]), "ins")))
        rows.append(row)
    out, i, j = [], len(ref), len(hyp)
    while i or j:
        kind = rows[i][j][1]
        i, j = i - (kind != "ins"), j - (kind != "del")
        out.append((kind, None if kind == "ins" else i, None if kind == "del" else j))
    return out[::-1]


class JudgeTest(unittest.TestCase):
    def tokens(self, text: str) -> list[dict]:
        return breeze.voice_text({"text": text}, {})[1]

    def test_the_verdict_names_its_reasons(self):
        tokens = self.tokens("Nobody answered the door.")

        def judge(heard: str) -> dict:
            return takecheck.judge({"v": takecheck.ASR_V, "engine": "parakeet", "heard": heard}, None, tokens)

        # A take that says its line has no reason and nothing to repair.
        self.assertEqual(judge("Nobody answered the door."),
                         {"words": 4, "sub": 0, "del": 0, "ins": 0, "wer": 0.0, "names": {}, "verdict": "ok",
                          "reasons": [], "repairs": {"cuts": []}})

        # A loop is flagged for its inserted words, and a line that lost its end for its dropped words and its
        # ending.
        self.assertEqual((judge(LOOP)["verdict"], judge(LOOP)["reasons"]), ("flagged", ["inserted"]))
        lost = takecheck.judge({"v": takecheck.ASR_V, "heard": "Nobody answered."}, None,
                               self.tokens("Nobody answered the door that cold morning."))
        self.assertEqual((lost["verdict"], lost["reasons"], lost["del"]), ("flagged", ["dropped", "ending"], 5))

        # One changed word in four passes, two make the take suspect, and neither is a reason for a retake.
        self.assertEqual((judge("Nobody answered the dog.")["verdict"], judge("Nobody answered the dog.")["sub"]),
                         ("ok", 1))
        self.assertEqual((judge("Somebody answered the dog.")["verdict"], judge("Somebody answered the dog.")["reasons"]),
                         ("suspect", []))

        # Without an asr block of the current version no word rule fires, and the take is unreadable.
        for block in (None, {"heard": LOOP}, {"v": takecheck.ASR_V - 1, "heard": LOOP}, {"v": takecheck.ASR_V}):
            missing = takecheck.judge(block, None, tokens)
            self.assertEqual((missing["verdict"], missing["reasons"], missing["ins"], missing["del"], missing["words"]),
                             ("unreadable", [], 0, 0, 4), block)

        # A transcript in another script is unreadable, whatever its word count says.
        other = judge("कोई नहीं आया")
        self.assertEqual((other["verdict"], other["reasons"]), ("unreadable", []))

    def heard(self, said: str, heard: str, bible: dict | None = None, **signal) -> dict:
        """The verdict for a take of the line `said` that the recognizer heard as `heard`."""
        tokens = breeze.voice_text({"text": said}, BIBLE if bible is None else bible)[1]
        block = {"v": takecheck.SIGNAL_V, **signal} if signal else None
        return takecheck.judge({"v": takecheck.ASR_V, "engine": "parakeet", "heard": heard}, block, tokens)

    def test_the_alignment_lists_its_steps(self):
        # One word heard as another, one not heard and one added, in the order of the line.
        steps = takecheck.align("the old door was shut".split(), "the cold door shut tight".split())
        self.assertEqual(steps, [("=", 0, 0), ("sub", 1, 1), ("=", 2, 2), ("del", 3, None), ("=", 4, 3), ("ins", None, 4)])

        # With two ways to count the same number of edits, the substitution goes to the more similar word.
        steps = takecheck.align("un lord paimon".split(), "unlord payman".split())
        self.assertEqual(steps, [("del", 0, None), ("sub", 1, 0), ("sub", 2, 1)])
        self.assertEqual(takecheck.align([], ["a"]), [("ins", None, 0)])
        self.assertEqual(takecheck.align(["a"], []), [("del", 0, None)])

    def test_the_alignment_is_the_same_however_few_edits_it_first_looks_for(self):
        # Lines of a few similar words, heard with up to four slips or as other words altogether, so that many
        # alignments have the same number of edits.
        rng = random.Random(7)
        vocab = "a b ab ba abc the then them cat cot coat x".split()
        pairs = []
        for _ in range(600):
            ref = [rng.choice(vocab) for _ in range(rng.randrange(14))]
            hyp = list(ref) if rng.random() < 0.6 else [rng.choice(vocab) for _ in range(rng.randrange(14))]
            for _ in range(rng.randrange(5)):
                at = rng.randrange(len(hyp) + 1)
                hyp[at:at + rng.randrange(2)] = [rng.choice(vocab)] * rng.randrange(2)
            pairs.append((ref, hyp, every_cell(ref, hyp)))
        self.assertGreater(max(sum(kind != "=" for kind, _, _ in steps) for _, _, steps in pairs), 9)

        # Whatever ALIGN_BAND is, the steps are those that the costs of every pair of words give.
        for band in (0, 2, 9):
            with mock.patch.object(takecheck, "ALIGN_BAND", band):
                for ref, hyp, steps in pairs:
                    self.assertEqual(takecheck.align(ref, hyp), steps, (band, ref, hyp))

    def test_a_name_stands_for_the_way_it_was_heard(self):
        # The recognizer spells an invented name its own way, in one word or in two. Neither counts as a wrong
        # word, and the take records each spelling under the name's term.
        got = self.heard("Thalas met Lauriane and Balzam Magnos.", "Fall us met Lorianne and Balsam Magnus.")
        self.assertEqual((got["verdict"], got["sub"], got["del"], got["ins"]), ("ok", 0, 0, 0))
        self.assertEqual(got["names"], {"Thalas": ["fall us"], "Lauriane": ["lorianne"], "Balzam": ["balsam"],
                                        "Magnos": ["magnus"]})

        # A name heard as written is recorded too, once for each time the line has it.
        got = self.heard("Thalas saw Thalas.", "Thalas saw Thallus.")
        self.assertEqual(got["names"], {"Thalas": ["thalas", "thallus"]})

        # A name heard as two words that run together into it records both, and so does the possessive of a
        # name that ends in s.
        got = self.heard("He ruled the demonbarrows.", "He ruled the demon barrows.")
        self.assertEqual((got["verdict"], got["names"]), ("ok", {"demonbarrows": ["demon barrows"]}))
        got = self.heard("Then Simon saw Thalas's guard and Balzam Magnos's horse.",
                         "Then Simon saw Fallus guard and Balsam Magnus horse.")
        self.assertEqual((got["verdict"], got["sub"]), ("ok", 0))
        self.assertEqual(got["names"], {"Simon": ["simon"], "Thalas": ["fallus"], "Balzam": ["balsam"],
                                        "Magnos": ["magnus"]})

        # A name can't stand for words that are nothing like it, or for more than two words, and a name that was
        # not heard is a dropped word.
        got = self.heard("Then Thalas spoke to the crowd.", "Then a resistance for it spoke to the crowd.")
        self.assertEqual((got["sub"], got["ins"], got["names"]), (1, 3, {}))
        self.assertEqual(got["reasons"], ["inserted"])
        got = self.heard("Then Thalas spoke to the crowd.", "Then spoke to the crowd.")
        self.assertEqual((got["del"], got["names"]), (1, {}))

    def test_two_words_heard_as_one_and_one_heard_as_two_are_heard(self):
        for said, heard in (("What about demonbarrows?", "What about demon barrows?"),
                            ("She was self-aware by then.", "She was selfaware by then."),
                            ("Why’s that so odd?", "Why is that so odd?"),
                            ("They will not marry Anna now.", "They will not Marianna now.")):
            got = self.heard(said, heard, bible={})
            self.assertEqual((got["verdict"], got["sub"] + got["del"] + got["ins"]), ("ok", 0), heard)

    def test_many_changed_words_outside_names_flag_a_take(self):
        # Three of eight words are other words, and none of them is a name.
        got = self.heard("Does the name Cobweb mean anything to you?", "Ah. Some antrode mean anything to you?", bible={})
        self.assertEqual((got["verdict"], got["reasons"], got["sub"]), ("flagged", ["substituted"], 3))

        # Three changed words in a long line are under the share, and changed names don't count at all.
        long = "The harbour was quiet that morning and nobody answered the door of the old house."
        got = self.heard(long, long.replace("harbour", "arbor").replace("quiet", "quite").replace("morning", "mourning"),
                         bible={})
        self.assertEqual((got["verdict"], got["sub"]), ("ok", 3))
        got = self.heard("Thalas, Balzam Magnos and Simon left.", "Fallus, Balsam Magnus and Seaman left.")
        self.assertEqual((got["verdict"], got["sub"]), ("ok", 0))

    def test_a_dropped_last_word_flags_a_take(self):
        got = self.heard("What are you doing here?", "What are you doing?", bible={})
        self.assertEqual((got["verdict"], got["reasons"], got["del"]), ("flagged", ["ending"], 1))

        # A dropped word elsewhere and a changed last word don't.
        self.assertEqual(self.heard("What are you doing here?", "What you doing here?", bible={})["reasons"], [])
        self.assertEqual(self.heard("What are you doing here?", "What are you doing hear?", bible={})["reasons"], [])

    def test_a_short_line_is_flagged_for_an_added_or_dropped_word(self):
        # One to three words: an added or a dropped word flags the take.
        for said, heard in (("Sure.", "Ah, sure."), ("Leonard replied.", "Leonard replied. You."),
                            ("A plan?", "plan."), ("Thalas?", "Hoo-hoo-hoo.")):
            got = self.heard(said, heard)
            self.assertEqual((got["verdict"], got["reasons"]), ("flagged", ["short"]), heard)

        # A changed word is the recognizer's usual slip on a short line, so the take is suspect and not flagged.
        got = self.heard("Simon guessed.", "Simon Guest")
        self.assertEqual((got["verdict"], got["reasons"]), ("suspect", []))

        # A name heard its own way and a stutter are heard right.
        for said, heard in (("Thalas!", "Fall us."), ("Y-yes, master.", "Yes, master.")):
            self.assertEqual(self.heard(said, heard)["verdict"], "ok", heard)

        # Four words are not a short line: one added word is under every limit.
        self.assertEqual(self.heard("Well, I do know.", "Oh, well, I do know.")["verdict"], "ok")

    def test_a_line_the_words_cannot_prove_is_judged_on_its_length(self):
        # The recognizer hears nothing in "Oh...". With a second of speech the take passes, and with six it
        # is flagged. Without a signal block the words are all there is, and the take is suspect as before.
        self.assertEqual(self.heard("Oh…", "", active=0.8)["verdict"], "ok")
        got = self.heard("Oh…", "", active=6.0)
        self.assertEqual((got["verdict"], got["reasons"]), ("flagged", ["length"]))
        self.assertEqual(self.heard("Oh…", "")["verdict"], "suspect")

        # A line with fewer than two words outside its names is judged on its length too, though its words match.
        got = self.heard("Thalas and Balzam Magnos.", "Thalas and Balzam Magnos.", active=13.0)
        self.assertEqual((got["verdict"], got["reasons"]), ("flagged", ["length"]))
        self.assertEqual(self.heard("Thalas and Balzam Magnos.", "Thalas and Balzam Magnos.", active=1.6)["verdict"], "ok")

        # A longer line is not, whatever its length.
        self.assertEqual(self.heard("Nobody answered the door.", "Nobody answered the door.", active=30.0)["verdict"], "ok")

        # A line of three words that the recognizer hears nothing in is judged on its length and not on its
        # dropped words. A line of four is judged on its words.
        self.assertEqual(self.heard("I don’t know.", "", active=1.2)["verdict"], "ok")
        got = self.heard("I don’t know.", "", active=9.0)
        self.assertEqual((got["verdict"], got["reasons"]), ("flagged", ["length"]))
        got = self.heard("Nobody answered the door.", "", active=1.2)
        self.assertEqual((got["verdict"], got["reasons"]), ("flagged", ["dropped", "ending"]))

        # A vocal event is not a word of the line, so a take is not missing its ending or a word for one.
        self.assertEqual(self.heard("That is very funny indeed. (laugh)", "That is very funny indeed.")["verdict"], "ok")
        self.assertEqual(self.heard("(sigh) Fine.", "Fine.")["verdict"], "ok")

    def timed(self, said: str, gaps: dict[int, float] | None = None) -> dict:
        """An asr block for a take that says `said` at 0.3 s a word, with gaps[n] more seconds before word n."""
        tokens, starts, at = [], [], 0.0
        for n, word in enumerate(said.split()):
            at += (gaps or {}).get(n, 0.0)
            mark = word[-1] if word[-1] in ",.?!" else ""
            tokens += [f" {word.rstrip(',.?!')}"] + ([mark] if mark else [])
            starts += [round(at, 2)] + ([round(at + 0.24, 2)] if mark else [])
            at += 0.3
        return {"v": takecheck.ASR_V, "engine": "parakeet", "heard": said, "tokens": tokens, "starts": starts,
                "durations": [0.24] * len(tokens), "logprobs": [0.0] * len(tokens)}

    def test_the_tokens_say_when_each_word_was_heard(self):
        # A word runs from its first token to the end of its last, and a punctuation token doesn't move its end.
        asr = self.timed("Nobody answered, and the door stayed shut.", {2: 0.9})
        self.assertEqual(takecheck.word_times(asr)[:3],
                         [("nobody", 0.0, 0.24), ("answered", 0.3, 0.54), ("and", 1.5, 1.74)])

        # Tokens that don't give the transcript's words, and a block without times, give nothing.
        self.assertIsNone(takecheck.word_times({**asr, "heard": "Nobody answered."}))
        self.assertIsNone(takecheck.word_times({"heard": "Nobody answered."}))
        self.assertIsNone(takecheck.word_times({**asr, "starts": asr["starts"][:-1]}))

    def test_a_long_pause_that_no_punctuation_explains_is_cut(self):
        line = "The harbour was quiet that morning and nobody answered, so he left."
        tokens = breeze.voice_text({"text": line}, {})[1]

        def cuts(silences, asr=None, **kw) -> list:
            block = {"v": takecheck.SIGNAL_V, "silences": silences}
            return takecheck.judge(asr or self.timed(line, {6: 0.9, 9: 0.9}), block, tokens, **kw)["repairs"]["cuts"]

        # 0.9 s of silence sits between "morning" and "and", and the same after the comma. Only the first is cut,
        # and it keeps PAUSE_KEEP of the pause.
        runs = [[1.76, 2.66], [3.56, 4.46]]
        self.assertEqual(cuts(runs), [[1.89, 2.54]])
        self.assertAlmostEqual(0.9 - (2.54 - 1.89), takecheck.PAUSE_KEEP, places=2)

        # A pause under PAUSE_OVER stays, and so does every pause of a line that keeps its pauses.
        self.assertEqual(cuts([[1.76, 2.3]]), [])
        self.assertEqual(cuts(runs, cut_pauses=False), [])

        # No cut beside a word that was heard as another word, or in a run the token times can't place.
        wrong = self.timed(line.replace("morning", "mourning"), {6: 0.9, 9: 0.9})
        self.assertEqual(cuts(runs, asr=wrong), [])
        self.assertEqual(cuts([[0.4, 1.3]]), [])

        # A line with a vocal event or a direction about pace keeps its pauses.
        self.assertTrue(breeze.keeps_its_pauses({"text": "Well (sigh) there it is.", "direction": ""}))
        self.assertTrue(breeze.keeps_its_pauses({"text": "I... I can't.", "direction": "weakly, halting"}))
        self.assertFalse(breeze.keeps_its_pauses({"text": "Well (he thought) there it is.", "direction": "amused"}))

    def test_the_signal_gives_its_own_reasons(self):
        base = {"n": 20, "level": [-26.0, 1.0], "rate": [0.3, 0.03]}

        def reasons(words_said=8, baseline=base, offset=0.0, **signal) -> list[str]:
            return takecheck.signal_reasons({"v": takecheck.SIGNAL_V, **signal}, words_said, baseline, offset)

        # A clean take of the voice's own level and rate has no reason. Each measure at its limit has one.
        self.assertEqual(reasons(clip_run=2, tail_drop=-9.0, swing=8.0, jump=9.0, active=2.4, level=-25.0), [])
        self.assertEqual(reasons(clip_run=takecheck.CLIP_RUN), ["clipped"])
        self.assertEqual(reasons(tail_drop=takecheck.FADE_DROP), ["fading"])
        self.assertEqual(reasons(swing=takecheck.SWING), ["swing"])
        self.assertEqual(reasons(jump=takecheck.JUMP), ["swing"])
        from serial_audio import casting
        self.assertEqual(takecheck.CLIP_RUN, casting.CLIP_RUN)

        # Eight words in a second, or in seven, are far from 0.3 s a word. A short line has no rate.
        self.assertEqual(reasons(active=1.0), ["rate"])
        self.assertEqual(reasons(active=7.0), ["rate"])
        self.assertEqual(reasons(active=1.2, words_said=3), [])

        # A take 13 dB under the voice is off, unless its direction puts it there.
        self.assertEqual(reasons(level=-39.0), ["level"])
        self.assertEqual(reasons(level=-39.0, offset=breeze.LEVEL_OFFSETS["whisper"]), [])
        self.assertEqual(reasons(level=-12.0), ["level"])

        # A voice with few takes has no baseline to compare with, and a measure the block lacks fires nothing.
        few = {"n": takecheck.BASELINE_MIN - 1, "level": [-26.0, 1.0], "rate": [0.3, 0.03]}
        self.assertEqual(reasons(active=1.0, level=-39.0, baseline=few), [])
        self.assertEqual(reasons(active=1.0, level=-39.0, baseline=None), [])
        self.assertEqual(reasons(tail_drop=None, swing=None, jump=None, level=None), [])

        # The direction's words give its class, the softest when it names two.
        self.assertEqual([breeze.direction_class(d) for d in ("whispering, menacing", "muttering to himself", None,
                                                              "shouting, furious", "quietly, then shouting", "amused")],
                         ["whisper", "soft", "normal", "shout", "soft", "normal"])

    def test_a_signal_reason_flags_a_take_and_ranks_it(self):
        clipped = self.heard("Nobody answered the door.", "Nobody answered the door.", bible={}, clip_run=40)
        clean = self.heard("Nobody answered the door.", "Nobody answered the door.", bible={}, clip_run=0)
        self.assertEqual((clipped["verdict"], clipped["reasons"], clean["verdict"]), ("flagged", ["clipped"], "ok"))

        # Between two takes with the same words, the one without the signal reason is the better one.
        entry = {"signal": {"v": takecheck.SIGNAL_V}}
        self.assertTrue(takecheck.better({**entry, **clean}, {**entry, **clipped}))
        self.assertFalse(takecheck.better({**entry, **clipped}, {**entry, **clipped}))

    def test_two_takes_are_ranked_on_the_slots_both_have(self):
        def entry(verdict: str, errors: int, signal: bool = False, reasons=()) -> dict:
            out = {"verdict": verdict, "sub": errors, "del": 0, "ins": 0, "reasons": list(reasons)}
            return {**out, "signal": {"v": takecheck.SIGNAL_V}} if signal else out

        # The slots are the verdict, the word errors, the name score, the signal reasons and the prosody rank.
        self.assertEqual(takecheck.badness(entry("flagged", 5)), (3, 5, None, None, None))
        self.assertEqual(takecheck.badness(entry("ok", 1, signal=True)), (0, 1, None, 0, None))

        # The verdict outranks the word errors, and a take that is no better does not win.
        self.assertTrue(takecheck.better(entry("suspect", 9), entry("flagged", 3)))
        self.assertTrue(takecheck.better(entry("flagged", 3), entry("flagged", 5)))
        self.assertFalse(takecheck.better(entry("flagged", 5), entry("flagged", 5)))
        self.assertFalse(takecheck.better(entry("flagged", 6), entry("flagged", 5)))

        # With equal words the signal reasons decide, and only between two takes whose signal was measured.
        with mock.patch.object(takecheck, "SIGNAL_REASONS", frozenset({"clipped"})):
            clean, clipped = entry("flagged", 5, True), entry("flagged", 5, True, ["clipped", "inserted"])
            self.assertEqual(takecheck.badness(clipped)[3], 1)
            self.assertTrue(takecheck.better(clean, clipped))
            self.assertFalse(takecheck.better(clipped, clean))
            self.assertFalse(takecheck.better(clean, entry("flagged", 5, reasons=["clipped"])))

    def test_each_voice_gets_a_baseline_from_its_takes(self):
        lines = [LINES[0]] * 4 + [{"speaker": "simon", "text": "Hi."}]
        takes = {**{str(i): {"voice": "narrator"} for i in range(4)}, "4": {"voice": "simon"}, "5": {"voice": "simon"}}

        def entry(level, active, v=takecheck.SIGNAL_V) -> dict:
            return {"signal": {"v": v, "level": level, "active": active}}

        # Three narrator takes of six words count. A block of another version, a take with no level and a take
        # of a line the script lacks do not.
        entries = {"0": entry(-26.0, 1.8), "1": entry(-24.0, 2.4), "2": entry(-30.0, 3.0),
                   "3": entry(-1.0, 9.0, takecheck.SIGNAL_V + 1), "4": entry(-20.0, 0.4), "5": entry(None, 0.0)}
        self.assertEqual(takecheck.baselines(entries, takes, lines, {}),
                         {"narrator": {"n": 3, "level": [-26.0, 2.0], "rate": [0.4, 0.1]},
                          "simon": {"n": 1, "level": [-20.0, 0.0]}})

    def test_judging_again_reads_each_baseline_from_the_current_takes(self):
        # Five takes of one voice at -26 LKFS and a sixth at -40. The file also holds eight entries at -40 for audio
        # that was replaced since.
        said = LINES[0]["text"]

        def entry(level: float, audio: str) -> dict:
            got = record(said, level=level)
            return {"key": "k", "audio": audio, "signal": got.pop("signal"),
                    "asr": {"v": takecheck.ASR_V, "engine": "parakeet", "heard": got["text"]}}

        entries = {**{str(i): entry(-26.0, "a") for i in range(5)}, "5": entry(-40.0, "a"),
                   **{str(i): entry(-40.0, "old") for i in range(6, 14)}}
        takes = {str(i): {"key": "k", "audio": "a" if i < 6 else "new", "voice": "narrator"} for i in range(14)}

        # The sixth take is 14 dB under its voice's current takes, so it is flagged. With the replaced takes in the
        # baseline it would sit on the median.
        again = takecheck.rejudged(entries, takes, [LINES[0]] * 14, {})
        self.assertEqual((again["5"]["verdict"], again["5"]["reasons"]), ("flagged", ["level"]))
        self.assertEqual(again["0"]["verdict"], "ok")

    def test_a_reason_that_flags_much_of_a_voice_is_a_voice_finding(self):
        takes = {str(i): {"voice": "keeper" if i < 10 else "narrator"} for i in range(30)}

        def entry(*reasons) -> dict:
            return {"verdict": "flagged" if reasons else "ok", "reasons": list(reasons)}

        # Three of ten takes are over the share and under the count.
        entries = {**{str(i): entry() for i in range(30)}, **{str(i): entry("clipped") for i in range(3)}}
        self.assertEqual(takecheck.voice_findings(entries, takes), [])

        # A fourth makes it a finding, for that reason only.
        entries["3"] = entry("clipped", "dropped")
        finding = [{"voice": "keeper", "reason": "clipped", "lines": [0, 1, 2, 3], "of": 10}]
        self.assertEqual(takecheck.voice_findings(entries, takes), finding)

        # Four of twenty takes reach the count and not the share.
        entries.update({str(i): entry("inserted") for i in range(10, 14)})
        self.assertEqual(takecheck.voice_findings(entries, takes), finding)


class RetakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.logs: list[str] = []
        self.saves: list[dict] = []
        self.asked: list[tuple[str, int]] = []
        self.engine = "parakeet"
        for p in (mock.patch.object(takecheck, "records", heard_file_text),
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

    def retake(self, again, made=("0", "1"), lines=LINES) -> list[int]:
        self.outcome = breeze.retake_flagged(self.folder, lines, {}, self.saved, list(made), again, comfy_url="",
                                             log=self.logs.append,
                                             save=lambda: self.saves.append(json.loads(json.dumps(self.saved))))
        return self.outcome["replaced"]

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

    def test_a_line_goes_on_from_the_round_it_reached(self):
        # An earlier render took the line through round one, so this one gives it round two only.
        self.saved["1"]["retake_rounds"] = 1
        self.assertEqual(self.retake(self.again(LOOP)), [])
        self.assertEqual((self.asked, self.saved["1"]["retake_rounds"]), ([("1", 2)], 2))

        # A line that has been through every round is heard, stays flagged and is not taken again.
        self.asked.clear()
        self.assertEqual(self.retake(self.again(LINES[1]["text"])), [])
        self.assertEqual((self.asked, self.checked()["1"]["verdict"], self.saved["1"]["retake_rounds"]),
                         ([], "flagged", 2))

    def test_a_line_the_ceiling_stops_after_round_one_goes_on_with_round_two(self):
        # The render may take a line again once. Round one's take is no closer, and round two is left for later.
        with mock.patch.object(breeze, "RETAKE_FLOOR", 1), mock.patch.object(breeze, "RETAKE_SHARE", 0.0):
            self.assertEqual(self.retake(self.again(LOOP)), [])
            self.assertEqual((self.asked, self.outcome["left"], self.saved["1"]["retake_rounds"]), ([("1", 1)], [1], 1))
            self.assertEqual(breeze.flagged_before(self.folder, self.saved), {"1"})

            # The next render takes round two, and no seed of round one again.
            self.asked.clear()
            self.assertEqual(self.retake(self.again(LINES[1]["text"])), [1])
            self.assertEqual((self.asked, self.saved["1"]["retake_rounds"], self.saved["1"]["retaken"]),
                             ([("1", 2)], 2, 2))

    def test_a_take_the_signal_flags_is_taken_again(self):
        # Line 1 says its words and is clipped. The new take says them without the clipping and replaces it.
        self.saved["1"] = self.take(1, "#clipped " + LINES[1]["text"], "a1")
        self.assertEqual(self.retake(self.again(LINES[1]["text"])), [1])
        self.assertEqual((self.asked, self.checked()["1"]["verdict"], self.checked()["1"]["reasons"]),
                         ([("1", 1)], "ok", []))

        # A new take that is clipped too is no better, so the first one stays.
        self.saved["1"] = self.take(1, "#clipped " + LINES[1]["text"], "a1")
        self.assertEqual(self.retake(self.again("#clipped " + LINES[1]["text"])), [])
        self.assertEqual((self.saved["1"]["audio"], self.checked()["1"]["reasons"]), ("a1", ["clipped"]))

    def test_the_check_before_the_join_stores_the_evidence_and_the_baseline(self):
        self.retake(self.again(LINES[1]["text"]))
        data = lib.load_json(self.folder / takecheck.CHECK_FILE)

        # Each entry keeps both blocks as the recognizer gave them, beside what the judge derived.
        entry, heard = data["takes"]["0"], record(LINES[0]["text"])
        self.assertEqual(entry["asr"], {"v": takecheck.ASR_V, "engine": "parakeet", "heard": heard["text"],
                                        **{f: heard[f] for f in takecheck.TOKEN_FIELDS}})
        self.assertEqual(entry["signal"], heard["signal"])
        self.assertEqual((entry["key"], entry["audio"], entry["verdict"], entry["reasons"], entry["repairs"]),
                         ("k0", "a0", "ok", [], {"cuts": []}))

        # The file holds the voice's baseline over the chapter: both takes at -26 LKFS and 0.3 s a word.
        self.assertEqual(data["voices"], {"narrator": {"n": 2, "level": [-26.0, 0.0], "rate": [0.3, 0.0]}})

        # On disk each take's entry is one line, so the token lists stay small.
        text = (self.folder / takecheck.CHECK_FILE).read_text(encoding="utf-8")
        self.assertEqual([line[:9] for line in text.splitlines() if line.startswith("    ")], ['    "0": ', '    "1": '])
        takecheck.write_check(self.folder / "empty.json", {"asr": "parakeet", "takes": {}})
        self.assertEqual(lib.load_json(self.folder / "empty.json"), {"asr": "parakeet", "takes": {}})

    def test_a_voice_that_one_reason_keeps_flagging_is_reported_and_not_taken_again(self):
        # Five of the keeper's six takes loop, so the cause is the voice.
        lines, made = [LINES[1]] * 6, [str(i) for i in range(6)]
        self.saved = {k: {**self.take(int(k), LOOP if int(k) else LINES[1]["text"], f"a{k}"), "voice": "keeper"}
                      for k in made}
        self.assertEqual(self.retake(self.again(LINES[1]["text"]), made=made, lines=lines), [])

        # No line is taken again. The finding names the voice, the reason and the lines, and each take records it.
        self.assertEqual(self.asked, [])
        self.assertEqual(self.outcome["findings"],
                         [{"voice": "keeper", "reason": "inserted", "lines": [1, 2, 3, 4, 5], "of": 6}])
        self.assertEqual(self.saved["1"]["retake_held"], ["inserted"])
        self.assertNotIn("retake_held", self.saved["0"])
        self.assertEqual(self.saves[-1], self.saved)
        self.assertTrue(any("voice finding: inserted flags 5 of the 6 takes of keeper" in line for line in self.logs),
                        self.logs)

        # A later render leaves those takes alone, though the check still flags them. Without the record on the
        # take it would take them again.
        self.assertEqual(breeze.flagged_before(self.folder, self.saved), set())
        bare = {k: {f: v for f, v in t.items() if f != "retake_held"} for k, t in self.saved.items()}
        self.assertEqual(breeze.flagged_before(self.folder, bare), {"1", "2", "3", "4", "5"})

        # A take held for one reason is still taken again when the check flags it for another.
        other = {**self.saved, "1": {**self.saved["1"], "retake_held": ["clipped"]}}
        self.assertEqual(breeze.flagged_before(self.folder, other), {"1"})

    def test_a_render_takes_lines_again_only_up_to_its_ceiling(self):
        self.assertEqual((breeze.retake_ceiling(40), breeze.retake_ceiling(400)), (8, 50))

        # Three lines loop, four, six and five times, and the render may take a line again twice.
        lines, made, loops = [LINES[1]] * 4, list("0123"), {1: 4, 2: 6, 3: 5}
        self.saved = {k: self.take(int(k), " ".join(["Nobody"] * loops.get(int(k), 1)) + " answered the door.", f"a{k}")
                      for k in made}
        with mock.patch.object(breeze, "RETAKE_FLOOR", 2), mock.patch.object(breeze, "RETAKE_SHARE", 0.0):
            replaced = self.retake(self.again(LINES[1]["text"]), made=made, lines=lines)

        # The two worst lines are taken again, and the third is left for a later render with no round spent.
        self.assertEqual((replaced, self.asked, self.outcome["left"]), ([2, 3], [("2", 1), ("3", 1)], [1]))
        self.assertNotIn("retake_rounds", self.saved["1"])
        self.assertEqual(breeze.flagged_before(self.folder, self.saved), {"1"})
        self.assertTrue(any("ceiling of 2 retakes" in line for line in self.logs), self.logs)

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

    def test_verdicts_from_older_rules_are_judged_again_from_the_evidence(self):
        # The check file holds the evidence of a looping take under a verdict of "ok", from rules that passed it.
        self.flag_saved()
        data = lib.load_json(self.folder / takecheck.CHECK_FILE)
        self.assertEqual(data["rules"], takecheck.JUDGE_V)
        data["takes"]["1"].update(verdict="ok", reasons=[])
        stale = {**data, "rules": takecheck.JUDGE_V - 1}
        takecheck.write_check(self.folder / takecheck.CHECK_FILE, stale)

        # Without the script the stored verdict stands. With it the take is judged again and is one to take again,
        # and the file is left as it was.
        self.assertEqual(breeze.flagged_before(self.folder, self.saved), set())
        self.assertEqual(breeze.flagged_before(self.folder, self.saved, LINES, {}), {"1"})
        self.assertEqual(lib.load_json(self.folder / takecheck.CHECK_FILE), stale)

        # Verdicts under the current rules are taken as they are.
        takecheck.write_check(self.folder / takecheck.CHECK_FILE, data)
        self.assertEqual(breeze.flagged_before(self.folder, self.saved, LINES, {}), set())

        # A check that writes a file whose verdicts are stale leaves it marked stale, and a full check marks it
        # current.
        takecheck.write_check(self.folder / takecheck.CHECK_FILE, stale)
        entries = takecheck.check_entries({"0": self.saved["0"]}, LINES, {}, engine="parakeet", comfy_url="",
                                          log=self.logs.append)
        takecheck.save_entries(self.folder, entries, "parakeet")
        self.assertNotIn("rules", lib.load_json(self.folder / takecheck.CHECK_FILE))

    def test_a_flagged_take_from_before_takes_recorded_their_audio_is_taken_again(self):
        # Neither the take nor its verdict holds an audio hash, so they match on the key.
        for take in self.saved.values():
            del take["audio"]
        self.flag_saved()
        self.assertNotIn("audio", self.checked()["1"])
        self.assertEqual(breeze.flagged_before(self.folder, self.saved), {"1"})

    def paused(self) -> None:
        """Puts a pause of 0.94 s after the second word of line 0 into its saved evidence."""
        data = lib.load_json(self.folder / takecheck.CHECK_FILE)
        entry = data["takes"]["0"]
        entry["asr"]["starts"] = [0.0, 0.3] + [round(1.5 + 0.3 * n, 2) for n in range(len(entry["asr"]["starts"]) - 2)]
        entry["signal"]["silences"] = [[0.56, 1.5]]
        takecheck.write_check(self.folder / takecheck.CHECK_FILE, data)

    def test_the_join_gets_the_cuts_of_the_takes_whose_evidence_is_current(self):
        # The evidence of line 0 has a long pause between two words that no punctuation parts.
        self.saved["1"] = self.take(1, LINES[1]["text"], "a1")
        self.flag_saved()
        self.assertEqual(takecheck.join_cuts(self.folder, self.saved, LINES, {}), {})
        self.paused()

        # The cut leaves a quarter of a second of the pause, half on each side.
        cuts = takecheck.join_cuts(self.folder, self.saved, LINES, {})
        self.assertEqual(list(cuts), ["0"])
        (start, end), = cuts["0"]
        self.assertAlmostEqual(start, 0.56 + takecheck.PAUSE_KEEP / 2, delta=0.011)
        self.assertAlmostEqual(end, 1.5 - takecheck.PAUSE_KEEP / 2, delta=0.011)

        # A line whose direction is about its pace keeps its pauses, and so does a take whose audio changed since
        # it was heard.
        slow = [{**LINES[0], "direction": "slowly, hesitating"}, LINES[1]]
        self.assertEqual(takecheck.join_cuts(self.folder, self.saved, slow, {}), {})
        replaced = {**self.saved, "0": {**self.saved["0"], "audio": "other"}}
        self.assertEqual(takecheck.join_cuts(self.folder, replaced, LINES, {}), {})

        # Evidence in an older version gives no cut, and neither does a file that doesn't parse or isn't there.
        data = lib.load_json(self.folder / takecheck.CHECK_FILE)
        data["takes"]["0"]["signal"]["v"] = takecheck.SIGNAL_V - 1
        takecheck.write_check(self.folder / takecheck.CHECK_FILE, data)
        self.assertEqual(takecheck.join_cuts(self.folder, self.saved, LINES, {}), {})
        (self.folder / takecheck.CHECK_FILE).write_bytes(b"{")
        self.assertEqual(takecheck.join_cuts(self.folder, self.saved, LINES, {}), {})
        (self.folder / takecheck.CHECK_FILE).unlink()
        self.assertEqual(takecheck.join_cuts(self.folder, self.saved, LINES, {}), {})

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
        blocks = {"asr": {"v": takecheck.ASR_V, "engine": "parakeet", "heard": "x", "tokens": [" x"]},
                  "signal": {"v": takecheck.SIGNAL_V}}
        old = {"key": "k1", "audio": "a1", **blocks}
        self.assertTrue(takecheck.reusable(old, {"key": "k1", "audio": "a1"}, "parakeet"))
        self.assertFalse(takecheck.reusable(old, {"key": "k1", "audio": "b1"}, "parakeet"))
        self.assertFalse(takecheck.reusable(old, {"key": "k1", "audio": "a1"}, "qwen3"))
        # A take and an entry from before takes recorded their audio still match on the key.
        self.assertTrue(takecheck.reusable({"key": "k1", **blocks}, {"key": "k1"}, "parakeet"))
        self.assertFalse(takecheck.reusable({"key": "k1", **blocks}, {"key": "k1", "audio": "a1"}, "parakeet"))

    def test_saved_evidence_is_reused_only_in_the_current_versions(self):
        take = {"key": "k1", "audio": "a1"}
        asr = {"v": takecheck.ASR_V, "engine": "parakeet", "heard": "x", "tokens": [" x"]}
        signal = {"v": takecheck.SIGNAL_V}
        self.assertTrue(takecheck.reusable({**take, "asr": asr, "signal": signal}, take, "parakeet"))

        # An entry from before the blocks holds a transcript only, so its take is heard again.
        self.assertFalse(takecheck.reusable({**take, "asr": "parakeet", "heard": "x"}, take, "parakeet"))

        # So is a take whose asr or signal block is missing, of another version, or without tokens.
        for entry in ({"asr": asr}, {"asr": asr, "signal": {"v": takecheck.SIGNAL_V + 1}},
                      {"asr": {**asr, "v": takecheck.ASR_V - 1}, "signal": signal},
                      {"asr": {k: v for k, v in asr.items() if k != "tokens"}, "signal": signal}, {"signal": signal}):
            self.assertFalse(takecheck.reusable({**take, **entry}, take, "parakeet"), entry)

        # Qwen3-ASR gives a transcript only, so its entry needs no tokens and no signal block.
        heard = {"v": takecheck.ASR_V, "engine": "qwen3", "heard": "x"}
        self.assertTrue(takecheck.reusable({**take, "asr": heard}, take, "qwen3"))
        self.assertFalse(takecheck.reusable({**take, "asr": heard}, take, "parakeet"))

        # The signal version here is the one take_measure writes, which this module cannot import.
        source = (Path(takecheck.__file__).parent / "take_measure.py").read_text(encoding="utf-8")
        self.assertIn(f"\nSIGNAL_V = {takecheck.SIGNAL_V}\n", source)


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
        self.heard: list[int] = []
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
                self.heard.append(line)
                yield i, record(said if line == 1 else LINES[line]["text"])

        def ready(engine, comfy_url):
            if not self.parakeet_runs:
                raise ComfyError("sherpa-onnx is missing from the audio Python")

        def run(args, *a, **k):
            if len(args) < 4 or str(args[2]) != str(breeze.JOIN_SCRIPT):
                return real_run(args, *a, **k)
            job = json.loads(Path(args[3]).read_text(encoding="utf-8"))
            self.joined = {t["line"]: self.number_in(t["file"]) for t in job["takes"]}
            self.job = job
            Path(job["output"]).write_bytes(b"opus")
            summary = {"takes": len(job["takes"]), "seconds": 4.0, "sample_rate": self.RATE, "clicks": 0,
                       "cuts": sum(len(t["cuts"]) for t in job["takes"]), "cut_seconds": 0.0, "loudness": -18.0,
                       "true_peak": -1.2, "compressed": 0.0, "limited": 0.0}
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
                  mock.patch.object(takecheck, "records", hears),
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

    def test_the_join_job_gives_each_take_its_offset_its_cuts_and_its_gap(self):
        # Line 1 gets another speaker, whom the narrator's voice reads, and a soft direction, so its take is made
        # again. The evidence of line 0 gets a long pause between two of its words.
        script = [LINES[0], {**LINES[1], "speaker": "mira", "direction": "quietly"}]
        lib.save_json(self.chapter / "script.json", {"lines": script})
        data = self.check_file()
        entry = data["takes"]["0"]
        entry["asr"]["starts"] = [0.0, 0.3] + [round(1.5 + 0.3 * n, 2) for n in range(len(entry["asr"]["starts"]) - 2)]
        entry["signal"]["silences"] = [[0.56, 1.5]]
        takecheck.write_check(self.chapter / takecheck.CHECK_FILE, data)

        # The join job gives line 0 its cut and the gap before another speaker, and line 1 the offset of a soft
        # line and no cut.
        record = self.render()
        self.assertEqual(len(self.asked), 1)
        first, second = self.job["takes"]
        self.assertEqual((first["line"], first["offset"], first["gap"]), (0, 0.0, breeze.GAP_CHANGE))
        self.assertEqual(len(first["cuts"]), 1)
        self.assertAlmostEqual(first["cuts"][0][0], 0.56 + takecheck.PAUSE_KEEP / 2, delta=0.011)
        self.assertEqual((second["line"], second["offset"], second["cuts"]), (1, breeze.LEVEL_OFFSETS["soft"], []))

        # The record says which join made the audio and what it measured.
        self.assertEqual((record["join"], record["pauses_cut"], record["loudness"], record["true_peak"]),
                         (breeze.JOIN_VERSION, 1, -18.0, -1.2))

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

    def test_a_render_keeps_the_chapters_seed_unless_the_request_names_one(self):
        # The chapter was rendered with seed 3: its record says so, its takes carry that seed's keys, both say their
        # line, and no check has heard them.
        meta = lib.load_chapter("s", 1)[0]
        meta["render"]["seed"] = 3
        lib.save_chapter_meta("s", 1, meta)
        plan = breeze.plan_takes(LINES, {"narrator": 0}, {0: "narrator"}, {}, 3,
                                 {"narrator": hashlib.sha1(b"clip").hexdigest()}, "cpp")
        saved = lib.load_json(self.chapter / breeze.TAKES_FILE)["takes"]
        for p in plan:
            saved[str(p[0])]["key"] = p[5]
        pcm = self.pcm(7)
        breeze.write_wav(Path(saved["1"]["file"]), pcm, self.RATE)
        saved["1"]["audio"] = hashlib.sha1(pcm).hexdigest()[:16]
        breeze.save_takes(self.chapter / breeze.TAKES_FILE, saved, "cpp")
        (self.chapter / takecheck.CHECK_FILE).unlink()

        # A render that names no seed plans with seed 3, makes no take and records seed 3 again.
        self.asked.clear()
        record = breeze.render_breeze("s", 1, comfy_url="", log=self.logs.append)
        self.assertEqual((self.asked, record["seed"], record["generated_takes"]), ([], 3, 0))

        # A render that names seed 1 makes both lines again and records seed 1.
        record = breeze.render_breeze("s", 1, seed=1, comfy_url="", log=self.logs.append)
        self.assertEqual((len(self.asked), record["seed"], record["generated_takes"]), (2, 1, 2))

    def test_a_render_gives_the_script_its_emphasis_and_makes_those_lines_again(self):
        # The chapter's text sets a word of line 0 in italics, and the script doesn't have the field yet.
        (self.chapter / "text.md").write_text("The harbour was *quiet* that morning.\n\nNobody answered the door.\n",
                                              encoding="utf-8")
        before = (self.chapter / "script.json").read_bytes()

        # The list of what a render would change counts line 0 as a take to make, and writes nothing.
        self.assertEqual({k: v for k, v in breeze.render_changes("s", 1).items() if k in ("make", "retake")},
                         {"make": 1, "retake": 1})
        self.assertEqual((self.chapter / "script.json").read_bytes(), before)

        # A join-only run joins the takes the chapter has and leaves the script as their keys were made from it.
        self.render(join_only=True)
        self.assertEqual((self.asked, (self.chapter / "script.json").read_bytes()), ([], before))

        # A render writes the field, sends line 0 to the voice with its word between asterisks, and saves the take
        # under the key of that text. It also takes flagged line 1 again, as any render does.
        sent: list[str] = []
        real = breeze.CppServer.take

        def take(server, fields, wav, label, log):
            sent.append(fields["text"])
            return real(server, fields, wav, label, log)

        with mock.patch.object(breeze.CppServer, "take", take):
            self.render()
        script = lib.load_json(self.chapter / "script.json")
        self.assertEqual((script["emphasis"], script["lines"][0]["emphasis"]), (screenplay.EMPHASIS_V, ["quiet"]))
        self.assertNotIn("emphasis", script["lines"][1])
        self.assertEqual(sent, ["The harbour was *quiet* that morning.", "Nobody answered the door."])
        plan = breeze.plan_takes(script["lines"], {"narrator": 0}, {0: "narrator"}, {}, 1,
                                 {"narrator": hashlib.sha1(b"clip").hexdigest()}, "cpp")
        self.assertEqual((self.take(0)["key"], self.verdict(0)), (plan[0][5], "ok"))

        # Nothing is left to make, and a second render asks the voice for nothing.
        self.assertEqual({k: v for k, v in breeze.render_changes("s", 1).items() if k in ("make", "retake")},
                         {"make": 0, "retake": 0})
        self.render()
        self.assertEqual(self.asked, [])

    def check_file(self) -> dict:
        return lib.load_json(self.chapter / takecheck.CHECK_FILE)

    def test_the_list_of_what_a_render_would_change_agrees_with_the_render(self):
        # The fixture's chapter has a saved take the check flagged, and its record doesn't name any audio, so a
        # render would join the takes whatever their state.
        self.assertEqual(breeze.render_changes("s", 1),
                         {"spoken": 2, "make": 0, "retake": 1, "unheard": 0, "join": True, "engine": "breeze"})
        self.assertEqual(breeze.reusable_takes("s", 1), (1, 2))

        # A check file from older rules that passed the flagged take: the list and the chapter page judge it
        # again, and the scheduler's count reads the stored verdict.
        data = self.check_file()
        passed = json.loads(json.dumps(data))
        passed["takes"]["1"].update(verdict="ok", reasons=[])
        takecheck.write_check(self.chapter / takecheck.CHECK_FILE, {**passed, "rules": takecheck.JUDGE_V - 1})
        self.assertEqual(breeze.render_changes("s", 1)["retake"], 1)
        self.assertEqual(breeze.render_changes("s", 1, stored=True)["retake"], 0)
        self.assertEqual(breeze.reusable_takes("s", 1), (2, 2))
        from serial_audio.web import views
        script, meta = lib.load_json(self.chapter / "script.json"), {"render": {"check": {}}}
        self.assertEqual([(t["line"], t["verdict"]) for t in views._take_check(self.chapter, script, meta, {}, {})],
                         [(1, "flagged")])
        self.assertEqual(views._take_check(self.chapter, script, meta, {}), [])
        takecheck.write_check(self.chapter / takecheck.CHECK_FILE, data)

        # With another seed every take has another key, and a flagged take that is made again is not counted twice.
        self.assertEqual({k: v for k, v in breeze.render_changes("s", 1, 9).items() if k in ("make", "retake")},
                         {"make": 2, "retake": 0})

        # A take whose evidence is gone is one a render would hear, and neither make nor take again.
        data = self.check_file()
        del data["takes"]["0"]["signal"]
        lib.save_json(self.chapter / takecheck.CHECK_FILE, data)
        self.assertEqual(breeze.render_changes("s", 1)["unheard"], 1)
        self.assertEqual(breeze.reusable_takes("s", 1), (1, 2))

        # The render does what the list said, records the join's version, and leaves nothing to change.
        record = self.render()
        self.assertEqual((len(self.asked), record["check"]["retaken"], record["join"]), (1, [1], breeze.JOIN_VERSION))
        self.assertEqual(breeze.render_changes("s", 1),
                         {"spoken": 2, "make": 0, "retake": 0, "unheard": 0, "join": False, "engine": "breeze"})

        # A join that would cut what the chapter's audio wasn't cut makes a render due: here newer rules find a tail
        # in line 0's take that a silent run sets apart from its last word. A render then joins it with the cut, and
        # records it.
        data = self.check_file()
        data["takes"]["0"]["signal"].update(speech=[0.0, 2.6], silences=[[1.8, 2.3]], seconds=2.7)
        takecheck.write_check(self.chapter / takecheck.CHECK_FILE, {**data, "rules": takecheck.JUDGE_V - 1})
        self.assertTrue(breeze.render_changes("s", 1)["join"])
        self.render()
        self.assertEqual((len(self.asked), self.job["takes"][0]["cuts"][-1][1]), (0, 2.7))
        self.assertFalse(breeze.render_changes("s", 1)["join"])

        # A record from before renders recorded their cuts counts as having made every cut but one that ends a take.
        meta = lib.load_chapter("s", 1)[0]
        del meta["render"]["cuts"]
        lib.save_chapter_meta("s", 1, meta)
        self.assertTrue(breeze.render_changes("s", 1)["join"])
        data = self.check_file()
        data["takes"]["0"]["signal"].update(speech=[0.0, 1.8], silences=[])
        takecheck.write_check(self.chapter / takecheck.CHECK_FILE, {**data, "rules": takecheck.JUDGE_V - 1})
        self.assertFalse(breeze.render_changes("s", 1)["join"])

        # A chapter whose audio comes from an older join is one a render would change. A record from before
        # renders recorded the join's version counts as version 1.
        with mock.patch.object(breeze, "JOIN_VERSION", breeze.JOIN_VERSION + 1):
            self.assertTrue(breeze.render_changes("s", 1)["join"])
        meta = lib.load_chapter("s", 1)[0]
        del meta["render"]["join"]
        lib.save_chapter_meta("s", 1, meta)
        with mock.patch.object(breeze, "JOIN_VERSION", 1):
            self.assertFalse(breeze.render_changes("s", 1)["join"])
        with mock.patch.object(breeze, "JOIN_VERSION", 2):
            self.assertTrue(breeze.render_changes("s", 1)["join"])

        # A chapter that Drama 3 rendered says so, and its Breeze render would make every line.
        meta = lib.load_chapter("s", 1)[0]
        meta["render"] = {"codes_file": "s/ch0001_00001.safetensors", "audio": "audio/ch0001.opus", "seed": 1}
        lib.save_chapter_meta("s", 1, meta)
        (self.chapter / breeze.TAKES_FILE).unlink()
        self.assertEqual(breeze.render_changes("s", 1),
                         {"spoken": 2, "make": 2, "retake": 0, "unheard": 0, "join": True, "engine": "drama3"})

        # So would the Breeze render of a chapter that has its takes and Drama 3 audio.
        breeze.save_takes(self.chapter / breeze.TAKES_FILE, {"0": {"key": "x"}}, "cpp")
        self.assertTrue(breeze.render_changes("s", 1)["join"])

    def test_the_cast_tab_gets_the_voice_findings_of_every_chapter(self):
        from serial_audio.web import views

        # A render that found nothing records no finding, and a voice has none to show.
        self.render()
        self.assertNotIn("voice_findings", lib.load_chapter("s", 1)[0]["render"])
        self.assertEqual(views.voice_findings("s"), {})

        # A render record with a finding gives it to its voice, with the count of flagged takes. A damaged entry
        # is left out.
        meta = lib.load_chapter("s", 1)[0]
        meta["render"]["voice_findings"] = [{"voice": "narrator", "reason": "inserted", "lines": [1, 4, 6, 9], "of": 12},
                                            {"voice": "narrator"}, "junk"]
        lib.save_chapter_meta("s", 1, meta)
        self.assertEqual(views.voice_findings("s"),
                         {"narrator": [{"chapter": 1, "reason": "inserted", "takes": 4, "of": 12}]})

        # A render that takes nothing keeps the record's findings.
        self.assertEqual(self.render()["voice_findings"][0]["lines"], [1, 4, 6, 9])

    def test_saved_takes_without_evidence_are_heard_before_the_join(self):
        # The chapter's check is from before the evidence blocks. It holds transcripts only, and it passed both takes.
        takes = lib.load_json(self.chapter / breeze.TAKES_FILE)["takes"]
        old = {k: {"key": t["key"], "audio": t["audio"], "asr": "parakeet", "heard": LINES[int(k)]["text"],
                   "words": 4, "sub": 0, "del": 0, "ins": 0, "wer": 0.0, "verdict": "ok"} for k, t in takes.items()}
        lib.save_json(self.chapter / takecheck.CHECK_FILE, {"asr": "parakeet", "takes": old})
        self.assertEqual((breeze.flagged_before(self.chapter, takes), breeze.unheard_before(self.chapter, takes)),
                         (set(), {"0", "1"}))
        self.assertEqual(breeze.reusable_takes("s", 1, 1, "cpp"), (2, 2))

        # A render hears both saved takes while breeze-server is up. Line 1 loops, so it is taken again in the same
        # render, and its new take is heard too.
        self.heard.clear()
        record = self.render()
        self.assertEqual((self.heard, self.asked), ([0, 1, 1], [self.seed_of_try(breeze.RETAKES + 1)]))
        self.assertEqual((record["generated_takes"], record["check"]["retaken"], record["check"]["flagged"]),
                         (0, [1], []))
        self.assertTrue(any("2 saved takes don't have evidence" in line for line in self.logs), self.logs)
        self.assertEqual({k: (e["asr"]["v"], e["signal"]["v"]) for k, e in self.check_file()["takes"].items()},
                         {k: (takecheck.ASR_V, takecheck.SIGNAL_V) for k in ("0", "1")})

        # The next render has nothing to hear and nothing to take.
        self.heard.clear()
        self.render()
        self.assertEqual((self.heard, self.asked), ([], []))

    def test_a_line_that_went_through_every_round_is_heard_and_left_alone(self):
        # Line 1 loops, an earlier render took it through both rounds, and its check is from before the evidence
        # blocks.
        takes = lib.load_json(self.chapter / breeze.TAKES_FILE)["takes"]
        takes["1"].update(retake_rounds=breeze.CHECK_RETAKES, takes_tried=9)
        breeze.save_takes(self.chapter / breeze.TAKES_FILE, takes, "cpp")
        old = {k: {"key": t["key"], "audio": t["audio"], "asr": "parakeet", "heard": LOOP if k == "1" else LINES[0]["text"],
                   "words": 4, "sub": 0, "del": 0, "ins": 4 if k == "1" else 0, "wer": 0.0,
                   "verdict": "flagged" if k == "1" else "ok"} for k, t in takes.items()}
        lib.save_json(self.chapter / takecheck.CHECK_FILE, {"asr": "parakeet", "takes": old})

        # The render hears both takes for their evidence and takes neither again.
        self.heard.clear()
        record = self.render()
        self.assertEqual((self.heard, self.asked, self.joined[1]), ([0, 1], [], SAVED_TAKE))
        self.assertEqual((self.take(1)["retake_rounds"], self.take(1)["takes_tried"], record["check"]["flagged"]),
                         (breeze.CHECK_RETAKES, 9, [1]))

    def test_a_second_check_hears_only_the_takes_without_evidence(self):
        def check(**kw) -> list[int]:
            self.heard.clear()
            takecheck.check_takes("s", 1, comfy_url="", log=self.logs.append, **kw)
            return list(self.heard)

        # The fixture's check holds both blocks for both takes, so a check hears nothing and judges both again.
        self.assertEqual(check(), [])
        self.assertEqual({k: e["verdict"] for k, e in self.check_file()["takes"].items()}, {"0": "ok", "1": "flagged"})
        self.assertEqual(self.check_file()["voices"]["narrator"]["n"], 2)

        # A take whose signal block is gone is heard again, alone.
        data = self.check_file()
        del data["takes"]["0"]["signal"]
        lib.save_json(self.chapter / takecheck.CHECK_FILE, data)
        self.assertEqual(check(), [0])
        self.assertEqual(self.check_file()["takes"]["0"]["signal"]["v"], takecheck.SIGNAL_V)

        # With redo every take is heard.
        self.assertEqual(check(redo=True), [0, 1])

    def test_measure_takes_reports_each_voice_and_writes_nothing(self):
        from serial_audio import take_report

        def files() -> dict:
            return {p: p.read_bytes() for p in self.chapter.parents[1].rglob("*") if p.is_file()}

        # The chapter has one take that says its line and one that loops. Chapter 2 does not exist.
        before = files()
        report = take_report.measure_takes("s", [1, 2], log=self.logs.append)
        self.assertEqual((report["chapters"], list(report["skipped"]), report["takes"], report["heard"],
                          report["without_evidence"]), ([1], [2], 2, 0, 0))
        self.assertEqual(report["verdicts"], {"ok": 1, "suspect": 0, "unreadable": 0, "flagged": 1})
        self.assertEqual(report["reasons"], {"inserted": [[1, 1]]})

        # The voice's block counts its takes and reasons and gives the spread of each measure.
        narrator = report["voices"]["narrator"]
        self.assertEqual((narrator["takes"], narrator["verdicts"], narrator["reasons"]),
                         (2, {"flagged": 1, "ok": 1}, {"inserted": 1}))
        self.assertEqual((narrator["measures"]["level"]["n"], narrator["measures"]["level"]["p50"]), (2, -26.0))
        self.assertEqual(narrator["measures"]["errors"]["max"], 3)
        self.assertIsNone(narrator["measures"]["swing"])

        # Each take has a row, and the table names the flagged take.
        row = report["rows"][1]
        self.assertEqual((row["chapter"], row["line"], row["voice"], row["verdict"], row["said"], row["heard"]),
                         (1, 1, "narrator", "flagged", LINES[1]["text"], LOOP))
        self.assertIn("reason inserted: 1 takes: 1:1", take_report.table(report))
        self.assertIn("narrator: 2 takes, flagged 1, ok 1, reasons: inserted 1", take_report.table(report))

        # A take without evidence is heard for the report, or left out of it and counted when the report is to
        # read stored evidence only. Neither writes a file.
        data = self.check_file()
        del data["takes"]["0"]["signal"]
        lib.save_json(self.chapter / takecheck.CHECK_FILE, data)
        before = files()
        self.heard.clear()
        stored = take_report.measure_takes("s", [1], log=self.logs.append, stored=True)
        self.assertEqual((stored["heard"], stored["without_evidence"], len(stored["rows"]), self.heard), (0, 1, 1, []))
        heard = take_report.measure_takes("s", [1], log=self.logs.append)
        self.assertEqual((heard["heard"], heard["without_evidence"], len(heard["rows"]), self.heard), (1, 0, 2, [0]))
        self.assertEqual(files(), before)

        # Evidence that Qwen3-ASR stored counts in a report of stored evidence, and that take has no signal
        # measures. A report that hears takes hears it with Parakeet.
        data["takes"]["0"]["asr"] = {"v": takecheck.ASR_V, "engine": "qwen3", "heard": LINES[0]["text"]}
        lib.save_json(self.chapter / takecheck.CHECK_FILE, data)
        self.heard.clear()
        stored = take_report.measure_takes("s", [1], log=self.logs.append, stored=True)
        self.assertEqual((stored["without_evidence"], len(stored["rows"]), stored["rows"][0]["level"], self.heard),
                         (0, 2, None, []))
        self.assertEqual((take_report.measure_takes("s", [1], log=self.logs.append)["heard"], self.heard), (1, [0]))


class FakeTakes:
    """A breeze-server whose take lasts the seconds listed for its turn, and that records each seed and text."""

    sample_rate = 24000

    def __init__(self, seconds: list[float]):
        self.seconds, self.seeds, self.texts = list(seconds), [], []

    def take(self, fields, wav, label, log):
        self.seeds.append(fields["seed"])
        self.texts.append(fields["text"])
        return b"\x00\x00" * int(self.sample_rate * self.seconds[len(self.seeds) - 1])


LONG = ("The castle towered over him, and a mere beat of her wings blew away the tents and the flames. He could not "
        "see all of her at once, because she filled the sky from the hills to the river, and the sound of her "
        "breathing shook the ground under his feet. Nobody in the camp had ever seen anything like it, and nobody who "
        "saw it that night would ever forget it.")


class LongLineTest(unittest.TestCase):
    def test_a_long_line_splits_at_its_sentences_and_clauses(self):
        pieces = breeze.clause_pieces(LONG)
        self.assertEqual(" ".join(pieces), LONG)
        self.assertEqual([breeze.count_words(p) for p in pieces], [19, 32, 21])
        self.assertTrue(all(p[-1] in ".," for p in pieces), pieces)

        # A sentence over the limit splits after a comma, and short sentences share a piece.
        self.assertEqual(breeze.clause_pieces("One two three, four five six. Seven eight.", limit=4),
                         ["One two three,", "four five six.", "Seven eight."])
        self.assertEqual(breeze.clause_pieces("Yes. No. Maybe so.", limit=4), ["Yes. No. Maybe so."])

        # Words without a place to split stay one piece.
        self.assertEqual(breeze.clause_pieces("one two three four five six", limit=4), ["one two three four five six"])

        # A sentence ends before a closing quote too, and a dash splits a long sentence with or without a space
        # after it.
        dash, close = chr(0x2014), chr(0x2019)
        self.assertEqual(breeze.clause_pieces(f"He said no.{close} Then she left the room.", limit=5),
                         [f"He said no.{close}", "Then she left the room."])
        self.assertEqual(breeze.clause_pieces(f"one two three{dash}four five six{dash} seven eight nine", limit=4),
                         [f"one two three{dash}", f"four five six{dash}", "seven eight nine"])

        # A piece of under a fifth of the limit is too short to take by itself, so it joins its shorter neighbour.
        rest = " ".join(["word"] * 42)
        pieces = breeze.clause_pieces(f"Otherwise, {rest}. " + " ".join(["more"] * 23) + ".")
        self.assertEqual([breeze.count_words(p) for p in pieces], [43, 23])
        self.assertEqual(" ".join(pieces), f"Otherwise, {rest}. " + " ".join(["more"] * 23) + ".")

    def test_a_flagged_long_line_is_taken_again_in_pieces(self):
        # 72 words are over LONG_LINE, so the retake asks for three pieces with seeds of its round and joins them
        # with a gap between each.
        self.assertGreater(breeze.count_words(LONG), breeze.LONG_LINE)
        server = FakeTakes([7.0, 12.0, 9.0])
        logs: list[str] = []
        got, pieces = breeze.retake_line(server, b"", "ref", LONG, "", 1000, 7, logs.append, 1)
        self.assertEqual((pieces, server.texts), (3, breeze.clause_pieces(LONG)))
        self.assertEqual(len(set(server.seeds)), 1)
        self.assertNotEqual(server.seeds[0], 1000)
        self.assertEqual(len(got["pcm"]) / 2 / server.sample_rate, 7.0 + 12.0 + 9.0 + 2 * breeze.PIECE_GAP)
        self.assertEqual((got["tries"], got["fits"]), (3, True))
        self.assertTrue(any("in 3 pieces" in line for line in logs), logs)

        # A piece whose length doesn't fit is taken again by itself, and the take says so when none of its tries fits.
        server = FakeTakes([7.0, 60.0, 60.0, 60.0, 9.0])
        got, _ = breeze.retake_line(server, b"", "ref", LONG, "", 1000, 7, lambda *_: None, 1)
        self.assertEqual((len(server.texts), got["tries"], got["fits"]), (5, 5, False))

        # A line of ordinary length is one request for the whole line.
        server = FakeTakes([2.0])
        got, pieces = breeze.retake_line(server, b"", "ref", "Nobody answered the door.", "", 1000, 7, lambda *_: None, 1)
        self.assertEqual((pieces, server.texts), (1, ["Nobody answered the door."]))


OTHER_LONG = LONG.replace("castle", "tower")
LOST = " ".join(LONG.split()[:19] + LONG.split()[51:])


class RenderLongLineTest(unittest.TestCase):
    """render_breeze on a chapter of two lines over LONG_LINE words, in the fixture of RenderFlaggedTest. The saved
    take of line 1 lost its middle, and the check flagged it. The saved take of line 0 says its line. A take of the
    fake breeze-server lasts as long as its words call for, and test.words records the word count of each request."""

    RATE = RenderFlaggedTest.RATE
    number_in, render, take, verdict = (RenderFlaggedTest.number_in, RenderFlaggedTest.render, RenderFlaggedTest.take,
                                        RenderFlaggedTest.verdict)

    def setUp(self):
        me = sys.modules[__name__]
        long_lines = [{"speaker": "narrator", "text": OTHER_LONG}, {"speaker": "narrator", "text": LONG}]
        for p in (mock.patch.object(me, "LINES", long_lines), mock.patch.object(me, "LOOP", LOST)):
            p.start()
            self.addCleanup(p.stop)
        RenderFlaggedTest.setUp(self)
        self.words: list[int] = []
        test = self

        class Server:
            sample_rate = self.RATE

            def __init__(self, log_path, log):
                pass

            def refuse_if_busy(self):
                pass

            def start(self):
                pass

            def take(self, fields, wav, label, log):
                count = breeze.count_words(fields["text"])
                test.asked.append(fields["seed"])
                test.words.append(count)
                return struct.pack("<i", fields["seed"]) * int(self.sample_rate * (0.75 * count + 2.0) / 2)

            def __exit__(self, *exc):
                pass

        patch = mock.patch.object(breeze, "CppServer", Server)
        patch.start()
        self.addCleanup(patch.stop)

    def pcm(self, number: int) -> bytes:
        """Thirty seconds of audio that holds number, a length a line of LONG's words may have."""
        return struct.pack("<i", number) * (self.RATE * 15)

    def test_a_flagged_long_line_is_taken_again_in_pieces_and_a_passing_one_is_left_alone(self):
        # The render takes line 1 again in three pieces and keeps the new take. Line 0 passes, so nothing asks for it.
        self.assertEqual((self.verdict(0), self.verdict(1)), ("ok", "flagged"))
        record = self.render()
        take = self.take(1)
        self.assertEqual(self.words, [19, 32, 21])
        self.assertEqual((take["pieces"], take["retaken"], record["check"]["retaken"]), (3, 1, [1]))
        self.assertEqual((self.verdict(1), self.joined[0]), ("ok", self.voice_seed))

        # The pieces are longer together than one take of the line may be, and the next render keeps the take.
        self.assertGreater(breeze.wav_seconds(Path(take["file"])), breeze.take_bounds(breeze.count_words(LONG))[1])
        self.words.clear()
        self.render()
        self.assertEqual(self.words, [])

        # Without its count of pieces the same take is out of range, and a render makes the line again in one take.
        saved = lib.load_json(self.chapter / breeze.TAKES_FILE)["takes"]
        del saved["1"]["pieces"]
        breeze.save_takes(self.chapter / breeze.TAKES_FILE, saved, "cpp")
        self.render()
        self.assertEqual(self.words[0], breeze.count_words(LONG))


class NamesHeardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(lib, "LIBRARY", Path(self.tmp.name) / "library")
        patch.start()
        self.addCleanup(patch.stop)
        lib.save_serial("s", {"title": "s"})
        lib.save_bible("s", {"characters": {}, "pronunciations": {"Eole": {"respelling": "AY-ohl"},
                                                                  "Lauriane": {"say": "Laurie Anne"}}})

    def chapter(self, number: int, names: list[dict | None], takes: bool = True) -> Path:
        """A chapter whose check heard the names of names[i] in take i. None is an entry from before names."""
        folder = lib.chapter_dir("s", number)
        folder.mkdir(parents=True)
        lib.save_chapter_meta("s", number, {"number": number})
        if takes:
            lib.save_json(folder / breeze.TAKES_FILE,
                          {"takes": {str(i): {"key": f"k{i}", "audio": f"a{i}"} for i in range(len(names))}})
            lib.save_json(folder / takecheck.CHECK_FILE, {"takes": {
                str(i): {"key": f"k{i}", "audio": f"a{i}", **({} if found is None else {"names": found})}
                for i, found in enumerate(names)}})
        return folder

    def test_the_names_of_a_serial_are_listed_with_the_spellings_heard(self):
        from serial_audio.web import views
        self.chapter(1, [{"Eole": ["eol", "eoli"], "Lauriane": ["laurie anne"]}, {"Eole": ["eol's"]}, {}])
        two = self.chapter(2, [{"Eole": ["aol"], "Casval": ["casval"]}, {"Lauriane": ["lorianne"]}])
        self.chapter(3, [None, None])
        self.chapter(4, [], takes=False)

        # The name with the most spellings is first. A possessive is the same spelling, and a say-as name counts as
        # written when it is heard as the voice is sent it.
        got = views.names_heard("s")
        self.assertEqual(got["chapters"], {"with": [1, 2], "without": [3], "judged": []})
        self.assertEqual([(t["term"], t["heard"], t["spellings"], t["as_written"]) for t in got["terms"]],
                         [("Eole", 4, 3, 0), ("Lauriane", 2, 2, 1), ("Casval", 1, 1, 1)])
        eole, lauriane, _ = got["terms"]
        self.assertEqual((eole["top"], eole["chapters"], eole["respelling"], eole["say"]),
                         ([["eol", 2], ["eoli", 1], ["aol", 1]], [1, 2], "AY-ohl", ""))
        self.assertEqual((lauriane["say"], lauriane["top"][0]), ("Laurie Anne", ["laurie anne", 1]))

        # An entry for audio that was replaced doesn't count, and a changed file is read again.
        takes = lib.load_json(two / breeze.TAKES_FILE)
        takes["takes"]["0"]["audio"] = "new"
        lib.save_json(two / breeze.TAKES_FILE, takes)
        got = views.names_heard("s")
        self.assertEqual([(t["term"], t["heard"]) for t in got["terms"]], [("Eole", 3), ("Lauriane", 2)])

        # A check file that doesn't parse puts its chapter with the ones that don't have names yet.
        (two / takecheck.CHECK_FILE).write_bytes(b"{")
        self.assertEqual(views.names_heard("s")["chapters"], {"with": [1], "without": [2, 3], "judged": []})

    def test_a_name_whose_takes_have_phones_is_listed_by_the_ways_it_was_said(self):
        from serial_audio import pronounce
        from serial_audio.web import views
        for target, switch in ((takecheck, "NAME_FLAGS"), (pronounce, "SCREENS")):
            patcher = mock.patch.object(target, switch, True)
            patcher.start()
            self.addCleanup(patcher.stop)
        one = self.chapter(1, [{"Eole": ["aol"]}] * 6 + [{"Lauriane": ["laurie anne"]}])
        check = lib.load_json(one / takecheck.CHECK_FILE)
        sayings = ["eɪoʊl", "eɪoʊl", "eɪɔl", "kæzvɔl", "kæzvɔl", "hɛli"]
        for i, name in enumerate(sayings):
            check["takes"][str(i)]["said"] = {"Eole": [[name, f"nd{name}sɛd"]]}
        lib.save_json(one / takecheck.CHECK_FILE, check)
        lib.save_chapter_meta("s", 1, {"number": 1, "render": {"name_findings": [{"term": "Eole", "lines": [3, 4, 5],
                                                                                   "of": 6}]}})

        # Without a picked saying the first way is the usual one. The sayings sort by sound though the recognizer
        # wrote one spelling, and the name with the most sayings off its first way is first.
        got = views.names_heard("s")
        eole, lauriane = got["terms"]
        self.assertEqual((eole["term"], eole["spellings"], eole["held"]), ("Eole", 1, False))
        self.assertEqual(eole["ways"], {"said": 6, "usual": 3, "count": 3,
                                        "ways": [["eɪoʊl", 3], ["kæzvɔl", 2], ["hɛli", 1]]})
        self.assertNotIn("ways", lauriane)
        # The name finding in a render record is not shown.
        self.assertNotIn("findings", eole)

        # A name with a say-as is held to its spelling, and the usual saying of the spelling leads the ways.
        bible = lib.load_bible("s")
        bible["pronunciations"]["Eole"].update(say="Cazvol", said="kæzvɔl")
        lib.save_bible("s", bible)
        got = views.names_heard("s")
        eole = got["terms"][0]
        self.assertEqual((eole["held"], eole["ways"]["usual"], eole["ways"]["ways"][0]), (True, 2, ["kæzvɔl", 2]))

        # The takes that add a sound to the spelling are counted over the chapters whose check was judged by the
        # rules as they are now, with the name's spelling as it is now. This chapter's was not, so it counts for
        # nothing yet.
        self.assertEqual((eole["adds"], got["chapters"]["judged"]), ([0, 0], []))
        check = lib.load_json(one / takecheck.CHECK_FILE)
        check.update(rules=takecheck.JUDGE_V, names=takecheck.picked_spellings(bible))
        for i in (0, 1, 2, 5):
            check["takes"][str(i)]["missed"] = ["Eole"]
        lib.save_json(one / takecheck.CHECK_FILE, check)
        got = views.names_heard("s")
        self.assertEqual((got["terms"][0]["term"], got["terms"][0]["adds"], got["chapters"]["judged"]),
                         ("Eole", [4, 6], [1]))
        self.assertEqual(got["terms"][1]["adds"], [0, 0])
        # A spelling is unsteady when its takes that add a sound are BREAKER_MIN or more and over BREAKER_SHARE
        # of the takes that say it.
        self.assertEqual([t["unsteady"] for t in got["terms"]], [True, False])
        for limit, value in (("BREAKER_MIN", 5), ("BREAKER_SHARE", Fraction(4, 6))):
            with mock.patch.object(takecheck, limit, value):
                self.assertFalse(views.names_heard("s")["terms"][0]["unsteady"], limit)
        # A name's count waits for the chapters that say it whose check was not judged with its spelling. The
        # one take of Lauriane has no phones, so its chapter still waits for it.
        self.assertEqual([t["waiting"] for t in got["terms"]], [0, 1])

        # A new spelling for another name leaves this name's count, though the chapter's check is no longer
        # current. A new spelling for this name puts its chapter back to waiting.
        other = lib.load_bible("s")
        other["pronunciations"]["Lauriane"]["say"] = "Lorry Ann"
        lib.save_bible("s", other)
        got = views.names_heard("s")
        eole = got["terms"][0]
        self.assertEqual((eole["adds"], eole["unsteady"], eole["waiting"], got["chapters"]["judged"]),
                         ([4, 6], True, 0, []))
        other["pronunciations"]["Eole"]["say"] = "Kazvol"
        lib.save_bible("s", other)
        eole = next(t for t in views.names_heard("s")["terms"] if t["term"] == "Eole")
        self.assertEqual((eole["adds"], eole["unsteady"], eole["waiting"]), ([0, 0], False, 1))
        lib.save_bible("s", bible)

        # A candidate that was screened shows its count, and one that is not steady is not offered. A count from
        # an older screen is not shown, and its candidate is offered as an unscreened one is. So is the name as
        # written, whatever its count, because the rule doesn't judge a name as written.
        screen = pronounce.SCREEN_V
        bible["pronunciations"]["Eole"]["options"] = {"line": {"chapter": 1}, "candidates": [
            {"say": "Eole", "said": "iol", "steady": [2, 8], "typical": False, "screen": screen},
            {"say": "Cazvol", "said": "kæzvɔl", "steady": [8, 8], "typical": True, "screen": screen},
            {"say": "Ay-ole", "said": "eɪoʊl", "steady": [5, 8], "typical": True, "screen": screen},
            {"say": "Ay-ohl", "said": "eɪoʊl", "steady": [3, 8], "typical": True}]}
        lib.save_bible("s", bible)
        shown = views.names_heard("s")["terms"][0]["options"]["candidates"]
        self.assertEqual([(c["say"], c["steady"], c["offered"]) for c in shown],
                         [("Eole", None, True), ("Cazvol", [8, 8], True), ("Ay-ole", [5, 8], False),
                          ("Ay-ohl", None, True)])
        self.assertTrue(views.names_heard("s")["screens"])

        # While the rule and the screen are off, no name is held, no count is shown and every option is offered.
        # The ways are still listed.
        with mock.patch.object(takecheck, "NAME_FLAGS", False), mock.patch.object(pronounce, "SCREENS", False):
            got = views.names_heard("s")
            eole = got["terms"][0]
            self.assertEqual((got["screens"], eole["held"], eole["ways"]["ways"][0]), (False, False, ["kæzvɔl", 2]))
            self.assertNotIn("adds", eole)
            self.assertNotIn("unsteady", eole)
            self.assertEqual([(c["steady"], c["offered"]) for c in eole["options"]["candidates"]], [(None, True)] * 4)


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

        # The line of a render that also hears saved takes doesn't say "new", and it reads the same way.
        p = render_progress(self.HEAD + "   checking the 189 takes with Parakeet before the join...\n", None, 0.0, None)
        self.assertEqual((p["phase"], p["prechecked"], p["eta"]), ("checking", True, 189 * 0.5 + 60))

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


def timed(said: str, start: float = 0.0) -> dict:
    """An asr block for a take that says `said` at 0.3 s a word from start, each word 0.24 s long."""
    tokens, starts, at = [], [], start
    for word in said.split():
        mark = word[-1] if word[-1] in ",.?!" else ""
        tokens += [f" {word.rstrip(',.?!')}"] + ([mark] if mark else [])
        starts += [round(at, 2)] + ([round(at + 0.24, 2)] if mark else [])
        at += 0.3
    return {"v": takecheck.ASR_V, "engine": "parakeet", "heard": said, "tokens": tokens, "starts": starts,
            "durations": [0.24] * len(tokens), "logprobs": [0.0] * len(tokens)}


def unit(*values: float) -> list[float]:
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


class ExtraBlocksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def folder(self, name: str, files: tuple[str, ...]) -> Path:
        path = self.root / name
        path.mkdir()
        for f in files:
            (path / f).write_bytes(b"x")
        return path

    def test_a_machine_gathers_the_blocks_whose_model_folders_it_has(self):
        vad = self.folder("vad", parakeet_asr.VAD_FILES)
        moonshine = self.folder("moonshine", parakeet_asr.ASR2_FILES[:1])
        parakeet = self.folder("parakeet", parakeet_asr.MODEL_FILES)
        loads = []

        def load(folders):
            loads.append(dict(folders))
            return {"vad"}, {}

        with mock.patch.object(machine, "VAD_MODEL", vad), mock.patch.object(machine, "ASR2_MODEL", moonshine), \
                mock.patch.object(machine, "SPEAKER_MODEL", self.root / "none"), \
                mock.patch.object(machine, "PHONE_MODEL", self.root / "nophone"), \
                mock.patch.object(machine, "PARAKEET_MODEL", parakeet), mock.patch.dict(asr._LOADS, clear=True), \
                mock.patch.object(asr, "_load", load):
            # A block counts when its folder holds all its files and its model loads. A missing folder leaves its
            # block out, and so does a folder without all its files, which is a broken install. The load is tried
            # once in a process for the same folders.
            self.assertEqual(REAL_EXTRA_BLOCKS(), {"vad"})
            self.assertEqual(REAL_EXTRA_BLOCKS(), {"vad"})
            self.assertEqual(loads, [{"vad": str(vad)}])

            # The broken install doesn't stop the take check, and model_problems names what the folder lacks and
            # the variable that moves it. A missing folder is not a problem.
            with mock.patch.object(asr, "extra_blocks", REAL_EXTRA_BLOCKS), \
                    mock.patch.object(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")):
                asr.check_ready("parakeet", "")
            problems = asr.model_problems()
            self.assertEqual(sorted(problems), ["asr2"])
            self.assertIn("SERIAL_AUDIO_ASR2_MODEL", problems["asr2"])
            self.assertIn(parakeet_asr.ASR2_FILES[1], problems["asr2"])

            # ZIPA's folder gets the same check, and a whole one adds the phone block to the load.
            phone = self.folder("phone", parakeet_asr.PHONE_FILES[:1])
            with mock.patch.object(machine, "PHONE_MODEL", phone):
                self.assertIn("SERIAL_AUDIO_PHONE_MODEL", asr.model_problems()["phone"])
            whole = self.folder("zipa", parakeet_asr.PHONE_FILES)
            with mock.patch.object(machine, "PHONE_MODEL", whole), mock.patch.dict(asr._LOADS, clear=True), \
                    mock.patch.object(asr, "_load", lambda folders: (set(folders), {})):
                self.assertEqual(REAL_EXTRA_BLOCKS(), {"vad", "phone"})

        # A model whose files are there but don't load leaves its block out, and model_problems names the error.
        # A load tried for other folders isn't reported.
        with mock.patch.object(machine, "VAD_MODEL", vad), mock.patch.object(machine, "ASR2_MODEL", self.root / "no"), \
                mock.patch.object(machine, "SPEAKER_MODEL", self.root / "none"), mock.patch.dict(asr._LOADS, clear=True), \
                mock.patch.object(machine, "PHONE_MODEL", self.root / "nophone"), \
                mock.patch.object(asr, "_load", lambda folders: (set(), {"vad": "RuntimeError: bad model"})):
            self.assertEqual(asr.model_problems(), {})
            self.assertEqual(REAL_EXTRA_BLOCKS(), set())
            self.assertIn("doesn't load (RuntimeError: bad model)", asr.model_problems()["vad"])
            with mock.patch.object(machine, "VAD_MODEL", self.folder("vad2", parakeet_asr.VAD_FILES)):
                self.assertEqual(asr.model_problems(), {})

    def test_the_default_folders_lie_beside_parakeets(self):
        # A node that names its Parakeet folder finds the other models beside it, and each has its own variable.
        code = ("import json; from serial_audio import machine; print(json.dumps([str(machine.VAD_MODEL), "
                "str(machine.SPEAKER_MODEL), str(machine.ASR2_MODEL), str(machine.PHONE_MODEL)]))")
        env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("SERIAL_AUDIO_")}
        models = self.root / "models"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                             env={**env, "SERIAL_AUDIO_PARAKEET_MODEL": str(models / "parakeet"),
                                  "SERIAL_AUDIO_SPEAKER_MODEL": str(self.root / "elsewhere")},
                             cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(json.loads(out.stdout), [str(models / "silero-vad-v5"), str(self.root / "elsewhere"),
                                                  str(models / "moonshine-base-en-2026-02-27"),
                                                  str(models / "zipa-small-crctc-300k")])

    def test_the_child_is_asked_for_the_blocks_this_machine_can_gather(self):
        script = self.root / "child.py"
        script.write_text("import json, sys\njob = json.loads(sys.stdin.read())\n"
                          "for i, f in enumerate(job['files']):\n"
                          "    said = {'text': 'x'} if 'asr' in job['blocks'] else {}\n"
                          "    print(json.dumps({'index': i, **said, 'job': {k: v for k, v in job.items() "
                          "if k not in ('files', 'model')}}))\n"
                          "print(json.dumps({'done': len(job['files'])}))\n", encoding="utf-8")
        vad = self.folder("vad", parakeet_asr.VAD_FILES)
        with mock.patch.object(asr, "PARAKEET_SCRIPT", script), \
                mock.patch.object(machine, "AUDIO_PYTHON", Path(sys.executable)), \
                mock.patch.object(machine, "VAD_MODEL", vad), mock.patch.object(asr, "extra_blocks", lambda: {"vad"}):
            # A block this machine doesn't have the model for is left out, and the job names the folder of each
            # block it asks for.
            got = list(asr.records([Path("a.wav")], engine="parakeet", comfy_url="",
                                   blocks=("asr", "signal", "vad", "speaker")))
            self.assertEqual(got[0][1]["job"], {"threads": asr.PARAKEET_THREADS, "blocks": ["asr", "signal", "vad"],
                                                "vad": str(vad)})

            # A hearing that asks for none of the other blocks doesn't try their models.
            with mock.patch.object(asr, "extra_blocks", mock.Mock(side_effect=AssertionError("tried the models"))):
                list(asr.records([Path("a.wav")], engine="parakeet", comfy_url=""))

            # By default the child gathers Parakeet's own two blocks. A hearing without the transcript gets no text.
            got = list(asr.records([Path("a.wav")], engine="parakeet", comfy_url=""))
            self.assertEqual(got[0][1]["job"]["blocks"], ["asr", "signal"])
            got = list(asr.records([Path("a.wav")], engine="parakeet", comfy_url="", blocks=("vad",)))
            self.assertNotIn("text", got[0][1])

    def test_a_saved_take_without_the_new_blocks_is_heard_for_those_alone(self):
        take = {"key": "k1", "audio": "a1", "file": str(self.root / "a.wav")}
        asr_block = {"v": takecheck.ASR_V, "engine": "parakeet", "heard": "x", "tokens": [" x"]}
        old = {"key": "k1", "audio": "a1", "asr": asr_block, "signal": {"v": takecheck.SIGNAL_V, "level": -20.0}}
        vad = {"v": takecheck.VAD_V, "engine": "silero-vad-v5", "segments": [[0.0, 1.0]]}

        # Without the models, a Parakeet entry with both of its blocks is complete.
        self.assertEqual(takecheck.missing(old, take, "parakeet"), ())
        # With them, only the blocks it lacks are heard, and a block of another version counts as lacking.
        extras = {"vad", "speaker"}
        self.assertEqual(takecheck.missing(old, take, "parakeet", extras), ("vad", "speaker"))
        self.assertEqual(takecheck.missing({**old, "vad": vad}, take, "parakeet", extras), ("speaker",))
        self.assertEqual(takecheck.missing({**old, "vad": {**vad, "v": 0}}, take, "parakeet", extras),
                         ("vad", "speaker"))
        self.assertFalse(takecheck.reusable(old, take, "parakeet", extras))
        # Other audio or an old transcript is heard in full, and Qwen3-ASR needs only its transcript.
        self.assertEqual(takecheck.missing(old, {**take, "audio": "b1"}, "parakeet", extras),
                         ("asr", "signal", "vad", "speaker"))
        self.assertEqual(takecheck.missing({**old, "asr": {**asr_block, "v": 1}}, take, "parakeet", extras),
                         ("asr", "signal", "vad", "speaker"))
        self.assertEqual(takecheck.missing({**old, "asr": {**asr_block, "engine": "qwen3"}}, take, "qwen3", extras),
                         ())

        # The hearing asks the child for the missing block only and keeps the saved transcript and signal.
        asked = []

        def hears(files, *, engine, comfy_url, blocks=("asr", "signal")):
            asked.append(tuple(blocks))
            for i, _ in enumerate(files):
                yield i, ({"vad": vad} if blocks == ("vad",) else {**record("y"), "vad": vad})

        with mock.patch.object(takecheck, "records", hears), mock.patch.object(takecheck, "check_ready", lambda *a: None), \
                mock.patch.object(takecheck, "extra_blocks", lambda: {"vad"}):
            got = takecheck.hear([("1", take)], "parakeet", "", print, keep={"1": old})
            self.assertEqual(asked, [("vad",)])
            self.assertEqual(got["1"], {**old, "vad": vad})

            # A take the hearing doesn't have a saved entry for gets every block.
            got = takecheck.hear([("1", take)], "parakeet", "", print)
            self.assertEqual(asked[-1], ("asr", "signal", "vad"))
            self.assertEqual((got["1"]["asr"]["heard"], got["1"]["vad"]), ("y", vad))

        # A machine without the models that hears a saved take again in full keeps its current blocks of the others.
        speaker = {"v": takecheck.SPEAKER_V, **takecheck.packed([1.0, 0.0])}
        stale = {**old, "asr": {**asr_block, "v": 1}, "vad": vad, "speaker": speaker}
        with mock.patch.object(takecheck, "records", hears), mock.patch.object(takecheck, "check_ready", lambda *a: None):
            got = takecheck.hear([("1", take)], "parakeet", "", print, keep={"1": stale})
        self.assertEqual((asked[-1], got["1"]["asr"]["heard"], got["1"]["vad"], got["1"]["speaker"]),
                         (("asr", "signal"), "y", vad, speaker))


class TailTest(unittest.TestCase):
    LINE = "Leonard replied to the guard at the gate."

    def judge(self, heard: str, signal: dict | None = None, **kw) -> dict:
        tokens = breeze.voice_text({"text": self.LINE}, {})[1]
        block = {"v": takecheck.SIGNAL_V, "seconds": 3.0, "speech": [0.0, 2.6], "silences": [], **(signal or {})}
        return takecheck.judge(timed(heard), block, tokens, **kw)

    def test_words_heard_after_the_line_take_it_again(self):
        # Parakeet hears the voice go on after the line's last word, which ends at 2.34 s, so the take is flagged
        # for its tail.
        on = {"speech": [0.0, 3.2]}
        got = self.judge(self.LINE + " You.", on)
        self.assertEqual((got["verdict"], got["reasons"], got["tail"]), ("flagged", ["tail"], 0.86))

        # The second recognizer can hear the tail when Parakeet doesn't.
        moon = {"v": takecheck.ASR2_V, "engine": "moonshine-base-en", "heard": self.LINE + " Ha ha ha."}
        got = self.judge(self.LINE, on, asr2=moon)
        self.assertEqual((got["verdict"], got["reasons"], got["errors2"]), ("flagged", ["tail"], 3))

        # When the sound ends within TAIL_WORDS of the last word, a word heard after it is a recognizer's way of
        # hearing the last word, such as "keep, Anna" heard as "keep on, huh".
        self.assertEqual(self.judge(self.LINE, {"speech": [0.0, 2.6]}, asr2=moon)["reasons"], [])

        # A line that asks for a sound after its words keeps it, and a line that lost its ending has no tail.
        self.assertEqual(self.judge(self.LINE, on, asr2=moon, sounds=True)["reasons"], [])
        self.assertNotIn("tail", self.judge("Leonard replied to the guard you", on)["reasons"])

        # A short line with an added word is flagged as a short line, as before.
        tokens = breeze.voice_text({"text": "Leonard replied."}, {})[1]
        short = takecheck.judge(timed("Leonard replied. You."), None, tokens)
        self.assertEqual(short["reasons"], ["short"])

    def test_sound_after_the_last_word_is_cut_when_a_silence_sets_it_apart(self):
        # The line's last word ends at 2.34 s and the take's sound goes on to 3.0 s, after a silent run.
        got = self.judge(self.LINE, {"speech": [0.0, 3.0], "silences": [[2.375, 2.8]], "seconds": 3.1})
        self.assertEqual((got["verdict"], got["tail"]), ("ok", 0.66))
        self.assertEqual(got["repairs"]["cuts"], [[2.5, 3.1]])

        # The speech detector can find the sound go on later than the speech frames do.
        vad = {"v": takecheck.VAD_V, "engine": "silero-vad-v5", "segments": [[0.0, 2.3], [2.9, 3.05]]}
        got = self.judge(self.LINE, {"speech": [0.0, 3.0], "silences": [[2.375, 2.8]], "seconds": 3.1}, vad=vad)
        self.assertEqual((got["tail"], got["repairs"]["cuts"]), (0.71, [[2.5, 3.1]]))

        # Without a silent run between the word and the sound there is nothing safe to cut, a sound of up to
        # TAIL_OVER is left alone, and so is a line that keeps its pauses or asks for a sound.
        self.assertEqual(self.judge(self.LINE, {"speech": [0.0, 3.0]})["repairs"]["cuts"], [])
        self.assertEqual(self.judge(self.LINE, {"speech": [0.0, 2.8], "silences": [[2.375, 2.6]]})["repairs"]["cuts"],
                         [])
        apart = {"speech": [0.0, 3.0], "silences": [[2.375, 2.8]], "seconds": 3.1}
        self.assertEqual(self.judge(self.LINE, apart, cut_pauses=False)["repairs"]["cuts"], [])
        self.assertEqual(self.judge(self.LINE, apart, sounds=True)["repairs"]["cuts"], [])

        # A cut never removes words. A take whose last word was dropped, or after whose line a recognizer heard a
        # word, keeps its end.
        tokens = breeze.voice_text({"text": self.LINE}, {})[1]
        lost = takecheck.judge(timed("Leonard replied to the guard at the"), {"v": takecheck.SIGNAL_V, **apart}, tokens)
        self.assertEqual((lost["reasons"], lost["repairs"]["cuts"]), (["ending"], []))
        short_tokens = breeze.voice_text({"text": "Agnes,"}, {})[1]
        babble = takecheck.judge(timed("Agnes, mellowed fur."), {"v": takecheck.SIGNAL_V, "seconds": 2.0,
                                                               "speech": [0.0, 1.9], "silences": [[0.3, 0.9]]},
                                 short_tokens)
        self.assertEqual((babble["reasons"], babble["repairs"]["cuts"]), (["short"], []))
        moon = {"v": takecheck.ASR2_V, "engine": "moonshine-base-en", "heard": self.LINE + " Ha."}
        self.assertEqual(self.judge(self.LINE, {**apart, "speech": [0.0, 2.6]}, asr2=moon)["repairs"]["cuts"], [])

        # A silent run just before a last word that Parakeet gives no length doesn't set a tail apart from it, so
        # the cut doesn't take the word.
        asr = timed(self.LINE)
        asr["durations"] = asr["durations"][:-2] + [0.0, 0.0]
        asr["starts"] = asr["starts"][:-2] + [2.3, 2.3]
        got = takecheck.judge(asr, {"v": takecheck.SIGNAL_V, "speech": [0.0, 3.0], "silences": [[2.12, 2.28]],
                                    "seconds": 3.1}, tokens)
        self.assertEqual(got["repairs"]["cuts"], [])

        # Odd fields in the signal block give no cut and no error.
        for odd in ({"silences": 5}, {"seconds": True}, {"speech": [0.0, float("inf")]}, {"silences": [[2.375]]}):
            got = self.judge(self.LINE, {**apart, **odd})
            self.assertEqual(got["repairs"]["cuts"], [], odd)

        # A take whose words the token times can't place has no tail measure.
        tokens = breeze.voice_text({"text": self.LINE}, {})[1]
        got = takecheck.judge({"v": takecheck.ASR_V, "heard": self.LINE}, {"v": takecheck.SIGNAL_V, **apart}, tokens)
        self.assertNotIn("tail", got)

    def test_a_line_asks_for_a_sound_by_its_text_or_its_direction(self):
        self.assertTrue(takecheck.asks_for_sound({"text": "Fine. (laugh)"}))
        self.assertTrue(takecheck.asks_for_sound({"text": "Fine.", "direction": "laughing scornfully"}))
        self.assertTrue(takecheck.asks_for_sound({"text": "Fine.", "direction": "coughing, terrified"}))
        self.assertFalse(takecheck.asks_for_sound({"text": "Fine.", "direction": "flatly"}))
        # The sounds of version 4 of the rules, among them the direction of chapter 45 line 129.
        for direction in ("purring with delight", "cackling", "hissing, angry", "choking", "whimpering"):
            self.assertTrue(takecheck.asks_for_sound({"text": "Fine.", "direction": direction}), direction)
        # A word that only starts like a sound is not one.
        for direction in ("humbly", "soberly", "cryptically", "humiliated", "sighting the ship"):
            self.assertFalse(takecheck.asks_for_sound({"text": "Fine.", "direction": direction}), direction)


class DriftTest(unittest.TestCase):
    CENTRE = unit(1, 0, 0, 0, 0, 0, 0, 0)

    def judge(self, voice: list[float], baseline: dict | None = None, plain: bool = True) -> dict:
        tokens = breeze.voice_text({"text": "Nobody answered the door."}, {})[1]
        middle = {"n": 10, "level": [-26.0, 1.0], "centre": takecheck.packed(self.CENTRE),
                  "similarity": [0.95, 0.01], "m": 10}
        return takecheck.judge(timed("Nobody answered the door."), {"v": takecheck.SIGNAL_V}, tokens,
                               {**middle, **(baseline or {})}, speaker=takecheck.packed(voice), plain=plain)

    def test_an_embedding_survives_its_packing(self):
        v = unit(0.3, -0.2, 0.05, 0.9, -0.01, 0.0, 0.4, -0.7)
        back = takecheck.embedding(takecheck.packed(v))
        self.assertGreater(takecheck.similarity(v, back), 0.9999)
        self.assertIsNone(takecheck.embedding({**takecheck.packed(v), "v": takecheck.SPEAKER_V + 1}))
        self.assertIsNone(takecheck.embedding({**takecheck.packed(v), "q": "not base64!"}))
        self.assertIsNone(takecheck.embedding(None))
        # A scale no packed embedding can have gives none.
        for scale in (1e308, float("nan"), 0, -0.01, True):
            self.assertIsNone(takecheck.embedding({**takecheck.packed(v), "scale": scale}), scale)

    def test_a_take_far_from_its_voices_centre_is_taken_again(self):
        # Close to the centre is fine. Far below its median, by DRIFT_MADS deviations, the voice drifted.
        near = self.judge(unit(1, 0.3, 0, 0, 0, 0, 0, 0))
        self.assertEqual((near["verdict"], near["similarity"]), ("ok", 0.958))
        far = self.judge(unit(1, 1, 0, 0, 0, 0, 0, 0))
        self.assertEqual((far["verdict"], far["reasons"], far["similarity"]), ("flagged", ["drift"], 0.707))

        # A line that can't show the voice's timbre, or a voice with too few takes for a centre, isn't judged on it.
        self.assertEqual(self.judge(unit(1, 1, 0, 0, 0, 0, 0, 0), plain=False)["reasons"], [])
        self.assertEqual(self.judge(unit(1, 1, 0, 0, 0, 0, 0, 0), {"m": takecheck.DRIFT_MIN - 1})["reasons"], [])
        # A tight voice is judged on a spread of at least DRIFT_MAD_FLOOR.
        self.assertEqual(self.judge(unit(1, 0.35, 0, 0, 0, 0, 0, 0), {"similarity": [0.99, 0.0]})["reasons"], [])

        # The drift counts with the signal reasons in the rank, so the take without it is the better one.
        entry = {"signal": {"v": takecheck.SIGNAL_V}}
        self.assertTrue(takecheck.better({**entry, **near}, {**entry, **far}))

    def test_each_voice_gets_a_centre_from_its_takes_that_show_its_timbre(self):
        lines = [{"speaker": "narrator", "text": "Nobody answered the door at all."}] * 12
        takes = {str(i): {"voice": "narrator"} for i in range(12)}

        def entry(voice: list[float], active: float = 4.0) -> dict:
            return {"signal": {"v": takecheck.SIGNAL_V, "level": -26.0, "active": active},
                    "speaker": takecheck.packed(voice)}

        # Eight takes long enough to show the voice give its centre. One far take is left out of the centre.
        entries = {str(i): entry(unit(1, 0.1 * (i % 3), 0, 0, 0, 0, 0, 0)) for i in range(7)}
        entries["7"] = entry(unit(0, 1, 0, 0, 0, 0, 0, 0))
        entries["8"] = entry(unit(0, 1, 0, 0, 0, 0, 0, 0), active=1.0)
        got = takecheck.baselines(entries, takes, lines, {})["narrator"]
        self.assertEqual(got["m"], 8)
        self.assertGreater(takecheck.similarity(takecheck.embedding(got["centre"]), self.CENTRE), 0.99)
        self.assertEqual(got["similarity"][0], 0.996)

        # Embeddings that sum to nothing give no centre.
        opposite = {str(i): entry(unit(1, 0, 0, 0, 0, 0, 0, 0) if i % 2 else unit(-1, 0, 0, 0, 0, 0, 0, 0))
                    for i in range(8)}
        self.assertNotIn("centre", takecheck.baselines(opposite, takes, lines, {})["narrator"])

        # Under DRIFT_MIN such takes, the voice has no centre.
        del entries["7"]
        self.assertNotIn("centre", takecheck.baselines(entries, takes, lines, {})["narrator"])

        # A take of a line with any direction, or with a vocal event, doesn't count toward the centre, because a
        # direction such as "echoing through the hall" changes the timbre on purpose.
        signal = entry(self.CENTRE)["signal"]
        for line in ({"text": "x", "direction": "whispering"}, {"text": "x", "direction": "slowly"},
                     {"text": "x", "direction": "echoing through the hall"}, {"text": "x (laugh)", "direction": ""}):
            self.assertFalse(takecheck.shows_timbre(line, signal), line)
        self.assertTrue(takecheck.shows_timbre({"text": "x", "direction": ""}, signal))
        self.assertTrue(takecheck.shows_timbre({"text": "x"}, signal))


class SecondOpinionTest(unittest.TestCase):
    def judge(self, heard: str, heard2: str | None) -> dict:
        tokens = breeze.voice_text({"text": "Nobody answered the door."}, {})[1]
        moon = {"v": takecheck.ASR2_V, "engine": "moonshine-base-en", "heard": heard2} if heard2 is not None else None
        return takecheck.judge({"v": takecheck.ASR_V, "engine": "parakeet", "heard": heard}, None, tokens, asr2=moon)

    def test_a_suspect_take_both_recognizers_hear_wrong_is_taken_again(self):
        # Parakeet alone makes the take suspect. The second recognizer hearing it wrong too flags it.
        self.assertEqual(self.judge("Somebody answered the dog.", None)["verdict"], "suspect")
        both = self.judge("Somebody answered the dog.", "Somebody answers the dog.")
        self.assertEqual((both["verdict"], both["reasons"], both["errors2"], both["wer2"]),
                         ("flagged", ["garbled"], 3, 0.75))

        # When the second recognizer hears the line, the take stays suspect.
        one = self.judge("Somebody answered the dog.", "Nobody answered the door.")
        self.assertEqual((one["verdict"], one["reasons"], one["errors2"]), ("suspect", [], 0))

        # A take Parakeet hears right isn't judged on the second recognizer's words.
        self.assertEqual(self.judge("Nobody answered the door.", "Somebody answers the dog.")["verdict"], "ok")

        # In a line of one to three words a changed word is a homophone both recognizers can hear, as "Simon
        # guessed" heard as "Simon guest", so the take stays suspect.
        tokens = breeze.voice_text({"text": "Simon guessed."}, {})[1]
        moon = {"v": takecheck.ASR2_V, "engine": "moonshine-base-en", "heard": "Simon guest"}
        short = takecheck.judge({"v": takecheck.ASR_V, "heard": "Simon Guest"}, None, tokens, asr2=moon)
        self.assertEqual((short["verdict"], short["reasons"]), ("suspect", []))

    def test_two_takes_are_ranked_on_both_recognizers_when_both_have_both(self):
        def entry(errors: int, errors2: int | None = None) -> dict:
            out = {"verdict": "flagged", "sub": errors, "del": 0, "ins": 0, "reasons": []}
            return {**out, "errors2": errors2} if errors2 is not None else out

        # Three and none beat two and three.
        self.assertTrue(takecheck.better(entry(3, 0), entry(2, 3)))
        self.assertEqual(takecheck.badness(entry(3, 0))[1], 3)
        # Against a take without the second recognizer, only Parakeet's errors count.
        self.assertFalse(takecheck.better(entry(3, 0), entry(2)))
        self.assertTrue(takecheck.better(entry(2), entry(3, 0)))


def said_at(*parts: tuple[str, float]) -> dict:
    """A phone block that says each string from its second on, with a phone every 0.04 s."""
    symbols, starts = "", []
    for said, at in parts:
        symbols += said
        starts += [round(at + 0.04 * n, 2) for n in range(len(said))]
    return {"v": takecheck.PHONE_V, "engine": "zipa-small-crctc-300k", "phones": symbols, "starts": starts}


NAME_BIBLE = {"characters": {}, "pronunciations": {"Eole": {"say": "Eh-ole", "said": "eɪoʊl"}}}
NAME_LINE = {"speaker": "narrator", "text": "Tell Eole to wait."}


def name_phones(name: str) -> dict:
    """The phone block of a take of NAME_LINE that says the name as given."""
    return said_at(("tɛl", 0.0), (name, 0.3), ("tə", 0.6), ("weɪt", 0.9))


# how Parakeet writes the name in a take whose phones say it with an added sound
NAME_HEARD = {"jol": "Yol", "ɪtɔl": "It all", "eɪoʊli": "Aoli"}


def name_heard(name: str) -> str:
    """What Parakeet hears in a take of NAME_LINE that says the name as given. A sound the phones add is in its
    spelling too, and any other saying is "Aol"."""
    return f"Tell {NAME_HEARD.get(name, 'Aol')} to wait."


# one saying AJ marked on a listening page per line, with what the name rule reads of it
MARKS = Path(__file__).parent / "data" / "name_marks.jsonl"


class PhonesTest(unittest.TestCase):
    def test_a_window_holds_a_reference_whatever_stands_around_it(self):
        # The reference as a part of the window is no distance, whatever is before and after it.
        self.assertEqual(phones.apart("luis", "ændluissɛd"), 0.0)
        # A near vowel costs little, a missing first phone a quarter of four, and other phones most of it.
        self.assertLess(phones.apart("luis", "ændluɪssɛd"), 0.15)
        self.assertEqual(phones.apart("luis", "uissɛd"), 0.25)
        self.assertGreater(phones.apart("luis", "kæpɪtən"), 0.6)
        # The picked "ay-ole" against ways the voice has said it. With a glide or a near vowel it is the same
        # saying, and as "yol" or "it all" it is another.
        for window, same in (("ndeɪjoʊlsɛd", True), ("ndeɪɔlsɛd", True), ("jolsɛd", False), ("ɪtɔlsɛd", False)):
            self.assertEqual(phones.apart("eɪoʊl", window) <= takecheck.NAME_APART, same, window)
        # End to end, a neighbour's phone at either end counts.
        self.assertEqual(phones.whole("luis", "luis"), 0.0)
        self.assertEqual(phones.whole("luis", "luisk"), 0.2)

    def test_the_reference_of_a_name_is_its_usual_saying_without_a_neighbours_phone(self):
        # Most spans carry a phone of the word before or after. The reference is what the windows share.
        spans = [["luisk", "ɪdluiskən"], ["luis", "æzluisən"], ["luisf", "ætluisfɛl"], ["dluis", "ndluisɹɪ"],
                 ["luis", "luisweɪ"], ["luɪs", "təluɪsæz"]]
        self.assertEqual(phones.consensus(spans), "luis")
        self.assertEqual(phones.consensus([["luisk", "ɪdluiskən"], ["luisf", "ætluisfɛl"], ["dluis", "ndluisɹɪ"]]),
                         "luis")
        # Sayings that all have the phone keep it, and sayings without phones give no reference.
        self.assertEqual(phones.consensus([["luisk", "luiskən"]] * 3), "luisk")
        self.assertIsNone(phones.consensus([["", "ən"]]))

        # The sayings sort into ways by what their windows hold, the reference first and then the most frequent.
        found = phones.ways([["eɪoʊl", "deɪoʊls"], ["eɪɔl", "eɪɔlɹɪ"], ["hɛli", "hɛlisɛ"], ["jol", "ndjolsɛ"],
                             ["jol", "jollʊk"]], "eɪoʊl", takecheck.NAME_APART)
        self.assertEqual(found, [{"phones": "eɪoʊl", "count": 2}, {"phones": "jol", "count": 2},
                                 {"phones": "hɛli", "count": 1}])
        self.assertEqual(phones.ways([], "eɪoʊl", takecheck.NAME_APART), [{"phones": "eɪoʊl", "count": 0}])
        self.assertEqual(phones.ways([], None, takecheck.NAME_APART), [])

    def test_a_skeleton_is_the_consonants_with_one_vowel_for_each_run(self):
        # From a spelling, read as English: a final e after a consonant is silent unless it is read loud, a w or a
        # y after a vowel is part of the vowel, an h counts only before a vowel, and each symbol knows its word.
        self.assertEqual(phones.spelled("Eh-ole"), (["V", "l"], [0, 1]))
        self.assertEqual(phones.spelled("Lewis")[0], ["l", "V", "s"])
        self.assertEqual(phones.spelled("Laurie Anne"), (["l", "V", "ɹ", "V", "n"], [0, 0, 0, 0, 1]))
        self.assertEqual(phones.spelled("Vweevra")[0], ["v", "w", "V", "v", "ɹ", "V"])
        self.assertEqual(phones.spelled("y'all")[0], ["j", "V", "l"])
        self.assertEqual((phones.spelled("aole")[0], phones.spelled("aole", loud=True)[0]), (["V", "l"], ["V", "l", "V"]))
        # From phones: a glide after a vowel is part of the vowel, and a glottal stop and a mark are left out.
        self.assertEqual(phones.sounded("eɪjoʊl"), ["V", "l"])
        self.assertEqual(phones.sounded("heɪhoʊlm"), ["h", "V", "h", "V", "l", "m"])
        self.assertEqual(phones.sounded("ʔeɪoʊlː"), ["V", "l"])
        # A possessive may add its z, and a vowel before it after a hissing sound.
        self.assertEqual(phones.forms("eh ole's"), [["V", "l"], ["V", "l", "z"]])
        self.assertEqual(phones.forms("lewis'"), [["l", "V", "s"], ["l", "V", "s", "z"], ["l", "V", "s", "V", "z"]])

    def test_a_saying_adds_a_sound_when_both_recognizers_hear_it(self):
        # Each case is a saying AJ marked on the listening pages of 2026-11-13, as (the name's words, Parakeet's
        # spelling, ZIPA's phones in and around the span, the next word, the word before).
        def adds(*case) -> list:
            return phones.adds(*case)

        # A syllable after the name, an h inside it and a consonant before it, each heard by both.
        self.assertEqual(adds("eh ole", "aeoli", "eɪoʊliɪnsɪ", "insisted", ""), [("V", 2)])
        self.assertEqual(adds("eh ole", "a ho", "uheɪhoʊlm", "she", "to"), [("h", 2)])
        self.assertEqual(adds("eh ole", "so well", "soʊwɛlʃ", "shook", ""), [("s", 0)])
        self.assertEqual(adds("eh ole", "y'all", "jol", "", ""), [("j", 0)])
        self.assertEqual(adds("vwee vruh", "vrivru", "stvɹivɹuənd", "and", "least"), [("ɹ", 2)])
        # Sayings he hears as picked: nothing added, or the two recognizers hear different sounds.
        self.assertEqual(adds("eh ole", "aol", "eoʊldblɪ", "blinked", ""), [])
        self.assertEqual(adds("eh ole", "they all", "hiɔlɹɪme", "remained", ""), [])
        self.assertEqual(adds("eh ole", "hale", "etɪlglæn", "glanced", ""), [])
        # A sound only one of the two hears is not added, and neither is a sound the saying lacks.
        self.assertEqual(adds("eh ole", "aol", "jol", "to", ""), [])
        self.assertEqual(adds("eh ole", "yol", "eɪoʊl", "to", ""), [])
        self.assertEqual(adds("eh ole", "ao", "eɪoʊ", "recalled", ""), [])

        # A final e of the spelling is a vowel where the vowel in the phones can't be the next word's.
        self.assertEqual(adds("eh ole", "aole", "eoʊleɪb", "bit", ""), [("V", 2)])
        # It is silent before a vowel or an h, before the name's own last sound, and in a possessive. The first
        # and the last of these are sayings he hears another way, which the rule lets pass.
        self.assertEqual(adds("eh ole", "eole", "oleəp", "appeared", ""), [])
        self.assertEqual(adds("eh ole", "aole", "eoʊleɪh", "had", ""), [])
        self.assertEqual(adds("laurie anne", "lorianne", "lɔɹiænnɑ", "nodded", ""), [])
        # So it is before a word without letters, such as a number, whose first sound is not known. At the end
        # of the line no word follows, and it is a vowel.
        self.assertEqual(adds("eh ole", "aole", "eoʊleɪt", "8", "told"), [])
        self.assertEqual(adds("eh ole", "aole", "eoʊleɪ", "", "told"), [("V", 2)])
        self.assertEqual(adds("eh ole's", "aeole's", "eɪoʊleɪɛsfɹɛnd", "friendship", "of"), [])
        # A possessive's own sounds are not added, and a vowel before them that the spelling has too is.
        self.assertEqual(adds("laurie anne's", "lorianne's", "lɔɹiænɛzgeɪ", "gaze", ""), [])
        self.assertEqual(adds("lewis'", "louis", "luisɪsmæ", "madness", "of"), [])
        self.assertEqual(adds("eh ole's", "aoli's", "eɪoʊliɛsɹəfj", "refusal", ""), [("V", 2)])

        # The neighbours' sounds are not the name's: a heard word that holds none of the name, the next word's
        # phones inside the span, and a consonant that the word before ends in.
        self.assertEqual(adds("vwee vruh", "lord vuivru", "ənlɔɹdvuwivɹusmɑ", "smirked", "dragonlord"), [])
        self.assertEqual(adds("vwee vruh", "vouivreux", "vwivɹuwɑzn", "was", "like"), [])
        self.assertEqual(adds("eh ole", "lol", "llol", "to", "tell"), [])
        self.assertEqual(adds("eh ole", "yol", "ljol", "to", "tell"), [("j", 0)])
        # Without phones, without a spelling or without a name there is nothing to hold.
        for case in (("eh ole", "aol", "", "to", ""), ("eh ole", "", "eɪoʊl", "to", ""), ("", "aol", "eɪoʊl", "to", "")):
            self.assertEqual(adds(*case), [], case)

    def test_the_rule_is_scored_on_the_sayings_aj_marked(self):
        # A record's mark is y for a name he hears as picked, n for one he hears another way, and u for one he
        # couldn't judge. Its phones are those within half a second of the saying's span, t0 to t1.
        marks = [json.loads(raw) for raw in MARKS.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([sum(1 for m in marks if m["mark"] == mark) for mark in "ynu"], [95, 72, 2])
        self.assertTrue(all(len(m["phones"]) == len(m["starts"]) for m in marks))

        def flagged(m: dict) -> bool:
            near = "".join(p for p, s in zip(m["phones"], m["starts"])
                           if m["t0"] - takecheck.NAME_NEAR <= s < m["t1"] + takecheck.NAME_NEAR)
            return bool(phones.adds(m["said"], m["heard"], near, m["after"], m["before"]))

        # The rule agrees with him on 129 of the 167 he judged. It flags two sayings he hears as picked, and it
        # passes 36 he hears another way, each named by its page and clip. On pages 1 and 2 those are nine.
        apart = {mark: [f'{m["page"]}.{m["clip"]}' for m in marks if m["mark"] == mark and flagged(m) != (mark == "n")]
                 for mark in "ny"}
        self.assertEqual(apart["y"], ["2.E04", "2.V11"])
        self.assertEqual(apart["n"][:9], ["1.E02", "2.E03", "2.E09", "2.E35", "2.V02", "2.V03", "2.V05", "2.V08", "2.V09"])
        # Of the others, 26 are every take of "Ay-ohl" on page 3 that he rejected. They add no sound and say the
        # name with the first vowel of "eye", which the rule doesn't hear. The last is the one take of "Éole" he
        # rejected on page 5, of 30 the rule passes.
        eye = [1, 2, 3, 5, 6, 7, 8, 9, 10, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 24, 25, 26, 27, 28, 29, 30]
        self.assertEqual(apart["n"][9:], [f"3.E{n:02d}" for n in eye] + ["5.E17"])

    def test_the_child_gives_a_takes_phones_as_one_string_with_their_seconds(self):
        # sherpa-onnx times ZIPA's tokens at twice their seconds. The word boundary and a token that is not one
        # symbol are left out with their times.
        result = mock.Mock(tokens=[" ", "l", "u", "<unk>", "ɪ"], timestamps=[0.0, 0.16, 0.32, 0.4, 0.48])
        self.assertEqual(parakeet_asr.phones(result), {"phones": "luɪ", "starts": [0.08, 0.16, 0.24]})


class NameTest(unittest.TestCase):
    """The name rule as it works while takecheck.NAME_FLAGS is on. The last test is the rule switched off."""

    def setUp(self):
        patcher = mock.patch.object(takecheck, "NAME_FLAGS", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def judge(self, name: str, heard: str | None = None, **more) -> dict:
        tokens = breeze.voice_text(NAME_LINE, NAME_BIBLE)[1]
        blocks = {"phone": name_phones(name), "said": takecheck.picked_spellings(NAME_BIBLE), **more}
        return takecheck.judge(timed(name_heard(name) if heard is None else heard), None, tokens, **blocks)

    def test_a_take_is_held_to_the_spelling_that_was_picked_for_a_name(self):
        # The name's phones are the ones inside the span of the word heard in its place, and its window takes in
        # the words around it.
        right = self.judge("eɪoʊl")
        self.assertEqual((right["verdict"], right["reasons"], right["missed"]), ("ok", [], []))
        self.assertEqual(right["said"], {"Eole": [["eɪoʊl", "tɛleɪoʊltə"]]})
        self.assertEqual(self.judge("eɪɔl")["reasons"], [])

        # A take that both recognizers hear an added sound in is flagged for it: a consonant before the name, a
        # syllable after it, or a word in its place that has more sounds.
        wrong = self.judge("jol")
        self.assertEqual((wrong["verdict"], wrong["reasons"], wrong["missed"]), ("flagged", ["name"], ["Eole"]))
        for name in ("eɪoʊli", "ɪtɔl"):
            self.assertEqual(self.judge(name)["missed"], ["Eole"], name)
        # A sound that only the phones have, or only the spelling, is not enough.
        self.assertEqual(self.judge("jol", "Tell Aol to wait.")["missed"], [])
        self.assertEqual(self.judge("eɪoʊl", "Tell Yol to wait.")["missed"], [])

        # A name that is not held to a spelling is recorded and not judged.
        free = self.judge("jol", said={})
        self.assertEqual((free["reasons"], free["missed"], free["said"]), ([], [], {"Eole": [["jol", "tɛljoltə"]]}))

        # Without a phone block of this version the rule has no evidence, and the entry says nothing of names.
        for phone in (None, {**name_phones("jol"), "v": takecheck.PHONE_V + 1}, {"v": takecheck.PHONE_V, "error": "x"}):
            got = self.judge("jol", phone=phone)
            self.assertEqual((got["reasons"], "said" in got and bool(got["said"]), got.get("missed") or []),
                             ([], False, []), phone)
        # A name the recognizer heard no word for has no span to judge.
        dropped = self.judge("jol", heard="Tell to wait.")
        self.assertEqual((dropped["said"], dropped["missed"]), ({}, []))

        # The rank counts the names a take misses, after its word errors, and skips the slot without evidence.
        self.assertEqual((takecheck.badness(right)[2], takecheck.badness(wrong)[2]), (0, 1))
        self.assertIsNone(takecheck.badness(self.judge("jol", phone=None))[2])
        self.assertTrue(takecheck.better(right, wrong))
        self.assertFalse(takecheck.better(wrong, right))

    def test_a_names_span_carries_what_the_rule_reads(self):
        # The words the voice was sent for the name, the words heard in their place, the line's words on either
        # side, and the phones in the span, near it and in the window around it.
        tokens = breeze.voice_text(NAME_LINE, NAME_BIBLE)[1]
        asr = timed("Tell Aoli to wait.")
        edits = takecheck.word_edits(asr["heard"], tokens)[0]
        self.assertEqual(takecheck.name_spans(asr, name_phones("eɪoʊli"), tokens, edits),
                         [{"term": "Eole", "inside": "eɪoʊli", "window": "tɛleɪoʊlitə", "near": "eɪoʊlitə",
                           "said": "eh ole", "heard": "aoli", "before": "tell", "after": "to"}])
        # A name at an end of the line has no word on that side.
        last = {"speaker": "narrator", "text": "Eole"}
        tokens = breeze.voice_text(last, NAME_BIBLE)[1]
        asr = timed("Aol", 0.3)
        edits = takecheck.word_edits(asr["heard"], tokens)[0]
        span = takecheck.name_spans(asr, name_phones("eɪoʊl"), tokens, edits)[0]
        self.assertEqual((span["before"], span["after"], span["said"]), ("", "", "eh ole"))

    def test_a_saved_entry_is_judged_with_the_bibles_picked_spellings(self):
        entry = {"key": "k", "audio": "a", "asr": timed(name_heard("jol")), "phone": name_phones("jol")}
        got = takecheck.judged(entry, NAME_LINE, NAME_BIBLE, None)
        self.assertEqual((got["missed"], got["phone"]), (["Eole"], entry["phone"]))
        self.assertEqual(takecheck.picked_spellings(NAME_BIBLE), {"Eole": "Eh-ole"})
        # A bible whose entry has no say-as holds no take to a spelling.
        loose = {"characters": {}, "pronunciations": {"Eole": {"note": "a siren"}}}
        self.assertEqual(takecheck.picked_spellings(loose), {})
        self.assertEqual(takecheck.judged(entry, NAME_LINE, loose, None)["reasons"], [])

    def test_a_take_that_misses_a_name_is_recorded_and_not_flagged_while_the_rule_is_off(self):
        # With the switch off, a take's sayings and misses are still recorded, the take is not flagged for them,
        # and the rank skips the name slot.
        with mock.patch.object(takecheck, "NAME_FLAGS", False):
            wrong = self.judge("jol")
            self.assertEqual((wrong["verdict"], wrong["reasons"], wrong["missed"]), ("ok", [], ["Eole"]))
            self.assertEqual(wrong["said"], {"Eole": [["jol", "tɛljoltə"]]})
            self.assertIsNone(takecheck.badness(wrong)[2])
            self.assertFalse(takecheck.better(self.judge("eɪoʊl"), wrong))

    def test_a_name_that_many_takes_miss_is_no_finding(self):
        def entry(missed: bool, reason: str) -> dict:
            return {"verdict": "flagged" if missed else "ok", "reasons": [reason] if missed else [],
                    "said": {"Eole": [["x", "x"]]}, "missed": ["Eole"] if missed and reason == "name" else []}

        # Four takes of a voice's twelve that add a sound to a name are over the breaker's limits, and they give
        # no finding, so a render takes each of them again. Another reason at the same count is a voice finding.
        takes = {str(i): {"voice": "narrator"} for i in range(12)}
        self.assertEqual(takecheck.voice_findings({k: entry(int(k) < 4, "name") for k in takes}, takes), [])
        self.assertEqual(takecheck.voice_findings({k: entry(int(k) < 4, "clipped") for k in takes}, takes),
                         [{"voice": "narrator", "reason": "clipped", "lines": [0, 1, 2, 3], "of": 12}])


class NameRetakeTest(unittest.TestCase):
    """A render's check before the join, on takes of NAME_LINE whose file holds the phones of the name, while
    takecheck.NAME_FLAGS is on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.logs: list[str] = []
        self.asked: list[tuple[str, int]] = []

        def hears(files, *, engine, comfy_url, blocks=("asr", "signal")):
            for i, f in enumerate(files):
                text = Path(f).read_text(encoding="utf-8")
                name = text.removeprefix("#clipped ")
                yield i, {**record(name_heard(name), clip_run=40 if text != name else 0), "phone": name_phones(name)}

        for p in (mock.patch.object(takecheck, "records", hears),
                  mock.patch.object(takecheck, "NAME_FLAGS", True),
                  mock.patch.object(takecheck, "check_ready", lambda *a, **k: None),
                  mock.patch.object(takecheck, "extra_blocks", lambda: {"phone"}),
                  mock.patch.object(breeze, "choose_asr", lambda *a, **k: "parakeet")):
            p.start()
            self.addCleanup(p.stop)

    def takes(self, names: list[str]) -> dict:
        saved = {}
        for i, name in enumerate(names):
            file = self.folder / f"l{i:04d}.wav"
            file.write_text(name, encoding="utf-8")
            saved[str(i)] = {"key": f"k{i}", "file": str(file), "voice": "narrator", "seed": 5, "audio": f"a{i}"}
        return saved

    def retake(self, saved: dict) -> dict:
        def again(k: str, round_: int) -> dict:
            self.asked.append((k, round_))
            file = self.folder / f"l{int(k):04d}.retake.wav"
            file.write_text("eɪoʊl", encoding="utf-8")
            return {"key": f"k{k}", "file": str(file), "seed": 90 + round_, "audio": f"b{k}"}

        return breeze.retake_flagged(self.folder, [NAME_LINE] * len(saved), NAME_BIBLE, saved, list(saved), again,
                                     comfy_url="", log=self.logs.append, save=lambda: None)

    def test_a_take_that_adds_a_sound_to_a_picked_name_is_taken_again(self):
        # One take of six adds a sound to the name. It alone is taken again, and its new take is kept.
        saved = self.takes(["eɪoʊl", "eɪɔl", "jol", "eɪoʊl", "eɪjoʊl", "eɪoʊl"])
        got = self.retake(saved)
        self.assertEqual((got["replaced"], self.asked), ([2], [("2", 1)]))
        self.assertEqual(Path(saved["2"]["file"]).read_text(encoding="utf-8"), "eɪoʊl")
        self.assertEqual([saved[k]["audio"] for k in saved], ["a0", "a1", "b2", "a3", "a4", "a5"])
        checked = lib.load_json(self.folder / takecheck.CHECK_FILE)["takes"]
        self.assertEqual({k: e["verdict"] for k, e in checked.items()}, {str(i): "ok" for i in range(6)})
        self.assertEqual(checked["2"]["phone"], name_phones("eɪoʊl"))

    def test_a_name_most_takes_miss_is_taken_again_all_the_same(self):
        # Four of six takes add a sound to the name, all in one voice. Each of the four is taken again and its
        # new take is kept. No take is held, and the render gets no finding for the name.
        saved = self.takes(["jol", "jol", "ɪtɔl", "jol", "eɪoʊl", "eɪoʊl"])
        got = self.retake(saved)
        self.assertEqual((got["replaced"], self.asked), ([0, 1, 2, 3], [("0", 1), ("1", 1), ("2", 1), ("3", 1)]))
        self.assertEqual((got["findings"], "names" in got), ([], False))
        self.assertEqual([saved[k].get("retake_held") for k in saved], [None] * 6)
        self.assertFalse(any("finding" in line for line in self.logs), self.logs)
        self.assertEqual([saved[k]["audio"] for k in saved], ["b0", "b1", "b2", "b3", "a4", "a5"])
        # A later render finds nothing flagged.
        self.assertEqual(breeze.flagged_before(self.folder, saved), set())

    def test_a_take_flagged_for_a_name_alone_is_the_last_under_the_ceiling(self):
        # Three takes add a sound to the name, and a fourth is clipped. A file that starts with "#clipped " is a
        # take with a long run of full-scale samples. With two retakes to spend, the clipped take and the first
        # of the three are taken again, and the other two are left for a later render.
        saved = self.takes(["jol", "jol", "jol", "#clipped eɪoʊl"])
        with mock.patch.object(breeze, "RETAKE_FLOOR", 2), mock.patch.object(breeze, "RETAKE_SHARE", 0.0):
            got = self.retake(saved)
        self.assertEqual((got["replaced"], self.asked, got["left"]), ([0, 3], [("0", 1), ("3", 1)], [1, 2]))

    def test_a_hold_does_not_cover_a_name(self):
        # A take whose retake_held names the reason "name" counts as flagged, so a render takes it again.
        saved = self.takes(["jol", "eɪoʊl"])
        breeze.retake_flagged(self.folder, [NAME_LINE] * 2, NAME_BIBLE, saved, list(saved), None, comfy_url="",
                              log=self.logs.append, save=lambda: None, rounds=0)
        saved["0"]["retake_held"] = ["name"]
        self.assertEqual(breeze.flagged_before(self.folder, saved), {"0"})

    def test_a_spelling_that_comes_after_the_check_gets_the_saved_takes_judged_again(self):
        # The chapter is checked while the name is not held to a spelling, so no take is flagged for it.
        loose = {"characters": {}, "pronunciations": {"Eole": {"note": "a siren"}}}
        saved = self.takes(["eɪoʊl", "jol", "eɪoʊl", "eɪoʊl", "eɪoʊl", "eɪoʊl"])
        lines = [NAME_LINE] * len(saved)
        results = takecheck.check_entries(saved, lines, loose, engine="parakeet", comfy_url="", log=lambda *_: None)
        takecheck.save_entries(self.folder, results, "parakeet", saved, lines, loose)
        data = lib.load_json(self.folder / takecheck.CHECK_FILE)
        self.assertEqual((data["rules"], data["names"]), (takecheck.JUDGE_V, {}))
        self.assertFalse(takecheck.stale(data, loose))
        self.assertEqual(breeze.flagged_before(self.folder, saved, lines, loose), set())

        # The name gets a spelling. The file was judged without it, so its verdicts are judged again from the
        # evidence, and a render then takes the line that adds a sound to the name again.
        self.assertTrue(takecheck.stale(data, NAME_BIBLE))
        self.assertFalse(takecheck.stale(data))
        self.assertEqual(breeze.flagged_before(self.folder, saved, lines, NAME_BIBLE), {"1"})
        got = self.retake({k: saved[k] for k in ("1",)} | saved)
        self.assertEqual((got["replaced"], self.asked), ([1], [("1", 1)]))

        # A file that still mixes verdicts from before the spelling keeps saying so. A whole check doesn't.
        partial = takecheck.check_entries({"1": saved["1"]}, lines, NAME_BIBLE, engine="parakeet", comfy_url="",
                                          log=lambda *_: None)
        (self.folder / takecheck.CHECK_FILE).write_text(json.dumps({**data, "takes": data["takes"]}), encoding="utf-8")
        takecheck.save_entries(self.folder, partial, "parakeet", saved, lines, NAME_BIBLE)
        self.assertTrue(takecheck.stale(lib.load_json(self.folder / takecheck.CHECK_FILE), NAME_BIBLE))


if __name__ == "__main__":
    unittest.main()
