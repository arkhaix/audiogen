"""Unit tests for what stays up between jobs: the processes a worker keeps, the recognizer that serves a process's
jobs, breeze-server under a worker's registry, and the next job the app hands a worker.
Run: python -m unittest tests.test_kept
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from serial_audio import asr, breeze, kept, machine, parakeet_asr
from serial_audio import worker as w
from serial_audio.comfy import ComfyError

LISTENER = ("import socket, sys, time\ns = socket.socket()\ns.bind(('127.0.0.1', 0))\ns.listen()\n"
            "print(s.getsockname()[1], flush=True)\ntime.sleep(600)\n")

# A parakeet_asr.py stand-in. With --serve it follows the serving protocol, and without it does one job from stdin.
# A file named die.wav ends a serving process, and a job with "bad" gets "fatal".
SERVING_CHILD = '''
import json, os, socket, sys

def records(job):
    for i, f in enumerate(job.get("files") or []):
        yield {"index": i, "text": "heard " + os.path.basename(f), "by": os.getpid()}
    yield {"done": len(job.get("files") or [])}

if "--serve" not in sys.argv:
    for record in records(json.loads(sys.stdin.read())):
        print(json.dumps(record), flush=True)
    sys.exit(0)
config = json.loads(sys.stdin.readline())
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
print(json.dumps({"port": listener.getsockname()[1], "pid": os.getpid()}), flush=True)
if not config.get("stay"):
    import threading
    def gone():
        sys.stdin.buffer.read()
        os._exit(0)
    threading.Thread(target=gone, daemon=True).start()
while True:
    conn, _ = listener.accept()
    with conn, conn.makefile("rb") as reader:
        line = reader.readline()
        if not line:
            continue
        job = json.loads(line)
        def send(record):
            conn.sendall((json.dumps(record) + "\\n").encode())
        if job.get("token") != config.get("token"):
            send({"fatal": "wrong token"})
        elif job.get("check"):
            send({"loaded": job.get("blocks"), "errors": {}, "threads": job.get("threads")})
        elif job.get("bad"):
            send({"fatal": "the model folder lacks tokens.txt"})
        else:
            for record in records(job):
                if "index" in record and job["files"][record["index"]].endswith("die.wav"):
                    os._exit(3)
                send(record)
'''

# A breeze-server stand-in: python <this script> --host 127.0.0.1 --port <n> ... answers /health.
FAKE_BREEZE = '''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        body = json.dumps({"status": "ok", "sample_rate": 24000}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

print("warmed up", flush=True)
HTTPServer(("127.0.0.1", int(sys.argv[sys.argv.index("--port") + 1])), H).serve_forever()
'''


class Lines:
    def __init__(self):
        self.lines = []

    def line(self, text):
        self.lines.append(text)

    def __call__(self, text):
        self.lines.append(text)


def listener() -> tuple[subprocess.Popen, int]:
    """A process that listens on a port of this machine, and the port."""
    proc = subprocess.Popen([sys.executable, "-c", LISTENER], stdout=subprocess.PIPE)
    return proc, int(proc.stdout.readline())


def gone(proc: subprocess.Popen, within: float = 10.0) -> bool:
    try:
        proc.wait(timeout=within)
        return True
    except subprocess.TimeoutExpired:
        return False


class RegistryCase(unittest.TestCase):
    """A registry as a worker opens it, named in the environment the way the worker names it for its steps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / "gpu.kept.json"
        self.started: list[subprocess.Popen] = []
        self.addCleanup(self.stop_started)

    def stop_started(self):
        for proc in self.started:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()

    def open_registry(self):
        handle = kept.open_registry(self.registry)
        self.addCleanup(kept.close_job, handle)
        patcher = mock.patch.dict(os.environ, {kept.ENV: str(self.registry)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return handle

    def listener(self) -> tuple[subprocess.Popen, int]:
        proc, port = listener()
        self.started.append(proc)
        return proc, port


class KeptTest(RegistryCase):
    def test_a_step_without_a_registry_keeps_nothing(self):
        # No worker named a registry, so a process is not handed over and nothing is found.
        with mock.patch.dict(os.environ):
            os.environ.pop(kept.ENV, None)
            self.assertIsNone(kept.registry())
            self.assertFalse(kept.record("breeze", os.getpid(), 1))
            self.assertIsNone(kept.entry("breeze"))
            kept.forget("breeze")

    def test_a_recorded_process_is_found_while_it_runs_and_ended_with_the_registry(self):
        self.open_registry()
        proc, port = self.listener()

        # Recorded with its port and the facts a later step needs.
        self.assertTrue(kept.record("breeze", proc.pid, port, model="m.gguf"))
        self.assertEqual(kept.entry("breeze"), {"pid": proc.pid, "born": kept.born(proc.pid), "port": port,
                                                "model": "m.gguf"})
        self.assertIsNone(kept.entry("asr"))
        self.assertEqual(kept.load(self.registry)["owner"], os.getpid())

        # Ending the registry's processes ends it and empties the registry.
        self.assertEqual(kept.end_all(self.registry), [f"breeze (pid {proc.pid})"])
        self.assertTrue(gone(proc))
        self.assertEqual(kept.load(self.registry)["kept"], {})
        self.assertEqual(kept.end_all(self.registry), [])

    def test_an_entry_for_another_start_time_is_not_the_process_it_names(self):
        # The id belongs to a process, here this one, that started at another time: it got the id later.
        self.open_registry()
        data = kept.load(self.registry)
        data["kept"]["asr"] = {"pid": os.getpid(), "born": "long ago", "port": 9}
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(kept.recorded("asr"))
        self.assertIsNone(kept.entry("asr"))
        self.assertEqual(kept.end_all(self.registry), [])
        self.assertTrue(kept.alive(os.getpid()))

    def test_a_recorded_process_that_does_not_listen_is_no_use_to_a_step_and_is_still_ended(self):
        # A server that hangs before it listens: no step uses it, and the worker still ends it.
        self.open_registry()
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        proc = subprocess.Popen([sys.executable, "-c", "import time\ntime.sleep(600)\n"])
        self.started.append(proc)
        self.assertTrue(kept.record("breeze", proc.pid, port))
        self.assertEqual(kept.recorded("breeze")["pid"], proc.pid)
        self.assertIsNone(kept.entry("breeze"))
        self.assertEqual(kept.end_all(self.registry), [f"breeze (pid {proc.pid})"])
        self.assertTrue(gone(proc))

    @unittest.skipIf(kept.WINDOWS, "Windows has no file modes of this kind")
    def test_only_the_owner_reads_the_registry(self):
        self.open_registry()
        self.assertEqual(self.registry.stat().st_mode & 0o077, 0)

    def test_a_process_that_cannot_be_ended_stays_in_the_registry(self):
        self.open_registry()
        proc, port = self.listener()
        self.assertTrue(kept.record("breeze", proc.pid, port))
        with mock.patch.object(kept, "end", lambda pid: False):
            self.assertEqual(kept.end_all(self.registry), [])
        self.assertEqual(kept.recorded("breeze")["pid"], proc.pid)
        self.assertEqual(kept.end_all(self.registry), [f"breeze (pid {proc.pid})"])
        self.assertEqual(kept.load(self.registry)["kept"], {})

    def test_what_cannot_be_recorded_is_not_kept_and_what_is_not_a_process_does_not_run(self):
        self.open_registry()
        proc, port = self.listener()
        # A fact the registry can't write, and ids and ports that are not plain numbers.
        self.assertFalse(kept.record("breeze", proc.pid, port, log=self.root))
        self.assertFalse(kept.record("breeze", "12", port))
        self.assertFalse(kept.record("breeze", proc.pid, True))
        self.assertEqual(kept.load(self.registry)["kept"], {})
        self.assertIsNone(kept.born("12"))
        self.assertFalse(kept.alive(True))
        self.assertFalse(kept.runs({"pid": True, "port": port}))
        # A port outside the range is no port a step could use.
        data = kept.load(self.registry)
        data["kept"]["asr"] = {"pid": proc.pid, "born": kept.born(proc.pid), "port": 99999}
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(kept.recorded("asr")["pid"], proc.pid)
        self.assertIsNone(kept.entry("asr"))

    def test_a_registry_that_does_not_parse_is_empty(self):
        self.registry.write_text("{not json", encoding="utf-8")
        self.assertEqual(kept.load(self.registry), {"kept": {}})
        self.assertEqual(kept.load(self.root / "none.json"), {"kept": {}})
        self.assertEqual(kept.end_all(self.registry), [])

    def test_alive_and_end(self):
        proc, _ = self.listener()
        self.assertTrue(kept.alive(proc.pid))
        self.assertTrue(kept.end(proc.pid))
        self.assertTrue(gone(proc))
        self.assertFalse(kept.alive(proc.pid))
        self.assertTrue(kept.end(proc.pid))
        self.assertFalse(kept.alive(0))

    @unittest.skipUnless(kept.WINDOWS, "a job object is how Windows ends a kept process with its worker")
    def test_on_windows_a_kept_process_ends_when_the_worker_lets_go_of_its_job(self):
        handle = kept.open_registry(self.registry)
        self.assertIsNotNone(handle)
        proc, port = self.listener()
        with mock.patch.dict(os.environ, {kept.ENV: str(self.registry)}):
            self.assertTrue(kept.record("breeze", proc.pid, port))
        self.assertIsNone(proc.poll())
        kept.close_job(handle)
        self.assertTrue(gone(proc))

    @unittest.skipUnless(kept.WINDOWS, "only Windows needs the worker's job object")
    def test_on_windows_a_process_that_cannot_join_the_workers_job_is_not_kept(self):
        # A registry without a job, as a worker leaves it when Windows gave none.
        self.registry.write_text(json.dumps({"owner": os.getpid(), "job": None, "kept": {}}), encoding="utf-8")
        proc, port = self.listener()
        with mock.patch.dict(os.environ, {kept.ENV: str(self.registry)}):
            self.assertFalse(kept.record("breeze", proc.pid, port))
            self.assertIsNone(kept.entry("breeze"))


class HeldTest(RegistryCase):
    def test_a_worker_ends_what_an_earlier_worker_left_and_what_it_keeps(self):
        left, left_port = self.listener()
        self.registry.write_text(json.dumps({"owner": 1, "job": None, "kept": {"breeze": {
            "pid": left.pid, "born": kept.born(left.pid), "port": left_port}}}), encoding="utf-8")
        log = Lines()
        held = w.Held(self.root / "gpu.lock")
        held.take(log, threading.Event())
        self.addCleanup(held.release)

        # The process the registry still named is ended before this worker's steps run.
        self.assertIsNotNone(held.lock)
        self.assertTrue(gone(left))
        self.assertTrue(any("an earlier job left running: breeze" in t for t in log.lines), log.lines)
        self.assertEqual(held.kept_pids(), set())

        # A step records a process, and the worker counts it as its own and ends it when it lets go.
        proc, port = self.listener()
        with mock.patch.dict(os.environ, {kept.ENV: str(held.registry)}):
            self.assertTrue(kept.record("asr", proc.pid, port, token="t"))
        self.assertEqual(held.kept_pids(), {proc.pid})
        with mock.patch.object(machine, "breeze_server_pids", lambda: [proc.pid, 4242]), \
                mock.patch.object(w, "jobs", create=True):
            self.assertIn("4242", w.machine_busy("http://127.0.0.1:9", held.kept_pids()))
            self.assertNotIn(str(proc.pid), w.machine_busy("http://127.0.0.1:9", held.kept_pids()))
        held.release(log, "so the card is free")
        self.assertTrue(gone(proc))
        self.assertTrue(any(f"stopped asr (pid {proc.pid}), so the card is free" in t for t in log.lines), log.lines)
        self.assertIsNone(held.lock)
        self.assertIsNone(held.registry)

    def test_the_kept_processes_end_before_a_wait_for_something_outside(self):
        held = w.Held(self.root / "gpu.lock")
        held.take(Lines(), threading.Event())
        self.addCleanup(held.release)
        proc, port = self.listener()
        with mock.patch.dict(os.environ, {kept.ENV: str(held.registry)}):
            self.assertTrue(kept.record("breeze", proc.pid, port))
        answers = iter(["ComfyUI is busy with 1 prompt(s)", None])
        log = Lines()
        with mock.patch.object(w, "machine_busy", lambda *a: next(answers)), mock.patch.object(w, "BUSY_POLL", 0.05):
            w.wait_for_machine("http://127.0.0.1:9", log, threading.Event(), held)
        self.assertTrue(gone(proc))
        self.assertEqual([t for t in log.lines if "stopped breeze" in t],
                         [f"[worker] stopped breeze (pid {proc.pid}), so what is outside this job has the card"])
        self.assertEqual(log.lines[-1], "[worker] the machine is free")


class ServingCase(RegistryCase):
    def setUp(self):
        super().setUp()
        script = self.root / "child.py"
        script.write_text(SERVING_CHILD, encoding="utf-8")
        for p in (mock.patch.object(asr, "PARAKEET_SCRIPT", script),
                  mock.patch.object(machine, "AUDIO_PYTHON", Path(sys.executable)),
                  mock.patch.object(asr, "SERVE", True), mock.patch.object(asr, "_served", None),
                  mock.patch.object(asr, "_serve_starts", 0), mock.patch.object(asr, "extra_blocks", lambda: set())):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.end_serving)

    def end_serving(self):
        if asr._served is not None:
            asr._served.kill()

    def hear(self, *names: str) -> list[tuple[int, dict]]:
        return list(asr.records([Path(n) for n in names], engine="parakeet", comfy_url=""))


class ServingTest(ServingCase):
    def test_one_process_hears_every_call_of_this_process(self):
        # Two calls, one child: the records arrive in order, shaped like the one-job route's.
        first = self.hear("a.wav", "b.wav")
        second = self.hear("c.wav")
        self.assertEqual([(i, r["text"]) for i, r in first], [(0, "heard a.wav"), (1, "heard b.wav")])
        self.assertEqual([(i, r["text"]) for i, r in second], [(0, "heard c.wav")])
        self.assertEqual({r["by"] for _, r in first + second}, {asr._served.pid})
        self.assertNotIn("index", first[0][1])
        self.assertEqual(asr._serve_starts, 1)

    def test_check_ready_and_the_model_check_use_the_serving_process(self):
        # check_ready starts no process of its own once one serves, and the check job reaches the server with
        # the threads the hearing uses, so the models it loads are the ones the hearing finds.
        model = self.root / "parakeet"
        model.mkdir()
        for name in parakeet_asr.MODEL_FILES:
            (model / name).write_text("x", encoding="utf-8")
        with mock.patch.object(machine, "PARAKEET_MODEL", model), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("started a process for the check")):
            asr.check_ready("parakeet", "")
            got = asr._served_check({"check": True, "blocks": ["phone"], "threads": asr.PARAKEET_THREADS})
            loaded, errors = asr._load({"phone": str(self.root / "zipa")})
        self.assertEqual(got, {"loaded": ["phone"], "errors": {}, "threads": asr.PARAKEET_THREADS})
        self.assertEqual((loaded, errors), ({"phone"}, {}))

    def test_a_serving_process_that_ends_early_leaves_the_rest_to_a_process_of_its_own(self):
        # The child dies at the second file. The first record stands, and the other two files are heard by a
        # process that does one job, so the caller gets every file once and in order.
        got = self.hear("a.wav", "die.wav", "c.wav")
        self.assertEqual([(i, r["text"]) for i, r in got], [(0, "heard a.wav"), (1, "heard die.wav"), (2, "heard c.wav")])
        self.assertNotEqual(got[0][1]["by"], got[1][1]["by"])
        self.assertEqual(got[1][1]["by"], got[2][1]["by"])

        # The next call starts a serving process again.
        again = self.hear("d.wav")
        self.assertEqual(again[0][1]["text"], "heard d.wav")
        self.assertEqual(asr._serve_starts, 2)

    def test_a_job_that_cannot_run_raises_comfy_error(self):
        served = asr.serving()
        job = {"bad": True, "files": ["a.wav"], "blocks": ["asr"]}
        with self.assertRaises(ComfyError) as e:
            list(asr._parakeet_served(served, job, [Path("a.wav")]))
        self.assertIn("stopped after 0 of 1 files: the model folder lacks tokens.txt", str(e.exception))
        self.assertTrue(served.alive())

    def test_without_a_serving_process_each_call_runs_one_of_its_own(self):
        # A start that fails turns serving off for this process, and the one-job route hears the files.
        with mock.patch.object(asr, "_start_serving", side_effect=OSError("no python")):
            self.assertIsNone(asr.serving())
            got = self.hear("a.wav")
        self.assertEqual(got[0][1]["text"], "heard a.wav")
        self.assertEqual(asr._serve_starts, asr.SERVE_STARTS)
        self.assertIsNone(asr.serving())

    def test_the_serving_process_ends_with_the_process_that_started_it(self):
        # Without a worker's registry the child watches its stdin, which closes when its parent goes.
        served = asr.serving()
        proc = served.proc
        proc.stdin.close()
        self.assertTrue(gone(proc))

    def test_under_a_worker_the_serving_process_outlives_the_step_and_serves_the_next(self):
        handle = self.open_registry()
        first = self.hear("a.wav")
        found = kept.entry(asr.KEPT_NAME)
        self.assertIsNotNone(found)
        self.assertEqual(found["pid"], first[0][1]["by"])
        self.assertIsNone(asr._served.proc)
        self.assertTrue((self.registry.with_suffix(".asr.log")).is_file())

        # The next step is another process: it has no child of its own and finds the worker's.
        with mock.patch.object(asr, "_served", None), mock.patch.object(asr, "_serve_starts", 0), \
                mock.patch.object(asr, "_start_serving", side_effect=AssertionError("started a second process")):
            second = self.hear("b.wav")
            self.assertEqual(second[0][1]["by"], found["pid"])

        # The worker ends it.
        self.assertEqual(kept.end_all(self.registry), [f"asr (pid {found['pid']})"])
        self.assertFalse(kept.alive(found["pid"]))
        kept.close_job(handle)


class ServeLoopTest(unittest.TestCase):
    """parakeet_asr's side of the protocol, with the hearing itself replaced."""

    def exchange(self, job, run=None, token="secret") -> list[dict]:
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        cache = {}
        with mock.patch.object(parakeet_asr, "run", run or (lambda job, emit, cache: emit({"done": 0}))):
            ours.sendall((json.dumps(job) + "\n").encode())
            parakeet_asr.answer(theirs, token, cache)
            theirs.close()
        with ours.makefile("rb") as reader:
            return [json.loads(line) for line in reader]

    def test_a_job_with_the_token_runs_and_any_other_is_refused(self):
        self.assertEqual(self.exchange({"token": "secret", "files": []}), [{"done": 0}])
        self.assertEqual(self.exchange({"token": "guess", "files": []}),
                         [{"fatal": "the job does not have this process's token"}])
        self.assertEqual(self.exchange([1, 2]), [{"fatal": "the job does not have this process's token"}])

    def test_a_job_that_raises_gives_fatal_and_a_client_that_left_gives_nothing(self):
        def breaks(job, emit, cache):
            raise RuntimeError("no model")

        def client_left(job, emit, cache):
            raise BrokenPipeError(32, "Broken pipe")

        self.assertEqual(self.exchange({"token": "secret"}, breaks), [{"fatal": "RuntimeError: no model"}])
        self.assertEqual(self.exchange({"token": "secret"}, client_left), [])

    def test_a_serving_process_keeps_each_model_and_a_check_loads_with_the_jobs_threads(self):
        loads = []

        def models(job, blocks, threads, errors=None, cache=None):
            loads.append((sorted(blocks), threads, cache is not None))
            return {b: object() for b in blocks}

        out = []
        with mock.patch.object(parakeet_asr, "models", models):
            # One job in a process of its own checks with one thread, as before.
            parakeet_asr.run({"check": True, "blocks": ["asr", "phone", "vad"], "threads": 4}, out.append)
            # A serving process checks with the threads the hearing uses.
            parakeet_asr.run({"check": True, "blocks": ["asr", "phone", "vad"], "threads": 4}, out.append, {})
        self.assertEqual(loads, [(["phone", "vad"], 1, False), (["phone", "vad"], 4, True)])
        self.assertEqual(out[0], {"loaded": ["phone", "vad"], "errors": {}})

    def test_a_missing_parakeet_file_is_fatal_for_a_serving_process(self):
        out = []
        with tempfile.TemporaryDirectory() as tmp:
            code = parakeet_asr.run({"model": tmp, "files": ["a.wav"], "blocks": ["asr"]}, out.append, {})
        self.assertEqual(code, 2)
        self.assertIn("lacks encoder.int8.onnx", out[0]["fatal"])

    def test_models_are_loaded_once_for_a_folder_and_thread_count(self):
        made = []

        class Sherpa:
            class OfflineRecognizer:
                @staticmethod
                def from_transducer(**kwargs):
                    made.append(kwargs["num_threads"])
                    return object()

        cache: dict = {}
        with mock.patch.dict(sys.modules, {"sherpa_onnx": Sherpa}):
            first = parakeet_asr.models({"model": "m"}, {"asr"}, 4, None, cache)
            again = parakeet_asr.models({"model": "m"}, {"asr"}, 4, None, cache)
            other = parakeet_asr.models({"model": "m"}, {"asr"}, 1, None, cache)
            alone = parakeet_asr.models({"model": "m"}, {"asr"}, 4)
        self.assertIs(first["asr"], again["asr"])
        self.assertIsNot(first["asr"], other["asr"])
        self.assertIsNot(first["asr"], alone["asr"])
        self.assertEqual(made, [4, 1, 4])


class BreezeServerKeptTest(RegistryCase):
    def setUp(self):
        super().setUp()
        script = self.root / "fake_breeze.py"
        script.write_text(FAKE_BREEZE, encoding="utf-8")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        for p in (mock.patch.object(breeze, "CPP_SERVER", Path(sys.executable)),
                  mock.patch.object(breeze, "CPP_MODEL", script), mock.patch.object(breeze, "CPP_HOME", self.root),
                  mock.patch.object(breeze, "CPP_PORT", port),
                  mock.patch.object(machine, "breeze_server_pids", lambda: [])):
            p.start()
            self.addCleanup(p.stop)

    def server(self, name: str, log: Lines) -> breeze.CppServer:
        server = breeze.CppServer(self.root / name, log)
        self.addCleanup(server.kill)
        return server

    def test_without_a_registry_the_server_stops_with_its_process(self):
        log = Lines()
        server = self.server("chapter.log", log)
        server.refuse_if_busy()
        server.start()
        proc = server.proc
        self.assertEqual(server.sample_rate, 24000)
        self.assertIsNone(server.kept_pid)
        server.stop()
        self.assertTrue(gone(proc))
        self.assertIn("warmed up", (self.root / "chapter.log").read_text(encoding="utf-8"))

    def test_under_a_worker_the_server_stays_up_and_the_next_step_uses_it(self):
        handle = self.open_registry()
        log = Lines()

        # The first step starts it and leaves it up. What it wrote is in the step's own log.
        first = self.server("one.log", log)
        first.refuse_if_busy()
        first.start()
        proc = first.proc
        self.started.append(proc)
        found = kept.entry(breeze.KEPT_NAME)
        self.assertEqual((found["pid"], found["port"]), (proc.pid, breeze.CPP_PORT))
        first.stop()
        self.assertIsNone(proc.poll())
        self.assertIn("warmed up", (self.root / "one.log").read_text(encoding="utf-8"))
        self.assertEqual(log.lines, [f"   breeze-server started at {first.url}"])

        # The next step neither refuses it nor starts another, and its stop leaves it up too.
        with mock.patch.object(machine, "breeze_server_pids", lambda: [proc.pid]):
            second = self.server("two.log", log)
            second.refuse_if_busy()
            second.start()
            self.assertIsNone(second.proc)
            self.assertEqual((second.kept_pid, second.sample_rate), (proc.pid, 24000))
            self.assertEqual(log.lines[-1], f"   breeze-server is still up at {second.url}")
            second.stop()
        self.assertIsNone(proc.poll())

        # A take that fails restarts the server for real.
        third = self.server("three.log", log)
        third.start()
        third.kill()
        self.assertTrue(gone(proc))
        self.assertIsNone(kept.entry(breeze.KEPT_NAME))
        kept.close_job(handle)

    def test_another_breeze_server_is_still_refused_under_a_worker(self):
        self.open_registry()
        with mock.patch.object(machine, "breeze_server_pids", lambda: [4242]):
            with self.assertRaises(ComfyError) as e:
                self.server("one.log", Lines()).refuse_if_busy()
        self.assertIn("another breeze-server is running (pids 4242", str(e.exception))

    def test_a_kept_server_of_another_model_is_stopped_and_not_used(self):
        handle = self.open_registry()
        other, port = self.listener()
        self.assertTrue(kept.record(breeze.KEPT_NAME, other.pid, port, server=str(breeze.CPP_SERVER), model="other.gguf"))
        log = Lines()
        server = self.server("one.log", log)
        server.refuse_if_busy()
        server.start()
        self.started.append(server.proc)
        self.assertTrue(gone(other))
        self.assertEqual(log.lines, ["   the breeze-server that was left up runs another model, so it is stopped",
                                     f"   breeze-server started at {server.url}"])
        self.assertEqual(kept.entry(breeze.KEPT_NAME)["pid"], server.proc.pid)
        server.kill()
        kept.close_job(handle)

    def test_a_wait_for_comfyui_stops_the_kept_server_first(self):
        # ComfyUI has a prompt when the next step asks, so the server from the job before gives up the card.
        handle = self.open_registry()
        first = self.server("one.log", Lines())
        first.start()
        proc = first.proc
        self.started.append(proc)
        first.stop()
        self.assertIsNone(proc.poll())
        asked = []

        class Comfy(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                asked.append(proc.poll() is None)
                busy = len(asked) < 2
                body = json.dumps({"queue_running": [[0, "p1", {}]] if busy else [], "queue_pending": []}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Comfy)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        log = Lines()
        with mock.patch.object(breeze.time, "sleep", lambda s: None):
            breeze.wait_comfy_idle(f"http://127.0.0.1:{httpd.server_address[1]}", log)
        self.assertTrue(gone(proc))
        self.assertEqual(log.lines, ["   stopped the breeze-server that was left up, so ComfyUI has the card",
                                     "   waiting for ComfyUI to finish its prompts before starting breeze-server"])
        self.assertIsNone(kept.recorded(breeze.KEPT_NAME))

        # The step then starts a server of its own.
        second = self.server("two.log", Lines())
        second.refuse_if_busy()
        second.start()
        self.started.append(second.proc)
        self.assertNotEqual(second.proc.pid, proc.pid)
        second.kill()
        kept.close_job(handle)


class ListeningTest(unittest.TestCase):
    def test_a_closed_port_of_this_machine_is_not_listening(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.listen()
            self.assertTrue(machine.listening(f"http://127.0.0.1:{port}"))
        began = time.time()
        self.assertFalse(machine.listening(f"http://127.0.0.1:{port}/queue"))
        self.assertLess(time.time() - began, machine.PROBE_TIMEOUT + 1.0)

    def test_another_host_counts_as_listening_without_a_connection(self):
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("connected")):
            self.assertTrue(machine.listening("http://comfy.example:8188"))
            self.assertTrue(machine.listening("not a url"))


def node(name, classes, gpu="nvidia"):
    return {"name": name, "classes": classes, "os": "windows", "gpu": gpu, "ready": True, "status": "ready",
            "eligible": True, "busy": False}


class HandNextTest(unittest.TestCase):
    """The app's side: a worker that sends a result with its code version may get its node's next job."""

    def setUp(self):
        from serial_audio.web import nomad
        self.nomad = nomad
        self.tmp = tempfile.TemporaryDirectory()
        self.m = nomad.NomadJobManager(nomad.NomadClient("http://127.0.0.1:9", "token"), state_dir=Path(self.tmp.name),
                                       autostart=False)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.m._lock_file.close)
        self.m.workers = [node("laptop", ["breeze", "comfyui"]), node("mac", ["breeze"], "metal")]
        self.m.nomad_state["up"] = True
        self.logged: dict[str, list[str]] = {}
        report = {"applied": ["chapters/0001/chapter.yaml"], "unchanged": [], "conflicts": [], "held": [], "media": 0}
        for p in (mock.patch.object(nomad.exchange, "read_result", lambda data: (json.loads(data), None)),
                  mock.patch.object(nomad.exchange, "apply_result", lambda *a, **k: dict(report)),
                  mock.patch.object(nomad.exchange, "code_version", lambda: "code-1"),
                  mock.patch.object(nomad.exchange, "build_bundle", lambda slug, info: b"zip"),
                  mock.patch.object(self.m, "_append_log", lambda job, text: self.logged.setdefault(job["id"], []).append(text)),
                  mock.patch.object(self.m, "_save", lambda: None),
                  mock.patch.object(self.m, "_record_timing", lambda *a: None),
                  mock.patch.object(self.m, "_record_placement", lambda *a: None),
                  mock.patch.object(self.m, "_already_done", lambda job: None),
                  mock.patch.object(self.m, "_duration", lambda *a, **k: 60.0),
                  mock.patch.object(self.m, "holder", lambda job: (None, False))):
            p.start()
            self.addCleanup(p.stop)

    def render(self, jid, seq, n, status="queued", comfy=False, slug="demo"):
        job = {"id": jid, "seq": seq, "kind": "render", "slug": slug, "status": status, "heavy": True, "gpu": True,
               "comfy": comfy, "title": f"Render · ch {n}", "args": ["render", slug, str(n)],
               "meta": {"chapters": [n], "lines": 100, "engine": "drama3" if comfy else "breeze"},
               "batch": None, "backlog": False, "started": None}
        self.m.jobs[jid] = job
        return job

    def running(self, jid, seq, n, node_name="laptop"):
        job = self.render(jid, seq, n, status="running")
        job["nomad"] = {"key": f"key-{jid}", "stage": 0, "stages": [{"class": "breeze", "args": job["args"]}],
                        "dispatched": f"serial-audio-breeze-{node_name}/dispatch-{jid}", "target": node_name,
                        "node": node_name, "contact": "t", "dispatching": False, "marks": {}}
        return job

    def result(self, job, code="code-1", exit_code=0):
        data = json.dumps({"exit_code": exit_code, "node": job["nomad"]["target"]}).encode()
        return self.m.worker_result(job["id"], job["nomad"]["key"], "0", data, code)

    def test_the_worker_that_finishes_a_breeze_job_gets_the_next_one_for_its_node(self):
        a, b, c = self.running("a", 1, 1), self.render("b", 2, 2), self.render("c", 3, 3)
        task = a["nomad"]["dispatched"]
        report = self.result(a)

        # The first queued render is the worker's: it runs under the worker's Nomad job, and none is dispatched.
        self.assertEqual(a["status"], "done")
        self.assertEqual(report["next"], {"job": "b", "key": b["nomad"]["key"]})
        self.assertEqual((b["status"], b["nomad"]["dispatched"], b["nomad"]["follows"], b["nomad"]["dispatching"]),
                         ("running", task, "a", False))
        self.assertEqual(b["nomad"]["target"], "laptop")
        self.assertIn("handed to the worker on laptop as it finished Render · ch 1", self.logged["b"][0])
        self.assertEqual(self.m._handing, None)

        # The render after it starts on the other node, by a dispatch of its own on the next tick.
        self.assertEqual((c["status"], c["nomad"]["target"], c["nomad"]["dispatching"], c["nomad"]["follows"]),
                         ("running", "mac", True, None))

        # The stored report holds no key, and a repeated upload gets the same answer while the job waits.
        self.assertEqual(a["nomad"]["reports"]["0"]["next"], "b")
        self.assertEqual(self.result(a)["next"], {"job": "b", "key": b["nomad"]["key"]})

        # The worker fetches the bundle with the handed job's key, and after that the answer names no job.
        self.assertEqual(self.m.worker_bundle("b", b["nomad"]["key"], "laptop"), b"zip")
        self.assertNotIn("next", self.result(a))

    def test_nothing_is_handed_to_other_code_another_class_a_failed_job_or_with_handing_off(self):
        # A worker that runs other code than the app's: the next job is dispatched as before, on a tick.
        a, b = self.running("a", 1, 1), self.render("b", 2, 2)
        self.assertNotIn("next", self.result(a, code="code-0"))
        self.assertEqual((b["status"], b.get("nomad")), ("queued", None))

        # A worker that did not ask.
        a2 = self.running("a2", 3, 3)
        self.assertNotIn("next", self.m.worker_result("a2", "key-a2", "0", json.dumps({"exit_code": 0}).encode()))
        self.assertEqual(b["status"], "queued")

        # A job that failed hands nothing on.
        a3 = self.running("a3", 4, 4)
        self.assertNotIn("next", self.result(a3, exit_code=1))
        self.assertEqual((a3["status"], b["status"]), ("failed", "queued"))

        # With HAND_NEXT off.
        a4 = self.running("a4", 5, 5)
        with mock.patch.object(self.nomad, "HAND_NEXT", False):
            self.assertNotIn("next", self.result(a4))
        self.assertEqual(b["status"], "queued")

        # A job of another class for the same node is dispatched the usual way.
        self.m.jobs.pop("b")
        a5, drama = self.running("a5", 6, 6), self.render("d", 7, 7, comfy=True)
        self.assertNotIn("next", self.result(a5))
        self.assertEqual((drama["status"], drama["nomad"]["dispatching"], drama["nomad"]["follows"]),
                         ("running", True, None))

    def test_a_handed_job_is_queued_again_when_the_task_ends_before_taking_it(self):
        a, b = self.running("a", 1, 1), self.render("b", 2, 2)
        self.result(a)
        self.assertEqual(b["nomad"]["follows"], "a")
        ended = {"polled": 1.0, "allocs": [{"ID": "alloc-1", "NodeName": "laptop", "ClientStatus": "complete",
                                            "CreateTime": 1}]}

        # While the task runs, the job waits for it.
        self.m._apply_poll(b, {**ended, "allocs": [{**ended["allocs"][0], "ClientStatus": "running"}]}, 1.0)
        self.assertEqual(b["status"], "running")

        # The task ended and never fetched the bundle: the job is as it was before it was handed over.
        self.m._apply_poll(b, ended, 2.0)
        self.assertEqual((b["status"], b.get("nomad")), ("queued", None))
        self.assertIn("ended before it took it, so the job is queued again", self.logged["b"][-1])
        self.assertNotIn("next", self.result(a))

        # A handed job that did fetch its bundle and then lost its task fails like any other.
        b2 = self.render("b2", 3, 3)
        a2 = self.running("a2", 4, 4)
        self.m.jobs.pop("b")
        self.result(a2)
        self.assertEqual(b2["nomad"]["follows"], "a2")
        self.m.worker_bundle("b2", b2["nomad"]["key"], "laptop")
        self.m._apply_poll(b2, ended, 3.0)
        self.assertEqual(b2["status"], "running")
        self.m._apply_poll(b2, {**ended, "tail": ""}, 3.0 + self.nomad.END_GRACE + 1)
        self.assertEqual(b2["status"], "failed")

    def test_a_pass_counts_the_takes_of_a_render_only_when_nobody_has_counted_them(self):
        b = self.render("b", 2, 2)
        with mock.patch.object(self.nomad, "reusable_takes", return_value=(90, 100)) as count:
            # Nobody has counted the job, so the pass counts it, and that count stands for the passes after it.
            self.assertEqual(self.m._takes_needed(b, 100), 10)
            self.assertEqual(self.m._takes_needed(b, 100), 10)
            self.assertEqual(count.call_count, 1)
            # The count is marked for another one outside the lock, which also counts the takes to hear.
            self.assertEqual(self.m._work["b"], (0.0, 10, 0))

            # A job finished, which drops the pass's own counts. The count kept outside the lock stands, also
            # while it is marked for another count, so the pass that follows a result reads no chapter.
            self.m._takes_cache.clear()
            self.m._work["b"] = (0.0, 12, 0)
            self.assertEqual(self.m._takes_needed(b, 100), 12)
            self.assertEqual(count.call_count, 1)

    def test_a_handed_job_stopped_before_its_bundle_is_fetched_is_cancelled(self):
        a, b = self.running("a", 1, 1), self.render("b", 2, 2)
        key = self.result(a)["next"]["key"]
        self.m.cancel("b")
        self.assertEqual(b["status"], "cancelled")
        self.assertIn("the worker on laptop gets no bundle for it", b["note"])
        with self.assertRaises(self.nomad.WorkerError) as e:
            self.m.worker_bundle("b", key, "laptop")
        self.assertEqual(e.exception.status, 410)


class FetchNextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def bundle(self) -> bytes:
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("bundle.json", json.dumps({"job": "b", "stage": 0}))
            z.writestr("library/demo/serial.yaml", "title: Demo\n")
        return buf.getvalue()

    def test_the_next_jobs_bundle_is_unpacked_into_a_folder_of_its_own(self):
        asked = []

        def fetch(app, path, timeout=120):
            asked.append((path, app.headers[w.KEY_HEADER], app.headers[w.NODE_HEADER]))
            return self.bundle()

        with mock.patch.object(w.App, "fetch", fetch):
            work = w.fetch_next("http://app", {"job": "b", "key": "kb"}, "laptop", self.root / "next-2",
                                threading.Event())
        self.assertEqual(work, self.root / "next-2" / "work")
        self.assertEqual(asked, [("/api/worker/b/bundle", "kb", "laptop")])
        self.assertEqual(json.loads((work / "bundle.json").read_text(encoding="utf-8"))["job"], "b")
        self.assertTrue((work / "library" / "demo" / "serial.yaml").is_file())

    def test_a_job_that_was_stopped_or_an_app_that_is_away_gives_no_work_folder(self):
        halt = threading.Event()

        def stopped(app, path, timeout=120):
            raise w.AppError(410, "the job was cancelled")

        tries = []

        def away(app, path, timeout=120):
            tries.append(path)
            raise w.AppError(None, "the app is not reachable")

        with mock.patch.object(w.App, "fetch", stopped):
            self.assertIsNone(w.fetch_next("http://app", {"job": "b", "key": "kb"}, "laptop", self.root / "n", halt))
        with mock.patch.object(w.App, "fetch", away), mock.patch.object(w, "BUNDLE_RETRY", 0.01):
            self.assertIsNone(w.fetch_next("http://app", {"job": "b", "key": "kb"}, "laptop", self.root / "n", halt))
        self.assertEqual(len(tries), w.BUNDLE_TRIES)

        # A worker that is being stopped does not start on another job.
        halt.set()
        with mock.patch.object(w.App, "fetch", side_effect=AssertionError("fetched")):
            self.assertIsNone(w.fetch_next("http://app", {"job": "b", "key": "kb"}, "laptop", self.root / "n", halt))


class RunJobCodeTest(RegistryCase):
    """What a worker keeps and asks for, by the code of each job it runs."""

    def run_job(self, held, code: str, name: str) -> tuple[int, dict | None, list[dict]]:
        work = self.root / name / "work"
        (work / "library" / "demo").mkdir(parents=True)
        (work / "bundle.json").write_text(json.dumps({
            "job": name, "stage": 0, "steps": [["chapters", "demo"]], "class": "breeze", "gpu": True, "sync": False,
            "takes": False, "title": name, "kind": "render", "chapters": [1], "slug": "demo", "code": code,
            "manifest": {}}), encoding="utf-8")
        results = []

        def request(app, method, path, data=None, timeout=60, headers=None):
            if path == "result":
                results.append(dict(headers or {}))
                return {"applied": [], "next": {"job": "after", "key": "k"}} if headers else {"applied": []}
            if path.startswith("log?offset="):
                return {"offset": int(path.split("=")[1]) + len(data or b"")}
            return {"have": {}}

        def step(args, work, env, log, stop):
            self.registries.append(env.get(kept.ENV))
            return 0

        with mock.patch.object(w.App, "request", request), mock.patch.object(w, "run_step", step), \
                mock.patch.object(w, "machine_busy", lambda *a: None), mock.patch.object(w, "node_config", lambda: {}), \
                mock.patch.object(w.signal, "signal", lambda *a: None):
            code_, following = w.run_job("http://app", name, "key", work, "laptop", held)
        return code_, following, results

    def test_a_worker_asks_for_the_next_job_with_its_own_code_and_not_after_a_job_of_other_code(self):
        held = w.Held(self.root / "gpu.lock")
        self.addCleanup(held.release)
        self.registries = []

        # The first job's code is the worker's own, and the worker sends it with the result.
        code, following, results = self.run_job(held, "code-1", "one")
        self.assertEqual((code, following), (0, {"job": "after", "key": "k"}))
        self.assertEqual(results, [{w.NEXT_HEADER: "code-1"}])
        self.assertIsNotNone(held.lock)

        # A process a step left up stays through a job of the same code.
        proc, port = self.listener()
        with mock.patch.dict(os.environ, {kept.ENV: str(held.registry)}):
            self.assertTrue(kept.record("asr", proc.pid, port))
        self.run_job(held, "code-1", "two")
        self.assertIsNone(proc.poll())

        # The app was deployed while the task ran: the handed job has other code. Its steps start without the
        # processes of the old code, and the worker asks for no further job and lets the lock go.
        code, following, results = self.run_job(held, "code-2", "three")
        self.assertEqual((code, following), (0, None))
        self.assertEqual(results, [{}])
        self.assertTrue(gone(proc))
        self.assertIsNone(held.lock)
        # The steps of the first two jobs got the registry, and the steps of the job with other code did not.
        self.assertEqual(self.registries, [str(self.registry), str(self.registry), None])

    def test_a_result_the_app_does_not_take_at_once_ends_the_kept_processes(self):
        answers = [w.AppError(None, "the app is not reachable"), w.AppError(503, "busy"), {"applied": []}]
        waited = []

        def request(app, method, path, data=None, timeout=60, headers=None):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch.object(w.App, "request", request), mock.patch.object(w, "RESULT_RETRY", 0.01):
            report = w.send_result(w.App("http://app", "j", "k", "laptop", 0), b"zip", Lines(), "code-1",
                                   lambda: waited.append(len(answers)))
        self.assertEqual(report, {"applied": []})
        self.assertEqual(waited, [2])


if __name__ == "__main__":
    unittest.main()
