"""Tests for the look pass and the bible's images, with a fake LLM and a fake ComfyUI, and for their jobs.
Run: python -m unittest tests.test_images"""
from __future__ import annotations

import io
import json
import re
import socket
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from serial_audio import images, looks, machine
from serial_audio import library as lib
from serial_audio.comfy import IMAGE_MODELS, ComfyClient, ComfyError, image_graph
from serial_audio.llm import LLMError, PassResult
from serial_audio.web import exchange, mirror, views
from serial_audio.web.jobs import HEAVY, build, draw_progress, to_draw
from serial_audio.web.nomad import LLM_KINDS, _draw_units, conflicts, drew_nothing, job_stages

SLUG = "demo"
CHARACTERS = {
    "kael": {"name": "Kael", "aliases": ["the smith"], "gender": "male", "age_group": "adult", "role": "A smith.",
             "description": "A smith who left his forge.", "importance": "major", "chapters_present": [1, 2],
             "facts": [{"chapter": 2, "text": "Has a grey beard."}]},
    "mira": {"name": "Mira", "aliases": [], "gender": "female", "age_group": "young_adult", "role": "A courier.",
             "description": "A courier.", "importance": "recurring", "chapters_present": [1]},
    "guard": {"name": "the guard", "aliases": [], "gender": "male", "age_group": "adult", "role": "A gate guard.",
              "description": "A guard.", "importance": "one_off", "chapters_present": [2]},
}


def fact(text: str, lasting: bool = True) -> dict:
    return {"text": text, "lasting": lasting}


def job(kind, slug=SLUG, chapters=None, args=None):
    return {"kind": kind, "slug": slug, "meta": {"chapters": chapters or []}, "args": args or [kind]}


class LibraryCase(unittest.TestCase):
    """A library in a temporary folder with one serial: three characters, and two chapters with a character pass."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for patch in (mock.patch.object(lib, "LIBRARY", self.root / "library"),
                      mock.patch.object(machine, "COMFY_OUTPUT", self.root / "output"),
                      mock.patch.object(machine, "breeze_server_pids", lambda: [])):
            patch.start()
            self.addCleanup(patch.stop)
        lib.save_serial(SLUG, {"title": "Demo", "author": "A. Writer", "description": "A smith goes to war.",
                               "source": {"tags": ["Fantasy"]}})
        lib.save_bible(SLUG, {"characters": {k: dict(v) for k, v in CHARACTERS.items()}, "pronunciations": {}})
        for n, (present, text) in enumerate(((["kael", "mira"], "Kael met Mira at the Gilded Anchor."),
                                             (["kael", "guard"], "Kael passed the guard at the gate.")), 1):
            folder = lib.chapter_dir(SLUG, n)
            folder.mkdir(parents=True)
            (folder / "text.md").write_text(f"# Chapter {n}\n\n{text}\n", encoding="utf-8")
            lib.save_chapter_meta(SLUG, n, {"number": n, "label": f"Chapter {n}", "title": f"Part {n}",
                                            "word_count": 8, "status": "characters", "characters_present": present})


class FakeLLM:
    """run_pass that gives the look pass its next queued answer and describes every record of an image pass."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls: list[tuple[str, str, str]] = []
        self.drop: set[str] = set()

    def __call__(self, system_prompt, user_text, schema, **kwargs):
        kind = "images" if "images" in schema["properties"] else "looks"
        self.calls.append((kind, user_text, kwargs.get("instruction") or ""))
        if kind == "looks":
            data = self.answers.pop(0)
        else:
            ids = re.findall(r"^## (\S+)$", user_text, re.M)
            data = {"style": f"Style {len(self.calls)}.",
                    "images": [{"id": i, "prompt": f"Picture {len(self.calls)} of {i}."} for i in ids if i not in self.drop]}
        return PassResult(data=data, backend="fake", model="fake", cost_estimate_usd=0.0)

    def count(self, kind: str) -> int:
        return sum(1 for call in self.calls if call[0] == kind)


CHAPTER_1 = {
    "characters": [{"id": "kael", "look": [fact('Broad and soot-marked, "arms like bridge cables"'),
                                           fact("A borrowed green cloak", lasting=False)]}],
    "new_settings": [{"id": "the-gilded-anchor", "name": "The Gilded Anchor", "aliases": ["the Anchor", "the inn"],
                      "description": "A harbour inn.", "look": [fact("Low beams black with smoke")],
                      "evidence": "met Mira at the Gilded Anchor"}],
    "settings": [],
}
CHAPTER_2 = {
    "characters": [{"id": "kael", "look": [fact("A burn scar across the right hand")]}],
    "new_settings": [{"id": "north-gate", "name": "the north gate", "aliases": [], "description": "A city gate.",
                      "look": [], "evidence": "at the gate"}],
    "settings": [{"id": "the-gilded-anchor", "new_aliases": ["the Anchor", "Old Anchor"], "look": []}],
}


class LookPassTest(LibraryCase):
    def run_pass(self, number, *answers, **kwargs):
        llm = FakeLLM(answers)
        with mock.patch.object(looks, "run_pass", llm):
            looks.look_pass(SLUG, number, log=lambda *a: None, **kwargs)
        return llm

    def test_a_pass_records_the_looks_and_the_settings_of_its_chapter(self):
        # The input lists the characters of this chapter alone, by id, and no setting yet.
        llm = self.run_pass(1, CHAPTER_1)
        text = llm.calls[0][1]
        self.assertIn("kael | Kael | the smith | male/adult | A smith.", text)
        self.assertIn("mira | Mira", text)
        self.assertNotIn("guard |", text)
        self.assertIn("(none yet)", text)
        self.assertIn("Kael met Mira at the Gilded Anchor.", text)

        # A detail of one scene keeps its mark. A character without a new detail gets no entry.
        self.assertEqual(looks.load_looks(SLUG), {"kael": [
            {"chapter": 1, "text": 'Broad and soot-marked, "arms like bridge cables"'},
            {"chapter": 1, "text": "A borrowed green cloak", "lasting": False}]})
        place = looks.load_settings(SLUG)["the-gilded-anchor"]
        self.assertEqual((place["name"], place["aliases"], place["chapters_present"], place["first_seen"]),
                         ("The Gilded Anchor", ["the Anchor", "the inn"], [1], 1))
        self.assertEqual(place["look"], [{"chapter": 1, "text": "Low beams black with smoke"}])

        # The chapter's own file holds the answer and who gave it, and marks the chapter as read.
        saved = lib.load_json(lib.chapter_dir(SLUG, 1) / looks.PASS_FILE)
        self.assertEqual(saved["characters"], CHAPTER_1["characters"])
        self.assertEqual(saved["pass"]["backend"], "fake")
        self.assertEqual(looks.unread(SLUG), [2])

    def test_a_later_pass_sees_the_record_and_adds_to_it(self):
        self.run_pass(1, CHAPTER_1)
        llm = self.run_pass(2, CHAPTER_2)
        text = llm.calls[0][1]

        # Each character's look so far is in the input, with the detail of one scene marked.
        self.assertIn('look so far: Broad and soot-marked, "arms like bridge cables" / A borrowed green cloak '
                      "(that scene only)", text)
        # A setting the chapter does not name is listed without its look.
        self.assertIn("the-gilded-anchor | The Gilded Anchor | the Anchor, the inn | A harbour inn.", text)
        self.assertNotIn("Low beams", text)

        # A known setting with a scene in the chapter counts the chapter and takes only the alias it lacks.
        places = looks.load_settings(SLUG)
        anchor = places["the-gilded-anchor"]
        self.assertEqual((anchor["chapters_present"], anchor["last_seen"]), ([1, 2], 2))
        self.assertEqual(anchor["aliases"], ["the Anchor", "the inn", "Old Anchor"])
        self.assertEqual(places["north-gate"]["chapters_present"], [2])
        self.assertEqual([f["chapter"] for f in looks.load_looks(SLUG)["kael"]], [1, 1, 2])
        self.assertEqual(looks.unread(SLUG), [])

    def test_a_setting_named_in_the_chapter_is_listed_with_its_look(self):
        self.run_pass(1, CHAPTER_1)
        meta, text = lib.load_chapter(SLUG, 2)
        shown = looks.build_look_input(lib.load_serial(SLUG), meta, text + " He slept at the inn.", {},
                                       looks.load_looks(SLUG), looks.load_settings(SLUG))
        self.assertIn("    look so far: Low beams black with smoke", shown)
        self.assertIn("## Characters in this chapter\n\n(none)", shown)

    def test_a_pass_of_a_chapter_again_replaces_what_the_first_recorded(self):
        self.run_pass(1, CHAPTER_1)
        self.run_pass(2, CHAPTER_2)
        again = {"characters": [{"id": "mira", "look": [fact("Red hair in a braid")]}], "new_settings": [],
                 "settings": []}
        llm = self.run_pass(1, again)

        # The input no longer has chapter 1's details, and the setting only chapter 1 made is gone from chapter 1.
        self.assertNotIn("soot-marked", llm.calls[0][1])
        record = looks.load_looks(SLUG)
        self.assertEqual(record["kael"], [{"chapter": 2, "text": "A burn scar across the right hand"}])
        self.assertEqual(record["mira"], [{"chapter": 1, "text": "Red hair in a braid"}])
        anchor = looks.load_settings(SLUG)["the-gilded-anchor"]
        self.assertEqual((anchor["chapters_present"], anchor["first_seen"], anchor["look"]), ([2], 2, []))

    def test_an_answer_that_breaks_a_rule_is_sent_back_once(self):
        bad = {"characters": [{"id": "guard", "look": [fact("A dented helm")]}],
               "new_settings": [{"id": "The Gate!", "name": "the gate", "aliases": [], "description": "A gate.",
                                 "look": [], "evidence": "x"}],
               "settings": [{"id": "nowhere", "new_aliases": [], "look": []}]}
        llm = self.run_pass(1, bad, CHAPTER_1)
        self.assertEqual(llm.count("looks"), 2)
        for part in ("`guard` under `characters` is not in the list", "`The Gate!` is not a kebab-case slug",
                     "`nowhere` under `settings` is not a known setting"):
            self.assertIn(part, llm.calls[1][2])
        self.assertIn("kael", looks.load_looks(SLUG))

        # A second bad answer fails the pass, and the record and the chapter stay as they were.
        with self.assertRaises(LLMError):
            self.run_pass(2, bad, bad)
        self.assertEqual(looks.unread(SLUG), [2])
        self.assertNotIn("guard", looks.load_looks(SLUG))

        # A setting the record has is not new, and an id can't be new twice.
        self.run_pass(2, CHAPTER_2)
        twice = dict(CHAPTER_1["new_settings"][0], id="quay")
        problems = looks.validate_look_output({"characters": [], "settings": [],
                                               "new_settings": [CHAPTER_1["new_settings"][0], twice, twice]},
                                              {}, looks.load_settings(SLUG))
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("already a setting" in p for p in problems) and any("more than once" in p for p in problems))

    def test_a_chapter_without_a_character_pass_is_refused(self):
        folder = lib.chapter_dir(SLUG, 3)
        folder.mkdir(parents=True)
        (folder / "text.md").write_text("# Chapter 3\n\nText.\n", encoding="utf-8")
        lib.save_chapter_meta(SLUG, 3, {"number": 3, "label": "Chapter 3", "title": "x", "word_count": 1, "status": "fetched"})
        with self.assertRaises(ValueError):
            self.run_pass(3, CHAPTER_1)
        self.assertEqual(looks.unread(SLUG), [1, 2])

    def test_a_detail_of_a_second_appearance_keeps_its_slug(self):
        armour = {"characters": [{"id": "kael", "look": [
            fact("Broad and soot-marked"),
            {"text": "Plate of black iron with a horned helm", "lasting": True, "appearance": "War Plate"},
            {"text": "The plate is dented at the shoulder", "lasting": False, "appearance": "war-plate"}]}],
            "new_settings": [], "settings": []}
        self.run_pass(1, armour)

        # The record keeps the appearance as a slug, however the pass wrote it, and no key for the everyday look.
        self.assertEqual(looks.load_looks(SLUG)["kael"], [
            {"chapter": 1, "text": "Broad and soot-marked"},
            {"chapter": 1, "text": "Plate of black iron with a horned helm", "appearance": "war-plate"},
            {"chapter": 1, "text": "The plate is dented at the shoulder", "lasting": False, "appearance": "war-plate"}])

        # The next chapter's input shows the slug in front of each of the appearance's details. A detail in the
        # words of an everyday one is kept when it is the appearance's.
        again = {"characters": [{"id": "kael", "look": [
            {"text": "Broad and soot-marked", "lasting": True, "appearance": "war-plate"},
            {"text": "Broad and soot-marked", "lasting": True, "appearance": ""}]}], "new_settings": [], "settings": []}
        llm = self.run_pass(2, again)
        self.assertEqual(looks.load_looks(SLUG)["kael"][3:],
                         [{"chapter": 2, "text": "Broad and soot-marked", "appearance": "war-plate"}])
        self.assertIn("look so far: Broad and soot-marked / [war-plate] Plate of black iron with a horned helm / "
                      "[war-plate] The plate is dented at the shoulder (that scene only)", llm.calls[0][1])

    def test_a_detail_keeps_a_quote_that_is_the_chapters_words(self):
        quoted = {"characters": [{"id": "kael", "look": [
            {"text": "Meets people at inns", "lasting": True, "quote": "kael met  Mira at the *Gilded Anchor*."},
            {"text": "Tall", "lasting": True, "quote": "Kael was as tall as a door."}]}],
            "new_settings": [{"id": "the-gilded-anchor", "name": "The Gilded Anchor", "aliases": [],
                              "description": "A harbour inn.", "evidence": "at the Gilded Anchor",
                              "look": [{"text": "An inn", "lasting": True, "quote": "Kael met Mira ... the Gilded Anchor."}]}],
            "settings": []}
        said: list[str] = []
        with mock.patch.object(looks, "run_pass", FakeLLM([quoted])):
            looks.look_pass(SLUG, 1, log=said.append)

        # A quote stands whatever its case, spacing and emphasis marks, and with a part left out. A quote that is
        # not in the chapter is dropped, its detail stays, and the log counts it. The parts around a gap stand
        # only in their order.
        self.assertFalse(looks.stands_in("the Gilded Anchor ... Kael met", "Kael met Mira at the Gilded Anchor."))
        self.assertFalse(looks.stands_in(None, "None of them saw it."))
        self.assertEqual(looks.load_looks(SLUG)["kael"], [
            {"chapter": 1, "text": "Meets people at inns", "quote": "kael met Mira at the *Gilded Anchor*."},
            {"chapter": 1, "text": "Tall"}])
        self.assertEqual(looks.load_settings(SLUG)["the-gilded-anchor"]["look"],
                         [{"chapter": 1, "text": "An inn", "quote": "Kael met Mira ... the Gilded Anchor."}])
        self.assertIn("chapter 1: 1 detail(s) kept without a quote", "\n".join(said))

    def test_reset_removes_the_record_and_every_pass(self):
        self.run_pass(1, CHAPTER_1)
        self.assertEqual(looks.reset(SLUG), 1)
        self.assertEqual((looks.load_looks(SLUG), looks.load_settings(SLUG), looks.unread(SLUG)), ({}, {}, [1, 2]))


class FakeClient(ComfyClient):
    """ComfyUI that writes a file for each save node of a graph and records the graphs it ran."""

    ran: list[dict] = []
    urls: set[str] = set()
    fail_at: int | None = None
    freed = 0

    def run(self, graph, timeout=900, log=None, report_every=600.0):
        if FakeClient.fail_at is not None and len(FakeClient.ran) >= FakeClient.fail_at:
            raise ComfyError("ComfyUI execution error: out of memory")
        FakeClient.ran.append(graph)
        FakeClient.urls.add(self.base_url)
        outputs = {}
        for node in ("save", "save_thumb"):
            prefix = graph[node]["inputs"]["filename_prefix"]
            sub, _, name = prefix.rpartition("/")
            path = self.output_dir / sub / f"{name}_00001_.webp"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{node} {graph['sample']['inputs']['seed']}".encode())
            outputs[node] = {"images": [{"filename": path.name, "subfolder": sub, "type": "output"}]}
        return {"outputs": outputs, "status": {"completed": True}}

    def free(self, settle=2.0):
        FakeClient.freed += 1


class FakeServer:
    """ComfyServer that starts nothing. With `found` set, it finds a ComfyUI at that address."""

    started = 0
    found: str | None = None

    def __init__(self, url, log_path, log=print):
        self.url = url

    def __enter__(self):
        FakeServer.started += 1
        self.url = FakeServer.found or self.url
        return self

    def __exit__(self, *exc):
        pass


class ImageCase(LibraryCase):
    """LibraryCase with a look record, a fake LLM for the image pass and a fake ComfyUI."""

    def setUp(self):
        super().setUp()
        looks.save(SLUG, {"kael": [{"chapter": 1, "text": "Broad and soot-marked"},
                                   {"chapter": 1, "text": "A borrowed green cloak", "lasting": False}],
                          "guard": [{"chapter": 2, "text": "A dented helm"}]},
                   {"the-gilded-anchor": {"name": "The Gilded Anchor", "aliases": [], "description": "A harbour inn.",
                                          "chapters_present": [1, 2], "look": [{"chapter": 1, "text": "Low beams"}]},
                    "north-gate": {"name": "the north gate", "aliases": [], "description": "A city gate.",
                                   "chapters_present": [2], "look": [{"chapter": 2, "text": "Iron-bound oak"}]}})
        self.llm = FakeLLM()
        FakeClient.ran, FakeClient.fail_at, FakeClient.freed, FakeServer.started = [], None, 0, 0
        FakeClient.urls, FakeServer.found = set(), None
        for patch in (mock.patch.object(images, "run_pass", self.llm),
                      mock.patch.object(images, "ComfyClient", FakeClient),
                      mock.patch.object(images, "ComfyServer", FakeServer)):
            patch.start()
            self.addCleanup(patch.stop)
        self.log: list[str] = []

    def draw(self, **kwargs):
        return images.draw(SLUG, log=self.log.append, **kwargs)

    def said(self) -> str:
        return "\n".join(self.log)


# what a description of Kael goes by in ImageCase's look record, which is not the cloak of one scene
KAEL_DETAILS = [{"chapter": 1, "text": "Broad and soot-marked"}]
WAR_PLATE = {"chapter": 2, "text": "Black plate with a horned helm", "appearance": "war-plate",
             "quote": "The plate closed over him, black, and the helm grew horns."}


class PlanTest(ImageCase):
    def found(self):
        return images.subjects(lib.load_bible(SLUG), looks.load_looks(SLUG), looks.load_settings(SLUG))

    def test_a_draw_for_everyone_takes_the_described_subjects_the_policy_covers(self):
        # Mira is recurring, and her bible entry describes her though the text has given no look detail. The guard
        # is a one-off. The gate has scenes in one chapter. So the major character, the recurring one and the
        # setting of two chapters are drawn, in that order.
        todo = images.plan(lib.load_serial(SLUG), self.found(), set())
        self.assertEqual(todo, {"keys": ["characters/kael", "characters/mira", "settings/the-gilded-anchor"],
                                "undescribed": [], "left": 0, "kept": []})

        # A setting that qualifies waits while the text has given no look detail of it.
        bare = self.found()
        bare["settings/the-gilded-anchor"]["look"] = []
        waiting = images.plan(lib.load_serial(SLUG), bare, set())
        self.assertEqual((waiting["keys"], waiting["undescribed"]),
                         (["characters/kael", "characters/mira"], ["settings/the-gilded-anchor"]))

        # A subject that has an image is left out, and the limit leaves the rest for the next draw.
        self.assertEqual(images.plan(lib.load_serial(SLUG), self.found(), {"characters/kael"})["keys"],
                         ["characters/mira", "settings/the-gilded-anchor"])
        limited = images.plan(lib.load_serial(SLUG), self.found(), set(), limit=1)
        self.assertEqual((limited["keys"], limited["left"]), (["characters/kael"], 2))

        # serial.yaml image_policy widens both rules.
        wide = {"image_policy": {"characters_for": ["major", "one_off"], "setting_chapters": 1}}
        self.assertEqual(images.plan(wide, self.found(), set())["keys"],
                         ["characters/kael", "characters/guard", "settings/the-gilded-anchor", "settings/north-gate"])

    def test_a_second_appearance_is_a_subject_of_its_own(self):
        record_looks = looks.load_looks(SLUG)
        record_looks["kael"].append(dict(WAR_PLATE))
        looks.save(SLUG, record_looks, looks.load_settings(SLUG))
        found = self.found()

        # The character keeps its everyday details and names its other appearance. The appearance has the
        # character's fields, its own details and the everyday ones beside them.
        kael, plate = found["characters/kael"], found["characters/kael--war-plate"]
        self.assertEqual(([f["text"] for f in kael["look"]], kael["appearances"]),
                         (["Broad and soot-marked", "A borrowed green cloak"], ["war-plate"]))
        self.assertEqual((plate["id"], plate["of"], plate["appearance"], plate["name"], plate["importance"]),
                         ("kael--war-plate", "kael", "war-plate", "Kael, war plate", "major"))
        self.assertEqual((plate["look"], plate["everyday"]), ([WAR_PLATE], kael["look"]))
        self.assertNotIn("appearances", found["characters/mira"])

        # A draw for everyone leaves out an appearance that one chapter shows, and a named draw paints it.
        self.assertEqual(images.plan(lib.load_serial(SLUG), found, set())["keys"],
                         ["characters/kael", "characters/mira", "settings/the-gilded-anchor"])
        self.assertEqual(images.plan(lib.load_serial(SLUG), found, set(), named=["characters/kael--war-plate"])["keys"],
                         ["characters/kael--war-plate"])

        # With a detail from a second chapter, a draw for everyone takes the appearance right after its
        # character. serial.yaml image_policy sets how many chapters that needs.
        record_looks["kael"].append({"chapter": 1, "text": "The plate smokes", "appearance": "war-plate"})
        twice = images.subjects(lib.load_bible(SLUG), record_looks, looks.load_settings(SLUG))
        self.assertEqual(images.plan(lib.load_serial(SLUG), twice, set())["keys"],
                         ["characters/kael", "characters/kael--war-plate", "characters/mira", "settings/the-gilded-anchor"])
        self.assertEqual(images.plan({"image_policy": {"appearance_chapters": 1}}, found, set())["keys"][:2],
                         ["characters/kael", "characters/kael--war-plate"])

        # A character whose id reads like the appearance's keeps the key. The appearance is then no subject, and
        # its details stay with the everyday look, so the page still shows them.
        bible = lib.load_bible(SLUG)
        bible["characters"]["kael--war-plate"] = dict(CHARACTERS["guard"], name="Plate")
        taken = images.subjects(bible, record_looks, {})
        self.assertEqual((taken["characters/kael--war-plate"]["name"], "appearances" in taken["characters/kael"]),
                         ("Plate", False))
        self.assertEqual([f["text"] for f in taken["characters/kael"]["look"][-2:]],
                         [WAR_PLATE["text"], "The plate smokes"])

    def test_named_subjects_are_drawn_whatever_the_policy_says(self):
        found = self.found()
        todo = images.plan({}, found, {"characters/kael"}, named=["characters/mira", "characters/kael", "settings/north-gate"])
        self.assertEqual((todo["keys"], todo["kept"]), (["characters/mira", "settings/north-gate"], ["characters/kael"]))
        again = images.plan({}, found, {"characters/kael"}, named=["characters/kael"], redraw=True)
        self.assertEqual((again["keys"], again["kept"]), (["characters/kael"], []))
        with self.assertRaises(ComfyError):
            images.plan({}, found, set(), named=["characters/nobody"])

    def test_an_id_that_cannot_name_a_file_is_no_subject(self):
        bible = {"characters": {"../x": dict(CHARACTERS["kael"]), "kael": dict(CHARACTERS["kael"])}}
        self.assertEqual(list(images.subjects(bible, {}, {"Bad Id": {"name": "x"}})), ["characters/kael"])
        for bad in ("characters/../x", "voices/kael", "characters/", "kael"):
            with self.assertRaises(ValueError):
                images.split_key(bad)


class DrawTest(ImageCase):
    def test_a_dry_run_describes_and_paints_nothing(self):
        briefs = self.draw(dry_run=True)
        self.assertEqual([b["id"] for b in briefs], ["characters/kael", "characters/mira", "settings/the-gilded-anchor"])
        self.assertEqual((FakeClient.ran, FakeServer.started), ([], 0))

        # The pass gets the serial, the bible's entry with its facts and each look detail with its chapter, and
        # sets the style. A character without a look detail is described from its entry alone.
        text = self.llm.calls[0][1]
        for part in ("# Images for Demo by A. Writer", "tags: Fantasy", "blurb: A smith goes to war.",
                     "style in use: none yet, so choose one", "## characters/kael", "kind: character",
                     "importance: major", "facts:\n- chapter 2: Has a grey beard.",
                     "- chapter 1: Broad and soot-marked",
                     "- chapter 1 (that scene only): A borrowed green cloak",
                     "## characters/mira", "description: A courier.", "facts:\n- (none)",
                     "look:\n- (the text has given none yet)", "## settings/the-gilded-anchor",
                     "scenes in chapters: 1, 2"):
            self.assertIn(part, text)
        cache = images.load_briefs(SLUG)
        self.assertEqual(cache["styles"], {"main": "Style 1."})
        self.assertEqual(cache["briefs"]["characters/kael"],
                         {"prompt": "Picture 1 of characters/kael.", "facts": 2,
                          "look": images.look_digest(looks.load_looks(SLUG)["kael"]), "details": KAEL_DETAILS})

        # A second run finds the descriptions cached and asks nothing.
        self.draw(dry_run=True)
        self.assertEqual(self.llm.count("images"), 1)

    def test_an_appearance_is_described_and_painted_apart_from_the_everyday_look(self):
        record_looks = looks.load_looks(SLUG)
        record_looks["kael"].append(dict(WAR_PLATE))
        looks.save(SLUG, record_looks, looks.load_settings(SLUG))
        self.draw(characters=["kael", "kael--war-plate"], candidates=1)
        text = self.llm.calls[0][1]
        everyday, plate = text.split("## characters/kael--war-plate")

        # The everyday portrait is told of the other appearance and gets none of its details. The appearance
        # gets its own details first and the everyday ones after them.
        self.assertIn("other appearances, each with a picture of its own: war plate", everyday)
        self.assertNotIn("Black plate", everyday)
        self.assertIn("name: Kael, war plate\nappearance: war plate\n", plate)
        self.assertEqual((everyday.count("facts:"), plate.count("facts:")), (1, 0))
        self.assertIn("look in this appearance:\n- chapter 2: Black plate with a horned helm\n"
                      "everyday look:\n- chapter 1: Broad and soot-marked", plate)

        # Each has a record and files of its own, and each record keeps the passages its description saw.
        record = images.load_record(SLUG, "characters/kael--war-plate", "main")
        self.assertEqual((record["id"], record["candidates"][0]["image"]),
                         ("kael--war-plate", "images/main/characters/kael--war-plate/1000.webp"))
        self.assertEqual(record["basis"]["details"],
                         [{"chapter": 2, "text": WAR_PLATE["text"], "quote": WAR_PLATE["quote"]}])
        self.assertEqual(images.load_record(SLUG, "characters/kael", "main")["basis"]["details"], KAEL_DETAILS)

        # The bible page lists the appearance under its character, with its details, its image and the passages.
        kael = next(c for c in views.bible(SLUG)["characters"] if c["id"] == "kael")
        self.assertEqual([f["text"] for f in kael["look"]], ["Broad and soot-marked", "A borrowed green cloak"])
        shown = kael["appearances"][0]
        self.assertEqual((shown["id"], shown["appearance"], shown["name"], shown["look"]),
                         ("kael--war-plate", "war-plate", "Kael, war plate", [WAR_PLATE]))
        self.assertEqual((shown["images"]["main"]["seed"], shown["images"]["main"]["details"]),
                         (1000, [{"chapter": 2, "text": WAR_PLATE["text"], "quote": WAR_PLATE["quote"]}]))
        self.assertEqual(kael["images"]["main"]["details"], KAEL_DETAILS)

    def test_a_draw_files_the_first_candidate_of_each_subject(self):
        filed = self.draw(candidates=2)
        self.assertEqual([r["id"] for r in filed], ["kael", "mira", "the-gilded-anchor"])
        self.assertEqual(FakeServer.started, 1)

        # Each candidate is one prompt with its own seed, in the size of its kind, from the style, the framing
        # and the description.
        self.assertEqual([g["sample"]["inputs"]["seed"] for g in FakeClient.ran], [1000, 1001] * 3)
        first, place = FakeClient.ran[0], FakeClient.ran[4]
        self.assertEqual(first["text"]["inputs"]["prompt"],
                         f"Style 1. {images.FRAMING['characters']} Picture 1 of characters/kael.")
        self.assertEqual((first["latent"]["inputs"]["width"], first["latent"]["inputs"]["height"]), (896, 1152))
        self.assertEqual((place["latent"]["inputs"]["width"], place["latent"]["inputs"]["height"]), (1344, 768))
        self.assertEqual(first["save"]["inputs"]["filename_prefix"], "images/demo/main/characters/kael/s1000")
        self.assertEqual(first["unet"]["inputs"]["unet_name"], IMAGE_MODELS["qwen21"]["unet"])

        # The record names the candidates by their place in the serial's folder, and the files left ComfyUI's.
        record = images.load_record(SLUG, "characters/kael", "main")
        self.assertEqual((record["status"], record["seed"], record["chosen"], record["model"]), ("auto", 1000, 0, "qwen21"))
        self.assertEqual(record["basis"], {"facts": 2, "look": images.look_digest(looks.load_looks(SLUG)["kael"]),
                                           "details": KAEL_DETAILS})
        self.assertEqual(FakeClient.urls, {"http://127.0.0.1:8188"})
        self.assertEqual(record["candidates"], [
            {"seed": 1000, "image": "images/main/characters/kael/1000.webp", "thumb": "images/main/characters/kael/1000.thumb.webp"},
            {"seed": 1001, "image": "images/main/characters/kael/1001.webp", "thumb": "images/main/characters/kael/1001.thumb.webp"}])
        folder = lib.serial_dir(SLUG)
        self.assertEqual((folder / "images/main/characters/kael/1001.webp").read_bytes(), b"save 1001")
        self.assertEqual((folder / "images/main/characters/kael/1001.thumb.webp").read_bytes(), b"save_thumb 1001")
        self.assertEqual([p for p in (self.root / "output").rglob("*") if p.is_file()], [])

        # Everyone the policy covers now has an image, so the next draw does nothing.
        self.assertEqual(self.draw(), [])
        self.assertIn("nothing to draw", self.said())
        self.assertEqual(len(FakeClient.ran), 6)

    def test_a_redraw_keeps_the_old_round_and_starts_at_a_new_seed(self):
        self.draw(characters=["kael"], candidates=2)
        self.assertEqual(self.draw(characters=["kael"]), [])
        self.assertIn("already drawn, kept", self.said())

        # The description is cached and the text gave nothing new, so the redraw asks no LLM.
        self.draw(characters=["kael"], redraw=True, candidates=1, note="too old", image_model="krea2")
        self.assertEqual(self.llm.count("images"), 1)
        record = images.load_record(SLUG, "characters/kael", "main")
        self.assertEqual((record["seed"], record["model"], [c["seed"] for c in record["candidates"]]), (1002, "krea2", [1002]))
        old = record["history"][0]
        self.assertEqual(([c["seed"] for c in old["candidates"]], old["why_replaced"], old["model"]), ([1000, 1001], "too old", "qwen21"))
        self.assertEqual(FakeClient.ran[-1]["unet"]["inputs"]["unet_name"], IMAGE_MODELS["krea2"]["unet"])

        # A new detail from the text makes the next redraw ask for the description again, in the same style.
        record_looks, places = looks.load_looks(SLUG), looks.load_settings(SLUG)
        record_looks["kael"].append({"chapter": 2, "text": "A burn scar"})
        looks.save(SLUG, record_looks, places)
        self.draw(characters=["kael"], redraw=True, candidates=1)
        self.assertEqual(self.llm.count("images"), 2)
        self.assertIn("style in use: Style 1.", self.llm.calls[-1][1])
        latest = images.load_record(SLUG, "characters/kael", "main")
        self.assertEqual((latest["basis"], latest["seed"], len(latest["history"])),
                         ({"facts": 3, "look": images.look_digest(record_looks["kael"]),
                           "details": KAEL_DETAILS + [{"chapter": 2, "text": "A burn scar"}]}, 1003, 2))
        self.assertTrue(latest["prompt"].startswith("Style 1. "))

        # A chapter read again can change a detail and keep the count. The description is asked for again then too.
        record_looks["kael"][0]["text"] = "Lean and silver-haired"
        looks.save(SLUG, record_looks, places)
        self.draw(characters=["kael"], redraw=True, candidates=1)
        self.assertEqual(self.llm.count("images"), 3)
        self.assertIn("- chapter 1: Lean and silver-haired", self.llm.calls[-1][1])

    def test_a_seed_that_is_used_does_not_paint_over_its_candidate(self):
        self.draw(characters=["kael"], candidates=2)
        folder = lib.serial_dir(SLUG) / "images/main/characters/kael"
        old = (folder / "1000.webp").stat().st_mtime_ns

        # A candidate's file is named by its seed, so a redraw from a used seed starts at the first free one.
        for asked in (1000, 1001, 999):
            self.draw(characters=["kael"], redraw=True, candidates=2, seed0=asked)
        record = images.load_record(SLUG, "characters/kael", "main")
        rounds = [[c["seed"] for c in r["candidates"]] for r in record["history"]]
        rounds.append([c["seed"] for c in record["candidates"]])
        self.assertEqual(rounds, [[1000, 1001], [1002, 1003], [1004, 1005], [1006, 1007]])
        self.assertEqual((folder / "1000.webp").stat().st_mtime_ns, old)
        self.assertIn("a candidate has a seed from 1000 on already, so the draw starts at 1002", self.said())

        # A seed range that is free is used as asked.
        self.draw(characters=["kael"], redraw=True, candidates=1, seed0=5000)
        self.assertEqual(images.load_record(SLUG, "characters/kael", "main")["seed"], 5000)

    def test_the_prompts_go_to_the_comfyui_the_server_found(self):
        FakeServer.found = "http://100.64.0.9:8188"
        self.draw(characters=["kael"], candidates=1)
        self.assertEqual((FakeClient.urls, FakeClient.freed), ({"http://100.64.0.9:8188"}, 1))

    def test_a_draw_needs_a_candidate(self):
        with self.assertRaises(ComfyError):
            self.draw(characters=["kael"], candidates=0)
        self.assertEqual((self.llm.calls, FakeClient.ran), ([], []))

    def test_a_description_of_your_own_replaces_the_pass(self):
        self.draw(characters=["mira"], prompt="  A wiry courier\n with red hair. ", candidates=1, seed0=7)
        self.assertEqual(self.llm.calls, [])
        record = images.load_record(SLUG, "characters/mira", "main")
        self.assertEqual((record["brief"], record["seed"]), ("A wiry courier with red hair.", 7))
        self.assertEqual(record["prompt"], f"{images.DEFAULT_STYLE} {images.FRAMING['characters']} A wiry courier with red hair.")

        # The text's later details don't replace a description written by hand, and a fresh one is asked for by name.
        record_looks, places = looks.load_looks(SLUG), looks.load_settings(SLUG)
        record_looks["mira"] = [{"chapter": 2, "text": "A red braid"}]
        looks.save(SLUG, record_looks, places)
        self.draw(characters=["mira"], redraw=True, candidates=1)
        self.assertEqual(self.llm.calls, [])
        self.draw(characters=["mira"], redraw=True, candidates=1, redo_brief=True)
        self.assertEqual(self.llm.count("images"), 1)
        with self.assertRaises(ComfyError):
            self.draw(characters=["mira", "kael"], redraw=True, prompt="One description")

    def test_the_serial_sets_the_model_and_the_draw_the_style(self):
        serial = lib.load_serial(SLUG)
        serial.update(image_model="krea2")
        lib.save_serial(SLUG, serial)
        self.draw(characters=["kael"], candidates=1, style_text="Woodcut.")
        self.assertIn("style in use: Woodcut.", self.llm.calls[0][1])
        graph = FakeClient.ran[0]
        self.assertEqual(graph["text"]["class_type"], "CLIPTextEncode")
        self.assertTrue(graph["text"]["inputs"]["text"].startswith("Woodcut. "))
        with self.assertRaises(ComfyError):
            self.draw(characters=["mira"], image_model="dalle")

    def test_a_draw_that_is_given_a_style_keeps_it_for_later_draws(self):
        self.draw(characters=["kael"], candidates=1)
        self.assertEqual(images.load_briefs(SLUG)["styles"], {"main": "Style 1."})

        # The given text replaces the one the first image pass chose, for this draw and in the cache. The
        # description stays, because the style is not part of it.
        self.draw(characters=["kael"], redraw=True, candidates=1, style_text="  Ink and\n wash. ")
        self.assertEqual(self.llm.count("images"), 1)
        self.assertTrue(FakeClient.ran[-1]["text"]["inputs"]["prompt"].startswith("Ink and wash. "))
        self.assertEqual(images.load_briefs(SLUG)["styles"], {"main": "Ink and wash."})
        self.assertEqual(images.load_record(SLUG, "characters/kael", "main")["style"], "Ink and wash.")

        # A later draw without a style uses the kept one, and its image pass is told of it.
        self.draw(characters=["mira"], candidates=1)
        self.assertIn("style in use: Ink and wash.", self.llm.calls[-1][1])
        self.assertTrue(FakeClient.ran[-1]["text"]["inputs"]["prompt"].startswith("Ink and wash. "))

    def test_a_second_style_has_images_of_its_own_from_the_same_descriptions(self):
        self.draw(characters=["kael"], candidates=1)
        before = images.load_record(SLUG, "characters/kael", "main")
        self.draw(style="anime", style_text="Cel shading.", candidates=1)

        # A draw for everyone in the new style takes every subject, also the one the first style has. It asks
        # the image pass only for the descriptions no draw has asked for, and tells it the style's own text.
        self.assertEqual(list(images.records(SLUG, "anime")),
                         ["characters/kael", "characters/mira", "settings/the-gilded-anchor"])
        self.assertEqual((self.llm.count("images"), self.llm.calls[-1][1].count("\n## ")), (2, 2))
        self.assertIn("style in use: Cel shading.", self.llm.calls[-1][1])

        # Its records and files are under its name, in its text, from the description the first style has. The
        # first style's image and the order of the styles stay.
        anime = images.load_record(SLUG, "characters/kael", "anime")
        self.assertEqual((anime["brief"], anime["style"], anime["seed"], anime["candidates"][0]["image"]),
                         (before["brief"], "Cel shading.", 1000, "images/anime/characters/kael/1000.webp"))
        self.assertEqual(FakeClient.ran[1]["save"]["inputs"]["filename_prefix"], "images/demo/anime/characters/kael/s1000")
        self.assertEqual(images.load_record(SLUG, "characters/kael", "main"), before)
        self.assertEqual(images.load_briefs(SLUG)["styles"], {"main": "Style 1.", "anime": "Cel shading."})

        # A draw that names no style paints in the first, and a pick names the style whose image it files.
        self.draw(characters=["mira"], candidates=2)
        self.assertEqual(list(images.records(SLUG, "main")), ["characters/kael", "characters/mira"])
        picked = images.pick_image(SLUG, "characters/mira", style="anime", approve=True)
        self.assertEqual((picked["status"], images.load_record(SLUG, "characters/mira", "main")["status"]),
                         ("approved", "auto"))
        with self.assertRaises(FileNotFoundError):
            images.pick_image(SLUG, "settings/the-gilded-anchor", style="main")

        # A name that would not be a folder of its own is refused before anything is asked or painted.
        for bad in ("characters", "../x", "Film Still"):
            with self.assertRaises(ComfyError):
                self.draw(style=bad)
        self.assertEqual(self.llm.count("images"), 2)

        # A new style needs its text when every description is there, because no image pass runs that could
        # choose one. Nothing is painted, and the style is not made.
        painted = len(FakeClient.ran)
        with self.assertRaises(ComfyError) as failed:
            self.draw(style="ink")
        self.assertIn("the style ink has no text yet", str(failed.exception))
        self.assertEqual((len(FakeClient.ran), list(images.load_briefs(SLUG)["styles"])), (painted, ["main", "anime"]))

        # A text is kept also when the draw finds nothing to draw.
        self.assertEqual(self.draw(style="anime", style_text="Flat colours."), [])
        self.assertEqual(images.load_briefs(SLUG)["styles"]["anime"], "Flat colours.")

    def test_a_briefed_draw_asks_no_llm(self):
        # The stage that paints runs where no LLM does. It paints what has a description and leaves the rest.
        self.draw(characters=["kael"], dry_run=True)
        filed = self.draw(characters=["kael", "mira"], briefed=True, candidates=1)
        self.assertEqual(([r["id"] for r in filed], self.llm.count("images")), (["kael"], 1))
        self.assertIn("no description yet, left for the next draw: characters/mira", self.said())

        # With nothing left to paint, it doesn't reach for ComfyUI at all.
        started = FakeServer.started
        self.assertEqual(self.draw(characters=["mira"], briefed=True), [])
        self.assertEqual(FakeServer.started, started)

    def test_a_failure_keeps_the_subjects_painted_before_it(self):
        FakeClient.fail_at = 2
        with self.assertRaises(ComfyError):
            self.draw(candidates=2)
        self.assertEqual(list(images.records(SLUG, "main")), ["characters/kael"])
        # ComfyUI is asked to unload once, at the end, so the next job has the card
        self.assertEqual(FakeClient.freed, 1)

    def test_no_image_is_painted_beside_a_breeze_server(self):
        with mock.patch.object(machine, "breeze_server_pids", lambda: [4242]):
            with self.assertRaises(ComfyError) as caught:
                self.draw(characters=["kael"])
        self.assertIn("4242", str(caught.exception))
        self.assertEqual((FakeClient.ran, FakeServer.started), ([], 0))

    def test_the_pass_must_describe_every_subject(self):
        self.llm.drop = {"settings/the-gilded-anchor"}
        with self.assertRaises(LLMError):
            self.draw(dry_run=True)
        self.assertEqual(images.load_briefs(SLUG)["briefs"], {})


class PickTest(ImageCase):
    def test_a_pick_files_another_candidate_and_approves_it(self):
        self.draw(characters=["kael"], candidates=2)
        record = images.pick_image(SLUG, "characters/kael", seed=1001)
        self.assertEqual((record["seed"], record["chosen"], record["status"]), (1001, 1, "approved"))
        kept = images.pick_image(SLUG, "characters/kael", seed=1000, approve=False)
        self.assertEqual((kept["seed"], kept["chosen"], kept["status"]), (1000, 0, "approved"))
        with self.assertRaises(ValueError):
            images.pick_image(SLUG, "characters/kael", seed=5)
        with self.assertRaises(FileNotFoundError):
            images.pick_image(SLUG, "characters/mira")

        # A key that is not a subject's is refused before a file is read or written.
        before = (lib.serial_dir(SLUG) / "serial.yaml").read_bytes()
        for bad in ("settings/../../serial", "characters/../../serial", "voices/kael", "kael"):
            with self.assertRaises(ValueError):
                images.pick_image(SLUG, bad)
        self.assertEqual((lib.serial_dir(SLUG) / "serial.yaml").read_bytes(), before)

    def test_a_pick_from_an_earlier_round_brings_the_round_back(self):
        self.draw(characters=["kael"], candidates=2)
        self.draw(characters=["kael"], candidates=1, redraw=True, prompt="Another look.")
        record = images.pick_image(SLUG, "characters/kael", seed=1001)

        # The earlier round is the current one again, with its own description, and the newer one is history.
        self.assertEqual((record["seed"], record["chosen"], record["brief"]), (1001, 1, "Picture 1 of characters/kael."))
        self.assertEqual([c["seed"] for c in record["candidates"]], [1000, 1001])
        self.assertEqual(len(record["history"]), 1)
        self.assertEqual((record["history"][0]["brief"], [c["seed"] for c in record["history"][0]["candidates"]]),
                         ("Another look.", [1002]))
        self.assertEqual(images.next_seed(record), 1003)
        self.assertEqual(images.load_record(SLUG, "characters/kael", "main"), record)


class GraphTest(unittest.TestCase):
    def test_each_model_has_a_graph_that_saves_the_image_and_a_small_copy(self):
        for model, spec in IMAGE_MODELS.items():
            graph = image_graph(model=model, prompt="a tower", seed=3, width=896, height=1152, prefix="images/x/s3", thumb=256)
            for node in graph.values():
                for value in node["inputs"].values():
                    if isinstance(value, list):
                        self.assertIn(value[0], graph, (model, value))
            self.assertEqual(graph["sample"]["inputs"]["steps"], spec["steps"])
            self.assertEqual(graph["clip"]["inputs"]["type"], spec["clip_type"])
            self.assertEqual(graph["save"]["inputs"]["filename_prefix"], "images/x/s3")
            self.assertEqual(graph["save_thumb"]["inputs"]["filename_prefix"], "images/x/s3_thumb")
            self.assertEqual((graph["small"]["inputs"]["width"], graph["small"]["inputs"]["height"]), (256, 0))
        with self.assertRaises(ComfyError):
            image_graph(model="sd15", prompt="x", seed=1, width=64, height=64, prefix="p", thumb=8)


FAKE_MAIN = '''
import argparse, json
from http.server import BaseHTTPRequestHandler, HTTPServer
ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int)
ap.add_argument("--listen")
args, rest = ap.parse_known_args()
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        body = json.dumps({"system": {"argv": rest}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
HTTPServer((args.listen, args.port), H).serve_forever()
'''


class ComfyServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        main = root / "comfy" / "ComfyUI" / "main.py"
        main.parent.mkdir(parents=True)
        main.write_text(FAKE_MAIN, encoding="utf-8")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.url = f"http://127.0.0.1:{s.getsockname()[1]}"
        self.log_path = root / "out" / "comfyui.log"
        for patch in (mock.patch.object(machine, "COMFY_MAIN", main),
                      mock.patch.object(machine, "COMFY_PYTHON", Path(sys.executable)),
                      mock.patch.object(images, "COMFY_START_POLL", 0.1)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_it_starts_a_comfyui_when_none_answers_and_stops_it(self):
        said = []
        with images.ComfyServer(self.url, self.log_path, said.append) as server:
            proc = server.proc
            self.assertIsNotNone(proc)
            self.assertTrue(server.answers())
            self.assertIn("started ComfyUI", said[0])

            # A second job finds it up, uses it and leaves it up.
            with images.ComfyServer(self.url, self.log_path, said.append) as other:
                self.assertIsNone(other.proc)
            self.assertTrue(server.answers())

            # A ComfyUI on another address of this machine is used too, and no second one is started beside it.
            port = self.url.rsplit(":", 1)[1]
            with mock.patch.object(machine, "local_addresses", lambda: ["127.0.0.1"]):
                with images.ComfyServer(f"http://[::1]:{port}", self.log_path, said.append) as third:
                    self.assertEqual((third.proc, third.url), (None, self.url))
            self.assertIn(f"using the ComfyUI that is up at {self.url}", said[-1])
            self.assertTrue(server.answers())
        self.assertIsNotNone(proc.poll())
        self.assertFalse(images.ComfyServer(self.url, self.log_path).answers())
        self.assertTrue(self.log_path.is_file())

    def test_a_comfyui_that_cannot_start_is_an_error(self):
        # A ComfyUI that ends while it starts, one on another machine, and a machine without one.
        machine.COMFY_MAIN.write_text("raise SystemExit(3)\n", encoding="utf-8")
        with self.assertRaises(ComfyError) as caught:
            images.ComfyServer(self.url, self.log_path).__enter__()
        self.assertIn("exited with 3", str(caught.exception))
        with self.assertRaises(ComfyError):
            images.ComfyServer("http://comfy.invalid:8188", self.log_path).__enter__()
        machine.COMFY_MAIN.unlink()
        with self.assertRaises(ComfyError) as caught:
            images.ComfyServer(self.url, self.log_path).__enter__()
        self.assertIn("none to start", str(caught.exception))


class StopTest(unittest.TestCase):
    def test_a_stopped_draw_is_cleaned_up_at_the_comfyui_it_painted_with(self):
        from serial_audio import worker

        # The draw's ComfyUI is the one that is up: at the address given, else on another address of the machine.
        with mock.patch.object(images.ComfyServer, "answers", lambda self, url=None: True):
            self.assertEqual(images.comfy_up("http://127.0.0.1:8188/"), "http://127.0.0.1:8188")
        with mock.patch.object(images.ComfyServer, "answers", lambda self, url=None: False), \
                mock.patch.object(images.ComfyServer, "elsewhere", lambda self: "http://10.0.0.2:8188"):
            self.assertEqual(images.comfy_up("http://127.0.0.1:8188"), "http://10.0.0.2:8188")
            self.assertEqual(worker.draw_comfy("http://127.0.0.1:8188"), "http://10.0.0.2:8188")

        # With none up, or when the look fails, the worker keeps the address it was given.
        with mock.patch.object(images, "comfy_up", lambda url: None):
            self.assertEqual(worker.draw_comfy("http://127.0.0.1:8188"), "http://127.0.0.1:8188")
        with mock.patch.object(images, "comfy_up", side_effect=OSError("no route")):
            self.assertEqual(worker.draw_comfy("http://127.0.0.1:8188"), "http://127.0.0.1:8188")

        # The worker asks that ComfyUI to unload, and a ComfyUI that does not answer is no error.
        freed = []
        with mock.patch.object(machine, "listening", lambda url: True):
            with mock.patch.object(ComfyClient, "free", lambda self, settle=2.0: freed.append((self.base_url, settle))):
                self.assertTrue(worker.unload_comfy("http://10.0.0.2:8188"))
            self.assertEqual(freed, [("http://10.0.0.2:8188", 0.0)])
            with mock.patch.object(ComfyClient, "free", side_effect=ComfyError("down")):
                self.assertFalse(worker.unload_comfy("http://10.0.0.2:8188"))
        # A ComfyUI that is not listening is not asked, because a refused connection takes 2 s on Windows.
        with mock.patch.object(machine, "listening", lambda url: False), \
                mock.patch.object(ComfyClient, "free", lambda self, settle=2.0: freed.append("asked")):
            self.assertFalse(worker.unload_comfy("http://10.0.0.2:8188"))
        self.assertEqual(len(freed), 1)


class JobsTest(ImageCase):
    def test_the_jobs_build_cli_args(self):
        one = build("look-pass", {"slug": SLUG, "chapters": "1-2", "model": "opus", "redo": True})
        self.assertEqual(one["args"], ["look-pass", SLUG, "1-2", "--backend", "auto", "--model", "opus",
                                       "--on-limit", "api", "--redo"])
        # A look pass that names no model runs with Sonnet, where the other passes run with Opus.
        self.assertEqual(build("look-pass", {"slug": SLUG, "chapters": "1"})["args"][3:7],
                         ["--backend", "auto", "--model", "sonnet"])
        self.assertEqual((one["heavy"], one["gpu"], one["title"], one["meta"]),
                         (True, False, "Look pass · ch 1-2", {"chapters": [1, 2]}))
        self.assertTrue({"look-pass", "draw"} <= HEAVY and "look-pass" in LLM_KINDS)

        # A draw for everyone counts its subjects when it is queued, for its estimate.
        every = build("draw", {"slug": SLUG, "image_model": "krea2"}, comfy_url="http://c:1")
        self.assertEqual(every["args"], ["draw", SLUG, "--candidates", "2", "--comfy-url", "http://c:1", "--backend",
                                         "auto", "--model", "opus", "--on-limit", "api", "--image-model", "krea2"])
        self.assertEqual((every["gpu"], every["comfy"], every["title"]), (True, True, "Draw · everyone who needs an image"))
        # A style's name and its text go to both stages, the text after an equals sign as a description does.
        # The job says which style it paints in.
        styled = build("draw", {"slug": SLUG, "style": "ink", "style_text": " Ink and wash. "}, comfy_url="http://c:1")
        self.assertEqual(styled["args"][-3:], ["--style", "ink", "--style-text=Ink and wash."])
        self.assertEqual((styled["title"], styled["meta"]["style"], styled["meta"]["subjects"]),
                         ("Draw · everyone who needs an image · ink", "ink", 3))
        self.assertEqual([s["args"][-4:] for s in job_stages({"kind": "draw", **styled})],
                         [["--style", "ink", "--style-text=Ink and wash.", "--dry-run"],
                          ["--style", "ink", "--style-text=Ink and wash.", "--briefed"]])
        self.assertEqual(every["meta"], {"characters": [], "settings": [], "candidates": 2, "dry_run": False, "subjects": 3})
        self.assertEqual((_draw_units(every["meta"]), to_draw(SLUG)), (6, 3))

        # A redraw of one subject carries its description and its note after an equals sign.
        redo = build("draw", {"slug": SLUG, "settings": ["north-gate"], "redraw": True, "candidates": 3, "seed": 5,
                              "prompt": "-A gate of oak.", "note": "too new"})
        self.assertEqual(redo["args"][redo["args"].index("--seed"):],
                         ["--seed", "5", "--settings", "north-gate", "--redraw", "--prompt=-A gate of oak.", "--note=too new"])
        self.assertEqual((redo["title"], _draw_units(redo["meta"])), ("Redraw · north-gate", 3))
        dry = build("draw", {"slug": SLUG, "characters": ["kael"], "dry_run": True})
        self.assertEqual((dry["gpu"], dry["comfy"], dry["title"]), (False, False, "Image briefs · kael"))

        pick = build("pick-image", {"slug": SLUG, "subject": "characters/kael", "seed": 1001})
        self.assertEqual((pick["args"], pick["heavy"], pick["title"]),
                         (["pick-image", SLUG, "characters/kael", "--seed", "1001"], False, "Approve image · kael (seed 1001)"))
        inked = build("pick-image", {"slug": SLUG, "subject": "characters/kael", "seed": 1001, "style": "ink"})
        self.assertEqual((inked["args"][-2:], inked["title"], inked["meta"]),
                         (["--style", "ink"], "Approve image · kael (seed 1001) · ink",
                          {"subject": "characters/kael", "style": "ink"}))

        # Bad requests are refused before anything is queued.
        for kind, bad in (("draw", {"slug": SLUG, "redraw": True}), ("draw", {"slug": SLUG, "prompt": "x"}),
                          ("draw", {"slug": SLUG, "characters": ["a", "b"], "prompt": "x"}),
                          ("draw", {"slug": SLUG, "settings": ["../x"]}), ("draw", {"slug": SLUG, "image_model": "dalle"}),
                          ("draw", {"slug": SLUG, "style": "Ink and wash."}), ("draw", {"slug": SLUG, "style": "settings"}),
                          ("pick-image", {"slug": SLUG, "subject": "characters/kael", "style": "../x"}),
                          ("draw", {"slug": SLUG, "candidates": 9}), ("pick-image", {"slug": SLUG, "subject": "voices/kael"}),
                          ("pick-image", {"slug": SLUG, "subject": "characters/../x"}), ("look-pass", {"slug": SLUG}),
                          ("look-pass", {"slug": SLUG, "chapters": "new"}),
                          ("look-pass", {"slug": "nope", "chapters": "1"})):
            with self.assertRaises(ValueError, msg=(kind, bad)):
                build(kind, bad)

    def test_the_jobs_run_where_their_work_is(self):
        # A look pass is one Claude stage. A draw describes on a Claude node and paints on a ComfyUI node, which
        # asks no LLM. A draw from a description of your own paints only, and a pick runs on the app's machine.
        self.assertEqual(job_stages(job("look-pass", args=["look-pass", SLUG, "1"])),
                         [{"class": "llm", "args": ["look-pass", SLUG, "1"]}])
        args = ["draw", SLUG, "--candidates", "2", "--redo-brief"]
        stages = job_stages(job("draw", args=args))
        self.assertEqual([(s["class"], s["label"]) for s in stages], [("llm", "descriptions"), ("comfyui", "images")])
        self.assertEqual(stages[0]["args"], args + ["--dry-run"])
        self.assertEqual(stages[1]["args"], ["draw", SLUG, "--candidates", "2", "--briefed"])
        own = job_stages(job("draw", args=["draw", SLUG, "--characters", "kael", "--prompt=A smith."]))
        self.assertEqual(own, [{"class": "comfyui", "args": ["draw", SLUG, "--characters", "kael", "--prompt=A smith.", "--briefed"]}])
        self.assertEqual([s["class"] for s in job_stages(job("draw", args=["draw", SLUG, "--dry-run"]))], ["llm"])
        self.assertIsNone(job_stages(job("pick-image")))

        # A draw whose descriptions stage found nothing to draw ends there, without a wait for the card.
        log = "[worker] Draw on dev-arch, stage 0\n$ python -m serial_audio.cli draw demo\nnothing to draw\n[worker] exit 0\n"
        self.assertTrue(drew_nothing(job("draw"), log))
        self.assertFalse(drew_nothing(job("draw"), "drawing 2: a, b\n-- a\n   There is nothing to draw on the wall.\n"))
        self.assertFalse(drew_nothing(job("cast"), log))

    def test_the_jobs_wait_only_for_what_shares_their_files(self):
        # Looks, settings and images are files of their own, so none of the three waits for a chapter job, a cast
        # or a character pass of another chapter, and none of those waits for them.
        others = [job("render", chapters=[1]), job("screenplay", chapters=[1]), job("check-takes", chapters=[1]),
                  job("cast"), job("bios"), job("pick-voice"), job("say-as"), job("character-pass", chapters=[2]),
                  job("fetch")]
        for kind in ("look-pass", "draw", "pick-image"):
            mine = job(kind, chapters=[1] if kind == "look-pass" else None)
            for other in others:
                self.assertFalse(conflicts(mine, other), (kind, other["kind"]))
                self.assertFalse(conflicts(other, mine), (other["kind"], kind))

        # A look pass reads who is in its chapter from the chapter's character pass, and the text from its
        # import. So it and each of them wait for each other on a chapter that both work on.
        page = job("fetch", chapters=[1], args=["fetch", SLUG, "1", "--html", "p"])
        for reader in (job("character-pass", chapters=[1]), page):
            self.assertTrue(conflicts(job("look-pass", chapters=[1]), reader), reader["kind"])
            self.assertTrue(conflicts(reader, job("look-pass", chapters=[1])), reader["kind"])
            self.assertTrue(conflicts(job("look-pass", chapters=[1, 2]), reader), reader["kind"])
            self.assertFalse(conflicts(job("look-pass", chapters=[2]), reader), reader["kind"])
            self.assertFalse(conflicts(reader, job("look-pass", chapters=[2, 3])), reader["kind"])

        # Two look passes write one record, two draws the descriptions, and two picks may write one image's
        # record. A look pass and a draw or a pick share no file.
        self.assertTrue(conflicts(job("look-pass", chapters=[1]), job("look-pass", chapters=[2])))
        for a, b in (("draw", "draw"), ("pick-image", "pick-image")):
            self.assertTrue(conflicts(job(a), job(b)), (a, b))
        for a, b in (("look-pass", "draw"), ("draw", "look-pass"), ("look-pass", "pick-image"), ("pick-image", "look-pass")):
            self.assertFalse(conflicts(job(a), job(b)), (a, b))

        # A pick and a draw wait for each other only when the draw names the pick's subject. A draw for everyone
        # takes the subjects without an image, so a pick starts beside it.
        pick = {**job("pick-image"), "meta": {"subject": "characters/kael"}}
        every = {**job("draw"), "meta": {"characters": [], "settings": [], "subjects": 4}}
        redraw = {**job("draw"), "meta": {"characters": ["kael"], "settings": []}}
        place = {**job("draw"), "meta": {"characters": ["mira"], "settings": ["kael"]}}
        for draw, waits in ((every, False), (redraw, True), (place, False)):
            self.assertEqual(conflicts(pick, draw), waits, draw["meta"])
            self.assertEqual(conflicts(draw, pick), waits, draw["meta"])
        self.assertTrue(conflicts(job("pick-image"), redraw))

        # A reset of the bible and each of them wait for each other, and another serial's jobs never do.
        for kind in ("look-pass", "draw", "pick-image"):
            self.assertTrue(conflicts(job(kind), job("reset-bible")) and conflicts(job("reset-bible"), job(kind)), kind)
            self.assertFalse(conflicts(job(kind), job(kind, slug="other")), kind)

    def test_a_draw_reports_its_images_from_its_log(self):
        self.assertEqual(draw_progress("$ python -m serial_audio.cli draw demo\n", 2)["phase"], "briefs")
        log = ("drawing 2: characters/kael, settings/x\n-- characters/kael\n   A smith.\n"
               "   characters/kael seed 1000: drawn in 90 s\n   characters/kael seed 1001: drawn in 40 s\n"
               "   settings/x seed 1000: drawn in 50 s\n")
        p = draw_progress(log, 2)
        self.assertEqual((p["phase"], p["fraction"], p["eta"], p["detail"]), ("images", 0.75, 50, "drawn 3 of 4 images"))
        self.assertIsNone(draw_progress("drawing 2: a, b\n", 2)["eta"])
        self.assertEqual(draw_progress("nothing to draw\n", 2)["fraction"], 1.0)


class FilesTest(ImageCase):
    def test_a_bundle_leaves_the_images_out_and_a_stopped_draw_keeps_them(self):
        self.draw(characters=["kael"], candidates=1)
        data = exchange.build_bundle(SLUG, {"job": "j"}, root=self.root)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            listed = json.loads(z.read(exchange.BUNDLE_FILE))["manifest"]
        self.assertIn(f"library/{SLUG}/images/main/characters/kael.yaml", names)
        self.assertIn(f"library/{SLUG}/bible/looks.yaml", names)
        self.assertNotIn(f"library/{SLUG}/images/main/characters/kael/1000.webp", names)
        self.assertEqual(listed["images/main/characters/kael/1000.webp"]["shipped"], False)
        self.assertEqual(listed["images/main/characters/kael/1000.thumb.webp"]["shipped"], False)

        # A worker's new images, records and looks reach the library also when its job failed or was stopped.
        work = self.root / "work" / "library" / SLUG
        for rel, text in (("images/main/characters/mira/1000.webp", "image"), ("images/main/characters/mira.yaml", "id: mira\n"),
                          ("bible/looks.yaml", "mira: []\n"), ("chapters/0001/look_pass.json", "{}"),
                          ("bible/characters.yaml", "changed: true\n"), ("bible/looks.yaml.part", "mira: ["),
                          ("images/main/characters/mira.yaml.part", "id: mi")):
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(text, encoding="utf-8")
        changed = {rel: change for rel, change in exchange.changes(work, listed).items()}
        info, archive = exchange.read_result(exchange.build_result(work, {"job": "j"}, changed))
        report = exchange.apply_result(SLUG, info, archive, success=False, conflicts_dir=self.root / "conflicts",
                                       root=self.root)
        self.assertEqual(sorted(report["applied"]), ["bible/looks.yaml", "chapters/0001/look_pass.json",
                                                     "images/main/characters/mira.yaml", "images/main/characters/mira/1000.webp"])
        # A file that a stop left half written has another name, and stays with the worker.
        self.assertEqual(report["held"], ["bible/characters.yaml", "bible/looks.yaml.part",
                                          "images/main/characters/mira.yaml.part"])

    def test_a_save_that_is_cut_short_leaves_the_old_record(self):
        record, places = looks.load_looks(SLUG), looks.load_settings(SLUG)

        def cut(data, fh, **kwargs):
            fh.write("kael: [")
            fh.flush()
            raise KeyboardInterrupt

        with mock.patch.object(lib.yaml, "safe_dump", cut):
            for save in (lambda: looks.save(SLUG, {"kael": []}, {}),
                         lambda: images.save_briefs(SLUG, {"style": "x", "briefs": {}})):
                with self.assertRaises(KeyboardInterrupt):
                    save()
        self.assertEqual((looks.load_looks(SLUG), looks.load_settings(SLUG)), (record, places))
        self.assertEqual(images.load_briefs(SLUG), {"styles": {}, "briefs": {}})

    def test_the_backup_copies_the_images_and_keeps_them_out_of_the_snapshot(self):
        self.draw(characters=["kael"], candidates=1)
        dest = self.root / "backup"
        dest.mkdir()
        (dest / mirror.MARKER).write_text("", encoding="utf-8")
        report = mirror.backup(lib.LIBRARY, dest)
        self.assertEqual(report["images_copied"], 2)
        with tarfile.open(dest / "snapshots" / report["snapshot"]) as tar:
            names = tar.getnames()
        self.assertIn(f"library/{SLUG}/images/main/characters/kael.yaml", names)
        self.assertIn(f"library/{SLUG}/images/_briefs.yaml", names)
        self.assertEqual([n for n in names if n.endswith(".webp")], [])
        self.assertTrue((dest / "library" / SLUG / "images/main/characters/kael/1000.webp").is_file())
        self.assertEqual(mirror.backup(lib.LIBRARY, dest, now=1e9)["images_copied"], 0)

    def test_the_bible_view_has_the_looks_the_settings_and_the_images(self):
        self.draw(characters=["kael"], candidates=2)
        lib.save_json(lib.chapter_dir(SLUG, 1) / looks.PASS_FILE, {})
        record_looks, places = looks.load_looks(SLUG), looks.load_settings(SLUG)
        record_looks["kael"].append({"chapter": 2, "text": "A burn scar"})
        looks.save(SLUG, record_looks, places)
        b = views.bible(SLUG)

        # A character has its look details and its image, with how many details the image has not seen.
        kael = next(c for c in b["characters"] if c["id"] == "kael")
        self.assertEqual([f["text"] for f in kael["look"]][-1], "A burn scar")
        image = kael["images"]["main"]
        self.assertEqual((image["status"], image["seed"], image["new_facts"], len(image["candidates"])), ("auto", 1000, 1, 2))
        self.assertTrue(image["changed"])
        self.assertRegex(image["url"], r"^/media/lib/demo/images/main/characters/kael/1000\.webp\?v=\d+$")
        self.assertRegex(image["thumb"], r"^/media/lib/demo/images/main/characters/kael/1000\.thumb\.webp\?v=\d+$")
        self.assertEqual(next(c for c in b["characters"] if c["id"] == "mira")["images"], {})

        # The settings come with the most chapters first, and the page knows what is left to read and to draw.
        self.assertEqual([s["id"] for s in b["settings"]], ["the-gilded-anchor", "north-gate"])
        self.assertEqual(b["looks"], {"read": [1], "unread": [2]})
        self.assertEqual(b["draw"]["styles"], [{"name": "main", "text": "Style 1.", "ready": 2}])
        self.assertEqual((b["draw"]["ready"], b["draw"]["undescribed"], b["draw"]["model"]), (3, 0, "qwen21"))
        self.assertEqual(b["draw"]["models"], {k: m["label"] for k, m in IMAGE_MODELS.items()})
        rows = views.serial_detail(SLUG, 0.5)["chapters"]
        self.assertEqual([r["looked"] for r in rows], [True, False])

        # An image whose description saw the details as they are is neither behind nor changed. A detail that
        # a chapter read again words differently changes it without a new count.
        self.assertEqual(next(s for s in b["settings"] if s["id"] == "the-gilded-anchor")["images"], {})
        self.draw(settings=["the-gilded-anchor"], candidates=1)
        place = next(s for s in views.bible(SLUG)["settings"] if s["id"] == "the-gilded-anchor")
        self.assertEqual((place["images"]["main"]["new_facts"], place["images"]["main"]["changed"]), (0, False))
        places["the-gilded-anchor"]["look"][0]["text"] = "High white rafters"
        looks.save(SLUG, record_looks, places)
        place = next(s for s in views.bible(SLUG)["settings"] if s["id"] == "the-gilded-anchor")
        self.assertEqual((place["images"]["main"]["new_facts"], place["images"]["main"]["changed"]), (0, True))

        # A setting with scenes in two chapters and no look detail is counted as waiting for one.
        places["north-gate"].update(chapters_present=[1, 2], look=[])
        looks.save(SLUG, record_looks, places)
        self.assertEqual(views.bible(SLUG)["draw"]["undescribed"], 1)

    def test_the_bible_view_survives_a_malformed_record(self):
        self.draw(characters=["kael"], candidates=1)
        folder = lib.serial_dir(SLUG)
        lib.save_yaml(folder / "bible/looks.yaml", ["not", "a", "mapping"])
        lib.save_yaml(folder / "bible/settings.yaml", {"the-gilded-anchor": "an inn", "north-gate": {
            "name": "the north gate", "chapters_present": 2, "look": "oak"}})
        record = images.load_record(SLUG, "characters/kael", "main")
        lib.save_yaml(folder / "images/main/characters/kael.yaml", {**record, "basis": "two facts", "chosen": "first"})
        b = views.bible(SLUG)
        kael = next(c for c in b["characters"] if c["id"] == "kael")
        self.assertEqual((kael["look"], kael["images"]["main"]["seed"], kael["images"]["main"]["new_facts"]), ([], 1000, 0))
        self.assertEqual([(s["id"], s["look"], s["chapters_present"]) for s in b["settings"]], [("north-gate", [], [])])


if __name__ == "__main__":
    unittest.main()
