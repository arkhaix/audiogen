"""The web app's job queue.

Every change to the library runs as a `python -m serial_audio.cli` job under runjob.py, so jobs outlive the server.
Heavy jobs (render, cast, check-takes, character-pass, screenplay) run one at a time and wait while any pipeline process the app
did not start is running, or while ComfyUI is busy with a prompt the app did not queue. A job that queues ComfyUI
prompts also waits for ComfyUI to answer. A Breeze render under breeze-server does not. Quick jobs (pick-voice,
imports) run beside a render but never beside a job that rewrites the bible.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .. import library as lib
from ..comfy import ComfyError
from ..render import plan_engine
from . import procs

ROOT = lib.ROOT
STATE_DIR = ROOT / ".webapp"
JOBS_FILE = STATE_DIR / "jobs.json"
LOG_DIR = STATE_DIR / "jobs"
INCOMING = ROOT / "incoming"
COMFY_URL = "http://127.0.0.1:8188"

HEAVY = {"render", "cast", "bios", "check-takes", "character-pass", "screenplay", "say-options"}
BIBLE_WRITERS = {"character-pass", "cast", "bios"}
SERIAL_WRITERS = {"voice-effect"}    # rewrites serial.yaml in place, so it waits for any job on that serial
# a render's own changes show in render_changes, and no render reads a name's options
RENDER_NEUTRAL = {"render", "say-options"}
# the kinds of backlog work that write only their own chapter's files, so they run beside each other
BACKLOG_BESIDE = frozenset({"render", "check-takes"})
FINISHED = {"done", "failed", "cancelled", "stopped", "skipped", "lost"}
SETTLE_SECONDS = 15
TICK_SECONDS = 2.0
COMFY_POLL_SECONDS = 4.0
KEEP_FINISHED = 300
DEFAULT_MIN_PER_LINE = 0.5
DECODE_SECONDS = 150
CHECK_SECONDS_PER_TAKE = 2
COMFY_TAKE_SECONDS = 14
# seconds each part of a Breeze render takes, as (fixed, per unit), until a node's own renders give its numbers.
# setup runs from the step to the end of the first take, which warms the model up. takes counts the takes after
# the first, finish the takes made, check the takes heard before the join, and join the chapter's words
RENDER_COST = {"setup": (25.0, 0.0), "takes": (0.0, 4.5), "check": (20.0, 1.8), "join": (10.0, 0.012),
               "finish": (2.0, 0.15)}
WORDS_PER_LINE = 16
QUICK_SETUP_SECONDS = 5.0
# a round of retakes starts the recognizer again, and a flagged take costs about two takes to make and hear
RETAKE_SECONDS = 8.0
RETAKE_TRIES = 2
REORDER = ("top", "bottom", "up", "down", "before")
# a part that outlasts its estimate keeps this share of it as time left
OVERRUN_SHARE = 0.1

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
SPEC_RE = re.compile(r"^(all|new|\d+(-\d+)?(,\d+(-\d+)?)*)$")
BACKENDS = {"auto", "subscription", "api"}
MODELS = {"opus", "sonnet", "haiku"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
ON_LIMIT = {"api", "wait", "fail"}


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return _dt.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


# Commands

def _slug(p: dict, must_exist: bool = True) -> str:
    slug = str(p.get("slug") or "").strip()
    if not SLUG_RE.match(slug):
        raise ValueError(f"bad serial slug {slug!r}")
    if must_exist and not (lib.serial_dir(slug) / "serial.yaml").exists():
        raise ValueError(f"no serial {slug!r} in the library")
    return slug


def _spec(value, default: str | None = None) -> str:
    spec = str(value if value not in (None, "") else (default or "")).replace(" ", "")
    if not SPEC_RE.match(spec):
        raise ValueError(f"bad chapter list {value!r}: use 7, 7-12, 3,5,8 or all")
    return spec


def _int(p: dict, key: str, lo: int, hi: int, default: int | None = None) -> int | None:
    value = p.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{key} must be a whole number")
    try:
        n = int(value)
    except ValueError:
        raise ValueError(f"{key} must be a whole number") from None
    if not lo <= n <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return n


def _text(p: dict, key: str, limit: int = 2000) -> str | None:
    value = p.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    text = value.replace("\x00", "").strip()
    if len(text) > limit:
        raise ValueError(f"{key} is longer than {limit} characters")
    return text or None


def _choice(p: dict, key: str, allowed: set[str], default: str | None) -> str | None:
    value = p.get(key) or default
    if value is not None and (not isinstance(value, str) or value not in allowed):
        raise ValueError(f"{key} must be one of {sorted(allowed)}")
    return value


def _incoming_file(slug_dir: str, name, suffixes: tuple[str, ...]) -> Path:
    path = (INCOMING / slug_dir / Path(str(name or "")).name).resolve()
    if not path.is_relative_to(INCOMING.resolve()) or not path.is_file():
        raise ValueError(f"no uploaded file {name!r}")
    if path.suffix.lower() not in suffixes:
        raise ValueError(f"{path.name}: expected {', '.join(suffixes)}")
    return path


def _character_ids(p: dict) -> list[str]:
    raw = p.get("characters") or []
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        raise ValueError("characters must be a list of ids")
    ids = [c.strip() for c in raw if c.strip()]
    for cid in ids:
        if not SLUG_RE.match(cid):
            raise ValueError(f"bad character id {cid!r}")
    return ids


def _llm_args(p: dict) -> list[str]:
    args = ["--backend", _choice(p, "backend", BACKENDS, "auto"), "--model", _choice(p, "model", MODELS, "opus"),
            "--on-limit", _choice(p, "on_limit", ON_LIMIT, "api")]
    effort = _choice(p, "effort", EFFORTS, None)
    return args + (["--effort", effort] if effort else [])


def _expand(slug: str, spec: str, coming=()) -> list[int]:
    """The chapters a spec names.

    slug: the serial.
    spec: a chapter list such as 7-12, "all" or "new".
    coming: chapters that earlier jobs of the batch import. They count beside the chapters on disk.
    Returns the chapter numbers in order. "new" names none.
    """
    have = sorted(set(lib.list_chapters(slug)) | set(coming))
    if spec in ("all", "new"):
        return have if spec == "all" else []
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.extend(n for n in have if int(a) <= n <= int(b or a))
    return out


def _chapter_lines(slug: str, number: int) -> int | None:
    meta = lib.load_yaml(lib.chapter_dir(slug, number) / "chapter.yaml", {}) or {}
    return (meta.get("script") or {}).get("lines")


def _render_plan(slug: str, number: int, decode_only: bool) -> tuple[str, str | None]:
    """The engine and Breeze runner a render job for the chapter asks for. Raises ValueError on a bad serial.yaml."""
    meta = lib.load_yaml(lib.chapter_dir(slug, number) / "chapter.yaml", {}) or {}
    last = meta.get("render") if isinstance(meta.get("render"), dict) else None
    try:
        return plan_engine(lib.load_serial(slug), last, skip_generate=decode_only)
    except ComfyError as e:
        raise ValueError(str(e)) from None


def build(kind: str, p: dict, comfy_url: str = COMFY_URL, coming: dict | None = None) -> dict:
    """The job fields for a request: CLI args, lane, title and metadata.

    kind: a CLI subcommand the app exposes.
    p: the request's parameters.
    comfy_url: the ComfyUI address render and cast jobs use.
    coming: {slug: chapters} that jobs queued ahead of this one in its batch import. A character pass or a
        screenplay counts them as chapters of the serial.
    Returns {"args", "heavy", "gpu", "comfy", "title", "slug", "meta"}. gpu: the job uses the card. comfy: it queues
    ComfyUI prompts. Raises ValueError on a bad request.
    """
    if not isinstance(kind, str) or not isinstance(p, dict):
        raise ValueError("each job needs a kind and a params object")
    if kind in ("character-pass", "screenplay"):
        slug = _slug(p)
        spec = _spec(p.get("chapters"))
        args = [kind, slug, spec] + _llm_args(p) + (["--redo"] if p.get("redo") else [])
        label = "Character pass" if kind == "character-pass" else "Screenplay"
        return {"args": args, "heavy": True, "gpu": False, "comfy": False, "title": f"{label} · ch {spec}", "slug": slug,
                "meta": {"chapters": _expand(slug, spec, (coming or {}).get(slug, ()))}}

    if kind == "render":
        slug = _slug(p)
        n = _int(p, "chapter", 1, 99999)
        if n is None:
            raise ValueError("render needs a chapter")
        seed = _int(p, "seed", 0, 2**31)
        decode_only = bool(p.get("decode_only"))
        engine, runner = _render_plan(slug, n, decode_only)
        args = ["render", slug, str(n)] + (["--seed", str(seed)] if seed is not None else [])
        args += ["--comfy-url", comfy_url, "--engine", engine]
        if runner:
            args += ["--runner", runner]
        if decode_only:
            args.append("--decode-only")
        again = "Join" if engine == "breeze" else "Decode"
        title = f"{again if decode_only else 'Render'} · ch {n}"
        comfy = engine == "drama3" or (runner == "comfyui" and not decode_only)
        return {"args": args, "heavy": True, "gpu": comfy or not decode_only, "comfy": comfy, "title": title,
                "slug": slug, "meta": {"chapters": [n], "lines": _chapter_lines(slug, n), "decode_only": decode_only,
                                       "engine": engine, "runner": runner}}

    if kind == "check-takes":
        slug = _slug(p)
        n = _int(p, "chapter", 1, 99999)
        if n is None:
            raise ValueError("check-takes needs a chapter")
        args = ["check-takes", slug, str(n), "--comfy-url", comfy_url] + (["--redo"] if p.get("redo") else [])
        return {"args": args, "heavy": True, "gpu": True, "comfy": True, "title": f"Check takes · ch {n}", "slug": slug,
                "meta": {"chapters": [n], "lines": _chapter_lines(slug, n)}}

    if kind == "cast":
        slug = _slug(p)
        ids = _character_ids(p)
        args = ["cast", slug, "--candidates", str(_int(p, "candidates", 1, 12, 3)),
                "--seed", str(_int(p, "seed", 0, 2**31, 1000)), "--comfy-url", comfy_url] + _llm_args(p)
        if ids:
            args += ["--characters", ",".join(ids)]
        if p.get("recast"):
            if not ids:
                raise ValueError("a recast needs the characters to replace")
            args.append("--recast")
        instruct = _text(p, "instruct")
        if instruct:
            if len(ids) != 1:
                raise ValueError("a voice description applies to exactly one character")
            args += ["--instruct", instruct]
        if p.get("redo_brief"):
            args.append("--redo-brief")
        note = _text(p, "note", 500)
        if note:
            args += ["--note", note]
        dry = bool(p.get("dry_run"))
        if dry:
            args.append("--dry-run")
        first = _int(p, "from_chapter", 1, 99999)
        if first is not None and ids:
            raise ValueError("a cast that follows a chapter's character pass names no characters")
        who = ", ".join(ids) if ids else f"ch {first}" if first else "everyone who needs a voice"
        verb = "Recast" if p.get("recast") else ("Casting briefs" if dry else "Cast")
        meta = {"characters": ids, "candidates": _int(p, "candidates", 1, 12, 3), "dry_run": dry}
        if first is not None:
            meta["from_chapter"] = first
        return {"args": args, "heavy": True, "gpu": not dry, "comfy": not dry, "title": f"{verb} · {who}", "slug": slug,
                "meta": meta}

    if kind == "bios":
        slug = _slug(p)
        ids = _character_ids(p)
        redo = bool(p.get("redo"))
        args = ["bios", slug] + _llm_args(p)
        if ids:
            args += ["--characters", ",".join(ids)]
        if redo:
            args.append("--redo")
        who = "every voice" if redo else "every voice without one"
        if ids:
            who = ", ".join(ids) if len(ids) <= 3 else f"{len(ids)} characters"
        return {"args": args, "heavy": True, "gpu": False, "comfy": False, "title": f"Casting bios · {who}",
                "slug": slug, "meta": {"characters": ids}}

    if kind == "pick-voice":
        slug = _slug(p)
        cid = str(p.get("character") or "")
        if not SLUG_RE.match(cid):
            raise ValueError(f"bad character id {cid!r}")
        seed = _int(p, "seed", 0, 2**31)
        args = ["pick-voice", slug, cid] + (["--seed", str(seed)] if seed is not None else [])
        approve = p.get("approve", True)
        if not approve:
            args.append("--no-approve")
        what = f"seed {seed}" if seed is not None else "current"
        title = f"{'Approve' if approve else 'Pick'} voice · {cid} ({what})"
        return {"args": args, "heavy": False, "gpu": False, "comfy": False, "title": title, "slug": slug,
                "meta": {"characters": [cid]}}

    if kind == "say-as":
        slug = _slug(p)
        term = _text(p, "term", 200)
        if not term:
            raise ValueError("say-as needs a term")
        clear = bool(p.get("clear"))
        say = None if clear else _text(p, "say", 200)
        if not clear and not say:
            raise ValueError("say-as needs the spelling the voice reads, or clear")
        args = ["say-as", slug]
        note = p.get("note")
        if note is not None:
            if not isinstance(note, str) or len(note) > 600:
                raise ValueError("note must be text of at most 600 characters")
            args.append(f"--note={note.strip()}")
        if clear:
            args.append("--clear")
        args += ["--", term] + ([say] if say else [])
        title = f"Read {term} as written" if clear else f"Read {term} as {say}"
        return {"args": args, "heavy": False, "gpu": False, "comfy": False, "title": title, "slug": slug,
                "meta": {"term": term}}

    if kind == "say-options":
        slug = _slug(p)
        term = _text(p, "term", 80)
        if not term:
            raise ValueError("say-options needs a term")
        again = bool(p.get("render_only"))
        say = _text(p, "say", 120)
        if p.get("say") is not None and not re.search(r"[^\W\d_]", say or ""):
            raise ValueError("say-options needs a letter in the spelling to hear")
        if say and again:
            raise ValueError("say-options takes a spelling to add or render_only, not both")
        args = ["say-options", slug, "--comfy-url", comfy_url] + _llm_args(p)
        args += ([f"--add={say}"] if say else ["--render-only"] if again else []) + ["--", term]
        title = (f"Hear {term} as {say}" if say else f"Say the options for {term} again" if again
                 else f"Options for {term}")
        return {"args": args, "heavy": True, "gpu": True, "comfy": False, "title": title, "slug": slug,
                "meta": {"term": term}}

    if kind == "voice-effect":
        from ..render import VOICE_EFFECTS
        slug = _slug(p)
        voice = str(p.get("voice") or "")
        if not SLUG_RE.match(voice):
            raise ValueError(f"bad voice id {voice!r}")
        clear = bool(p.get("clear"))
        effect = None if clear else _text(p, "effect", 40)
        if not clear and effect not in VOICE_EFFECTS:
            raise ValueError(f"voice-effect needs clear or an effect from {', '.join(sorted(VOICE_EFFECTS))}")
        args = ["voice-effect", slug, voice] + (["--clear"] if clear else [effect])
        level = p.get("level")
        if level is not None and not clear:
            if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 <= level <= 1:
                raise ValueError("level must be a number from 0 to 1")
            args += ["--level", str(level)]
        title = f"No effect on {voice}" if clear else f"{effect.capitalize()} effect on {voice}"
        return {"args": args, "heavy": False, "gpu": False, "comfy": False, "title": title, "slug": slug,
                "meta": {"voice": voice}}

    if kind == "fetch":
        slug = _slug(p)
        if p.get("html"):
            n = _int(p, "chapter", 1, 99999)
            if n is None:
                raise ValueError("importing a saved page needs its chapter number")
            path = _incoming_file(slug, p["html"], (".htm", ".html"))
            args = ["fetch", slug, str(n), "--html", str(path)] + (["--force"] if p.get("force") else [])
            return {"args": args, "heavy": False, "gpu": False, "comfy": False, "title": f"Import saved page · ch {n}", "slug": slug,
                    "meta": {"chapters": [n], "file": path.name}}
        spec = _spec(p.get("chapters"), "new")
        args = ["fetch", slug, spec]
        limit = _int(p, "limit", 1, 1000)
        if limit and spec == "new":
            args += ["--limit", str(limit)]
        if p.get("no_refresh"):
            args.append("--no-refresh")
        if p.get("force"):
            args.append("--force")
        return {"args": args, "heavy": False, "gpu": False, "comfy": False, "title": f"Fetch from site · {spec}", "slug": slug,
                "meta": {}}

    if kind == "add-royalroad":
        url = str(p.get("url") or "").strip()
        if not (url.isdigit() or re.match(r"^https://(www\.)?royalroad\.com/fiction/\d+", url)):
            raise ValueError("give a Royal Road fiction URL or id")
        args = ["add-royalroad", url, "--chapters", _spec(p.get("chapters"), "1") if p.get("chapters") != "none" else "none"]
        if p.get("slug"):
            args += ["--slug", _slug(p, must_exist=False)]
        return {"args": args, "heavy": False, "gpu": False, "comfy": False, "title": f"Add from Royal Road · {url}",
                "slug": p.get("slug") or None, "meta": {}}

    if kind == "add-epub":
        path = _incoming_file("_new", p.get("file"), (".epub",))
        args = ["add-epub", str(path)]
        if p.get("slug"):
            args += ["--slug", _slug(p, must_exist=False)]
        for key in ("title", "author"):
            value = _text(p, key, 300)
            if value:
                args += [f"--{key}", value]
        if p.get("force"):
            args.append("--force")
        return {"args": args, "heavy": False, "gpu": False, "comfy": False, "title": f"Add EPUB · {path.name}",
                "slug": p.get("slug") or None, "meta": {}}

    if kind == "reset-bible":
        slug = _slug(p)
        if p.get("confirm") != slug:
            raise ValueError("type the serial's slug to confirm")
        return {"args": ["reset-bible", slug], "heavy": True, "gpu": False, "comfy": False, "title": "Reset bible", "slug": slug,
                "meta": {}}

    raise ValueError(f"unknown job kind {kind!r}")


# Progress

PROGRESS_RE = re.compile(r"\[(\d+) min\] chunk (\d+): step (\d+)/(\d+)(?:, (\d+) ms/step)?")
RENDER_HEAD_RE = re.compile(r"render: \S+ chapter (\d+): (\d+) lines")
TAKES_RE = re.compile(r"\[(\d+) min\] take (\d+)/(\d+), (\d+) ms/take")
TAKES_HEAD_RE = re.compile(r"generating takes .*?: (\d+) of \d+ lines")
CHECK_HEAD_RE = re.compile(r"checking (\d+) takes with (?:Qwen3-ASR|Parakeet)")
CHECK_RE = re.compile(r"\[(\d+) min\] checked (\d+)/(\d+), ([\d.]+) s/take")
PRECHECK_RE = re.compile(r"checking the (\d+) (?:new )?takes? with")
RETAKE_RE = re.compile(r"taking (\d+) of them again")
FIRST_RE = re.compile(r"first take done in (\d+) s")
# the first line of each part of a job in a worker's log, by the name its arrival time is kept under
LOG_MARKS = (("step", "$ python -m serial_audio.cli"), ("takes", "generating takes"), ("first", "first take done in"),
             ("check", "before the join..."), ("join", "joining takes"), ("joined", "   joined "),
             ("audio", "   audio: "))
BREEZE_HINTS = ("with Breeze TTS 2", "generating takes", "joining takes", "before the join")
CASTING_RE = re.compile(r"^casting (\d+): (.*)$", re.M)
CANDIDATE_RE = re.compile(r"^\s+\S+ seed \d+: match", re.M)
DESIGNED_RE = re.compile(r"^\s+\S+ seed \d+: designed ", re.M)


def read_tail(path: Path, size: int = 96_000) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            fh.seek(max(0, end - size))
            data = fh.read()
    except OSError:
        return ""
    return data.replace(b"\x00", b"").decode("utf-8", "replace")


def render_cost(part: str, units: float = 0.0) -> float:
    """Seconds a part of a Breeze render is expected to take on a node that has no numbers of its own.

    part: a key of RENDER_COST.
    units: the part's amount of work, in the unit RENDER_COST names for it.
    """
    fixed, per = RENDER_COST[part]
    return fixed + per * units


def render_progress(text: str, log_mtime: float | None, now: float, started: float | None, *,
                    marks: dict | None = None, cost=None, make: int | None = None, hear: int | None = None) -> dict:
    """Phase, fraction done and seconds left for a render, from its log.

    text: the log, or its tail.
    log_mtime: when the log last changed, which dates its last progress line.
    now: the current time.
    started: when the render process started, or None.
    marks, cost, make, hear: as breeze_progress takes them. A render with make counts as a Breeze render before
        its log says so.
    Returns {"phase", "fraction", "eta", "detail"}. Fraction and eta may be None. "prechecked" is True once a
    Breeze render has checked its new takes before the join, so the check after the join has nothing to transcribe.
    """
    head = text.rfind("render: ")
    seg = text[head:] if head >= 0 else text
    if make is not None or any(hint in seg for hint in BREEZE_HINTS):
        return breeze_progress(seg, log_mtime, now, started, marks=marks, cost=cost, make=make, hear=hear)
    out = {"phase": "starting", "fraction": None, "eta": None, "detail": ""}
    m = RENDER_HEAD_RE.search(seg)
    if m:
        out["lines"] = int(m.group(2))
    if "audio: " in seg:
        return {**out, "phase": "done", "fraction": 1.0, "eta": 0, "detail": "audio written"}
    if "voice effects" in seg:
        return {**out, "phase": "effects", "fraction": 0.99, "eta": 30, "detail": "applying voice effects"}
    if "decoding..." in seg:
        return {**out, "phase": "decoding", "fraction": 0.98, "eta": DECODE_SECONDS, "detail": "decoding audio"}
    if "generating codes" not in seg:
        return out
    marks = PROGRESS_RE.findall(seg)
    out["phase"] = "generating"
    if not marks:
        elapsed = now - started if started else None
        out["detail"] = "loading Drama 3" + (f", {elapsed / 60:.0f} min in" if elapsed else "")
        if out.get("lines"):
            out["eta"] = out["lines"] * DEFAULT_MIN_PER_LINE * 60
        return out
    _, _, step, total, ms = marks[-1]
    step, total = int(step), int(total)
    per = int(ms) / 1000 if ms else None
    if per and log_mtime:
        step = min(total, step + int(max(0.0, now - log_mtime) / per))
    out["fraction"] = step / total if total else None
    out["steps"] = [step, total]
    if per:
        out["eta"] = (total - step) * per + DECODE_SECONDS
        out["rate_ms"] = int(ms)
    out["detail"] = f"step {step:,} of {total:,}" + (f" at {ms} ms/step" if ms else "")
    return out


def breeze_progress(seg: str, log_mtime: float | None, now: float, started: float | None, *,
                    marks: dict | None = None, cost=None, make: int | None = None, hear: int | None = None) -> dict:
    """Phase, fraction done and seconds left for a Breeze render, from its log.

    seg: the log from the render's head line on, or all of it before that line.
    log_mtime: when the log last changed, which dates its last progress line.
    now: the current time.
    started: when the render started, or None.
    marks: when the first line of each part arrived, by the names of LOG_MARKS. A part without a mark does not
        count down.
    cost: cost(part, units) gives the seconds a part of RENDER_COST is expected to take on the render's node.
        None reads RENDER_COST.
    make: the takes the render is expected to make, for the time before its log says.
    hear: the takes its check before the join is expected to hear, for the same time. None means the takes it
        makes. A render also hears saved takes that lack evidence of the current check.
    Returns {"phase", "fraction", "eta", "detail"}, plus "lines" from the head line, "prechecked" once the render
    has checked its takes before the join, and "steps" once a line counts the takes made. eta runs to the end of
    the job: the rest of the current part and every part after it. It is None while the takes to make are unknown.
    A part that outlasts its estimate keeps OVERRUN_SHARE of it. fraction is the share of the job's expected
    length that has passed, and None when that length is unknown.
    """
    # Read the log
    timed = marks is not None
    marks, cost = marks or {}, cost or render_cost
    out: dict = {}
    m = RENDER_HEAD_RE.search(seg)
    lines = int(m.group(2)) if m else 0
    if m:
        out["lines"] = lines
    head = TAKES_HEAD_RE.search(seg)
    if head:
        make = int(head.group(1))
    pre = PRECHECK_RE.search(seg) if "could not check the new takes" not in seg else None
    if pre:
        out["prechecked"] = True
    post = CHECK_HEAD_RE.search(seg)

    def rest(mark: str, expected: float) -> float:
        began = marks.get(mark)
        if began is None:
            return expected
        return max(expected - (now - began), OVERRUN_SHARE * expected)

    # Cost the current part and the parts after it
    made = make or 0
    heard = max(made, hear or 0)
    join = cost("join", lines * WORDS_PER_LINE)
    # only a job on the cluster, whose parts the app times, sends its audio to the app after the join
    finish = cost("finish", made) if timed else 0.0
    steps = None
    if "audio: " in seg:
        phase, detail, eta = "done", "audio written", rest("joined", finish)
    elif post:
        p = check_progress(seg[post.start():], log_mtime, now)
        phase, detail, eta = p["phase"], p["detail"], (p["eta"] or 0) + rest("joined", finish)
    elif "joining takes" in seg:
        # takes that were not checked before the join are heard after it. A render that made none hears none
        after = cost("check", heard) if heard and not pre else 0.0
        phase, detail, eta = "joining", "joining takes", rest("join", join) + after + finish
    elif pre:
        heard, part = int(pre.group(1)), seg[pre.start():]
        found = CHECK_RE.findall(part)
        again = RETAKE_RE.findall(part)
        if again:
            # the log's last line dates the round, which makes each flagged take again and hears it
            flagged = int(again[-1])
            whole = RETAKE_SECONDS + RETAKE_TRIES * cost("takes", flagged)
            spent = max(0.0, now - log_mtime) if log_mtime else 0.0
            left = max(whole - spent, OVERRUN_SHARE * whole)
            detail = f"taking {flagged:,} flagged take{'' if flagged == 1 else 's'} again"
        else:
            left = rest("check", cost("check", heard))
            if found:
                by_log = (int(found[-1][2]) - int(found[-1][1])) * float(found[-1][3])
                # a check that the app did not time counts down by the lines of its log alone
                left = max(left, by_log) if "check" in marks else by_log
            done = min(heard, int(found[-1][1])) if found else 0
            detail = f"checking the takes before the join, {done:,} of {heard:,}"
        phase, eta = "checking", left + join + finish
    elif head:
        found, first = TAKES_RE.findall(seg), FIRST_RE.search(seg)
        if found:
            done, total, per = int(found[-1][1]), int(found[-1][2]), int(found[-1][3]) / 1000
            # the log's rate is a mean that starts with the first take, which also warms the model up
            if first and done > 1:
                per = max(0.0, per * done - int(first.group(1))) / (done - 1) or per
            if per and log_mtime:
                done = min(total, done + int(max(0.0, now - log_mtime) / per))
            left, steps = (total - done) * per, [done, total]
            detail = f"take {done:,} of {total:,} at {per:.1f} s/take"
        elif "via ComfyUI" in seg:
            left, detail = rest("takes", made * COMFY_TAKE_SECONDS), "making takes in ComfyUI"
        elif first:
            left = rest("first", cost("takes", made - 1))
            detail = f"making {made:,} take{'' if made == 1 else 's'}"
        else:
            left, detail = rest("step", cost("setup")) + cost("takes", made - 1), "loading Breeze TTS 2"
        phase, eta = "generating", left + cost("check", heard) + join + finish
    elif make is None:
        phase, detail, eta = "starting", "", None
    else:
        # a render that makes and hears no take starts no breeze-server
        setup = cost("setup") if heard else min(cost("setup"), QUICK_SETUP_SECONDS)
        ahead = (cost("takes", make - 1) if make else 0.0) + (cost("check", heard) if heard else 0.0)
        phase, detail, eta = "starting", "", rest("step", setup) + ahead + join + finish

    # Share of the job that has passed
    began = marks.get("step") or started
    if phase == "done" and not eta:
        fraction = 1.0
    elif eta is not None and began:
        spent = max(0.0, now - began)
        fraction = spent / (spent + eta) if spent + eta > 0 else None
    else:
        fraction = steps[0] / steps[1] if steps and steps[1] else None
    out.update({"phase": phase, "fraction": fraction, "eta": eta, "detail": detail})
    if steps:
        out["steps"] = steps
    return out


def check_progress(text: str, log_mtime: float | None, now: float) -> dict:
    """Phase, fraction done and seconds left for a take check, from its log.

    text: the log, or its tail.
    log_mtime: when the log last changed, which dates its last progress line.
    now: the current time.
    Returns {"phase", "fraction", "eta", "detail"}. Fraction and eta may be None.
    """
    end = text.rfind("take check")
    if end >= 0:
        return {"phase": "done", "fraction": 1.0, "eta": 0, "detail": text[end:].splitlines()[0].strip()[:200]}
    head = CHECK_HEAD_RE.search(text)
    marks = CHECK_RE.findall(text)
    if not marks:
        total = int(head.group(1)) if head else None
        return {"phase": "checking", "fraction": None, "eta": total * CHECK_SECONDS_PER_TAKE if total else None,
                "detail": "loading the recognizer" if head else "starting"}
    _, done, total, per = marks[-1]
    done, total, per = int(done), int(total), float(per)
    if per and log_mtime:
        done = min(total, done + int(max(0.0, now - log_mtime) / per))
    return {"phase": "checking", "fraction": done / total if total else None, "eta": (total - done) * per,
            "detail": f"checked take {done:,} of {total:,} at {per:.1f} s/take"}


def cast_progress(text: str, candidates: int) -> dict:
    m = None
    for m in CASTING_RE.finditer(text):
        pass
    if "nothing to cast" in text:
        return {"phase": "done", "fraction": 1.0, "eta": 0, "detail": "nothing to cast"}
    if m is None:
        return {"phase": "briefs", "fraction": None, "eta": None, "detail": "casting pass"}
    count = int(m.group(1))
    rest = text[m.end():]
    matched = len(CANDIDATE_RE.findall(rest))
    designed = len(DESIGNED_RE.findall(rest))
    total = max(1, count * candidates)
    if designed:
        fraction = (designed + matched) / (2 * total)
        detail = f"checked {matched} of {total} candidates" if matched else f"designed {designed} of {total} candidates"
    else:
        fraction, detail = matched / total, f"{matched} of {total} candidates"
    return {"phase": "candidates", "fraction": min(1.0, fraction), "eta": None, "detail": f"{detail} ({m.group(2)})"}


# Activity outside the app

def _split(cmdline: str) -> list[str]:
    try:
        return [t.strip('"') for t in shlex.split(cmdline, posix=False)]
    except ValueError:
        return cmdline.split()


def _cli_args(cmdline: str) -> list[str] | None:
    tokens = _split(cmdline)
    for i, t in enumerate(tokens):
        if t == "serial_audio.cli":
            return tokens[i + 1:]
    return None


_script_cache: dict[str, tuple[float, bool]] = {}


def _script_runs_pipeline(cmdline: str) -> str | None:
    """The .ps1 path when a PowerShell command line runs a script that calls the pipeline, "inline command" when
    the command line calls it directly, else None."""
    tokens = _split(cmdline)
    lowered = [t.lower() for t in tokens]
    if "-command" in lowered and "serial_audio.cli" in cmdline:
        return "inline command"
    if "-file" not in lowered:
        return None
    i = lowered.index("-file")
    if i + 1 >= len(tokens):
        return None
    path = tokens[i + 1]
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _script_cache.get(path)
    if cached is None or cached[0] != mtime:
        try:
            body = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = ""
        cached = (mtime, "serial_audio.cli" in body)
        _script_cache[path] = cached
    return path if cached[1] else None


def external_activity(snapshot: dict[int, procs.Proc], ours: set[int], now: float) -> list[dict]:
    """Pipeline processes and scripts the app did not start, with render progress where a log shows it."""
    out = []
    for p in snapshot.values():
        if p.pid in ours or not p.cmdline:
            continue
        name = p.name.lower()
        if name.startswith("python"):
            args = _cli_args(p.cmdline)
            if not args:
                continue
            sub = args[0] if args else ""
            item = {"pid": p.pid, "kind": sub, "args": args, "heavy": sub in HEAVY, "started": p.started,
                    "slug": args[1] if len(args) > 1 else None, "source": "process"}
            if sub == "render" and len(args) > 2 and args[2].isdigit():
                n = int(args[2])
                item["chapter"] = n
                log = lib.chapter_dir(args[1], n) / "render.log"
                try:
                    mtime = log.stat().st_mtime
                except OSError:
                    mtime = None
                if mtime and (p.started is None or mtime >= p.started - 5):
                    item["progress"] = render_progress(read_tail(log), mtime, now, p.started)
                    item["log"] = str(log.relative_to(ROOT))
            out.append(item)
        elif name in ("pwsh.exe", "powershell.exe"):
            script = _script_runs_pipeline(p.cmdline)
            if script:
                out.append({"pid": p.pid, "kind": "script", "args": [], "heavy": True, "started": p.started,
                            "slug": None, "source": "script", "script": script})
    return out


def backlog_wait(job: dict, jobs) -> str | None:
    """Why a queued backlog job cannot start now, or None.

    job: the backlog job.
    jobs: every job the manager holds.
    A backlog job starts only when no job that is not backlog work is running or queued. It also waits for
    running backlog work, unless both jobs are of BACKLOG_BESIDE, which run beside each other, each on a GPU
    node of its own. It doesn't start beside one while a backlog job of another kind is queued ahead of it,
    which would otherwise wait for as long as such jobs keep coming. A job that waits between two stages on the
    cluster counts as running, and every backlog job waits for it.
    """
    jobs = list(jobs)
    beside = job.get("kind") in BACKLOG_BESIDE and not any(
        other is not job and other["status"] == "queued" and other.get("backlog")
        and other.get("kind") not in BACKLOG_BESIDE and other.get("seq", 0) < job.get("seq", 0) for other in jobs)
    for other in jobs:
        if other is job:
            continue
        staged = between_stages(other)
        if other["status"] == "running" or staged:
            if beside and not staged and other.get("backlog") and other.get("kind") in BACKLOG_BESIDE:
                continue
            return f"backlog work, waiting for {other['title']} to finish"
        if other["status"] == "queued" and not other.get("backlog"):
            return "backlog work, waiting for the queue to empty"
    return None


def between_stages(job: dict) -> bool:
    """Whether a queued job has run a stage on the cluster already and waits for its next one."""
    return job["status"] == "queued" and bool(job.get("nomad"))


def start_order(job: dict) -> tuple[bool, int]:
    """The order in which queued jobs are offered a start: backlog work that has not started last, then by seq."""
    return bool(job.get("backlog")) and not between_stages(job), job["seq"]


# The manager

class StateInUse(RuntimeError):
    pass


class JobManager:
    """The persistent job list and the thread that starts, watches and finishes jobs.

    Holds an exclusive lock on state_dir for its lifetime. Raises StateInUse when another manager holds it.
    """

    def __init__(self, comfy_url: str = COMFY_URL, state_dir: Path = STATE_DIR, autostart: bool = True):
        self.comfy_url = comfy_url.rstrip("/")
        self.state_dir = state_dir
        self.jobs_file = state_dir / "jobs.json"
        self.log_dir = state_dir / "jobs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = self._take_lock(state_dir / "lock")
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.paused = False
        self.wait_reasons: dict[str, str] = {}
        self.external: list[dict] = []
        self.comfy: dict = {"up": False, "checked": None}
        self.last_busy = time.time()
        self._last_comfy_poll = 0.0
        self._stop = threading.Event()
        self._load()
        self.thread = threading.Thread(target=self._loop, name="jobs", daemon=True)
        if autostart:
            self.thread.start()

    # Persistence

    @staticmethod
    def _take_lock(path: Path):
        fh = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise StateInUse(f"another web app instance is using {path.parent}") from None
        return fh

    def _load(self) -> None:
        data = None
        if self.jobs_file.exists():
            try:
                data = json.loads(self.jobs_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
        if data:
            self.paused = bool(data.get("paused"))
            for job in data.get("jobs", []):
                job.pop("stopping", None)
                job.setdefault("comfy", job.get("gpu", False))
                self.jobs[job["id"]] = job
        snap = procs.snapshot()
        for job in self.jobs.values():
            if job["status"] == "running":
                self._reconcile(job, snap)

    def _save(self) -> None:
        finished = [j for j in self.jobs.values() if j["status"] in FINISHED]
        for old in sorted(finished, key=lambda j: j["created"])[:-KEEP_FINISHED]:
            self.jobs.pop(old["id"], None)
        tmp = self.jobs_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"paused": self.paused, "jobs": list(self.jobs.values())}, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.jobs_file)

    # Requests

    def submit(self, requests: list[dict], stop_on_failure: bool = True,
               covered: list[dict] | None = None) -> list[dict]:
        """Queues the jobs in order as one batch. Validates them all first.

        requests: [{"kind", "params"}]. A request whose params hold "backlog": true is queued as backlog work,
            which starts only when backlog_wait lets it.
        stop_on_failure: when one job fails, skip the batch's queued jobs that can't go on. Here that is the rest
            of the batch, and on the cluster the jobs that wait for the failed one.
        covered: when given, receives the waiting job that stands in for each request that was not queued.
        A character pass or a screenplay that follows the import of its chapter in `requests` names that chapter in
        its meta before the chapter is on disk. A backlog render is not queued while a backlog render with the same
        arguments waits outside a batch and no other kind of backlog job is queued after that one. The render that
        waits stands in for it, and from then on counts as queued at the time of this request.
        Returns the new jobs. Raises ValueError and queues nothing if any request is bad.
        """
        built = []
        coming: dict[str, set[int]] = {}
        for r in requests:
            if not isinstance(r, dict):
                raise ValueError("each job must be an object with a kind and params")
            params = r.get("params") if r.get("params") is not None else {}
            b = self._build(r.get("kind"), params, coming)
            if r.get("kind") == "fetch":
                coming.setdefault(b["slug"], set()).update(b["meta"].get("chapters") or [])
            built.append((r.get("kind"), params, b))
        batch = uuid.uuid4().hex[:8]
        out = []
        with self.lock:
            seq = max((j.get("seq", 0) for j in self.jobs.values()), default=0)
            for kind, params, b in built:
                backlog = isinstance(params, dict) and params.get("backlog") is True
                waiting = self._stands_in(kind, b["args"]) if backlog else None
                if waiting:
                    waiting["asked"] = now_iso()
                    if covered is not None:
                        covered.append(waiting)
                    continue
                seq += 1
                jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
                job = {"id": jid, "kind": kind, "params": params, "args": b["args"], "heavy": b["heavy"],
                       "gpu": b["gpu"], "comfy": b["comfy"], "title": b["title"], "slug": b["slug"], "meta": b["meta"],
                       "batch": batch if len(built) > 1 and stop_on_failure else None, "seq": seq,
                       "backlog": backlog,
                       "status": "queued", "created": now_iso(), "started": None, "finished": None,
                       "exit_code": None, "pid": None, "note": None}
                self.jobs[jid] = job
                out.append(job)
            self._save()
        return out

    def _build(self, kind, params, coming) -> dict:
        """The job fields for one request, as build returns them. Raises ValueError on a bad request."""
        return build(kind, params, self.comfy_url, coming)

    def _stands_in(self, kind: str, args: list[str]) -> dict | None:
        """Finds the waiting job that a new backlog render would only repeat.

        kind: the new job's kind.
        args: the new job's CLI arguments.
        Returns the last queued backlog render with these arguments that is not part of a batch, or None when
        there is none or another kind of backlog job is queued after it.
        """
        if kind != "render":
            return None
        same = [j for j in self.jobs.values() if j["kind"] == "render" and j.get("backlog") and j["args"] == args
                and j["status"] == "queued" and not between_stages(j) and not j.get("batch")]
        if not same:
            return None
        last = max(same, key=lambda j: j["seq"])
        if any(j.get("backlog") and j["kind"] != "render" and j["status"] not in FINISHED and j["seq"] > last["seq"]
               for j in self.jobs.values()):
            return None
        return last

    def cancel(self, jid: str) -> dict:
        """Cancels a queued job, or stops a running one and cancels its ComfyUI prompts. Returns the job."""
        with self.lock:
            job = self.jobs.get(jid)
            if job is None:
                raise KeyError(jid)
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["finished"] = now_iso()
                self._save()
                return job
            if job["status"] != "running" or job.get("stopping"):
                return job
            job["stopping"] = True
            pid = job.get("pid")
        if pid:
            procs.kill_tree(pid)
        cancelled = self._cancel_comfy_prompts(job) if job.get("comfy") else []
        with self.lock:
            job.pop("stopping", None)
            job["status"] = "stopped"
            job["finished"] = now_iso()
            job["note"] = "stopped from the web app" + (f"; cancelled ComfyUI prompt(s) {', '.join(cancelled)}"
                                                        if cancelled else "")
            self._append_log(job, f"\n[web] {job['note']}\n")
            self._skip_batch(job, "an earlier job in the batch was stopped")
            self._save()
        return job

    def move(self, jid: str, direction: str) -> None:
        self.reorder([jid], direction)

    def reorder(self, ids: list[str], to: str, before: str | None = None) -> None:
        """Moves queued jobs within the queue.

        ids: the jobs to move. They stay in the order the queue has them in.
        to: "top", "bottom", "up" or "down" by one place, or "before".
        before: with "before", the queued job the moved ones go ahead of.
        Returns nothing. The queue is in start_order, so a job moves only among the jobs of its own half: backlog
        work that has not started, or the rest. A job stays behind every job of its batch that it was queued
        behind and that _keeps_ahead names. A job of ids that started meanwhile is passed over. Raises KeyError
        when none of ids is queued or `before` is not, and ValueError on another to.
        """
        if to not in REORDER:
            raise ValueError(f"cannot move jobs {to!r}: use one of {', '.join(REORDER)}")
        with self.lock:
            # Find the jobs
            queue = sorted((j for j in self.jobs.values() if j["status"] == "queued"), key=start_order)
            by_id = {j["id"]: j for j in queue}
            moving, order = {jid for jid in ids if jid in by_id}, []
            if not moving:
                raise KeyError(", ".join(map(str, ids)))
            if to == "before" and before not in by_id:
                raise KeyError(before)

            # Place them in their half of the queue
            for late in (False, True):
                half = [j for j in queue if start_order(j)[0] == late]
                mine = [j for j in half if j["id"] in moving]
                rest = [j for j in half if j["id"] not in moving]
                if not mine:
                    order += half
                    continue
                if to == "top":
                    at = 0
                elif to == "bottom":
                    at = len(rest)
                elif to == "up":
                    at = max(0, half.index(mine[0]) - 1)
                elif to == "down":
                    at = min(len(rest), half.index(mine[-1]) - len(mine) + 2)
                elif by_id[before] in rest:
                    at = rest.index(by_id[before])
                else:
                    # the job to go ahead of is in the other half, or is one of the moved jobs
                    at = (half.index(by_id[before]) - sum(1 for j in mine if half.index(j) < half.index(by_id[before]))
                          if by_id[before] in half else 0 if late else len(rest))
                order += rest[:at] + mine + rest[at:]

            # A job goes no higher than the jobs of its batch that it has to follow
            was = {j["id"]: i for i, j in enumerate(queue)}
            for _ in range(len(order)):
                moved = False
                for i, job in enumerate(order):
                    last = max((k for k, other in enumerate(order) if k > i and job.get("batch")
                                and other.get("batch") == job["batch"] and was[other["id"]] < was[job["id"]]
                                and self._keeps_ahead(other, job)), default=None)
                    if last is not None:
                        order.insert(last, order.pop(i))
                        moved = True
                        break
                if not moved:
                    break

            # Deal the queue's numbers out again in the new order
            for job, seq in zip(order, sorted(j["seq"] for j in queue)):
                job["seq"] = seq
            self._save()

    def _keeps_ahead(self, first: dict, job: dict) -> bool:
        """Whether job has to stay behind first, a job of its batch that was queued ahead of it."""
        return True

    def cancel_queued(self, ids: list[str]) -> int:
        """Cancels the jobs of ids that are still queued, as cancel does one.

        ids: job ids. A job that started meanwhile keeps running, and an unknown id is passed over.
        Returns how many were cancelled.
        """
        with self.lock:
            done = 0
            for jid in ids:
                job = self.jobs.get(jid)
                if job and job["status"] == "queued":
                    job.update({"status": "cancelled", "finished": now_iso()})
                    done += 1
            if done:
                self._save()
        return done

    def retry(self, jid: str) -> dict:
        with self.lock:
            job = self.jobs.get(jid)
            if job is None:
                raise KeyError(jid)
            covered: list[dict] = []
            new = self.submit([{"kind": job["kind"], "params": job["params"]}], covered=covered)
            return (new or covered)[0]

    def clear_finished(self) -> int:
        with self.lock:
            gone = [jid for jid, j in self.jobs.items() if j["status"] in FINISHED]
            for jid in gone:
                log = self.log_path(self.jobs.pop(jid))
                for path in (log, log.with_suffix(".exit")):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass  # a lost job's process may still hold its log open
            self._save()
        return len(gone)

    def set_paused(self, paused: bool) -> None:
        with self.lock:
            self.paused = paused
            self._save()

    # Reading

    def log_path(self, job: dict) -> Path:
        return self.log_dir / f"{job['id']}.log"

    def read_log(self, jid: str, offset: int = 0, limit: int = 262_144) -> dict:
        with self.lock:
            job = self.jobs.get(jid)
            if job is None:
                raise KeyError(jid)
            path, status = self.log_path(job), job["status"]
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                if offset < 0:
                    offset = max(0, size + offset)
                fh.seek(min(offset, size))
                data = fh.read(limit)
        except OSError:
            data, size = b"", 0
        return {"text": data.replace(b"\x00", b"").decode("utf-8", "replace"), "offset": offset + len(data),
                "size": size, "finished": status in FINISHED}

    def progress(self, job: dict, now: float | None = None) -> dict | None:
        now = now or time.time()
        if job["status"] != "running":
            return None
        path = self.log_path(job)
        text = read_tail(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        started = _ts(job.get("started"))
        if job["kind"] == "render":
            return render_progress(text, mtime, now, started, **self._render_timing(job))
        if job["kind"] == "check-takes":
            return check_progress(text, mtime, now)
        if job["kind"] == "cast" and not job["meta"].get("dry_run"):
            return cast_progress(text, job["meta"].get("candidates") or 3)
        last = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip() and not ln.startswith("$")), "")
        return {"phase": "running", "fraction": None, "eta": None, "detail": last[:200]}

    def _render_timing(self, job: dict) -> dict:
        """What render_progress takes for a job beyond its log: its marks, cost and make. Nothing on one machine."""
        return {}

    def minutes_per_line(self) -> float:
        rates = []
        for j in sorted(self.jobs.values(), key=lambda j: j.get("finished") or "", reverse=True):
            if j["kind"] == "render" and j["status"] == "done" and j["meta"].get("lines") and not j["meta"].get("decode_only"):
                start, end = _ts(j.get("started")), _ts(j.get("finished"))
                if start and end and end > start:
                    rates.append((end - start) / 60 / j["meta"]["lines"])
            if len(rates) == 3:
                break
        return sum(rates) / len(rates) if rates else DEFAULT_MIN_PER_LINE

    def snapshot(self) -> dict:
        """Jobs, activity outside the app and ComfyUI's state, for the API."""
        now = time.time()
        with self.lock:
            rate = self.minutes_per_line()
            running = sorted((j for j in self.jobs.values() if j["status"] == "running"), key=lambda j: j["started"] or "")
            queued = sorted((j for j in self.jobs.values() if j["status"] == "queued"), key=start_order)
            finished = sorted((j for j in self.jobs.values() if j["status"] in FINISHED),
                              key=lambda j: j.get("finished") or j["created"], reverse=True)
            jobs = []
            for j in running + queued + finished:
                item = dict(j)
                item["progress"] = self.progress(j, now)
                item["wait"] = self.wait_reasons.get(j["id"]) if j["status"] == "queued" else None
                if j["kind"] == "render" and j["status"] == "queued" and j["meta"].get("lines"):
                    item["estimate"] = (DECODE_SECONDS if j["meta"].get("decode_only")
                                        else j["meta"]["lines"] * rate * 60 + DECODE_SECONDS)
                jobs.append(item)
            return {"jobs": jobs, "external": self.external, "comfy": self.comfy, "paused": self.paused,
                    "minutes_per_line": round(rate, 3), "now": now}

    # The loop

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                print(f"[jobs] tick failed: {e!r}", file=sys.stderr)
            self._stop.wait(TICK_SECONDS)

    def start(self) -> None:
        if not self.thread.is_alive():
            self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        now = time.time()
        snap = procs.snapshot()
        if now - self._last_comfy_poll >= COMFY_POLL_SECONDS:
            self._last_comfy_poll = now
            self.comfy = self._poll_comfy()
        with self.lock:
            for job in [j for j in self.jobs.values() if j["status"] == "running"]:
                self._reconcile(job, snap)
            ours = {os.getpid()}
            for job in self.jobs.values():
                if job["status"] == "running" and job.get("pid"):
                    ours |= procs.descendants(snap, job["pid"])
            self.external = external_activity(snap, ours, now)
            running = [j for j in self.jobs.values() if j["status"] == "running"]
            ours_on_gpu = any(j["gpu"] for j in running)
            comfy_busy = bool(self.comfy.get("running") or self.comfy.get("pending")) and not ours_on_gpu
            ext_heavy = [e for e in self.external if e["heavy"]]
            if ext_heavy or comfy_busy:
                self.last_busy = now
            self.wait_reasons = {}
            queued = sorted((j for j in self.jobs.values() if j["status"] == "queued"), key=start_order)
            changed = False
            ahead: dict[bool, dict] = {}
            for job in queued:
                first = ahead.get(job["heavy"])
                held = backlog_wait(job, self.jobs.values()) if job.get("backlog") else None
                reason = held or (f"queued behind {first['title']}" if first else
                                  self._blocker(job, running, ext_heavy, comfy_busy, now))
                if reason:
                    self.wait_reasons[job["id"]] = reason
                    ahead.setdefault(job["heavy"], job)
                    continue
                stale = self._already_done(job)
                if stale:
                    job.update({"status": "skipped", "finished": now_iso(), "note": stale})
                    changed = True
                    continue
                self._launch(job)
                if job["status"] == "running":
                    running.append(job)
                changed = True
            if changed:
                self._save()

    def _blocker(self, job: dict, running: list[dict], ext_heavy: list[dict], comfy_busy: bool,
                 now: float) -> str | None:
        """Why the queued job cannot start now, or None."""
        if job["heavy"]:
            if self.paused:
                return "the queue is on hold"
            if running:
                return f"waiting for {running[0]['title']}"
            if ext_heavy:
                e = ext_heavy[0]
                what = (f"{e['kind']} chapter {e['chapter']}" if e.get("chapter") else
                        e["kind"] if e["kind"] != "script" else f"script {Path(e['script']).name}")
                return f"waiting for an outside {what} (pid {e['pid']})"
            if comfy_busy:
                return "waiting for ComfyUI to finish a prompt the app did not queue"
            if job.get("comfy") and not self.comfy.get("up"):
                return "waiting for ComfyUI at " + self.comfy_url
            settle = SETTLE_SECONDS - (now - self.last_busy)
            if settle > 0:
                return f"checking the machine is idle ({settle:.0f} s)"
            return None
        if any(not j["heavy"] for j in running):
            return "waiting for the quick job ahead of it"
        writer = next((j for j in running if j["kind"] in BIBLE_WRITERS), None)
        if writer:
            return f"waiting for {writer['title']}, which rewrites the bible"
        if any(e["kind"] in BIBLE_WRITERS for e in self.external):
            return "waiting for an outside job that rewrites the bible"
        if job["kind"] in SERIAL_WRITERS:
            same = next((j for j in running if j.get("slug") == job.get("slug")), None)
            if same:
                return f"waiting for {same['title']}, which reads serial.yaml"
        return None

    def _nobody_to_cast(self, job: dict) -> str | None:
        """Why a queued cast has nothing to do, or None.

        job: a cast job.
        Returns a note when the cast names no characters, is not a dry run, and no character of the serial needs a
        voice as casting.characters_needing_voices counts them. None for any other cast, and when the serial or its
        bible can't be read. Never raises.
        """
        meta = job.get("meta")
        if not isinstance(meta, dict) or meta.get("characters") or meta.get("dry_run"):
            return None
        try:
            from ..casting import characters_needing_voices
            if characters_needing_voices(lib.load_serial(job["slug"]), lib.load_bible(job["slug"])):
                return None
        except Exception:
            return None
        return "nobody needs a voice, so there is nothing to cast"

    def _already_done(self, job: dict) -> str | None:
        """Why a queued render or cast no longer needs to run, or None.

        A cast is done when _nobody_to_cast says so.
        A render is stale when its chapter was rendered after the job was queued, by a run the app did not start.
        A backlog render is also stale when a render with the same arguments started after it was queued, no job
        that changes what a render reads has run since that render started, the chapter's last render is Breeze,
        and a render would make, take again, hear and join nothing. A job that stands in for a later request
        counts as queued at that request.
        """
        if job["kind"] == "cast":
            return self._nobody_to_cast(job)
        if job["kind"] != "render" or job["meta"].get("decode_only"):
            return None
        n = job["meta"]["chapters"][0]
        twin = self._repeats(job) if job.get("backlog") else None
        if twin:
            return f"{twin['title']} started at {twin['started']}, after this job was queued, and left it nothing to do"
        meta = lib.load_yaml(lib.chapter_dir(job["slug"], n) / "chapter.yaml", {}) or {}
        rendered = _ts((meta.get("render") or {}).get("at"))
        queued = _ts(job.get("asked") or job["created"])
        if not (rendered and queued and rendered > queued):
            return None
        for other in self.jobs.values():
            if (other is not job and other["kind"] == "render" and other["slug"] == job["slug"]
                    and other["meta"].get("chapters") == [n] and other["status"] == "done"
                    and (_ts(other.get("finished")) or 0) >= queued - 1):
                return None
        return f"chapter {n} was rendered outside the app at {meta['render']['at']}, after this job was queued"

    def _repeats(self, job: dict) -> dict | None:
        """The finished render that left a queued backlog render nothing to do, or None."""
        queued = _ts(job.get("asked") or job["created"])
        twins = [j for j in self.jobs.values()
                 if j is not job and j["kind"] == "render" and j["status"] == "done" and j["args"] == job["args"]
                 and queued and (_ts(j.get("started")) or 0) >= queued]
        if not twins:
            return None
        twin = max(twins, key=lambda j: _ts(j["started"]))
        began = _ts(twin["started"])
        for other in self.jobs.values():
            if other["kind"] in RENDER_NEUTRAL or not other.get("started"):
                continue
            ended = _ts(other.get("finished"))
            if other["status"] not in FINISHED or not ended or ended >= began:
                return None
        try:
            from ..breeze import render_changes
            args = job["args"]
            seed = int(args[args.index("--seed") + 1]) if "--seed" in args else None
            changes = render_changes(job["slug"], job["meta"]["chapters"][0], seed, job["meta"].get("runner"))
            idle = changes["engine"] == "breeze" and not any(changes[k] for k in ("make", "retake", "unheard", "join"))
        except Exception:
            return None
        return twin if idle else None

    def latest_log(self, kind: str, slug: str, n: int) -> tuple[Path, float] | None:
        """The newest log and its mtime of the app's jobs of `kind` for chapter n, or None."""
        best = None
        with self.lock:
            for j in self.jobs.values():
                if j["kind"] == kind and j["slug"] == slug and n in (j["meta"].get("chapters") or []) and j.get("started"):
                    path = self.log_path(j)
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    if best is None or mtime > best[1]:
                        best = (path, mtime)
        return best

    def _launch(self, job: dict) -> None:
        log = self.log_path(job)
        exit_file = log.with_suffix(".exit")
        argv = [sys.executable, "-u", "-m", "serial_audio.web.runjob", str(exit_file), str(log), "--",
                sys.executable, "-u", "-m", "serial_audio.cli", *job["args"]]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
        base = CREATE_NEW_PROCESS_GROUP | procs.CREATE_NO_WINDOW
        kwargs = dict(cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, close_fds=True)
        try:
            try:
                proc = subprocess.Popen(argv, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            except OSError:
                proc = subprocess.Popen(argv, creationflags=base, **kwargs)
        except OSError as e:
            job.update({"status": "failed", "finished": now_iso(), "note": f"could not start: {e}"})
            self._skip_batch(job, "an earlier job in the batch failed")
            return
        job.update({"status": "running", "pid": proc.pid, "started": now_iso(), "note": None})

    def _reconcile(self, job: dict, snap: dict[int, procs.Proc]) -> None:
        """Finishes a running job whose wrapper has exited."""
        if job.get("stopping"):
            return
        exit_file = self.log_path(job).with_suffix(".exit")
        proc = snap.get(job.get("pid") or -1)
        if proc is not None and (str(exit_file) in proc.cmdline
                                 or (not proc.cmdline and proc.name.lower().startswith("python"))):
            return
        if exit_file.exists():
            try:
                info = json.loads(exit_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = {"code": None}
            job["exit_code"] = info.get("code")
            job["finished"] = info.get("at") or now_iso()
            job["status"] = "done" if info.get("code") == 0 else "failed"
        elif snap:
            job["status"] = "lost"
            job["finished"] = now_iso()
            job["note"] = "the job's process ended without recording an exit code"
        else:
            return
        if job["status"] != "done":
            self._skip_batch(job, f"an earlier job in the batch {job['status']}")
        self._save()

    def _skip_batch(self, job: dict, why: str) -> None:
        if not job.get("batch"):
            return
        for other in self.jobs.values():
            if other["batch"] == job["batch"] and other["status"] == "queued":
                other["status"] = "skipped"
                other["finished"] = now_iso()
                other["note"] = why

    def _append_log(self, job: dict, text: str) -> None:
        try:
            with self.log_path(job).open("a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass

    # ComfyUI

    def _get(self, path: str, timeout: float = 3.0):
        with urllib.request.urlopen(self.comfy_url + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _poll_comfy(self) -> dict:
        try:
            stats = self._get("/system_stats")
            jobs = self._get("/api/jobs?status=in_progress,pending&limit=50")
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"up": False, "checked": time.time(), "error": str(getattr(e, "reason", e))}
        system = stats.get("system") or {}
        device = (stats.get("devices") or [{}])[0]
        items = jobs.get("jobs") or []
        running = [j for j in items if j.get("status") == "in_progress"]
        return {"up": True, "checked": time.time(), "version": system.get("comfyui_version"),
                "ram_free": system.get("ram_free"), "ram_total": system.get("ram_total"),
                "vram_free": device.get("vram_free"), "vram_total": device.get("vram_total"),
                "running": len(running), "pending": len(items) - len(running),
                "running_since": (running[0].get("create_time") or 0) / 1000 if running else None}

    def _cancel_comfy_prompts(self, job: dict) -> list[str]:
        """Cancels the running or pending ComfyUI prompts that belong to the job's render or cast. Returns their ids."""
        slug = job.get("slug") or ""
        if job["kind"] == "render":
            marker = f"{slug}/ch{int(job['meta']['chapters'][0]):04d}"
        elif job["kind"] == "cast":
            marker = f"cast/{slug}/"
        elif job["kind"] == "check-takes":
            marker = f"breeze/{slug}/ch{int(job['meta']['chapters'][0]):04d}/"
        else:
            return []
        try:
            queue = self._get("/queue", timeout=10)
        except (urllib.error.URLError, OSError, ValueError):
            return []
        ids = []
        for item in (queue.get("queue_running") or []) + (queue.get("queue_pending") or []):
            if len(item) > 2 and marker in json.dumps(item[2]):
                ids.append(item[1])
        done = []
        for pid in ids:
            req = urllib.request.Request(f"{self.comfy_url}/api/jobs/{pid}/cancel", data=b"{}",
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if json.loads(resp.read().decode("utf-8") or "{}").get("cancelled"):
                        done.append(pid)
            except (urllib.error.URLError, OSError, ValueError):
                pass
        return done
