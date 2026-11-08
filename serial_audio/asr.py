"""Speech recognition for the casting check and the take check, and the word comparison both use.

Two recognizers. "qwen3" is Qwen3-ASR in ComfyUI, one prompt per file, so the files must lie under ComfyUI's output
folder. "parakeet" is NVIDIA Parakeet TDT 0.6B v2 through sherpa-onnx, which parakeet_asr.py runs on the CPU under
the audio Python, with no ComfyUI.
"""
from __future__ import annotations

import json
import queue
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Iterator

from . import machine
from .comfy import ComfyClient, ComfyError, transcribe_graph

ENGINES = ("qwen3", "parakeet")
QWEN3_MODEL = "qwen3_asr_1.7b_bf16.safetensors"
PARAKEET_SCRIPT = Path(__file__).with_name("parakeet_asr.py")
PARAKEET_THREADS = 4
FILE_TIMEOUT = 300
LOAD_TIMEOUT = 600


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


def check_ready(engine: str, comfy_url: str) -> None:
    """Raises ComfyError when the recognizer cannot run on this machine: ComfyUI does not answer for qwen3, or the
    Parakeet model folder or the audio Python is missing for parakeet."""
    if engine == "qwen3":
        try:
            ComfyClient(comfy_url).system_stats()
        except Exception as e:
            raise ComfyError(f"Qwen3-ASR needs ComfyUI at {comfy_url}, which did not answer: {e}") from None
        return
    for path in (machine.PARAKEET_MODEL, machine.AUDIO_PYTHON):
        if not path.exists():
            raise ComfyError(f"Parakeet needs {path}; see SERIAL_AUDIO_PARAKEET_MODEL and SERIAL_AUDIO_AUDIO_PYTHON")


def transcripts(files: list[Path], *, engine: str, comfy_url: str) -> Iterator[tuple[int, str]]:
    """Transcribes the files in order.

    files: the audio files.
    engine: "qwen3" or "parakeet".
    comfy_url: the ComfyUI that runs Qwen3-ASR.
    Yields (index, transcript) for each file, in order, as each finishes. Raises ComfyError when a file lies outside
    ComfyUI's output folder for qwen3, when a file cannot be transcribed, or when the recognizer fails or stalls.
    """
    if not files:
        return
    if engine == "qwen3":
        yield from _qwen3(files, comfy_url)
    else:
        yield from _parakeet(files)


def _qwen3(files: list[Path], comfy_url: str) -> Iterator[tuple[int, str]]:
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
            yield i, " ".join(entry.get("outputs", {}).get("transcript", {}).get("text") or [])
    finally:
        try:
            client.free()
        except Exception:
            pass


def _parakeet(files: list[Path]) -> Iterator[tuple[int, str]]:
    job = {"model": str(machine.PARAKEET_MODEL), "files": [str(f) for f in files], "threads": PARAKEET_THREADS}
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

        threading.Thread(target=read, name="parakeet-output", daemon=True).start()
        try:
            proc.stdin.write(json.dumps(job).encode("utf-8"))
            proc.stdin.close()
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
                if "done" in record:
                    break
                if record.get("index") != expected:
                    continue
                if "error" in record:
                    raise ComfyError(f"Parakeet could not transcribe {files[expected]}: {record['error']}")
                yield expected, str(record.get("text") or "")
                expected, wait = expected + 1, FILE_TIMEOUT
            proc.wait(timeout=30)
            if expected < len(files):
                err.seek(0)
                tail = err.read().decode("utf-8", "replace").strip()[-1500:]
                raise ComfyError(f"Parakeet stopped after {expected} of {len(files)} files "
                                 f"(exit {proc.returncode}): {tail}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


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
DIGIT_ORDINAL_RE = re.compile(r"^(\d+)(?:st|nd|rd|th)$")


def _symbols(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'").replace("′", "'").replace("″", '"')
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    text = re.sub(r"(?<=\d)\.(?=\d)", " point ", text)
    text = re.sub(r"(?<=\d):(?=\d\d\b)", " ", text)
    text = re.sub(r"\$\s*(\d+)", r"\1 dollars", text)
    text = re.sub(r"£\s*(\d+)", r"\1 pounds", text)
    text = text.replace("%", " percent ").replace("°", " degrees ")
    text = re.sub(r"(degrees\s+\d+)\s*'", r"\1 minutes ", text)
    return re.sub(r"(minutes\s+\d+)\s*\"", r"\1 seconds ", text)


def _number_groups(tokens: list[str]) -> list[str]:
    """Each run of number words as its digits and an ordinal as <n>th. A two digit group right after a group of
    one or two digits joins it, so "eighteen sixty six" matches "1866" and "ten thirty" matches "10:30"."""
    out: list[str] = []
    joinable = False

    def push(digits: str) -> None:
        nonlocal joinable
        if joinable and len(out[-1]) <= 2 and len(digits) == 2:
            out[-1] += digits
            joinable = False
        else:
            out.append(digits)
            joinable = True

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        start = tok in NUMBER_WORDS or (tok == "a" and nxt in SCALES)
        if not start:
            m = DIGIT_ORDINAL_RE.match(tok)
            if m:
                out.append(f"{int(m.group(1))}th")
                joinable = False
            elif tok.isdigit():
                push(tok)
            elif tok in ",.;:!?":
                joinable = False
            else:
                out.append(tok)
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
            out.append(value + "th")
            joinable = False
        else:
            push(value)
    return out


def words(text: str) -> list[str]:
    """The comparable words of a line or transcript.

    Lower case, straight apostrophes, no punctuation. Numbers become digits whether they are spelled out or written
    as digits, ordinals become <n>th, and %, °, $ and £ become words, so "a hundred and fifty" and "150" compare equal.
    """
    tokens = [t.strip("'") for t in TOKEN_RE.findall(_symbols(text))]
    return _number_groups([t for t in tokens if t])
