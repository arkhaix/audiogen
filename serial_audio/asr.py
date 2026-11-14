"""Speech recognition for the casting check and the take check, and the word comparison both use.

Two recognizers. "qwen3" is Qwen3-ASR in ComfyUI, one prompt per file, so the files must lie under ComfyUI's output
folder. "parakeet" is NVIDIA Parakeet TDT 0.6B v2 through sherpa-onnx, which parakeet_asr.py runs on the CPU under
the audio Python, with no ComfyUI. A process that hears with Parakeet starts one parakeet_asr.py that serves all its
jobs, so the models load once. Under a worker that keeps processes up between jobs (kept.py), that process outlives
this one and serves the next job's steps too.
"""
from __future__ import annotations

import atexit
import json
import queue
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import Iterator

from . import kept, machine
from .comfy import ComfyClient, ComfyError, transcribe_graph
from .parakeet_asr import ASR2_FILES, MODEL_FILES, PHONE_FILES, SERVE_FLAG, SPEAKER_FILES, VAD_FILES

ENGINES = ("qwen3", "parakeet")
# the evidence blocks Parakeet's child gathers beside its own, with the model folder and the files each needs
EXTRA_MODELS = {"vad": ("VAD_MODEL", VAD_FILES, "SERIAL_AUDIO_VAD_MODEL"),
                "speaker": ("SPEAKER_MODEL", SPEAKER_FILES, "SERIAL_AUDIO_SPEAKER_MODEL"),
                "asr2": ("ASR2_MODEL", ASR2_FILES, "SERIAL_AUDIO_ASR2_MODEL"),
                "phone": ("PHONE_MODEL", PHONE_FILES, "SERIAL_AUDIO_PHONE_MODEL")}
# what loading the optional models gave in this process, by the folders it loaded them from
_LOADS: dict[tuple, tuple[set[str], dict[str, str]]] = {}
QWEN3_MODEL = "qwen3_asr_1.7b_bf16.safetensors"
PARAKEET_SCRIPT = Path(__file__).with_name("parakeet_asr.py")
PARAKEET_THREADS = 4
FILE_TIMEOUT = 300
LOAD_TIMEOUT = 600
IMPORT_TIMEOUT = 120
EXIT_TIMEOUT = 30
# whether a process keeps one parakeet_asr.py up for all its jobs
SERVE = True
SERVE_STARTS = 3
CONNECT_TIMEOUT = 10
KEPT_NAME = "asr"


class _Served:
    """A parakeet_asr.py that serves jobs on a port of this machine."""

    def __init__(self, port: int, token: str, pid: int, proc: subprocess.Popen | None = None, err=None):
        self.port, self.token, self.pid, self.proc, self.err = port, token, pid, proc, err

    def alive(self) -> bool:
        return self.proc.poll() is None if self.proc is not None else kept.alive(self.pid)

    def answers(self, job: dict, first: float, each: float) -> Iterator[dict]:
        """Sends one job and yields each JSON object of the answer until the process closes the connection.

        job: the job, as parakeet_asr describes it, without the token.
        first, each: seconds to wait for the first object, and for each one after it.
        Raises OSError when the connection fails or an object does not arrive in time.
        """
        with socket.create_connection(("127.0.0.1", self.port), timeout=CONNECT_TIMEOUT) as conn:
            conn.sendall((json.dumps({**job, "token": self.token}) + "\n").encode("utf-8"))
            conn.settimeout(first)
            with conn.makefile("rb") as reader:
                for raw in reader:
                    try:
                        record = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        yield record
                        conn.settimeout(each)

    def kill(self) -> None:
        """Ends the process and drops it from the worker's registry. Never raises."""
        try:
            found = kept.recorded(KEPT_NAME)
            if found is not None and found.get("pid") == self.pid:
                kept.forget(KEPT_NAME)
                kept.end(self.pid)
            if self.proc is not None:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
                if self.proc.poll() is None:
                    self.proc.kill()
                    self.proc.wait(timeout=EXIT_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            if self.err is not None:
                try:
                    self.err.close()
                except OSError:
                    pass
                self.err = None


_served: _Served | None = None
_serve_starts = 0


def serving() -> _Served | None:
    """The parakeet_asr.py that serves this process's jobs, found or started when none runs.

    Returns None when SERVE is off, when the process cannot start, and after SERVE_STARTS starts in this process.
    The caller then runs each job in a process of its own. Under a worker's registry the process is the one an
    earlier step left up, and one started here stays up after this process. Never raises.
    """
    global _served, _serve_starts
    if not SERVE:
        return None
    if _served is not None and _served.alive():
        return _served
    if _served is not None:
        _served.kill()
    _served = None
    found = kept.entry(KEPT_NAME)
    if found:
        _served = _Served(found["port"], str(found.get("token") or ""), found["pid"])
        return _served
    if _serve_starts >= SERVE_STARTS:
        return None
    _serve_starts += 1
    try:
        _served = _start_serving()
    except (OSError, subprocess.SubprocessError, ValueError):
        _served = None
        _serve_starts = SERVE_STARTS
    return _served


def _start_serving() -> _Served:
    """Starts a serving parakeet_asr.py. Raises OSError or ValueError when it does not start or name its port."""
    stay = kept.registry() is not None
    token = secrets.token_hex(16)
    err = open(kept.registry().with_suffix(".asr.log"), "ab") if stay else tempfile.TemporaryFile()
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if machine.WINDOWS else {}
    try:
        proc = subprocess.Popen([str(machine.AUDIO_PYTHON), "-I", str(PARAKEET_SCRIPT), SERVE_FLAG],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, **kwargs)
    except OSError:
        err.close()
        raise
    served = _Served(0, token, proc.pid, proc, err)
    lines: queue.Queue = queue.Queue()

    def read():
        for raw in proc.stdout:
            lines.put(raw)
        lines.put(None)

    threading.Thread(target=read, name="parakeet-serving", daemon=True).start()
    try:
        proc.stdin.write((json.dumps({"token": token, "stay": stay}) + "\n").encode("utf-8"))
        proc.stdin.flush()
        while not served.port:
            try:
                raw = lines.get(timeout=IMPORT_TIMEOUT)
            except queue.Empty:
                raise ValueError("the serving Parakeet process did not name its port") from None
            if raw is None:
                raise ValueError("the serving Parakeet process ended while it started")
            try:
                said = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(said, dict) and isinstance(said.get("port"), int):
                served.port = said["port"]
    except BaseException:
        served.kill()
        raise
    if stay and kept.record(KEPT_NAME, proc.pid, port=served.port, token=token):
        # the worker ends it, so this process only forgets it
        served.proc = None
        proc.stdin.close()
    else:
        atexit.register(served.kill)
    return served


def choose(engine: str | None = None) -> str:
    """The recognizer to use.

    engine: "qwen3" or "parakeet" to force one.
    Returns engine, else SERIAL_AUDIO_ASR, else parakeet when its model folder exists, else qwen3. Raises ComfyError
    when the name is unknown.
    """
    engine = engine or machine.ASR or ("parakeet" if machine.PARAKEET_MODEL.is_dir() else "qwen3")
    if engine not in ENGINES:
        raise ComfyError(f"unknown speech recognizer {engine!r}; known: {', '.join(ENGINES)}")
    return engine


def model_name(engine: str) -> str:
    return QWEN3_MODEL if engine == "qwen3" else f"parakeet:{machine.PARAKEET_MODEL.name}"


def extra_blocks() -> set[str]:
    """The blocks of EXTRA_MODELS this machine can gather: those whose model folder holds all its files and whose
    model loads under the audio Python. The load is tried once per process for each set of folders. Never raises.
    """
    folders = _present()
    if not folders:
        return set()
    key = tuple(folders.items())
    if key not in _LOADS:
        _LOADS[key] = _load(folders)
    return set(folders) & _LOADS[key][0]


def _present() -> dict[str, str]:
    """{block: folder} for the blocks of EXTRA_MODELS whose folder exists and holds all its files."""
    problems = _file_problems()
    return {block: str(getattr(machine, EXTRA_MODELS[block][0])) for block in EXTRA_MODELS
            if block not in problems and _folder_exists(block)}


def _load(folders: dict[str, str]) -> tuple[set[str], dict[str, str]]:
    """(the blocks whose models loaded, {block: error} for the others), from the child's check of the folders."""
    job = {"check": True, "blocks": sorted(folders), "model": str(machine.PARAKEET_MODEL),
           "threads": PARAKEET_THREADS, **folders}
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if machine.WINDOWS else {}
    try:
        got = _served_check(job)
        if got is None:
            done = subprocess.run([str(machine.AUDIO_PYTHON), "-I", str(PARAKEET_SCRIPT)], input=json.dumps(job),
                                  capture_output=True, text=True, timeout=LOAD_TIMEOUT, **kwargs)
            got = json.loads((done.stdout or "").strip().splitlines()[-1])
        listed, failed = got.get("loaded"), got.get("errors")
        loaded = {b for b in (listed if isinstance(listed, list) else []) if isinstance(b, str) and b in folders}
        errors = {b: str(e) for b, e in (failed.items() if isinstance(failed, dict) else ()) if b in folders}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, AttributeError, TypeError,
            RecursionError) as e:
        return set(), {b: f"the audio Python could not load it: {type(e).__name__}: {e}" for b in folders}
    return loaded, {**{b: "it did not load" for b in folders if b not in loaded and b not in errors}, **errors}


def _served_check(job: dict) -> dict | None:
    """The serving process's answer to a check job, or None when no process serves or it gave no answer."""
    served = serving()
    if served is None:
        return None
    try:
        for record in served.answers(job, LOAD_TIMEOUT, LOAD_TIMEOUT):
            if "loaded" in record:
                return record
    except OSError:
        served.kill()
    return None


def _folder_exists(block: str) -> bool:
    try:
        return getattr(machine, EXTRA_MODELS[block][0]).is_dir()
    except OSError:
        return False


def model_problems() -> dict[str, str]:
    """The take check's optional model folders that exist and lack a file, or whose model didn't load.

    Returns {block: problem}, for the blocks of EXTRA_MODELS, with a problem that names the
    missing files or the load error, and the variable that moves the folder. A folder that doesn't exist is not a
    problem, and a load is reported only after extra_blocks tried it in this process. Never raises.
    """
    out = _file_problems()
    _, errors = _LOADS.get(tuple(_present().items()), (set(), {}))
    for block, error in errors.items():
        out.setdefault(block, f"the take check's {block} model doesn't load ({error}); see {EXTRA_MODELS[block][2]}")
    return out


def _file_problems() -> dict[str, str]:
    out = {}
    for block, (attr, names, variable) in EXTRA_MODELS.items():
        try:
            folder = getattr(machine, attr)
            if folder.is_dir():
                missing = [str(folder / name) for name in names if not (folder / name).is_file()]
                if missing:
                    out[block] = f"the take check's {block} model needs {', '.join(missing)}; see {variable}"
        except OSError:
            pass
    return out


def check_ready(engine: str, comfy_url: str) -> None:
    """Raises ComfyError when the recognizer cannot run on this machine.

    engine: "qwen3" or "parakeet".
    comfy_url: the ComfyUI that runs Qwen3-ASR.
    For qwen3, ComfyUI must answer. For parakeet, the model files must exist and the audio Python must import
    sherpa-onnx, soundfile, numpy and scipy. A folder of EXTRA_MODELS that doesn't exist or lacks a file only leaves
    its block out, and model_problems lists the second kind.
    """
    if engine == "qwen3":
        try:
            ComfyClient(comfy_url).system_stats()
        except Exception as e:
            raise ComfyError(f"Qwen3-ASR needs ComfyUI at {comfy_url}, which did not answer: {e}") from None
        return
    folder = machine.PARAKEET_MODEL
    missing = [str(folder / name) for name in MODEL_FILES if not (folder / name).is_file()]
    if missing:
        raise ComfyError(f"Parakeet needs {', '.join(missing)}; see SERIAL_AUDIO_PARAKEET_MODEL")
    # a serving process imported the packages when it started
    if serving() is not None:
        return
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if machine.WINDOWS else {}
    try:
        done = subprocess.run([str(machine.AUDIO_PYTHON), "-I", "-c",
                               "import sherpa_onnx, soundfile, numpy, scipy.signal"],
                              capture_output=True, text=True, timeout=IMPORT_TIMEOUT, **kwargs)
    except (OSError, subprocess.SubprocessError) as e:
        raise ComfyError(f"Parakeet could not run {machine.AUDIO_PYTHON}: {e}") from None
    if done.returncode != 0:
        raise ComfyError(f"Parakeet needs sherpa-onnx, soundfile, numpy and scipy in {machine.AUDIO_PYTHON}: "
                         f"{(done.stderr or done.stdout).strip()[-500:]}")


def records(files: list[Path], *, engine: str, comfy_url: str,
            blocks: tuple[str, ...] = ("asr", "signal")) -> Iterator[tuple[int, dict]]:
    """Hears the files in order.

    files: the audio files.
    engine: "qwen3" or "parakeet".
    comfy_url: the ComfyUI that runs Qwen3-ASR.
    blocks: for parakeet, the evidence to gather, from parakeet_asr.BLOCKS. A block of EXTRA_MODELS that this
        machine can't gather, by extra_blocks, is left out, and so is one whose model doesn't load. Qwen3-ASR gives
        the transcript only.
    Yields (index, record) for each file, in order, as each finishes. A record holds the transcript as "text" when
    the transcript was asked for. Parakeet adds its raw "tokens" with their "starts", "durations" and "logprobs",
    one of each per token, and a block under its own name for each other block it gathered, as parakeet_asr
    describes them. Raises ComfyError when a file lies outside
    ComfyUI's output folder for qwen3, when a file cannot be transcribed, or when the recognizer fails or stalls.
    """
    if not files:
        return
    if engine == "qwen3":
        yield from _qwen3(files, comfy_url)
    else:
        yield from _parakeet(files, blocks)


def _qwen3(files: list[Path], comfy_url: str) -> Iterator[tuple[int, dict]]:
    output = machine.COMFY_OUTPUT
    refs = []
    for file in files:
        try:
            refs.append(Path(file).relative_to(output).as_posix())
        except ValueError:
            raise ComfyError(f"{file} is outside the ComfyUI output folder, so Qwen3-ASR cannot load it") from None
    client = ComfyClient(comfy_url, output_dir=output)
    client.free()
    try:
        for i, rel in enumerate(refs):
            entry = client.run(transcribe_graph(clip_ref=f"{rel} [output]", asr_model=QWEN3_MODEL), timeout=FILE_TIMEOUT)
            yield i, {"text": " ".join(entry.get("outputs", {}).get("transcript", {}).get("text") or [])}
    finally:
        try:
            client.free()
        except Exception:
            pass


def _parakeet(files: list[Path], blocks: tuple[str, ...]) -> Iterator[tuple[int, dict]]:
    """records() for Parakeet. The serving process hears the files, and a process of its own hears the ones that
    are left when none serves or the serving process ends early."""
    extras = extra_blocks() if set(blocks) & set(EXTRA_MODELS) else set()
    job = {"model": str(machine.PARAKEET_MODEL), "files": [str(f) for f in files], "threads": PARAKEET_THREADS,
           "blocks": [b for b in blocks if b not in EXTRA_MODELS or b in extras],
           **{b: str(getattr(machine, EXTRA_MODELS[b][0])) for b in sorted(extras)}}
    heard = 0
    served = serving()
    if served is not None:
        for i, record in _parakeet_served(served, job, files):
            heard = i + 1
            yield i, record
    if heard < len(files):
        rest = {**job, "files": job["files"][heard:]}
        for i, record in _parakeet_once(files[heard:], rest):
            yield heard + i, record


def _shaped(record: dict, job: dict) -> dict:
    """A file's record as records() yields it: without its index, and with its text when the job asked for it."""
    return {**{k: v for k, v in record.items() if k != "index"},
            **({"text": str(record.get("text") or "")} if "asr" in job["blocks"] else {})}


def _parakeet_served(served: _Served, job: dict, files: list[Path]) -> Iterator[tuple[int, dict]]:
    """Hears files with the serving process.

    Yields (index, record) for each file it heard, in order. Returns early, having ended the process, when the
    process stops answering before the last file, so the caller hears the rest another way. Raises ComfyError
    when a file cannot be transcribed, when the job cannot run, or when a record does not arrive in time.
    """
    expected = 0
    try:
        for record in served.answers(job, LOAD_TIMEOUT, FILE_TIMEOUT):
            if "fatal" in record:
                raise ComfyError(f"Parakeet stopped after {expected} of {len(files)} files: {record['fatal']}")
            if "done" in record:
                return
            if record.get("index") != expected:
                continue
            if "error" in record:
                raise ComfyError(f"Parakeet could not transcribe {files[expected]}: {record['error']}")
            yield expected, _shaped(record, job)
            expected += 1
    except socket.timeout:
        served.kill()
        wait = FILE_TIMEOUT if expected else LOAD_TIMEOUT
        raise ComfyError(f"Parakeet gave no transcript for {files[expected]} within {wait} s") from None
    except OSError:
        pass
    served.kill()


def _parakeet_once(files: list[Path], job: dict) -> Iterator[tuple[int, dict]]:
    """Hears files with a parakeet_asr.py that does this one job, as records() describes."""
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if machine.WINDOWS else {}
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen([str(machine.AUDIO_PYTHON), "-I", str(PARAKEET_SCRIPT)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=err, **kwargs)
        except OSError as e:
            raise ComfyError(f"could not start Parakeet under {machine.AUDIO_PYTHON}: {e}") from None
        lines: queue.Queue = queue.Queue()

        def read():
            for raw in proc.stdout:
                lines.put(raw)
            lines.put(None)

        reader = threading.Thread(target=read, name="parakeet-output", daemon=True)
        reader.start()
        try:
            try:
                proc.stdin.write(json.dumps(job).encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass  # the child exited before reading its job, and the count check below reports its stderr
            expected, wait = 0, LOAD_TIMEOUT
            while True:
                try:
                    raw = lines.get(timeout=wait)
                except queue.Empty:
                    raise ComfyError(f"Parakeet gave no transcript for {files[expected]} within {wait} s") from None
                if raw is None:
                    break
                try:
                    record = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if "done" in record:
                    break
                if record.get("index") != expected:
                    continue
                if "error" in record:
                    raise ComfyError(f"Parakeet could not transcribe {files[expected]}: {record['error']}")
                yield expected, _shaped(record, job)
                expected, wait = expected + 1, FILE_TIMEOUT
            try:
                proc.wait(timeout=EXIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if expected < len(files):
                err.seek(0)
                tail = err.read().decode("utf-8", "replace").strip()[-1500:]
                raise ComfyError(f"Parakeet stopped after {expected} of {len(files)} files "
                                 f"(exit {proc.returncode}): {tail}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            reader.join(timeout=EXIT_TIMEOUT)
            for pipe in (proc.stdin, proc.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass


# Word comparison

ONES = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
SCALES = {"hundred": 100, "thousand": 1000, "million": 10**6, "billion": 10**9}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fifth": 5, "eighth": 8, "ninth": 9, "twelfth": 12}
ORDINALS.update({w + "th": v for w, v in ONES.items() if v >= 4 and w + "th" not in ("fiveth", "eightth", "nineth",
                                                                                     "twelveth")})
ORDINALS.update({w[:-1] + "ieth": v for w, v in TENS.items()})
ORDINALS.update({w + "th": v for w, v in SCALES.items()})
NUMBER_WORDS = set(ONES) | set(TENS) | set(SCALES) | set(ORDINALS)
TOKEN_RE = re.compile(r"[a-z0-9']+|[,.;:!?]")
TITLES = {"mr": "mister", "mrs": "missus", "dr": "doctor"}
TITLE_RE = re.compile(r"\b(mr|mrs|dr)\b\.?")
STUTTER_RE = re.compile(r"\b([a-z]|[cstw]h)-(?=\1)")
PUNCTUATION = frozenset(",.;:!?")
DIGIT_ORDINAL_RE = re.compile(r"^(\d+)(?:st|nd|rd|th)$")


def _symbols(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'").replace("′", "'").replace("″", '"')
    text = "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))
    text = TITLE_RE.sub(lambda m: TITLES[m.group(1)], text)
    text = STUTTER_RE.sub("", text)
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    text = re.sub(r"(?<=\d)\.(?=\d)", " point ", text)
    text = re.sub(r"(?<=\d):(?=\d\d\b)", " ", text)
    text = re.sub(r"\$\s*(\d+)", r"\1 dollars", text)
    text = re.sub(r"£\s*(\d+)", r"\1 pounds", text)
    text = text.replace("%", " percent ").replace("°", " degrees ")
    text = re.sub(r"(degrees\s+\d+)\s*'", r"\1 minutes ", text)
    return re.sub(r"(minutes\s+\d+)\s*\"", r"\1 seconds ", text)


def _number_groups(tokens: list[str]) -> list[tuple[str, int]]:
    """Each run of number words as its digits and an ordinal as <n>th, with the index of the last token each word
    was made from. A two digit group right after a group of one or two digits joins it, so "eighteen sixty six"
    matches "1866" and "ten thirty" matches "10:30"."""
    out: list[tuple[str, int]] = []
    joinable = False

    def push(digits: str, last: int) -> None:
        nonlocal joinable
        if joinable and len(out[-1][0]) <= 2 and len(digits) == 2:
            out[-1] = (out[-1][0] + digits, last)
            joinable = False
        else:
            out.append((digits, last))
            joinable = True

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        start = tok in NUMBER_WORDS or (tok == "a" and nxt in SCALES)
        if not start:
            m = DIGIT_ORDINAL_RE.match(tok)
            if m:
                out.append((f"{int(m.group(1))}th", i))
                joinable = False
            elif tok.isdigit():
                push(tok, i)
            elif tok in PUNCTUATION:
                joinable = False
            else:
                out.append((tok, i))
                joinable = False
            i += 1
            continue
        total, current, last, ordinal = 0, 0, None, False
        while i < len(tokens):
            tok = tokens[i]
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if tok == "a" and last is None and nxt in SCALES:
                current, last = 1, "one"
            elif tok == "and" and last == "scale" and (nxt in ONES or nxt in TENS or nxt in ORDINALS):
                pass
            elif tok in ONES or tok in ORDINALS and ORDINALS[tok] < 100:
                value = ONES.get(tok, ORDINALS.get(tok))
                if last in ("one", "teen") or (last == "tens" and value >= 10):
                    break
                current += value
                last = "teen" if value >= 10 else "one"
                if tok in ORDINALS and tok not in ONES:
                    ordinal = True
                    i += 1
                    break
            elif tok in TENS:
                if last in ("one", "teen", "tens"):
                    break
                current += TENS[tok]
                last = "tens"
            elif tok in SCALES or tok in ORDINALS:
                scale = SCALES.get(tok) or ORDINALS[tok]
                if scale == 100:
                    current = max(current, 1) * 100
                else:
                    total += max(current, 1) * scale
                    current = 0
                last = "scale"
                if tok in ORDINALS:
                    ordinal = True
                    i += 1
                    break
            else:
                break
            i += 1
        value = str(total + current)
        if ordinal:
            out.append((value + "th", i - 1))
            joinable = False
        else:
            push(value, i - 1)
    return out


def word_map(text: str) -> list[tuple[str, str]]:
    """The comparable words of a line or transcript, each with what stands between it and the next word.

    text: a line or a transcript.
    Returns [(word, after), ...]. The words are the ones words() gives. after holds every character between the
    word and the next one, or the end of the text, that is not a space, so "" means only spaces lie between.
    """
    spoken = _symbols(text)
    found = [(m.group().strip("'"), m.start(), m.end()) for m in TOKEN_RE.finditer(spoken)]
    found = [(w[:-2] if w.endswith("s's") else w, a, b) for w, a, b in found if w]
    out = []
    for word, last in _number_groups([f[0] for f in found]):
        nxt = last + 1
        while nxt < len(found) and found[nxt][0] in PUNCTUATION:
            nxt += 1
        gap = spoken[found[last][2]: found[nxt][1] if nxt < len(found) else len(spoken)]
        out.append((word, "".join(gap.split())))
    return out


def words(text: str) -> list[str]:
    """The comparable words of a line or transcript.

    Lower case, straight apostrophes, no accents, no punctuation. Numbers become digits whether they are spelled out
    or written as digits, ordinals become <n>th, and %, °, $ and £ become words, so "a hundred and fifty" and "150"
    compare equal. Mr, Mrs and Dr become mister, missus and doctor, and a possessive of a word that ends in s loses
    its 's, so "Thalas's" and "Thalas'" compare equal. The fragment of a stutter is left out, so "Y-yes" is "yes".
    """
    return [word for word, _ in word_map(text)]
