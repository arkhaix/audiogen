"""Paths and record I/O for the library.

library/<slug>/serial.yaml            the serial: title, source, narration, casting policy
library/<slug>/bible/characters.yaml  id -> character record (grows chapter by chapter)
library/<slug>/bible/pronunciations.yaml
library/<slug>/chapters/0001/text.md  cleaned chapter text, immutable once ingested
library/<slug>/chapters/0001/chapter.yaml
library/<slug>/chapters/0001/author_notes.md  (web serials) the author's notes, kept out of the narration
library/<slug>/source_index.yaml      (web serials) the chapter index as last fetched from the site
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import unicodedata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "library"
PROMPTS = ROOT / "prompts"
SCHEMAS = ROOT / "schemas"


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "untitled"


def serial_dir(slug: str) -> Path:
    return LIBRARY / slug


def chapter_dir(slug: str, number: int) -> Path:
    return serial_dir(slug) / "chapters" / f"{number:04d}"


def load_yaml(path: Path, default=None):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return default if data is None else data


def save_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, width=100)


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def load_serial(slug: str) -> dict:
    data = load_yaml(serial_dir(slug) / "serial.yaml")
    if data is None:
        raise FileNotFoundError(f"no serial '{slug}' in {LIBRARY}")
    return data


def save_serial(slug: str, data: dict) -> None:
    save_yaml(serial_dir(slug) / "serial.yaml", data)


def load_chapter(slug: str, number: int) -> tuple[dict, str]:
    folder = chapter_dir(slug, number)
    meta = load_yaml(folder / "chapter.yaml")
    if meta is None:
        raise FileNotFoundError(f"no chapter {number} for '{slug}'")
    text = (folder / "text.md").read_text(encoding="utf-8")
    return meta, text


def save_chapter_meta(slug: str, number: int, meta: dict) -> None:
    save_yaml(chapter_dir(slug, number) / "chapter.yaml", meta)


def list_chapters(slug: str) -> list[int]:
    folder = serial_dir(slug) / "chapters"
    if not folder.exists():
        return []
    return sorted(int(p.name) for p in folder.iterdir() if p.is_dir() and p.name.isdigit())


def load_bible(slug: str) -> dict:
    bible = serial_dir(slug) / "bible"
    return {
        "characters": load_yaml(bible / "characters.yaml", {}) or {},
        "pronunciations": load_yaml(bible / "pronunciations.yaml", {}) or {},
    }


def save_bible(slug: str, bible: dict) -> None:
    folder = serial_dir(slug) / "bible"
    save_yaml(folder / "characters.yaml", bible["characters"])
    save_yaml(folder / "pronunciations.yaml", bible["pronunciations"])
