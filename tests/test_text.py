"""Tests for the text side of the pipeline: the Royal Road and EPUB parsers, the character pass merge and its rules,
the screenplay rules, the script against its chapter, the screenplay pass and its fields, the script audit, and what
the LLM backends do at the subscription limit."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from serial_audio import breeze, cli, epub, llm, passes, royalroad, screenplay

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

    def test_italics_inside_a_paragraph_are_kept_as_markers(self):
        book = """<html><body><div class="chapter"><h2>CHAPTER I.<br/><i>A</i> SHIFTING REEF</h2>
<p>He was <em>very </em>tired, and thought: <i>not again</i>.</p>
<p>It was<em> loud</em>, an empty <em></em>run, and a <i>,</i> comma.</p>
<p><i>Well, that's that.</i></p>
<p>* * *</p>
<p>He said f*** and left**.</p>
<div class="fig"><p class="caption">The <i>Nautilus</i> at rest.</p></div></div></body></html>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.epub"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("META-INF/container.xml", CONTAINER)
                z.writestr("OEBPS/content.opf", OPF)
                z.writestr("OEBPS/c1.xhtml", book)
            _, chapters = epub.extract(path)

        # A marker closes before trailing space, opens after leading space, and an empty or punctuation run loses it.
        self.assertEqual(chapters[0].paragraphs[:3], ["He was *very* tired, and thought: *not again*.",
                                                      "It was *loud*, an empty run, and a , comma.",
                                                      "*Well, that's that.*"])

        # A paragraph without italics keeps the asterisks the book has itself, like a scene break.
        self.assertEqual(chapters[0].paragraphs[3:], ["* * *", "He said f*** and left**."])

        # A heading stays plain.
        self.assertEqual((chapters[0].label, chapters[0].title), ("Chapter I", "A Shifting Reef"))

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

    def test_asterisks_in_a_lines_text_are_sent_back(self):
        problems, _ = self.check([{"speaker": "narrator", "text": "The harbour was *quiet*."},
                                  {"speaker": "mira", "text": "Nobody answered,"},
                                  {"speaker": "narrator", "text": "said Mira."}])
        self.assertEqual(problems, ["line 0: `text` has asterisks around words. Drop them and keep the words between "
                                    "them."])

        # An asterisk that doesn't mark italics, as in a censored word or a scene break, is not a problem.
        for text in ("The harbour was f*cking quiet.", "The harbour was q***t and f**ing quiet.", "The harbour * was quiet.",
                     "The harbour was quiet. * * *"):
            problems, _ = self.check([{"speaker": "narrator", "text": text},
                                      {"speaker": "mira", "text": "Nobody answered,"},
                                      {"speaker": "narrator", "text": "said Mira."}])
            self.assertFalse([p for p in problems if "asterisks" in p], text)

    def test_a_cast_changes_nothing_the_pass_is_sent(self):
        # A screenplay is written before its chapter's speakers are cast or beside the cast, so the pass's input
        # names the chapter's characters and doesn't say who has a voice.
        serial = {"title": "Harbour"}
        meta = {"number": 1, "label": "Chapter 1", "title": "Morning", "characters_present": ["mira"]}
        mira = {"name": "Mira", "aliases": ["the guard"]}
        uncast = {"characters": {"mira": mira, "odo": {"name": "Odo"}}, "pronunciations": {}}
        cast = {"characters": {"mira": {**mira, "voice_id": "mira", "voice_status": "auto", "bio": "A guard."},
                               "odo": {"name": "Odo"}}, "pronunciations": {}}
        sent = screenplay.build_input(serial, meta, TEXT, uncast)
        self.assertEqual(screenplay.build_input(serial, meta, TEXT, cast), sent)
        self.assertIn("id | name | aliases\nmira | Mira | the guard\n", sent)
        self.assertNotIn("odo", sent)


MARKED = [{"speaker": "narrator", "text": "The harbour was quiet.", "direction": ""},
          {"speaker": "mira", "text": "Nobody answered,", "direction": "shouting, panicked"},
          {"speaker": "narrator", "text": "said Mira’s.", "direction": ""}]


class MarksTest(unittest.TestCase):
    def entry(self, line, **marks) -> dict:
        return {"line": line, "emphasis": [], "focus": "", "heteronyms": [], "emotion": "", "energy": "", "pace": "",
                **marks}

    def test_a_mark_is_kept_when_its_line_has_a_place_for_it(self):
        # A word is found whole, in any case and with either apostrophe, and a directed line keeps what restates
        # its direction.
        placed, dropped = screenplay.placed_marks([
            self.entry(0, emphasis=["harbour was"], focus="QUIET"),
            self.entry(1, emotion="panicked", energy="shout"),
            self.entry(2, emphasis=["Mira's"], heteronyms=[{"word": "said", "sounds_like": "sed"}])], MARKED)
        self.assertEqual(dropped, 0)
        self.assertEqual(placed[0], {"emphasis": ["harbour was"], "focus": "QUIET", "heteronyms": [], "emotion": "",
                                     "energy": "", "pace": ""})
        self.assertEqual((placed[1]["emotion"], placed[1]["energy"], placed[1]["pace"]), ("panicked", "shout", ""))
        self.assertEqual((placed[2]["emphasis"], placed[2]["heteronyms"]),
                         (["Mira's"], [{"word": "said", "sounds_like": "sed"}]))

    def test_a_mark_without_a_place_is_left_out_and_counted(self):
        # A word the line doesn't have, a part of a word, a line the script doesn't have, and an emotion on a line
        # without a direction.
        placed, dropped = screenplay.placed_marks([
            self.entry(0, emphasis=["harb", "quiet", ""], focus="Somebody", emotion="calm"),
            self.entry(1, heteronyms=[{"word": "read", "sounds_like": "red"}]),
            self.entry(3, emphasis=["said"]), self.entry(-1), self.entry(True)], MARKED)
        self.assertEqual(dropped, 8)

        # An energy or a pace outside the schema's values is left out, and an emotion loses its outer spaces.
        odd, left = screenplay.placed_marks([self.entry(1, energy="deafening", pace="brisk", emotion=" angry ")], MARKED)
        self.assertEqual((odd[1]["energy"], odd[1]["pace"], odd[1]["emotion"], left), ("", "", "angry", 2))
        self.assertEqual(placed, {0: {"emphasis": ["quiet"], "focus": "", "heteronyms": [], "emotion": "", "energy": "",
                                      "pace": ""},
                                  1: {"emphasis": [], "focus": "", "heteronyms": [], "emotion": "", "energy": "",
                                      "pace": ""}})

        # An answer of the wrong types gives no marks and doesn't raise.
        for found in (None, "marks", [None, "x", 3], [{"line": "0"}],
                      [{"line": 0, "emphasis": "quiet", "focus": 5, "heteronyms": ["read"], "energy": None}],
                      [{"line": 0, "emphasis": 5, "heteronyms": {"word": "quiet"}, "focus": ["quiet"], "pace": 3,
                        "emotion": {"a": 1}}]):
            placed, _ = screenplay.placed_marks(found, MARKED)
            self.assertFalse(any(any(m.values()) for m in placed.values()), found)

    def test_a_line_gets_all_six_fields_and_the_pass_reads_numbered_rows(self):
        bare = {"speaker": "mira", "text": "Nobody answered,", "direction": ""}
        self.assertEqual(screenplay.with_marks(bare), {**bare, "emphasis": [], "focus": "", "heteronyms": [],
                                                       "emotion": "", "energy": "", "pace": ""})
        again = screenplay.with_marks({**bare, "focus": "old", "emphasis": ["old"]}, {"focus": "Nobody"})
        self.assertEqual((again["focus"], again["emphasis"]), ("Nobody", []))
        rows = screenplay.marks_input("# One\n\nBody *text*.", MARKED).split("## Script\n\n")[1].splitlines()
        self.assertEqual(rows, ["line | speaker | direction | text", "0 | narrator | - | The harbour was quiet.",
                                "1 | mira | shouting, panicked | Nobody answered,", "2 | narrator | - | said Mira’s."])


GATE = ("# Chapter 2: The Gate (3)\n\nMr. Adrissant was late again, and the gate was shut.\n\n> +5 Agility\n\n"
        "“Open it,” he said. “Open it *now*.”\n\nThe guard did not move. She had orders from the captain, and the "
        "captain was asleep.\n\n* * *\n\nBy noon the whole town knew.\n")
GATE_SCRIPT = [{"speaker": "narrator", "text": "Chapter 2. The Gate, part three."},
               {"speaker": "narrator", "text": "Mister Adrissant was late again, and the gate was shut."},
               {"speaker": "narrator", "text": "plus 5 Agility"},
               {"speaker": "mira", "text": "Open it,"},
               {"speaker": "narrator", "text": "he said."},
               {"speaker": "mira", "text": "Open it now."},
               {"speaker": "narrator", "text": "The guard did not move. She had orders from the captain, and the "
                                               "captain was asleep."},
               {"speaker": "narrator", "text": "By noon the whole town knew."}]
GUARD = "The guard did not ... the captain was asleep"


class ScriptAgainstChapterTest(unittest.TestCase):
    def test_a_faithful_script_differs_only_by_its_normalizations(self):
        # The heading lines, the set-off marker, the asterisks and the scene break are left out.
        self.assertEqual(screenplay.text_runs(GATE, GATE_SCRIPT), [
            {"kind": "dropped", "words": 1, "line": 1, "quote": "Mr"},
            {"kind": "added", "words": 1, "line": 1, "quote": "Mister"},
            {"kind": "added", "words": 1, "line": 2, "quote": "plus"}])
        self.assertEqual(screenplay.long_runs(GATE, GATE_SCRIPT), [])
        self.assertEqual(screenplay.validate_script({"lines": GATE_SCRIPT}, BIBLE, GATE)[0], [])

        # A first line that has the chapter's opening words is chapter text, so its words count.
        self.assertEqual(screenplay.text_runs(GATE, GATE_SCRIPT[1:]), [
            {"kind": "dropped", "words": 1, "line": 0, "quote": "Mr"},
            {"kind": "added", "words": 1, "line": 0, "quote": "Mister"},
            {"kind": "added", "words": 1, "line": 1, "quote": "plus"}])

        # Any other first line is the heading as a reader says it, whatever words it uses, and it is left out.
        other = [{"speaker": "narrator", "text": "Prologue. The Gate."}] + GATE_SCRIPT[1:]
        self.assertEqual(screenplay.text_runs(GATE, other), screenplay.text_runs(GATE, GATE_SCRIPT))
        road = "# Ch. 5: The Long Road Home\n\nThe road ran north along the river for a day and a night.\n"
        spoken = [{"speaker": "narrator", "text": "Chapter Five. The Long Road Home."},
                  {"speaker": "narrator", "text": "The road ran north along the river for a day and a night."}]
        self.assertEqual(screenplay.text_runs(road, spoken), [])

        # A chapter without a heading doesn't have a heading line in its script.
        self.assertEqual(screenplay.text_runs(road.split("\n", 2)[2], spoken),
                         [{"kind": "added", "words": 6, "line": 0, "quote": "Chapter Five The Long Road Home"}])

    def test_a_dropped_paragraph_is_found_and_quoted(self):
        script = GATE_SCRIPT[:6] + GATE_SCRIPT[7:]
        self.assertEqual(screenplay.long_runs(GATE, script),
                         [{"kind": "dropped", "words": 16, "line": 6, "quote": GUARD}])
        problems, _ = screenplay.validate_script({"lines": script}, BIBLE, GATE)
        self.assertIn(f"the script leaves out 16 words of the chapter before line 6: \"{GUARD}\". Put them back where "
                      f"the chapter has them.", problems)

        # A run at the end of the chapter falls after the last line.
        problems, _ = screenplay.validate_script({"lines": GATE_SCRIPT[:7]}, BIBLE, GATE + "\nThen the bells rang out over the water.\n")
        self.assertIn("the script leaves out 14 words of the chapter after the last line: \"By noon the whole ... out over "
                      "the water\". Put them back where the chapter has them.", problems)

    def test_a_repeated_line_is_found_and_quoted(self):
        script = GATE_SCRIPT[:7] + GATE_SCRIPT[6:]
        self.assertEqual(screenplay.long_runs(GATE, script), [{"kind": "added", "words": 16, "line": 7, "quote": GUARD}])
        problems, _ = screenplay.validate_script({"lines": script}, BIBLE, GATE)
        self.assertIn(f"line 7: 16 words are not in the chapter at this place: \"{GUARD}\". Take out the repeated or "
                      f"added words.", problems)

    def test_the_limits_are_the_longest_runs_that_pass(self):
        script = [dict(line) for line in GATE_SCRIPT]
        for size, flagged in ((screenplay.MAX_DROPPED_RUN, False), (screenplay.MAX_DROPPED_RUN + 1, True)):
            script[7]["text"] = " ".join("By noon the whole town knew.".split()[size:])
            self.assertEqual(bool(screenplay.long_runs(GATE, script)), flagged, size)
        script[7]["text"] = "By noon the whole town knew."
        for size, flagged in ((screenplay.MAX_ADDED_RUN, False), (screenplay.MAX_ADDED_RUN + 1, True)):
            script[4]["text"] = "he said " + " ".join(["loudly"] * size) + "."
            self.assertEqual(bool(screenplay.long_runs(GATE, script)), flagged, size)


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
    """A backend that raises the limit error for its first `limits` calls and then answers, with each of `answers` in
    turn while it has them. It keeps the instruction of every call."""

    def __init__(self, name: str, limits: int = 0, answers: list[dict] | None = None):
        self.name, self.limits, self.calls = name, limits, 0
        self.answers, self.instructions = list(answers or []), []

    def run(self, *args) -> llm.PassResult:
        self.calls += 1
        self.instructions.append(args[-1])
        if self.calls <= self.limits:
            raise llm.UsageLimitError("subscription session limit reached", "3:45pm")
        data = self.answers.pop(0) if self.answers else {"from": self.name}
        return llm.PassResult(data=data, backend=self.name, model="m", cost_estimate_usd=None)


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


HARBOUR = ("# Chapter 7: Morning (1)\n\nMira woke before dawn and found the harbour *empty*.\n\n*Something’s wrong.*\n\n"
           "> [Quest received: Find the missing fleet before the tide turns.]\n\n"
           "“I said Tuesday, not Monday,” Mira told the harbourmaster. “Read the ledger again.”\n\n* * *\n\n"
           "The old man had read it twice already, and the wind would not wait for a third reading.\n")
NO_FIELDS = {"emphasis": [], "focus": "", "heteronyms": [], "emotion": "", "energy": "", "pace": ""}
HARBOUR_SCRIPT = [
    {"speaker": "narrator", "text": "Chapter 7. Morning, part one.", "direction": "", **NO_FIELDS},
    {"speaker": "narrator", "text": "Mira woke before dawn and found the harbour empty.", "direction": "",
     **NO_FIELDS, "emphasis": ["empty"]},
    {"speaker": "mira", "text": "Something’s wrong.", "direction": "thought, quietly", **NO_FIELDS, "energy": "soft"},
    {"speaker": "narrator", "text": "[Quest received: Find the missing fleet before the tide turns.]",
     "direction": "system notification, flat and formal", **NO_FIELDS},
    {"speaker": "mira", "text": "I said Tuesday, not Monday,", "direction": "", **NO_FIELDS, "focus": "Tuesday"},
    {"speaker": "narrator", "text": "Mira told the harbourmaster.", "direction": "", **NO_FIELDS},
    {"speaker": "mira", "text": "Read the ledger again.", "direction": "", **NO_FIELDS,
     "heteronyms": [{"word": "Read", "sounds_like": "reed"}]},
    {"speaker": "narrator", "text": "The old man had read it twice already, and the wind would not wait for a third "
                                    "reading.", "direction": "", **NO_FIELDS,
     "heteronyms": [{"word": "read", "sounds_like": "red"}]}]


HARBOUR_BARE = [{name: line[name] for name in ("speaker", "text", "direction")} for line in HARBOUR_SCRIPT]
HARBOUR_MARKS = {"marks": [{"line": i, **{name: line[name] for name in NO_FIELDS}}
                           for i, line in enumerate(HARBOUR_SCRIPT) if any(line[name] for name in NO_FIELDS)]}


class ScreenplayPassTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(screenplay.lib, "LIBRARY", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        lib = screenplay.lib
        lib.save_serial("harbour", {"title": "Harbour", "author": "A. Writer",
                                    "narration": {"point_of_view": "third_person"}})
        lib.save_yaml(lib.serial_dir("harbour") / "bible" / "characters.yaml", {"mira": {"name": "Mira"}})
        (lib.chapter_dir("harbour", 7)).mkdir(parents=True)
        (lib.chapter_dir("harbour", 7) / "text.md").write_text(HARBOUR, encoding="utf-8")
        lib.save_chapter_meta("harbour", 7, {"number": 7, "label": "Chapter 7", "title": "Morning (1)",
                                             "word_count": 52, "status": "characters", "characters_present": ["mira"]})

    def run_pass(self, *answers: dict, log=lambda *_: None, marks: bool = True) -> FakeBackend:
        backend = FakeBackend("subscription", answers=list(answers))
        with mock.patch.object(llm, "ClaudeCodeBackend", lambda: backend):
            screenplay.screenplay_pass("harbour", 7, backend="subscription", log=log, marks=marks)
        return backend

    def saved(self) -> dict:
        return screenplay.lib.load_json(screenplay.lib.chapter_dir("harbour", 7) / "script.json")

    def test_a_dropped_paragraph_and_asterisks_are_sent_back_and_the_fields_are_saved(self):
        broken = [dict(line) for line in HARBOUR_BARE[:7]]
        broken[1] = {**broken[1], "text": "Mira woke before dawn and found the harbour *empty*."}
        backend = self.run_pass({"lines": broken, "notes": ""}, {"lines": HARBOUR_BARE, "notes": ""}, HARBOUR_MARKS)

        # The second request names the dropped run by its first and last words, and the asterisks.
        self.assertEqual(backend.calls, 3)
        retry = backend.instructions[1]
        self.assertIn("the script leaves out 18 words of the chapter after the last line: \"The old man had ... for a "
                      "third reading\"", retry)
        self.assertIn("line 1: `text` has asterisks around words.", retry)

        # The script is the second answer with the marks of the third, every line has the six fields, and no text
        # has an asterisk.
        saved = self.saved()
        self.assertEqual(saved["lines"], HARBOUR_SCRIPT)
        self.assertTrue(all(set(NO_FIELDS) <= set(line) for line in saved["lines"]))
        self.assertFalse([line for line in saved["lines"] if "*" in line["text"]])
        meta = screenplay.lib.load_chapter("harbour", 7)[0]
        self.assertEqual((meta["status"], sorted(meta["passes"])), ("scripted", ["marks", "screenplay"]))

    def test_the_marks_pass_reads_the_saved_script_and_leaves_out_what_it_cannot_place(self):
        # The marks name three words their lines don't have: an emphasis, a focus and a heteronym.
        wrong = {"marks": [dict(entry) for entry in HARBOUR_MARKS["marks"]]}
        by_line = {entry["line"]: entry for entry in wrong["marks"]}
        by_line[1]["emphasis"] = ["empty", "full"]
        by_line[4]["focus"] = "Wednesday"
        by_line[6]["heteronyms"] = [{"word": "lead", "sounds_like": "led"}]
        logs: list[str] = []
        backend = self.run_pass({"lines": HARBOUR_BARE, "notes": ""}, wrong, log=logs.append)

        # The script is saved without them, with the words its lines do have, and the log counts what was left out.
        self.assertEqual(backend.calls, 2)
        self.assertIn("Return the marks of the script above", backend.instructions[1])
        lines = self.saved()["lines"]
        self.assertEqual((lines[1]["emphasis"], lines[4]["focus"], lines[6]["heteronyms"]), (["empty"], "", []))
        self.assertEqual(lines[7]["heteronyms"], [{"word": "read", "sounds_like": "red"}])
        self.assertTrue(any("marks on 3 of 8 lines, 3 left out" in line for line in logs), logs)

        # A later marks pass replaces the marks and leaves each line's speaker, text and direction as they were.
        backend = FakeBackend("subscription", answers=[HARBOUR_MARKS])
        with mock.patch.object(llm, "ClaudeCodeBackend", lambda: backend):
            screenplay.marks_pass("harbour", 7, backend="subscription", log=lambda *_: None)
        self.assertEqual(self.saved()["lines"], HARBOUR_SCRIPT)

    def test_a_marks_pass_that_fails_leaves_the_script_with_empty_fields(self):
        logs: list[str] = []
        backend = FakeBackend("subscription", answers=[{"lines": HARBOUR_BARE, "notes": ""}])
        real = backend.run

        def run(*args):
            if backend.calls >= 1:
                backend.calls += 1
                raise llm.LLMError("the answer was not JSON")
            return real(*args)

        backend.run = run
        with mock.patch.object(llm, "ClaudeCodeBackend", lambda: backend):
            screenplay.screenplay_pass("harbour", 7, backend="subscription", log=logs.append, marks=True)

        # The chapter is scripted all the same, with the lines as the screenplay pass wrote them.
        self.assertEqual(backend.calls, 2)
        self.assertEqual(self.saved()["lines"], HARBOUR_BARE)
        self.assertNotIn("marks", self.saved())
        self.assertEqual(screenplay.lib.load_chapter("harbour", 7)[0]["status"], "scripted")
        self.assertTrue(any("the marks pass failed" in line for line in logs), logs)

        # Without the marks asked for, the screenplay pass is one request and the script has its three fields.
        backend = self.run_pass({"lines": HARBOUR_BARE, "notes": ""}, HARBOUR_MARKS, marks=False)
        self.assertEqual((backend.calls, self.saved()["lines"]), (1, HARBOUR_BARE))

        # A chapter without a script has nothing to mark.
        with self.assertRaises(FileNotFoundError):
            screenplay.marks_pass("harbour", 8, backend="subscription", log=lambda *_: None)

    def test_a_script_that_still_drops_text_is_not_saved(self):
        short = {"lines": HARBOUR_BARE[:7], "notes": ""}
        with self.assertRaises(llm.LLMError) as e:
            self.run_pass(short, short)
        self.assertIn("leaves out 18 words", str(e.exception))
        self.assertFalse((screenplay.lib.chapter_dir("harbour", 7) / "script.json").exists())

    def test_the_two_schemas_name_the_fields_of_a_line_and_of_its_marks(self):
        # A line of the screenplay pass is its speaker, text and direction.
        line = screenplay.lib.load_json(screenplay.SCHEMA)["properties"]["lines"]["items"]
        self.assertEqual(line["required"], ["speaker", "text", "direction"])
        self.assertEqual(set(line["properties"]), set(line["required"]))
        self.assertFalse(line["additionalProperties"])

        # An entry of the marks pass is a line number and all six fields.
        marks = screenplay.lib.load_json(screenplay.MARKS_SCHEMA)["properties"]["marks"]["items"]
        self.assertEqual(marks["required"], ["line", *NO_FIELDS])
        self.assertEqual((list(marks["properties"]), list(NO_FIELDS)), (marks["required"], list(screenplay.MARKS)))
        self.assertFalse(marks["additionalProperties"])
        self.assertEqual((marks["properties"]["energy"]["enum"], marks["properties"]["pace"]["enum"]),
                         (["", "whisper", "soft", "normal", "raised", "shout"], ["", "slow", "fast", "halting"]))

    def test_only_emphasis_changes_a_take_key_and_only_of_its_line(self):
        slots, voices, clips = {"narrator": 0, "mira": 1}, {0: "narrator", 1: "mira"}, {"narrator": "a" * 40, "mira": "b" * 40}
        bare = [{k: line[k] for k in ("speaker", "text", "direction")} for line in HARBOUR_SCRIPT]
        bible = {"characters": {"mira": {"name": "Mira"}}, "pronunciations": {}}

        # The focus, the heteronyms, the emotion, the energy and the pace leave every take key as it was.
        others = [{**line, "emphasis": []} for line in HARBOUR_SCRIPT]
        for runner in ("cpp", "comfyui"):
            self.assertEqual(breeze.plan_takes(others, slots, voices, bible, 3, clips, runner),
                             breeze.plan_takes(bare, slots, voices, bible, 3, clips, runner), runner)

        # The emphasis of line 1 puts its word between asterisks in the text the voice is sent, so that line has
        # another key, and every other line keeps its own.
        for runner in ("cpp", "comfyui"):
            marked = breeze.plan_takes(HARBOUR_SCRIPT, slots, voices, bible, 3, clips, runner)
            plain = breeze.plan_takes(bare, slots, voices, bible, 3, clips, runner)
            self.assertEqual([i for i, (a, b) in enumerate(zip(marked, plain)) if a != b], [1], runner)
            self.assertEqual(marked[1][2], "Mira woke before dawn and found the harbour *empty*.")
            self.assertNotEqual(marked[1][5], plain[1][5], runner)


STORM = ("# Chapter 2: The Storm\n\n"
         "“I *told* you the tide would turn,” said Mira. “*Now* we *have to* row, and I mean *all of us at once*.”\n\n"
         "*Shadowchain* snapped across the deck, and the crew called it *the Long Night* ever after.\n\n"
         "“*Mira* said it would hold. Did *Mira* check? Because *I* did not, and *we’re* done for,” said Tam.\n\n"
         "*It will not hold.*\n\n"
         "The rope *held*. The rope held for an hour, and the *far shore* came no nearer. A *lost* line follows.\n")
STORM_SCRIPT = [
    {"speaker": "narrator", "text": "Chapter 2. The Storm.", "direction": ""},
    {"speaker": "mira", "text": "I told you the tide would turn,", "direction": ""},
    {"speaker": "narrator", "text": "said Mira.", "direction": ""},
    {"speaker": "mira", "text": "Now we have to row, and I mean all of us at once.", "direction": "shouting"},
    {"speaker": "narrator", "text": "Shadowchain snapped across the deck, and the crew called it the Long Night ever "
                                    "after.", "direction": ""},
    {"speaker": "tam", "text": "Mira said it would hold. Did Mira check? Because I did not, and we’re done for,",
     "direction": ""},
    {"speaker": "narrator", "text": "said Tam.", "direction": ""},
    {"speaker": "tam", "text": "It will not hold.", "direction": "thought"},
    {"speaker": "narrator", "text": "The rope held. The rope held for an hour, and the far", "direction": ""},
    {"speaker": "narrator", "text": "shore came no nearer.", "direction": ""}]
STORM_BIBLE = {"characters": {"mira": {"name": "Mira"}, "tam": {"name": "Tam"}},
               "pronunciations": {"Shadowchain": {"respelling": "SHAD-oh-chayn"}}}


class EmphasisTest(unittest.TestCase):
    def test_the_authors_italics_for_stress_are_found_in_the_script(self):
        found = screenplay.italic_emphasis(STORM, STORM_SCRIPT, STORM_BIBLE)

        # A word, a word that opens a sentence, and a phrase of two words are emphasis. A span of over three words
        # is not.
        self.assertEqual((found[1], found[3]), (["told"], ["Now", "have to"]))

        # A term of the bible and a title are not, also where the title's first word is in lower case.
        self.assertNotIn(4, found)

        # A character's name is not, even where it opens a sentence. The pronoun I is, and so is a word with a
        # curly apostrophe, spelled as the line spells it.
        self.assertEqual(found[5], ["I", "we’re"])

        # An italic sentence that is a whole line is a thought and not emphasis. A word that stands twice in its
        # line can't be placed, a span that crosses two lines is not one stress, and a word the script doesn't
        # have is left out.
        self.assertEqual(sorted(found), [1, 3, 5])

        # A term of the bible's pronunciations is not emphasis, also in lower case and of several words.
        bible = {"pronunciations": {"coup de grace": {"respelling": "koo duh GRAHS"}, "manatree": {}}}
        text = "# 1\n\nHe dealt the *coup de grace* by the old *manatree*, and it was *over*.\n"
        script = [{"text": "He dealt the coup de grace by the old manatree, and it was over."}]
        self.assertEqual(screenplay.italic_emphasis(text, script, bible), {0: ["over"]})

        # A chapter without italics has none. One with a stressed word, an italic line and a scene break has the
        # word alone.
        self.assertEqual(screenplay.italic_emphasis("# 1\n\nNo stress here.\n", [{"text": "No stress here."}], {}), {})
        self.assertEqual(screenplay.italic_emphasis(HARBOUR, HARBOUR_BARE, {}), {1: ["empty"]})

    def test_the_marked_words_stand_between_asterisks_where_they_can_be_placed(self):
        # Each phrase that stands once in the text as whole words gets its asterisks, in the text's order.
        self.assertEqual(screenplay.emphatic("Now we have to row, and I mean it.", ["have to", "Now"]),
                         "*Now* we *have to* row, and I mean it.")

        # A phrase the text has twice or not at all, part of a longer word, and one that overlaps an earlier
        # phrase are left alone, and so is anything that is not a list of phrases.
        self.assertEqual(screenplay.emphatic("The rope held. The rope held for an hour.", ["held"]),
                         "The rope held. The rope held for an hour.")
        self.assertEqual(screenplay.emphatic("He told them, untold times.", ["told", "gone", "told them", ""]),
                         "He *told* them, untold times.")
        for wrong in (None, "told", [3, None], {"told": 1}):
            self.assertEqual(screenplay.emphatic("He told them.", wrong), "He told them.", wrong)

        # A name with a say-as keeps its spoken spelling inside the asterisks, and the line's token map is the one
        # the line has without the field.
        bible = {"characters": {}, "pronunciations": {"Eole": {"say": "Ay-ohl"}}}
        line = {"speaker": "narrator", "text": "Only Eole’s song could wake them.", "direction": ""}
        text, tokens = breeze.voice_text({**line, "emphasis": ["Eole", "could"]}, bible)
        self.assertEqual(text, "Only *Ay-ohl*’s song *could* wake them.")
        self.assertEqual(tokens, breeze.voice_text(line, bible)[1])
        self.assertEqual([t["name"] for t in tokens if t["name"]], ["Eole", "Eole"])
        accented = {"characters": {}, "pronunciations": {"Eole": {"say": "Éole"}}}
        text, tokens = breeze.voice_text({**line, "emphasis": ["Eole"]}, accented)
        self.assertEqual(text, "Only *Éole*’s song could wake them.")
        self.assertEqual((tokens, tokens[1]["name"]), (breeze.voice_text(line, accented)[1], "Eole"))

        # An asterisk never lands inside a say-as term of several words, which would stop its spoken spelling, nor
        # inside a vocal event, which would be read aloud. The whole term can be marked.
        bible = {"characters": {}, "pronunciations": {"sea wyrm": {"say": "seaworm"}}}
        line = {"speaker": "narrator", "text": "(sigh) The sea wyrm rose again.", "direction": ""}
        for stressed, sent in ((["wyrm"], "(sigh) The seaworm rose again."), (["sea"], "(sigh) The seaworm rose again."),
                               (["sigh"], "(sigh) The seaworm rose again."),
                               (["sea wyrm", "again"], "(sigh) The *seaworm* rose *again*.")):
            text, tokens = breeze.voice_text({**line, "emphasis": stressed}, bible)
            self.assertEqual((text, tokens), (sent, breeze.voice_text(line, bible)[1]), stressed)

    def test_a_script_gets_the_field_once_and_keeps_its_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(screenplay.lib, "LIBRARY", Path(tmp)):
            lib = screenplay.lib
            lib.save_yaml(lib.serial_dir("storm") / "bible" / "characters.yaml", STORM_BIBLE["characters"])
            lib.save_yaml(lib.serial_dir("storm") / "bible" / "pronunciations.yaml", STORM_BIBLE["pronunciations"])
            lib.chapter_dir("storm", 2).mkdir(parents=True)
            (lib.chapter_dir("storm", 2) / "text.md").write_text(STORM, encoding="utf-8")
            lib.save_chapter_meta("storm", 2, {"number": 2})
            path = lib.chapter_dir("storm", 2) / "script.json"
            marked = [dict(line) for line in STORM_SCRIPT]
            marked[2] = {**marked[2], "emphasis": ["Mira"], "focus": "Mira"}
            lib.save_json(path, {"lines": marked, "notes": "n"})

            # The lines with italics for stress get the field, a line that had it without them gets an empty one
            # and keeps its other fields, and every other line stays as it was.
            logs: list[str] = []
            lines = screenplay.write_emphasis("storm", 2, logs.append)
            saved = lib.load_json(path)
            self.assertEqual((saved["emphasis"], saved["notes"], saved["lines"]), (screenplay.EMPHASIS_V, "n", lines))
            self.assertEqual({i: line["emphasis"] for i, line in enumerate(lines) if "emphasis" in line},
                             {1: ["told"], 2: [], 3: ["Now", "have to"], 5: ["I", "we’re"]})
            self.assertEqual(lines[2]["focus"], "Mira")
            self.assertEqual([{k: v for k, v in line.items() if k != "emphasis"} for line in lines],
                             [{k: v for k, v in line.items() if k != "emphasis"} for line in marked])
            self.assertEqual(logs, ["chapter 2: 3 of 10 lines have a word the chapter sets in italics for stress"])

            # A script that records the version is left alone, also when its text has other italics by now.
            (lib.chapter_dir("storm", 2) / "text.md").write_text(STORM.replace("*told*", "told"), encoding="utf-8")
            before = path.read_bytes()
            self.assertEqual(screenplay.write_emphasis("storm", 2, logs.append), lines)
            self.assertEqual((path.read_bytes(), len(logs)), (before, 1))

            # A script from an older version gets the field again from the text as it is: line 1 lost its italics,
            # so its field is empty and the voice gets its text without asterisks.
            lib.save_json(path, {**saved, "emphasis": screenplay.EMPHASIS_V - 1})
            again = screenplay.write_emphasis("storm", 2, logs.append)
            self.assertEqual((again[1], again[3]["emphasis"]), ({**STORM_SCRIPT[1], "emphasis": []}, ["Now", "have to"]))
            self.assertEqual(breeze.voice_text(again[1], STORM_BIBLE)[0], "I told you the tide would turn")
            self.assertEqual(breeze.voice_text(lines[1], STORM_BIBLE)[0], "I *told* you the tide would turn")

            # A file without lines is not a script, and it is left as it was.
            lib.save_json(path, {"notes": "n"})
            before = path.read_bytes()
            with self.assertRaises(KeyError):
                screenplay.write_emphasis("storm", 2, logs.append)
            self.assertEqual(path.read_bytes(), before)


class AuditScriptsTest(unittest.TestCase):
    def test_the_audit_lists_the_runs_over_the_limits_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(screenplay.lib, "LIBRARY", Path(tmp)):
            lib = screenplay.lib
            for n, lines in ((1, GATE_SCRIPT), (2, GATE_SCRIPT[:6] + GATE_SCRIPT[7:]), (3, None)):
                lib.chapter_dir("gate", n).mkdir(parents=True)
                (lib.chapter_dir("gate", n) / "text.md").write_text(GATE, encoding="utf-8")
                lib.save_chapter_meta("gate", n, {"number": n})
                if lines:
                    lib.save_json(lib.chapter_dir("gate", n) / "script.json", {"lines": lines})
            before = {p: p.read_bytes() for p in Path(tmp).rglob("*") if p.is_file()}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["audit-scripts", "gate"]), 0)
            after = {p: p.read_bytes() for p in Path(tmp).rglob("*") if p.is_file()}

        # Chapter 2 leaves out a paragraph, chapter 1 is faithful, and chapter 3 has no script.
        self.assertEqual(out.getvalue().splitlines(), [
            f"gate: 1 of 2 scripts have a run of more than {screenplay.MAX_DROPPED_RUN} dropped or "
            f"{screenplay.MAX_ADDED_RUN} added words",
            " chapter  kind     words  line  quote",
            f"       2  dropped     16     6  \"{GUARD}\""])
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
