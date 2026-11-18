"""What the characters and the places of a serial look like, as its text says it.

The look pass reads one chapter beside the characters who appear in it and the settings recorded so far, and records
each visual detail the chapter adds. It writes three files and no other, so it shares none with the character pass,
a cast or a render:

library/<slug>/bible/looks.yaml               character id -> [{chapter, text, quote}], with lasting: false on a
                                              detail of one scene, and appearance: <slug> on a detail of a second
                                              appearance the story returns to, such as a suit of armour. quote is
                                              the chapter's passage that shows the detail
library/<slug>/bible/settings.yaml            setting id -> name, aliases, description, the chapters with a scene
                                              there, and look, a list like a character's
library/<slug>/chapters/NNNN/look_pass.json   the pass's answer and its provenance

A chapter that has look_pass.json has been read.
"""
from __future__ import annotations

import os
import re

from . import library as lib
from .llm import LLMError, PassResult, run_pass

LOOK_PROMPT = lib.PROMPTS / "look_pass.system.md"
LOOK_SCHEMA = lib.SCHEMAS / "look_pass.schema.json"
PASS_FILE = "look_pass.json"
# a setting's id names its image files
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
# typography that a copied passage may lose
PLAIN_MARKS = str.maketrans({chr(0x2018): "'", chr(0x2019): "'", chr(0x201c): '"', chr(0x201d): '"', chr(0x2013): "-",
                             chr(0x2014): "-", chr(0xa0): " ", chr(0x2026): "...", "*": None, "_": None})


def load_looks(slug: str) -> dict:
    """{character id: [{chapter, text, quote, lasting, appearance}]} for a serial, or {} before its first look
    pass."""
    return lib.load_yaml(lib.serial_dir(slug) / "bible" / "looks.yaml", {}) or {}


def load_settings(slug: str) -> dict:
    """{setting id: record} for a serial, or {} before its first look pass."""
    return lib.load_yaml(lib.serial_dir(slug) / "bible" / "settings.yaml", {}) or {}


def save(slug: str, looks: dict, settings: dict) -> None:
    """Saves the visual record. Each of its two files is replaced whole."""
    folder = lib.serial_dir(slug) / "bible"
    lib.replace_yaml(folder / "looks.yaml", looks)
    lib.replace_yaml(folder / "settings.yaml", settings)


def was_read(slug: str, number: int) -> bool:
    return (lib.chapter_dir(slug, number) / PASS_FILE).is_file()


def unread(slug: str) -> list[int]:
    """The chapters that have a character pass and no look pass, in order."""
    out = []
    for n in lib.list_chapters(slug):
        meta = lib.load_yaml(lib.chapter_dir(slug, n) / "chapter.yaml", {}) or {}
        if "characters_present" in meta and not was_read(slug, n):
            out.append(n)
    return out


def appearance_slug(value) -> str:
    """The slug of an appearance as the record keeps it, or "" for the everyday one.

    value: what a pass or a hand edit wrote, a slug or a name.
    Returns lower-case letters, digits and single hyphens, at most 40 characters.
    """
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")[:40].strip("-")


def _plain(text: str) -> str:
    return " ".join(str(text).translate(PLAIN_MARKS).split()).casefold()


def stands_in(quote: str, text: str) -> bool:
    """Whether a passage is the chapter's own words.

    quote: the passage a pass copied.
    text: the chapter's text.
    Returns True when the passage stands in the text, read without regard to case, spacing, emphasis marks and
    the kind of quotation mark or dash. A passage with "..." in it stands when its parts do, in their order. An
    empty passage does not.
    """
    plain, at = _plain(text), 0
    parts = [p.strip() for p in _plain(quote or "").split("...") if p.strip()]
    for part in parts:
        found = plain.find(part, at)
        if found < 0:
            return False
        at = found + len(part)
    return bool(parts)


def appearances(facts) -> list[str]:
    """The slugs of a character's second appearances, in the order the record has them."""
    out = []
    for f in facts or []:
        slug = appearance_slug(f.get("appearance")) if isinstance(f, dict) else ""
        if slug and slug not in out:
            out.append(slug)
    return out


def said(facts) -> str:
    """A subject's look so far as one line, with the details of one scene marked and each detail of a second
    appearance behind its slug in brackets."""
    parts = [(f"[{f['appearance']}] " if f.get("appearance") else "") + f"{f.get('text')}"
             + ("" if f.get("lasting", True) else " (that scene only)")
             for f in facts or [] if isinstance(f, dict) and f.get("text")]
    return " / ".join(parts) or "(nothing recorded yet)"


def build_look_input(serial: dict, meta: dict, text: str, present: dict, looks: dict, settings: dict) -> str:
    """The look pass's input for one chapter.

    serial: the serial.yaml record.
    meta, text: the chapter's record and text.
    present: {id: bible character} for the characters who appear in the chapter.
    looks, settings: the visual record so far.
    Returns the text. A setting whose name or an alias stands in the chapter is listed with its look so far, and
    the others with their names alone.
    """
    part = f", {meta['part_title']}" if meta.get("part_title") else ""
    lines = [f"# {serial['title']} by {serial.get('author') or 'unknown'}{part}",
             f"# Chapter {meta['number']} of the serial: {meta['label']}: {meta['title']}", "",
             "## Characters in this chapter", ""]
    for cid, c in present.items():
        aliases = ", ".join(c.get("aliases") or []) or "-"
        lines.append(f"{cid} | {c.get('name')} | {aliases} | {c.get('gender')}/{c.get('age_group')} | {c.get('role')}")
        lines.append(f"    look so far: {said(looks.get(cid))}")
    if not present:
        lines.append("(none)")
    lines += ["", "## Settings so far", ""]
    lowered = text.casefold()
    for sid, s in settings.items():
        names = [s.get("name") or ""] + list(s.get("aliases") or [])
        aliases = ", ".join(s.get("aliases") or []) or "-"
        lines.append(f"{sid} | {s.get('name')} | {aliases} | {s.get('description')}")
        if any(n and n.casefold() in lowered for n in names):
            lines.append(f"    look so far: {said(s.get('look'))}")
    if not settings:
        lines.append("(none yet)")
    return "\n".join(lines) + "\n\n## Chapter text\n\n" + text


def validate_look_output(out: dict, present: dict, settings: dict) -> list[str]:
    """Checks a look pass's answer against the rules its schema cannot express.

    out: the answer.
    present: {id: bible character} for the characters who appear in the chapter.
    settings: the settings recorded so far.
    Returns the problems, each a sentence the model can act on, or [] for an answer that keeps the rules.
    """
    problems = []
    for c in out.get("characters", []):
        if c["id"] not in present:
            problems.append(f"`{c['id']}` under `characters` is not in the list of characters in this chapter. "
                            f"Report only those characters, under the ids given.")
    new_ids = [s["id"] for s in out.get("new_settings", [])]
    for sid in new_ids:
        if not ID_RE.match(sid):
            problems.append(f"`{sid}` is not a kebab-case slug of lower-case letters, digits and hyphens.")
        if sid in settings:
            problems.append(f"`{sid}` is already a setting. Report it under `settings`, not `new_settings`.")
        if new_ids.count(sid) > 1:
            problems.append(f"`{sid}` is under `new_settings` more than once.")
    for s in out.get("settings", []):
        if s["id"] not in settings:
            problems.append(f"`{s['id']}` under `settings` is not a known setting. A new place belongs in "
                            f"`new_settings`.")
    return sorted(set(problems))


def forget_chapter(looks: dict, settings: dict, number: int) -> None:
    """Takes out of the record, in place, everything an earlier look pass of the chapter put in.

    looks, settings: the record.
    number: the chapter.
    Returns nothing. A setting that only this chapter has a scene in is removed.
    """
    for cid in list(looks):
        looks[cid] = [f for f in looks[cid] if f.get("chapter") != number]
        if not looks[cid]:
            del looks[cid]
    for sid in list(settings):
        s = settings[sid]
        s["look"] = [f for f in s.get("look") or [] if f.get("chapter") != number]
        s["chapters_present"] = [n for n in s.get("chapters_present") or [] if n != number]
        if not s["chapters_present"]:
            del settings[sid]
            continue
        s["first_seen"], s["last_seen"] = min(s["chapters_present"]), max(s["chapters_present"])


def _facts(items, number: int, have: list, chapter_text: str | None, report: dict) -> list[dict]:
    seen = {(f.get("text"), appearance_slug(f.get("appearance"))) for f in have}
    out = []
    for item in items or []:
        text = str(item.get("text") or "").strip()
        appearance = appearance_slug(item.get("appearance"))
        if text and (text, appearance) not in seen:
            seen.add((text, appearance))
            quote = " ".join(str(item.get("quote") or "").split())
            if quote and chapter_text is not None and not stands_in(quote, chapter_text):
                report["unquoted"] += 1
                quote = ""
            out.append({"chapter": number, "text": text, **({"quote": quote} if quote else {}),
                        **({} if item.get("lasting", True) else {"lasting": False}),
                        **({"appearance": appearance} if appearance else {})})
    return out


def _aliases(existing: list, extra: list, name: str) -> list[str]:
    seen, out = {name.casefold()}, []
    for item in list(existing or []) + list(extra or []):
        key = str(item).strip().casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(str(item).strip())
    return out


def merge_look_pass(looks: dict, settings: dict, out: dict, number: int, text: str | None = None) -> dict:
    """Applies a look pass's answer to the record in place.

    looks, settings: the record.
    out: the answer, which validate_look_output passed.
    number: the chapter.
    text: the chapter's text. A detail keeps its quote only when the quote stands in it. None keeps every quote.
    Returns {"characters": the ids that got a detail, "settings": the ids with a scene in the chapter, "new": the
    ids of the new settings, "unquoted": how many details lost a quote that is not the chapter's words}.
    """
    report = {"characters": [], "settings": [], "new": [], "unquoted": 0}
    for c in out.get("characters", []):
        facts = _facts(c.get("look"), number, looks.get(c["id"], []), text, report)
        if facts:
            looks.setdefault(c["id"], []).extend(facts)
            report["characters"].append(c["id"])
    for new in out.get("new_settings", []):
        sid = new["id"]
        if sid not in settings:
            settings[sid] = {"name": new["name"], "aliases": _aliases([], new.get("aliases"), new["name"]),
                             "description": new["description"], "evidence": new.get("evidence", ""),
                             "first_seen": number, "last_seen": number, "chapters_present": [], "look": []}
            report["new"].append(sid)
    for s in list(out.get("new_settings", [])) + list(out.get("settings", [])):
        entry = settings.get(s["id"])
        if entry is None:
            continue
        if s.get("new_aliases"):
            entry["aliases"] = _aliases(entry.get("aliases"), s["new_aliases"], entry["name"])
        entry.setdefault("look", []).extend(_facts(s.get("look"), number, entry.get("look") or [], text, report))
        present = entry.setdefault("chapters_present", [])
        if number not in present:
            present.append(number)
            present.sort()
        entry["first_seen"], entry["last_seen"] = present[0], present[-1]
        if s["id"] not in report["settings"]:
            report["settings"].append(s["id"])
    return report


def look_pass(slug: str, number: int, *, backend="auto", model="sonnet", effort=None, on_limit="api", log=print,
              max_attempts: int = 2) -> PassResult:
    """Reads one chapter for what its characters and places look like and adds that to the record.

    slug, number: the serial and the chapter, which has a character pass.
    backend, model, effort, on_limit: passed to run_pass.
    log: receives the progress lines.
    max_attempts: how often the pass is asked before its answer has to keep the rules.
    Returns the pass's result. What an earlier look pass of the chapter recorded is replaced. The chapter's
    look_pass.json is written last, so a chapter's first pass that is cut short counts as not made. Each file is
    replaced whole.
    Raises LLMError when the pass fails or its answer still breaks a rule, and ValueError for a chapter without
    a character pass.
    """
    # Setup
    serial = lib.load_serial(slug)
    meta, text = lib.load_chapter(slug, number)
    if "characters_present" not in meta:
        raise ValueError(f"chapter {number} has no character pass, which says who is in it")
    chars = lib.load_bible(slug)["characters"]
    present = {cid: chars[cid] for cid in meta.get("characters_present") or [] if cid in chars}
    looks, settings = load_looks(slug), load_settings(slug)
    forget_chapter(looks, settings, number)
    user_text = build_look_input(serial, meta, text, present, looks, settings)

    # Ask
    log(f"look pass: {slug} chapter {number} ({meta['word_count']} words, {len(present)} characters in it, "
        f"{len(settings)} settings so far) via {backend}/{model}")
    instruction = "Carry out your instructions on the input above and answer with the JSON object only."
    for attempt in range(1, max_attempts + 1):
        result = run_pass(LOOK_PROMPT.read_text(encoding="utf-8"), user_text, lib.load_json(LOOK_SCHEMA),
                          model=model, effort=effort, backend=backend, on_limit=on_limit, instruction=instruction,
                          log=log)
        problems = validate_look_output(result.data, present, settings)
        if not problems:
            break
        log(f"chapter {number}: attempt {attempt} violated {len(problems)} rule(s): " + " ".join(problems))
        if attempt == max_attempts:
            raise LLMError(f"chapter {number}: the look pass still breaks its rules after {max_attempts} attempts: "
                           + " ".join(problems))
        instruction = ("Carry out your instructions on the input above and answer with the JSON object only. "
                       "A previous answer broke these rules; fix them: " + " ".join(problems))

    # Record
    report = merge_look_pass(looks, settings, result.data, number, text)
    save(slug, looks, settings)
    path = lib.chapter_dir(slug, number) / PASS_FILE
    lib.save_json(path.with_name(path.name + ".part"), {**result.data, "pass": result.provenance()})
    os.replace(path.with_name(path.name + ".part"), path)
    log(f"chapter {number}: {len(report['characters'])} characters with a new detail, {len(report['settings'])} "
        f"settings, {len(report['new'])} of them new; {result.backend}/{result.model}, "
        f"est ${result.cost_estimate_usd or 0:.4f}")
    if report["unquoted"]:
        log(f"chapter {number}: {report['unquoted']} detail(s) kept without a quote, because the quote is not the "
            f"chapter's words")
    return result


def reset(slug: str) -> int:
    """Deletes the visual record and every chapter's look pass. Returns how many chapters had one."""
    folder = lib.serial_dir(slug) / "bible"
    for name in ("looks.yaml", "settings.yaml"):
        (folder / name).unlink(missing_ok=True)
    count = 0
    for n in lib.list_chapters(slug):
        path = lib.chapter_dir(slug, n) / PASS_FILE
        if path.exists():
            path.unlink()
            count += 1
    return count
