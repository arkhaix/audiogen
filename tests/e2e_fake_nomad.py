"""End-to-end test of the Nomad executor against a fake Nomad.

Copies the code into a temporary folder, writes a small serial there, and runs the web app's NomadJobManager and
HTTP server there. A fake Nomad HTTP server starts deploy/nomad/bootstrap.py as a local process for each dispatch,
the way a raw_exec task would, so the real bootstrap, worker, bundle, log stream and result upload all run.
Run from the repo root: python tests/e2e_fake_nomad.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SECOND = "fake-node-2"
REGISTERED = {"serial-audio-breeze", "serial-audio-comfyui", "serial-audio-llm", "serial-audio-breeze-fake-node",
              "serial-audio-comfyui-fake-node", f"serial-audio-breeze-{SECOND}"}
SLUG = "demo"
SERIAL = {
    "serial.yaml": "title: Demo\nauthor: Test\nsource:\n  type: epub\nnarration:\n  point_of_view: third_person\n"
                   "  narrator_character_id: ''\n",
    "bible/characters.yaml": "captain:\n  name: The Captain\n  importance: major\n  gender: male\n  age_group: adult\n"
                             "  role: Commands the ship.\n  voice_brief: A deep, calm voice.\n  speaks: true\n"
                             "  chapters_present: [1]\n",
    "chapters/0001/chapter.yaml": "number: 1\nlabel: Chapter 1\ntitle: The Ship\nword_count: 12\nstatus: characters\n"
                                  "characters_present: [captain]\n",
    "chapters/0001/text.md": "The captain looked out at the sea.\n\n\"We sail at dawn,\" he said.\n",
}


def make_serial(folder: Path) -> None:
    """Writes the SERIAL files under folder: one chapter after the character pass, with a bible."""
    for rel, text in SERIAL.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeNomad:
    """Answers the Nomad API calls the app makes and runs each dispatch as a local bootstrap process."""

    def __init__(self, root: Path, app_url: str, config: Path):
        self.root, self.app_url, self.config = root, app_url, config
        self.children: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}
        self.delay_next = 0.0
        self.lock = threading.Lock()
        # the second node offers breeze jobs only, and Nomad lists it once a scenario switches it on
        self.second = False

    def dispatch(self, parent: str, body: dict) -> dict:
        with self.lock:
            token = body.get("IdempotencyToken")
            if token in self.tokens:
                return {"DispatchedJobID": self.tokens[token], "EvalID": "e"}
            child = f"{parent}/dispatch-{int(time.time())}-{uuid.uuid4().hex[:8]}"
            self.tokens[token] = child
            entry = {"parent": parent, "meta": body["Meta"], "proc": None, "alloc": uuid.uuid4().hex,
                     "node": SECOND if parent.endswith("-" + SECOND) else "fake-node",
                     "created": time.time_ns(), "task": self.root / "alloc" / child.replace("/", "_")}
            self.children[child] = entry
            delay, self.delay_next = self.delay_next, 0.0
        threading.Timer(delay, self._start, args=(entry,)).start()
        return {"DispatchedJobID": child, "EvalID": "e"}

    def _start(self, entry: dict) -> None:
        task = entry["task"]
        (task / "local").mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / "deploy" / "nomad" / "bootstrap.py", task / "local" / "bootstrap.py")
        lock = "gpu.lock" if entry["node"] == "fake-node" else f"gpu-{entry['node']}.lock"
        env = {**os.environ, "SERIAL_AUDIO_APP": self.app_url, "SERIAL_AUDIO_NODE": entry["node"],
               "SERIAL_AUDIO_CONFIG": str(self.config), "NOMAD_TASK_DIR": str(task / "local"),
               "SERIAL_AUDIO_GPU_LOCK": str(self.root / lock),
               "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               **{f"NOMAD_META_{k}": v for k, v in entry["meta"].items()}}
        out = (task / "stdout.log").open("wb")
        err = (task / "stderr.log").open("wb")
        entry["proc"] = subprocess.Popen([sys.executable, "-u", "local/bootstrap.py"], cwd=str(task), env=env,
                                         stdout=out, stderr=err)

    def allocations(self, child: str) -> list[dict] | None:
        entry = self.children.get(child)
        if entry is None:
            return None
        proc = entry["proc"]
        if proc is None:
            return []
        code = proc.poll()
        status = "running" if code is None else ("complete" if code == 0 else "failed")
        return [{"ID": entry["alloc"], "NodeName": entry["node"], "ClientStatus": status, "CreateTime": entry["created"],
                 "TaskStates": {"work": {"Events": [{"Type": "Terminated", "DisplayMessage": f"Exit Code: {code}"}]}}}]

    def serve(self, port: int) -> ThreadingHTTPServer:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, data, status=200, raw=False):
                body = data if raw else json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
                if path == "/v1/nodes":
                    names = [("n1", "fake-node")] + ([("n2", SECOND)] if fake.second else [])
                    return self.reply([{"ID": ident, "Name": name, "Status": "ready", "Drain": False,
                                        "SchedulingEligibility": "eligible", "ModifyIndex": 1}
                                       for ident, name in names])
                if path == "/v1/node/n2":
                    return self.reply({"ID": "n2", "Name": SECOND, "Attributes": {"kernel.name": sys.platform},
                                       "Meta": {"serial_audio": "breeze", "busy": "false"}})
                if path == "/v1/node/n1":
                    return self.reply({"ID": "n1", "Name": "fake-node", "Attributes": {"kernel.name": sys.platform},
                                       "Meta": {"serial_audio": "breeze,comfyui,llm", "busy": "false"}})
                if path.startswith("/v1/job/") and path.endswith("/allocations"):
                    allocs = fake.allocations(path[len("/v1/job/"):-len("/allocations")])
                    return self.reply(allocs) if allocs is not None else self.reply({"error": "no job"}, 404)
                if path.startswith("/v1/job/") and path.endswith("/evaluations"):
                    return self.reply([])
                if path.startswith("/v1/client/fs/logs/"):
                    alloc = path.rsplit("/", 1)[1]
                    entry = next((e for e in fake.children.values() if e["alloc"] == alloc), None)
                    data = (entry["task"] / "stderr.log").read_bytes()[-6000:] if entry else b""
                    return self.reply(data, raw=True)
                self.reply({"error": path}, 404)

            def do_POST(self):
                path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if path.startswith("/v1/job/") and path.endswith("/dispatch"):
                    parent = path[len("/v1/job/"):-len("/dispatch")]
                    if parent not in REGISTERED:
                        return self.reply({"error": "job not found"}, 404)
                    return self.reply(fake.dispatch(parent, body))
                self.reply({"error": path}, 404)

        httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd


class FakeComfy:
    """A ComfyUI that holds one running prompt for the slug's chapter 1 and records cancel requests."""

    def __init__(self, slug: str):
        self.slug = slug
        self.cancelled: list[str] = []

    def serve(self, port: int) -> ThreadingHTTPServer:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, data):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/queue"):
                    graph = {"1": {"inputs": {"filename_prefix": f"breeze/{fake.slug}/ch0001/l0001"}}}
                    return self.reply({"queue_running": [[0, "prompt-1", graph]], "queue_pending": []})
                self.reply({"jobs": []})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path.startswith("/api/jobs/") and self.path.endswith("/cancel"):
                    fake.cancelled.append(self.path.split("/")[3])
                    return self.reply({"cancelled": True})
                self.reply({})

        httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd


def inside(slug: str) -> int:
    """Runs the scenarios from inside the temporary copy, where library.ROOT is that copy."""
    sys.path.insert(0, os.getcwd())
    from serial_audio import library as lib
    from serial_audio.web import exchange, server, views
    from serial_audio.web.nomad import NomadClient, NomadJobManager

    # Setup
    root = Path(os.getcwd())
    nomad_port, app_port, comfy_port = free_port(), free_port(), free_port()
    app_url = f"http://127.0.0.1:{app_port}"
    comfy = FakeComfy(slug)
    comfy.serve(comfy_port)
    config = root / "node.json"
    output = root / "node-output"
    config.write_text(json.dumps({"comfy_url": f"http://127.0.0.1:{comfy_port}",
                                  "env": {"SERIAL_AUDIO_COMFY_OUTPUT": str(output)}}), encoding="utf-8")
    fake = FakeNomad(root, app_url, config)
    fake.serve(nomad_port)
    m = NomadJobManager(NomadClient(f"http://127.0.0.1:{nomad_port}", "fake-token"), state_dir=root / "state", autostart=False)
    server.Handler.manager = m
    views.MEDIA_MIRROR = m.media_dir
    server.Handler.machine_name = socket.gethostname().lower()
    server.Handler.allowed_names = {"127.0.0.1", "localhost"}
    httpd = server.Server(("127.0.0.1", app_port), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    failures = []

    def check(ok, what):
        print(("PASS " if ok else "FAIL ") + what, flush=True)
        if not ok:
            failures.append(what)

    def run_until(pred, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            m.tick()
            if pred():
                return True
            time.sleep(0.5)
        return False

    def submit(kind, params, args=None, stages=None):
        job = m.submit([{"kind": kind, "params": params}])[0]
        if args is not None:
            job["args"] = args
        if stages is not None:
            job["nomad"] = {"key": uuid.uuid4().hex, "stage": 0, "stages": stages}
        return job

    def log(job):
        return m.log_path(job).read_text(encoding="utf-8", errors="replace")

    def finished(job):
        return lambda: job["status"] in ("done", "failed", "stopped", "cancelled", "lost", "skipped")

    serial_before = (lib.serial_dir(slug) / "serial.yaml").read_text(encoding="utf-8")

    # A step that writes library files: its changes come back, deletions do not
    job = submit("character-pass", {"slug": slug, "chapters": "1"}, args=["reset-bible", slug])
    check(run_until(finished(job)), "reset-bible step finished")
    text = log(job)
    check(job["status"] == "done", f"reset-bible step done (status {job['status']}, note {job.get('note')})")
    check(f"$ python -m serial_audio.cli reset-bible {slug}" in text, "the log carries the worker's command line")
    check("serial.yaml" in ((job["nomad"].get("reports") or {}).get("0") or {}).get("applied", []),
          "serial.yaml was written back")
    check((lib.serial_dir(slug) / "serial.yaml").read_text(encoding="utf-8") != serial_before, "the library copy changed")
    check("fake-node fetched the bundle" in text, "the log records the bundle fetch")
    check(all("key" not in (j.get("nomad") or {}) for j in m.snapshot()["jobs"]), "the snapshot hides the job keys")
    timed = ((m.placement.get("timing") or {}).get("fake-node") or {}).get("character-pass/llm") or []
    check(len(timed) == 1 and 0 <= timed[0][1] < 120,
          f"the app timed the step from the worker's command line to the result ({m.placement.get('timing')})")
    check("step" in (job["nomad"].get("marks") or {}), f"the job records when its step began ({job['nomad'].get('marks')})")

    # A failing step fails the job and writes nothing back
    job = submit("screenplay", {"slug": slug, "chapters": "1"}, args=["no-such-command"])
    check(run_until(finished(job)), "failing step finished")
    check(job["status"] == "failed" and job.get("exit_code") not in (0, None),
          f"failing step failed (status {job['status']}, exit {job.get('exit_code')}, note {job.get('note')})")

    # Two stages run one after the other in one job
    job = submit("screenplay", {"slug": slug, "chapters": "1"}, stages=[
        {"class": "llm", "args": ["chapters", slug], "label": "first"},
        {"class": "llm", "args": ["bible", slug], "label": "second"}])
    check(run_until(finished(job)), "two-stage job finished")
    text = log(job)
    check(job["status"] == "done" and job["nomad"]["stage"] == 1, f"two-stage job done at stage 2 ({job['status']})")
    check(f"cli chapters {slug}" in text and f"cli bible {slug}" in text, "both stages' commands are in the log")

    # Backlog work queued ahead of another job runs after it, one job at a time
    trio = m.submit([{"kind": "render", "params": {"slug": slug, "chapter": 2, "backlog": True}},
                     {"kind": "render", "params": {"slug": slug, "chapter": 3, "backlog": True}},
                     {"kind": "render", "params": {"slug": slug, "chapter": 4}}], stop_on_failure=False)
    for job in trio:
        job["args"] = ["chapters", slug]
    check(run_until(lambda: all(finished(j)() for j in trio)), "the backlog jobs and the other job finished")
    check(all(j["status"] == "done" for j in trio), f"all three are done ({[j['status'] for j in trio]})")
    sent = [j["nomad"].get("dispatched_at") or "" for j in trio]
    check(sent[2] <= sent[0] <= sent[1] and trio[2]["finished"] <= sent[0] and trio[0]["finished"] <= sent[1],
          f"the job that is not backlog work ran first, then the backlog jobs in turn ({sent})")
    check([j.get("backlog") for j in m.snapshot()["jobs"] if j["id"] in {t["id"] for t in trio}].count(True) == 2,
          "the job list marks the two backlog jobs")

    # Stopped before a node starts it: the late worker exits without running anything
    fake.delay_next = 6
    job = submit("screenplay", {"slug": slug, "chapters": "1"}, args=["chapters", slug])
    check(run_until(lambda: bool(job.get("nomad", {}).get("dispatched"))), "delayed job dispatched")
    m.cancel(job["id"])
    check(job["status"] == "cancelled", f"cancelled before contact ({job['status']})")
    entry = fake.children[job["nomad"]["dispatched"]]
    check(run_until(lambda: entry["proc"] is not None and entry["proc"].poll() is not None, 60), "late worker exited")
    out = (entry["task"] / "stdout.log").read_text(encoding="utf-8", errors="replace")
    check(entry["proc"].returncode == 0 and "before it started" in out, f"late worker did nothing ({out.strip()[:200]})")

    # A wrong key is refused
    req = urllib.request.Request(f"{app_url}/api/worker/{job['id']}/log?offset=0", data=b"x", method="POST",
                                 headers={"X-Serial-Audio": "1", "X-Serial-Audio-Key": "wrong"})
    try:
        urllib.request.urlopen(req, timeout=10)
        status = 200
    except urllib.error.HTTPError as e:
        status = e.code
    check(status in (403, 410), f"wrong key refused ({status})")

    # A quick library job runs on the app's own machine, not on the cluster
    dispatched = len(fake.children)
    job = submit("reset-bible", {"slug": slug, "confirm": slug})
    check(run_until(finished(job), 60), "local job finished")
    check(job["status"] == "done" and len(fake.children) == dispatched and not job.get("nomad"),
          f"local job ran here ({job['status']}, note {job.get('note')}, exit {job.get('exit_code')})")
    check(not (lib.serial_dir(slug) / "bible" / "characters.yaml").exists(), "the local reset removed the bible")

    # A chapter whose takes another node made: the worker fetches them from the app, the takes file then names this
    # node's copies, and the app gets those copies under this node's paths
    sep = "/" if os.name == "nt" else "\\"
    far = "/Users/elsewhere/output" if os.name == "nt" else "D:\\elsewhere\\output"

    def far_path(*parts):
        return far + sep + sep.join(parts)

    def wav(samples):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(samples)
        return buf.getvalue()

    def mirrored(raw):
        return m.media_dir / exchange.mirror_rel(raw)

    pcm = [bytes([i + 1]) * 4800 for i in range(3)]
    takes = {str(i): {"key": f"k{i}", "file": far_path("breeze", slug, "ch0001", "cpp", f"l{i:04d}.wav"),
                      "voice": "narrator", "audio": hashlib.sha1(pcm[i]).hexdigest()[:16]} for i in range(3)}
    takes_path = lib.chapter_dir(slug, 1) / "breeze_takes.json"
    lib.save_json(takes_path, {"engine": "breeze", "runner": "cpp", "takes": takes})
    for i in range(3):
        mirrored(takes[str(i)]["file"]).parent.mkdir(parents=True, exist_ok=True)
        mirrored(takes[str(i)]["file"]).write_bytes(wav(pcm[i]))
    job = submit("check-takes", {"slug": slug, "chapter": 1}, args=["chapters", slug])
    check(run_until(finished(job)), "job on another node's takes finished")
    text = log(job)
    check(job["status"] == "done", f"job on another node's takes done ({job['status']}, note {job.get('note')})")
    check(fake.children[job["nomad"]["dispatched"]]["parent"] == "serial-audio-breeze-fake-node",
          f"a take check is dispatched as a breeze job ({job['nomad'].get('dispatched')})")
    check("3 of the 3 takes this node lacked come from the app" in text, "the log records the fetched takes")
    now = lib.load_json(takes_path)["takes"]
    here = [output / "breeze" / slug / "ch0001" / "cpp" / f"l{i:04d}.wav" for i in range(3)]
    check([now[str(i)]["file"] for i in range(3)] == [str(p) for p in here],
          f"the takes file names this node's copies ({now['0']['file']})")
    check(all(p.is_file() and p.read_bytes() == wav(pcm[i]) for i, p in enumerate(here)),
          "the takes are under the node's output folder")
    check(now["0"]["key"] == "k0" and now["0"]["audio"] == takes["0"]["audio"], "the entries keep their other fields")
    check(all(mirrored(str(p)).is_file() for p in here) and "sent the app 3 audio file(s)" in text,
          "the app holds the takes under this node's paths")
    check(m.placement["chapters"][slug]["1"] == "fake-node", f"the app records the new holder ({m.placement})")

    # A sync job sends the app what its node holds and the app lacks, without a CLI step
    mirrored(str(here[0])).unlink()
    job = m.submit([{"kind": "sync-media", "params": {"slug": slug, "node": "fake-node"}}])[0]
    check(run_until(finished(job)), "sync job finished")
    text = log(job)
    check(job["status"] == "done", f"sync job done ({job['status']}, note {job.get('note')})")
    check(mirrored(str(here[0])).is_file() and mirrored(str(here[0])).read_bytes() == wav(pcm[0]),
          "the sync job restored the file the app lacked")
    check("sent the app 1 audio file(s)" in text and "already had 2" in text and "$ python" not in text,
          f"the sync job sent only what the app lacked and ran no step ({text.strip()[-300:]})")
    try:
        m.submit([{"kind": "sync-media", "params": {"slug": slug, "node": "nowhere"}}])
        refused = False
    except ValueError:
        refused = True
    check(refused, "a sync job for an unknown node is refused")

    # A voice pick runs on the app's machine and files a candidate that only the app's mirror holds
    clip = far_path("breeze", "cast", slug, "captain_s1000.wav")
    mirrored(clip).parent.mkdir(parents=True, exist_ok=True)
    mirrored(clip).write_bytes(wav(pcm[2]))
    voices = lib.serial_dir(slug) / "voices"
    lib.save_yaml(voices / "captain.yaml", {
        "id": "captain", "character_id": "captain", "status": "auto", "engine": "breeze", "instruct": "A deep voice.",
        "sample_text": "We sail at dawn.", "seed": 999, "clip": "captain.wav", "chosen": 0,
        "candidates": [{"seed": 999, "clip": far_path("breeze", "cast", slug, "captain_s999.wav"), "score": 1.0},
                       {"seed": 1000, "clip": clip, "score": 0.9}]})
    dispatched = len(fake.children)
    job = submit("pick-voice", {"slug": slug, "character": "captain", "seed": 1000})
    check(run_until(finished(job), 60), "voice pick finished")
    check(job["status"] == "done" and len(fake.children) == dispatched and not job.get("nomad"),
          f"the voice pick ran here ({job['status']}, note {job.get('note')}, exit {job.get('exit_code')})")
    filed = lib.load_yaml(voices / "captain.yaml") or {}
    check((filed.get("seed"), filed.get("status"), filed.get("chosen")) == (1000, "approved", 1),
          f"the pick filed the candidate ({filed.get('seed')}, {filed.get('status')})")
    check((voices / "captain.wav").is_file() and (voices / "captain.wav").read_bytes() == wav(pcm[2]),
          "the voice's clip comes from the mirror")

    # Two breeze jobs queued together run in one task: the worker of the first is handed the second with its result
    dispatched = len(fake.children)
    pair = m.submit([{"kind": "render", "params": {"slug": slug, "chapter": 5}},
                     {"kind": "render", "params": {"slug": slug, "chapter": 6}}], stop_on_failure=False)
    for job in pair:
        job["args"] = ["chapters", slug]
    check(run_until(lambda: all(finished(j)() for j in pair)), "the two renders finished")
    first, second = pair
    check(all(j["status"] == "done" for j in pair), f"both are done ({[(j['status'], j.get('note')) for j in pair]})")
    check(len(fake.children) == dispatched + 1, f"one Nomad job ran the two ({len(fake.children) - dispatched})")
    check(second["nomad"].get("dispatched") == first["nomad"].get("dispatched")
          and second["nomad"].get("follows") == first["id"],
          f"the second job runs under the first one's Nomad job ({second['nomad'].get('dispatched')})")
    text = log(second)
    check("handed to the worker on fake-node as it finished" in text and "fake-node fetched the bundle" in text
          and f"$ python -m serial_audio.cli chapters {slug}" in text and "[web] dispatched" not in text,
          f"the second job's log says how it started and has its step ({text.strip()[:300]})")
    check("next" not in json.dumps(m.snapshot()["jobs"]) or all(
        "key" not in json.dumps((j.get("nomad") or {}).get("reports") or {}) for j in m.snapshot()["jobs"]),
        "no report in the snapshot holds a job key")
    entry = fake.children[first["nomad"]["dispatched"]]
    check(run_until(lambda: entry["proc"].poll() is not None, 60) and entry["proc"].returncode == 0,
          f"the task ended once the app had no further job for it (exit {entry['proc'].returncode})")
    out = (entry["task"] / "stdout.log").read_text(encoding="utf-8", errors="replace")
    check(f"the app handed this worker its next job, {second['id']}" in out, "the task's output names the handed job")
    check((entry["task"] / "local" / "next-2" / "work" / "bundle.json").is_file()
          and not (entry["task"] / "local" / "work" / "library").exists(),
          "the second job has a work folder of its own, and the first one's library copy is gone")
    kept_file = root / "gpu.kept.json"
    check(kept_file.is_file() and json.loads(kept_file.read_text(encoding="utf-8")).get("kept") == {},
          "the worker's registry of kept processes is empty after the task")

    # A GPU job waits while something outside it uses the machine, and stops when asked
    if os.name == "nt":
        dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "serial_audio.cli", "render",
                                  "dummy", "1"])
        try:
            job = submit("render", {"slug": slug, "chapter": 1}, args=["chapters", slug])
            check(run_until(lambda: "[worker] waiting:" in (log(job) if m.log_path(job).exists() else ""), 90),
                  "GPU job waits for the outside render")
            p = m.progress(job)
            check(bool(p) and p.get("phase") == "waiting", f"progress shows the wait ({p})")
            check(fake.children[job["nomad"]["dispatched"]]["parent"] == "serial-audio-breeze-fake-node",
                  f"GPU job dispatched pinned to its node ({job['nomad'].get('dispatched')})")
            m.cancel(job["id"])
            check(run_until(finished(job), 60), "stopped GPU job finished")
            check(job["status"] == "stopped", f"GPU job stopped ({job['status']}, note {job.get('note')})")
            check(comfy.cancelled == ["prompt-1"], f"the stop cancelled the job's ComfyUI prompt ({comfy.cancelled})")
        finally:
            dummy.kill()

    # A backlog render gives its node to a job that is not backlog work, and runs again after it
    if os.name == "nt":
        fake.second = True
        check(run_until(lambda: {w["name"] for w in m.workers} == {"fake-node", SECOND}, 60),
              f"the app sees the second node ({[w['name'] for w in m.workers]})")
        dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "serial_audio.cli", "render",
                                  "dummy", "1"])
        try:
            pair = m.submit([{"kind": "render", "params": {"slug": slug, "chapter": 2, "backlog": True}},
                             {"kind": "render", "params": {"slug": slug, "chapter": 3, "backlog": True}}],
                            stop_on_failure=False)
            for job in pair:
                job["args"] = ["chapters", slug]
            check(run_until(lambda: all((j.get("nomad") or {}).get("contact") for j in pair), 90),
                  "two backlog renders run beside each other")
            check({(j.get("nomad") or {}).get("node") for j in pair} == {"fake-node", SECOND},
                  f"one on each node ({[(j.get('nomad') or {}).get('node') for j in pair]})")
            other = submit("render", {"slug": slug, "chapter": 4}, args=["chapters", slug])
            check(run_until(lambda: any(j.get("evicted") for j in pair), 90),
                  f"one backlog render was paused and queued again ({[(j['status'], j.get('note')) for j in pair]})")
            gave = next((j for j in pair if j.get("evicted")), pair[0])
            stayed = pair[1] if gave is pair[0] else pair[0]
            check(gave["status"] == "queued" and gave.get("backlog") and "nomad" not in gave,
                  f"it is queued as backlog work, as before it started ({gave['status']}, {gave.get('note')})")
            check("paused for Render" in log(gave), "its log says which job it was paused for")
            check(stayed["status"] == "running" and not stayed["nomad"].get("cancel"),
                  f"the other backlog render was not asked to stop ({stayed['status']})")
            check(run_until(lambda: (other.get("nomad") or {}).get("contact"), 90) and stayed["status"] == "running",
                  f"the job that is not backlog work got the freed node while a backlog render still ran "
                  f"({(other.get('nomad') or {}).get('node')})")
        finally:
            dummy.kill()
        check(run_until(lambda: all(finished(j)() for j in pair + [other]), 180), "all three finished once the machine was free")
        check([j["status"] for j in pair + [other]] == ["done"] * 3, f"all three are done ({[j['status'] for j in pair + [other]]})")
        check(gave.get("evicted") is True and other["finished"] <= (gave["nomad"].get("dispatched_at") or ""),
              f"the paused render ran again after the other job ({gave.get('evicted')})")
        check("gave_way" not in json.dumps(m.snapshot()["jobs"]), "the snapshot hides the paused attempt's key")

    httpd.shutdown()
    print(f"\n{len(failures)} failure(s)" + (": " + "; ".join(failures) if failures else ""), flush=True)
    return 1 if failures else 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--inside":
        return inside(sys.argv[2])
    with tempfile.TemporaryDirectory(prefix="serial-audio-e2e-") as tmp:
        root = Path(tmp)
        for top in ("serial_audio", "prompts", "schemas", "deploy", "tests"):
            shutil.copytree(REPO / top, root / top, ignore=shutil.ignore_patterns("__pycache__"))
        make_serial(root / "library" / SLUG)
        return subprocess.call([sys.executable, "-u", str(root / "tests" / "e2e_fake_nomad.py"), "--inside", SLUG],
                               cwd=str(root))


if __name__ == "__main__":
    sys.exit(main())
