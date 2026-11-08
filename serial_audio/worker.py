"""The task a cluster node runs for one web app job.

deploy/nomad/bootstrap.py fetches the job's bundle from the web app, unpacks it into a work folder and calls main().
The worker runs the job's CLI steps there against its copy of the serial, sends the log to the app every few
seconds, stops when the app asks, and sends back every file the steps created or changed.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .web import exchange

LOG_EVERY = 3.0
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
CHILD_ENV_DROP = ("CLAUDECODE", "NOMAD_TOKEN")
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

    def request(self, method: str, path: str, data: bytes | None = None, timeout: float = 60) -> dict:
        """The JSON answer to one request. Raises AppError with the HTTP status, or None when unreachable."""
        req = urllib.request.Request(f"{self.url}/api/worker/{self.job}/{path}", data=data, method=method,
                                     headers={**self.headers, "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            raise AppError(e.code, f"the app answered {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
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


def machine_busy(comfy_url: str) -> str | None:
    """Why the GPU on this machine is in use by something outside this job, or None."""
    from .web import jobs, procs
    snap = procs.snapshot()
    ours = procs.descendants(snap, os.getpid()) if snap else {os.getpid()}
    for e in jobs.external_activity(snap, ours, time.time()):
        if e["heavy"]:
            what = f"{e['kind']} chapter {e['chapter']}" if e.get("chapter") else e["kind"]
            return f"an outside {what} is running (pid {e['pid']})"
    from . import machine
    pids = machine.breeze_server_pids()
    if pids:
        return f"a breeze-server is running (pid {', '.join(map(str, pids))})"
    try:
        with urllib.request.urlopen(f"{comfy_url}/api/jobs?status=in_progress,pending&limit=5", timeout=5) as resp:
            items = json.loads(resp.read().decode("utf-8")).get("jobs") or []
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return f"ComfyUI is busy with {len(items)} prompt(s)" if items else None


def media_files(folder: Path, changed: dict) -> list[str]:
    """Node paths of the audio the app plays from ComfyUI's output for the changed files: the candidates of each
    changed voice record, and the takes of a changed chapter that its take check did not pass. Never raises."""
    import yaml
    out = set()
    for rel in changed:
        path = folder / rel
        try:
            if rel.startswith("voices/") and rel.endswith(".yaml") and rel != "voices/_briefs.yaml":
                rec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                for batch in [rec] + list(rec.get("history") or []):
                    out.update(c.get("clip") for c in batch.get("candidates") or [] if isinstance(c, dict))
            elif rel.endswith(("/breeze_takes.json", "/take_check.json")):
                chapter = path.parent
                takes = json.loads((chapter / "breeze_takes.json").read_text(encoding="utf-8")).get("takes") or {}
                check_path = chapter / "take_check.json"
                checks = (json.loads(check_path.read_text(encoding="utf-8")).get("takes") or {}
                          if check_path.is_file() else {})
                for k, take in takes.items():
                    verdict = "out_of_range" if take.get("out_of_range") else (checks.get(k) or {}).get("verdict")
                    if verdict not in (None, "ok"):
                        out.add(take.get("file"))
        except (OSError, ValueError, AttributeError, yaml.YAMLError):
            continue
    return sorted(p for p in out if isinstance(p, str) and Path(p).suffix.lower() in exchange.MEDIA_SUFFIXES
                  and Path(p).is_file())


def child_env(config: dict, work: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in CHILD_ENV_DROP}
    env.update({str(k): str(v) for k, v in (config.get("env") or {}).items()})
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(work)})
    return env


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


def wait_for_machine(comfy_url: str, log: Log, stop: threading.Event) -> None:
    """Returns once nothing outside this job uses the GPU, or when the job is stopped."""
    last, said = None, 0.0
    while not stop.is_set():
        reason = machine_busy(comfy_url)
        if reason is None:
            if last:
                log.line("[worker] the machine is free")
            return
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


def send_result(app: App, data: bytes, log: Log) -> dict | None:
    """Delivers the result, retrying while the app is unreachable. Returns the app's report, or None."""
    for attempt in range(RESULT_TRIES):
        try:
            return app.request("POST", "result", data, timeout=300)
        except AppError as e:
            if e.status is not None and e.status < 500:
                log.line(f"[worker] the app refused the result: {e}")
                return None
            if attempt == 0 or attempt % 10 == 9:
                log.line(f"[worker] result not delivered yet, retrying: {e}")
            time.sleep(RESULT_RETRY)
    log.line("[worker] gave up delivering the result")
    return None


def main(app_url: str, job: str, key: str, work: str | Path) -> int:
    """Runs one job step from an unpacked bundle.

    app_url: the web app's address.
    job, key: the app's job id and the key it gave this job.
    work: the folder the bundle was unpacked into.
    Returns 0 when every step succeeded and the app took the result, else non-zero.
    """
    # Setup
    work = Path(work)
    info = json.loads((work / exchange.BUNDLE_FILE).read_text(encoding="utf-8"))
    node = os.environ.get("SERIAL_AUDIO_NODE") or platform.node()
    if node.startswith("${"):
        node = platform.node()
    app = App(app_url, job, key, node, int(info.get("stage") or 0))
    log_path = work.parent / "job.log"
    log = Log(log_path)
    pump = LogPump(app, log_path)
    stop = pump.cancel
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    pump.start()
    config = node_config()
    log.line(f"[worker] {info.get('title') or job} on {node}, stage {app.headers[STAGE_HEADER]}, code {info.get('code')}")

    # Steps
    comfy_url = config.get("comfy_url") or COMFY_URL
    lock = None
    if info.get("gpu"):
        lock = gpu_lock(log, stop)
        wait_for_machine(comfy_url, log, stop)
    env = child_env(config, work)
    code = 0
    for args in info.get("steps") or []:
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
    if lock is not None:
        lock.close()

    # Result
    folder = work / "library" / info["slug"]
    changed = exchange.changes(folder, info.get("manifest") or {})
    media = media_files(folder, changed)
    log.line(f"[worker] {'stopped' if cancelled else f'exit {code}'}, {len(changed)} file(s) to send back"
             + (f", {len(media)} audio file(s) for the app's media mirror" if media else ""))
    pump.stop()
    pump.join(timeout=LOG_EVERY + 35)
    for _ in range(10):
        if pump.flush():
            break
        time.sleep(5)
    data = exchange.build_result(folder, {"job": job, "stage": info.get("stage") or 0, "exit_code": code,
                                          "cancelled": cancelled, "node": node, "finished": now_iso()}, changed, media)
    report = send_result(app, data, log)
    if report is not None:
        print(f"[worker] the app took the result: {json.dumps(report)[:2000]}", flush=True)
    log.close()
    if report is None:
        return 3
    return 0 if code == 0 and not cancelled else (code if code > 0 else 1)
