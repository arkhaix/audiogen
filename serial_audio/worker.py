"""The task a cluster node runs for one web app job.

deploy/nomad/bootstrap.py fetches the job's bundle from the web app, unpacks it into a work folder and calls main().
The worker runs the job's CLI steps there against its copy of the serial, sends the log to the app every few
seconds, stops when the app asks, and sends back every file the steps created or changed. Before the steps it fetches
the chapter's saved takes from the app when another node made them, and after the steps it sends the app the takes
and cast candidates this node made, so a later job can run on any node.

A worker that finishes a breeze job asks the app, with the result, for the job this node would get next. When the
app hands it one, the worker runs that job in the same task. It holds the machine's GPU lock from its first GPU job
to its last, and the breeze-server and the recognizer its steps start stay up between them (kept.py).
"""
from __future__ import annotations

import concurrent.futures
import datetime as _dt
import hashlib
import http.client
import io
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
import zipfile
from pathlib import Path

from . import kept
from . import library as lib
from .web import exchange

LOG_EVERY = 3.0
MEDIA_THREADS = 4
MEDIA_TRIES = 4
MEDIA_RETRY = 10.0
MEDIA_REPORT = 30.0
HAVE_BATCH = 400
LOG_CHUNK = 1 << 19
BUSY_POLL = 20.0
BUSY_REPORT = 300.0
RESULT_TRIES = 40
RESULT_RETRY = 30.0
KILL_GRACE = 10.0
COMFY_URL = "http://127.0.0.1:8188"
CSRF_HEADER = "X-Serial-Audio"
KEY_HEADER = "X-Serial-Audio-Key"
NODE_HEADER = "X-Serial-Audio-Node"
STAGE_HEADER = "X-Serial-Audio-Stage"
# sent with a result by a worker that would take another job: the version of the code it runs
NEXT_HEADER = "X-Serial-Audio-Next"
NEXT_CLASSES = ("breeze",)
BUNDLE_TRIES = 6
BUNDLE_RETRY = 10.0
CHILD_ENV_DROP = ("CLAUDECODE", "NOMAD_TOKEN")
CLAUDE_AUTH = "SERIAL_AUDIO_CLAUDE_AUTH"
CLAUDE_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
GPU_LOCK = Path(os.environ.get("SERIAL_AUDIO_GPU_LOCK") or (
    Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "serial-audio" / "gpu.lock" if os.name == "nt"
    else "/tmp/serial-audio-gpu.lock"))
LOCK_POLL = 5.0


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


class AppError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


class App:
    """The web app's worker API for one job step."""

    def __init__(self, url: str, job: str, key: str, node: str, stage: int):
        self.url, self.job = url.rstrip("/"), job
        self.headers = {CSRF_HEADER: "1", KEY_HEADER: key, NODE_HEADER: node, STAGE_HEADER: str(stage)}

    def request(self, method: str, path: str, data: bytes | None = None, timeout: float = 60,
                headers: dict | None = None) -> dict:
        """The JSON answer to one request. Raises AppError with the HTTP status, or None when unreachable."""
        req = urllib.request.Request(f"{self.url}/api/worker/{self.job}/{path}", data=data, method=method,
                                     headers={**self.headers, "Content-Type": "application/octet-stream",
                                              **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            raise AppError(e.code, f"the app answered {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from None
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            raise AppError(None, f"the app is not reachable: {getattr(e, 'reason', e)}") from None

    def fetch(self, path: str, timeout: float = 120) -> bytes:
        """The body of a GET of path on the app. Raises AppError with the HTTP status, or None when unreachable or
        when the answer is cut short."""
        req = urllib.request.Request(self.url + path, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise AppError(e.code, f"the app answered {e.code} for {path}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise AppError(None, f"the app is not reachable: {getattr(e, 'reason', e)}") from None


class Log:
    """The job's log: a local file the pump sends on, mirrored to this task's stdout for Nomad."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.fh = path.open("ab")

    def write(self, data: bytes) -> None:
        with self.lock:
            self.fh.write(data)
            self.fh.flush()
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            except (OSError, ValueError):
                pass

    def line(self, text: str) -> None:
        self.write((text.rstrip("\n") + "\n").encode("utf-8"))

    def close(self) -> None:
        with self.lock:
            self.fh.close()


class LogPump(threading.Thread):
    """Sends the log's new bytes to the app every few seconds and notes when the app asks the job to stop."""

    def __init__(self, app: App, path: Path):
        super().__init__(name="log-pump", daemon=True)
        self.app, self.path = app, path
        self.offset = 0
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.last_error = 0.0

    def run(self) -> None:
        while not self.done.is_set():
            self.flush()
            self.done.wait(LOG_EVERY)

    def flush(self) -> bool:
        """Sends what the app does not have yet. Returns whether the app now has the whole log. Never raises."""
        with self.lock:
            try:
                size = self.path.stat().st_size
                while True:
                    with self.path.open("rb") as fh:
                        fh.seek(self.offset)
                        data = fh.read(LOG_CHUNK)
                    answer = self.app.request("POST", f"log?offset={self.offset}", data, timeout=30)
                    if not isinstance(answer, dict) or not isinstance(answer.get("offset", 0), int):
                        return False
                    if answer.get("cancel"):
                        self.cancel.set()
                    self.offset = int(answer.get("offset", self.offset + len(data)))
                    if not data or self.offset >= size:
                        return self.offset >= size
            except AppError as e:
                if e.status in (403, 404, 410):
                    self.cancel.set()
                if time.time() - self.last_error > 60:
                    self.last_error = time.time()
                    print(f"[worker] log not sent: {e}", file=sys.stderr, flush=True)
                return False
            except OSError as e:
                print(f"[worker] cannot read the log: {e}", file=sys.stderr, flush=True)
                return False

    def stop(self) -> None:
        self.done.set()


def node_config() -> dict:
    """The node's settings file named by SERIAL_AUDIO_CONFIG, or {} when there is none. Never raises."""
    path = os.environ.get("SERIAL_AUDIO_CONFIG") or ""
    if not path or path.startswith("${"):
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[worker] ignoring the node settings in {path}: {e}", file=sys.stderr, flush=True)
        return {}
    return data if isinstance(data, dict) else {}


def machine_busy(comfy_url: str, kept_pids=()) -> str | None:
    """Why the GPU on this machine is in use by something outside this job, or None.

    comfy_url: this machine's ComfyUI.
    kept_pids: the processes this worker keeps up between its jobs, which are not outside it.
    """
    from .web import jobs, procs
    snap = procs.snapshot()
    ours = procs.descendants(snap, os.getpid()) if snap else {os.getpid()}
    for e in jobs.external_activity(snap, ours, time.time()):
        if e["heavy"]:
            what = f"{e['kind']} chapter {e['chapter']}" if e.get("chapter") else e["kind"]
            return f"an outside {what} is running (pid {e['pid']})"
    from . import machine
    pids = [p for p in machine.breeze_server_pids() if p not in kept_pids]
    if pids:
        return f"a breeze-server is running (pid {', '.join(map(str, pids))})"
    if not machine.listening(comfy_url):
        return None
    try:
        with urllib.request.urlopen(f"{comfy_url}/api/jobs?status=in_progress,pending&limit=5", timeout=5) as resp:
            items = json.loads(resp.read().decode("utf-8")).get("jobs") or []
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return f"ComfyUI is busy with {len(items)} prompt(s)" if items else None


def media_files(folder: Path, changed: dict | None) -> list[str]:
    """Node paths of the audio this node holds that the app keeps a copy of.

    folder: the worker's copy of the serial.
    changed: the files the steps changed. The answer then covers the candidates of each changed voice record, with
        their Drama 3 voice files, every take of each chapter that has a changed file, and the pronunciation options
        when the pronunciations changed. None covers every voice record, every chapter and every option of the
        serial.
    Returns the paths that are files on this node, sorted. Never raises.
    """
    import yaml
    said = folder / "bible" / "pronunciations.yaml"
    if changed is None:
        voices = sorted((folder / "voices").glob("*.yaml"))
        chapters = sorted(p.parent for p in (folder / "chapters").glob(f"*/{exchange.TAKES_FILE}"))
    else:
        voices = [folder / rel for rel in changed if rel.startswith("voices/") and rel.endswith(".yaml")]
        chapters = sorted({folder / "chapters" / rel.split("/")[1] for rel in changed
                           if rel.startswith("chapters/") and rel.count("/") >= 2})
    out = set()
    if changed is None or "bible/pronunciations.yaml" in changed:
        try:
            entries = yaml.safe_load(said.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            entries = None
        for entry in entries.values() if isinstance(entries, dict) else []:
            options = entry.get("options") if isinstance(entry, dict) else None
            candidates = options.get("candidates") if isinstance(options, dict) else None
            for c in candidates if isinstance(candidates, list) else []:
                if isinstance(c, dict) and isinstance(c.get("clip"), str):
                    out.add(c["clip"])
    for path in voices:
        if path.name == "_briefs.yaml":
            continue
        try:
            rec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for batch in [rec] + list(rec.get("history") or []):
                for c in batch.get("candidates") or []:
                    if isinstance(c, dict):
                        out.update(v for v in (c.get("clip"), c.get("voice_file")) if isinstance(v, str))
        except (OSError, ValueError, AttributeError, TypeError, yaml.YAMLError):
            continue
    for chapter in chapters:
        out.update(exchange.take_files(chapter).values())

    def sendable(p: str) -> bool:
        try:
            return Path(p).suffix.lower() in exchange.MIRROR_SUFFIXES and Path(p).stat().st_size > 0
        except OSError:
            return False

    return sorted(p for p in out if sendable(p))


def app_call(call, down: threading.Event, stop: threading.Event | None = None, fatal: tuple = (403, 404, 410)):
    """The answer of one request to the app, tried again while the app is unreachable or answers 5xx.

    call: makes the request and raises AppError when it fails.
    down: set once the app has refused this job or a request has run out of tries. A call made after that fails
        without a request, so one outage costs one wait and not one per file.
    stop: when it is set, the call fails without another try.
    fatal: the HTTP statuses that mean the app no longer takes requests for this job.
    Returns call's answer. Raises AppError at once on any 4xx, and after MEDIA_TRIES tries otherwise.
    """
    error = AppError(None, "the app did not take an earlier request of this job")
    for attempt in range(MEDIA_TRIES):
        if down.is_set() or (stop is not None and stop.is_set()):
            raise error
        try:
            return call()
        except AppError as e:
            error = e
            if e.status in fatal:
                down.set()
            if e.status is not None and e.status < 500:
                raise
            if attempt + 1 < MEDIA_TRIES:
                (stop or down).wait(MEDIA_RETRY)
    down.set()
    raise error


def app_copies(app: App, rels: list[str], down: threading.Event, stop: threading.Event | None = None) -> dict[str, str]:
    """{mirror path: SHA-256} for each path in rels that the app's media mirror holds.

    down, stop: as app_call takes them. Raises AppError when the app does not answer.
    """
    out = {}
    for start in range(0, len(rels), HAVE_BATCH):
        body = json.dumps({"paths": rels[start:start + HAVE_BATCH]}).encode("utf-8")
        answer = app_call(lambda: app.request("POST", "media/have", body, timeout=600), down, stop).get("have")
        if isinstance(answer, dict):
            out.update({k: v for k, v in answer.items() if isinstance(v, str)})
    return out


def send_media(app: App, paths: list[str], log: Log, stop: threading.Event | None = None) -> dict:
    """Sends the app each file its media mirror lacks or holds with other contents.

    app: the web app.
    paths: files on this node, by absolute path.
    log: the job log, which gets a progress line every MEDIA_REPORT seconds and a line at the end.
    stop: when it is set, the files not sent yet are left out and count as failed.
    Returns {"sent", "bytes", "kept", "failed"}: files sent, their size, files the app already had, and files that
    did not arrive. A file the app refuses fails alone. While the app is unreachable a request is tried again for
    MEDIA_TRIES tries, and after that the rest fail at once. Never raises.
    """
    by_rel = {rel: Path(p) for p in paths if (rel := exchange.mirror_rel(p))}
    out = {"sent": 0, "bytes": 0, "kept": 0, "failed": 0}
    if not by_rel:
        return out
    down = threading.Event()
    try:
        have = app_copies(app, sorted(by_rel), down, stop)
    except AppError as e:
        out["failed"] = len(by_rel)
        log.line(f"[worker] the app did not say which audio it holds, so none was sent: {e}")
        return out

    def send(rel: str) -> int | None:
        path = by_rel[rel]
        try:
            if rel in have and have[rel] == exchange.sha256_file(path):
                return 0
            data = path.read_bytes()
            app_call(lambda: app.request("POST", "media/file?path=" + urllib.parse.quote(rel, safe=""), data,
                                         timeout=300), down, stop)
        except (OSError, AppError):
            return None
        return len(data)

    last = time.time()
    with concurrent.futures.ThreadPoolExecutor(MEDIA_THREADS) as pool:
        for done, size in enumerate(pool.map(send, sorted(by_rel)), 1):
            if size is None:
                out["failed"] += 1
            elif size:
                out["sent"] += 1
                out["bytes"] += size
            else:
                out["kept"] += 1
            if time.time() - last >= MEDIA_REPORT:
                last = time.time()
                log.line(f"[worker] audio for the app: {done:,} of {len(by_rel):,} files done, "
                         f"{out['bytes'] / 1e6:,.0f} MB sent")
    if out["sent"] or out["failed"]:
        log.line(f"[worker] sent the app {out['sent']:,} audio file(s), {out['bytes'] / 1e6:,.1f} MB"
                 + (f", and it already had {out['kept']:,}" if out["kept"] else "")
                 + (f". {out['failed']:,} did not arrive, so a job on another node cannot use them"
                    if out["failed"] else ""))
    return out


def pcm_id(wav: bytes) -> str | None:
    """The id a Breeze take's entry keeps of its audio: 16 hex digits of the SHA-1 of the WAV's samples.

    Returns None for bytes that are not a readable WAV.
    """
    try:
        with wave.open(io.BytesIO(wav), "rb") as w:
            return hashlib.sha1(w.readframes(w.getnframes())).hexdigest()[:16]
    except (wave.Error, EOFError):
        return None


def fetch_takes(app: App, folder: Path, slug: str, chapters: list, root: Path, log: Log) -> int:
    """Brings the saved takes of the job's chapters to this node when another node made them.

    app: the web app, whose media mirror holds the takes the nodes sent it.
    folder: the worker's copy of the serial.
    slug, chapters: the serial and the chapters the job works on.
    root: this node's output folder.
    log: the job log, which gets one line per chapter that needed takes.
    Returns how many takes now come from the app. Such a take's file is under root, and its entry in the worker's
    copy of breeze_takes.json names that file. A take this node made and still holds is left alone. A take the
    app has no copy of, or only a copy of other audio than the entry names, is left as it is, so a render takes
    the line again. Never raises: a failure is logged and the steps run with the takes this node has.
    """
    total = 0
    down = threading.Event()
    for n in chapters:
        try:
            total += fetch_chapter_takes(app, folder, slug, int(n), root, log, down)
        except Exception as e:
            log.line(f"[worker] could not fetch the takes of chapter {n} from the app, so the steps use the takes "
                     f"this node has: {type(e).__name__}: {e}")
    return total


def fetch_chapter_takes(app: App, folder: Path, slug: str, n: int, root: Path, log: Log, down: threading.Event) -> int:
    """fetch_takes for one chapter. down is what app_call takes. Raises when the takes file or the app fails."""
    # Plan
    path = folder / "chapters" / f"{n:04d}" / exchange.TAKES_FILE
    if not path.is_file():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    takes = data.get("takes") if isinstance(data, dict) else None
    plan = []
    for entry in (takes.values() if isinstance(takes, dict) else []):
        raw = entry.get("file") if isinstance(entry, dict) else None
        target = exchange.local_path(raw, root, f"breeze/{slug}/ch{n:04d}")
        rel = exchange.mirror_rel(raw) if target else None
        if rel and not (exchange.same_path(raw, target) and Path(raw).is_file()):
            plan.append((entry, rel, Path(target)))
    if not plan:
        return 0
    have = app_copies(app, sorted({rel for _, rel, _ in plan}), down)

    # Fetch
    def bring(item) -> int | None:
        entry, rel, target = item
        digest = have.get(rel)
        if digest is None:
            return None
        try:
            held = target.is_file() and exchange.sha256_file(target) == digest
            if held:
                blob = target.read_bytes()
            else:
                blob = app_call(lambda: app.fetch("/media/mirror/" + urllib.parse.quote(rel)), down, fatal=())
                if hashlib.sha256(blob).hexdigest() != digest:
                    return None
            if entry.get("audio") and target.suffix.lower() == ".wav" and pcm_id(blob) != entry["audio"]:
                return None
            if not held:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".part")
                tmp.write_bytes(blob)
                os.replace(tmp, target)
        except (OSError, AppError):
            return None
        return 0 if held else len(blob)

    with concurrent.futures.ThreadPoolExecutor(MEDIA_THREADS) as pool:
        sizes = list(pool.map(bring, plan))

    # Record
    got = [(item, size) for item, size in zip(plan, sizes) if size is not None]
    moved = False
    for (entry, _, target), _ in got:
        if entry["file"] != str(target):
            entry["file"] = str(target)
            moved = True
    if moved:
        tmp = path.with_name(path.name + ".tmp")
        lib.save_json(tmp, data)
        os.replace(tmp, path)
    log.line(f"[worker] chapter {n}: {len(got):,} of the {len(plan):,} takes this node lacked come from the app, "
             f"{sum(size for _, size in got) / 1e6:,.1f} MB fetched"
             + (f". The app has no copy of {len(plan) - len(got):,}" if len(got) < len(plan) else ""))
    return len(got)


def child_env(config: dict, work: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in CHILD_ENV_DROP}
    env.update({str(k): str(v) for k, v in (config.get("env") or {}).items()})
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(work)})
    return env


def claude_sign_in(env: dict) -> tuple[bool, str]:
    """Checks that a job which must sign in to Claude with a token has one.

    env: the CLI's environment, a dict of strings.
    Returns (ok, a line for the job log). The line is empty when SERIAL_AUDIO_CLAUDE_AUTH is unset. Any value of
    it but "token" is refused.
    """
    want = env.get(CLAUDE_AUTH) or ""
    if not want:
        return True, ""
    if want != "token":
        return False, f"[worker] {CLAUDE_AUTH} is {want!r}, and the only value it takes is 'token'"
    if (env.get(CLAUDE_TOKEN) or "").strip():
        return True, f"[worker] Claude signs in with the token in {CLAUDE_TOKEN}"
    return False, f"[worker] this job must sign in to Claude with a token, and {CLAUDE_TOKEN} is not set (deploy/README.md)"


def kill_tree(proc: subprocess.Popen) -> None:
    """Stops the step's process and everything it started. Never raises."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def run_step(args: list[str], work: Path, env: dict, log: Log, stop: threading.Event) -> int:
    """Runs `python -m serial_audio.cli <args>` in the work folder. Returns its exit code, or -1 when stopped."""
    log.line(f"$ python -m serial_audio.cli {subprocess.list2cmdline(args)}")
    kwargs = ({"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW} if os.name == "nt"
              else {"start_new_session": True})
    try:
        proc = subprocess.Popen([sys.executable, "-u", "-m", "serial_audio.cli", *args], cwd=str(work), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    except OSError as e:
        log.line(f"could not start: {e}")
        return 127

    def copy():
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            log.write(chunk)

    reader = threading.Thread(target=copy, name="step-output", daemon=True)
    reader.start()
    stopped = False
    while proc.poll() is None:
        if stop.is_set() and not stopped:
            stopped = True
            log.line("[worker] stopping the step")
            kill_tree(proc)
        time.sleep(0.5)
    reader.join(timeout=30)
    return -1 if stopped else proc.returncode


def wait_for_machine(comfy_url: str, log: Log, stop: threading.Event, held: "Held | None" = None) -> None:
    """Returns once nothing outside this job uses the GPU, or when the job is stopped.

    held: what the worker holds between jobs. The processes it keeps up are ended before the wait, so whatever is
        outside has the card to itself.
    """
    last, said = None, 0.0
    while not stop.is_set():
        reason = machine_busy(comfy_url, held.kept_pids() if held else ())
        if reason is None:
            if last:
                log.line("[worker] the machine is free")
            return
        if held is not None:
            held.end_kept(log, "so what is outside this job has the card")
        if reason != last or time.time() - said > BUSY_REPORT:
            log.line(f"[worker] waiting: {reason}")
            last, said = reason, time.time()
        stop.wait(BUSY_POLL)


def _try_lock(fh) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def gpu_lock(log: Log, stop: threading.Event, path: Path = GPU_LOCK):
    """The machine's GPU lock, held until the returned file closes or this process ends. Waits while another
    serial_audio GPU job holds it. Returns None when the job is stopped first or the lock file cannot be opened."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+b")
    except OSError as e:
        log.line(f"[worker] no GPU lock at {path}, running without it: {e}")
        return None
    said = False
    while not stop.is_set():
        if _try_lock(fh):
            if said:
                log.line("[worker] the other GPU job finished")
            return fh
        if not said:
            log.line("[worker] waiting: another serial_audio GPU job runs on this machine")
            said = True
        stop.wait(LOCK_POLL)
    fh.close()
    return None


class Held:
    """What a worker holds from one job to the next: the machine's GPU lock, and the processes its steps left up."""

    def __init__(self, lock_path: Path = GPU_LOCK):
        self.lock_path = lock_path
        self.path = lock_path.with_suffix(".kept.json")
        self.lock = None
        self.registry: Path | None = None
        self.job = None
        self.halt = threading.Event()
        self.code: str | None = None

    def take(self, log: Log, stop: threading.Event) -> None:
        """Takes the GPU lock unless this worker has it, and starts the registry of kept processes.

        log: the job log.
        stop: when it is set, the wait for the lock ends without the lock.
        A process the registry still records is one an earlier worker left behind, and is ended. Never raises.
        Without a registry the steps don't keep anything up.
        """
        if self.lock is not None:
            return
        self.lock = gpu_lock(log, stop, self.lock_path)
        if self.lock is None:
            return
        try:
            left = kept.end_all(self.path)
            if left:
                log.line(f"[worker] ended what an earlier job left running: {', '.join(left)}")
            self.job = kept.open_registry(self.path)
            self.registry = self.path
        except OSError as e:
            log.line(f"[worker] no registry at {self.path}, so the steps don't keep anything up between jobs: {e}")

    def kept_pids(self) -> set[int]:
        """The ids of the processes this worker keeps up. Never raises."""
        if self.registry is None:
            return set()
        found = kept.load(self.registry)["kept"].values()
        return {f["pid"] for f in found if isinstance(f, dict) and isinstance(f.get("pid"), int)}

    def end_kept(self, log: Log | None = None, why: str = "") -> None:
        """Ends the processes this worker keeps up. Never raises."""
        if self.registry is None:
            return
        ended = kept.end_all(self.registry)
        if ended and log is not None:
            log.line(f"[worker] stopped {', '.join(ended)}" + (f", {why}" if why else ""))

    def release(self, log: Log | None = None, why: str = "") -> None:
        """Ends the kept processes and gives up the GPU lock. Never raises."""
        self.end_kept(log, why)
        kept.close_job(self.job)
        self.job, self.registry = None, None
        if self.lock is not None:
            try:
                self.lock.close()
            except OSError:
                pass
            self.lock = None


def comfy_marker(info: dict) -> str | None:
    """The text that marks this job's ComfyUI prompts in their graphs, or None for a job that queues none."""
    slug, kind = info.get("slug") or "", info.get("kind")
    chapters = info.get("chapters") or []
    if kind == "render" and chapters:
        return f"{slug}/ch{int(chapters[0]):04d}"
    if kind == "cast":
        return f"cast/{slug}/"
    if kind == "check-takes" and chapters:
        return f"breeze/{slug}/ch{int(chapters[0]):04d}/"
    return None


def cancel_comfy(comfy_url: str, marker: str) -> list[str]:
    """Cancels the running and pending ComfyUI prompts whose graph holds marker. Returns their ids. Never raises."""
    try:
        with urllib.request.urlopen(f"{comfy_url}/queue", timeout=10) as resp:
            queue = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    done = []
    for item in (queue.get("queue_running") or []) + (queue.get("queue_pending") or []):
        if len(item) > 2 and marker in json.dumps(item[2]):
            req = urllib.request.Request(f"{comfy_url}/api/jobs/{item[1]}/cancel", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if json.loads(resp.read().decode("utf-8") or "{}").get("cancelled"):
                        done.append(str(item[1]))
            except (urllib.error.URLError, OSError, ValueError):
                pass
    return done


def send_result(app: App, data: bytes, log: Log, next_code: str | None = None, waiting=None) -> dict | None:
    """Delivers the result, retrying while the app is unreachable.

    next_code: the version of the code this worker runs, when it would take another job after this one.
    waiting: called once, before the first retry, so the caller can give up what it holds for a next job.
    Returns the app's report, or None. The report names the job the app hands this worker next under "next".
    """
    headers = {NEXT_HEADER: next_code} if next_code else None
    for attempt in range(RESULT_TRIES):
        try:
            return app.request("POST", "result", data, timeout=300, headers=headers)
        except AppError as e:
            if e.status is not None and e.status < 500:
                log.line(f"[worker] the app refused the result: {e}")
                return None
            if attempt == 0 and waiting is not None:
                waiting()
            if attempt == 0 or attempt % 10 == 9:
                log.line(f"[worker] result not delivered yet, retrying: {e}")
            time.sleep(RESULT_RETRY)
    log.line("[worker] gave up delivering the result")
    return None


def fetch_next(app_url: str, following: dict, node: str, folder: Path, halt: threading.Event) -> Path | None:
    """Fetches and unpacks the bundle of the job the app handed this worker.

    app_url: the web app's address.
    following: the job, as {"job", "key"}.
    node: this node's name.
    folder: an empty or missing folder for the job. The bundle goes to its work folder.
    halt: when it is set, the fetch ends without the bundle.
    Returns the work folder, or None when the job no longer wants to run or its bundle did not arrive. The app
    queues the job again when this task ends without having fetched it. Never raises.
    """
    app = App(app_url, str(following.get("job")), str(following.get("key")), node, 0)
    for attempt in range(BUNDLE_TRIES):
        if halt.is_set():
            return None
        try:
            data = app.fetch(f"/api/worker/{app.job}/bundle", timeout=300)
            work = folder / "work"
            zipfile.ZipFile(io.BytesIO(data)).extractall(work)
            return work
        except AppError as e:
            if e.status is not None and e.status < 500:
                print(f"[worker] the next job {app.job} is not for this worker: {e}", flush=True)
                return None
            print(f"[worker] the next job's bundle not fetched, try {attempt + 1} of {BUNDLE_TRIES}: {e}", flush=True)
            halt.wait(BUNDLE_RETRY)
        except (OSError, zipfile.BadZipFile) as e:
            print(f"[worker] the next job's bundle could not be unpacked: {e}", file=sys.stderr, flush=True)
            return None
    return None


def main(app_url: str, job: str, key: str, work: str | Path) -> int:
    """Runs the job the task was dispatched for, and after it each job the app hands this worker with a result.

    app_url: the web app's address.
    job, key: the app's job id and the key it gave this job.
    work: the folder the bundle was unpacked into.
    Returns 0 when every step of the last job succeeded and the app took the result, else non-zero.
    """
    held = Held()
    work, first = Path(work), Path(work)
    node = os.environ.get("SERIAL_AUDIO_NODE") or platform.node()
    if node.startswith("${"):
        node = platform.node()
    count = 1
    try:
        while True:
            code, following = run_job(app_url, job, key, work, node, held)
            if not following or held.halt.is_set():
                return code
            count += 1
            nxt = fetch_next(app_url, following, node, first.parent / f"next-{count}", held.halt)
            if nxt is None:
                return code
            # the code stays for the processes the steps left up
            shutil.rmtree(work / "library", ignore_errors=True)
            job, key, work = str(following["job"]), str(following["key"]), nxt
    finally:
        held.release()


def run_job(app_url: str, job: str, key: str, work: Path, node: str, held: Held) -> tuple[int, dict | None]:
    """Runs one job step from an unpacked bundle.

    app_url: the web app's address.
    job, key: the app's job id and the key it gave this job.
    work: the folder the bundle was unpacked into.
    node: this node's name.
    held: what the worker holds between jobs. A GPU job takes the GPU lock into it, and the lock and the processes
        the steps left up stay in it only while this worker may get another job. A job whose bundle has other code
        than the worker's first one ends those processes before its steps, its steps keep nothing up, and the
        worker asks for no job after it. The processes also end when the app doesn't take the result at once.
    Returns (code, following). code is 0 when every step succeeded and the app took the result, else non-zero.
    following is the job the app handed this worker next, as {"job", "key"}, or None.
    """
    # Setup
    info = json.loads((work / exchange.BUNDLE_FILE).read_text(encoding="utf-8"))
    app = App(app_url, job, key, node, int(info.get("stage") or 0))
    log_path = work.parent / "job.log"
    log = Log(log_path)
    pump = LogPump(app, log_path)
    stop = pump.cancel

    def halted(*_):
        held.halt.set()
        stop.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), halted)
    if held.halt.is_set():
        stop.set()
    pump.start()
    config = node_config()
    log.line(f"[worker] {info.get('title') or job} on {node}, stage {app.headers[STAGE_HEADER]}, code {info.get('code')}")
    if held.code is None:
        held.code = str(info.get("code") or "")
    same_code = bool(held.code) and str(info.get("code") or "") == held.code
    if not same_code:
        held.end_kept(log, "because this job's code is not the code they run")

    # Steps
    comfy_url = config.get("comfy_url") or COMFY_URL
    if info.get("gpu"):
        held.take(log, stop)
        wait_for_machine(comfy_url, log, stop, held)
    env = child_env(config, work)
    if info.get("gpu") and held.registry is not None and same_code:
        env[kept.ENV] = str(held.registry)
    folder = work / "library" / info["slug"]
    if info.get("takes") and not stop.is_set():
        from . import machine
        root = Path(env.get("SERIAL_AUDIO_COMFY_OUTPUT", "").strip() or machine.COMFY_OUTPUT)
        fetch_takes(app, folder, info["slug"], info.get("chapters") or [], root, log)
    code = 0
    steps = info.get("steps") or []
    signed_in, line = claude_sign_in(env)
    if line:
        log.line(line)
    if not signed_in:
        code, steps = 1, []
    for args in steps:
        if stop.is_set():
            break
        code = run_step(list(args), work, env, log, stop)
        if code != 0:
            break
    cancelled = stop.is_set()
    marker = comfy_marker(info) if cancelled else None
    if marker:
        ids = cancel_comfy(comfy_url, marker)
        if ids:
            log.line(f"[worker] cancelled ComfyUI prompt(s) {', '.join(ids)}")
    more = (code == 0 and not cancelled and bool(info.get("gpu")) and info.get("class") in NEXT_CLASSES
            and held.lock is not None and same_code)
    if not more:
        held.release(log)

    # Result
    changed = exchange.changes(folder, info.get("manifest") or {})
    log.line(f"[worker] {'stopped' if cancelled else f'exit {code}'}, {len(changed)} file(s) to send back")
    sync = bool(info.get("sync"))
    sent = send_media(app, media_files(folder, None if sync else changed), log, stop if sync else None)
    if sync:
        cancelled = stop.is_set()
        if not sent["sent"] and not sent["failed"]:
            log.line(f"[worker] the app already holds all {sent['kept']:,} audio file(s) this node has for the serial"
                     if sent["kept"] else "[worker] this node holds no audio for the serial")
        if sent["failed"] and code == 0:
            code = 1
    pump.stop()
    pump.join(timeout=LOG_EVERY + 35)
    for _ in range(10):
        if pump.flush():
            break
        time.sleep(5)
    data = exchange.build_result(folder, {"job": job, "stage": info.get("stage") or 0, "exit_code": code,
                                          "cancelled": cancelled, "node": node, "finished": now_iso()}, changed)
    report = send_result(app, data, log, held.code if more and not stop.is_set() else None,
                         lambda: held.end_kept(log, "while the app doesn't answer"))
    following = report.get("next") if isinstance(report, dict) and isinstance(report.get("next"), dict) else None
    if report is not None:
        shown = {k: v for k, v in report.items() if k != "next"}
        print(f"[worker] the app took the result: {json.dumps(shown)[:2000]}", flush=True)
        if following:
            print(f"[worker] the app handed this worker its next job, {following.get('job')}", flush=True)
    log.close()
    if not following:
        held.release()
    if report is None:
        return 3, None
    return (0 if code == 0 and not cancelled else (code if code > 0 else 1)), following
