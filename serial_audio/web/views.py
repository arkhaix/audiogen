"""Library records shaped for the web app's API. Never writes."""
from __future__ import annotations

import os
import re
import urllib.parse
import wave
from collections import Counter
from pathlib import Path

import yaml

from .. import library as lib
from ..casting import NARRATOR_ID, needs_narrator
from ..comfy import ComfyError
from ..breeze import TAKES_FILE
from ..render import COMFY_OUTPUT, plan_engine, plan_slots
from ..takecheck import CHECK_FILE, VERDICTS
from .jobs import INCOMING, read_tail

AUDIO_SUFFIXES = {".opus", ".flac", ".wav", ".mp3", ".ogg", ".m4a"}
MEDIA_MIRROR: Path | None = None
IMPORTANCE_ORDER = {"major": 0, "recurring": 1, "minor": 2, "one_off": 3}
_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_cache: dict[str, tuple[float, int, object]] = {}


def _yaml(path: Path, default=None):
    """The parsed file, re-read only when its mtime or size changes."""
    try:
        st = path.stat()
    except OSError:
        return default
    key = str(path)
    hit = _cache.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2] if hit[2] is not None else default
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh, Loader=_Loader)
    _cache[key] = (st.st_mtime, st.st_size, data)
    return data if data is not None else default


def _opus_seconds(path: Path) -> float | None:
    """Seconds in an Ogg Opus file: the last page's granule position less the pre-skip, at 48 kHz."""
    with path.open("rb") as fh:
        head = fh.read(4096)
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 65536))
        tail = fh.read()
    at = head.find(b"OpusHead")
    last = tail.rfind(b"OggS")
    if at < 0 or last < 0 or len(tail) < last + 14:
        return None
    pre_skip = int.from_bytes(head[at + 10:at + 12], "little")
    granule = int.from_bytes(tail[last + 6:last + 14], "little")
    return (granule - pre_skip) / 48000 if granule > pre_skip else None


def _duration(path: Path) -> float | None:
    """Seconds of audio in an Opus, FLAC or WAV file, or None when the file is missing or unreadable."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = "dur:" + str(path)
    hit = _cache.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    seconds = None
    try:
        if path.suffix.lower() == ".flac":
            with path.open("rb") as fh:
                head = fh.read(42)
            if head[:4] == b"fLaC" and (head[4] & 0x7F) == 0:
                si = head[8:42]
                rate = (si[10] << 12) | (si[11] << 4) | (si[12] >> 4)
                total = ((si[13] & 0x0F) << 32) | (si[14] << 24) | (si[15] << 16) | (si[16] << 8) | si[17]
                seconds = total / rate if rate and total else None
        elif path.suffix.lower() == ".opus":
            seconds = _opus_seconds(path)
        elif path.suffix.lower() == ".wav":
            with wave.open(str(path), "rb") as w:
                seconds = w.getnframes() / w.getframerate()
    except (OSError, EOFError, wave.Error, IndexError):
        seconds = None
    _cache[key] = (st.st_mtime, st.st_size, seconds)
    return seconds


def _relative(path: Path | str, root: Path) -> str | None:
    """path relative to root with forward slashes, or None when it lies outside root."""
    full = os.path.normpath(str(path))
    base = os.path.normpath(str(root))
    if os.path.normcase(full).startswith(os.path.normcase(base) + os.sep):
        return full[len(base) + 1:].replace(os.sep, "/")
    return None


def lib_media(path: Path) -> dict | None:
    rel = _relative(path, lib.LIBRARY)
    if rel is None or not path.is_file():
        return None
    return {"url": "/media/lib/" + urllib.parse.quote(rel), "seconds": _duration(path),
            "bytes": path.stat().st_size}


def comfy_media(raw: str | None) -> dict | None:
    if not raw:
        return None
    path = Path(raw)
    rel = _relative(path, COMFY_OUTPUT)
    if rel is not None and path.is_file():
        return {"url": "/media/comfy/" + urllib.parse.quote(rel), "seconds": _duration(path)}
    if MEDIA_MIRROR is None:
        return None
    from .exchange import mirror_rel
    rel = mirror_rel(raw)
    copy = MEDIA_MIRROR / rel if rel else None
    if copy is None or not copy.is_file():
        return None
    return {"url": "/media/mirror/" + urllib.parse.quote(rel), "seconds": _duration(copy)}


def _serial_slugs() -> list[str]:
    if not lib.LIBRARY.exists():
        return []
    return sorted(p.name for p in lib.LIBRARY.iterdir() if (p / "serial.yaml").is_file())


def _chapter_meta(slug: str, n: int) -> dict:
    return _yaml(lib.chapter_dir(slug, n) / "chapter.yaml", {}) or {}


def _voice_records(slug: str) -> dict[str, dict]:
    folder = lib.serial_dir(slug) / "voices"
    if not folder.exists():
        return {}
    out = {}
    for path in sorted(folder.glob("*.yaml")):
        if not path.name.startswith("_"):
            rec = _yaml(path, {}) or {}
            if rec.get("id"):
                out[rec["id"]] = rec
    return out


def _bible(slug: str) -> dict:
    folder = lib.serial_dir(slug) / "bible"
    return {"characters": _yaml(folder / "characters.yaml", {}) or {},
            "pronunciations": _yaml(folder / "pronunciations.yaml", {}) or {}}


def _audio_file(slug: str, meta: dict) -> Path | None:
    rel = (meta.get("render") or {}).get("audio")
    if rel:
        path = lib.serial_dir(slug) / rel
        if path.is_file():
            return path
    return None


def _file_safe(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", text)).strip(" .")


def download_name(serial_title: str, number: int, width: int, title: str | None, suffix: str) -> str:
    """The name a chapter's audio is saved under, so an audiobook reader sorts chapters in order.

    serial_title: the serial's title.
    number: the chapter number, zero-padded to width digits.
    title: the chapter's title, or None.
    suffix: the audio file's suffix.
    Returns "<serial> - <NNN> - <title><suffix>", leaving out empty parts.
    """
    parts = [_file_safe(serial_title or ""), f"{number:0{width}d}", _file_safe(title or "")]
    return " - ".join(p for p in parts if p)[:180].rstrip(" .") + suffix


def chapter_download(slug: str, n: int) -> tuple[Path, str]:
    """The chapter's audio file and the name to save it under. Raises FileNotFoundError when it has no audio."""
    meta = _chapter_meta(slug, n)
    audio = _audio_file(slug, meta) if meta else None
    if audio is None:
        raise FileNotFoundError(f"chapter {n} of {slug} has no audio")
    serial = lib.load_serial(slug)
    width = max(3, len(str(max(lib.list_chapters(slug), default=n))))
    return audio, download_name(serial.get("title") or slug, n, width, meta.get("title") or meta.get("label"),
                                audio.suffix)


def serials() -> list[dict]:
    out = []
    for slug in _serial_slugs():
        serial = _yaml(lib.serial_dir(slug) / "serial.yaml", {}) or {}
        counts: Counter = Counter()
        seconds = 0.0
        words = 0
        for n in lib.list_chapters(slug):
            meta = _chapter_meta(slug, n)
            counts[meta.get("status") or "fetched"] += 1
            words += meta.get("word_count") or 0
            audio = _audio_file(slug, meta)
            if audio:
                seconds += _duration(audio) or 0
        pending = sum(1 for v in _voice_records(slug).values() if v.get("status") != "approved")
        out.append({"slug": slug, "title": serial.get("title") or slug, "subtitle": serial.get("subtitle"),
                    "author": serial.get("author"), "source": (serial.get("source") or {}).get("type"),
                    "known": serial.get("chapters"), "on_disk": sum(counts.values()), "status": dict(counts),
                    "words": words, "audio_seconds": round(seconds), "voices_pending": pending})
    return out


def serial_detail(slug: str, minutes_per_line: float) -> dict:
    serial = lib.load_serial(slug)
    try:
        engine, runner = plan_engine(serial, None)
    except ComfyError:
        engine, runner = serial.get("render_engine"), serial.get("breeze_runner")
    bible = _bible(slug)
    chars = bible["characters"]
    voices = _voice_records(slug)
    policy = set((serial.get("casting_policy") or {}).get("dedicated_voice_for") or ["major", "recurring"])
    rows = []
    for n in lib.list_chapters(slug):
        meta = _chapter_meta(slug, n)
        script = meta.get("script") or {}
        speakers =[s for s in script.get("speakers") or meta.get("characters_present") or [] if s != "narrator"]
        will_cast, narrator_reads, unapproved = [], [], []
        for sp in speakers:
            c = chars.get(sp) or {}
            vid = c.get("voice_id")
            if vid:
                if (voices.get(vid) or {}).get("status") != "approved":
                    unapproved.append(sp)
            elif c.get("importance") in policy and c.get("speaks", True):
                will_cast.append(sp)
            else:
                narrator_reads.append(sp)
        folder = lib.chapter_dir(slug, n)
        audio = _audio_file(slug, meta)
        rows.append({
            "number": n, "label": meta.get("label"), "title": meta.get("title"), "part": meta.get("part"),
            "part_title": meta.get("part_title"), "words": meta.get("word_count"),
            "status": meta.get("status") or "fetched", "published": meta.get("published"),
            "lines": script.get("lines"), "coverage": script.get("coverage"), "speakers": speakers,
            "present": len(meta.get("characters_present") or []), "summary": meta.get("summary"),
            "audio": lib_media(audio) if audio else None, "rendered_at": (meta.get("render") or {}).get("at"),
            "has_notes": (folder / "author_notes.md").is_file(), "will_cast": will_cast,
            "narrator_reads": narrator_reads, "unapproved": unapproved,
            "estimate_minutes": round(script["lines"] * minutes_per_line) if script.get("lines") else None,
        })
    have = {r["number"] for r in rows}
    index = _yaml(lib.serial_dir(slug) / "source_index.yaml", {}) or {}
    remote = [{"number": c["number"], "title": c.get("title"), "date": c.get("date"), "url": c.get("url")}
              for c in index.get("chapters") or [] if c["number"] not in have]
    incoming_dir = INCOMING / slug
    incoming = sorted(p.name for p in incoming_dir.iterdir() if p.is_file()) if incoming_dir.is_dir() else []
    return {"serial": serial, "chapters": rows, "remote": remote, "index_checked": index.get("checked_at"),
            "voices_pending": sum(1 for v in voices.values() if v.get("status") != "approved"),
            "characters": len(chars), "incoming": incoming, "minutes_per_line": minutes_per_line,
            "render_engine": engine, "render_runner": runner}


def _take_check(folder: Path, script: dict | None, meta: dict, names: dict) -> list[dict] | None:
    """The takes to listen to, worst first, with the script line, the transcript and the take's media.

    Takes the last check did not pass come from take_check.json. Takes the length guard kept out of range come from
    breeze_takes.json with the verdict "out_of_range", whether or not a check ran. None when the chapter has no
    Breeze takes.
    """
    render = meta.get("render") if isinstance(meta.get("render"), dict) else {}
    if not script or not (folder / TAKES_FILE).is_file():
        return None
    takes = lib.load_json(folder / TAKES_FILE).get("takes") or {}
    check = render.get("check")
    results = {}
    if isinstance(check, dict) and not check.get("skipped") and (folder / CHECK_FILE).is_file():
        results = lib.load_json(folder / CHECK_FILE).get("takes") or {}
    rank = {v: i for i, v in enumerate(VERDICTS)}
    rank["out_of_range"] = len(VERDICTS)
    out = []
    for k, take in takes.items():
        i = int(k)
        r = results.get(k) or {}
        verdict = "out_of_range" if take.get("out_of_range") else r.get("verdict")
        if verdict in (None, "ok") or i >= len(script["lines"]):
            continue
        line = script["lines"][i]
        out.append({"line": i, "verdict": verdict, "speaker": line["speaker"], "name": names.get(line["speaker"]),
                    "voice": take.get("voice"), "direction": line.get("direction") or "", "said": line["text"],
                    "heard": r.get("heard"), "ins": r.get("ins"), "del": r.get("del"), "sub": r.get("sub"),
                    "wer": r.get("wer"), "seconds": _duration(Path(take["file"])) if take.get("file") else None,
                    "media": comfy_media(take.get("file"))})
    out.sort(key=lambda t: (-rank.get(t["verdict"], 0), t["line"]))
    return out


def chapter_detail(slug: str, n: int) -> dict:
    serial = lib.load_serial(slug)
    folder = lib.chapter_dir(slug, n)
    meta = _chapter_meta(slug, n)
    if not meta:
        raise FileNotFoundError(f"no chapter {n} for {slug}")
    text = (folder / "text.md").read_text(encoding="utf-8") if (folder / "text.md").is_file() else ""
    notes = (folder / "author_notes.md").read_text(encoding="utf-8") if (folder / "author_notes.md").is_file() else None
    bible = _bible(slug)
    voices = _voice_records(slug)
    script = lib.load_json(folder / "script.json") if (folder / "script.json").is_file() else None
    cast = []
    plan_notes = []
    if script:
        try:
            speaker_slot, slot_voice, plan_notes = plan_slots(serial, bible, script["lines"])
        except ComfyError as e:
            speaker_slot, slot_voice, plan_notes = {}, {}, [str(e)]
        counts = Counter(line["speaker"] for line in script["lines"])
        for sp, count in counts.most_common():
            c = bible["characters"].get(sp) or {}
            slot = speaker_slot.get(sp)
            voice = slot_voice.get(slot) if slot is not None else None
            if sp != "narrator" and not c.get("voice_id"):
                voice = None
            cast.append({"id": sp, "name": "Narrator" if sp == "narrator" else c.get("name") or sp,
                         "lines": count, "slot": slot, "voice": voice,
                         "voice_status": (voices.get(voice) or {}).get("status") if voice else None,
                         "importance": c.get("importance")})
    audio = _audio_file(slug, meta)
    log = folder / "render.log"
    pass_out = None
    if (folder / "character_pass.json").is_file():
        raw = lib.load_json(folder / "character_pass.json")
        pass_out = {"new": [{"id": c["id"], "name": c.get("name"), "importance": c.get("importance"),
                             "role": c.get("role")} for c in raw.get("new_characters") or []],
                    "pronunciations": raw.get("pronunciations") or []}
    numbers = lib.list_chapters(slug)
    i = numbers.index(n)
    return {"slug": slug, "title": serial.get("title"), "meta": meta, "text": text, "author_notes": notes,
            "script": script, "cast": cast, "plan_notes": plan_notes,
            "audio": lib_media(audio) if audio else None,
            "render_log": read_tail(log, 40_000) if log.is_file() else None,
            "take_check": _take_check(folder, script, meta, {c["id"]: c["name"] for c in cast}),
            "character_pass": pass_out,
            "prev": numbers[i - 1] if i > 0 else None, "next": numbers[i + 1] if i + 1 < len(numbers) else None}


def _with_media(candidates: list[dict]) -> list[dict]:
    return [{**c, "media": comfy_media(c.get("clip"))} for c in candidates or []]


def voices(slug: str) -> dict:
    serial = lib.load_serial(slug)
    bible = _bible(slug)
    chars = bible["characters"]
    records = _voice_records(slug)
    briefs = _yaml(lib.serial_dir(slug) / "voices" / "_briefs.yaml", {}) or {}
    seeds = []
    out = []
    for vid, rec in records.items():
        c = chars.get(rec.get("character_id") or vid) or {}
        history = []
        for h in rec.get("history") or []:
            history.append({**h, "candidates": _with_media(h.get("candidates"))})
            seeds += [x.get("seed", 0) for x in h.get("candidates") or []]
        seeds += [x.get("seed", 0) for x in rec.get("candidates") or []]
        clip = lib.serial_dir(slug) / "voices" / (rec.get("clip") or "")
        used_in = [n for n in lib.list_chapters(slug)
                   if vid in ((_chapter_meta(slug, n).get("render") or {}).get("slots") or {}).values()]
        out.append({**rec, "name": "Narrator" if vid == NARRATOR_ID else c.get("name") or vid,
                    "importance": "narrator" if vid == NARRATOR_ID else c.get("importance"),
                    "role": c.get("role"), "description": c.get("description"),
                    "voice_brief": c.get("voice_brief"), "chapters": len(c.get("chapters_present") or []),
                    "media": lib_media(clip) if rec.get("clip") else None,
                    "candidates": _with_media(rec.get("candidates")), "history": history,
                    "effect": (serial.get("voice_effects") or {}).get(vid), "rendered_in": used_in})
    out.sort(key=lambda v: (v["id"] != NARRATOR_ID, IMPORTANCE_ORDER.get(v.get("importance"), 9), v["name"].lower()))
    policy = set((serial.get("casting_policy") or {}).get("dedicated_voice_for") or ["major", "recurring"])
    uncast = []
    for cid, c in chars.items():
        if c.get("voice_id"):
            continue
        uncast.append({"id": cid, "name": c.get("name"), "importance": c.get("importance"),
                       "speaks": c.get("speaks", True), "chapters": len(c.get("chapters_present") or []),
                       "first_seen": c.get("first_seen"), "last_seen": c.get("last_seen"), "role": c.get("role"),
                       "voice_brief": c.get("voice_brief"),
                       "policy": c.get("importance") in policy and c.get("speaks", True),
                       "brief": briefs.get(cid)})
    uncast.sort(key=lambda u: (not u["policy"], IMPORTANCE_ORDER.get(u["importance"], 9), -(u["chapters"] or 0)))
    top = max(seeds, default=0)
    return {"voices": out, "uncast": uncast, "next_seed": (top // 1000 + 1) * 1000 if top else 1000,
            "needs_narrator": needs_narrator(serial), "narrator_voice_id": serial.get("narrator_voice_id"),
            "effects": serial.get("voice_effects") or {}}


def bible(slug: str) -> dict:
    b = _bible(slug)
    voices_by_id = _voice_records(slug)
    chars = []
    for cid, c in b["characters"].items():
        vid = c.get("voice_id")
        chars.append({**c, "id": cid, "voice_status": (voices_by_id.get(vid) or {}).get("status") if vid else None})
    chars.sort(key=lambda c: (IMPORTANCE_ORDER.get(c.get("importance"), 9), c.get("first_seen") or 0,
                              (c.get("name") or "").lower()))
    pron = [{"term": t, **(p or {})} for t, p in b["pronunciations"].items()]
    return {"characters": chars, "pronunciations": pron}
