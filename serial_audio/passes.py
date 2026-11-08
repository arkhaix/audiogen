"""The per-chapter passes. Each reads the library, calls the LLM, and writes back records."""
from __future__ import annotations

from . import library as lib
from .llm import LLMError, PassResult, run_pass

CHARACTER_PROMPT = lib.PROMPTS / "character_pass.system.md"
CHARACTER_SCHEMA = lib.SCHEMAS / "character_pass.schema.json"


def compact_bible(bible: dict) -> str:
    """The bible as the model sees it: one line per character, cheap on tokens."""
    chars = bible.get("characters") or {}
    if not chars:
        return "(no characters yet: this is the first chapter processed)"
    lines = ["id | name | aliases | gender/age | importance | role"]
    for cid, c in chars.items():
        aliases = ", ".join(c.get("aliases") or []) or "-"
        lines.append(f"{cid} | {c.get('name')} | {aliases} | {c.get('gender')}/{c.get('age_group')} | "
                     f"{c.get('importance')} | {c.get('role')}")
    return "\n".join(lines)


def build_character_input(serial: dict, meta: dict, text: str, bible: dict) -> str:
    part = f", {meta['part_title']}" if meta.get("part_title") else ""
    head = (f"# {serial['title']} by {serial.get('author') or 'unknown'}{part}\n"
            f"# Chapter {meta['number']} of the serial: {meta['label']}: {meta['title']}\n\n"
            f"## Known characters (bible after chapter {meta['number'] - 1})\n\n{compact_bible(bible)}\n\n"
            f"## Chapter text\n\n")
    return head + text


def merge_character_pass(bible: dict, out: dict, number: int) -> dict:
    """Apply a character-pass result to the bible in place; returns the list of ids present."""
    chars = bible.setdefault("characters", {})
    present: list[str] = []

    for new in out.get("new_characters", []):
        cid = new["id"]
        if cid in chars:  # the model re-created a known id: treat as an update
            entry = chars[cid]
            entry["aliases"] = _union(entry.get("aliases", []), new.get("aliases", []) + [new["name"]], entry["name"])
            _add_fact(entry, number, new["description"])
        else:
            chars[cid] = {
                "name": new["name"],
                "aliases": _union([], new.get("aliases", []), new["name"]),
                "gender": new["gender"],
                "age_group": new["age_group"],
                "role": new["role"],
                "description": new["description"],
                "voice_brief": new["voice_brief"],
                "importance": new["importance"],
                "speaks": bool(new["speaks"]),
                "first_seen": number,
                "last_seen": number,
                "chapters_present": [number],
                "voice_id": "",
                "facts": [{"chapter": number, "text": new["evidence"]}],
            }
        present.append(cid)
        entry = chars[cid]
        entry["last_seen"] = number
        if number not in entry["chapters_present"]:
            entry["chapters_present"].append(number)
        entry["speaks"] = entry.get("speaks") or bool(new["speaks"])

    for p in out.get("present", []):
        cid = p["id"]
        if cid not in chars:
            continue  # the model named an id it was not given; ignore rather than invent
        entry = chars[cid]
        entry["last_seen"] = number
        if number not in entry["chapters_present"]:
            entry["chapters_present"].append(number)
        entry["speaks"] = entry.get("speaks") or bool(p["speaks"])
        if cid not in present:
            present.append(cid)

    for up in out.get("updates", []):
        entry = chars.get(up["id"])
        if not entry:
            continue
        if up.get("new_aliases"):
            entry["aliases"] = _union(entry.get("aliases", []), up["new_aliases"], entry["name"])
            if entry["name"] == "the narrator":
                entry["name"] = up["new_aliases"][0]
        for fact in up.get("new_facts", []):
            _add_fact(entry, number, fact)
        if up.get("voice_brief_update"):
            entry.setdefault("voice_brief_history", []).append({"chapter": number, "text": entry["voice_brief"]})
            entry["voice_brief"] = up["voice_brief_update"]
        if up.get("importance_update"):
            entry["importance"] = up["importance_update"]
        for key in ("role", "description", "gender", "age_group"):
            if up.get(f"{key}_update"):
                entry[key] = up[f"{key}_update"]

    pron = bible.setdefault("pronunciations", {})
    for p in out.get("pronunciations", []):
        if p["term"] and p["term"] not in pron:
            pron[p["term"]] = {"respelling": p["respelling"], "note": p.get("note", ""), "chapter": number}
    return present


def _add_fact(entry: dict, number: int, text: str) -> None:
    """Append a fact unless the same text is already recorded (re-running a chapter must not duplicate)."""
    facts = entry.setdefault("facts", [])
    if text and not any(f.get("text") == text for f in facts):
        facts.append({"chapter": number, "text": text})


def _union(existing: list[str], extra: list[str], name: str) -> list[str]:
    seen = {name.casefold()}
    out = []
    for item in list(existing) + list(extra):
        key = item.strip().casefold()
        if item.strip() and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


def validate_character_output(out: dict, bible: dict) -> list[str]:
    """Rules the schema cannot express. Each problem is a sentence the model can act on."""
    chars = bible.get("characters") or {}
    new_ids = [c["id"] for c in out.get("new_characters", [])]
    problems = []
    for cid in new_ids:
        if cid in chars:
            problems.append(f"`{cid}` already exists in the bible; report it under `present` or `updates`, not `new_characters`.")
    for p in out.get("present", []):
        if p["id"] not in chars:
            problems.append(f"`{p['id']}` in `present` is not a bible id; new characters belong in `new_characters`.")
    for u in out.get("updates", []):
        if u["id"] not in chars:
            problems.append(f"`{u['id']}` in `updates` is not a bible id.")
    nar = out.get("narration") or {}
    nid = nar.get("narrator_character_id", "")
    if nar.get("point_of_view") == "first_person":
        if "narrator" in chars and nid != "narrator":
            problems.append("The bible already has the first-person narrator under id `narrator`; set "
                            "`narrator_character_id` to `narrator` and report anything new about the narrator in `updates`.")
        elif not nid or (nid not in chars and nid not in new_ids):
            problems.append("The narration is first person, so the narrator must be a character: add an entry to "
                            "`new_characters` (id `narrator`, name \"the narrator\" if the text gives no name) and set "
                            "`narrator_character_id` to it.")
    return problems


def character_pass(slug: str, number: int, *, backend="auto", model="opus", effort=None, on_limit="api",
                   log=print, max_attempts: int = 2) -> PassResult:
    serial = lib.load_serial(slug)
    meta, text = lib.load_chapter(slug, number)
    bible = lib.load_bible(slug)
    system_prompt = CHARACTER_PROMPT.read_text(encoding="utf-8")
    schema = lib.load_json(CHARACTER_SCHEMA)
    user_text = build_character_input(serial, meta, text, bible)

    log(f"character pass: {slug} chapter {number} ({meta['word_count']} words, "
        f"{len(bible['characters'])} known characters) via {backend}/{model}")
    instruction = ("Carry out your instructions on the input above and answer with the JSON object only.")
    for attempt in range(1, max_attempts + 1):
        result = run_pass(system_prompt, user_text, schema, model=model, effort=effort, backend=backend,
                          on_limit=on_limit, instruction=instruction, log=log)
        problems = validate_character_output(result.data, bible)
        if not problems:
            break
        log(f"chapter {number}: attempt {attempt} violated {len(problems)} rule(s): " + " ".join(problems))
        if attempt == max_attempts:
            raise LLMError(f"chapter {number}: output still violates rules after {max_attempts} attempts: "
                           + " ".join(problems))
        instruction = ("Carry out your instructions on the input above and answer with the JSON object only. "
                       "A previous answer broke these rules; fix them: " + " ".join(problems))

    present = merge_character_pass(bible, result.data, number)
    lib.save_bible(slug, bible)
    lib.save_json(lib.chapter_dir(slug, number) / "character_pass.json", result.data)

    meta["characters_present"] = present
    meta["narration"] = result.data.get("narration")
    meta["summary"] = result.data.get("summary", "")
    meta.setdefault("passes", {})["character"] = result.provenance()
    meta["status"] = "characters"
    lib.save_chapter_meta(slug, number, meta)

    # The serial's narration record follows the first chapter that names a first-person narrator
    # (an expository opening chapter may read as third person) and otherwise the first chapter processed.
    nar = result.data.get("narration") or {}
    current = serial.get("narration") or {}
    if nar and (not current or (nar.get("narrator_character_id") and not current.get("narrator_character_id"))):
        serial["narration"] = nar
        lib.save_serial(slug, serial)
    return result
