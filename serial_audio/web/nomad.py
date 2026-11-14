"""Runs the web app's jobs on the Nomad cluster.

NomadJobManager keeps JobManager's queue, batches, records and progress parsing, and changes where a job runs. Jobs
that need a GPU, ComfyUI or Claude are dispatched as Nomad jobs (deploy/nomad/serial-audio.nomad.hcl) to a node
whose client meta offers their class. Quick library jobs (fetch, add a serial, reset the bible) run on the app's
own machine. A worker fetches its job's bundle from the app, streams its log back and returns the files it changed,
which the app writes into the library unless they changed there meanwhile.

Jobs on different chapters, serials or nodes run at the same time. Jobs on the same chapter wait for each other,
and conflicts() says which job waits for a job that rewrites a serial's bible or voices. A node runs one GPU job at
a time, and LLM_SLOTS Claude jobs run at once.

The app's media mirror holds a copy of every take and cast candidate a worker sent it, so a join, a take check and
a render that reuses takes run on any breeze node, and picking a voice runs on the app's machine. A chapter whose
takes the mirror lacks still needs the node that made them.
"""
from __future__ import annotations

import heapq
import hmac
import json
import math
import os
import re
import secrets
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import library as lib
from .. import machine
from ..breeze import render_changes, reusable_takes
from . import exchange
from .jobs import (BACKLOG_BESIDE, CHECK_HEAD_RE, LOG_MARKS, OVERRUN_SHARE, PRECHECK_RE, QUICK_SETUP_SECONDS,
                   RENDER_COST, RENDER_HEAD_RE, ROOT, TAKES_HEAD_RE, WORDS_PER_LINE, JobManager, _slug, _ts,
                   backlog_wait, between_stages, now_iso, read_tail, start_order)

NAMESPACE = "serial-audio"
PREFIX = "serial-audio"
CLASSES = ("breeze", "comfyui", "llm")
GPU_CLASSES = {"breeze", "comfyui"}
LLM_KINDS = {"character-pass", "screenplay", "bios"}
# a job with no CLI step: its worker sends the app every take and candidate its node holds for the serial
SYNC_KIND = "sync-media"
SERIAL_WIDE = {"character-pass", "cast", "bios", "reset-bible", "pick-voice", "fetch", "say-as", "voice-effect",
               "say-options"}
# serial-wide jobs that write only voices/, the bible and serial.yaml, which a chapter job took in its bundle
CONFIG_WRITERS = {"cast", "bios", "pick-voice", "say-as", "voice-effect", "say-options"}
# jobs that write only their chapters' files
CHAPTER_KINDS = {"render", "screenplay", "check-takes"}
# pairs of serial-wide jobs that share no file: a fetch writes chapter files, the index and serial.yaml, and bios
# writes bible/characters.yaml
NO_SHARED_FILE = {frozenset({"bios", "fetch"})}
# serial-wide jobs that change nothing a chapter job reads: the options of a term are not its say-as
NO_CHAPTER_INPUT = {"say-options"}
# serial-wide jobs that change nothing a screenplay reads: a script names who speaks, with a voice or without
NO_SCRIPT_INPUT = {"cast", "bios", "pick-voice", "voice-effect"}
# chapter jobs that read the voices a cast files
VOICE_READERS = {"render", "check-takes"}
# seconds per Breeze take by GPU type, until a node's own render records give its rate
RATE_DEFAULT = {"nvidia": 3.0, "metal": 18.0, "none": 25.0}
RATE_MIN_TAKES = 20
RATE_KEEP = 3
TAKES_CACHE_SECONDS = 600.0
STALE_LOG_SECONDS = 600.0
JOIN_ESTIMATE = 90.0
CHECK_PER_TAKE = 0.5
# seconds per take to fetch a chapter's takes from the app and send them back from the new node
MOVE_PER_TAKE = 0.05
# a cast candidate is about nine seconds of audio, two takes' worth
CAST_TAKES = 2
# about seven options, each said in pronounce.SCREEN_TAKES lines
OPTION_TAKES = 56
DEFAULT_LINES = 200
# a render that makes under half of its chapter's lines is a touch-up, and its takes run longer than a chapter's
TOUCH_RATIO = 1.25
# seconds the check before the join takes in a render of a whole chapter, as (fixed, per take heard)
WHOLE_CHECK = (20.0, 0.9)
# seconds from a dispatch to the job's step on the node, without any wait for the machine
PRE_STEP = 6.0
# how many timings of one kind of work a node keeps, and the fewest that scale the default
TIMING_KEEP = 12
TIMING_MIN = 3
# seconds a stage takes on a Claude node, as (fixed, per word of its chapters), until its own jobs say
LLM_COST = {"screenplay": (40.0, 0.028), "character-pass": (30.0, 0.01)}
LLM_STAGE = 45.0
OTHER_STAGE = 120.0
LOCAL_SECONDS = 5.0
# the takes of queued renders counted in one pass, outside the lock, at about half a second each
WORK_PER_TICK = 2
# the seconds between two forecasts of the queue, for each second one takes
OUTLOOK_REST = 20.0
# the bytes before a new piece of a worker's log that are read with it, so a line cut in two is still found
MARK_OVERLAP = 64
WAIT_MARGIN = 120.0
LLM_SLOTS = 3
NODE_POLL = 15.0
JOB_POLL = 5.0
END_GRACE = 30.0
STOP_GRACE = 90.0
TICK_SECONDS = 2.0
TAIL_BYTES = 6000
DISPATCH_RETRY = 15.0
# whether a worker that finishes a job of HAND_CLASSES is handed the job its node would get next
HAND_NEXT = True
HAND_CLASSES = ("breeze",)
PLACEMENT_FILE = "placement.json"
WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


class NomadError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


class WorkerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class NomadClient:
    """The few Nomad HTTP API calls the app makes, in one namespace."""

    def __init__(self, addr: str, token: str | None, namespace: str = NAMESPACE, timeout: float = 15):
        self.addr, self.token, self.namespace, self.timeout = addr.rstrip("/"), token, namespace, timeout

    def request(self, method: str, path: str, body: dict | None = None, query: dict | None = None, raw: bool = False):
        """The decoded JSON answer, or the bytes when raw. Raises NomadError with the HTTP status, or None when
        Nomad is unreachable."""
        url = f"{self.addr}{path}" + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Nomad-Token"] = self.token
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            raise NomadError(e.code, f"Nomad answered {e.code} to {method} {path}: "
                                     f"{e.read()[:300].decode('utf-8', 'replace').strip()}") from None
        except (urllib.error.URLError, OSError) as e:
            raise NomadError(None, f"Nomad at {self.addr} is not reachable: {getattr(e, 'reason', e)}") from None
        if raw:
            return payload
        try:
            return json.loads(payload.decode("utf-8") or "null")
        except ValueError:
            raise NomadError(None, f"Nomad sent something that is not JSON for {path}") from None

    def _job(self, job_id: str) -> str:
        return "/v1/job/" + urllib.parse.quote(job_id, safe="")

    def dispatch(self, job_id: str, meta: dict, token: str) -> dict:
        return self.request("POST", self._job(job_id) + "/dispatch", {"Meta": meta, "IdempotencyToken": token},
                            {"namespace": self.namespace})

    def allocations(self, job_id: str) -> list[dict]:
        return self.request("GET", self._job(job_id) + "/allocations", query={"namespace": self.namespace}) or []

    def evaluations(self, job_id: str) -> list[dict]:
        return self.request("GET", self._job(job_id) + "/evaluations", query={"namespace": self.namespace}) or []

    def nodes(self) -> list[dict]:
        return self.request("GET", "/v1/nodes") or []

    def node(self, node_id: str) -> dict:
        return self.request("GET", f"/v1/node/{urllib.parse.quote(node_id, safe='')}")

    def task_log(self, alloc_id: str, kind: str = "stderr", tail: int = TAIL_BYTES) -> str:
        data = self.request("GET", f"/v1/client/fs/logs/{urllib.parse.quote(alloc_id, safe='')}",
                            query={"task": "work", "type": kind, "origin": "end", "offset": str(tail), "plain": "true",
                                   "namespace": self.namespace}, raw=True)
        return data.decode("utf-8", "replace")


def worker_summary(node: dict) -> dict:
    """What the app shows and schedules on for one Nomad node."""
    meta = node.get("Meta") or {}
    classes = [c.strip() for c in str(meta.get("serial_audio") or "").split(",") if c.strip()]
    status = node.get("Status")
    eligible = node.get("SchedulingEligibility") == "eligible" and not node.get("Drain")
    busy = str(meta.get("busy", "false")).lower() != "false"
    return {"name": node.get("Name"), "id": node.get("ID"), "status": status, "eligible": eligible, "busy": busy,
            "classes": classes, "gpu": meta.get("gpu"), "ready": status == "ready" and eligible and not busy,
            "os": (node.get("Attributes") or {}).get("kernel.name")}


def _why_not(n: dict) -> str:
    if n["status"] != "ready":
        return f"{n['name']} is {n['status']}"
    if not n["eligible"]:
        return f"{n['name']} is not taking jobs"
    return f"{n['name']} is busy"


def placement_wait(evals: list[dict]) -> str | None:
    """Why Nomad has not placed a dispatched job yet, from its evaluations, or None."""
    for ev in sorted(evals, key=lambda e: e.get("CreateIndex", 0), reverse=True):
        failed = ev.get("FailedTGAllocs") or {}
        metrics = failed.get("work") or next(iter(failed.values()), None)
        if not metrics:
            continue
        parts = []
        exhausted = metrics.get("DimensionExhausted") or {}
        if any("port" in k for k in exhausted):
            parts.append("every matching node is running another GPU job")
        elif exhausted:
            parts.append("matching nodes lack " + ", ".join(sorted(exhausted)))
        filtered = metrics.get("ConstraintFiltered") or {}
        if filtered and not parts:
            parts.append(f"{sum(filtered.values())} node(s) do not offer this class or are busy")
        if not parts:
            parts.append(f"{metrics.get('NodesEvaluated', 0)} node(s) evaluated, none fit")
        return "Nomad has no node for it yet: " + "; ".join(parts)
    return None


def _mins(seconds: float) -> int:
    return max(1, round(seconds / 60))


def _overhead(lines: int) -> float:
    """Seconds a render that is not costed part by part spends after its takes, joining and checking them."""
    return JOIN_ESTIMATE + CHECK_PER_TAKE * lines


def expected(samples, units: float, fixed: float, per: float) -> float:
    """Seconds a piece of work is expected to take on a node.

    samples: the node's recent [units, seconds] timings of the same kind of work, as placement.json holds them.
    units: the size of the work to estimate.
    fixed, per: the default line, fixed + per * units, which stands until TIMING_MIN samples exist.
    Returns the estimate, never below zero: the default line scaled by the samples' median ratio to it, so one
    job that ran long or short doesn't move it. Never raises, whatever samples is: what is not a timing of two
    finite numbers is passed over.
    """
    base = fixed + per * units
    ratios = [p[1] / (fixed + per * p[0]) for p in (samples if isinstance(samples, list) else [])
              if isinstance(p, (list, tuple)) and len(p) == 2
              and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in p)
              and p[0] >= 0 and p[1] >= 0 and fixed + per * p[0] > 0]
    return base * statistics.median(ratios) if len(ratios) >= TIMING_MIN else base


def _is_import(job: dict) -> bool:
    """Whether a job imports one saved chapter page. It writes only that chapter's files, on the app's machine."""
    return job["kind"] == "fetch" and "--html" in (job.get("args") or [])


def _chapters(job: dict) -> set[int]:
    return set((job.get("meta") or {}).get("chapters") or [])


def _share_chapter(a: set[int], b: set[int]) -> bool:
    """Whether two jobs with these chapters may work on one chapter. A job that names none may work on any."""
    return not a or not b or bool(a & b)


def _touches(job: dict) -> tuple[str | None, set[int] | None]:
    """The serial a job works on and its chapters, or None for chapters when it affects the whole serial."""
    if job["kind"] in SERIAL_WIDE and not _is_import(job):
        return job.get("slug"), None
    return job.get("slug"), _chapters(job)


def _reads_takes(job: dict) -> bool:
    """Whether a job reads its chapter's saved Breeze takes: a take check, and a Breeze render or join."""
    breeze = (job.get("meta") or {}).get("engine") == "breeze"
    return job["kind"] == "check-takes" or (job["kind"] == "render" and breeze)


def _starts_beside(job: dict) -> bool:
    """Whether a job can start beside a running chapter job of its serial."""
    return job["kind"] in CONFIG_WRITERS or job["kind"] == "character-pass" or _is_import(job)


def conflicts(job: dict, other: dict) -> bool:
    """Whether job must wait for other.

    job: the job that would start, a record with kind, slug and meta.
    other: a job that runs or was queued ahead of it.
    Returns True when both work on one serial and either changes the whole serial or they share a chapter, with
    these exceptions. A SYNC_KIND job writes no library file, so it and any other job never wait for each other.
    A CONFIG_WRITERS job never waits for a CHAPTER_KINDS job. The two jobs of a NO_SHARED_FILE
    pair never wait for each other. The import of a saved page changes only its chapter, and it and a
    CONFIG_WRITERS job never wait for each other. A character pass and a CHAPTER_KINDS job or an import wait for
    each other only on a chapter that both work on, and one of them that names no chapter counts as working on
    every chapter. A screenplay never waits for a NO_SCRIPT_INPUT job. A VOICE_READERS job doesn't wait for a cast
    whose meta names a from_chapter after every chapter of the job. Raises KeyError on a record without kind.
    """
    slug, chapters = _touches(job)
    slug_other, chapters_other = _touches(other)
    if not slug or slug != slug_other:
        return False
    if SYNC_KIND in (job["kind"], other["kind"]):
        return False
    if job["kind"] in CONFIG_WRITERS and other["kind"] in CHAPTER_KINDS:
        return False
    if job["kind"] in CHAPTER_KINDS and other["kind"] in NO_CHAPTER_INPUT:
        return False
    if job["kind"] == "screenplay" and other["kind"] in NO_SCRIPT_INPUT:
        return False
    if job["kind"] in VOICE_READERS and other["kind"] == "cast":
        meta = other.get("meta")
        first = meta.get("from_chapter") if isinstance(meta, dict) else None
        if isinstance(first, int) and chapters and max(chapters) < first:
            return False
    if frozenset({job["kind"], other["kind"]}) in NO_SHARED_FILE:
        return False
    if (_is_import(job) and other["kind"] in CONFIG_WRITERS) or (job["kind"] in CONFIG_WRITERS and _is_import(other)):
        return False
    if job["kind"] == "character-pass" and (other["kind"] in CHAPTER_KINDS or _is_import(other)):
        return _share_chapter(_chapters(job), chapters_other)
    if other["kind"] == "character-pass" and (job["kind"] in CHAPTER_KINDS or _is_import(job)):
        return _share_chapter(chapters, _chapters(other))
    return chapters is None or chapters_other is None or bool(chapters & chapters_other)


def _cast_units(meta: dict) -> int:
    """The candidates a cast is costed at: its candidates for each character it names. A cast for everyone counts
    as three characters, and one that follows a chapter's character pass as one."""
    who = len(meta.get("characters") or []) or (1 if meta.get("from_chapter") else 3)
    return (meta.get("candidates") or 3) * who


def _int_flag(args: list, flag: str) -> int | None:
    """The integer that follows `flag` in args, or None when there is none."""
    if flag in args and args.index(flag) + 1 < len(args):
        try:
            return int(args[args.index(flag) + 1])
        except (TypeError, ValueError):
            return None
    return None


def _cast_engine(job: dict) -> str | None:
    """The voice design engine a cast job asks for: its --engine flag, else the serial's cast_engine, else breeze.

    An unknown cast_engine comes back as written, so the CLI refuses it before the casting pass. None when the
    serial can't be read.
    """
    args = list(job.get("args") or [])
    if "--engine" in args and args.index("--engine") + 1 < len(args):
        return args[args.index("--engine") + 1]
    from ..casting import plan_cast_engine
    from ..comfy import ComfyError
    try:
        serial = lib.load_serial(job.get("slug") or "")
    except Exception:
        return None
    try:
        return plan_cast_engine(serial)
    except ComfyError:
        return str(serial.get("cast_engine"))


def job_stages(job: dict) -> list[dict] | None:
    """The Nomad dispatches a job runs as, in order, or None when it runs on the app's machine.

    A cast writes its casting briefs on a Claude node first and then designs the voices on a GPU node, which finds
    the briefs cached and calls no LLM. Only a Qwen cast needs a ComfyUI node. The options of a term are proposed on
    a Claude node and then said on a breeze node, and a job that only says options, again or with one spelling
    added, is the breeze stage alone. A take check runs on any breeze node, and picking a voice runs on the app's
    machine, from the media mirror.
    """
    kind, args = job["kind"], list(job["args"])
    if kind in LLM_KINDS:
        return [{"class": "llm", "args": args}]
    if kind in ("check-takes", SYNC_KIND):
        return [{"class": "breeze", "args": args}]
    if kind == "cast":
        if "--dry-run" in args:
            return [{"class": "llm", "args": args}]
        engine = _cast_engine(job)
        flag = [] if engine is None or "--engine" in args else ["--engine", engine]
        voices = [a for a in args if a != "--redo-brief"] + flag
        return [{"class": "llm", "args": args + flag + ["--dry-run"], "label": "casting briefs"},
                {"class": "comfyui" if engine == "qwen" else "breeze", "args": voices, "label": "voices"}]
    if kind == "say-options":
        cut = args.index("--") if "--" in args else len(args)
        if any(a == "--render-only" or a.startswith("--add=") for a in args[:cut]):
            return [{"class": "breeze", "args": args}]
        return [{"class": "llm", "args": args[:cut] + ["--dry-run"] + args[cut:], "label": "respellings"},
                {"class": "breeze", "args": args[:cut] + ["--render-only"] + args[cut:], "label": "takes"}]
    if kind == "render":
        return [{"class": "comfyui" if job.get("comfy") else "breeze", "args": args}]
    return None


class NomadJobManager(JobManager):
    """JobManager that dispatches heavy jobs to the Nomad cluster. Worker requests arrive through worker_*()."""

    def __init__(self, nomad: NomadClient, prefix: str = PREFIX, **kwargs):
        self.nomad = nomad
        self.prefix = prefix
        self.workers: list[dict] = []
        self.nomad_state: dict = {"up": None, "error": None, "checked": None, "addr": nomad.addr}
        self._node_cache: dict[str, tuple[int, dict]] = {}
        self._last_nodes = 0.0
        self._local: dict[str, subprocess.Popen] = {}
        self._io_lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._picked: dict[str, tuple[str, str]] = {}
        self._plan: dict[str, float] = {}
        self._takes_cache: dict[str, tuple[float, int]] = {}
        self._mirror_cache: dict[tuple[str, int], tuple[tuple, bool]] = {}
        self._work: dict[str, tuple[float, int, int]] = {}
        self._work_marks = 0
        self._words_cache: dict[tuple[str, int], tuple[float, int]] = {}
        self._outlook: dict[str, dict] = {}
        self._outlook_at = 0.0
        self._outlook_cost = 0.0
        self._handing: dict | None = None
        super().__init__(**kwargs)
        self.media_dir = self.state_dir / "media"
        self.placement_file = self.state_dir / PLACEMENT_FILE
        self.placement = self._load_placement()

    # Routing

    @staticmethod
    def is_nomad(job: dict) -> bool:
        return bool(job.get("nomad")) or job_stages(job) is not None

    def stage(self, job: dict) -> dict | None:
        n = job.get("nomad")
        if n:
            return n["stages"][n["stage"]]
        stages = job_stages(job)
        return stages[0] if stages else None

    def lane(self, job: dict) -> str:
        st = self.stage(job)
        if st:
            return st["class"]
        return "local-heavy" if job["heavy"] else "local"

    def _keeps_ahead(self, first: dict, job: dict) -> bool:
        return conflicts(job, first)

    def _build(self, kind, params, coming) -> dict:
        if kind != SYNC_KIND:
            return super()._build(kind, params, coming)
        if not isinstance(params, dict):
            raise ValueError("each job needs a kind and a params object")
        slug = _slug(params)
        node = str(params.get("node") or "")
        nodes = sorted(w["name"] for w in self.workers if GPU_CLASSES & set(w["classes"]))
        if node not in nodes:
            raise ValueError(f"{SYNC_KIND} needs a GPU node: {', '.join(nodes) or 'none is known yet'}")
        return {"args": [], "heavy": True, "gpu": False, "comfy": False, "title": f"Send audio to the app · {node}",
                "slug": slug, "meta": {"node": node}}

    # Persistence

    def _load(self) -> None:
        super()._load()
        for job in self.jobs.values():
            n = job.get("nomad")
            if job["status"] == "running" and n and not n.get("dispatched"):
                n["dispatching"] = True

    def _reconcile(self, job: dict, snap) -> None:
        if self.is_nomad(job):
            return
        self._reconcile_local(job, snap)

    def _load_placement(self) -> dict:
        try:
            data = json.loads(self.placement_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _record_placement(self, job: dict, node: str | None, report: dict, success: bool) -> None:
        """Remembers the node that ran a chapter's render or take check as the node that holds its takes."""
        slug = job.get("slug")
        if not node or not slug:
            return
        touched = set(report.get("applied") or []) | set(report.get("unchanged") or [])
        chapters = self.placement.setdefault("chapters", {}).setdefault(slug, {})
        for n in (job.get("meta") or {}).get("chapters") or []:
            takes = f"chapters/{int(n):04d}/breeze_takes.json"
            if job["kind"] in ("render", "check-takes") and (success or takes in touched):
                chapters[str(n)] = node
            if success and job["kind"] == "render" and f"chapters/{int(n):04d}/chapter.yaml" in touched:
                self._record_rate(slug, int(n), node)
        self._takes_cache.clear()
        self._mirror_cache.clear()
        tmp = self.placement_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.placement, indent=1), encoding="utf-8")
        os.replace(tmp, self.placement_file)

    def _node_for_paths(self, paths: list[str]) -> str | None:
        """The one serial_audio node whose OS writes paths like these, or None when that is not one node."""
        for p in paths:
            kind = ("windows" if WINDOWS_PATH_RE.match(p) else "darwin" if p.startswith("/Users/")
                    else "linux" if p.startswith("/") else None)
            if kind:
                names = [w["name"] for w in self.workers if w.get("os") == kind]
                return names[0] if len(names) == 1 else None
        return None

    def holder(self, job: dict) -> tuple[str | None, bool]:
        """The node that holds the takes a job works on, or None, and whether the job must run there.

        A Breeze join and a take check must run there while the media mirror lacks a take of the chapter. With
        every take in the mirror they run on any node, and a render reuses its takes on any node. The holder is
        then only the node that need not fetch them. A Drama 3 decode reads no takes, so no node holds it. A
        SYNC_KIND job must run on the node it names.
        """
        slug, kind, meta = job.get("slug"), job["kind"], job.get("meta") or {}
        if kind in ("render", "check-takes") and meta.get("chapters"):
            n = int(meta["chapters"][0])
            node = ((self.placement.get("chapters") or {}).get(slug) or {}).get(str(n))
            if not node:
                node = self._node_for_paths(list(exchange.take_files(lib.chapter_dir(slug, n)).values())[:5])
            needs_takes = kind == "check-takes" or (bool(meta.get("decode_only")) and _reads_takes(job))
            return node, needs_takes and not self._mirrored(slug, n)
        if kind == SYNC_KIND:
            return meta.get("node"), True
        return None, False

    def _mirrored(self, slug: str | None, n: int) -> bool:
        """Whether the media mirror holds a file for every take the chapter's takes file names.

        slug, n: the serial and the chapter.
        Returns True for a chapter without a takes file. Cached until the takes file changes or a worker sends
        the mirror a file.
        """
        if not slug:
            return True
        folder = lib.chapter_dir(slug, n)
        try:
            st = (folder / exchange.TAKES_FILE).stat()
        except OSError:
            return True
        stamp = (st.st_mtime_ns, st.st_size)
        hit = self._mirror_cache.get((slug, n))
        if hit and hit[0] == stamp:
            return hit[1]
        rels = [exchange.mirror_rel(raw) for raw in exchange.take_files(folder).values()]
        held = all(rel and (self.media_dir / rel).is_file() for rel in rels)
        self._mirror_cache[(slug, n)] = (stamp, held)
        return held

    def _save(self) -> None:
        with self._io_lock:
            super()._save()

    # Requests

    def cancel(self, jid: str) -> dict:
        """Cancels a queued job, asks a running worker to stop, or stops a local job. Returns the job."""
        with self.lock:
            job = self.jobs.get(jid)
            if job is None:
                raise KeyError(jid)
            if job["status"] != "running" or not self.is_nomad(job):
                local = job["status"] == "running"
            else:
                n = job["nomad"]
                if not n.get("contact"):
                    job.update({"status": "cancelled", "finished": now_iso(),
                                "note": "stopped before a node started it" + (
                                    f"; the worker on {n.get('target')} gets no bundle for it" if n.get("follows")
                                    else f"; Nomad job {n['dispatched']} exits as soon as a node starts it"
                                    if n.get("dispatched") else "")})
                    self._append_log(job, f"\n[web] {job['note']}\n")
                    self._skip_batch(job, "an earlier job in the batch was stopped")
                elif not n.get("cancel"):
                    n["cancel"] = True
                    n["stop_at"] = time.time()
                    job["stopping"] = True
                    self._append_log(job, "\n[web] stop requested; the worker stops within a few seconds\n")
                    self._skip_batch(job, "an earlier job in the batch was stopped")
                self._save()
                return job
        if not local:
            return super().cancel(jid)
        if os.name == "nt":
            return super().cancel(jid)
        with self.lock:
            pid = job.get("pid")
            if pid:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except OSError:
                    pass
            proc = self._local.pop(jid, None)
            if proc is not None:
                threading.Thread(target=proc.wait, name="reap", daemon=True).start()
            job.update({"status": "stopped", "finished": now_iso(), "note": "stopped from the web app"})
            self._append_log(job, f"\n[web] {job['note']}\n")
            self._skip_batch(job, "an earlier job in the batch was stopped")
            self._save()
        return job

    # Reading

    def progress(self, job: dict, now: float | None = None) -> dict | None:
        if job["status"] != "running" or not job.get("nomad"):
            return super().progress(job, now)
        n = job["nomad"]
        if job.get("stopping") or n.get("cancel"):
            return {"phase": "stopping", "fraction": None, "eta": None, "detail": "stopping", "node": n.get("node")}
        if not n.get("contact"):
            node = n.get("node") or n.get("target")
            where = (n.get("wait") or ("dispatching to Nomad" if n.get("dispatching") else
                     f"starting on {node}" if node else "waiting for Nomad to place it"))
            return {"phase": "waiting", "fraction": None, "eta": None, "detail": where, "node": node}
        now = now or time.time()
        p = super().progress(job, now) or {"phase": "running", "fraction": None, "eta": None, "detail": ""}
        if n.get("wait"):
            p["detail"] = n["wait"]
        elif p.get("phase") in ("starting", "running", "briefs"):
            last = next((ln.strip() for ln in reversed(read_tail(self.log_path(job), 4000).splitlines()) if ln.strip()), "")
            if last.startswith("[worker] waiting:"):
                p.update({"phase": "waiting", "detail": last[len("[worker] "):], "eta": None, "fraction": None})
        st = n["stages"][n["stage"]]
        if p.get("phase") != "waiting":
            self._fill_eta(job, p, now)
        label = (f"{st.get('label') or 'stage'} ({n['stage'] + 1} of {len(n['stages'])})"
                 if len(n["stages"]) > 1 else "")
        p["detail"] = ", ".join(x for x in (n.get("node"), label, p.get("detail")) if x)
        p["node"] = n.get("node")
        return p

    def _fill_eta(self, job: dict, p: dict, now: float) -> None:
        """Gives a running cluster job's progress the seconds left to the end of the job.

        job: the running job.
        p: its progress, changed in place. An eta the log gave is kept, and one that is missing is the stage's
            expected length less the time since its step began. The stages still to come are added to both, and
            "ahead" then holds their share of the eta.
        now: the current time.
        Returns nothing. Leaves eta as None for a Breeze render whose takes nobody has counted yet.
        """
        n = job["nomad"]
        st, node = n["stages"][n["stage"]], n.get("node") or n.get("target") or ""
        if p.get("eta") is None and p.get("phase") != "done":
            if st["class"] != "llm" and self._is_breeze(job) and self._known_takes(job) is None:
                return
            began = (n.get("marks") or {}).get("step")
            # the way from the dispatch to the step is behind a step that has begun
            total = self._stage_seconds(job, st, node) - (PRE_STEP if began and st["class"] == "llm" else 0.0)
            spent = max(0.0, now - began) if began else 0.0
            p["eta"] = max(total - spent, OVERRUN_SHARE * total)
            if p.get("fraction") is None and began:
                p["fraction"] = spent / (spent + p["eta"])
        ahead = sum(self._stage_best(job, later) for later in n["stages"][n["stage"] + 1:])
        if ahead and p.get("eta") is not None:
            began = _ts(job.get("started"))
            p["eta"] += ahead
            p["ahead"] = ahead
            p["fraction"] = (now - began) / (now - began + p["eta"]) if began and now > began else None

    def _render_timing(self, job: dict) -> dict:
        n = job.get("nomad") or {}
        if not n or not self._is_breeze(job):
            return {}
        node = n.get("node") or n.get("target") or ""
        make = self._known_takes(job)
        return {"marks": n.get("marks") or {}, "cost": self._cost(job, node), "make": make,
                "hear": None if make is None else make + self._known_unheard(job)}

    def minutes_per_line(self) -> float:
        """Minutes a line takes in a Breeze render of a whole chapter, on the breeze node that is fastest at it."""
        nodes = [w["name"] for w in self.workers if "breeze" in w["classes"]]
        if not nodes:
            return super().minutes_per_line()
        probe = {"kind": "render", "meta": {"engine": "breeze", "lines": DEFAULT_LINES}}
        return min(self._render_seconds(probe, node, DEFAULT_LINES) for node in nodes) / 60 / DEFAULT_LINES

    def snapshot(self) -> dict:
        snap = super().snapshot()
        with self.lock:
            age = max(0.0, snap["now"] - self._outlook_at)
            outlook = {jid: dict(o) for jid, o in self._outlook.items()}
        ends = []
        for item in snap["jobs"]:
            if item.get("nomad"):
                item["nomad"] = {k: v for k, v in item["nomad"].items() if k not in ("key", "marks")}
            if item["status"] not in ("queued", "running"):
                continue
            item.pop("estimate", None)
            o = outlook.get(item["id"]) or {}
            for key in ("estimate", "on"):
                if o.get(key) is not None:
                    item[key] = o[key]
            for key in ("start_in", "end_in"):
                if o.get(key) is not None:
                    item[key] = max(0.0, o[key] - age)
            ends.append(item.get("end_in"))
        snap.update({"executor": "nomad", "workers": self.workers, "nomad": self.nomad_state,
                     "queue_end_in": max(ends) if ends and None not in ends else None})
        return snap

    # The loop

    def tick(self) -> None:
        now = time.time()
        if now - self._last_nodes >= NODE_POLL:
            self._last_nodes = now
            self._poll_nodes()
        self._poll_jobs(now)
        dispatch = []
        with self.lock:
            for job in [j for j in self.jobs.values() if j["status"] == "running"]:
                if self.is_nomad(job):
                    self._check_stop(job, now)
                    n = job.get("nomad") or {}
                    if (n.get("dispatching") and job["id"] not in self._in_flight
                            and now >= n.get("retry_at", 0)):
                        dispatch.append(job["id"])
                else:
                    self._reconcile_local(job, None)
                    if job["status"] != "running":
                        self._stale_work(job.get("slug"))
            changed = self._schedule(now, dispatch)
            if changed:
                self._save()
        for jid in dispatch:
            self._dispatch(jid)
        self._poll_work(now)
        # a pass over a long queue holds the lock for a while, so the passes take a twentieth of the time at most
        if now - self._outlook_at >= OUTLOOK_REST * self._outlook_cost:
            with self.lock:
                began = time.time()
                # a forecast that fails leaves no old times behind
                self._outlook = {}
                self._outlook, self._outlook_at = self._forecast(now), now
                self._outlook_cost = time.time() - began

    def _poll_work(self, now: float) -> None:
        """Counts the takes that queued Breeze renders would make and hear, for the queue's estimates.

        now: the current time.
        Returns nothing. Counts WORK_PER_TICK renders a call, in queue order: the ones without a count or marked
        for another count first, then the ones whose count is older than TAKES_CACHE_SECONDS. Reads the library
        outside the lock. A chapter that can't be planned counts as all its lines to make. A count that was
        marked for another count while it was read stays marked. Forgets the counts of jobs that are no longer
        queued or running.
        """
        with self.lock:
            for jid in [k for k in self._work if (self.jobs.get(k) or {}).get("status") not in ("queued", "running")]:
                del self._work[jid]
            todo, marked = [], self._work_marks
            for j in self.jobs.values():
                meta = j.get("meta") or {}
                if (j["status"] != "queued" or not self._is_breeze(j) or meta.get("decode_only")
                        or not meta.get("chapters")):
                    continue
                at = self._work.get(j["id"], (None, 0, 0))[0]
                if at is None or now - at > TAKES_CACHE_SECONDS:
                    todo.append((at or 0.0, start_order(j), j["id"], j["slug"], int(meta["chapters"][0]),
                                 _int_flag(list(j.get("args") or []), "--seed"), meta.get("runner"),
                                 meta.get("lines") or DEFAULT_LINES))
        for _, _, jid, slug, number, seed, runner, lines in sorted(todo)[:WORK_PER_TICK]:
            try:
                changes = render_changes(slug, number, seed, runner, stored=True)
                needed, unheard = changes["make"] + changes["retake"], changes["unheard"]
            except Exception:
                needed, unheard = lines, 0
            with self.lock:
                self._work[jid] = (time.time() if marked == self._work_marks else 0.0, needed, unheard)

    def _stale_work(self, slug: str | None) -> None:
        """Marks the counts of a serial's queued renders for counting again. They stand until then."""
        self._work_marks += 1
        for jid, (_, needed, unheard) in list(self._work.items()):
            if (self.jobs.get(jid) or {}).get("slug") == slug:
                self._work[jid] = (0.0, needed, unheard)

    def _poll_nodes(self) -> None:
        try:
            stubs = self.nomad.nodes()
            out = []
            for stub in stubs:
                cached = self._node_cache.get(stub["ID"])
                if cached is None or cached[0] != stub.get("ModifyIndex"):
                    cached = (stub.get("ModifyIndex"), self.nomad.node(stub["ID"]))
                    self._node_cache[stub["ID"]] = cached
                full = {**cached[1], "Status": stub.get("Status"), "Drain": stub.get("Drain"),
                        "SchedulingEligibility": stub.get("SchedulingEligibility")}
                summary = worker_summary(full)
                if summary["classes"]:
                    out.append(summary)
            self.workers = sorted(out, key=lambda n: n["name"] or "")
            self.nomad_state.update({"up": True, "error": None, "checked": time.time()})
        except NomadError as e:
            self.nomad_state.update({"up": False, "error": str(e), "checked": time.time()})

    def _poll_jobs(self, now: float) -> None:
        with self.lock:
            todo = [(j["id"], j["nomad"]["dispatched"], bool(j["nomad"].get("ended_seen")))
                    for j in self.jobs.values()
                    if j["status"] == "running" and j.get("nomad") and j["nomad"].get("dispatched")
                    and now - j["nomad"].get("polled", 0) >= JOB_POLL]
        for jid, dispatched, ended in todo:
            seen: dict = {"polled": now}
            try:
                allocs = self.nomad.allocations(dispatched)
                seen["allocs"] = allocs
                if not allocs:
                    seen["evals"] = self.nomad.evaluations(dispatched)
                elif ended:
                    alloc = max(allocs, key=lambda a: a.get("CreateTime", 0))
                    try:
                        seen["tail"] = self.nomad.task_log(alloc["ID"])
                    except NomadError:
                        seen["tail"] = ""
            except NomadError as e:
                seen["error"] = e
            with self.lock:
                job = self.jobs.get(jid)
                if job and job["status"] == "running" and job.get("nomad", {}).get("dispatched") == dispatched:
                    self._apply_poll(job, seen, now)

    def _apply_poll(self, job: dict, seen: dict, now: float) -> None:
        n = job["nomad"]
        n["polled"] = seen["polled"]
        err = seen.get("error")
        if err is not None:
            if err.status == 404 and n.get("follows") and not n.get("contact"):
                self._take_back(job, f"Nomad no longer knows the task that was handed this job, {n['dispatched']}")
            elif err.status == 404:
                self._finish(job, "lost", f"Nomad no longer knows job {n['dispatched']}")
            return
        allocs = seen.get("allocs") or []
        if not allocs:
            n["wait"] = placement_wait(seen.get("evals") or [])
            return
        alloc = max(allocs, key=lambda a: a.get("CreateTime", 0))
        n.update({"alloc": alloc["ID"], "node": alloc.get("NodeName") or n.get("node"),
                  "client_status": alloc.get("ClientStatus")})
        status = alloc.get("ClientStatus")
        if status == "unknown":
            n["wait"] = f"{n['node']} is disconnected; the job carries on if it reconnects within 2 hours"
            return
        n["wait"] = None
        if status not in ("complete", "failed", "lost"):
            return
        if n.get("follows") and not n.get("contact"):
            self._take_back(job, f"the task on {n['node']} that was handed this job ended before it took it")
            return
        if not n.get("ended_seen"):
            n["ended_seen"] = now
            n["polled"] = 0
            return
        if now - n["ended_seen"] < END_GRACE or "tail" not in seen:
            return
        events = [e for t in (alloc.get("TaskStates") or {}).values() for e in (t.get("Events") or [])]
        last = next((e.get("DisplayMessage") or e.get("Type") for e in reversed(events)
                     if e.get("Type") not in ("Received", "Task Setup", "Started")), "")
        tail = seen.get("tail") or ""
        if tail.strip():
            self._append_log(job, "\n[web] the end of the task's stderr in Nomad:\n" + tail.rstrip() + "\n")
        self._finish(job, "lost" if status == "lost" else "failed",
                     f"the Nomad allocation on {n['node']} ended {status} without a result" + (f": {last}" if last else ""))

    def _check_stop(self, job: dict, now: float) -> None:
        n = job["nomad"]
        if n.get("cancel") and now - n.get("stop_at", now) > STOP_GRACE:
            self._finish(job, "stopped", f"the worker did not confirm the stop; check Nomad job {n.get('dispatched')}")

    def _finish(self, job: dict, status: str, note: str, code: int | None = None) -> None:
        job.update({"status": status, "finished": now_iso(), "note": note, "exit_code": code})
        job.pop("stopping", None)
        self._takes_cache.clear()
        self._mirror_cache.clear()
        self._append_log(job, f"\n[web] {note}\n")
        if status != "done":
            self._skip_batch(job, f"an earlier job in the batch {status}")
        self._save()

    def _skip_batch(self, job: dict, why: str) -> None:
        """Skips the queued jobs of a batch that can't go on after job ended early.

        job: the batch job that failed, was lost, was cancelled, or was stopped or asked to stop.
        why: the note each skipped job gets.
        Returns nothing. A stop, a stop request or a cancel skips every queued job of the batch. A failure or a
        loss skips only the queued jobs that wait for the job, or for a job skipped with it, so the batch's
        other chapters still run. A character pass is not skipped for a cast.
        """
        if not job.get("batch") or job["status"] not in ("failed", "lost"):
            super()._skip_batch(job, why)
            return
        gone = [job]
        queued = sorted((j for j in self.jobs.values() if j.get("batch") == job["batch"] and j["status"] == "queued"),
                        key=lambda j: j["seq"])
        for other in queued:
            # the job ran, so every queued job that conflicts with it waited for it, whatever their order.
            # A character pass waits for a cast only to write the bible after it, and reads nothing the cast files
            if any(conflicts(other, g) and (g is job or g["seq"] < other["seq"])
                   and not (other["kind"] == "character-pass" and g["kind"] == "cast") for g in gone):
                other.update({"status": "skipped", "finished": now_iso(), "note": why})
                gone.append(other)

    # Scheduling

    def _schedule(self, now: float, dispatch: list[str]) -> bool:
        running = [j for j in self.jobs.values() if j["status"] == "running"]
        queued = sorted((j for j in self.jobs.values() if j["status"] == "queued"), key=start_order)
        self.wait_reasons = {}
        self._plan = {}
        blocked: dict[str, dict] = {}
        changed = False
        for job in queued:
            lane = self.lane(job)
            first = blocked.get(lane)
            held = (backlog_wait(job, self.jobs.values()) if job.get("backlog") and not between_stages(job)
                    else None)
            if held:
                reason = held
            elif first and lane not in CLASSES and (not _starts_beside(job) or conflicts(job, first)):
                reason = f"queued behind {first['title']}"
            else:
                reason = self._blocker_for(job, lane, running)
            if reason:
                self.wait_reasons[job["id"]] = reason
                blocked.setdefault(lane, job)
                continue
            if not job.get("nomad"):
                stale = self._already_done(job)
                if stale:
                    job.update({"status": "skipped", "finished": now_iso(), "note": stale})
                    changed = True
                    continue
            if lane in CLASSES:
                node, cls = self._picked.pop(job["id"], (None, None))
                self._mark_dispatch(job, node, cls)
                if not self._handed(job, node):
                    dispatch.append(job["id"])
            else:
                self._launch_local(job)
            if job["status"] == "running":
                running.append(job)
            changed = True
        return changed

    def _handed(self, job: dict, node: str | None) -> bool:
        """Hands a job that a pass just marked for dispatch to the worker the pass runs for, if it is that worker's.

        job: the job, marked by _mark_dispatch.
        node: the node the pass picked for it, or None.
        Returns whether the job is now that worker's. It is when the pass runs for a worker (_hand_next), the
        worker has no job from this pass yet, and the job goes to the worker's node as the class its task was
        dispatched for. The job then counts as dispatched under the worker's Nomad job, so the app follows it by
        that task and doesn't dispatch a Nomad job for it.
        """
        handing = self._handing
        if (not handing or handing.get("got") or not node or node != handing["node"]
                or self.lane(job) != handing["class"]):
            return False
        job["nomad"].update({"dispatching": False, "dispatched": handing["dispatched"], "dispatched_at": now_iso(),
                             "follows": handing["from"]})
        handing["got"] = job["id"]
        self._append_log(job, f"[web] handed to the worker on {node} as it finished {handing['title']}, so it runs "
                              f"in that task, {handing['dispatched']}, and what the task has loaded stays up\n")
        return True

    def _take_back(self, job: dict, why: str) -> None:
        """Queues a job again that was handed to a worker which never fetched its bundle.

        job: the job, running and handed to a worker by _handed.
        why: what happened to the worker's task, for the job's log.
        """
        n = job["nomad"]
        if n["stage"] == 0 and not n.get("reports"):
            job.pop("nomad")
        else:
            n.update({"dispatched": None, "alloc": None, "follows": None, "dispatching": False,
                      "client_status": None, "ended_seen": None, "wait": None})
        job.update({"status": "queued", "note": None})
        self._append_log(job, f"[web] {why}, so the job is queued again\n")
        self._save()

    def _hand_next(self, job: dict, node: str | None, cls: str, dispatched: str | None, code: str,
                   report: dict) -> None:
        """Hands the worker that just finished a stage of job the queued job its node would get next.

        job: the job whose result the worker just sent, already finished or queued for its next stage.
        node: the worker's node.
        cls: the class the worker's task was dispatched for.
        dispatched: the id of the worker's Nomad job.
        code: the version of the code the worker runs.
        report: the report the worker gets, which receives the id of the handed job under "next".
        Doesn't hand a job when HAND_NEXT is off, when the class is not of HAND_CLASSES, or when the worker's code
        is not the app's. Else runs a scheduling pass now, and the first job it would dispatch to the node as that
        class becomes the worker's. The pass marks every other job it would start, and the next tick dispatches those.
        Never raises.
        """
        if not HAND_NEXT or not node or not dispatched or cls not in HAND_CLASSES:
            return
        handing = {"node": node, "class": cls, "dispatched": dispatched, "from": job["id"], "title": job["title"],
                   "got": None}
        try:
            if code != exchange.code_version():
                return
            self._handing = handing
            self._schedule(time.time(), [])
        except Exception as e:
            print(f"[jobs] no next job handed after {job.get('id')}: {e!r}", file=sys.stderr)
        finally:
            self._handing = None
            if handing["got"]:
                report["next"] = handing["got"]
            self._save()

    def _with_next(self, jid: str, report: dict) -> dict:
        """A stage's report as its worker gets it.

        jid: the job the report is for.
        report: the stored report, which names the job handed to the worker under "next", by id.
        Returns the report with "next" as {"job", "key"} while that job still waits for this worker, and without
        "next" otherwise.
        """
        handed = report.get("next")
        other = self.jobs.get(handed) if isinstance(handed, str) else None
        n = (other or {}).get("nomad") or {}
        if not other or other["status"] != "running" or n.get("follows") != jid or n.get("contact"):
            return {k: v for k, v in report.items() if k != "next"}
        return {**report, "next": {"job": handed, "key": n["key"]}}

    def _blocker_for(self, job: dict, lane: str, running: list[dict]) -> str | None:
        """Why the queued job cannot start now, or None."""
        if self.paused and (lane in CLASSES or job["heavy"]):
            return "the queue is on hold"
        # a job that is not backlog work never waits for backlog work that has not started, which waits for it
        earlier = next((j for j in self.jobs.values() if j["status"] == "queued" and j["seq"] < job["seq"]
                        and conflicts(job, j)
                        and (job.get("backlog") or not j.get("backlog") or between_stages(j))), None)
        if earlier:
            return f"waiting for {earlier['title']}, queued ahead of it on the same serial"
        for other in running:
            if conflicts(job, other):
                wide = ((_touches(other)[1] is None or _touches(job)[1] is None)
                        and not _chapters(job) & _chapters(other))
                return f"waiting for {other['title']}" + (", which changes the whole serial" if wide else
                                                          ", which works on the same chapter")
            if not job.get("slug") or other.get("slug") != job["slug"]:
                continue
            n = other.get("nomad")
            # a chapter job reads the serial's files when it builds its bundle, not when it is dispatched
            if _starts_beside(job) and job["kind"] not in NO_CHAPTER_INPUT and not (n or {}).get("contact"):
                return f"waiting for {other['title']}" + (" to fetch its bundle" if n else "")
            # an import writes its files in place, so a job that builds a bundle starts after it
            if lane in CLASSES and _is_import(other):
                return f"waiting for {other['title']}"
        if lane not in CLASSES:
            return None
        if not self.nomad.token:
            return "the app has no Nomad dispatch token (see deploy/README.md)"
        if self.nomad_state["up"] is False:
            return f"waiting for Nomad: {self.nomad_state['error']}"
        if self.nomad_state["up"] is None:
            return "checking which Nomad nodes can take it"
        if lane in GPU_CLASSES:
            node, cls, reason = self._pick_node(job, lane, running)
            if reason:
                return reason
            self._picked[job["id"]] = (node, cls)
            return None
        offering = [n for n in self.workers if lane in n["classes"]]
        if not offering:
            return f"no Nomad node offers {lane} jobs yet (client meta serial_audio, see deploy/README.md)"
        ready = [n for n in offering if n["ready"]]
        if not ready:
            return f"no {lane} node is ready: " + ", ".join(_why_not(n) for n in offering)
        mine = [j for j in running if j.get("nomad") and self.lane(j) == lane]
        if len(mine) >= LLM_SLOTS:
            return f"waiting for a running Claude job to finish, {LLM_SLOTS} run at once"
        return None

    def _pick_node(self, job: dict, lane: str, running: list[dict]) -> tuple[str | None, str | None, str | None]:
        """The node and class a GPU job runs on, as (node, class, None), or (None, None, why it waits).

        A node runs one GPU job at a time. A job that needs a node's files, because the media mirror has no copy
        of them, gets that node. Any other job goes to the ready node where its estimated finish is earliest,
        counting the node's running job and the queued jobs already waiting for it, and waits for a busy node when
        that beats every free one by WAIT_MARGIN. A job that waits adds its estimate to the node's timeline for
        the rest of the pass.
        """
        busy = {j["nomad"].get("target") or j["nomad"].get("node") for j in running
                if j.get("nomad") and self.lane(j) in GPU_CLASSES}
        holder, required = self.holder(job)
        if required and holder:
            w = next((w for w in self.workers if w["name"] == holder), None)
            need = f"{holder}, which holds the files this job needs"
            if w is None:
                return None, None, f"waiting for {need}, to offer serial_audio jobs"
            if not w["ready"]:
                return None, None, f"waiting for {need}: {_why_not(w)}"
            if holder in busy:
                self._plan[holder] = self._plan.get(holder, 0.0) + self._duration(job, holder, holder)
                return None, None, f"waiting for {need} and is running another GPU job"
            if lane not in w["classes"]:
                return None, None, f"{need}, does not offer {lane} jobs"
            return holder, lane, None
        offering = [w for w in self.workers if lane in w["classes"]]
        if not offering:
            return None, None, f"no Nomad node offers {lane} jobs yet (client meta serial_audio, see deploy/README.md)"
        ready = [w for w in offering if w["ready"]]
        if not ready:
            return None, None, f"no {lane} node is ready: " + ", ".join(_why_not(w) for w in offering)
        free = {w["name"]: self._free_at(w["name"], running) for w in ready}
        est = {w["name"]: free[w["name"]] + self._duration(job, w["name"], holder) for w in ready}
        order = [w["name"] for w in sorted(ready, key=lambda w: (est[w["name"]], w.get("gpu") != "nvidia", w["name"]))]
        best = order[0]
        if best not in busy:
            return best, lane, None
        idle = [name for name in order if name not in busy]
        if idle and est[best] + WAIT_MARGIN > est[idle[0]]:
            return idle[0], lane, None
        self._plan[best] = self._plan.get(best, 0.0) + self._duration(job, best, holder)
        if idle:
            return None, None, (f"waiting for {best}, about {_mins(est[best])} min, instead of about "
                                f"{_mins(est[idle[0]])} min on {idle[0]} now")
        others = ", ".join(sorted(name for name in order if name in busy and name != best))
        return None, None, (f"waiting for {best}, about {_mins(est[best])} min"
                            + (f" ({others} busy too)" if others else ""))

    def _free_at(self, node: str, running: list[dict]) -> float:
        """Seconds until node can take a job: its running GPU job's remaining estimate, plus the queued jobs that
        chose to wait for it earlier in this pass."""
        mine = [self._remaining(j) for j in running if j.get("nomad") and self.lane(j) in GPU_CLASSES
                and (j["nomad"].get("target") or j["nomad"].get("node")) == node]
        return max(mine, default=0.0) + self._plan.get(node, 0.0)

    def _remaining(self, job: dict, count: bool = True) -> float:
        """Seconds a running job is estimated to keep its node.

        job: a running job record.
        count: False keeps it from counting a Breeze render's takes, which reads the library.
        Returns the seconds the job's progress gives for its current stage, the full estimate when it gives none
        yet, and a day when the log has not changed for STALE_LOG_SECONDS, so no job waits for a node that may be
        hung. With count False, a Breeze render that gives no time and that nobody has counted also gets a day. A
        render that is not Breeze also counts the check after its join.
        """
        n = job.get("nomad") or {}
        node = n.get("target") or n.get("node") or ""
        try:
            p = self.progress(job) if job.get("status") == "running" else None
        except OSError:
            p = None
        if not p or p.get("eta") is None:
            holder = self.holder(job)[0] if job["kind"] == "render" else None
            known = self._known_takes(job)
            if not count and known is None and self._is_breeze(job):
                return 86400.0
            return self._duration(job, node, holder, None if count else known)
        try:
            mtime = self.log_path(job).stat().st_mtime
        except (OSError, KeyError, TypeError):
            mtime = None
        if mtime and time.time() - mtime > STALE_LOG_SECONDS:
            return 86400.0
        eta = float(p["eta"]) - float(p.get("ahead") or 0.0)
        if (job["kind"] == "render" and not self._is_breeze(job) and p.get("phase") not in ("checking", "done")
                and not p.get("prechecked")):
            eta += 30 + CHECK_PER_TAKE * ((job.get("meta") or {}).get("lines") or DEFAULT_LINES)
        return eta

    @staticmethod
    def _is_breeze(job: dict) -> bool:
        return job["kind"] == "render" and (job.get("meta") or {}).get("engine") == "breeze"

    def _duration(self, job: dict, node: str, holder: str | None, needed: int | None = None) -> float:
        """Seconds a job's GPU stage is estimated to take on node.

        job: the job record, whose meta gives its lines, engine, candidates and characters.
        node: the node it would run on.
        holder: the node that holds its saved takes, or None.
        needed: the takes a Breeze render makes where its saved takes are at hand. None counts them.
        Returns the estimate. A Breeze render counts only the takes it has to make where the saved ones are at
        hand: on its holder, and on any node when the media mirror holds them. There it also counts the saved
        takes it hears, as last counted. A job on another node than its holder also counts fetching the takes and
        sending them back. The node's own timings of the same work scale each default once it has TIMING_MIN of
        them.
        """
        meta = job.get("meta") or {}
        lines = meta.get("lines") or DEFAULT_LINES
        rate = self._rate(node)
        kind = job["kind"]
        chapter = int(meta["chapters"][0]) if meta.get("chapters") else None
        move = MOVE_PER_TAKE * lines if holder and node != holder else 0.0
        if kind == "render":
            if meta.get("engine") != "breeze":
                return JOIN_ESTIMATE + move if meta.get("decode_only") else lines * rate + _overhead(lines)
            if meta.get("decode_only"):
                return self._render_seconds(job, node, 0) + move
            if node != holder and not (chapter is not None and self._mirrored(job.get("slug"), chapter)):
                return self._render_seconds(job, node, lines)
            if needed is None:
                needed = self._takes_needed(job, lines)
            return self._render_seconds(job, node, needed, self._known_unheard(job)) + move
        if kind == "check-takes":
            return self._timed(node, "check-takes/gpu", lines, 30.0, CHECK_PER_TAKE) + move
        if kind == "cast":
            return self._timed(node, "cast/gpu", _cast_units(meta), 60.0, CAST_TAKES * rate)
        if kind == "pick-voice":
            return 15.0
        if kind == "say-options":
            return self._timed(node, "say-options/gpu", 1, 60 + OPTION_TAKES * rate, 0.0)
        return OTHER_STAGE

    def _render_seconds(self, job: dict, node: str, needed: int, unheard: int = 0) -> float:
        """Seconds a Breeze render is estimated to take on node, from its dispatch.

        job: the render.
        node: the node it runs on.
        needed: the takes it makes or takes again.
        unheard: the saved takes it only hears before the join.
        Returns the estimate. A render that makes and hears nothing is a join.
        """
        cost = self._cost(job, node)
        heard = needed + unheard
        if heard <= 0:
            return PRE_STEP + min(cost("setup"), QUICK_SETUP_SECONDS) + cost("join") + cost("finish")
        takes = cost("takes", needed - 1) if needed > 0 else 0.0
        return PRE_STEP + cost("setup") + takes + cost("check", heard) + cost("join") + cost("finish", needed)

    def _cost(self, job: dict, node: str):
        """The cost(part, units) of a Breeze render of job on node, as breeze_progress takes it.

        job: the render, whose meta gives its lines and chapter.
        node: the node it runs on.
        Returns the function. A render that makes half of the chapter's lines or more is a whole chapter, and
        one that makes fewer is a touch-up. The takes of a whole chapter cost the node's take rate, and a check
        that hears half of the lines or more is costed from the node's whole chapters. A touch-up's takes and
        check are costed from the node's touch-ups. The join is costed by the chapter's words, whatever units it
        is given.
        """
        lines = (job.get("meta") or {}).get("lines") or DEFAULT_LINES
        words, rate = self._words(job), self._rate(node)

        def cost(part: str, units: float = 0.0) -> float:
            if part == "takes":
                # the units are the takes after the first
                whole = (units + 1) * 2 >= lines
                return units * rate if whole else self._timed(node, "touch", units, 0.0, rate * TOUCH_RATIO)
            if part == "check" and units * 2 >= lines:
                return self._timed(node, "check-whole", units, *WHOLE_CHECK)
            return self._timed(node, part, words if part == "join" else units, *RENDER_COST[part])

        return cost

    def _stage_seconds(self, job: dict, st: dict, node: str) -> float:
        """Seconds a stage of a job is estimated to take on node, from its dispatch to its result.

        job: the job.
        st: the stage, one of the job's stages.
        node: the node it runs on.
        Returns the estimate. Counts no take, so a Breeze render whose takes nobody has counted is costed at all
        its lines.
        """
        if st["class"] != "llm":
            holder = self.holder(job)[0] if job["kind"] in ("render", "check-takes") else None
            known = self._known_takes(job)
            lines = (job.get("meta") or {}).get("lines") or DEFAULT_LINES
            return self._duration(job, node, holder, lines if known is None else known)
        kind = job["kind"]
        fixed, per = LLM_COST.get(kind, (LLM_STAGE, 0.0))
        return PRE_STEP + self._timed(node, f"{kind}/llm", self._words(job) if kind in LLM_COST else 1, fixed, per)

    def _stage_best(self, job: dict, st: dict) -> float:
        """Seconds a stage is estimated to take on the node that offers its class and is fastest at it."""
        nodes = [w["name"] for w in self.workers if st["class"] in w["classes"]]
        return min((self._stage_seconds(job, st, node) for node in nodes), default=OTHER_STAGE)

    def _known_takes(self, job: dict) -> int | None:
        """The takes a Breeze render makes where its saved takes are at hand, by the newest count, or None."""
        if (job.get("meta") or {}).get("decode_only"):
            return 0
        jid = job.get("id") or ""
        hits = [hit for hit in (self._takes_cache.get(jid), self._work.get(jid)) if hit]
        return max(hits, key=lambda hit: hit[0])[1] if hits else None

    def _known_unheard(self, job: dict) -> int:
        """The saved takes a Breeze render only hears before its join, as last counted, or 0 when nobody did."""
        hit = self._work.get(job.get("id") or "")
        return hit[2] if hit and not (job.get("meta") or {}).get("decode_only") else 0

    def _words(self, job: dict) -> int:
        """The words of a job's chapters, from their records, or WORDS_PER_LINE a line where no record says."""
        meta = job.get("meta") or {}
        total, now = 0, time.time()
        for n in meta.get("chapters") or []:
            key = (job.get("slug") or "", int(n))
            hit = self._words_cache.get(key)
            if not hit or now - hit[0] > TAKES_CACHE_SECONDS:
                try:
                    record = lib.load_yaml(lib.chapter_dir(*key) / "chapter.yaml", {}) or {}
                    hit = (now, int(record.get("word_count") or 0))
                except Exception:
                    hit = (now, 0)
                self._words_cache[key] = hit
            total += hit[1]
        return total or (meta.get("lines") or DEFAULT_LINES) * WORDS_PER_LINE

    def _timed(self, node: str, key: str, units: float, fixed: float, per: float) -> float:
        """Seconds a piece of work is expected to take on node: expected() over the node's timings under key.

        Never raises, whatever placement.json holds under "timing".
        """
        timing = self.placement.get("timing")
        kept = timing.get(node) if isinstance(timing, dict) else None
        return expected(kept.get(key) if isinstance(kept, dict) else None, units, fixed, per)

    def _record_timing(self, job: dict, node: str | None, now: float) -> None:
        """Keeps how long each part of the stage a job just finished took on node, for later estimates.

        job: the job, whose stage succeeded. Its marks say when each part's first line arrived.
        node: the node that ran it.
        now: when the result arrived.
        Returns nothing. Keeps the last TIMING_KEEP timings of each part, as [units, seconds], in the units
        RENDER_COST names. A Breeze render adds its takes only when it is a touch-up, and it keeps a check that
        heard half of the chapter's lines or more apart from a touch-up's. A stage of another kind adds one
        timing from its step to its result. Records nothing for a stage whose step the app did not see start.
        Never raises, whatever placement.json holds under "timing".
        """
        try:
            n = job["nomad"]
            marks, st = n.get("marks") or {}, n["stages"][n["stage"]]
            step = marks.get("step")
            if not node or step is None:
                return
            if not isinstance(self.placement.get("timing"), dict):
                self.placement["timing"] = {}
            if not isinstance(self.placement["timing"].get(node), dict):
                self.placement["timing"][node] = {}
            kept = self.placement["timing"][node]

            def keep(key: str, units: float, seconds: float) -> None:
                # a mark is rounded to a tenth of a second, so two that came together can differ by that much
                if seconds > -0.2:
                    if not isinstance(kept.get(key), list):
                        kept[key] = []
                    kept[key].append([round(units, 1), round(max(0.0, seconds), 1)])
                    del kept[key][:-TIMING_KEEP]

            if not self._is_breeze(job):
                meta = job.get("meta") or {}
                units = {"check-takes": meta.get("lines") or DEFAULT_LINES, "cast": _cast_units(meta)}
                words = self._words(job) if job["kind"] in LLM_COST else 1
                gpu = st["class"] != "llm"
                keep(f"{job['kind']}/{'gpu' if gpu else 'llm'}", units.get(job["kind"], 1) if gpu else words,
                     now - step)
                return
            text = read_tail(self.log_path(job))
            seg = text[max(0, text.rfind("render: ")):]
            head, made, pre = RENDER_HEAD_RE.search(seg), TAKES_HEAD_RE.search(seg), PRECHECK_RE.search(seg)
            make = int(made.group(1)) if made else 0
            lines = int(head.group(2)) if head else (job.get("meta") or {}).get("lines") or DEFAULT_LINES
            first, after = marks.get("first"), marks.get("check") or marks.get("join")
            if first is not None:
                keep("setup", 0, first - step)
            if first is not None and after and make > 1 and make * 2 < lines:
                keep("touch", make - 1, after - first)
            if "check" in marks and "join" in marks and pre:
                heard = int(pre.group(1))
                keep("check-whole" if heard * 2 >= lines else "check", heard, marks["join"] - marks["check"])
            if "join" in marks and "joined" in marks:
                keep("join", self._words(job), marks["joined"] - marks["join"])
            post = CHECK_HEAD_RE.search(seg)
            # a check after the join that hears takes belongs to no part
            if "joined" in marks and not (post and int(post.group(1))):
                keep("finish", make, now - marks["joined"])
        except Exception as e:
            print(f"[jobs] timing of {job.get('id')} not kept: {e!r}", file=sys.stderr)

    def _forecast(self, now: float) -> dict[str, dict]:
        """When each running and queued job is expected to start and end, for the queue's display.

        now: the current time.
        Returns {job id: {"estimate", "start_in", "end_in", "on"}}, in seconds from now. estimate is the time the
        job's remaining stages take, and on is the GPU node it is expected to run on. start_in and end_in are
        missing for a job that can't be placed: the queue is on hold, no ready node offers its class, its takes
        are not counted yet, or it waits for such a job. A node whose running job has no end in sight takes no
        queued job. Follows the scheduler's rules in outline: one GPU job a node, LLM_SLOTS Claude jobs, a job
        after the running and the earlier queued jobs it conflicts with, backlog work as backlog_wait holds it,
        and a GPU job on the node where it ends first. Starts no job and counts no take.
        """
        running = [j for j in self.jobs.values() if j["status"] == "running"]
        queued = sorted((j for j in self.jobs.values() if j["status"] == "queued"), key=start_order)
        ready = {w["name"]: w for w in self.workers if w["ready"]}
        free: dict[str, float] = {}
        # the GPU nodes whose running job has no end in sight
        gone: set[str] = set()
        slots: list[float] = []
        ends: dict[str, float | None] = {}
        out: dict[str, dict] = {}

        def place(job: dict, st: dict, t: float) -> tuple[float, float, str | None] | None:
            if st["class"] == "llm":
                node = next((name for name, w in ready.items() if "llm" in w["classes"]), None)
                if node is None:
                    return None
                start = t if len(slots) < LLM_SLOTS else max(t, heapq.heappop(slots))
                end = start + self._stage_seconds(job, st, node)
                heapq.heappush(slots, end)
                return None if math.isinf(end) else (start, end, None)
            if self._is_breeze(job) and self._known_takes(job) is None:
                return None
            holder, required = self.holder(job)
            names = [holder] if required and holder else list(ready)
            cost = {name: self._stage_seconds(job, st, name) for name in names
                    if name in ready and name not in gone and st["class"] in ready[name]["classes"]}
            if not cost:
                return None
            best = min(cost, key=lambda name: (max(t, free.get(name, 0.0)) + cost[name],
                                               ready[name].get("gpu") != "nvidia", name))
            start = max(t, free.get(best, 0.0))
            free[best] = start + cost[best]
            return start, free[best], best

        def run(job: dict, stages: list[dict], t: float | None) -> dict:
            first = on = None
            spent: float | None = 0.0
            counted = not self._is_breeze(job) or self._known_takes(job) is not None
            for st in stages:
                got = place(job, st, t) if t is not None else None
                if got is None:
                    t = None
                    spent = spent + self._stage_best(job, st) if counted and spent is not None else None
                    continue
                first, on = (got[0] if first is None else first), on or got[2]
                spent, t = (None if spent is None else spent + got[1] - got[0]), got[1]
            return {"start": first, "end": t, "on": on, "estimate": spent}

        # Running jobs keep their node, and their later stages follow
        for job in running:
            n = job.get("nomad")
            left = self._remaining(job, count=False) if n else LOCAL_SECONDS
            t = left if left < 86400.0 else None
            if n:
                st, node = n["stages"][n["stage"]], n.get("target") or n.get("node")
                if st["class"] == "llm":
                    heapq.heappush(slots, math.inf if t is None else t)
                elif node and t is None:
                    gone.add(node)
                elif node:
                    free[node] = max(free.get(node, 0.0), t)
            ends[job["id"]] = run(job, n["stages"][n["stage"] + 1:] if n else [], t)["end"]
            out[job["id"]] = {"end_in": ends[job["id"]], "on": (n or {}).get("node")}

        # Queued jobs in the order they are offered a start
        done: list[dict] = list(running)
        for job in queued:
            n = job.get("nomad")
            stages = (n["stages"][n["stage"]:] if n else job_stages(job)) or []
            staged = between_stages(job)
            waits = [o for o in done if conflicts(job, o) and (o["status"] == "running" or job.get("backlog")
                                                                or not o.get("backlog") or between_stages(o))]
            if job.get("backlog") and not staged:
                beside = job["kind"] in BACKLOG_BESIDE
                waits += [o for o in done if not (beside and o.get("backlog") and o["kind"] in BACKLOG_BESIDE
                                                  and not between_stages(o))]
            held = self.paused and (bool(stages) or job["heavy"])
            t = None if held or any(ends[o["id"]] is None for o in waits) else max(
                (ends[o["id"]] for o in waits), default=0.0)
            if stages:
                got = run(job, stages, t)
            else:
                # a job on the app's machine is quick, and nothing says how quick
                got = {"start": t, "end": None if t is None else t + LOCAL_SECONDS, "on": None, "estimate": None}
            ends[job["id"]] = got["end"]
            out[job["id"]] = {"estimate": got["estimate"] or None, "start_in": got["start"], "end_in": got["end"],
                              "on": got["on"]}
            done.append(job)
        return out

    def _takes_needed(self, job: dict, lines: int) -> int:
        """The takes a Breeze render would make on the node that holds its saved takes.

        job: the render job.
        lines: the chapter's line count, which is the answer when the count cannot be worked out, so an unknown
            reuse never sends a render to the slow node.
        Returns the count. A count made here stands until a job finishes or TAKES_CACHE_SECONDS pass. After that
        the count _poll_work keeps stands, also while it is marked for another count, so a pass counts here only
        the jobs that nobody has counted yet. A count made here is marked for another count by _poll_work, which
        also counts the saved takes the render only hears.
        """
        meta = job.get("meta") or {}
        if not meta.get("chapters"):
            return lines
        cached = self._takes_cache.get(job.get("id") or "")
        if cached and time.time() - cached[0] < TAKES_CACHE_SECONDS:
            return cached[1]
        kept = self._work.get(job.get("id") or "")
        if kept:
            return kept[1]
        try:
            seed = _int_flag(list(job.get("args") or []), "--seed")
            reused, spoken = reusable_takes(job["slug"], int(meta["chapters"][0]), seed, meta.get("runner"))
            needed = spoken - reused
        except Exception:
            needed = lines
        if job.get("id"):
            self._takes_cache[job["id"]] = (time.time(), needed)
            self._work[job["id"]] = (0.0, needed, self._known_unheard(job))
        return needed

    def _rate(self, node: str) -> float:
        """Seconds per Breeze take on node: the mean of the rates recorded for it, else RATE_DEFAULT for its GPU type."""
        recorded = [v for v in ((self.placement.get("rates") or {}).get(node) or [])
                    if isinstance(v, (int, float)) and v > 0]
        if recorded:
            return sum(recorded) / len(recorded)
        w = next((w for w in self.workers if w["name"] == node), None)
        return RATE_DEFAULT.get((w or {}).get("gpu") or "none", RATE_DEFAULT["none"])

    def _record_rate(self, slug: str, n: int, node: str) -> None:
        """Adds a finished render's seconds per take to the last RATE_KEEP recorded for node.

        slug: the serial.
        n: the chapter the render finished.
        node: the node that rendered it.
        Returns nothing. Records only a Breeze render under breeze-server that made RATE_MIN_TAKES takes or more
        and at least half of its chapter's lines. Never raises.
        """
        try:
            meta = lib.load_yaml(lib.chapter_dir(slug, n) / "chapter.yaml", {}) or {}
            r = meta.get("render") if isinstance(meta.get("render"), dict) else {}
            script = meta.get("script") if isinstance(meta.get("script"), dict) else {}
            takes, secs = int(r.get("generated_takes") or 0), float(r.get("generate_seconds") or 0)
            if r.get("engine") != "breeze" or r.get("runner") != "cpp" or takes < RATE_MIN_TAKES or secs <= 0:
                return
            # part of a chapter is a few voices' lines, whose lengths differ from the whole chapter's
            if takes * 2 < int(script.get("lines") or 0):
                return
        except Exception:
            return
        rates = self.placement.setdefault("rates", {}).setdefault(node, [])
        rates.append(round(secs / takes, 3))
        del rates[:-RATE_KEEP]

    # Dispatch

    def _mark_dispatch(self, job: dict, node: str | None = None, cls: str | None = None) -> None:
        n = job.get("nomad")
        if not n:
            n = job["nomad"] = {"key": secrets.token_urlsafe(24), "stage": 0, "stages": job_stages(job)}
        if cls:
            n["stages"][n["stage"]]["class"] = cls
        n.update({"dispatched": None, "alloc": None, "node": None, "target": node, "contact": None,
                  "client_status": None, "wait": None, "ended_seen": None, "polled": 0, "dispatching": True,
                  "retry_at": 0, "log_got": 0, "marks": {}, "follows": None})
        job.update({"status": "running", "pid": None, "note": None})

    def dispatch_target(self, n: dict) -> str:
        """The registered job a stage dispatches: <prefix>-<class>, or <prefix>-<class>-<node> when pinned."""
        st = n["stages"][n["stage"]]
        return f"{self.prefix}-{st['class']}" + (f"-{n['target']}" if n.get("target") else "")

    def _dispatch(self, jid: str) -> None:
        # Request
        with self.lock:
            job = self.jobs.get(jid)
            if not job or job["status"] != "running" or not job["nomad"].get("dispatching") or jid in self._in_flight:
                return
            n = job["nomad"]
            target = self.dispatch_target(n)
            meta = {"job": jid, "key": n["key"], "stage": str(n["stage"])}
            token = f"{jid}-{n['stage']}"
            self._in_flight.add(jid)
        try:
            answer = self.nomad.dispatch(target, meta, token)
            error = None
        except NomadError as e:
            answer, error = None, e

        # Record
        with self.lock:
            self._in_flight.discard(jid)
            job = self.jobs.get(jid)
            if not job or not job.get("nomad"):
                return
            n = job["nomad"]
            if error is None:
                n["dispatching"] = False
                n["dispatched"] = answer.get("DispatchedJobID")
                if job["status"] == "running":
                    n["dispatched_at"] = now_iso()
                    n["polled"] = 0
                    n["wait"] = None
                    part = f" (stage {n['stage'] + 1} of {len(n['stages'])})" if len(n["stages"]) > 1 else ""
                    self._append_log(job, f"[web] dispatched{part} to Nomad as {n['dispatched']}\n")
                self._save()
            elif job["status"] != "running":
                n["dispatching"] = False
            elif error.status is None or error.status >= 500:
                n["retry_at"] = time.time() + DISPATCH_RETRY
                n["wait"] = f"retrying the dispatch of {target}: {error}"
            else:
                n["dispatching"] = False
                hint = (f"; register it with nomad job run -var class={n['stages'][n['stage']]['class']}"
                        f" -var node={n['target']} deploy/nomad/serial-audio.nomad.hcl"
                        if error.status == 404 and n.get("target") else "")
                self._finish(job, "failed", f"could not dispatch {target}: {error}{hint}")

    # Local jobs

    def _launch_local(self, job: dict) -> None:
        # the job's CLI inherits this, and a voice pick reads its candidates from the mirror
        os.environ[machine.MEDIA_MIRROR_ENV] = str(self.media_dir)
        if os.name == "nt":
            self._launch(job)
            return
        log = self.log_path(job)
        argv = [sys.executable, "-u", "-m", "serial_audio.web.runjob", str(log.with_suffix(".exit")), str(log), "--",
                sys.executable, "-u", "-m", "serial_audio.cli", *job["args"]]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
        try:
            proc = subprocess.Popen(argv, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        except OSError as e:
            job.update({"status": "failed", "finished": now_iso(), "note": f"could not start: {e}"})
            self._skip_batch(job, "an earlier job in the batch failed")
            return
        self._local[job["id"]] = proc
        job.update({"status": "running", "pid": proc.pid, "started": now_iso(), "note": None})

    def _reconcile_local(self, job: dict, snap) -> None:
        if os.name == "nt":
            from . import procs
            super()._reconcile(job, snap if snap is not None else procs.snapshot())
            return
        exit_file = self.log_path(job).with_suffix(".exit")
        proc = self._local.get(job["id"])
        if proc is not None:
            if proc.poll() is None:
                return
            self._local.pop(job["id"], None)
        elif job.get("pid") and _alive(job["pid"], str(exit_file)):
            return
        if exit_file.exists():
            try:
                info = json.loads(exit_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = {"code": None}
            job.update({"exit_code": info.get("code"), "finished": info.get("at") or now_iso(),
                        "status": "done" if info.get("code") == 0 else "failed"})
        else:
            job.update({"status": "lost", "finished": now_iso(),
                        "note": "the job's process ended without recording an exit code"})
        if job["status"] != "done":
            self._skip_batch(job, f"an earlier job in the batch {job['status']}")
        self._save()

    # The worker API

    def _worker_job(self, jid: str, key: str, stage: str | None) -> dict:
        job = self.jobs.get(jid)
        if job is None:
            raise WorkerError(404, f"no job {jid}")
        n = job.get("nomad")
        if not n or not _key_ok(n, key):
            raise WorkerError(403, "wrong key for this job")
        if job["status"] in ("cancelled", "stopped", "skipped"):
            raise WorkerError(410, f"the job was {job['status']}")
        if job["status"] != "running":
            raise WorkerError(409, f"the job is {job['status']}")
        if stage not in (None, "") and str(stage) != str(n["stage"]):
            raise WorkerError(409, f"the job is at stage {n['stage']}, not {stage}")
        return job

    def worker_bundle(self, jid: str, key: str, node: str) -> bytes:
        """The bundle for the job's current stage.

        jid, key: the job and the key the app gave it.
        node: the name the worker reports for its node, recorded as where the job runs.
        Returns the zip bytes. Raises WorkerError when the key is wrong or the job no longer wants to run.
        """
        # Check
        with self.lock:
            job = self._worker_job(jid, key, None)
            n = job["nomad"]
            if n.get("cancel"):
                raise WorkerError(410, "the job was stopped")
            st = n["stages"][n["stage"]]
            sync = job["kind"] == SYNC_KIND
            info = {"job": jid, "stage": n["stage"], "steps": [] if sync else [st["args"]], "class": st["class"],
                    "gpu": st["class"] in GPU_CLASSES and not sync, "sync": sync, "takes": _reads_takes(job),
                    "title": job["title"], "kind": job["kind"],
                    "chapters": (job.get("meta") or {}).get("chapters") or []}
            slug = job["slug"]

        # Build outside the lock, then check again
        fetched = now_iso()
        data = exchange.build_bundle(slug, info)
        with self.lock:
            job = self._worker_job(jid, key, str(info["stage"]))
            n = job["nomad"]
            if n.get("cancel"):
                raise WorkerError(410, "the job was stopped")
            n["contact"] = now_iso()
            n["log_got"] = 0
            if node:
                n["node"] = node
            if not job.get("started") or n["stage"] == 0:
                job["started"] = fetched
            self._append_log(job, f"[web] {n.get('node') or 'a node'} fetched the bundle ({len(data) / 1e6:.1f} MB)\n")
            self._save()
        return data

    def worker_log(self, jid: str, key: str, stage: str | None, offset: int, data: bytes) -> dict:
        """Appends the part of a worker's log chunk the app does not have yet.

        jid, key: the job and its key.
        stage: the stage the worker runs, as its header gives it.
        offset: where data starts in the worker's log.
        data: the chunk.
        Returns {"offset": the bytes of the worker's log the app now has, "cancel": the app asks the worker to stop}.
        Raises WorkerError on a wrong key, a job that is no longer running, or a negative offset.
        """
        if offset < 0:
            raise WorkerError(400, "the log offset is negative")
        with self.lock:
            job = self._worker_job(jid, key, stage)
            n = job["nomad"]
            got = int(n.get("log_got") or 0)
            if offset <= got < offset + len(data):
                fresh = data[got - offset:]
                try:
                    with self.log_path(job).open("ab") as fh:
                        fh.write(fresh)
                except OSError as e:
                    raise WorkerError(503, f"cannot write the log: {e}") from None
                got += len(fresh)
                n["log_got"] = got
                marks, arrived = n.setdefault("marks", {}), read_tail(self.log_path(job), len(fresh) + MARK_OVERLAP)
                for name, line in LOG_MARKS:
                    if name not in marks and line in arrived:
                        marks[name] = round(time.time(), 1)
                self._save()
            return {"offset": got, "cancel": bool(n.get("cancel"))}

    def _media_job(self, jid: str, key: str) -> None:
        """Raises WorkerError unless the key is the job's and the job runs or was stopped while its worker ran."""
        job = self.jobs.get(jid)
        if job is None:
            raise WorkerError(404, f"no job {jid}")
        n = job.get("nomad")
        if not n or not _key_ok(n, key):
            raise WorkerError(403, "wrong key for this job")
        if job["status"] != "running" and not (job["status"] == "stopped" and n.get("cancel")):
            raise WorkerError(410, f"the job is {job['status']}")

    def worker_media_have(self, jid: str, key: str, paths: list[str]) -> dict:
        """Tells a worker which media mirror files the app holds.

        jid, key: the job and its key.
        paths: mirror paths, as exchange.mirror_rel gives them.
        Returns {"have": {path: SHA-256}} for the paths that are files in the mirror. A path that is not a mirror
        path is left out. Raises WorkerError on a wrong key or a job that no longer runs.
        """
        with self.lock:
            self._media_job(jid, key)
        have = {}
        for rel in paths:
            if exchange.mirror_rel(rel) != rel:
                continue
            try:
                if (self.media_dir / rel).is_file():
                    have[rel] = exchange.sha256_file(self.media_dir / rel)
            except OSError:
                continue
        return {"have": have}

    def worker_media_target(self, jid: str, key: str, rel: str) -> Path:
        """Where the mirror keeps the file a worker is about to send.

        jid, key: the job and its key.
        rel: the file's mirror path.
        Returns the path inside the mirror. Raises WorkerError on a wrong key, a job that no longer runs, a path
        that is not a mirror path, or a file type the mirror does not keep.
        """
        with self.lock:
            self._media_job(jid, key)
        if exchange.mirror_rel(rel) != rel or os.path.splitext(rel)[1].lower() not in exchange.MIRROR_SUFFIXES:
            raise WorkerError(400, f"bad media path {rel!r}")
        return self.media_dir / rel

    def worker_media_stored(self) -> None:
        """Notes that a worker's file reached the mirror, so the next node choice looks at the mirror again."""
        self._mirror_cache.clear()

    def _result_job(self, jid: str, key: str, stage: str | None) -> tuple[dict, dict | None]:
        """The job a result belongs to and the report the app already gave for that stage, or None."""
        job = self.jobs.get(jid)
        if job is None:
            raise WorkerError(404, f"no job {jid}")
        n = job.get("nomad")
        if not n or not _key_ok(n, key):
            raise WorkerError(403, "wrong key for this job")
        done = (n.get("reports") or {}).get(str(stage if stage not in (None, "") else n["stage"]))
        if done is None and not (job["status"] == "stopped" and n.get("cancel")):
            self._worker_job(jid, key, stage)
        return job, done

    def worker_result(self, jid: str, key: str, stage: str | None, data: bytes, next_code: str | None = None) -> dict:
        """Writes a worker's changed files into the library and finishes the stage.

        jid, key: the job and its key.
        stage: the stage the worker ran, as its header gives it.
        data: the result zip.
        next_code: the version of the code the worker runs, when it would take another job in the same task.
        Returns the apply report. After a stage that succeeded, a worker that sent next_code may get the job its
        node would get next, as {"job", "key"} under "next" (_hand_next). A repeated upload for a stage the app
        took returns that stage's report. A result
        that arrives after the app gave up on a stop writes back only resumable files. A failed Claude pass writes
        back the chapters it finished, because the passes save each chapter as it completes.
        Raises WorkerError on a wrong key, a job that no longer takes results, or a damaged upload.
        """
        # Check the job before reading the upload
        with self.lock:
            job, done = self._result_job(jid, key, stage)
            if done is not None:
                return self._with_next(jid, done)
        try:
            info, archive = exchange.read_result(data)
        except exchange.ExchangeError as e:
            raise WorkerError(400, str(e)) from None

        # Apply
        with self.lock:
            job, done = self._result_job(jid, key, stage)
            if done is not None:
                return self._with_next(jid, done)
            n = job["nomad"]
            late = job["status"] == "stopped"
            st = n["stages"][n["stage"]]
            task = n.get("dispatched")
            code = info.get("exit_code")
            cancelled = bool(info.get("cancelled")) or bool(n.get("cancel"))
            success = code == 0 and not cancelled
            keep = success or (st["class"] == "llm" and not cancelled)
            try:
                report = exchange.apply_result(job["slug"], info, archive, success=keep,
                                               conflicts_dir=self.state_dir / "conflicts" / jid,
                                               media_dir=self.media_dir)
            except exchange.ExchangeError as e:
                raise WorkerError(400, str(e)) from None
            n.setdefault("reports", {})[str(n["stage"])] = report
            self._log_report(job, report)
            node = info.get("node") or n.get("node")
            if success and not late:
                self._record_timing(job, node, time.time())
            if job["kind"] not in BACKLOG_BESIDE:
                self._stale_work(job.get("slug"))
            self._record_placement(job, node, report, success)
            node = node or "the node"

            # Finish the stage
            if late:
                self._save()
            elif cancelled:
                self._finish(job, "stopped", "stopped from the web app", code)
            elif not success:
                self._finish(job, "failed", f"exit {code} on {node}", code)
            elif n["stage"] + 1 < len(n["stages"]):
                n["stage"] += 1
                job["status"] = "queued"
                self._append_log(job, f"[web] stage {n['stage']} of {len(n['stages'])} done on {node}\n")
                self._save()
            else:
                note = None
                if report["conflicts"]:
                    note = (f"{len(report['conflicts'])} file(s) changed in the library while the job ran; the "
                            f"worker's copies are in {self.state_dir.name}/conflicts/{jid}")
                job.update({"status": "done", "finished": now_iso(), "exit_code": 0, "note": note})
                job.pop("stopping", None)
                self._save()
            if success and not late and next_code:
                self._hand_next(job, n.get("target") or info.get("node"), st["class"], task, next_code, report)
            return self._with_next(jid, report)

    def _log_report(self, job: dict, report: dict) -> None:
        lines = []
        for key, label in (("applied", "wrote"), ("conflicts", "kept aside, changed in the library meanwhile"),
                           ("held", "not written because the job did not succeed")):
            if report[key]:
                shown = ", ".join(report[key][:12]) + (f" and {len(report[key]) - 12} more" if len(report[key]) > 12 else "")
                lines.append(f"[web] {label}: {shown}")
        if not lines:
            lines.append("[web] no library files changed")
        if report.get("media"):
            lines.append(f"[web] mirrored {report['media']} audio file(s) from the node")
        self._append_log(job, "\n".join(lines) + "\n")


def _key_ok(n: dict, key: str | None) -> bool:
    """Whether key is the job's key. Never raises, whatever the header holds."""
    return bool(key) and hmac.compare_digest(str(n.get("key") or "").encode("utf-8"),
                                             str(key).encode("utf-8", "surrogateescape"))


def _alive(pid: int, marker: str) -> bool:
    """Whether pid is still the job's wrapper process."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        return marker in cmdline
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_token(path: str | None) -> str | None:
    """The Nomad token from NOMAD_TOKEN or the first line of path, or None."""
    if os.environ.get("NOMAD_TOKEN"):
        return os.environ["NOMAD_TOKEN"].strip()
    if path:
        return Path(path).read_text(encoding="utf-8").strip() or None
    return None

