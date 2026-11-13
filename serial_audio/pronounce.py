"""Say-as overrides: the spelling a voice reads for a term, stored as `say` on the term's entry in the bible's
pronunciations.yaml. render.say_as_map and render.apply_say_as apply them to every line a voice reads.

Pronunciation options: respellings of a term that a Claude pass proposes and that Breeze says inside a line of the
script, stored as `options` on the term's entry for AJ to pick from. Nothing here files an option as a say-as.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import library as lib
from . import machine
from .llm import LLMError, run_pass

MAX_TERM = 80
MAX_SAY = 120
MAX_NOTE = 300
OPTIONS_PROMPT = lib.PROMPTS / "say_options_pass.system.md"
OPTIONS_SCHEMA = lib.SCHEMAS / "say_options_pass.schema.json"
SAY_OUTPUT = machine.COMFY_OUTPUT / "breeze" / "say"
MAX_OPTIONS = 6
CONTEXT_LINES = 3
SAMPLE_WORDS = (6, 30)
TYPED = "typed on the Names tab"


def _clean(value: str | None, what: str, limit: int, required: bool) -> str:
    text = " ".join(str(value or "").split())
    if required and not re.search(r"[^\W\d_]", text):
        raise ValueError(f"the {what} needs at least one letter")
    if len(text) > limit:
        raise ValueError(f"the {what} is longer than {limit} characters")
    return text


def set_say_as(slug: str, term: str, say: str | None, *, note: str | None = None, clear: bool = False) -> dict | None:
    """Sets, changes or clears the say-as override for one term in the serial's pronunciations.yaml.

    slug: the serial.
    term: the word or name as the text spells it. Matching is case-sensitive and whole-word.
    say: the spelling the voice reads. Ignored when clear is set.
    note: replaces the entry's note when given. An empty string removes the note.
    clear: remove the override. An entry that is then left without a respelling and without options is removed.
    Returns the term's entry as saved, or None when it was removed. Raises ValueError on a bad term or say, or when
    clearing a term that has no override.
    """
    # Validate
    term = _clean(term, "term", MAX_TERM, True)
    if not clear:
        say = _clean(say, "spoken form", MAX_SAY, True)
        if say == term:
            raise ValueError(f"the spoken form is the same as the term {term!r}")
    if note is not None:
        note = _clean(note, "note", MAX_NOTE, False)

    # Change
    path = lib.serial_dir(slug) / "bible" / "pronunciations.yaml"
    entries = lib.load_yaml(path, {}) or {}
    entry = entries.get(term)
    entry = dict(entry) if isinstance(entry, dict) else {}
    if clear:
        if not entry.get("say"):
            raise ValueError(f"{term!r} has no say-as override")
        entry.pop("say")
    else:
        entry["say"] = say
    if note is not None:
        if note:
            entry["note"] = note
        else:
            entry.pop("note", None)
    if clear and not entry.get("respelling") and not entry.get("options"):
        entries.pop(term, None)
        saved = None
    else:
        entries[term] = entry
        saved = entry
    lib.save_yaml(path, entries)
    return saved


def lines_naming(slug: str, term: str) -> list[tuple[int, int, dict]]:
    """The script lines that have a term as a say-as would match it: whole, and in the term's own case.

    slug: the serial.
    term: the term as the text spells it.
    Returns (chapter, line number, line) for each such line of every script, in order.
    """
    from .render import apply_say_as
    found = []
    for n in lib.list_chapters(slug):
        path = lib.chapter_dir(slug, n) / "script.json"
        if not path.exists():
            continue
        for i, line in enumerate(lib.load_json(path).get("lines") or []):
            text = str(line.get("text") or "")
            if apply_say_as(text, {term: ""}) != text:
                found.append((n, i, line))
    return found


def sample_line(found: list[tuple[int, int, dict]]) -> tuple[int, int, dict] | None:
    """The line to say the options in: the first with a word count inside SAMPLE_WORDS, else the shortest, else None."""
    def size(item) -> int:
        return len(str(item[2].get("text") or "").split())
    fit = [item for item in found if SAMPLE_WORDS[0] <= size(item) <= SAMPLE_WORDS[1]]
    return fit[0] if fit else min(found, key=size, default=None)


def _options_input(serial: dict, bible: dict, term: str, found: list[tuple[int, int, dict]]) -> str:
    entry = (bible.get("pronunciations") or {}).get(term)
    entry = entry if isinstance(entry, dict) else {}
    notes = [f"{name}: {entry[name]}" for name in ("respelling", "say", "note") if entry.get(name)]
    notes += [f"in {longer}: {other['respelling']}" for longer, other in (bible.get("pronunciations") or {}).items()
              if longer != term and term in str(longer).split() and isinstance(other, dict) and other.get("respelling")]
    who = [f"{c.get('name')}: {c.get('description') or c.get('role') or ''}".strip()
           for c in (bible.get("characters") or {}).values()
           if isinstance(c, dict) and term in str(c.get("name") or "").split()]
    lines = [f"- chapter {n}, {line.get('speaker')}: {line.get('text')}" for n, _, line in found[:CONTEXT_LINES]]
    return (f"# {serial.get('title')} by {serial.get('author') or 'unknown'}\n\n## Term\n\n{term}\n\n"
            f"## What the bible says about how it sounds\n\n" + ("\n".join(notes) or "(nothing)") + "\n\n"
            f"## Who or what it is\n\n" + ("\n".join(who) or "(not a character's name)") + "\n\n"
            f"## Lines that use it\n\n" + ("\n".join(lines) or "(none yet)"))


def _options_line(slug: str, term: str) -> tuple[list[tuple[int, int, dict]], tuple[int, int, dict]]:
    """Finds the script lines that have a term and chooses the one its options are said in.

    slug: the serial.
    term: the term as the text spells it.
    Returns the lines as lines_naming gives them, and the chosen one: sample_line's choice among the chapters with
    a Breeze render when one of them has the term, else among every chapter. Raises ValueError when no script line
    has the term.
    """
    found = lines_naming(slug, term)
    rendered = {n for n in {f[0] for f in found}
                if ((lib.load_chapter(slug, n)[0].get("render") or {}).get("engine")) == "breeze"}
    line = sample_line([f for f in found if f[0] in rendered]) or sample_line(found)
    if line is None:
        raise ValueError(f"no script line has {term!r} as written")
    return found, line


def _line_stands(slug: str, at) -> bool:
    """Whether the script still has the line that a term's options name, with the text the options have for it.

    slug: the serial.
    at: the options' "line" entry.
    """
    try:
        lines = lib.load_json(lib.chapter_dir(slug, at["chapter"]) / "script.json")["lines"]
        return 0 <= at["index"] < len(lines) and lines[at["index"]].get("text") == at["text"]
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return False


def add_option(slug: str, term: str, say: str) -> dict:
    """Adds one spelling to a term's options in pronunciations.yaml, for render_options to say.

    slug: the serial.
    term: the term as the text spells it. At least one script line has to have it.
    say: the spelling to try.
    Returns the options as stored. A term without options gets them, with the term as written as their first
    candidate and the line that options_pass would choose. Options whose line the script no longer has get such a
    line too, and their candidates are stored without their clips, so render_options says each one again. A
    spelling the options have already is not added again. The term's say-as, if it has one, is left as it is.
    Raises ValueError on a bad term or spelling, or a term that no line has.
    """
    term = _clean(term, "term", MAX_TERM, True)
    say = _clean(say, "spoken form", MAX_SAY, True)
    path = lib.serial_dir(slug) / "bible" / "pronunciations.yaml"
    entries = lib.load_yaml(path, {}) or {}
    entry = dict(entries[term]) if isinstance(entries.get(term), dict) else {}
    options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
    stored = options.get("candidates") if isinstance(options.get("candidates"), list) else []
    had = [c for c in stored if isinstance(c, dict) and c.get("say")]
    if not had or not _line_stands(slug, options.get("line")):
        _, line = _options_line(slug, term)
        options = {"at": lib.now_iso(), "line": {"chapter": line[0], "index": line[1], "speaker": line[2].get("speaker"),
                                                 "text": line[2].get("text")},
                   "candidates": [{"say": c["say"], "why": str(c.get("why") or "")} for c in had]
                                 or [{"say": term, "why": "as written"}]}
    if say not in [c.get("say") for c in options["candidates"] if isinstance(c, dict)]:
        options["candidates"].append({"say": say, "why": TYPED})
    entry["options"] = options
    entries[term] = entry
    lib.save_yaml(path, entries)
    return options


def options_pass(slug: str, term: str, *, backend="auto", model="opus", effort=None, on_limit="api",
                 log=print) -> dict:
    """Asks Claude for respellings of a term and stores them as the term's options in pronunciations.yaml.

    slug: the serial.
    term: the term as the text spells it. At least one script line has to have it.
    backend, model, effort, on_limit: as run_pass uses them.
    log: receives the progress lines.
    Returns the options as stored: {"at", "line": {"chapter", "index", "speaker", "text"}, "candidates": [{"say",
    "why"}, ...]}. The first candidate is the term as written, and MAX_OPTIONS respellings at most follow it. The
    line is from a chapter with a Breeze render when one has the term. The term's say-as, if it has one, is left as
    it is. Raises ValueError on a bad term or one that no line has, and LLMError when the pass fails or gives no
    usable respelling.
    """
    term = _clean(term, "term", MAX_TERM, True)
    found, line = _options_line(slug, term)
    serial, bible = lib.load_serial(slug), lib.load_bible(slug)
    log(f"options pass: {slug} {term!r}, in {len(found)} lines, via {backend}/{model}")
    result = run_pass(OPTIONS_PROMPT.read_text(encoding="utf-8"), _options_input(serial, bible, term, found),
                      lib.load_json(OPTIONS_SCHEMA), model=model, effort=effort, backend=backend, on_limit=on_limit,
                      log=log, instruction="Propose the respellings for the term above and answer with the JSON "
                                           "object only.")
    candidates, seen = [{"say": term, "why": "as written"}], {term.lower()}
    for option in result.data.get("options") or []:
        try:
            say = _clean(option.get("say"), "spoken form", MAX_SAY, True)
        except (ValueError, AttributeError):
            continue
        if say.lower() not in seen and len(candidates) <= MAX_OPTIONS:
            seen.add(say.lower())
            candidates.append({"say": say, "why": " ".join(str(option.get("why") or "").split())[:MAX_NOTE]})
    if len(candidates) < 2:
        raise LLMError(f"the options pass gave no usable respelling of {term!r}")
    options = {"at": lib.now_iso(), "line": {"chapter": line[0], "index": line[1], "speaker": line[2].get("speaker"),
                                             "text": line[2].get("text")}, "candidates": candidates}
    path = lib.serial_dir(slug) / "bible" / "pronunciations.yaml"
    entries = lib.load_yaml(path, {}) or {}
    entry = dict(entries[term]) if isinstance(entries.get(term), dict) else {}
    entry["options"] = options
    entries[term] = entry
    lib.save_yaml(path, entries)
    log(f"{term}: {len(candidates) - 1} respellings: {', '.join(c['say'] for c in candidates[1:])}; "
        f"{result.backend}/{result.model}, est ${result.cost_estimate_usd or 0:.4f}")
    return options


def render_options(slug: str, term: str, *, comfy_url: str = "http://127.0.0.1:8188", log=print,
                   missing: bool = False) -> dict:
    """Has Breeze say the options' line once for each candidate, in the voice that says the line in the chapter.

    slug: the serial.
    term: the term, which has options from options_pass or add_option.
    comfy_url: the ComfyUI that is asked to free the card first.
    log: receives the progress lines.
    missing: say only the candidates that don't have a clip yet. When every candidate has one, nothing is said
        and breeze-server isn't started. When the line's voice, its reference clip, its direction, its seed or
        another say-as in the line is not what the clips were made with, every candidate is said again.
    Returns the options as stored. Each candidate that was said gets clip, a 16-bit WAV under SAY_OUTPUT, seconds,
    seed, the text the voice was sent, and heard, what Parakeet made of it, when Parakeet runs here. The options'
    made_with names what their clips were made with. Every candidate uses the
    direction and the reference clip that the chapter's render uses for the line, and its seed, or the next seed
    of cpp_take when a take's length doesn't fit the line. Raises ValueError when the term has no options or its
    line is gone, and ComfyError when breeze-server cannot start or a take fails.
    """
    from . import asr, breeze
    from .render import apply_say_as, plan_slots, say_as_map

    # Setup
    term = _clean(term, "term", MAX_TERM, True)
    path = lib.serial_dir(slug) / "bible" / "pronunciations.yaml"
    entry = (lib.load_yaml(path, {}) or {}).get(term)
    options = entry.get("options") if isinstance(entry, dict) else None
    if not isinstance(options, dict) or not options.get("candidates"):
        raise ValueError(f"{term!r} has no options yet")
    chapter, index = options["line"]["chapter"], options["line"]["index"]
    serial, bible = lib.load_serial(slug), lib.load_bible(slug)
    lines = lib.load_json(lib.chapter_dir(slug, chapter) / "script.json")["lines"]
    if index >= len(lines) or lines[index].get("text") != options["line"]["text"]:
        raise ValueError(f"line {index} of chapter {chapter} is not the line the options of {term!r} were made for")
    speaker_slot, slot_voice, _ = plan_slots(serial, bible, lines)
    refs = breeze.stage_references(slug, set(slot_voice.values()), "cpp")
    meta, _ = lib.load_chapter(slug, chapter)
    seed = breeze.render_seed(meta.get("render") if isinstance(meta.get("render"), dict) else {}, None)
    plan = breeze.plan_takes(lines, speaker_slot, slot_voice, bible, seed, {v: r["sha1"] for v, r in refs.items()}, "cpp")
    _, voice, _, direction, vseed, _ = next(p for p in plan if p[0] == index)
    spoken = {t: s for t, s in say_as_map(bible).items() if t != term}
    basis = [voice, refs[voice]["sha1"], direction, vseed, apply_say_as(lines[index]["text"], spoken)]
    made_with = hashlib.sha1(json.dumps(basis, default=str).encode("utf-8")).hexdigest()[:12]
    same = options.get("voice", voice) == voice and options.get("made_with", made_with) == made_with
    todo = [n for n, c in enumerate(options["candidates"]) if not (missing and same and c.get("clip"))]
    if not todo:
        return options
    folder = SAY_OUTPUT / slug
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{re.sub(r'[^A-Za-z0-9]+', '-', term).strip('-') or 'term'}_{int(time.time())}"

    # Takes
    server = breeze.CppServer(folder / "breeze_server.log", log)
    server.refuse_if_busy()
    breeze.wait_comfy_idle(comfy_url, log)
    breeze.free_comfy(comfy_url)
    server.start()
    try:
        wav = Path(refs[voice]["ref"]).read_bytes()
        for n in todo:
            candidate = options["candidates"][n]
            say = {**spoken, **({} if candidate["say"] == term else {term: candidate["say"]})}
            text = breeze.speakable(apply_say_as(lines[index]["text"], say))
            got = breeze.cpp_take(server, wav, refs[voice]["transcript"], text, direction, vseed, index, log)
            clip = folder / f"{stem}_{n}.wav"
            breeze.write_wav(clip, got["pcm"], server.sample_rate)
            candidate.update(clip=str(clip), seconds=round(len(got["pcm"]) / 2 / server.sample_rate, 2), text=text,
                             seed=got["seed"])
            candidate.pop("heard", None)
            log(f"   {term} as {candidate['say']!r}: {candidate['seconds']} s  {clip.name}")
    finally:
        server.stop()

    # Hear and record
    try:
        engine = asr.choose(None)
        if engine == "parakeet":
            files = [Path(options["candidates"][n]["clip"]) for n in todo]
            for i, record in asr.records(files, engine=engine, comfy_url=comfy_url):
                options["candidates"][todo[i]]["heard"] = str(record.get("text") or "")
    except Exception as e:
        log(f"   the options were not heard: {e}")
    options.update(voice=voice, made_with=made_with, rendered_at=lib.now_iso())
    entries = lib.load_yaml(path, {}) or {}
    current = dict(entries[term]) if isinstance(entries.get(term), dict) else {}
    current["options"] = options
    entries[term] = current
    lib.save_yaml(path, entries)
    return options
