"""Render a scripted chapter with Breeze TTS 2.

One take per script line. Each speaker's cast clip is cloned, and a line with a direction is performed with it
as the instruction. Two runners make the takes. The default, "cpp", is Breeze-TTS-2.cpp's breeze-server on the
GPU, through Vulkan on Windows and Metal on a Mac, which this module starts for the render and stops after it. It
refuses to start while another breeze-server runs or answers on its port. A cpp take whose length does not fit its line is taken again with new seeds. "comfyui"
queues the ComfyUI-Breeze-TTS-2 nodes, one prompt per line. Takes are listed in the chapter's breeze_takes.json, so a
rerun renders only the lines whose take is missing or out of date. breeze_join.py then joins the takes under the
audio Python (machine.AUDIO_PYTHON), applies voice effects, and matches the loudness of the Drama 3 chapters.
"""
from __future__ import annotations

import ctypes
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import wave
import zlib
from ctypes import wintypes
from pathlib import Path

from . import library as lib
from . import machine
from .comfy import ComfyClient, ComfyError
from .render import AUDIO_SUFFIX, COMFY_OUTPUT, COMFY_PYTHON, apply_say_as, plan_slots, say_as_map, slot_effects

JOIN_SCRIPT = Path(__file__).with_name("breeze_join.py")
TAKES_FILE = "breeze_takes.json"
RUNNERS = ("cpp", "comfyui")
VOCAL_EVENTS = {"laugh", "cough", "clears throat", "sigh"}
CLONE_CFG = 1.0
DIRECTION_CFG = 4.0
TAKE_TIMEOUT = 900
REPORT_EVERY = 120
MAX_TAKE_SECONDS = 120

MODEL = "int8 hybrid (recommended)"
DECODE_MODE = "cuda_graphs"
CONTROLS = {"max_new_tokens": 1500, "temperature": 0.9, "top_k": 50, "top_p": 1.0, "repetition_penalty": 1.1,
            "depth_temperature": 0.9, "depth_top_k": 50, "depth_top_p": 1.0}
HIGH_VRAM_FLAGS = {"--highvram", "--gpu-only"}

CPP_HOME = machine.BREEZE_HOME
CPP_SERVER = CPP_HOME / "bin" / machine.BREEZE_SERVER_NAME
CPP_MODEL = CPP_HOME / "models" / "breeze-tts-2-q8_0.gguf"
CPP_PORT = 8137
CPP_FIELDS = {"temperature": 0.9, "top_k": 50, "top_p": 1.0, "repetition_penalty": 1.1, "max_new_tokens": 1500}
CPP_START_TIMEOUT = 180
COMFY_IDLE_TIMEOUT = 4 * 3600
RETAKES = 2
RETAKE_SEED_STEP = 7919


def take_bounds(words: int) -> tuple[float, float]:
    """The shortest and longest plausible take, in seconds, for a line of `words` words."""
    return 0.12 * words, 2.25 + 0.75 * words


def miss(seconds: float, low: float, high: float) -> float:
    """How far a take's length falls outside [low, high], as a ratio. 1.0 is inside, larger is further out."""
    if seconds > high:
        return seconds / high
    if seconds < low:
        return low / max(seconds, 1e-3)
    return 1.0


def count_words(text: str) -> int:
    """Words in text, counting each CJK character as one, the way take_bounds expects."""
    return len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]|[^\W\u3400-\u9fff\uf900-\ufaff]+(?:['\u2019][^\W\u3400-\u9fff]+)*", text))


def wav_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except (OSError, wave.Error, EOFError, ZeroDivisionError):
        return None


def check_comfy(client: ComfyClient) -> None:
    """Raises ComfyError unless ComfyUI has the Breeze TTS 2 nodes and keeps models on the GPU."""
    argv = client.system_stats().get("system", {}).get("argv") or []
    if not HIGH_VRAM_FLAGS & set(argv):
        raise ComfyError("Breeze TTS 2 in ComfyUI needs ComfyUI started with --highvram. Under dynamic VRAM its CUDA "
                         "graph capture fails and the codec ends up split across CPU and GPU.")
    try:
        client._get("/object_info/BreezeTTS2LoadModel")["BreezeTTS2LoadModel"]
    except KeyError as e:
        raise ComfyError("ComfyUI has no Breeze TTS 2 nodes; install ComfyUI-Breeze-TTS-2 and restart it") from e


def speakable(text: str) -> str:
    """The line's text for Breeze. Parentheses that are not vocal events become commas, so they are read aloud."""
    def swap(m: re.Match) -> str:
        inner = m.group(1).strip()
        return m.group(0) if inner.lower() in VOCAL_EVENTS else f", {inner},"
    out = re.sub(r"\(([^()]*)\)", swap, " ".join(text.split()))
    out = re.sub(r"\s+,", ",", out)
    out = re.sub(r",\s*([,.!?;:])", r"\1", out)
    return re.sub(r"\s{2,}", " ", out).strip(" ,")


def instruction(direction: str) -> str:
    return f"Perform the line with this direction: {direction}."


def voice_seed(base: int, voice: str) -> int:
    """A positive Breeze seed that stays the same for a voice across chapters."""
    return 1 + zlib.crc32(f"{base}:{voice}".encode()) % (2**31 - 2)


def stage_references(slug: str, voices: set[str], runner: str) -> dict[str, dict]:
    """Copies each voice's cast clip where the runner reads it.

    slug: the serial.
    voices: the voice ids the chapter uses.
    runner: "comfyui" copies the clip under ComfyUI's output folder. "cpp" writes it as a 16-bit WAV beside breeze-server.
    Returns voice -> {ref, transcript, sha1}, where ref is a LoadAudio path for ComfyUI and a WAV path for cpp.
    Raises ComfyError when a voice has no record or the WAV conversion fails.
    """
    vdir = lib.serial_dir(slug) / "voices"
    stage = (COMFY_OUTPUT / "breeze_refs" / slug) if runner == "comfyui" else (CPP_HOME / "refs" / slug)
    stage.mkdir(parents=True, exist_ok=True)
    refs, convert = {}, []
    for voice in sorted(voices):
        record = lib.load_yaml(vdir / f"{voice}.yaml")
        if not record:
            raise ComfyError(f"voice {voice!r} has no record in {vdir}")
        src = vdir / record["clip"]
        data = src.read_bytes()
        sha1 = hashlib.sha1(data).hexdigest()
        if runner == "comfyui":
            dst = stage / f"{voice}{src.suffix}"
            if not dst.exists() or dst.read_bytes() != data:
                shutil.copyfile(src, dst)
            ref = f"breeze_refs/{slug}/{dst.name} [output]"
        else:
            dst = stage / f"{voice}.wav"
            mark = stage / f"{voice}.sha1"
            if not dst.exists() or not mark.exists() or mark.read_text().strip() != sha1:
                convert.append([str(src), str(dst), str(mark), sha1])
            ref = str(dst)
        refs[voice] = {"ref": ref, "transcript": record["sample_text"], "sha1": sha1}
    if convert:
        code = ("import sys, json, soundfile as sf\n"
                "for src, dst, mark, sha1 in json.loads(sys.argv[1]):\n"
                "    x, sr = sf.read(src, dtype='float32')\n"
                "    sf.write(dst, x, sr, subtype='PCM_16')\n"
                "    open(mark, 'w').write(sha1)\n")
        done = subprocess.run([str(COMFY_PYTHON), "-I", "-c", code, json.dumps(convert)], capture_output=True, text=True)
        if done.returncode != 0:
            raise ComfyError(f"converting reference clips to WAV failed: {(done.stderr or done.stdout)[-1500:]}")
    return refs


def take_graph(*, text: str, ref: str, transcript: str, direction: str, seed: int, prefix: str) -> dict:
    """One line as a ComfyUI graph: load the model and the reference clip, clone or direct, save the take."""
    common = {"breeze_model": ["model", 0], "text": text, "reference_audio": ["ref", 0], "reference_text": transcript,
              "seed": seed, **CONTROLS}
    if direction:
        gen = {"class_type": "BreezeTTS2VoiceDirection", "inputs": {
            **common, "instruction": instruction(direction), "cfg_scale": DIRECTION_CFG, "stitch_reference": "none"}}
    else:
        gen = {"class_type": "BreezeTTS2VoiceClone", "inputs": {**common, "cfg_scale": CLONE_CFG}}
    return {
        "model": {"class_type": "BreezeTTS2LoadModel", "inputs": {
            "model": MODEL, "dtype": "auto", "device": "auto", "attention": "auto", "decode_mode": DECODE_MODE,
            "download_if_missing": False}},
        "ref": {"class_type": "LoadAudio", "inputs": {"audio": ref}},
        "gen": gen,
        "save": {"class_type": "SaveAudio", "inputs": {"audio": ["gen", 0], "filename_prefix": prefix}},
    }


def take_key(**parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_takes(path: Path, log) -> dict:
    """The saved takes by line. A file that does not parse is moved aside and treated as empty."""
    if not path.exists():
        return {}
    try:
        return lib.load_json(path).get("takes") or {}
    except (json.JSONDecodeError, OSError, AttributeError) as e:
        aside = path.with_name(f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}.bad")
        os.replace(path, aside)
        log(f"   {path.name} did not parse ({e}); moved it to {aside.name} and starting the takes again")
        return {}


def save_takes(path: Path, takes: dict, runner: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    lib.save_json(tmp, {"engine": "breeze", "runner": runner, "takes": takes})
    os.replace(tmp, path)


def cancel_prompt(client: ComfyClient, prompt_id: str) -> None:
    """Asks ComfyUI to drop a running or queued prompt. Never raises."""
    try:
        client._post(f"/api/jobs/{prompt_id}/cancel", {})
    except Exception:
        pass


def run_comfy_take(client: ComfyClient, graph: dict, line: int, log) -> dict:
    """Runs one take and returns its history entry. A failed or timed-out prompt is cancelled and retried once."""
    for attempt in (1, 2):
        prompt_id = client.queue(graph)
        try:
            return client.wait(prompt_id, timeout=TAKE_TIMEOUT, poll=0.25)
        except ComfyError as e:
            cancel_prompt(client, prompt_id)
            if attempt == 2:
                raise
            log(f"   line {line} failed, retrying once after freeing the card: {str(e)[:500]}")
            client.free()
    raise AssertionError("unreachable")


JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def close_handle(handle) -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle(handle)


def tie_to_this_process(proc: subprocess.Popen):
    """Puts proc in a job that Windows closes, killing proc, when this process ends.

    proc: a process this process started.
    Returns the job handle, or None when Windows refused, in which case proc is left untied.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info))
    if not ok or not k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):
        close_handle(job)
        return None
    return job


class CppServer:
    """A breeze-server on 127.0.0.1 that this process starts, and stops on exit."""

    def __init__(self, log_path: Path, log):
        self.url = f"http://127.0.0.1:{CPP_PORT}"
        self.log_path, self.log = log_path, log
        self.proc, self.job, self.log_file = None, None, None
        self.sample_rate = None

    def health(self) -> dict | None:
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data if data.get("status") == "ok" else None
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def refuse_if_busy(self) -> None:
        """Raises ComfyError when a breeze-server process exists or a breeze-server answers on the port."""
        pids = [str(p) for p in machine.breeze_server_pids()]
        if pids or self.health():
            raise ComfyError(f"another breeze-server is running (pids {', '.join(pids) or 'unknown'}, port {CPP_PORT}); "
                             "stop it before rendering, so two models do not share the card")

    def __enter__(self) -> "CppServer":
        self.refuse_if_busy()
        self.start()
        return self

    def start(self) -> None:
        """Starts the server and waits until it answers. On Windows a job object stops it when this process ends.
        Elsewhere it stays in this process's group, so stopping the group stops it."""
        for path in (CPP_SERVER, CPP_MODEL):
            if not path.exists():
                raise ComfyError(f"breeze-server needs {path}; build Breeze-TTS-2.cpp and fetch its Q8_0 weights first")
        self.log_file = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [str(CPP_SERVER), str(CPP_MODEL), "--host", "127.0.0.1", "--port", str(CPP_PORT), "--ws-port", "-1",
             "--voices-dir", str(CPP_HOME / "voices")],
            cwd=str(CPP_HOME), stdout=self.log_file, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if machine.WINDOWS else 0)
        if machine.WINDOWS:
            self.job = tie_to_this_process(self.proc)
            if self.job is None:
                self.log("   warning: could not tie breeze-server to this process, so a hard kill may leave it running")
        deadline = time.time() + CPP_START_TIMEOUT
        try:
            while not (health := self.health()):
                if self.proc.poll() is not None:
                    raise ComfyError(f"breeze-server exited with {self.proc.returncode} while starting; see {self.log_path}")
                if time.time() > deadline:
                    raise ComfyError(f"breeze-server did not answer within {CPP_START_TIMEOUT} s; see {self.log_path}")
                time.sleep(1)
            self.sample_rate = int(health.get("sample_rate") or 24000)
        except BaseException:
            self.stop()
            raise
        self.log(f"   breeze-server started at {self.url}")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.job is not None:
            close_handle(self.job)
        if self.log_file is not None:
            self.log_file.close()
        self.proc, self.job, self.log_file = None, None, None

    def __exit__(self, *exc) -> None:
        self.stop()

    def speech(self, fields: dict, wav: bytes | None) -> bytes:
        """POSTs one take to /v1/audio/speech and returns its s16le PCM.

        fields: the form fields, without the reference clip.
        wav: the reference clip as WAV bytes, or None to design a voice from the instruction field.
        Waits while the server answers 409. Raises ComfyError on any other failure, a cut body, or an empty or
        odd-length answer. A take the server ended early with a short clean answer is not detected here.
        """
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
                 for k, v in fields.items()]
        if wav is not None:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="ref_audio"; filename="ref.wav"\r\n'
                         f'Content-Type: audio/wav\r\n\r\n'.encode() + wav + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"{self.url}/v1/audio/speech", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        deadline = time.time() + TAKE_TIMEOUT
        while True:
            try:
                with urllib.request.urlopen(req, timeout=TAKE_TIMEOUT) as resp:
                    pcm = resp.read()
                break
            except urllib.error.HTTPError as e:
                if e.code != 409 or time.time() > deadline:
                    raise ComfyError(f"breeze-server answered {e.code}: {e.read()[:300]!r}") from e
                time.sleep(1)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                raise ComfyError(f"breeze-server request failed: {e!r}") from e
        if not pcm or len(pcm) % 2:
            raise ComfyError(f"breeze-server returned {len(pcm)} bytes, not 16-bit audio")
        return pcm

    def take(self, fields: dict, wav: bytes | None, label: str, log) -> bytes:
        """One take's PCM, as speech() returns it. A failure is logged with label, restarts the server, and is
        retried once."""
        try:
            return self.speech(fields, wav)
        except ComfyError as e:
            log(f"   {label} failed, restarting breeze-server and retrying once: {str(e)[:500]}")
            self.stop()
            self.start()
            return self.speech(fields, wav)


def wait_comfy_idle(comfy_url: str, log, timeout: float = COMFY_IDLE_TIMEOUT) -> None:
    """Returns once ComfyUI has no running or queued prompt, or at once when it does not answer.

    comfy_url: the ComfyUI to watch.
    log: receives one line when the wait starts.
    timeout: seconds to wait. Raises ComfyError when ComfyUI is still busy after it.
    """
    client, deadline, told = ComfyClient(comfy_url), time.time() + timeout, False
    while True:
        try:
            queue = client._get("/queue", timeout=10)
        except Exception:
            return
        if not (queue.get("queue_running") or queue.get("queue_pending")):
            return
        if time.time() > deadline:
            raise ComfyError(f"ComfyUI stayed busy for {timeout / 60:.0f} min; breeze-server would share the card with it")
        if not told:
            log("   waiting for ComfyUI to finish its prompts before starting breeze-server")
            told = True
        time.sleep(5)


def free_comfy(comfy_url: str) -> None:
    """Asks ComfyUI to unload its models so breeze-server has the card. Never raises."""
    try:
        ComfyClient(comfy_url).free(settle=1.0)
    except Exception:
        pass


def choose_runner(serial: dict, last: dict | None, *, join_only: bool = False, runner: str | None = None) -> str:
    """The Breeze runner a render would use.

    serial: the serial.yaml record.
    last: the chapter's render record, or None.
    join_only: join the saved takes again, which keeps the runner that made them.
    runner: "cpp" or "comfyui" to force one.
    Returns the runner. Raises ComfyError when it is unknown.
    """
    if not runner and join_only and last and last.get("engine") == "breeze":
        runner = last.get("runner") or "comfyui"
    runner = runner or serial.get("breeze_runner") or "cpp"
    if runner not in RUNNERS:
        raise ComfyError(f"unknown Breeze runner {runner!r}; known: {', '.join(RUNNERS)}")
    return runner


def render_breeze(slug: str, number: int, *, seed: int = 1, comfy_url: str = "http://127.0.0.1:8188", log=print,
                  join_only: bool = False, runner: str | None = None) -> dict:
    """Renders a chapter with Breeze TTS 2 to audio/chNNNN.opus and marks it rendered.

    slug: the serial.
    number: the chapter.
    seed: the base for each voice's Breeze seed. A join-only run uses the seed the chapter was rendered with.
    comfy_url: the ComfyUI that the comfyui runner queues on, and that the cpp runner asks to free the card.
    log: receives the progress lines.
    join_only: join the saved takes again without generating any.
    runner: "cpp" or "comfyui". None uses the chapter's last runner on a join-only run, else serial.yaml
        breeze_runner, else cpp.
    Returns the chapter's render record. New takes are then checked against the script with this machine's speech
    recognizer (asr.choose), and the result, or why the check was skipped, is stored under render.check. Raises ComfyError when the runner cannot start, a take
    fails twice, or a join-only run finds a take missing.
    """
    # Setup
    serial = lib.load_serial(slug)
    meta, _ = lib.load_chapter(slug, number)
    folder = lib.chapter_dir(slug, number)
    script_path = folder / "script.json"
    if not script_path.exists():
        raise ComfyError(f"chapter {number} has no script.json; run the screenplay pass first")
    lines = lib.load_json(script_path)["lines"]
    bible = lib.load_bible(slug)
    speaker_slot, slot_voice, notes = plan_slots(serial, bible, lines)
    say = say_as_map(bible)
    effects = {slot_voice[s]: spec for s, spec in slot_effects(serial, slot_voice).items()}
    last = meta.get("render") if isinstance(meta.get("render"), dict) else {}
    last_breeze = last.get("engine") == "breeze"
    if join_only and last_breeze:
        seed = last.get("seed", seed)
    runner = choose_runner(serial, last, join_only=join_only, runner=runner)
    stem = f"ch{number:04d}"
    for n in notes:
        log(f"   {n}")
    log(f"render: {slug} chapter {number}: {len(lines)} lines, {len(slot_voice)} voices "
        f"{ {s: v for s, v in slot_voice.items()} } with Breeze TTS 2 via {runner}")

    takes_path = folder / TAKES_FILE
    saved = load_takes(takes_path, log)
    refs = stage_references(slug, set(slot_voice.values()), runner)
    model = MODEL if runner == "comfyui" else f"cpp:{CPP_MODEL.name}"
    controls = CONTROLS if runner == "comfyui" else CPP_FIELDS

    # Plan
    plan = []
    for i, line in enumerate(lines):
        voice = slot_voice[speaker_slot[line["speaker"]]]
        text = speakable(apply_say_as(line["text"], say))
        direction = " ".join((line.get("direction") or "").split())
        vseed = voice_seed(seed, voice)
        parts = dict(text=text, voice=voice, ref=refs[voice]["sha1"], direction=direction, seed=vseed,
                     model=model, controls=controls, clone_cfg=CLONE_CFG, direction_cfg=DIRECTION_CFG)
        if runner != "comfyui":
            parts["runner"] = runner
        plan.append((i, voice, text, direction, vseed, take_key(**parts)))
    spoken = [p for p in plan if re.search(r"\w", p[2])]

    def usable(p) -> bool:
        entry = saved.get(str(p[0])) or {}
        if entry.get("key") != p[5] or not Path(entry.get("file", "")).exists():
            return False
        if runner != "cpp" or join_only or entry.get("out_of_range"):
            return True
        secs = wav_seconds(Path(entry["file"]))
        low, high = take_bounds(count_words(p[2]))
        return secs is not None and low <= secs <= high

    stale = [p for p in spoken if not usable(p)]
    if join_only and stale:
        raise ComfyError(f"chapter {number} is missing {len(stale)} takes (first: line {stale[0][0]}); run a full render")

    # Takes
    generated_seconds = 0.0
    if stale:
        via = "breeze-server" if runner == "cpp" else "ComfyUI"
        if runner == "comfyui":
            client = ComfyClient(comfy_url, output_dir=COMFY_OUTPUT)
            check_comfy(client)
            client.free()
            server = None
        else:
            server = CppServer(folder / "breeze_server.log", log)
            server.refuse_if_busy()
            wait_comfy_idle(comfy_url, log)
            free_comfy(comfy_url)
            server.start()
        try:
            log(f"   generating takes with Breeze TTS 2 via {via} (this is the slow part): {len(stale)} of "
                f"{len(spoken)} lines, {len(spoken) - len(stale)} reused")
            take_dir = COMFY_OUTPUT / "breeze" / slug / stem / "cpp"
            take_dir.mkdir(parents=True, exist_ok=True)
            wavs = {v: Path(r["ref"]).read_bytes() for v, r in refs.items()} if server else {}
            start = last_report = time.time()
            for done, (i, voice, text, direction, vseed, key) in enumerate(stale, 1):
                t0 = time.time()
                if server is None:
                    graph = take_graph(text=text, ref=refs[voice]["ref"], transcript=refs[voice]["transcript"],
                                       direction=direction, seed=vseed, prefix=f"breeze/{slug}/{stem}/l{i:04d}")
                    entry = run_comfy_take(client, graph, i, log)
                    items = entry.get("outputs", {}).get("save", {}).get("audio") or []
                    if not items:
                        raise ComfyError(f"line {i} produced no audio: outputs {list(entry.get('outputs', {}))}")
                    file = client.output_path(items[0])
                else:
                    fields = {"text": text, "ref_text": refs[voice]["transcript"], "seed": vseed,
                              "cfg_scale": DIRECTION_CFG if direction else CLONE_CFG, **CPP_FIELDS}
                    if direction:
                        fields["instruction"] = instruction(direction)
                    low, high = take_bounds(count_words(text))
                    tries = []
                    for attempt in range(RETAKES + 1):
                        fields["seed"] = 1 + (vseed - 1 + attempt * RETAKE_SEED_STEP) % (2**31 - 2)
                        pcm = server.take(fields, wavs[voice], f"line {i}", log)
                        secs = len(pcm) / 2 / server.sample_rate
                        tries.append((secs, fields["seed"], pcm))
                        if low <= secs <= high:
                            break
                        log(f"   line {i}: a {secs:.1f} s take is outside {low:.1f} to {high:.1f} s for "
                            f"{count_words(text)} words" + (", taking it again" if attempt < RETAKES else ""))
                    secs, used_seed, pcm = min(tries, key=lambda t: miss(t[0], low, high))
                    file = take_dir / f"l{i:04d}.wav"
                    with wave.open(str(file), "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(server.sample_rate)
                        w.writeframes(pcm)
                saved[str(i)] = {"key": key, "file": str(file), "voice": voice, "direction": direction,
                                 "seconds": round(time.time() - t0, 2)}
                if server is not None:
                    saved[str(i)]["seed"] = used_seed
                    if len(tries) > 1:
                        saved[str(i)]["takes_tried"] = len(tries)
                    if not low <= secs <= high:
                        saved[str(i)]["out_of_range"] = True
                        log(f"   warning: line {i} kept a {secs:.1f} s take after {len(tries)} tries; listen to it")
                save_takes(takes_path, saved, runner)
                now = time.time()
                if done == 1:
                    log(f"   first take done in {now - t0:.0f} s")
                if now - last_report >= REPORT_EVERY:
                    last_report = now
                    per = (now - start) / done
                    log(f"   [{(now - start) / 60:.0f} min] take {done}/{len(stale)}, {per * 1000:.0f} ms/take, "
                        f"~{per * (len(stale) - done) / 60:.0f} min left")
            generated_seconds = time.time() - start
        finally:
            if server is not None:
                server.__exit__(None, None, None)
            else:
                try:
                    client.free()
                except Exception:
                    pass

    # Join
    log("   joining takes" + (f" with voice effects {effects}" if effects else "") + "...")
    out_dir = COMFY_OUTPUT / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / f"{stem}_breeze{AUDIO_SUFFIX}"
    job = {"output": str(src), "cap_seconds": MAX_TAKE_SECONDS,
           "takes": [{"line": i, "file": saved[str(i)]["file"], "effect": effects.get(voice)} for i, voice, *_ in spoken]}
    job_path = folder / "breeze_join_job.json"
    lib.save_json(job_path, job)
    done = subprocess.run([str(COMFY_PYTHON), "-I", str(JOIN_SCRIPT), str(job_path)], capture_output=True, text=True)
    if done.returncode != 0:
        raise ComfyError(f"joining takes failed: {(done.stderr or done.stdout).strip()[-2000:]}")
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    audio_dir = lib.serial_dir(slug) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    dst = audio_dir / f"{stem}{src.suffix}"
    shutil.copy2(src, dst)
    log(f"   joined {summary['takes']} takes into {summary['seconds']} s at {summary['sample_rate']} Hz")
    if summary.get("capped"):
        log(f"   warning: lines {summary['capped']} reached the {MAX_TAKE_SECONDS} s take limit and may be cut off")

    # Record
    meta["render"] = {"engine": "breeze", "runner": runner, "model": model, "takes_file": TAKES_FILE,
                      "comfy_audio": str(src), "audio": f"audio/{dst.name}", "seed": seed,
                      "slots": {str(s): v for s, v in slot_voice.items()}, "notes": notes,
                      "generated_takes": len(stale), "generate_seconds": round(generated_seconds), "at": lib.now_iso()}
    if summary.get("capped"):
        meta["render"]["capped_lines"] = summary["capped"]
    if effects:
        meta["render"]["effects"] = effects
        meta["render"]["fx"] = {"treated_lines": summary["treated_lines"], "raw_seconds": summary.get("raw_seconds"),
                                "seconds": summary["seconds"]}
    if not stale and isinstance(last.get("check"), dict):
        meta["render"]["check"] = last["check"]
    meta["status"] = "rendered"
    lib.save_chapter_meta(slug, number, meta)

    # Check
    if stale or "check" not in meta["render"]:
        from .takecheck import check_after_render
        meta["render"]["check"] = check_after_render(slug, number, comfy_url=comfy_url, log=log)
    log(f"   audio: {dst}")
    return meta["render"]
