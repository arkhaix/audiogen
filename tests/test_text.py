"""Tests for the text side of the pipeline: the Royal Road and EPUB parsers, the character pass merge and its rules,
the screenplay rules, and what the LLM backends do at the subscription limit."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from serial_audio import epub, llm, passes, royalroad, screenplay

PAGE = """<html><head><style>.cjZhYjNm { display: none; speak: never; }</style></head><body>
<h1 class="font-white">Chapter 3: The One Act Play (1)</h1>
<div class="portlet author-note-portlet"><div class="author-note"><p>Thanks for reading!</p></div></div>
<div class="chapter-inner chapter-content">
<p>Louis woke before dawn.</p>
<p class="cjZhYjNm">You could be reading stolen content. Head to Royal Road for the genuine story.</p>
<p>He was <em>very </em>tired, and thought: <em>not again</em>.</p>
<blockquote><p>[Quest received]</p><p>Reach the gate.</p></blockquote>
<hr>
<table><tr><td>Strength</td><td>12</td></tr></table>
<p>First line<br>second line</p>
<p style="display: none">hidden by its style</p>
</div>
<div class="portlet author-note-portlet"><div class="author-note"><p>Next chapter on Friday.</p></div></div>
</body></html>"""
ENTRY = {"number": 3, "id": 77, "title": "Chapter 3: The One Act Play (1)", "url": "https://example.invalid/3",
         "date": "2026-01-01", "volume": 1, "volume_title": "Book One"}


class RoyalRoadTest(unittest.TestCase):
    def test_a_saved_page_becomes_blocks_without_the_decoy_or_the_notes(self):
        parsed = royalroad.parse_chapter(PAGE)
        self.assertEqual(parsed.title, "Chapter 3: The One Act Play (1)")
        self.assertEqual(parsed.blocks, [
            "Louis woke before dawn.",
            "He was *very* tired, and thought: *not again*.",
            "> [Quest received]",
            "> Reach the gate.",
            "* * *",
            "Strength | 12",
            "First line\nsecond line",
        ])

        # The author's notes on both sides of the text are kept apart from it.
        self.assertEqual(parsed.author_notes, ["Thanks for reading!", "Next chapter on Friday."])

    def test_a_chapter_takes_its_label_and_title_from_the_page(self):
        chapter = royalroad.chapter(ENTRY, PAGE)
        self.assertEqual((chapter.number, chapter.label, chapter.title, chapter.part, chapter.part_title),
                         (3, "Chapter 3", "The One Act Play (1)", 1, "Book One"))
        self.assertTrue(chapter.text.startswith("# Chapter 3: The One Act Play (1)\n\nLouis woke before dawn.\n\n"))
        self.assertEqual(chapter.author_notes, ["Thanks for reading!", "Next chapter on Friday."])

    def test_a_page_without_chapter_text_is_refused(self):
        with self.assertRaises(royalroad.RoyalRoadError):
            royalroad.chapter(ENTRY, "<html><body><p>Checking your browser.</p></body></html>")

    def test_titles_split_into_label_and_title(self):
        self.assertEqual(royalroad.split_title("Chapter 3: The One Act Play (1)", 3), ("Chapter 3", "The One Act Play (1)"))
        self.assertEqual(royalroad.split_title("Interlude - The Keeper", 9), ("Interlude", "The Keeper"))
        self.assertEqual(royalroad.split_title("A Quiet Morning", 7), ("Chapter 7", "A Quiet Morning"))

    def test_fiction_ids(self):
        self.assertEqual(royalroad.fiction_id("12345"), 12345)
        self.assertEqual(royalroad.fiction_id("https://www.royalroad.com/fiction/12345/some-title"), 12345)
        with self.assertRaises(royalroad.RoyalRoadError):
            royalroad.fiction_id("https://example.invalid/fiction")


CONTAINER = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
             '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
             '</rootfiles></container>')
OPF = ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
       '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title>'
       '<dc:creator>A. Writer</dc:creator><dc:language>en</dc:language></metadata><manifest>'
       '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
       '<item id="pg-header" href="header.xhtml" media-type="application/xhtml+xml"/>'
       '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
       '<item id="css" href="s.css" media-type="text/css"/></manifest>'
       '<spine><itemref idref="nav"/><itemref idref="pg-header"/><itemref idref="c1"/></spine></package>')
BOOK = """<html><body>
<div class="chapter"><h2>PART ONE</h2></div>
<div class="chapter"><h2>CHAPTER I.<br/>A SHIFTING REEF</h2>
<p>The year 1866 was signalised by a remarkable incident.</p>
<div class="fig"><p>A caption under a picture.</p></div>
<p>Second   paragraph.</p></div>
<div class="chapter"><h2>CHAPTER II.<br/>NED LAND’S TEMPER</h2><p>Text two.</p></div>
<div class="chapter"><h2>EPILOGUE</h2><p>The end.</p></div>
</body></html>"""


class EpubTest(unittest.TestCase):
    def test_chapters_come_from_the_spine_with_parts_labels_and_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.epub"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("META-INF/container.xml", CONTAINER)
                z.writestr("OEBPS/content.opf", OPF)
                z.writestr("OEBPS/nav.xhtml", "<html><body><h2>CHAPTER IX.</h2><p>Contents.</p></body></html>")
                z.writestr("OEBPS/header.xhtml", "<html><body><h2>CHAPTER X.</h2><p>Licence.</p></body></html>")
                z.writestr("OEBPS/c1.xhtml", BOOK)
            meta, chapters = epub.extract(path)
        self.assertEqual(meta, {"title": "Test Book", "author": "A. Writer", "language": "en"})
        self.assertEqual([(c.number, c.part, c.part_title, c.label, c.title) for c in chapters],
                         [(1, 1, "Part One", "Chapter I", "A Shifting Reef"),
                          (2, 1, "Part One", "Chapter II", "Ned Land’s Temper"),
                          (3, 1, "Part One", "Epilogue", "")])

        # A figure's caption is not chapter text, and runs of spaces collapse.
        self.assertEqual(chapters[0].paragraphs, ["The year 1866 was signalised by a remarkable incident.", "Second paragraph."])
        self.assertEqual(chapters[2].text, "# Epilogue\n\nThe end.\n")

    def test_roman_numerals_and_title_case(self):
        self.assertEqual([epub._roman(s) for s in ("I", "IV", "XLII", "12")], [1, 4, 42, 12])
        self.assertEqual(epub.title_case("NED LAND’S TEMPER"), "Ned Land’s Temper")
        self.assertEqual(epub.title_case("Already Mixed case"), "Already Mixed case")


def new_character(cid: str, name: str, **over) -> dict:
    return {"id": cid, "name": name, "aliases": [], "gender": "female", "age_group": "adult", "role": "a guard",
            "description": "Keeps the gate.", "voice_brief": "Low and dry.", "importance": "recurring", "speaks": True,
            "evidence": "She stops Louis at the gate.", **over}


class CharacterPassTest(unittest.TestCase):
    def setUp(self):
        self.bible = {"characters": {}, "pronunciations": {"Lauriane": {"respelling": "LOR-ee-ahn", "say": "Loriann"}}}
        passes.merge_character_pass(self.bible, {"new_characters": [new_character("mira", "Mira", aliases=["the guard", "Mira"])]}, 4)

    def test_a_new_character_gets_a_record(self):
        mira = self.bible["characters"]["mira"]
        self.assertEqual((mira["name"], mira["aliases"], mira["first_seen"], mira["last_seen"], mira["chapters_present"],
                          mira["voice_id"]), ("Mira", ["the guard"], 4, 4, [4], ""))
        self.assertEqual(mira["facts"], [{"chapter": 4, "text": "She stops Louis at the gate."}])

    def test_a_later_chapter_updates_the_record(self):
        out = {"present": [{"id": "mira", "speaks": False}, {"id": "nobody", "speaks": True}],
               "updates": [{"id": "mira", "new_aliases": ["Captain Mira"], "new_facts": ["She is promoted.", "She is promoted."],
                            "voice_brief_update": "Low, dry and tired.", "importance_update": "major"},
                           {"id": "nobody", "new_facts": ["x"]}],
               "pronunciations": [{"term": "Lauriane", "respelling": "wrong"}, {"term": "Carcas", "respelling": "KAR-kass"}]}
        present = passes.merge_character_pass(self.bible, out, 5)

        # An id the bible does not hold is ignored, in present and in updates.
        self.assertEqual(present, ["mira"])
        self.assertEqual(list(self.bible["characters"]), ["mira"])

        # The record grows, a repeated fact is stored once, and the old voice brief is kept as history.
        mira = self.bible["characters"]["mira"]
        self.assertEqual((mira["last_seen"], mira["chapters_present"], mira["speaks"], mira["importance"]),
                         (5, [4, 5], True, "major"))
        self.assertEqual(mira["aliases"], ["the guard", "Captain Mira"])
        self.assertEqual([f["text"] for f in mira["facts"]], ["She stops Louis at the gate.", "She is promoted."])
        self.assertEqual((mira["voice_brief"], mira["voice_brief_history"]),
                         ("Low, dry and tired.", [{"chapter": 5, "text": "Low and dry."}]))

        # An existing pronunciation, with its say-as override, is never overwritten.
        self.assertEqual(self.bible["pronunciations"]["Lauriane"], {"respelling": "LOR-ee-ahn", "say": "Loriann"})
        self.assertEqual(self.bible["pronunciations"]["Carcas"]["respelling"], "KAR-kass")

    def test_a_known_id_sent_as_new_is_an_update(self):
        again = new_character("mira", "Mira of the Gate", description="She has a scar.")
        self.assertEqual(passes.merge_character_pass(self.bible, {"new_characters": [again]}, 6), ["mira"])
        mira = self.bible["characters"]["mira"]
        self.assertEqual((mira["name"], mira["first_seen"], mira["last_seen"]), ("Mira", 4, 6))
        self.assertIn("Mira of the Gate", mira["aliases"])
        self.assertIn("She has a scar.", [f["text"] for f in mira["facts"]])

    def test_the_rules_the_schema_cannot_express(self):
        problems = passes.validate_character_output(
            {"new_characters": [new_character("mira", "Mira")], "present": [{"id": "ghost", "speaks": True}],
             "updates": [{"id": "phantom"}], "narration": {"point_of_view": "first_person", "narrator_character_id": ""}},
            self.bible)
        self.assertEqual(len(problems), 4, problems)
        for needle in ("`mira` already exists", "`ghost` in `present`", "`phantom` in `updates`", "first person"):
            self.assertTrue(any(needle in p for p in problems), (needle, problems))
        self.assertEqual(passes.validate_character_output(
            {"present": [{"id": "mira", "speaks": True}], "narration": {"point_of_view": "third_person"}}, self.bible), [])


TEXT = "# Chapter 1: Morning\n\nThe harbour was quiet. “Nobody answered,” said Mira."
BIBLE = {"characters": {"mira": {"name": "Mira"}}}


class ScreenplayRulesTest(unittest.TestCase):
    def check(self, lines: list[dict]) -> tuple[list[str], dict]:
        return screenplay.validate_script({"lines": lines}, BIBLE, TEXT)

    def test_a_faithful_script_passes(self):
        problems, stats = self.check([{"speaker": "narrator", "text": "The harbour was quiet."},
                                      {"speaker": "mira", "text": "Nobody answered,"},
                                      {"speaker": "narrator", "text": "said Mira."}])
        self.assertEqual(problems, [])

        # The heading line is not counted as chapter text.
        self.assertEqual((stats["source_words"], stats["script_words"], stats["coverage"], stats["lines"], stats["speakers"]),
                         (8, 8, 1.0, 3, ["mira", "narrator"]))

    def test_broken_scripts_are_named(self):
        self.assertIn("`lines` is empty.", self.check([])[0])
        problems, _ = self.check([{"speaker": "louis", "text": "The harbour was quiet. Nobody answered, said Mira."},
                                  {"speaker": "narrator", "text": "  "}])
        self.assertTrue(any("speaker `louis`" in p for p in problems), problems)
        self.assertTrue(any("empty text" in p for p in problems), problems)

    def test_dropped_or_added_text_fails_the_coverage_check(self):
        dropped, stats = self.check([{"speaker": "narrator", "text": "The harbour was quiet."}])
        self.assertEqual(stats["coverage"], 0.5)
        self.assertTrue(any("dropped or added" in p for p in dropped), dropped)
        padded, _ = self.check([{"speaker": "narrator", "text": "The harbour was quiet. Nobody answered, said Mira. " * 2}])
        self.assertTrue(any("dropped or added" in p for p in padded), padded)


SCHEMA = {"type": "object", "properties": {"lines": {"type": "array"}}}


def claude_answer(**fields) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["claude"], fields.pop("returncode", 0), stdout=json.dumps(fields), stderr="")


class ClaudeCodeBackendTest(unittest.TestCase):
    def run_backend(self, answer: subprocess.CompletedProcess):
        backend = llm.ClaudeCodeBackend(executable="claude")
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t"}), \
                mock.patch.object(llm.subprocess, "run", return_value=answer) as run:
            try:
                return backend.run("system", "chapter text", SCHEMA, "opus", None, "do it"), run
            finally:
                self.call = run.call_args

    def test_the_answer_comes_from_structured_output(self):
        result, _ = self.run_backend(claude_answer(
            structured_output={"lines": []}, total_cost_usd=0.12, modelUsage={"claude-opus-x": {}},
            usage={"input_tokens": 10, "output_tokens": 20}))
        self.assertEqual((result.data, result.backend, result.model, result.cost_estimate_usd),
                         ({"lines": []}, "subscription", "claude-opus-x", 0.12))
        self.assertEqual(result.usage["output_tokens"], 20)

        # The chapter goes in on stdin, the schema answer needs three turns or more, and an API key never reaches
        # the child, because it would outrank the subscription token.
        cmd, kwargs = self.call.args[0], self.call.kwargs
        self.assertEqual(kwargs["input"], "chapter text")
        self.assertGreaterEqual(int(cmd[cmd.index("--max-turns") + 1]), 3)
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), SCHEMA)
        self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
        self.assertEqual(kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "t")

    def test_a_usage_limit_message_raises_usage_limit_error(self):
        with self.assertRaises(llm.UsageLimitError) as e:
            self.run_backend(claude_answer(result="You've hit your session limit · resets 3:45pm", is_error=True))
        self.assertEqual(e.exception.reset_text, "3:45pm")
        self.assertIn("session limit", str(e.exception))

    def test_a_failed_or_empty_answer_raises_llm_error(self):
        for answer in (claude_answer(result="boom", is_error=True), claude_answer(returncode=1, result="exit"),
                       claude_answer(result="no object here")):
            with self.assertRaises(llm.LLMError) as e:
                self.run_backend(answer)
            self.assertNotIsInstance(e.exception, llm.UsageLimitError)


class FakeBackend:
    """A backend that raises the limit error for its first `limits` calls and then answers."""

    def __init__(self, name: str, limits: int = 0):
        self.name, self.limits, self.calls = name, limits, 0

    def run(self, *args) -> llm.PassResult:
        self.calls += 1
        if self.calls <= self.limits:
            raise llm.UsageLimitError("subscription session limit reached", "3:45pm")
        return llm.PassResult(data={"from": self.name}, backend=self.name, model="m", cost_estimate_usd=None)


class RunPassTest(unittest.TestCase):
    def run_pass(self, limits: int, **kwargs):
        self.subscription, self.api = FakeBackend("subscription", limits), FakeBackend("api")
        with mock.patch.object(llm, "ClaudeCodeBackend", lambda: self.subscription), \
                mock.patch.object(llm, "AnthropicBackend", lambda: self.api), \
                mock.patch.object(llm.time, "sleep") as self.sleep:
            return llm.run_pass("system", "text", SCHEMA, log=lambda *_: None, **kwargs)

    def test_the_subscription_answers_first(self):
        self.assertEqual(self.run_pass(0).backend, "subscription")
        self.assertEqual(self.api.calls, 0)
        self.assertEqual(self.run_pass(0, backend="api").backend, "api")
        self.assertEqual(self.subscription.calls, 0)

    def test_at_the_limit_each_policy_does_what_it_says(self):
        # api falls back once, fail raises, and subscription never falls back.
        self.assertEqual(self.run_pass(1, on_limit="api").backend, "api")
        with self.assertRaises(llm.UsageLimitError):
            self.run_pass(1, on_limit="fail")
        with self.assertRaises(llm.UsageLimitError):
            self.run_pass(1, backend="subscription", on_limit="api")
        self.assertEqual(self.api.calls, 0)

        # wait sleeps for a minute or more per limit message and asks the subscription again.
        self.assertEqual(self.run_pass(2, on_limit="wait").backend, "subscription")
        self.assertEqual((self.subscription.calls, self.api.calls, self.sleep.call_count), (3, 0, 2))
        self.assertGreaterEqual(self.sleep.call_args.args[0], 60)

    def test_unknown_names_are_refused(self):
        with self.assertRaises(ValueError):
            self.run_pass(0, model="gpt")
        with self.assertRaises(ValueError):
            self.run_pass(0, backend="other")

    def test_reset_times_parse_to_the_next_matching_time(self):
        now = dt.datetime.now()
        at = llm._parse_reset("3:45pm")
        self.assertEqual((at.hour, at.minute), (15, 45))
        self.assertTrue(dt.timedelta(0) < at - now <= dt.timedelta(days=1))
        at = llm._parse_reset("Mon 12:00am")
        self.assertEqual((at.weekday(), at.hour), (0, 0))
        self.assertTrue(dt.timedelta(0) < at - now <= dt.timedelta(days=7))
        self.assertIsNone(llm._parse_reset("soon"))
        self.assertIsNone(llm._parse_reset(None))


if __name__ == "__main__":
    unittest.main()
