"""Runs the web app's jobs on the Nomad cluster.

NomadJobManager keeps JobManager's queue, batches, records and progress parsing, and changes where a job runs. Jobs
that need a GPU, ComfyUI or Claude are dispatched as Nomad jobs (deploy/nomad/serial-audio.nomad.hcl) to a node
whose client meta offers their class. Quick library jobs (fetch, add a serial, reset the bible) run on the app's
own machine. A worker fetches its job's bundle from the app, streams its log back and returns the files it changed,
which the app writes into the library unless they changed there meanwhile.

Jobs on different chapters, serials or nodes run at the same time. Jobs on the same chapter, and any job beside a
job that rewrites a serial's bible or voices, wait for each other. A node runs one GPU job at a time.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import library as lib
from ..breeze import reusable_takes
from . import exchange
from .jobs import FINISHED, ROOT, JobManager, now_iso, read_tail

NAMESPACE = "serial-audio"
PREFIX = "serial-audio"
CLASSES = ("breeze", "comfyui", "llm")
GPU_CLASSES = {"breeze", "comfyui"}
LLM_KINDS = {"character-pass", "screenplay"}
COMFY_KINDS = {"pick-voice", "check-takes"}
SERIAL_WIDE = {"character-pass", "cast", "reset-bible", "pick-voice", "fetch", "say-as", "voice-effect"}
# serial-wide jobs that write only voices/, the bible and serial.yaml, which a chapter job took in its bundle
CONFIG_WRITERS = {"cast", "pick-voice", "say-as", "voice-effect"}
# jobs that write only their chapters' files
CHAPTER_KINDS = {"render", "screenplay", "check-takes"}
# seconds per Breeze take by GPU type, until a node's own render records give its rate
RATE_DEFAULT = {"nvidia": 3.0, "metal": 18.0, "none": 60.0}
RATE_MIN_TAKES = 20
RATE_KEEP = 3
TAKES_CACHE_SECONDS = 600.0
STALE_LOG_SECONDS = 600.0
JOIN_ESTIMATE = 90.0
CHECK_PER_TAKE = 0.5
# a cast candidate is about nine seconds of audio, two takes' worth
CAST_TAKES = 2
DEFAULT_LINES = 200
WAIT_MARGIN = 120.0
LLM_SLOTS = 1
NODE_POLL = 15.0
JOB_POLL = 5.0
END_GRACE = 30.0
STOP_GRACE = 90.0
TICK_SECONDS = 2.0
TAIL_BYTES = 6000
DISPATCH_RETRY = 15.0
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
    """Seconds a Breeze render spends after its takes, joining and checking them."""
    return JOIN_ESTIMATE + CHECK_PER_TAKE * lines


def _touches(job: dict) -> tuple[str | None, set[int] | None]:
    """The serial a job works on and its chapters, or None for chapters when it affects the whole serial."""
    if job["kind"] in SERIAL_WIDE:
        return job.get("slug"), None
    return job.get("slug"), set((job.get("meta") or {}).get("chapters") or [])


def conflicts(job: dict, other: dict) -> bool:
    """Whether job must wait for other.

    job: the job that would start, a record with kind, slug and meta.
    other: a job that runs or was queued ahead of it.
    Returns True when both work on one serial and either changes the whole serial or they share a chapter, except
    that a CONFIG_WRITERS job never waits for a CHAPTER_KINDS job. Raises KeyError on a record without kind.
    """
    slug, chapters = _touches(job)
    slug_other, chapters_other = _touches(other)
    if not slug or slug != slug_other:
        return False
    if job["kind"] in CONFIG_WRITERS and other["kind"] in CHAPTER_KINDS:
        return False
    return chapters is None or chapters_other is None or bool(chapters & chapters_other)


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
    the briefs cached and calls no LLM. Only a Qwen cast needs a ComfyUI node.
    """
    kind, args = job["kind"], list(job["args"])
    if kind in LLM_KINDS:
        return [{"class": "llm", "args": args}]
    if kind == "cast":
        if "--dry-run" in args:
            return [{"class": "llm", "args": args}]
        engine = _cast_engine(job)
        flag = [] if engine is None or "--engine" in args else ["--engine", engine]
        voices = [a for a in args if a != "--redo-brief"] + flag
        return [{"class": "llm", "args": args + flag + ["--dry-run"], "label": "casting briefs"},
                {"class": "comfyui" if engine == "qwen" else "breeze", "args": voices, "label": "voices"}]
    if kind in COMFY_KINDS:
        return [{"class": "comfyui", "args": args}]
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
        """Remembers the node that now holds a chapter's takes or a voice's candidates after a stage it ran."""
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
        voices = self.placement.setdefault("voices", {}).setdefault(slug, {})
        for rel in touched:
            if rel.startswith("voices/") and rel.endswith(".yaml") and rel != "voices/_briefs.yaml":
                voices[rel[len("voices/"):-len(".yaml")]] = node
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
        """The node that holds the takes or candidates a job works on, or None, and whether the job must run there.

        A join, a take check and picking a voice must run there. A render would rather, so it can reuse its takes.
        A pick goes to the node that made the candidate it files, because a recast may have run on another node.
        """
        slug, kind, meta = job.get("slug"), job["kind"], job.get("meta") or {}
        if kind in ("render", "check-takes") and meta.get("chapters"):
            n = int(meta["chapters"][0])
            node = ((self.placement.get("chapters") or {}).get(slug) or {}).get(str(n))
            if not node:
                takes = lib.load_yaml(lib.chapter_dir(slug, n) / "breeze_takes.json", {}) or {}
                node = self._node_for_paths([t.get("file") or "" for t in (takes.get("takes") or {}).values()
                                             if isinstance(t, dict)][:5])
            return node, kind == "check-takes" or bool(meta.get("decode_only"))
        if kind == "pick-voice" and meta.get("characters"):
            cid = meta["characters"][0]
            rec = lib.load_yaml(lib.serial_dir(slug) / "voices" / f"{cid}.yaml", {}) or {}
            cands = [c for b in [rec] + list(rec.get("history") or []) for c in (b.get("candidates") or [])
                     if isinstance(c, dict)]
            seed = _int_flag(list(job.get("args") or []), "--seed")
            if seed is None:
                chosen = rec.get("chosen")
                filed = (rec.get("candidates") or [])[chosen:chosen + 1] if isinstance(chosen, int) else []
            else:
                filed = [c for c in cands if c.get("seed") == seed][:1]
            node = self._node_for_paths([c.get("clip") or "" for c in filed])
            if not node:
                node = ((self.placement.get("voices") or {}).get(slug) or {}).get(cid)
            if not node:
                node = self._node_for_paths([c.get("clip") or "" for c in cands][:5])
            return node, True
        return None, False

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
                                    f"; Nomad job {n['dispatched']} exits as soon as a node starts it"
                                    if n.get("dispatched") else "")})
                    self._append_log(job, f"\n[web] {job['note']}\n")
                    self._skip_batch(job, "an earlier job in the batch was stopped")
                elif not n.get("cancel"):
                    n["cancel"] = True
                    n["stop_at"] = time.time()
                    job["stopping"] = True
                    self._append_log(job, "\n[web] stop requested; the worker stops within a few seconds\n")
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
        p = super().progress(job, now) or {"phase": "running", "fraction": None, "eta": None, "detail": ""}
        if n.get("wait"):
            p["detail"] = n["wait"]
        elif p.get("phase") in ("starting", "running", "briefs"):
            last = next((ln.strip() for ln in reversed(read_tail(self.log_path(job), 4000).splitlines()) if ln.strip()), "")
            if last.startswith("[worker] waiting:"):
                p.update({"phase": "waiting", "detail": last[len("[worker] "):]})
        st = n["stages"][n["stage"]]
        label = (f"{st.get('label') or 'stage'} ({n['stage'] + 1} of {len(n['stages'])})"
                 if len(n["stages"]) > 1 else "")
        p["detail"] = ", ".join(x for x in (n.get("node"), label, p.get("detail")) if x)
        p["node"] = n.get("node")
        return p

    def snapshot(self) -> dict:
        snap = super().snapshot()
        for item in snap["jobs"]:
            if item.get("nomad"):
                item["nomad"] = {k: v for k, v in item["nomad"].items() if k != "key"}
        snap.update({"executor": "nomad", "workers": self.workers, "nomad": self.nomad_state})
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
            changed = self._schedule(now, dispatch)
            if changed:
                self._save()
        for jid in dispatch:
            self._dispatch(jid)

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
            if err.status == 404:
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
        self._append_log(job, f"\n[web] {note}\n")
        if status != "done":
            self._skip_batch(job, f"an earlier job in the batch {status}")
        self._save()

    # Scheduling

    def _schedule(self, now: float, dispatch: list[str]) -> bool:
        running = [j for j in self.jobs.values() if j["status"] == "running"]
        queued = sorted((j for j in self.jobs.values() if j["status"] == "queued"), key=lambda j: j["seq"])
        self.wait_reasons = {}
        self._plan = {}
        blocked: dict[str, dict] = {}
        changed = False
        for job in queued:
            lane = self.lane(job)
            first = blocked.get(lane)
            if first and lane not in GPU_CLASSES and (job["kind"] not in CONFIG_WRITERS or conflicts(job, first)):
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
                self._mark_dispatch(job, *self._picked.pop(job["id"], (None, None)))
                dispatch.append(job["id"])
            else:
                self._launch_local(job)
            if job["status"] == "running":
                running.append(job)
            changed = True
        return changed

    def _blocker_for(self, job: dict, lane: str, running: list[dict]) -> str | None:
        """Why the queued job cannot start now, or None."""
        if self.paused and (lane in CLASSES or job["heavy"]):
            return "the queue is on hold"
        if job.get("batch"):
            ahead = next((j for j in sorted(self.jobs.values(), key=lambda j: j["seq"])
                          if j["batch"] == job["batch"] and j["seq"] < job["seq"] and j["status"] not in FINISHED), None)
            if ahead:
                return f"waiting for {ahead['title']}, earlier in its batch"
        earlier = next((j for j in self.jobs.values() if j["status"] == "queued" and j["seq"] < job["seq"]
                        and conflicts(job, j)), None)
        if earlier:
            return f"waiting for {earlier['title']}, queued ahead of it on the same serial"
        for other in running:
            if conflicts(job, other):
                wide = other["kind"] in SERIAL_WIDE or job["kind"] in SERIAL_WIDE
                return f"waiting for {other['title']}" + (", which changes the whole serial" if wide else
                                                          ", which works on the same chapter")
            # a chapter job reads the serial's files when it builds its bundle, not when it is dispatched
            if (job["kind"] in CONFIG_WRITERS and job.get("slug") and other.get("slug") == job["slug"]
                    and not (other.get("nomad") or {}).get("contact")):
                return f"waiting for {other['title']} to fetch its bundle"
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
            return f"waiting for {mine[0]['title']}"
        return None

    def _pick_node(self, job: dict, lane: str, running: list[dict]) -> tuple[str | None, str | None, str | None]:
        """The node and class a GPU job runs on, as (node, class, None), or (None, None, why it waits).

        A node runs one GPU job at a time. A job that needs a node's files gets that node, and may switch to the
        other GPU class when that node offers only that one. Any other job goes to the ready node where its
        estimated finish is earliest, counting the node's running job and the queued jobs already waiting for it,
        and waits for a busy node when that beats every free one by WAIT_MARGIN. A job that waits adds its estimate
        to the node's timeline for the rest of the pass.
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
            cls = lane if lane in w["classes"] else next((c for c in ("comfyui", "breeze") if c in w["classes"]), None)
            if cls is None:
                return None, None, f"{need}, offers no GPU class"
            return holder, cls, None
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

    def _remaining(self, job: dict) -> float:
        """Seconds a running job is estimated to keep its node.

        job: a running job record.
        Returns the progress log's ETA plus the check still to come, the full estimate when the log has no ETA yet,
        and a day when the log has not changed for STALE_LOG_SECONDS, so no job waits for a node that may be hung.
        """
        n = job.get("nomad") or {}
        node = n.get("target") or n.get("node") or ""
        try:
            p = self.progress(job) if job.get("status") == "running" else None
        except OSError:
            p = None
        if not p or p.get("eta") is None:
            holder = self.holder(job)[0] if job["kind"] == "render" else None
            return self._duration(job, node, holder)
        try:
            mtime = self.log_path(job).stat().st_mtime
        except (OSError, KeyError, TypeError):
            mtime = None
        if mtime and time.time() - mtime > STALE_LOG_SECONDS:
            return 86400.0
        eta = float(p["eta"])
        if job["kind"] == "render" and p.get("phase") not in ("checking", "done"):
            eta += 30 + CHECK_PER_TAKE * ((job.get("meta") or {}).get("lines") or DEFAULT_LINES)
        return eta

    def _duration(self, job: dict, node: str, holder: str | None) -> float:
        """Seconds a job is estimated to take on node.

        job: the job record, whose meta gives its lines, engine, candidates and characters.
        node: the node it would run on.
        holder: the node that holds its saved takes, or None.
        Returns the estimate. A Breeze render on its holder counts only the takes the holder lacks.
        """
        meta = job.get("meta") or {}
        lines = meta.get("lines") or DEFAULT_LINES
        rate = self._rate(node)
        kind = job["kind"]
        if kind == "render":
            if meta.get("decode_only"):
                return JOIN_ESTIMATE
            takes = self._takes_needed(job, lines) if meta.get("engine") == "breeze" and node == holder else lines
            return takes * rate + _overhead(lines)
        if kind == "check-takes":
            return 30 + CHECK_PER_TAKE * lines
        if kind == "cast":
            count = (meta.get("candidates") or 3) * (len(meta.get("characters") or []) or 3)
            return 60 + count * CAST_TAKES * rate
        if kind == "pick-voice":
            return 15.0
        return 120.0

    def _takes_needed(self, job: dict, lines: int) -> int:
        """The takes a Breeze render would make on the node that holds its saved takes.

        job: the render job.
        lines: the chapter's line count, which is the answer when the count cannot be worked out, so an unknown
            reuse never sends a render to the slow node.
        Returns the count, cached per job until a job finishes or TAKES_CACHE_SECONDS pass.
        """
        meta = job.get("meta") or {}
        if not meta.get("chapters"):
            return lines
        cached = self._takes_cache.get(job.get("id") or "")
        if cached and time.time() - cached[0] < TAKES_CACHE_SECONDS:
            return cached[1]
        try:
            seed = _int_flag(list(job.get("args") or []), "--seed")
            reused, spoken = reusable_takes(job["slug"], int(meta["chapters"][0]), 1 if seed is None else seed,
                                            meta.get("runner"))
            needed = spoken - reused
        except Exception:
            needed = lines
        if job.get("id"):
            self._takes_cache[job["id"]] = (time.time(), needed)
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
        """Adds a finished render's seconds per take to the last RATE_KEEP recorded for node. Never raises."""
        try:
            meta = lib.load_yaml(lib.chapter_dir(slug, n) / "chapter.yaml", {}) or {}
            r = meta.get("render") if isinstance(meta.get("render"), dict) else {}
            takes, secs = int(r.get("generated_takes") or 0), float(r.get("generate_seconds") or 0)
            if r.get("engine") != "breeze" or r.get("runner") != "cpp" or takes < RATE_MIN_TAKES or secs <= 0:
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
                  "retry_at": 0, "log_got": 0})
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
            info = {"job": jid, "stage": n["stage"], "steps": [st["args"]], "class": st["class"],
                    "gpu": st["class"] in GPU_CLASSES, "title": job["title"], "kind": job["kind"],
                    "chapters": (job.get("meta") or {}).get("chapters") or []}
            slug = job["slug"]

        # Build outside the lock, then check again
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
                job["started"] = now_iso()
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
                self._save()
            return {"offset": got, "cancel": bool(n.get("cancel"))}

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

    def worker_result(self, jid: str, key: str, stage: str | None, data: bytes) -> dict:
        """Writes a worker's changed files into the library and finishes the stage.

        jid, key: the job and its key.
        stage: the stage the worker ran, as its header gives it.
        data: the result zip.
        Returns the apply report. A repeated upload for a stage the app took returns that stage's report. A result
        that arrives after the app gave up on a stop writes back only resumable files. A failed Claude pass writes
        back the chapters it finished, because the passes save each chapter as it completes.
        Raises WorkerError on a wrong key, a job that no longer takes results, or a damaged upload.
        """
        # Check the job before reading the upload
        with self.lock:
            job, done = self._result_job(jid, key, stage)
            if done is not None:
                return done
        try:
            info, archive = exchange.read_result(data)
        except exchange.ExchangeError as e:
            raise WorkerError(400, str(e)) from None

        # Apply
        with self.lock:
            job, done = self._result_job(jid, key, stage)
            if done is not None:
                return done
            n = job["nomad"]
            late = job["status"] == "stopped"
            st = n["stages"][n["stage"]]
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
            return report

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

