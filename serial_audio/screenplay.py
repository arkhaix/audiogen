"""The screenplay pass: chapter text -> ordered lines with speakers and directions (script.json)."""
from __future__ import annotations

import re

from . import library as lib
from .llm import LLMError, PassResult, run_pass

PROMPT = lib.PROMPTS / "screenplay_pass.system.md"
SCHEMA = lib.SCHEMAS / "screenplay_pass.schema.json"
WORDS = re.compile(r"[\w'’]+")


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


def validate_script(out: dict, bible: dict, text: str) -> tuple[list[str], dict]:
    """Rules the schema can't express, plus a coverage estimate. Returns (problems, stats)."""
    chars = bible.get("characters") or {}
    problems = []
    lines = out.get("lines") or []
    if not lines:
        problems.append("`lines` is empty.")
    for i, line in enumerate(lines):
        sp = line["speaker"]
        if sp != "narrator" and sp not in chars:
            problems.append(f"line {i}: speaker `{sp}` is not `narrator` or a cast id.")
        if not line["text"].strip():
            problems.append(f"line {i}: empty text.")
    body = text.split("\n", 1)[1] if text.startswith("#") else text
    src_words = len(WORDS.findall(body))
    out_words = sum(len(WORDS.findall(l["text"])) for l in lines)
    stats = {"source_words": src_words, "script_words": out_words,
             "coverage": round(out_words / src_words, 3) if src_words else 0.0, "lines": len(lines),
             "speakers": sorted({l["speaker"] for l in lines})}
    if src_words and not 0.9 <= stats["coverage"] <= 1.2:
        problems.append(f"the script has {out_words} words for a chapter of {src_words}: text was dropped or added. "
                        f"Every sentence of the chapter must appear once.")
    return problems, stats


def screenplay_pass(slug: str, number: int, *, backend="auto", model="opus", effort=None, on_limit="api",
                    log=print, max_attempts: int = 2) -> PassResult:
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
    return result
