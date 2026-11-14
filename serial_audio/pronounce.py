"""Say-as overrides: the spelling a voice reads for a term, stored as `say` on the term's entry in the bible's
pronunciations.yaml. render.say_as_map and render.apply_say_as apply them to every line a voice reads.

Pronunciation options: respellings of a term that a Claude pass proposes and that Breeze says inside a line of the
script, stored as `options` on the term's entry for AJ to pick from. Nothing here files an option as a say-as.

Screening: on a machine with the take check's phone model, each option is also said in other lines that have the
term, each in its own speaker's voice, and the option gets two counts: the takes that add no sound to its spelling,
by the take check's name rule, and those of them that have its vowels as the option's usual saying has them. An
option is offered when the clip AJ hears is one of the second kind, and while it is not, the job says the clip from
another seed. The usual saying of the spelling a term has now is stored as `said` on the term's entry, for the
Names tab.
"""
from __future__ import annotations

import hashlib
import json
import math
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
# whether an options job screens its options. Off, an option gets its one clip, and every option is offered.
SCREENS = True
# how many takes say each option in a round, with the one AJ hears, and the share of an option's takes that have
# to add no sound to it for hold() to keep its usual saying
SCREEN_TAKES = 8
STEADY_SHARE = 0.75
# the most rounds in which a job looks for a clip that says an option the usual way
SCREEN_ROUNDS = 3
# the version of what a screen counts. A candidate's count and sayings from another version are not read.
SCREEN_V = 3


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
    Returns the term's entry as saved, or None when it was removed. A change of the spelling the voice is sent
    also changes the entry's usual saying, by hold(). Raises ValueError on a bad term or say, or when clearing a
    term that has no override.
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
    was = entry.get("say") or term
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
    if (entry.get("say") or term) != was:
        hold(entry, term)
    if clear and not entry.get("respelling") and not entry.get("options"):
        entries.pop(term, None)
        saved = None
    else:
        entries[term] = entry
        saved = entry
    lib.save_yaml(path, entries)
    return saved


def _counted(candidate) -> list[int] | None:
    """A screened option's [takes without an added sound, takes], or None without a count of SCREEN_V over
    SCREEN_TAKES takes or more."""
    count = candidate.get("steady") if isinstance(candidate, dict) else None
    ok = (isinstance(count, list) and len(count) == 2 and candidate.get("screen") == SCREEN_V
          and all(isinstance(n, int) and not isinstance(n, bool) for n in count)
          and 0 <= count[0] <= count[1] and count[1] >= SCREEN_TAKES)
    return count if ok else None


def likely(clean: int, takes: int) -> float:
    """The chance that STEADY_SHARE or more of an option's takes add no sound, given a screen's count.

    clean: the screening takes that add no sound.
    takes: the screening takes.
    Returns the chance when every share was as likely as any other before the screen.
    """
    yes, no = math.log(STEADY_SHARE), math.log(1 - STEADY_SHARE)
    tries = math.lgamma(takes + 2)
    return min(1.0, sum(math.exp(tries - math.lgamma(i + 1) - math.lgamma(takes + 2 - i) + i * yes
                                 + (takes + 1 - i) * no) for i in range(clean + 1)))


def steady(candidate) -> bool:
    """Whether a screened option comes out as it is spelled, for hold().

    candidate: an option of a term, or anything else.
    Returns True when the option was screened by SCREEN_V, has SCREEN_TAKES screening takes or more, it is more
    likely than not by likely() that STEADY_SHARE of its takes say the name without an added sound, and the take
    AJ hears says it the usual way, by screened(). Never raises.
    """
    count = _counted(candidate)
    return count is not None and likely(*count) >= 0.5 and candidate.get("typical") is not False


def offered(candidate) -> bool:
    """Whether a screened option is offered for use.

    candidate: an option of a term, or anything else.
    Returns True when the option was screened by SCREEN_V over SCREEN_TAKES takes or more and the take AJ hears
    says it the usual way, by screened(). The count of takes that add a sound doesn't decide it. Never raises.
    """
    return _counted(candidate) is not None and candidate.get("typical") is not False


def unsure(candidate) -> bool:
    """Whether another round could get a screened option offered.

    candidate: an option of a term, or anything else.
    Returns True when the option has a count of SCREEN_V and the take AJ hears doesn't say the name the usual
    way. Never raises.
    """
    return _counted(candidate) is not None and candidate.get("typical") is False


def line_voice(slug: str, serial: dict, bible: dict, line: dict, fallback: str) -> str:
    """The voice that says a script line in a render.

    slug: the serial.
    serial, bible: the serial's record and its bible.
    line: the script line.
    fallback: the voice to use when the line's voice has no record in the serial's voices folder.
    Returns the voice of the line's speaker, or the narrator's for the narrator and for a speaker without one.
    """
    cast = ((bible.get("characters") or {}).get(line.get("speaker")) or {}).get("voice_id")
    voice = cast or serial.get("narrator_voice_id") or fallback
    return voice if (lib.serial_dir(slug) / "voices" / f"{voice}.yaml").is_file() else fallback


def hold(entry: dict, term: str) -> None:
    """Sets or removes an entry's `said`, how the term's spelling usually sounds, which leads the ways the Names
    tab lists.

    entry: the term's entry in pronunciations.yaml, changed in place.
    term: the term as the text spells it.
    It is the usual saying of the option that has the spelling the voice is sent now, the entry's say or the term
    as written, when that option was screened and is steady. Any other entry is left without one. The callers are
    a change of the spelling and a new screen of that option, so new options alone leave it as it is.
    """
    spelling = entry.get("say") or term
    options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
    found = [c for c in options.get("candidates") or [] if isinstance(c, dict) and c.get("say") == spelling]
    if found and steady(found[0]) and isinstance(found[0].get("said"), str):
        entry["said"] = found[0]["said"]
    else:
        entry.pop("said", None)


def spelled(bible: dict, term: str, say: str) -> dict:
    """A copy of a bible with one term spoken another way.

    bible: the serial's bible, which is not changed.
    term: the term as the text spells it.
    say: the spelling the voice reads. The term itself means as written.
    """
    entries = dict(bible.get("pronunciations") or {})
    entry = {k: v for k, v in (entries.get(term) or {}).items() if k != "say"} if isinstance(entries.get(term), dict) else {}
    entries[term] = {**entry, **({} if say == term else {"say": say})}
    return {**bible, "pronunciations": entries}


def screening_lines(found: list[tuple[int, int, dict]], main: tuple[int, int], count: int, term: str = "",
                    avoid=()) -> list[dict]:
    """The other lines an option is said in, so its name is heard between different words.

    found: the lines that have the term, from lines_naming.
    main: (chapter, index) of the line AJ hears, which is left out.
    count: how many lines to return.
    term: the term. With it, a line whose word before the term and word after it are both new comes before a line
        that shares one with a line already chosen.
    avoid: lines that an earlier round used. They are left out while count other lines remain.
    Returns count lines from across the ones with a word count inside SAMPLE_WORDS, or across all the others
    when none has. With fewer lines than count they repeat, and [] when the term has no other line.
    """
    def size(item) -> int:
        return len(str(item[2].get("text") or "").split())

    def beside(item) -> tuple[str, str]:
        m = re.search(rf"(?:([^\W\d_]+)\W+)?(?<!\w){re.escape(term)}(?!\w)(?:\W+([^\W\d_]+))?",
                      str(item[2].get("text") or "")) if term else None
        return ((m.group(1) or "").lower(), (m.group(2) or "").lower()) if m else ("", "")

    others = [f for f in found if (f[0], f[1]) != main]
    pool = [f for f in others if SAMPLE_WORDS[0] <= size(f) <= SAMPLE_WORDS[1]] or others
    unused = [f for f in pool if f[2] not in avoid]
    if avoid and len(unused) >= count:
        pool = unused
    if not pool or count <= 0:
        return []
    if len(pool) < count:
        return [pool[i % len(pool)][2] for i in range(count)]
    stride = max(1, len(pool) // count)
    order = [i for start in range(stride) for i in range(start, len(pool), stride)]
    chosen, before, after = [], set(), set()
    for i in order:
        b, a = beside(pool[i])
        if len(chosen) < count and b not in before and a not in after:
            chosen.append(i)
            before.add(b)
            after.add(a)
    chosen += [i for i in order if i not in chosen][: count - len(chosen)]
    return [pool[i][2] for i in sorted(chosen)]


def sayings(term: str, tokens: list[dict], record: dict) -> list[list[str]]:
    """How one heard take said a term: [inside, window, added, near, said, heard, before, after] for each place the
    term has in the line.

    term: the term.
    tokens: the token map of the text the take was sent, from breeze.voice_text.
    record: what asr.records gave for the take, with the asr and phone blocks.
    added names the sounds that both recognizers heard added to the spelling, by phones.adds, as "V@2 h@1" with
    the sound and the count of the spelling's sounds before it, and is "" for a saying that adds none. The other
    five are what takecheck.name_spans gives under those names, which phones.vowels and phones.leads read.
    """
    from . import phones, takecheck
    asr = {"v": takecheck.ASR_V, "heard": str(record.get("text") or ""),
           **{f: record[f] for f in takecheck.TOKEN_FIELDS if isinstance(record.get(f), list)}}
    edits = takecheck.word_edits(asr["heard"], tokens)[0]
    return [[s["inside"], s["window"],
             " ".join(f"{sound}@{gap}"
                      for sound, gap in phones.adds(s["said"], s["heard"], s["near"], s["after"], s["before"])),
             s["near"], s["said"], s["heard"], s["before"], s["after"]]
            for s in takecheck.name_spans(asr, record.get("phone"), tokens, edits) if s["term"] == term]


def screened(takes: list[list[list[str]]]) -> dict:
    """What an option's screening takes say about it.

    takes: the sayings of each take, from sayings(), with the take AJ hears first.
    Returns {"steady", "alike", "typical", "said"}: [the takes that say the name and add no sound to it, the
    takes], [the takes that say it the usual way, the takes], whether the first take says it the usual way, and
    the usual saying from phones.consensus over every saying, which is left out when none gives one. A take says
    the name the usual way when every saying in it adds no sound and has no vowel that phones.compared finds
    another way than the usual saying phones.usual gives for the sayings that add none. A saying that an older
    screen left without what phones.vowels reads is held to its added sounds alone. A take without a saying is a
    take that doesn't say the name as spelled. {} when fewer than half of the takes have a saying.
    """
    from . import phones
    heard = [spans for spans in takes if spans]
    if not heard or 2 * len(heard) < len(takes):
        return {}

    def clean(spans) -> bool:
        return bool(spans) and all(len(s) > 2 and not s[2] for s in spans)

    def vowels(s) -> tuple[list, bool] | None:
        return (phones.vowels(s[4], s[5], s[3], s[7]), phones.leads(s[4], s[6])) if len(s) >= 8 else None

    found = [vowels(s) for spans in takes if clean(spans) for s in spans]
    found = [f for f in found if f is not None]
    way = phones.usual(found)

    def as_usual(spans) -> bool:
        if not clean(spans):
            return False
        return way is None or all(f is None or not phones.compared(way, *f)[0] for f in map(vowels, spans))

    usual = phones.consensus([s for spans in heard for s in spans])
    return {"steady": [sum(clean(spans) for spans in takes), len(takes)],
            "alike": [sum(as_usual(spans) for spans in takes), len(takes)], "typical": as_usual(takes[0]),
            **({"said": usual} if usual else {})}


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
                   missing: bool = False, again: str | None = None) -> dict:
    """Has Breeze say the options' line once for each candidate, in the voice that says the line in the chapter.

    slug: the serial.
    term: the term, which has options from options_pass or add_option.
    comfy_url: the ComfyUI that is asked to free the card first.
    log: receives the progress lines.
    missing: say only the candidates that don't have a clip yet, and on a machine that screens, those that were
        not screened by SCREEN_V. When every candidate has what it needs, nothing is said and breeze-server isn't
        started. When the line's voice, its reference clip, its direction, its seed or another say-as in the line
        is not what the clips were made with, every candidate is said again.
    again: a spelling. On a machine that screens, the candidate with it is said and screened whatever it has.
    Returns the options as stored. Each candidate that was said gets clip, a 16-bit WAV under SAY_OUTPUT, seconds,
    seed, the text the voice was sent, and heard, what Parakeet made of it, when Parakeet runs here. The options'
    made_with names what their clips were made with. Every candidate uses the direction and the reference clip
    that the chapter's render uses for the line, and its seed, or the next seed of cpp_take when a take's length
    doesn't fit the line. While SCREENS is on, on a machine whose take check has the phone model, each candidate
    is also said in SCREEN_TAKES - 1 other lines that have the term, each in the voice a render gives the line and
    without a direction, and gets steady, alike, typical and said from screened(). A term with no other line is said in
    its own line again. Those takes are not kept, and their sayings are, under sayings, with SCREEN_V as screen,
    also when too few of them were heard for a count. A candidate that
    SCREEN_V screened before with the same voice and line is screened with new seeds, and its earlier sayings
    count with the new ones, so each screen makes the count surer. A candidate that this call screened and that is
    unsure() afterwards, because its clip is not typical, is said and screened again, in lines the call has not
    used yet where the term has enough of them, for SCREEN_ROUNDS rounds at most. Such a round says the clip's
    line from the seeds after the ones its clips used, counts the earlier clip as one more take, and stores the
    count of clips tried as clips. The term as written gets one round. Each round starts breeze-server again.
    When the candidate that has the term's spelling was screened, the term's entry gets or loses its usual saying
    by hold(). When it was not, or the takes could not be heard, the entry's stays. Raises ValueError when the term has no options or its line
    is gone, and ComfyError when breeze-server cannot start or a take fails.
    """
    options, said, used = _say_options(slug, term, comfy_url=comfy_url, log=log, missing=missing,
                                       again={again} if again is not None else set(), only=False, avoid=[])
    for _ in range(SCREEN_ROUNDS - 1):
        more = {c["say"] for c in options["candidates"]
                if isinstance(c, dict) and c.get("say") in said and c["say"] != term and unsure(c)}
        if not more:
            break
        log(f"   {term}: the clip of {', '.join(repr(say) for say in sorted(more))} is not said the usual way, so "
            f"another seed says it, and {SCREEN_TAKES - 1} more lines say {'the option' if len(more) == 1 else 'each'}")
        options, said, lines = _say_options(slug, term, comfy_url=comfy_url, log=log, missing=True, again=more,
                                            only=True, avoid=used)
        used = used + lines
    return options


def _say_options(slug: str, term: str, *, comfy_url: str, log, missing: bool, again: set, only: bool,
                 avoid: list) -> tuple[dict, set, list]:
    """One round of render_options.

    again: the spellings to say and screen whatever they have.
    only: say no other candidate.
    avoid: the screening lines of the rounds before.
    Returns the options as stored, the spellings this round screened, and its screening lines.
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
    try:
        screen = SCREENS and asr.choose(None) == "parakeet" and "phone" in asr.extra_blocks()
    except Exception:
        screen = False
    others = (screening_lines(lines_naming(slug, term), (chapter, index), SCREEN_TAKES - 1, term, avoid)
              if screen else [])
    if screen and not others:
        others = [lines[index]] * (SCREEN_TAKES - 1)
    narrator = slot_voice[0]
    said_by = [line_voice(slug, serial, bible, other, narrator) for other in others]
    refs = breeze.stage_references(slug, set(slot_voice.values()) | set(said_by), "cpp")
    meta, _ = lib.load_chapter(slug, chapter)
    seed = breeze.render_seed(meta.get("render") if isinstance(meta.get("render"), dict) else {}, None)
    plan = breeze.plan_takes(lines, speaker_slot, slot_voice, bible, seed, {v: r["sha1"] for v, r in refs.items()}, "cpp")
    _, voice, _, direction, vseed, _ = next(p for p in plan if p[0] == index)
    spoken = {t: s for t, s in say_as_map(bible).items() if t != term}
    basis = [voice, refs[voice]["sha1"], direction, vseed, apply_say_as(lines[index]["text"], spoken)]
    made_with = hashlib.sha1(json.dumps(basis, default=str).encode("utf-8")).hexdigest()[:12]
    same = options.get("voice", voice) == voice and options.get("made_with", made_with) == made_with
    todo = [n for n, c in enumerate(options["candidates"])
            if (not only and not (missing and same and c.get("clip")
                                  and (not screen or (c.get("steady") and c.get("screen") == SCREEN_V))))
            or (screen and isinstance(c.get("say"), str) and c["say"] in again)]
    if not todo:
        return options, set(), []
    folder = SAY_OUTPUT / slug
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{re.sub(r'[^A-Za-z0-9]+', '-', term).strip('-') or 'term'}_{int(time.time())}"

    # Takes
    server = breeze.CppServer(folder / "breeze_server.log", log)
    server.refuse_if_busy()
    breeze.wait_comfy_idle(comfy_url, log)
    breeze.free_comfy(comfy_url)
    server.start()
    heard_in: list[tuple[int, Path, list[dict]]] = []
    earlier: dict[int, list] = {}
    fresh: set[int] = set()
    try:
        try:
            wav = Path(refs[voice]["ref"]).read_bytes()
            wavs = {v: Path(refs[v]["ref"]).read_bytes() for v in set(said_by)}
            for n in todo:
                candidate = options["candidates"][n]
                say = {**spoken, **({} if candidate["say"] == term else {term: candidate["say"]})}
                text = breeze.speakable(apply_say_as(lines[index]["text"], say))
                kept = (candidate.get("sayings") if same and candidate.get("screen") == SCREEN_V
                        and isinstance(candidate.get("sayings"), list) else [])
                clips = candidate.get("clips") if kept and isinstance(candidate.get("clips"), int) else 1
                another = bool(screen and kept) and candidate.get("typical") is False
                tried = max(1, clips) - (0 if another else 1)
                got = breeze.cpp_take(server, wav, refs[voice]["transcript"], text, direction, vseed, index, log,
                                      first=tried * (breeze.RETAKES + 1))
                clip = folder / f"{stem}_{n}.wav"
                breeze.write_wav(clip, got["pcm"], server.sample_rate)
                candidate.update(clip=str(clip), seconds=round(len(got["pcm"]) / 2 / server.sample_rate, 2), text=text,
                                 seed=got["seed"], clips=tried + 1)
                candidate.pop("heard", None)
                if not kept:
                    for field in ("said", "steady", "alike", "typical", "sayings", "screen"):
                        candidate.pop(field, None)
                earlier[n] = kept if another else kept[1:]
                as_said = spelled(bible, term, candidate["say"])
                heard_in.append((n, clip, breeze.voice_text(lines[index], as_said)[1]))
                for m, (other, by) in enumerate(zip(others, said_by), len(earlier[n]) + 1):
                    sent, tokens = breeze.voice_text(other, as_said)
                    extra = breeze.cpp_take(server, wavs[by], refs[by]["transcript"], sent, "",
                                            breeze.voice_seed(seed, by), index, log, first=m * (breeze.RETAKES + 1))
                    file = folder / f"{stem}_{n}_s{m}.wav"
                    breeze.write_wav(file, extra["pcm"], server.sample_rate)
                    heard_in.append((n, file, tokens))
                log(f"   {term} as {candidate['say']!r}: {candidate['seconds']} s  {clip.name}"
                    + (f", and in {len(others)} other lines" if others else ""))
        finally:
            server.stop()

        # Hear, screen and record
        try:
            engine = asr.choose(None)
            if engine == "parakeet":
                said: dict[int, list] = {}
                blocks = {"blocks": ("asr", "phone")} if screen else {}
                files = [file for _, file, _ in heard_in]
                for i, record in asr.records(files, engine=engine, comfy_url=comfy_url, **blocks):
                    n, file, tokens = heard_in[i]
                    if str(file) == options["candidates"][n]["clip"]:
                        options["candidates"][n]["heard"] = str(record.get("text") or "")
                    if screen:
                        said.setdefault(n, []).append(sayings(term, tokens, record))
                for n, takes in said.items():
                    candidate = options["candidates"][n]
                    takes = takes[:1] + earlier[n] + takes[1:]
                    found = screened(takes)
                    for field in ("said", "steady", "alike", "typical"):
                        candidate.pop(field, None)
                    candidate.update(found, sayings=takes, screen=SCREEN_V)
                    fresh.add(n)
                    if found:
                        count = found["steady"]
                        log(f"   {term} as {candidate['say']!r}: {count[0]} of {count[1]} takes say it without an "
                            f"added sound, {found['alike'][0]} of them the usual way"
                            + ("" if found["typical"] else ", and the clip is not one of those")
                            + ("" if offered(candidate) else ", so it is not offered"))
        except Exception as e:
            log(f"   the options were not heard: {e}")
    finally:
        for n, file, _ in heard_in:
            if str(file) != options["candidates"][n].get("clip"):
                file.unlink(missing_ok=True)
    options.update(voice=voice, made_with=made_with, rendered_at=lib.now_iso())
    entries = lib.load_yaml(path, {}) or {}
    current = dict(entries[term]) if isinstance(entries.get(term), dict) else {}
    current["options"] = options
    if any(options["candidates"][n].get("say") == (current.get("say") or term) for n in fresh):
        hold(current, term)
    entries[term] = current
    lib.save_yaml(path, entries)
    return options, {options["candidates"][n].get("say") for n in fresh}, others
