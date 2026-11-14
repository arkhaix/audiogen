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
import functools
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

from . import kept
from . import library as lib
from . import machine
from . import screenplay
from .asr import choose as choose_asr
from .asr import word_map, words
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
CPP_START_POLL = 0.25
KEPT_NAME = "breeze"
COMFY_IDLE_TIMEOUT = 4 * 3600
RETAKES = 2
RETAKE_SEED_STEP = 7919
CHECK_RETAKES = 2
NAME_RETAKES = 5
RETAKE_FLOOR = 8
RETAKE_SHARE = 0.125
LONG_LINE = 60
PIECE_WORDS = 40
PIECE_GAP = 0.25
SENTENCE_ENDS = ".!?" + chr(0x2026)
CLAUSE_ENDS = ",;:" + chr(0x2014)
CLOSING_MARKS = "\"')" + chr(0x201D) + chr(0x2019)
SENTENCE_END_RE = re.compile(rf"(?:(?<=[{SENTENCE_ENDS}])|(?<=[{SENTENCE_ENDS}][{CLOSING_MARKS}]))\s+")
CLAUSE_END_RE = re.compile(rf"(?:(?<=[{CLAUSE_ENDS}])|(?<=[{CLAUSE_ENDS}][{CLOSING_MARKS}]))\s+"
                           rf"|(?<={chr(0x2014)})(?=\w)")
# the version of the join, which a render records as render.join
JOIN_VERSION = 2
GAP_CONTINUES = 0.15
GAP_SAME = 0.35
GAP_CHANGE = 0.5
ASIDE_RE = re.compile(r"\([^()]*\)")
QUOTE_MARKS = "\"'" + chr(0x201C) + chr(0x201D) + chr(0x2018) + chr(0x2019)

DIRECTION_STEMS = {
    "whisper": ("whisper", "breath", "inaudibl"),
    "soft": ("soft", "quiet", "murmur", "mutter", "mumbl", "hush", "gentl", "low", "thought", "faint", "meek", "timid"),
    "shout": ("shout", "yell", "scream", "roar", "bellow", "shriek", "howl", "holler"),
    "raised": ("loud", "raised", "exclaim", "calling", "barking", "booming", "snapp", "furious", "outrage", "angr"),
}
# the level a take of each class sits at against its voice's ordinary level, in dB
LEVEL_OFFSETS = {"whisper": -6.0, "soft": -3.0, "normal": 0.0, "raised": 2.0, "shout": 3.0}
PACE_WORDS = frozenset(
    "slow slowly hesitant hesitantly hesitating hesitation halting haltingly pause pauses pausing stammering "
    "stuttering faltering trailing drawn measured deliberate deliberately weak weakly dying gasping struggling "
    "sobbing crying choked choking panting breathless exhausted".split())
NAME_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
NAME_SKIP = frozenset("the and von van der del las los des".split())
NAME_MARK = "zqx{}qzx"
NAME_MARK_RE = re.compile(r"zqx(\d+)qzx")


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


def without_events(text: str) -> str:
    """text without its vocal events in parentheses, which a voice performs and doesn't say."""
    return re.sub(r"\(([^()]*)\)", lambda m: " " if m.group(1).strip().lower() in VOCAL_EVENTS else m.group(0), text)


def name_terms(bible: dict) -> dict[str, tuple[str, str]]:
    """The strings that mark a name in a script line, as {string: (term, spoken)}.

    bible: the serial's bible.
    A say-as term is spoken as its say. Each other word of a pronunciation term, and each capitalized word of a
    character's name, is a term of its own that is spoken as written. Words of one or two letters and NAME_SKIP are
    left out. voice_text matches a pronunciation term of one word with its first letter in either case, and any
    other string as written.
    """
    out: dict[str, tuple[str, str]] = {}
    for term in bible.get("pronunciations") or {}:
        for word in NAME_WORD_RE.findall(str(term)):
            if len(word) > 2 and word.lower() not in NAME_SKIP:
                out[word] = (word, word)
    for character in (bible.get("characters") or {}).values():
        for word in NAME_WORD_RE.findall(str((character or {}).get("name") or "")):
            if len(word) > 2 and word[0].isupper() and word.lower() not in NAME_SKIP:
                out[word] = (word, word)
    for term, say in say_as_map(bible).items():
        out[term] = (term, say)
    return out


def placed_names(plain: list[dict], found: list[tuple[str, str]]) -> list[dict]:
    """A token map with each name marked where its spoken words stand.

    plain: a line's token map without names.
    found: (term, spoken) for each name in the line, in the line's order.
    Returns a copy of plain. Each name is looked for after the name before it, and its last word matches with or
    without a possessive 's. A name whose words aren't there stays unmarked.
    """
    out = [dict(t) for t in plain]
    at = 0
    for term, spoken in found:
        said = words(spoken)
        for i in range(at, len(out) - len(said) + 1) if said else ():
            got = [t["w"] for t in out[i: i + len(said)]]
            if got[:-1] == said[:-1] and got[-1] in (said[-1], said[-1] + "'s"):
                for t in out[i: i + len(said)]:
                    t["name"] = term
                at = i + len(said)
                break
    return out


def name_marks(bible: dict) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]], re.Pattern | None]:
    """What voice_text reads of a bible to find the names in a line.

    bible: the serial's bible.
    Returns (terms, spelled, pattern): name_terms(bible), the same by each string as a line may spell it, and the
    expression that finds a name in a line. pattern is None for a bible without names. The three are shared between
    calls, so a caller must not change them.
    """
    said = tuple((term, entry.get("say") if isinstance(entry, dict) else None)
                 for term, entry in (bible.get("pronunciations") or {}).items())
    names = tuple(str((character or {}).get("name") or "") for character in (bible.get("characters") or {}).values())
    try:
        return _name_marks(said, names)
    except TypeError:
        # a say-as that can't be hashed
        return _name_marks.__wrapped__(said, names)


@functools.lru_cache(maxsize=16)
def _name_marks(said: tuple, names: tuple) -> tuple[dict, dict, re.Pattern | None]:
    """name_marks for a bible with these (term, say-as) pairs and these character names, each in the bible's order."""
    bible = {"pronunciations": {term: {"say": say} for term, say in said},
             "characters": dict(enumerate({"name": name} for name in names))}
    terms = name_terms(bible)
    if not terms:
        return terms, {}, None
    exact = set(say_as_map(bible))
    loose = {str(term) for term in bible["pronunciations"] if NAME_WORD_RE.fullmatch(str(term))} - exact
    spelled = {(t[0].lower() + t[1:] if t in loose else t).replace("’", "'"): v for t, v in terms.items()}

    def loosely(term: str) -> str:
        """A pattern for a name: either apostrophe, and either case of the first letter of a pronunciation term."""
        body = re.sub("['’]", "['’]", re.escape(term[1:]))
        first = f"[{re.escape(term[0].lower())}{re.escape(term[0].upper())}]" if term in loose else re.escape(term[0])
        return first + body

    # a say-as term matches where and as apply_say_as matches it
    pattern = "|".join([re.escape(t) for t in sorted(exact, key=len, reverse=True)]
                       + [loosely(t) for t in sorted(terms, key=len, reverse=True) if t not in exact])
    return terms, spelled, re.compile(rf"(?<!\w)(?:{pattern})(?!\w)")


def unbroken(text: str, bible: dict) -> list[tuple[int, int]]:
    """The parts of a line's text that an asterisk must not split, as (start, end).

    text: the line's text.
    bible: the serial's bible.
    Returns each say-as term where apply_say_as matches it, and each vocal event with its parentheses.
    """
    found = [m.span() for m in re.finditer(r"\(([^()]*)\)", text) if m.group(1).strip().lower() in VOCAL_EVENTS]
    say = say_as_map(bible)
    if say:
        pattern = "|".join(re.escape(term) for term in sorted(say, key=len, reverse=True))
        found += [m.span() for m in re.finditer(rf"(?<!\w)(?:{pattern})(?!\w)", text)]
    return found


def voice_text(line: dict, bible: dict) -> tuple[str, list[dict]]:
    """The text Breeze is sent for a script line, and the line's token map.

    line: the script line.
    bible: the serial's bible. Its say-as overrides replace their terms in the text.
    Returns (text, tokens). tokens has one {"w", "name", "after"} per word of the text, in order: the word as
    asr.words gives it, the term of name_terms it belongs to or "", and every character that is not a space
    between it and the next word. A vocal event in parentheses is not a word. A name that cannot be placed is a
    word without a name. In text, each word or phrase of the line's `emphasis` that screenplay.emphasis_spans
    places stands between asterisks. A phrase whose asterisk would land inside a part that unbroken() names is
    not marked. tokens is the same with and without the asterisks.
    """
    text = speakable(apply_say_as(line["text"], say_as_map(bible)))
    plain = [{"w": w, "name": "", "after": after} for w, after in word_map(without_events(text))]
    if line.get("emphasis"):
        starred = screenplay.emphatic(line["text"], line["emphasis"], unbroken(line["text"], bible))
        text = speakable(apply_say_as(starred, say_as_map(bible)))
    terms, spelled, pattern = name_marks(bible)
    if not terms:
        return text, plain

    # Mark each name with a word that survives asr.words
    found: list[tuple[str, str]] = []

    def mark(m: re.Match) -> str:
        hit = m.group(0)
        straight = hit.replace("’", "'")
        found.append(terms[hit] if hit in terms else spelled.get(straight) or spelled[straight[0].lower() + straight[1:]])
        return NAME_MARK.format(len(found) - 1)

    marked = without_events(speakable(pattern.sub(mark, line["text"])))

    # Put the spoken words of each name back
    tokens = []
    for word, after in word_map(marked):
        m = NAME_MARK_RE.search(word)
        if not m:
            tokens.append({"w": word, "name": "", "after": after})
            continue
        term, spoken = found[int(m.group(1))]
        said = words(spoken) or [""]
        said[0] = word[: m.start()] + said[0]
        said[-1] += word[m.end():]
        tokens += [{"w": w, "name": term, "after": after if n == len(said) - 1 else ""} for n, w in enumerate(said)]
    return text, tokens if [t["w"] for t in tokens] == [t["w"] for t in plain] else placed_names(plain, found)


def direction_class(direction: str | None) -> str:
    """How loud a direction says its line is: "whisper", "soft", "normal", "raised" or "shout".

    direction: the line's direction, or None.
    Returns the loudest or the softest class a word of the direction names, the softest when it names both, and
    "normal" when it names none.
    """
    said = re.findall(r"[a-z]+", (direction or "").lower())
    for name in ("whisper", "soft", "shout", "raised"):
        if any(word.startswith(stem) for word in said for stem in DIRECTION_STEMS[name]):
            return name
    return "normal"


def gap_after(line: dict, following: dict | None) -> float:
    """The silence the join leaves after a line's take, in seconds.

    line: the script line.
    following: the next line that has a take, or None after the last one.
    GAP_CONTINUES when the sentence goes on into the next line: the line's words end without a full stop, a question
    mark, an exclamation mark or an ellipsis, or the next line starts with a lowercase letter. Otherwise GAP_CHANGE
    when the next line has another speaker, and GAP_SAME when it doesn't. Quote marks, brackets and an aside in
    parentheses at either end don't count.
    """
    def bare(a_line: dict | None) -> str:
        return ASIDE_RE.sub(" ", str((a_line or {}).get("text") or "")).strip(QUOTE_MARKS + "()[] \t\n")

    said, comes = bare(line), bare(following)
    if (said and said[-1] not in SENTENCE_ENDS) or comes[:1].islower():
        return GAP_CONTINUES
    return GAP_CHANGE if following is not None and following.get("speaker") != line.get("speaker") else GAP_SAME


def keeps_its_pauses(line: dict) -> bool:
    """Whether a script line's pauses belong to its delivery, so no check may shorten them.

    line: the script line.
    True for a line with a vocal event in parentheses, and for a line whose direction has a word of PACE_WORDS.
    """
    events = {m.strip().lower() for m in re.findall(r"\(([^()]*)\)", line.get("text") or "")}
    said = set(re.findall(r"[a-z]+", (line.get("direction") or "").lower()))
    return bool(events & VOCAL_EVENTS or said & PACE_WORDS)


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
    """A breeze-server on 127.0.0.1 for this process's takes.

    This process starts it and stops it on exit. Under a worker that keeps processes up between its jobs (kept.py),
    start() uses the server that an earlier step or an earlier round of this one left up, and stop() leaves the
    server up for the next. The kept server writes to one log beside the worker's registry, and stop() and kill()
    copy what it wrote for this process to log_path.
    """

    def __init__(self, log_path: Path, log):
        self.url = f"http://127.0.0.1:{CPP_PORT}"
        self.log_path, self.log = log_path, log
        self.proc, self.job, self.log_file = None, None, None
        self.sample_rate = None
        self.kept_pid, self.kept_log, self.kept_from = None, None, 0

    def health(self) -> dict | None:
        if not machine.listening(self.url):
            return None
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data if data.get("status") == "ok" else None
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def refuse_if_busy(self) -> None:
        """Raises ComfyError when a breeze-server process exists or a breeze-server answers on the port, other
        than the one a worker keeps up for this process."""
        found = kept.recorded(KEPT_NAME)
        pids = [str(p) for p in machine.breeze_server_pids() if not (found and p == found["pid"])]
        if pids or (not found and self.health()):
            raise ComfyError(f"another breeze-server is running (pids {', '.join(pids) or 'unknown'}, port {CPP_PORT}); "
                             "stop it before rendering, so two models do not share the card")

    def __enter__(self) -> "CppServer":
        self.refuse_if_busy()
        self.start()
        return self

    def start(self) -> None:
        """Starts the server and waits until it answers, or uses the one a worker keeps up.

        On Windows a job object stops a server this process starts when this process ends. Elsewhere it stays in
        this process's group, so stopping the group stops it. A server that a worker keeps is stopped by the worker
        instead. On Windows it also ends with the worker's process, and elsewhere a worker that is killed leaves it
        to the next worker that takes the GPU lock.
        """
        # Use the kept server
        found = kept.recorded(KEPT_NAME)
        same = found and (found.get("port"), found.get("server"), found.get("model")) == (
            CPP_PORT, str(CPP_SERVER), str(CPP_MODEL))
        health = self.health() if same else None
        if health:
            self.kept_pid, self.sample_rate = found["pid"], int(health.get("sample_rate") or 24000)
            self.follow_kept_log(found.get("log"))
            self.log(f"   breeze-server is still up at {self.url}")
            return
        if found:
            self.log("   the breeze-server that was left up " + ("does not answer" if same else "runs another model")
                     + ", so it is stopped")
            kept.forget(KEPT_NAME)
            kept.end(found["pid"])

        # Start one
        for path in (CPP_SERVER, CPP_MODEL):
            if not path.exists():
                raise ComfyError(f"breeze-server needs {path}; build Breeze-TTS-2.cpp and fetch its Q8_0 weights first")
        registry = kept.registry()
        shared = registry.with_suffix(".breeze.log") if registry else None
        try:
            self.log_file = open(shared, "wb") if shared else open(self.log_path, "ab")
        except OSError:
            shared, self.log_file = None, open(self.log_path, "ab")
        if shared:
            self.follow_kept_log(str(shared))
        self.proc = subprocess.Popen(
            [str(CPP_SERVER), str(CPP_MODEL), "--host", "127.0.0.1", "--port", str(CPP_PORT), "--ws-port", "-1",
             "--voices-dir", str(CPP_HOME / "voices")],
            cwd=str(CPP_HOME), stdout=self.log_file, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if machine.WINDOWS else 0)
        if shared and kept.record(KEPT_NAME, self.proc.pid, CPP_PORT, server=str(CPP_SERVER), model=str(CPP_MODEL),
                                  log=str(shared)):
            self.kept_pid = self.proc.pid
        elif machine.WINDOWS:
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
                time.sleep(CPP_START_POLL)
            self.sample_rate = int(health.get("sample_rate") or 24000)
        except BaseException:
            self.kill()
            raise
        self.log(f"   breeze-server started at {self.url}")

    def follow_kept_log(self, shared: str | None) -> None:
        """Notes where the kept server's log stands, so stop() copies what the server writes from here on."""
        try:
            self.kept_log = Path(shared) if shared else None
            self.kept_from = self.kept_log.stat().st_size if self.kept_log else 0
        except OSError:
            self.kept_log, self.kept_from = None, 0

    def copy_kept_log(self) -> None:
        """Appends what the kept server wrote since follow_kept_log to log_path. Never raises."""
        if self.kept_log is None or self.kept_log == self.log_path:
            return
        try:
            with open(self.kept_log, "rb") as src, open(self.log_path, "ab") as dst:
                src.seek(self.kept_from)
                shutil.copyfileobj(src, dst)
                self.kept_from = src.tell()
        except OSError:
            pass

    def stop(self) -> None:
        """Stops the server, unless a worker keeps it up for the next job."""
        if self.kept_pid is None:
            self.kill()
            return
        self.copy_kept_log()
        if self.log_file is not None:
            self.log_file.close()
        self.proc, self.log_file, self.kept_pid, self.kept_log = None, None, None, None

    def kill(self) -> None:
        """Stops the server, also one a worker keeps up."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.kept_pid is not None:
            kept.forget(KEPT_NAME)
            kept.end(self.kept_pid)
        if self.job is not None:
            close_handle(self.job)
        if self.log_file is not None:
            self.log_file.close()
        self.copy_kept_log()
        self.proc, self.job, self.log_file, self.kept_pid, self.kept_log = None, None, None, None, None

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
            self.kill()
            self.start()
            return self.speech(fields, wav)


def cpp_take(server, wav: bytes, transcript: str, text: str, direction: str, vseed: int, line: int, log,
             first: int = 0) -> dict:
    """Takes one line with breeze-server, again with a new seed while its length does not fit the line.

    server: a started CppServer.
    wav, transcript: the speaker's reference clip and what it says.
    text, direction: the line as the voice reads it, and its direction or "".
    vseed: the voice's seed.
    line: the line's number, for the log.
    log: receives a line for each take whose length does not fit.
    first: how many seeds of this line earlier calls used, so this call uses the next ones.
    Returns {"pcm", "seed", "tries", "fits"} for the try whose length is closest to the plausible range, after at
    most RETAKES + 1 tries. fits says whether it is inside the range. Raises ComfyError when a take fails twice.
    """
    fields = {"text": text, "ref_text": transcript, "seed": vseed,
              "cfg_scale": DIRECTION_CFG if direction else CLONE_CFG, **CPP_FIELDS}
    if direction:
        fields["instruction"] = instruction(direction)
    low, high = take_bounds(count_words(text))
    tries = []
    for attempt in range(first, first + RETAKES + 1):
        fields["seed"] = 1 + (vseed - 1 + attempt * RETAKE_SEED_STEP) % (2**31 - 2)
        pcm = server.take(fields, wav, f"line {line}", log)
        secs = len(pcm) / 2 / server.sample_rate
        tries.append((secs, fields["seed"], pcm))
        if low <= secs <= high:
            break
        log(f"   line {line}: a {secs:.1f} s take is outside {low:.1f} to {high:.1f} s for "
            f"{count_words(text)} words" + (", taking it again" if attempt < first + RETAKES else ""))
    secs, seed, pcm = min(tries, key=lambda t: miss(t[0], low, high))
    return {"pcm": pcm, "seed": seed, "tries": len(tries), "fits": low <= secs <= high}


def clause_pieces(text: str, limit: int = PIECE_WORDS) -> list[str]:
    """A long line as pieces that end at a sentence or a clause.

    text: the line as the voice reads it.
    limit: the words a piece may have.
    Returns the pieces in order. Each is a stretch of the text with single spaces, and together they hold all of it
    but the spaces between them. A sentence over the limit is split after a comma, a semicolon, a colon or a dash.
    A piece is over the limit when it has no such place to split, or when it took in a neighbour of under a fifth
    of the limit.
    """
    flat = " ".join(text.split())

    def split(start: int, end: int, pattern: re.Pattern) -> list[tuple[int, int]]:
        marks = [start] + [at for m in pattern.finditer(flat, start, end) for at in m.span()] + [end]
        return [(marks[k], marks[k + 1]) for k in range(0, len(marks), 2) if marks[k + 1] > marks[k]]

    def size(a: int, b: int) -> int:
        return count_words(flat[a:b])

    # Sentences, and the clauses of a sentence over the limit
    parts = []
    for a, b in split(0, len(flat), SENTENCE_END_RE):
        parts += split(a, b, CLAUSE_END_RE) if size(a, b) > limit else [(a, b)]

    # Fill each piece up to the limit
    pieces: list[tuple[int, int]] = []
    for a, b in parts:
        if pieces and size(pieces[-1][0], b) <= limit:
            pieces[-1] = (pieces[-1][0], b)
        else:
            pieces.append((a, b))

    # Give a piece that is too short to the shorter of its neighbours
    while len(pieces) > 1:
        i = next((i for i, p in enumerate(pieces) if size(*p) < limit // 5), None)
        if i is None:
            break
        before = i > 0 and (i == len(pieces) - 1 or size(*pieces[i - 1]) <= size(*pieces[i + 1]))
        a = i - 1 if before else i
        pieces[a: a + 2] = [(pieces[a][0], pieces[a + 1][1])]
    return [flat[a:b] for a, b in pieces]


def cpp_pieces(server, wav: bytes, transcript: str, pieces: list[str], direction: str, vseed: int, line: int, log,
               first: int = 0) -> dict:
    """Takes a line piece by piece with breeze-server and joins the pieces into one take.

    server, wav, transcript, direction, vseed, line, log, first: as cpp_take takes them.
    pieces: the line's text in pieces, from clause_pieces.
    Returns what cpp_take returns, for the whole line: the audio of the pieces with PIECE_GAP of silence between
    them, the seed of the first piece, the tries of all pieces, and fits when every piece fits its own length.
    """
    takes = [cpp_take(server, wav, transcript, piece, direction, vseed, line, log, first=first) for piece in pieces]
    gap = b"\x00\x00" * int(PIECE_GAP * server.sample_rate)
    return {"pcm": gap.join(t["pcm"] for t in takes), "seed": takes[0]["seed"], "tries": sum(t["tries"] for t in takes),
            "fits": all(t["fits"] for t in takes)}


def retake_line(server, wav: bytes, transcript: str, text: str, direction: str, vseed: int, line: int, log,
                round_: int) -> tuple[dict, int]:
    """Takes a flagged line again for one round of the check before the join.

    server, wav, transcript, text, direction, vseed, line, log: as cpp_take takes them.
    round_: the round, which picks seeds no earlier round used.
    Returns (take, pieces). A line of more than LONG_LINE words is taken in the pieces of clause_pieces, and pieces
    is their count. Any other line is one piece.
    """
    first = round_ * (RETAKES + 1)
    pieces = clause_pieces(text) if count_words(text) > LONG_LINE else [text]
    if len(pieces) > 1:
        log(f"   line {line}: taking its {count_words(text)} words again in {len(pieces)} pieces")
        return cpp_pieces(server, wav, transcript, pieces, direction, vseed, line, log, first=first), len(pieces)
    return cpp_take(server, wav, transcript, text, direction, vseed, line, log, first=first), 1


def take_fields(got: dict) -> dict:
    """What a saved take records about a cpp_take answer: its seed, a hash of its audio, takes_tried when it took
    more than one try, and out_of_range when its length does not fit."""
    out = {"seed": got["seed"], "audio": hashlib.sha1(got["pcm"]).hexdigest()[:16]}
    if got["tries"] > 1:
        out["takes_tried"] = got["tries"]
    if not got["fits"]:
        out["out_of_range"] = True
    return out


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def retake_ceiling(spoken: int) -> int:
    """How many times one render may take a flagged line again, over all its rounds, for a chapter of `spoken` takes."""
    return max(RETAKE_FLOOR, int(spoken * RETAKE_SHARE))


def retake_flagged(folder: Path, lines: list[dict], bible: dict, saved: dict, made: list[str], again, *, save,
                   comfy_url: str, log, rounds: int = CHECK_RETAKES, name_rounds: int = NAME_RETAKES) -> dict:
    """Checks a render's takes before the join and replaces the ones the check flags.

    folder: the chapter folder. Its take_check.json gets the entry of every take in made, and each voice's baseline.
    lines: the script lines.
    bible: the serial's bible.
    saved: the chapter's takes by line. The entry of a replaced take is updated in place, the entry of every line
        taken again records its last round as retake_rounds, and the entry of a take whose reasons the findings
        cover records them as retake_held.
    made: the lines, as keys of saved, to check: the ones this render made, the ones whose saved take an earlier
        check flagged, and the ones whose saved take doesn't have evidence of the current versions.
    again: again(line, round) takes a line again and returns its entry, with the audio in a file of its own.
    save: save() writes saved to disk. It is called after each replaced take and at the end of each round.
    comfy_url: the ComfyUI address the recognizer functions take.
    log: receives the progress lines.
    rounds: how many rounds a flagged line gets, over every render of its take, while its take is not flagged for
        a name.
    name_rounds: how many it gets while its take is flagged for a name.
    Returns {"replaced", "findings", "left"}: the lines whose take was replaced, the voice findings from
    takecheck.voice_findings, and the flagged lines that the render's ceiling left for a later render. A new take
    replaces the old one only when takecheck.better ranks it above. A line goes on from the round its retake_rounds
    records, so a line that has been through every round is heard and not taken again. A take is not taken again
    when a voice finding covers every reason it is flagged for. No finding covers the reason "name", so a take
    that adds a sound to a name is taken again however many takes of the name do. The render takes lines again at
    most retake_ceiling times: the lines with the most word errors first, and of lines with as many, one that is
    flagged for a name alone last. Does nothing unless this machine's recognizer is Parakeet, which leaves the
    card to breeze-server.
    Never raises. A failure is logged, and the takes replaced before it stay replaced, in saved and in
    take_check.json.
    """
    out: dict = {"replaced": [], "findings": [], "left": []}
    replaced: list[int] = out["replaced"]
    pending: dict[str, dict] = {}
    try:
        from . import takecheck
        engine = choose_asr()
        if engine != "parakeet" or not made:
            return out
        check = dict(engine=engine, comfy_url=comfy_url, log=log, folder=folder, saved=saved)
        record = dict(takes=saved, lines=lines, bible=bible)
        log(f"   checking the {len(made)} take{'' if len(made) == 1 else 's'} with Parakeet before the join...")
        results = takecheck.check_entries({k: saved[k] for k in made}, lines, bible, **check)
        takecheck.save_entries(folder, results, engine, **record)

        # Voice findings
        judged = takecheck.saved_entries(folder, saved)
        out["findings"] = takecheck.voice_findings(judged, saved)
        tripped = {(f["voice"], f["reason"]) for f in out["findings"]}
        held, changed = set(), False
        for k in made:
            reasons = sorted(results[k].get("reasons") or [])
            covered = (results[k]["verdict"] == "flagged" and bool(reasons)
                       and all((saved[k].get("voice"), r) in tripped for r in reasons))
            if covered:
                held.add(k)
            if saved[k].get("retake_held") != (reasons if covered else None):
                changed = True
                saved[k].pop("retake_held", None)
                if covered:
                    saved[k]["retake_held"] = reasons
        for f in out["findings"]:
            log(f"   voice finding: {f['reason']} flags {len(f['lines'])} of the {f['of']} takes of {f['voice']}, so "
                f"the cause is the voice and those takes are not taken again")
        if changed:
            save()

        # Retakes
        def spent(k: str) -> int:
            done = saved[k].get("retake_rounds")
            return done if isinstance(done, int) and not isinstance(done, bool) else 0

        def limit(k: str) -> int:
            reasons = results[k].get("reasons")
            return name_rounds if isinstance(reasons, list) and "name" in reasons else rounds

        budget, left = retake_ceiling(len(saved)), set()
        for _ in range(max(rounds, name_rounds)):
            bad = [k for k in made if results[k]["verdict"] == "flagged" and k not in held and spent(k) < limit(k)]
            if len(bad) > budget:
                worst = sorted(bad, key=lambda k: (-(results[k]["sub"] + results[k]["del"] + results[k]["ins"]),
                                                   results[k].get("reasons") == ["name"]))
                left.update(worst[budget:])
                bad = [k for k in bad if k in worst[:budget]]
            budget -= len(bad)
            if not bad:
                break
            log(f"   taking {len(bad)} of them again, because the check flagged them: {', '.join(bad)}")
            for k in bad:
                pending[k] = again(k, spent(k) + 1)
            verdicts = takecheck.check_entries(pending, lines, bible, **check)
            for k in bad:
                new = pending.pop(k)
                round_ = spent(k) + 1
                saved[k]["takes_tried"] = saved[k].get("takes_tried", 1) + new.get("takes_tried", 1)
                saved[k]["retake_rounds"] = round_
                if not takecheck.better(verdicts[k], results[k]):
                    Path(new["file"]).unlink(missing_ok=True)
                    continue
                os.replace(new["file"], saved[k]["file"])
                for field in ("out_of_range", "pieces"):
                    saved[k].pop(field, None)
                saved[k].update({f: new[f] for f in ("seed", "audio", "out_of_range", "pieces") if f in new},
                                retaken=round_)
                results[k] = verdicts[k]
                save()
                if int(k) not in replaced:
                    replaced.append(int(k))
            save()
            takecheck.save_entries(folder, results, engine, **record)
        still = [k for k in made if results[k]["verdict"] == "flagged" and k not in held]
        out["left"] = sorted(int(k) for k in left if k in still)
        if replaced:
            log(f"   kept a new take for: {', '.join(map(str, sorted(replaced)))}")
        if out["left"]:
            log(f"   the render reached its ceiling of {retake_ceiling(len(saved))} retakes, so a later render takes "
                f"these lines again: {', '.join(map(str, out['left']))}")
        still = [k for k in still if int(k) not in out["left"]]
        if still:
            log(f"   still flagged after every round of retakes: {', '.join(still)}")
    except Exception as e:
        for new in pending.values():
            Path(new["file"]).unlink(missing_ok=True)
        log(f"   could not check the new takes before the join, so the join uses them as they are: "
            f"{type(e).__name__}: {e}")
    replaced.sort()
    return out


def retakes_flagged_here(comfy_url: str) -> bool:
    """Whether retake_flagged can work on this machine: its recognizer is Parakeet, and Parakeet can run.

    comfy_url: the ComfyUI address the recognizer functions take.
    Never raises.
    """
    try:
        from . import takecheck
        engine = choose_asr()
        if engine != "parakeet":
            return False
        takecheck.check_ready(engine, comfy_url)
        return True
    except ComfyError:
        return False


def flagged_before(folder: Path, saved: dict, lines: list[dict] | None = None, bible: dict | None = None) -> set[str]:
    """The lines whose saved take the take check flagged and that a render would take again.

    folder: the chapter folder, which holds take_check.json.
    saved: the chapter's takes by line.
    lines, bible: the script lines and the serial's bible. With them, a file that takecheck.stale names, for older
        rules or other picked spellings, is judged again from its evidence first, without being written.
    Returns the lines as keys of saved. A verdict counts only for the take and the audio it was heard in. A line
    that has been through every retake round is left out, NAME_RETAKES for a take flagged for a name and
    CHECK_RETAKES for any other, and so is a take whose reasons a voice finding covered (retake_held). No hold
    covers the reason "name". A take_check.json that cannot be read or parsed counts as no verdicts.
    """
    from . import takecheck
    path = folder / takecheck.CHECK_FILE
    try:
        data = lib.load_json(path) if path.is_file() else {}
        checked = data.get("takes") or {}
        if lines is not None and bible is not None and takecheck.stale(data, bible):
            try:
                checked = takecheck.rejudged(checked, saved, lines, bible)
            except Exception:
                pass
        verdicts = list(checked.items())
    except (OSError, ValueError, AttributeError, RecursionError):
        return set()
    lines = set()
    for k, verdict in verdicts:
        take = saved.get(k)
        if not isinstance(verdict, dict) or not isinstance(take, dict) or verdict.get("verdict") != "flagged":
            continue
        rounds = take.get("retake_rounds")
        held, reasons = take.get("retake_held"), verdict.get("reasons")
        named = isinstance(reasons, list) and "name" in reasons
        spent = isinstance(rounds, int) and rounds >= (NAME_RETAKES if named else CHECK_RETAKES)
        covered = (isinstance(held, list) and isinstance(reasons, list) and bool(reasons)
                   and all(isinstance(r, str) and r != "name" and r in held for r in reasons))
        if (verdict.get("key") == take.get("key") and verdict.get("audio") == take.get("audio") and not spent
                and not covered):
            lines.add(k)
    return lines


def unheard_before(folder: Path, saved: dict) -> set[str]:
    """The lines whose saved take doesn't have evidence from Parakeet, in the current versions, in take_check.json.

    folder: the chapter folder.
    saved: the chapter's takes by line.
    Returns the lines as keys of saved. The evidence counts the blocks this machine has the models for. Never raises.
    """
    from . import takecheck
    from .asr import extra_blocks
    entries = takecheck.saved_entries(folder, saved)
    extras = extra_blocks()
    return {k for k, take in saved.items()
            if isinstance(take, dict) and not takecheck.reusable(entries.get(k) or {}, take, "parakeet", extras)}


def wait_comfy_idle(comfy_url: str, log, timeout: float = COMFY_IDLE_TIMEOUT) -> None:
    """Returns once ComfyUI has no running or queued prompt, or at once when it does not answer.

    comfy_url: the ComfyUI to watch.
    log: receives one line when the wait starts, and one before it when a breeze-server that a worker keeps up
        is stopped, so ComfyUI has the card.
    timeout: seconds to wait. Raises ComfyError when ComfyUI is still busy after it.
    """
    client, deadline, told = ComfyClient(comfy_url), time.time() + timeout, False
    while True:
        if not machine.listening(comfy_url):
            return
        try:
            queue = client._get("/queue", timeout=10)
        except Exception:
            return
        if not (queue.get("queue_running") or queue.get("queue_pending")):
            return
        if time.time() > deadline:
            raise ComfyError(f"ComfyUI stayed busy for {timeout / 60:.0f} min; breeze-server would share the card with it")
        if not told:
            found = kept.recorded(KEPT_NAME)
            if found:
                kept.forget(KEPT_NAME)
                kept.end(found["pid"])
                log("   stopped the breeze-server that was left up, so ComfyUI has the card")
            log("   waiting for ComfyUI to finish its prompts before starting breeze-server")
            told = True
        time.sleep(5)


def free_comfy(comfy_url: str) -> None:
    """Asks ComfyUI to unload its models so breeze-server has the card. Never raises."""
    try:
        if machine.listening(comfy_url):
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


def take_model(runner: str) -> str:
    """The model name that a runner's takes record and that their keys include."""
    return MODEL if runner == "comfyui" else f"cpp:{CPP_MODEL.name}"


def render_seed(last: dict | None, seed: int | None = None) -> int:
    """The base seed a Breeze render uses.

    last: the chapter's render record, or None.
    seed: the seed the request names, or None.
    Returns seed when the request names one, else the seed of the last render when that was Breeze, else 1.
    """
    if seed is not None:
        return seed
    kept = last.get("seed") if isinstance(last, dict) and last.get("engine") == "breeze" else None
    return kept if isinstance(kept, int) and not isinstance(kept, bool) else 1


def plan_takes(lines: list[dict], speaker_slot: dict, slot_voice: dict, bible: dict, seed: int,
               ref_sha1: dict[str, str], runner: str) -> list[tuple]:
    """The take each script line needs, in script order, as (line, voice, text, direction, voice seed, key).

    lines: the script lines.
    speaker_slot: speaker -> slot, from plan_slots.
    slot_voice: slot -> voice id, from plan_slots.
    bible: the serial's bible, whose say-as overrides voice_text applies.
    seed: the render's base seed.
    ref_sha1: voice id -> the sha1 of its cast clip.
    runner: "cpp" or "comfyui".
    Returns one entry per line. A saved take with the same key can stand in for a new one.
    """
    model, controls = take_model(runner), CONTROLS if runner == "comfyui" else CPP_FIELDS
    plan = []
    for i, line in enumerate(lines):
        voice = slot_voice[speaker_slot[line["speaker"]]]
        text = voice_text(line, bible)[0]
        direction = " ".join((line.get("direction") or "").split())
        vseed = voice_seed(seed, voice)
        parts = dict(text=text, voice=voice, ref=ref_sha1[voice], direction=direction, seed=vseed,
                     model=model, controls=controls, clone_cfg=CLONE_CFG, direction_cfg=DIRECTION_CFG)
        if runner != "comfyui":
            parts["runner"] = runner
        plan.append((i, voice, text, direction, vseed, take_key(**parts)))
    return plan


def render_changes(slug: str, number: int, seed: int | None = None, runner: str | None = None, *,
                   stored: bool = False) -> dict:
    """What a render of a chapter would change.

    slug: the serial.
    number: the chapter.
    seed: the render's base seed. None picks it the way a render does, with render_seed.
    runner: "cpp" or "comfyui". None picks it the way a render does.
    stored: count the flagged takes from the verdicts as take_check.json holds them. A render judges verdicts from
        older rules again first, and so does this function when stored is False.
    Returns {"spoken", "make", "retake", "unheard", "join", "engine"}. spoken counts the lines the chapter speaks.
    make counts the lines whose saved take is missing or has another key. retake counts the saved takes a render
    would take again because the take check flagged them, and unheard the saved takes it would hear for their
    evidence, both under cpp only. join says whether a render would write audio the chapter doesn't have: it is
    False only when the chapter's audio comes from a Breeze join of JOIN_VERSION that made the cuts a join would
    make now. A render record from before renders recorded their cuts counts as having made every cut but the ones
    that end a take. engine is the engine of the
    chapter's last render, "breeze" or "drama3", or None for a chapter that was never rendered. Reads the library, and tries the take check's models once in a process on a machine that has them, so
    unheard counts the blocks this machine gathers. A take counts as saved when its key matches, even if its
    file is gone from the node that made it. Raises when the chapter cannot be planned, for example when it has no
    script.
    """
    serial = lib.load_serial(slug)
    meta, text = lib.load_chapter(slug, number)
    folder = lib.chapter_dir(slug, number)
    bible = lib.load_bible(slug)
    lines = screenplay.emphasized(lib.load_json(folder / "script.json"), text, bible)[0]
    speaker_slot, slot_voice, _ = plan_slots(serial, bible, lines)
    last = meta.get("render") if isinstance(meta.get("render"), dict) else {}
    runner = choose_runner(serial, last, runner=runner)
    seed = render_seed(last, seed)
    vdir = lib.serial_dir(slug) / "voices"
    ref_sha1 = {v: hashlib.sha1((vdir / (lib.load_yaml(vdir / f"{v}.yaml") or {})["clip"]).read_bytes()).hexdigest()
                for v in set(slot_voice.values())}
    plan = plan_takes(lines, speaker_slot, slot_voice, bible, seed, ref_sha1, runner)
    takes_path = folder / TAKES_FILE
    saved = (lib.load_json(takes_path).get("takes") or {}) if takes_path.exists() else {}
    spoken = [p for p in plan if re.search(r"\w", p[2])]
    kept = {str(p[0]) for p in spoken if (saved.get(str(p[0])) or {}).get("key") == p[5]}
    flagged = flagged_before(folder, saved) if stored else flagged_before(folder, saved, lines, bible)
    redo = flagged & kept if runner == "cpp" else set()
    unheard = (unheard_before(folder, saved) & kept) - redo if runner == "cpp" else set()
    rendered = last.get("engine") == "breeze" and bool(last.get("audio"))
    joined = last.get("join") if isinstance(last.get("join"), int) else 1
    cut = False
    if rendered and joined >= JOIN_VERSION:
        from . import takecheck
        cuts = takecheck.join_cuts(folder, saved, lines, bible, stored=True)
        if isinstance(last.get("cuts"), str):
            cut = last["cuts"] != cuts_digest(cuts)
        else:
            entries = takecheck.saved_entries(folder, saved)
            cut = any(takecheck.ends_a_take(c, entries.get(k) or {}) for k, found in cuts.items() for c in found)
    return {"spoken": len(spoken), "make": len(spoken) - len(kept), "retake": len(redo), "unheard": len(unheard),
            "join": not rendered or joined < JOIN_VERSION or cut,
            "engine": last.get("engine") or ("drama3" if last.get("codes_file") else None)}


def cuts_digest(cuts: dict) -> str:
    """A short hash of the cuts a join makes, {line: [[start, end], ...]}, which the render record keeps."""
    return hashlib.sha1(json.dumps({k: cuts[k] for k in sorted(cuts, key=int)}).encode("utf-8")).hexdigest()[:12]


def reusable_takes(slug: str, number: int, seed: int | None = None, runner: str | None = None) -> tuple[int, int]:
    """How many of a chapter's spoken lines a render could take from its saved takes, and how many it speaks.

    slug, number, seed, runner: as render_changes uses them.
    Returns (reusable, spoken). A take that render_changes counts under make or retake does not count, with the
    flagged takes read from the stored verdicts. Raises when the chapter cannot be planned.
    """
    changes = render_changes(slug, number, seed, runner, stored=True)
    return changes["spoken"] - changes["make"] - changes["retake"], changes["spoken"]


def render_breeze(slug: str, number: int, *, seed: int | None = None, comfy_url: str = "http://127.0.0.1:8188",
                  log=print, join_only: bool = False, runner: str | None = None) -> dict:
    """Renders a chapter with Breeze TTS 2 to audio/chNNNN.opus and marks it rendered.

    slug: the serial.
    number: the chapter.
    seed: the base for each voice's Breeze seed. None keeps the seed of the chapter's last Breeze render, else 1.
        A join-only run uses the seed the chapter was rendered with.
    comfy_url: the ComfyUI that the comfyui runner queues on, and that the cpp runner asks to free the card.
    log: receives the progress lines.
    join_only: join the saved takes again without generating any.
    runner: "cpp" or "comfyui". None uses the chapter's last runner on a join-only run, else serial.yaml
        breeze_runner, else cpp.
    Returns the chapter's render record. Under breeze-server with Parakeet, a new take that the transcript check
    flags is taken again before the join (retake_flagged). A saved take that an earlier check flagged goes through
    the same check and rounds, and stays unless a new take is closer to the line. A join-only run takes nothing
    again. New takes are then checked against the script with this machine's speech
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
    # a join-only run keeps the script as its takes were made from it
    lines = lib.load_json(script_path)["lines"] if join_only else screenplay.write_emphasis(slug, number, log)
    bible = lib.load_bible(slug)
    speaker_slot, slot_voice, notes = plan_slots(serial, bible, lines)
    effects = {slot_voice[s]: spec for s, spec in slot_effects(serial, slot_voice).items()}
    last = meta.get("render") if isinstance(meta.get("render"), dict) else {}
    last_breeze = last.get("engine") == "breeze"
    if join_only and last_breeze:
        seed = last.get("seed", seed)
    seed = render_seed(last, seed)
    runner = choose_runner(serial, last, join_only=join_only, runner=runner)
    stem = f"ch{number:04d}"
    for n in notes:
        log(f"   {n}")
    log(f"render: {slug} chapter {number}: {len(lines)} lines, {len(slot_voice)} voices "
        f"{ {s: v for s, v in slot_voice.items()} } with Breeze TTS 2 via {runner}")

    takes_path = folder / TAKES_FILE
    saved = load_takes(takes_path, log)
    refs = stage_references(slug, set(slot_voice.values()), runner)
    model = take_model(runner)

    # Plan
    plan = plan_takes(lines, speaker_slot, slot_voice, bible, seed, {v: r["sha1"] for v, r in refs.items()}, runner)
    spoken = [p for p in plan if re.search(r"\w", p[2])]

    def usable(p) -> bool:
        entry = saved.get(str(p[0])) or {}
        if entry.get("key") != p[5] or not Path(entry.get("file", "")).exists():
            return False
        if runner != "cpp" or join_only or entry.get("out_of_range"):
            return True
        secs = wav_seconds(Path(entry["file"]))
        low, high = take_bounds(count_words(p[2]))
        pieces = entry.get("pieces") if isinstance(entry.get("pieces"), int) else 1
        # each piece after the first has its own allowance in take_bounds, and a gap before it
        high += (pieces - 1) * (take_bounds(0)[1] + PIECE_GAP)
        return secs is not None and low <= secs <= high

    stale = [p for p in spoken if not usable(p)]
    if join_only and stale:
        raise ComfyError(f"chapter {number} is missing {len(stale)} takes (first: line {stale[0][0]}); run a full render")
    flagged = flagged_before(folder, saved, lines, bible) if runner == "cpp" and not join_only else set()
    unheard = unheard_before(folder, saved) if runner == "cpp" and not join_only else set()
    made_now = {p[0] for p in stale}
    retry = [p for p in spoken if (str(p[0]) in flagged or str(p[0]) in unheard) and p[0] not in made_now]
    if retry and not retakes_flagged_here(comfy_url):
        retry = []
    again_lines = [str(p[0]) for p in retry if str(p[0]) in flagged]
    if again_lines:
        log(f"   an earlier check flagged the saved takes of lines {', '.join(again_lines)}, so they are "
            f"checked with the new takes and taken again")
    if len(retry) > len(again_lines):
        log(f"   {len(retry) - len(again_lines)} saved takes don't have evidence from the current check, so they "
            f"are heard before the join")
    findings = last.get("voice_findings") if isinstance(last.get("voice_findings"), list) else []

    # Takes
    generated_seconds = 0.0
    if stale or retry:
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
            if stale:
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
                    got = cpp_take(server, wavs[voice], refs[voice]["transcript"], text, direction, vseed, i, log)
                    file = take_dir / f"l{i:04d}.wav"
                    write_wav(file, got["pcm"], server.sample_rate)
                saved[str(i)] = {"key": key, "file": str(file), "voice": voice, "direction": direction,
                                 "seconds": round(time.time() - t0, 2)}
                if server is not None:
                    saved[str(i)].update(take_fields(got))
                    if not got["fits"]:
                        secs = len(got["pcm"]) / 2 / server.sample_rate
                        log(f"   warning: line {i} kept a {secs:.1f} s take after {got['tries']} tries; listen to it")
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

            # Retakes
            if server is not None:
                by_line = {str(p[0]): p for p in stale + retry}

                def again(k: str, round_: int) -> dict:
                    i, voice, text, direction, vseed, key = by_line[k]
                    got, pieces = retake_line(server, wavs[voice], refs[voice]["transcript"], text, direction, vseed,
                                              i, log, round_)
                    file = Path(saved[k]["file"]).with_name(f"l{i:04d}.retake.wav")
                    write_wav(file, got["pcm"], server.sample_rate)
                    return {"key": key, "file": str(file), **take_fields(got), **({"pieces": pieces} if pieces > 1 else {})}

                checked = retake_flagged(folder, lines, bible, saved, list(by_line), again, comfy_url=comfy_url,
                                         log=log, save=lambda: save_takes(takes_path, saved, runner))
                findings = checked["findings"]
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
    from . import takecheck
    cuts = takecheck.join_cuts(folder, saved, lines, bible)
    after = {p[0]: lines[q[0]] for p, q in zip(spoken, spoken[1:])}
    job = {"output": str(src), "cap_seconds": MAX_TAKE_SECONDS,
           "takes": [{"line": i, "file": saved[str(i)]["file"], "voice": voice, "effect": effects.get(voice),
                      "offset": LEVEL_OFFSETS[direction_class(lines[i].get("direction"))],
                      "cuts": cuts.get(str(i), []), "gap": gap_after(lines[i], after.get(i))}
                     for i, voice, *_ in spoken]}
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
    log(f"   joined {summary['takes']} takes into {summary['seconds']} s at {summary['sample_rate']} Hz, "
        f"{summary.get('clicks', 0)} clicks filled in, {summary.get('cuts', 0)} pauses and tails cut by "
        f"{summary.get('cut_seconds', 0)} s")
    log(f"   the chapter is at {summary.get('loudness')} LUFS with a true peak of {summary.get('true_peak')} dB, "
        f"compressed by {summary.get('compressed', 0)} dB at most and limited by {summary.get('limited', 0)} dB")
    if summary.get("capped"):
        log(f"   warning: lines {summary['capped']} reached the {MAX_TAKE_SECONDS} s take limit and may be cut off")
    if summary.get("voice_gains"):
        log("   voice gains in dB: " + ", ".join(f"{v} {g:+.1f}" for v, g in summary["voice_gains"].items()))

    # Record
    meta["render"] = {"engine": "breeze", "runner": runner, "model": model, "takes_file": TAKES_FILE,
                      "comfy_audio": str(src), "audio": f"audio/{dst.name}", "seed": seed,
                      "slots": {str(s): v for s, v in slot_voice.items()}, "notes": notes,
                      "generated_takes": len(stale), "generate_seconds": round(generated_seconds),
                      "clicks_filled": summary.get("clicks", 0), "join": JOIN_VERSION, "cuts": cuts_digest(cuts),
                      "pauses_cut": summary.get("cuts", 0), "loudness": summary.get("loudness"),
                      "true_peak": summary.get("true_peak"), "at": lib.now_iso()}
    if summary.get("capped"):
        meta["render"]["capped_lines"] = summary["capped"]
    if effects:
        meta["render"]["effects"] = effects
        meta["render"]["fx"] = {"treated_lines": summary["treated_lines"], "raw_seconds": summary.get("raw_seconds"),
                                "seconds": summary["seconds"]}
    if findings:
        meta["render"]["voice_findings"] = findings
    if not (stale or retry) and isinstance(last.get("check"), dict):
        meta["render"]["check"] = last["check"]
    meta["status"] = "rendered"
    lib.save_chapter_meta(slug, number, meta)

    # Check
    if stale or retry or "check" not in meta["render"]:
        from .takecheck import check_after_render
        meta["render"]["check"] = check_after_render(slug, number, comfy_url=comfy_url, log=log)
    log(f"   audio: {dst}")
    return meta["render"]
