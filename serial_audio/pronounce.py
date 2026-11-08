"""Say-as overrides: the spelling a voice reads for a term, stored as `say` on the term's entry in the bible's
pronunciations.yaml. render.say_as_map and render.apply_say_as apply them to every line a voice reads.
"""
from __future__ import annotations

import re

from . import library as lib

MAX_TERM = 80
MAX_SAY = 120
MAX_NOTE = 300


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
    clear: remove the override. An entry that has no respelling left is removed.
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
    if clear and not entry.get("respelling"):
        entries.pop(term, None)
        saved = None
    else:
        entries[term] = entry
        saved = entry
    lib.save_yaml(path, entries)
    return saved
