"""Library records shaped for the web app's API. Never writes."""
from __future__ import annotations

import difflib
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
from .. import phones, pronounce, takecheck
from ..asr import words
from ..pronounce import steady
from ..takecheck import CHECK_FILE, NAME_APART, VERDICTS, for_take, heard, rejudged, stale
from .jobs import INCOMING, read_tail

AUDIO_SUFFIXES = {".opus", ".flac", ".wav", ".mp3", ".ogg", ".m4a"}
MEDIA_MIRROR: Path | None = None
IMPORTANCE_ORDER = {"major": 0, "recurring": 1, "minor": 2, "one_off": 3}
_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_cache: dict[str, tuple[float, int, object]] = {}
_names_cache: dict[str, tuple[tuple, tuple]] = {}
_ways_cache: dict[str, tuple[tuple, dict]] = {}
NAMES_SHOWN = 12
# the most sayings of one name that the Names page sorts into ways, and the most it takes a reference from
SAID_MAX = 400
SAID_SAMPLE = 16


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


def book_folder(serial: dict, slug: str) -> str:
    """The folder name a serial has in an audiobook player's library: its title, or its slug without one."""
    return _file_safe(str(serial.get("title") or "")) or slug


def book_file(number: int, numbers: list[int], meta: dict, suffix: str) -> str:
    """The name a chapter's audio has inside its serial's audiobook folder, "<NNN> - <title><suffix>".

    number: the chapter.
    numbers: every chapter number of the serial, which sets how many digits the number is padded to.
    meta: the chapter's record, for its title or label.
    suffix: the audio file's suffix.
    """
    width = max(3, len(str(max(numbers, default=number))))
    return download_name("", number, width, meta.get("title") or meta.get("label"), suffix)


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
    numbers = lib.list_chapters(slug)
    for n in numbers:
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
            "save_name": book_file(n, numbers, meta, audio.suffix) if audio else None,
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
            "folder": book_folder(serial, slug),
            "voices_pending": sum(1 for v in voices.values() if v.get("status") != "approved"),
            "characters": len(chars), "incoming": incoming, "minutes_per_line": minutes_per_line,
            "render_engine": engine, "render_runner": runner}


def _take_check(folder: Path, script: dict | None, meta: dict, names: dict, bible: dict | None = None) -> list[dict] | None:
    """The takes to listen to, worst first, with the script line, the transcript and the take's media.

    Takes the last check did not pass come from take_check.json. With the bible, verdicts from older rules are
    judged again from their evidence first, as a render judges them. Takes the length guard kept out of range come
    from breeze_takes.json with the verdict "out_of_range", whether or not a check ran. None when the chapter has
    no Breeze takes.
    """
    render = meta.get("render") if isinstance(meta.get("render"), dict) else {}
    if not script or not (folder / TAKES_FILE).is_file():
        return None
    takes = lib.load_json(folder / TAKES_FILE).get("takes") or {}
    check = render.get("check")
    results = {}
    if isinstance(check, dict) and not check.get("skipped") and (folder / CHECK_FILE).is_file():
        checked = lib.load_json(folder / CHECK_FILE)
        results = checked.get("takes") or {}
        if bible is not None and stale(checked, bible):
            try:
                results = rejudged(results, takes, script["lines"], bible)
            except Exception:
                pass
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
                    "heard": heard(r), "ins": r.get("ins"), "del": r.get("del"), "sub": r.get("sub"),
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
            "take_check": _take_check(folder, script, meta, {c["id"]: c["name"] for c in cast}, bible),
            "character_pass": pass_out,
            "prev": numbers[i - 1] if i > 0 else None, "next": numbers[i + 1] if i + 1 < len(numbers) else None}


def _with_media(candidates: list[dict]) -> list[dict]:
    return [{**c, "media": comfy_media(c.get("clip"))} for c in candidates or []]


def voice_findings(slug: str) -> dict[str, list[dict]]:
    """The voice findings in the render records of a serial's chapters, by voice.

    slug: the serial.
    Returns {voice: [{"chapter", "reason", "takes", "of"}, ...]} in chapter order, where takes counts the takes of
    the voice that the reason flagged in the chapter and of counts the voice's checked takes there. A record that
    is not a finding is left out.
    """
    out: dict[str, list[dict]] = {}
    for n in lib.list_chapters(slug):
        found = (_chapter_meta(slug, n).get("render") or {}).get("voice_findings")
        for f in found if isinstance(found, list) else []:
            if isinstance(f, dict) and isinstance(f.get("voice"), str) and isinstance(f.get("lines"), list):
                out.setdefault(f["voice"], []).append({"chapter": n, "reason": str(f.get("reason") or ""),
                                                       "takes": len(f["lines"]), "of": f.get("of")})
    return out


def _heard_in(folder: Path) -> tuple[dict[str, Counter] | None, dict[str, list], dict, tuple]:
    """How a chapter's take check heard each name, and how its takes said it.

    folder: the chapter folder.
    Returns (spellings, said, check, stamp). spellings is {term: Counter of spellings} over the check's entries
    for the takes' current audio, with a possessive 's left off a spelling. It is None when the chapter doesn't
    have both files, when one doesn't parse, or when no entry holds names, which is a check from before the rules
    recorded them. said is {term: [[inside, window], ...]}, the phones of each saying from the entries that have
    them. check is {"rules", "names", "missed"}: the rules and the picked spellings the file was judged with, and
    {term: [the takes that say it with an added sound, the takes that say it]}. stamp changes when either file
    does.
    """
    takes_path, check_path = folder / TAKES_FILE, folder / CHECK_FILE
    try:
        a, b = takes_path.stat(), check_path.stat()
    except OSError:
        return None, {}, {}, ()
    key, stamp = str(folder), (a.st_mtime_ns, a.st_size, b.st_mtime_ns, b.st_size)
    hit = _names_cache.get(key)
    if hit and hit[0] == stamp:
        return (*hit[1], stamp)
    found: dict[str, Counter] | None = None
    said: dict[str, list] = {}
    check: dict = {}
    try:
        takes = lib.load_json(takes_path).get("takes") or {}
        data = lib.load_json(check_path)
        entries = data.get("takes") or {}
        missed: dict[str, list[int]] = {}
        check = {"rules": data.get("rules"), "names": data.get("names"), "missed": missed}
        for k, entry in entries.items():
            if not for_take(entry, takes.get(k)) or not isinstance(entry.get("names"), dict):
                continue
            found = {} if found is None else found
            for term, spellings in entry["names"].items():
                for spelling in spellings if isinstance(spellings, list) else []:
                    if isinstance(spelling, str):
                        found.setdefault(str(term), Counter())[spelling.removesuffix("'s")] += 1
            off = entry.get("missed") if isinstance(entry.get("missed"), list) else []
            for term, spans in (entry["said"].items() if isinstance(entry.get("said"), dict) else ()):
                said.setdefault(str(term), []).extend(
                    [str(s[0]), str(s[1])] for s in (spans if isinstance(spans, list) else [])
                    if isinstance(s, list) and len(s) == 2)
                count = missed.setdefault(str(term), [0, 0])
                count[0] += term in off
                count[1] += 1
    except (OSError, ValueError, AttributeError, TypeError):
        found, said, check = None, {}, {}
    _names_cache[key] = (stamp, (found, said, check))
    return found, said, check, stamp


def _ways(spans: list, ref: str | None) -> dict:
    """How the sayings of one name sort into ways, for the Names page.

    spans: [inside, window] for each saying.
    ref: the usual saying of the name's spelling, or None for the usual way of a sample of SAID_SAMPLE of the spans.
    Returns {"said", "usual", "count", "ways"}: how many sayings were sorted, at most SAID_MAX of them thinned
    evenly, how many of those are the first way, how many ways there are, and the NAMES_SHOWN most frequent ways as
    [phones, count]. A saying whose window has a way's phones in it is that way.
    """
    if len(spans) > SAID_MAX:
        spans = [spans[i * len(spans) // SAID_MAX] for i in range(SAID_MAX)]
    ref = ref or phones.consensus(spans, SAID_SAMPLE)
    if not ref:
        return {"said": 0, "usual": 0, "count": 0, "ways": []}
    usual = [s for s in spans if ref in s[1]]
    rest = [s for s in spans if ref not in s[1]]
    found = phones.ways(rest, ref, NAME_APART)
    found[0]["count"] += len(usual)
    found = [w for w in found if w["count"]] or found[:1]
    return {"said": len(spans), "usual": found[0]["count"] if found[0]["phones"] == ref else 0, "count": len(found),
            "ways": [[w["phones"], w["count"]] for w in found[:NAMES_SHOWN]]}


def _options_view(options, term: str = "") -> dict | None:
    """A term's pronunciation options for the Names page, each candidate with its clip's media, or None.

    options: the term's options from pronunciations.yaml.
    term: the term as the text spells it, whose own candidate shows no count.
    """
    if not isinstance(options, dict) or not isinstance(options.get("candidates"), list):
        return None
    return {"line": options.get("line"), "voice": options.get("voice"),
            "at": options.get("rendered_at") or options.get("at"),
            "candidates": [{"say": c.get("say"), "why": c.get("why"),
                            "heard": _heard_as(c.get("text"), c.get("say"), c.get("heard")),
                            "seconds": c.get("seconds"),
                            "media": comfy_media(c["clip"]) if isinstance(c.get("clip"), str) else None,
                            "steady": _count(c, term),
                            "typical": c.get("typical") is not False or _count(c, term) is None,
                            "offered": _count(c, term) is None or steady(c)}
                           for c in options["candidates"] if isinstance(c, dict)]}


def _count(candidate: dict, term: str = "") -> list[int] | None:
    """A candidate's screening count as [takes without an added sound, takes].

    candidate: an option of a term.
    term: the term as the text spells it.
    Returns None while pronounce.SCREENS is off, for a candidate that pronounce.SCREEN_V did not screen, for the
    term as written, which the name rule doesn't judge, and for a count that is not two whole numbers.
    """
    value = candidate.get("steady")
    ok = (pronounce.SCREENS and candidate.get("screen") == pronounce.SCREEN_V and candidate.get("say") != term
          and isinstance(value, list) and len(value) == 2
          and all(isinstance(n, int) and not isinstance(n, bool) for n in value))
    return [value[0], value[1]] if ok else None


def _heard_as(text, say, heard) -> str:
    """The words of a transcript that stand where a spelling stands in the text the voice was sent.

    text: the text that was sent.
    say: the spelling in it.
    heard: the recognizer's transcript of the take.
    Returns the transcript's words in the spelling's place, or "" when any of the three is missing or the spelling
    isn't in the text.
    """
    if not all(isinstance(v, str) and v for v in (text, say, heard)):
        return ""
    sent, said, got = words(text), words(say), words(heard)
    start = next((i for i in range(len(sent) - len(said) + 1) if sent[i: i + len(said)] == said), None) if said else None
    if start is None:
        return ""
    end, out = start + len(said), []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, sent, got, autojunk=False).get_opcodes():
        if tag == "equal":
            out += got[j1 + max(0, start - i1): j2 - max(0, i2 - end)] if i1 < end and i2 > start else []
        elif (i1 < end and i2 > start) or (i1 == i2 and start < i1 < end):
            out += got[j1:j2]
    return " ".join(out)


def names_heard(slug: str) -> dict:
    """How the recognizer spelled each name of a serial, over the takes its checks heard.

    slug: the serial.
    Returns {"terms": [...], "chapters": {"with": [...], "without": [...]}}. A term is {"term", "say",
    "respelling", "heard", "spellings", "as_written", "top", "chapters", "options"}: the name as the bible has it,
    its say-as and its respelling or "", how many times a take's check heard it, in how many spellings, how many of
    those times in the spelling the voice is sent, the NAMES_SHOWN most frequent spellings as [spelling, count],
    the chapters, and its pronunciation options or None. A term whose takes have phones also has "ways", from
    _ways: how its sayings sort into ways by sound, the first being the usual saying of its spelling when the
    bible has one and else its usual way. A term is "held" while it has a say-as and takecheck.NAME_FLAGS is on,
    and then "adds" is [the takes that say it with an added sound, the takes that say it], over the chapters
    whose check was judged by the rules and the picked spellings as they are now. "findings" lists the chapters
    where a render found so many takes with an added sound that it took none again, as {"chapter", "takes",
    "of"}. The options are {"line", "voice", "at", "candidates"}, and a candidate is {"say", "why", "heard",
    "seconds", "media", "steady", "typical", "offered"}, where heard is how the recognizer wrote the spelling in
    the candidate's take, steady is [the screening takes without an added sound, the screening takes] or None,
    typical is whether the clip is one of those, and offered is False for a screened candidate that
    pronounce.steady rejects. The candidate that is the term as written has no count and is offered. While
    pronounce.SCREENS is off, steady is None and every candidate is offered.
    "screens" in the answer says whether options jobs screen. The held terms with the most takes that add a
    sound come first, then the terms with the most sayings off their first way, then those with the most
    spellings, and a term that has options but was never heard comes after them. chapters lists the chapters
    with Breeze takes whose check holds names, those whose check doesn't yet, and under "judged" those of the
    first whose check is current.
    """
    bible = _bible(slug)
    pronunciations = bible.get("pronunciations") or {}
    held = takecheck.picked_spellings(bible) if takecheck.NAME_FLAGS else {}
    counts: dict[str, Counter] = {}
    where: dict[str, list[int]] = {}
    spans: dict[str, list] = {}
    adds: dict[str, list[int]] = {}
    findings: dict[str, list[dict]] = {}
    known, unknown, judged, stamps = [], [], [], []
    for n in lib.list_chapters(slug):
        folder = lib.chapter_dir(slug, n)
        if not (folder / TAKES_FILE).is_file():
            continue
        found, said, check, stamp = _heard_in(folder)
        if found is None:
            unknown.append(n)
            continue
        known.append(n)
        stamps.append(stamp)
        if not stale(check, bible):
            judged.append(n)
            for term, (missed, saying) in check["missed"].items():
                if term in held:
                    count = adds.setdefault(term, [0, 0])
                    count[0] += missed
                    count[1] += saying
        for term, spellings in found.items():
            counts.setdefault(term, Counter()).update(spellings)
            where.setdefault(term, []).append(n)
        for term, sayings in said.items():
            spans.setdefault(term, []).extend(sayings)
            counts.setdefault(term, Counter())
            if n not in where.setdefault(term, []):
                where[term].append(n)
        off = (_chapter_meta(slug, n).get("render") or {}).get("name_findings")
        for f in off if isinstance(off, list) else []:
            if isinstance(f, dict) and isinstance(f.get("term"), str) and isinstance(f.get("lines"), list):
                findings.setdefault(f["term"], []).append({"chapter": n, "takes": len(f["lines"]), "of": f.get("of")})
    refs = {str(term): entry["said"] for term, entry in pronunciations.items()
            if isinstance(entry, dict) and isinstance(entry.get("said"), str) and entry["said"]}
    key = (tuple(stamps), tuple(sorted(refs.items())))
    if _ways_cache.get(slug, (None,))[0] != key:
        _ways_cache[slug] = (key, {term: _ways(found, refs.get(term)) for term, found in spans.items()})
    ways = _ways_cache[slug][1]

    def view(term: str, entry: dict, spellings: Counter) -> dict:
        sent = " ".join(words(str(entry.get("say") or term)))
        return {"term": term, "say": str(entry.get("say") or ""), "respelling": str(entry.get("respelling") or ""),
                "heard": sum(spellings.values()), "spellings": len(spellings), "as_written": spellings.get(sent, 0),
                "top": [[s, c] for s, c in spellings.most_common(NAMES_SHOWN)], "chapters": where.get(term, []),
                "options": _options_view(entry.get("options"), term), "held": term in held,
                **({"adds": adds.get(term, [0, 0])} if term in held else {}),
                "findings": findings.get(term, []), **({"ways": ways[term]} if ways.get(term, {}).get("said") else {})}

    terms = [view(term, pronunciations.get(term) if isinstance(pronunciations.get(term), dict) else {}, spellings)
             for term, spellings in counts.items()]
    terms += [view(term, entry, Counter()) for term, entry in pronunciations.items()
              if term not in counts and isinstance(entry, dict) and _options_view(entry.get("options"))]

    def off(t: dict) -> int:
        return t["ways"]["said"] - t["ways"]["usual"] if "ways" in t else 0

    terms.sort(key=lambda t: (-t.get("adds", [0])[0], -off(t), -t["spellings"], -t["heard"], t["term"]))
    return {"terms": terms, "chapters": {"with": known, "without": unknown, "judged": judged},
            "screens": pronounce.SCREENS}


def voices(slug: str) -> dict:
    serial = lib.load_serial(slug)
    bible = _bible(slug)
    chars = bible["characters"]
    records = _voice_records(slug)
    briefs = _yaml(lib.serial_dir(slug) / "voices" / "_briefs.yaml", {}) or {}
    findings = voice_findings(slug)
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
                    "role": c.get("role"), "description": c.get("description"), "bio": c.get("bio"),
                    "in_bible": bool(c), "voice_brief": c.get("voice_brief"), "chapters": len(c.get("chapters_present") or []),
                    "media": lib_media(clip) if rec.get("clip") else None,
                    "candidates": _with_media(rec.get("candidates")), "history": history,
                    "effect": (serial.get("voice_effects") or {}).get(vid), "rendered_in": used_in,
                    "findings": findings.get(vid) or []})
    out.sort(key=lambda v: (v["id"] != NARRATOR_ID, IMPORTANCE_ORDER.get(v.get("importance"), 9), v["name"].lower()))
    policy = set((serial.get("casting_policy") or {}).get("dedicated_voice_for") or ["major", "recurring"])
    uncast = []
    for cid, c in chars.items():
        if c.get("voice_id"):
            continue
        uncast.append({"id": cid, "name": c.get("name"), "importance": c.get("importance"),
                       "speaks": c.get("speaks", True), "chapters": len(c.get("chapters_present") or []),
                       "first_seen": c.get("first_seen"), "last_seen": c.get("last_seen"), "role": c.get("role"),
                       "bio": c.get("bio"), "voice_brief": c.get("voice_brief"),
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
