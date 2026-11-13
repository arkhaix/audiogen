"""The screenplay pass: chapter text -> ordered lines with speakers and directions (script.json), and the marks pass
that adds each line's emphasis, focus, heteronyms, emotion, energy and pace to the script."""
from __future__ import annotations

import difflib
import re
import unicodedata

from . import library as lib
from .llm import LLMError, PassResult, run_pass

PROMPT = lib.PROMPTS / "screenplay_pass.system.md"
SCHEMA = lib.SCHEMAS / "screenplay_pass.schema.json"
MARKS_PROMPT = lib.PROMPTS / "marks_pass.system.md"
MARKS_SCHEMA = lib.SCHEMAS / "marks_pass.schema.json"
WORDS = re.compile(r"[\w'’]+")
COMPARE_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
MARKS = {"emphasis": [], "focus": "", "heteronyms": [], "emotion": "", "energy": "", "pace": ""}
ENERGIES = ("whisper", "soft", "normal", "raised", "shout")
PACES = ("slow", "fast", "halting")
ITALIC_RE = re.compile(r"(?<![\w*])\*[^\s*](?:[^*]*[^\s*])?\*(?![\w*])")
MAX_DROPPED_RUN = 4
MAX_ADDED_RUN = 4
QUOTE_WORDS = 4
# the version of italic_emphasis, which a script records as "emphasis" once its lines have the field from it
EMPHASIS_V = 1
MAX_EMPHASIS_WORDS = 3
EMPHASIS_PHRASE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*(?: [^\W_]+(?:['’-][^\W_]+)*)*")
FIRST_PERSON = re.compile(r"I(?:['’][^\W_]+)?")
SENTENCE_OPENS = ".!?:" + chr(0x2026)
QUOTES = "\"'()" + chr(0x201C) + chr(0x201D) + chr(0x2018) + chr(0x2019)


def cast_list(bible: dict, present: list[str] | None) -> str:
    chars = bible.get("characters") or {}
    ids = present or list(chars)
    lines = ["id | name | aliases | voiced"]
    for cid in ids:
        c = chars.get(cid)
        if not c:
            continue
        lines.append(f"{cid} | {c['name']} | {', '.join(c.get('aliases') or []) or '-'} | "
                     f"{'yes' if c.get('voice_id') else 'no'}")
    return "\n".join(lines)


def build_input(serial: dict, meta: dict, text: str, bible: dict) -> str:
    nar = serial.get("narration") or {}
    pov = nar.get("point_of_view", "unknown")
    who = nar.get("narrator_character_id") or ""
    pron = bible.get("pronunciations") or {}
    pron_text = ", ".join(f"{t} ({p.get('respelling') or p.get('say')})" for t, p in list(pron.items())[:40]
                          if isinstance(p, dict) and (p.get("respelling") or p.get("say"))) or "(none)"
    return (f"# {serial['title']} by {serial.get('author') or 'unknown'}\n"
            f"# Chapter {meta['number']}: {meta['label']}: {meta['title']}\n"
            f"Narration: {pov}{f', narrated by {who}' if who else ''}\n\n"
            f"## Cast (characters present in this chapter)\n\n{cast_list(bible, meta.get('characters_present'))}\n\n"
            f"## Pronunciations (for reference)\n\n{pron_text}\n\n"
            f"## Chapter text\n\n{text}")


def compare_words(text: str) -> list[tuple[str, str]]:
    """The words of a text as the script check compares them, in order.

    text: a chapter's text or a script line's.
    Returns (key, word) per word. key is the word in lower case with a straight apostrophe. word is the word as the
    text spells it.
    """
    text = unicodedata.normalize("NFC", text)
    return [(m.group(0).lower().replace("’", "'"), m.group(0)) for m in COMPARE_WORD.finditer(text)]


def text_runs(text: str, lines: list[dict]) -> list[dict]:
    """The runs of words where a script differs from its chapter, in chapter order.

    text: the chapter's text.md. Its heading line is left out.
    lines: the script lines. When the chapter has a heading, the script's first line is the heading as a reader says
    it, and it is left out too, unless its words are the chapter's opening words.
    Returns {"kind", "words", "line", "quote"} per run. kind is "dropped" for chapter words the script doesn't have,
    and "added" for script words the chapter doesn't have. words counts the run's words. line is the script line that
    holds the run's first word, or for a dropped run the line the script goes on with, which is len(lines) at the
    end. quote is the run's first and last words as the text spells them.
    """
    head, _, body = text.partition("\n") if text.startswith("#") else ("", "", text)
    chapter, heading = compare_words(body), compare_words(head)
    script, line_of = [], []
    for i, line in enumerate(lines):
        found = compare_words(line.get("text") or "")
        if i == 0 and heading and found and not _opens(found, chapter):
            continue
        script += found
        line_of += [i] * len(found)

    matcher = difflib.SequenceMatcher(None, [k for k, _ in chapter], [k for k, _ in script], autojunk=False)
    runs = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            runs.append(_run("dropped", chapter[i1:i2], line_of[j1] if j1 < len(script) else len(lines)))
        if j2 > j1:
            runs.append(_run("added", script[j1:j2], line_of[j1]))
    return runs


def _opens(found: list[tuple[str, str]], chapter: list[tuple[str, str]]) -> bool:
    """Whether a script line's words are the first words of the chapter's body, give or take a normalization."""
    said = [k for k, _ in found]
    return difflib.SequenceMatcher(None, said, [k for k, _ in chapter[: len(said)]], autojunk=False).ratio() >= 0.5


def _run(kind: str, words: list[tuple[str, str]], line: int) -> dict:
    spelled = [w for _, w in words]
    quote = " ".join(spelled)
    if len(spelled) > 2 * QUOTE_WORDS:
        quote = " ".join(spelled[:QUOTE_WORDS]) + " ... " + " ".join(spelled[-QUOTE_WORDS:])
    return {"kind": kind, "words": len(words), "line": line, "quote": quote}


def long_runs(text: str, lines: list[dict]) -> list[dict]:
    """The runs of text_runs that are longer than MAX_DROPPED_RUN or MAX_ADDED_RUN words.

    text: the chapter's text.md.
    lines: the script lines.
    Returns the runs as text_runs gives them, in chapter order.
    """
    limit = {"dropped": MAX_DROPPED_RUN, "added": MAX_ADDED_RUN}
    return [r for r in text_runs(text, lines) if r["words"] > limit[r["kind"]]]


def _run_problem(run: dict, count: int) -> str:
    if run["kind"] == "dropped":
        where = f"before line {run['line']}" if run["line"] < count else "after the last line"
        return (f"the script leaves out {run['words']} words of the chapter {where}: \"{run['quote']}\". "
                f"Put them back where the chapter has them.")
    return (f"line {run['line']}: {run['words']} words are not in the chapter at this place: \"{run['quote']}\". "
            f"Take out the repeated or added words.")


def _occurs(phrase: str, text: str) -> bool:
    def fold(s: str) -> str:
        return " ".join(unicodedata.normalize("NFC", s).lower().replace("’", "'").split())
    phrase = fold(phrase)
    return bool(phrase) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", fold(text)) is not None


def emphasis_spans(text: str, phrases) -> list[tuple[int, int]]:
    """Where a line's emphasized words stand in its text.

    text: the line's text.
    phrases: the line's `emphasis`. Anything that is not a list gives no span, and so does an entry of it that
        is not a string.
    Returns (start, end) for each phrase that stands in text exactly once as whole words, in the text's order. Of
    two that overlap, the earlier one stays.
    """
    found = []
    for phrase in phrases if isinstance(phrases, list) else []:
        if not isinstance(phrase, str) or not phrase.strip():
            continue
        hits = list(re.finditer(rf"(?<![\w*]){re.escape(phrase)}(?![\w*])", text))
        if len(hits) == 1:
            found.append(hits[0].span())
    out: list[tuple[int, int]] = []
    for start, end in sorted(set(found)):
        if not out or start >= out[-1][1]:
            out.append((start, end))
    return out


def emphatic(text: str, phrases, whole=()) -> str:
    """text with an asterisk on either side of each emphasized word or phrase that emphasis_spans places.

    text: the line's text.
    phrases: the line's `emphasis`.
    whole: (start, end) for each part of text that has to stay in one piece. A phrase that would put an asterisk
        inside one is left as it is.
    """
    for start, end in reversed(emphasis_spans(text, phrases)):
        if any(a < at < b for a, b in whole for at in (start, end)):
            continue
        text = f"{text[:start]}*{text[start:end]}*{text[end:]}"
    return text


def _names(bible: dict) -> set[str]:
    """The capitalized words of the bible's terms and of its characters' names and aliases, in lower case."""
    found = [str(term) for term in bible.get("pronunciations") or {}]
    for character in (bible.get("characters") or {}).values():
        record = character if isinstance(character, dict) else {}
        found += [str(record.get("name") or "")] + [str(a) for a in record.get("aliases") or []]
    return {m.group(0).lower() for text in found for m in COMPARE_WORD.finditer(text) if m.group(0)[0].isupper()}


def italic_emphasis(text: str, lines: list[dict], bible: dict) -> dict[int, list[str]]:
    """The words of each script line that the chapter's text sets in italics for stress.

    text: the chapter's text.md, where italics stand between asterisks.
    lines: the script lines.
    bible: the serial's bible.
    Returns {line: [phrase, ...]} for the lines that have any, each phrase as the line's text spells it and in the
    line's order. An italic span counts when it has one to MAX_EMPHASIS_WORDS words with nothing but spaces
    between them, all of them in one line, is not the whole line, and stands in the line's text exactly once. A
    span that holds a term of the bible's pronunciations doesn't count. Nor does a span with a capitalized word,
    unless the word is its first, opens a sentence and is not a word of a term or of a character's name. The
    pronoun I counts anywhere. A word that a line spells with a combining accent is not found.
    """
    body = unicodedata.normalize("NFC", text.partition("\n")[2] if text.startswith("#") else text)
    spans = [m.span() for m in ITALIC_RE.finditer(body)]
    if not spans:
        return {}

    # The words of the chapter with the italic span each is in, and the words of the script with their places
    chapter: list[tuple[str, int | None]] = []
    at = 0
    for m in COMPARE_WORD.finditer(body):
        while at < len(spans) and spans[at][1] <= m.start():
            at += 1
        inside = at if at < len(spans) and spans[at][0] <= m.start() else None
        chapter.append((m.group(0).lower().replace("’", "'"), inside))
    script: list[tuple[str, int, int, int]] = []
    for i, line in enumerate(lines):
        script += [(m.group(0).lower().replace("’", "'"), i, m.start(), m.end())
                   for m in COMPARE_WORD.finditer(str(line.get("text") or ""))]
    matcher = difflib.SequenceMatcher(None, [k for k, _ in chapter], [k for k, _, _, _ in script], autojunk=False)
    placed: dict[int, list[int]] = {}
    count: dict[int, int] = {}
    for _, inside in chapter:
        if inside is not None:
            count[inside] = count.get(inside, 0) + 1
    for tag, i1, i2, j1, _ in matcher.get_opcodes():
        if tag == "equal":
            for d in range(i2 - i1):
                if chapter[i1 + d][1] is not None:
                    placed.setdefault(chapter[i1 + d][1], []).append(j1 + d)

    # The spans that are emphasis
    names = _names(bible)
    terms = [str(term).lower().replace("’", "'") for term in bible.get("pronunciations") or {} if str(term).strip()]
    words_of = {}
    for _, i, _, _ in script:
        words_of[i] = words_of.get(i, 0) + 1
    out: dict[int, list[str]] = {}
    for span in sorted(placed):
        found = placed[span]
        if len(found) != count[span] or not 1 <= len(found) <= MAX_EMPHASIS_WORDS:
            continue
        i = script[found[0]][1]
        if found != list(range(found[0], found[0] + len(found))) or script[found[-1]][1] != i:
            continue
        if len(found) == words_of[i]:
            continue
        said = str(lines[i].get("text") or "")
        start, end = script[found[0]][2], script[found[-1]][3]
        phrase = said[start:end]
        if not EMPHASIS_PHRASE.fullmatch(phrase) or emphasis_spans(said, [phrase]) != [(start, end)]:
            continue
        plain = phrase.lower().replace("’", "'")
        if any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", plain) for term in terms):
            continue
        words = phrase.split(" ")
        before = said[:start].rstrip(" " + QUOTES)
        opens = not before or before[-1] in SENTENCE_OPENS
        if any(w[0].isupper() and not FIRST_PERSON.fullmatch(w)
               and (n or not opens or w.lower().replace("’", "'") in names) for n, w in enumerate(words)):
            continue
        if phrase not in out.setdefault(i, []):
            out[i].append(phrase)
    return {i: found for i, found in out.items() if found}


def emphasized(script: dict, text: str, bible: dict) -> tuple[list[dict], bool]:
    """A script's lines with the `emphasis` that EMPHASIS_V gives them.

    script: the content of a script.json.
    text: the chapter's text.md.
    bible: the serial's bible.
    Returns (lines, changed). A script that records EMPHASIS_V gives its lines as they are and False. Any other
    gives a copy of each line and True: a line with italics for stress gets them as `emphasis`, a line without
    gets an empty one when it had the field, and stays without the field when it didn't. Raises KeyError for a
    script without lines.
    """
    lines = script["lines"]
    if script.get("emphasis") == EMPHASIS_V:
        return lines, False
    found = italic_emphasis(text, lines, bible)
    out = []
    for i, line in enumerate(lines):
        new = {k: v for k, v in line.items() if k != "emphasis"}
        if i in found or "emphasis" in line:
            new["emphasis"] = found.get(i, [])
        out.append(new)
    return out, True


def write_emphasis(slug: str, number: int, log=print) -> list[dict]:
    """Gives a chapter's script the `emphasis` of EMPHASIS_V, and returns its lines.

    slug, number: the chapter. It has to have a script.
    log: receives one line when the script is written.
    A script that records EMPHASIS_V is left alone. Any other is saved with the field and the version, and each
    line's speaker, text and direction stay as they are. A line whose `emphasis` changes gets another take key.
    """
    path = lib.chapter_dir(slug, number) / "script.json"
    script = lib.load_json(path)
    _, text = lib.load_chapter(slug, number)
    lines, changed = emphasized(script, text, lib.load_bible(slug))
    if changed:
        script["lines"] = lines
        script["emphasis"] = EMPHASIS_V
        lib.save_json(path, script)
        log(f"chapter {number}: {sum(1 for line in lines if line.get('emphasis'))} of {len(lines)} lines have a word "
            f"the chapter sets in italics for stress")
    return lines


def validate_script(out: dict, bible: dict, text: str) -> tuple[list[str], dict]:
    """Checks a screenplay pass's answer against the rules the schema can't express.

    out: the pass's answer.
    bible: the serial's bible.
    text: the chapter's text.md.
    Returns (problems, stats). problems holds one sentence per broken rule, for the model to fix: an unknown speaker,
    an empty line, a pair of asterisks around words in a line's text, a word count off the chapter's, and each run
    of long_runs. An asterisk that doesn't mark italics, as in a censored word, is not a problem. stats has the word
    counts and the coverage.
    """
    chars = bible.get("characters") or {}
    problems, starred = [], []
    lines = out.get("lines") or []
    if not lines:
        problems.append("`lines` is empty.")
    for i, line in enumerate(lines):
        sp = line["speaker"]
        if sp != "narrator" and sp not in chars:
            problems.append(f"line {i}: speaker `{sp}` is not `narrator` or a cast id.")
        if not line["text"].strip():
            problems.append(f"line {i}: empty text.")
        if ITALIC_RE.search(line["text"]):
            starred.append(str(i))
    if starred:
        shown = ", ".join(starred[:10]) + (f" and {len(starred) - 10} more" if len(starred) > 10 else "")
        problems.append(f"{'line' if len(starred) == 1 else 'lines'} {shown}: `text` has asterisks around words. Drop "
                        f"them and keep the words between them.")
    body = text.split("\n", 1)[1] if text.startswith("#") else text
    src_words = len(WORDS.findall(body))
    out_words = sum(len(WORDS.findall(l["text"])) for l in lines)
    stats = {"source_words": src_words, "script_words": out_words,
             "coverage": round(out_words / src_words, 3) if src_words else 0.0, "lines": len(lines),
             "speakers": sorted({l["speaker"] for l in lines})}
    if src_words and not 0.9 <= stats["coverage"] <= 1.2:
        problems.append(f"the script has {out_words} words for a chapter of {src_words}: text was dropped or added. "
                        f"Every sentence of the chapter must appear once.")
    problems += [_run_problem(run, len(lines)) for run in long_runs(text, lines)]
    return problems, stats


def with_marks(line: dict, marks: dict | None = None) -> dict:
    """A script line with the six fields of MARKS.

    line: a script line.
    marks: the fields the line has something in, or None.
    Returns the line with every field of MARKS, from marks where marks has it and empty otherwise.
    """
    bare = {k: v for k, v in line.items() if k not in MARKS}
    return {**bare, **{name: type(empty)() for name, empty in MARKS.items()}, **(marks or {})}


def marks_input(text: str, lines: list[dict]) -> str:
    """What the marks pass reads: the chapter's text, and the script as one numbered row per line."""
    rows = "\n".join(f"{i} | {line['speaker']} | {(line.get('direction') or '').strip() or '-'} | {line['text']}"
                     for i, line in enumerate(lines))
    return f"## Chapter text\n\n{text}\n\n## Script\n\nline | speaker | direction | text\n{rows}"


def placed_marks(found, lines: list[dict]) -> tuple[dict[int, dict], int]:
    """The marks of a marks pass's answer that fit the script.

    found: the answer's `marks`.
    lines: the script lines.
    Returns ({line: marks}, dropped), where marks has the six fields of MARKS. An entry for a line the script
    doesn't have is left out. So is an emphasis, focus or heteronym word that its line's text doesn't have, an
    energy that isn't in ENERGIES, a pace that isn't in PACES, an emotion, energy or pace on a line without a
    direction, and a value of another type than its field has. dropped counts what was left out. Of two entries
    for one line the later one counts. Never raises.
    """
    placed, dropped = {}, 0
    for entry in found if isinstance(found, list) else []:
        i = entry.get("line") if isinstance(entry, dict) else None
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(lines):
            dropped += 1
            continue
        text = str(lines[i].get("text") or "")
        directed = bool(str(lines[i].get("direction") or "").strip())
        given = {name: entry.get(name) for name in MARKS}
        stressed = given["emphasis"] if isinstance(given["emphasis"], list) else []
        sounds = given["heteronyms"] if isinstance(given["heteronyms"], list) else []
        marks = {
            "emphasis": [w for w in stressed if isinstance(w, str) and _occurs(w, text)],
            "focus": given["focus"] if isinstance(given["focus"], str) and _occurs(given["focus"], text) else "",
            "heteronyms": [{"word": h["word"], "sounds_like": str(h.get("sounds_like") or "")} for h in sounds
                           if isinstance(h, dict) and isinstance(h.get("word"), str) and _occurs(h["word"], text)],
            "emotion": given["emotion"].strip() if directed and isinstance(given["emotion"], str) else "",
            "energy": given["energy"] if directed and given["energy"] in ENERGIES else "",
            "pace": given["pace"] if directed and given["pace"] in PACES else "",
        }
        for name in MARKS:
            had = given[name] if isinstance(given[name], list) else [given[name]] if given[name] else []
            kept = marks[name] if isinstance(marks[name], list) else [marks[name]] if marks[name] else []
            dropped += len(had) - len(kept)
        placed[i] = marks
    return placed, dropped


def marks_pass(slug: str, number: int, *, backend="auto", model="opus", effort=None, on_limit="api",
               log=print) -> PassResult:
    """Writes the six fields of MARKS on every line of a chapter's script.json, from a pass that reads the script.

    slug, number: the chapter. It has to have a script.
    backend, model, effort, on_limit: as run_pass uses them.
    log: receives the progress lines.
    Returns the pass's result. A line the answer doesn't name gets the six fields empty, and marks from an earlier
    pass are replaced. `emphasis` is not the answer's: it comes from the chapter's italics, by emphasized(). Each
    line's speaker, text and direction stay as they are, so a take key changes only where `emphasis` does. Raises
    LLMError when the pass fails and FileNotFoundError when the chapter doesn't have a script, and leaves the
    script as it was.
    """
    meta, text = lib.load_chapter(slug, number)
    path = lib.chapter_dir(slug, number) / "script.json"
    script = lib.load_json(path)
    lines = script["lines"]
    log(f"marks pass: {slug} chapter {number} ({len(lines)} lines) via {backend}/{model}")
    result = run_pass(MARKS_PROMPT.read_text(encoding="utf-8"), marks_input(text, lines), lib.load_json(MARKS_SCHEMA),
                      model=model, effort=effort, backend=backend, on_limit=on_limit,
                      instruction="Return the marks of the script above as the JSON object only.", log=log)
    placed, dropped = placed_marks(result.data.get("marks"), lines)
    script["lines"] = [with_marks(line, placed.get(i)) for i, line in enumerate(lines)]
    script["lines"] = emphasized({"lines": script["lines"]}, text, lib.load_bible(slug))[0]
    script["emphasis"] = EMPHASIS_V
    script["marks"] = result.provenance()
    lib.save_json(path, script)
    meta.setdefault("passes", {})["marks"] = result.provenance()
    lib.save_chapter_meta(slug, number, meta)
    log(f"chapter {number}: marks on {sum(any(m.values()) for m in placed.values())} of {len(lines)} lines"
        + (f", {dropped} left out that the script doesn't have a place for" if dropped else "")
        + f"; {result.backend}/{result.model}, est ${result.cost_estimate_usd or 0:.4f}")
    return result


def screenplay_pass(slug: str, number: int, *, backend="auto", model="opus", effort=None, on_limit="api",
                    log=print, max_attempts: int = 2, marks: bool = False) -> PassResult:
    """Writes a chapter's script.json from its text and marks the chapter scripted.

    slug, number: the chapter.
    backend, model, effort, on_limit: as run_pass uses them.
    log: receives the progress lines.
    max_attempts: how many answers the pass may ask for. An answer that validate_script finds problems in goes
        back to the model with the problems.
    marks: run the marks pass on the saved script too. When that pass fails, the script stays as the screenplay
        pass wrote it, and the log says so.
    Returns the screenplay pass's result. Raises LLMError when the pass fails or its last answer still has
    problems, and then no script is written.
    """
    serial = lib.load_serial(slug)
    meta, text = lib.load_chapter(slug, number)
    bible = lib.load_bible(slug)
    system_prompt = PROMPT.read_text(encoding="utf-8")
    schema = lib.load_json(SCHEMA)
    user_text = build_input(serial, meta, text, bible)
    log(f"screenplay pass: {slug} chapter {number} ({meta['word_count']} words) via {backend}/{model}")
    instruction = "Turn the chapter above into the screenplay and answer with the JSON object only."
    for attempt in range(1, max_attempts + 1):
        result = run_pass(system_prompt, user_text, schema, model=model, effort=effort, backend=backend,
                          on_limit=on_limit, instruction=instruction, log=log)
        problems, stats = validate_script(result.data, bible, text)
        if not problems:
            break
        log(f"chapter {number}: attempt {attempt} violated {len(problems)} rule(s): " + " ".join(problems[:5]))
        if attempt == max_attempts:
            raise LLMError(f"chapter {number}: screenplay still violates rules: " + " ".join(problems[:5]))
        instruction = ("Turn the chapter above into the screenplay and answer with the JSON object only. "
                       "A previous answer broke these rules; fix them: " + " ".join(problems[:5]))

    folder = lib.chapter_dir(slug, number)
    lib.save_json(folder / "script.json", {"lines": result.data["lines"], "notes": result.data.get("notes", ""),
                                          "stats": stats, "provenance": result.provenance()})
    meta.setdefault("passes", {})["screenplay"] = result.provenance()
    meta["script"] = stats
    meta["status"] = "scripted"
    lib.save_chapter_meta(slug, number, meta)
    log(f"chapter {number}: {stats['lines']} lines, coverage {stats['coverage']:.2f}, speakers {stats['speakers']}; "
        f"{result.backend}/{result.model}, est ${result.cost_estimate_usd or 0:.4f}")
    if result.data.get("notes"):
        log(f"   notes: {result.data['notes']}")
    if marks:
        try:
            marks_pass(slug, number, backend=backend, model=model, effort=effort, on_limit=on_limit, log=log)
        except LLMError as e:
            log(f"chapter {number}: the marks pass failed, and the script doesn't have marks: {e}")
    return result
