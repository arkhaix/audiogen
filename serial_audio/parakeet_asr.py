"""Hears audio files with NVIDIA Parakeet TDT 0.6B v2 through sherpa-onnx, on the CPU, and measures their signal.

    python -I parakeet_asr.py < job.json

The job on stdin is {"model": <folder>, "files": [<path>, ...], "threads": <n>, "blocks": [<block>, ...], "vad":
<folder>, "speaker": <folder>, "asr2": <folder>, "phone": <folder>}. blocks names the evidence to gather for every file, from BLOCKS.
Without it the child gathers ["asr", "signal"], and with an empty list it gathers nothing. A job with "check": true
hears no file: it loads the models of the blocks other than asr and signal and gives one line, {"loaded": [<block>,
...], "errors": {<block>: "..."}}. The Parakeet folder holds
the int8 export from the k2-fsa asr-models release: encoder.int8.onnx, decoder.int8.onnx, joiner.int8.onnx and
tokens.txt. vad, speaker, asr2 and phone name the folders that
hold VAD_FILES, SPEAKER_FILES, ASR2_FILES and PHONE_FILES. A block whose folder the job doesn't name, or whose model doesn't
load, is left out.

Each file gives one JSON line on stdout, in order: {"index": i, ...} with "text", "tokens", "starts", "durations" and
"logprobs" for asr, and a block under its own name for each of the others, or {"index": i, "error": "..."}. tokens
are the recognizer's raw tokens, and starts and durations are in seconds. signal is take_measure.measure of the file
at its own sample rate. vad is {"v", "engine", "segments": [[start, end], ...]}, the speech Silero VAD v5 finds, in
seconds. speaker is {"v", "engine", "scale", "q"}: TitaNet small's embedding of the file at length 1, as int8 values
in base64 that scale turns back into the embedding. asr2 is {"v", "engine", "heard", "tokens"}, the transcript of
Moonshine base English. phone is {"v", "engine", "phones", "starts"}: the IPA that ZIPA small decodes with no text
to guide it, one character per symbol, and the second each symbol starts at. A block that fails for a file is {"v": n, "error": "..."}. A last line {"done": n, "seconds":
s} follows. Runs under the audio Python (machine.AUDIO_PYTHON) with sherpa-onnx, numpy, scipy and soundfile. The top
level imports the standard library only, and main imports take_measure from this folder.

    python -I parakeet_asr.py --serve

stays up and keeps each model it loads, so the jobs after the first one that needs a model don't load it again. Its
stdin gives one line, {"token": <text>, "stay": <bool>}, and its stdout gives one line, {"port": <n>, "pid": <n>},
once its packages are imported and it listens on that port of 127.0.0.1. A client connects, sends one job as one
line with the token under "token", and reads the lines above until the connection closes. A job that cannot run
gives one line, {"fatal": "..."}. A check job loads its models with the job's threads, so the jobs after it use
them. Without stay it ends when its stdin ends, which is when the process that started it ends. With stay it ends
after SERVE_IDLE seconds without a connection, or when it is stopped.
"""
from __future__ import annotations

import base64
import json
import math
import os
import socket
import sys
import threading
import time
from pathlib import Path

RATE = 16000
SERVE_FLAG = "--serve"
SERVE_IDLE = 1800.0
SERVE_READ = 30.0
MODEL_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
VAD_FILES = ("silero_vad_v5.onnx",)
SPEAKER_FILES = ("nemo_en_titanet_small.onnx",)
ASR2_FILES = ("encoder_model.ort", "decoder_model_merged.ort", "tokens.txt")
PHONE_FILES = ("model.int8.onnx", "tokens.txt")
VAD_V = 1
SPEAKER_V = 1
ASR2_V = 1
PHONE_V = 1
ENGINES = {"vad": "silero-vad-v5", "speaker": "titanet-small", "asr2": "moonshine-base-en",
           "phone": "zipa-small-crctc-300k"}
BLOCKS = ("asr", "signal", "vad", "speaker", "asr2", "phone")
# sherpa-onnx times a Zipformer CTC token at 40 ms a frame, and ZIPA gives a frame every 20 ms
PHONE_TIME_SCALE = 0.5
VAD_THRESHOLD = 0.5
VAD_MIN_SILENCE = 0.1
VAD_MIN_SPEECH = 0.05
VAD_WINDOW = 512
# so the detector closes a segment that runs to the end of the file
VAD_PAD = 0.5


def emit(record: dict) -> None:
    # ASCII only: under -I a piped stdout on Windows is cp1252, and the parent reads UTF-8.
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def load(path: str):
    """The file as mono float32 samples, and its sample rate."""
    import soundfile as sf
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    return x.mean(axis=1), int(sr)


def at_rate(x, sr: int):
    """Mono x at RATE, the rate the recognizer takes."""
    import numpy as np
    from scipy.signal import resample_poly
    if sr == RATE:
        return x
    g = math.gcd(sr, RATE)
    return resample_poly(x, RATE // g, sr // g).astype(np.float32)


def recognizer(model: Path, threads: int):
    import sherpa_onnx
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(model / MODEL_FILES[0]), decoder=str(model / MODEL_FILES[1]), joiner=str(model / MODEL_FILES[2]),
        tokens=str(model / MODEL_FILES[3]), num_threads=threads, model_type="nemo_transducer",
        decoding_method="greedy_search")


def heard(result) -> dict:
    """The transcript and the raw tokens of a sherpa-onnx result, with one start, duration and log-probability per
    token where the recognizer gives them."""
    out = {"text": result.text.strip(), "tokens": [str(t) for t in result.tokens]}
    for name, attr, places in (("starts", "timestamps", 2), ("durations", "durations", 2), ("logprobs", "ys_log_probs", 2)):
        values = list(getattr(result, attr, None) or [])
        if len(values) == len(out["tokens"]):
            out[name] = [round(float(v), places) for v in values]
    return out


def vad_config(folder: Path):
    import sherpa_onnx
    return sherpa_onnx.VadModelConfig(
        silero_vad=sherpa_onnx.SileroVadModelConfig(model=str(folder / VAD_FILES[0]), threshold=VAD_THRESHOLD,
                                                    min_silence_duration=VAD_MIN_SILENCE,
                                                    min_speech_duration=VAD_MIN_SPEECH, window_size=VAD_WINDOW),
        sample_rate=RATE, num_threads=1)


def speech_segments(config, x16) -> list[list[float]]:
    """The speech Silero VAD finds in a file.

    config: the detector's sherpa-onnx config, from vad_config.
    x16: the file's mono samples at RATE.
    Returns [[start, end], ...] in seconds, in order.
    """
    import numpy as np
    import sherpa_onnx
    vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=len(x16) / RATE + 2 * VAD_PAD + 1)
    padded = np.concatenate([x16, np.zeros(int(VAD_PAD * RATE), dtype=np.float32)])
    for at in range(0, len(padded) - VAD_WINDOW + 1, VAD_WINDOW):
        vad.accept_waveform(padded[at: at + VAD_WINDOW])
    vad.flush()
    out = []
    while not vad.empty():
        segment = vad.front
        end = min(len(x16), segment.start + len(segment.samples))
        out.append([round(segment.start / RATE, 2), round(end / RATE, 2)])
        vad.pop()
    return out


def packed(embedding) -> dict:
    """{"scale", "q"}: embedding at length 1 as int8 values in base64, which scale multiplies back."""
    import numpy as np
    e = np.asarray(embedding, dtype=np.float64)
    e = e / (np.linalg.norm(e) or 1.0)
    top = float(np.abs(e).max()) or 1.0
    q = np.clip(np.round(e / top * 127), -127, 127).astype(np.int8)
    return {"scale": round(top / 127, 8), "q": base64.b64encode(q.tobytes()).decode("ascii")}


def models(job: dict, blocks: set[str], threads: int, errors: dict | None = None, cache: dict | None = None) -> dict:
    """The model of each block in blocks that the job names a folder for, by block.

    job: the job from stdin.
    blocks: the blocks to gather.
    threads: the CPU threads each model may use.
    errors: receives {block: error} for each block that is left out.
    cache: the models loaded before, by block, folder and threads. A model in it is not loaded again, and each
        model loaded now is added.
    Returns {block: model}. A block other than asr whose folder lacks a file or whose model doesn't load is left
    out, with a line on stderr, so the other blocks are still gathered. Raises when Parakeet doesn't load.
    """
    import sherpa_onnx

    def kept(block: str, folder: Path, load_model):
        key = (block, str(folder), threads)
        if cache is not None and key in cache:
            return cache[key]
        model = load_model(folder)
        if cache is not None:
            cache[key] = model
        return model

    def speaker(folder: Path):
        return sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(folder / SPEAKER_FILES[0]), num_threads=threads))

    def vad(folder: Path):
        config = vad_config(folder)
        # the detector reads its model file only when one is built
        sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=1)
        return config

    def moonshine(folder: Path):
        return sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
            encoder=str(folder / ASR2_FILES[0]), decoder=str(folder / ASR2_FILES[1]),
            tokens=str(folder / ASR2_FILES[2]), num_threads=threads)

    def zipa(folder: Path):
        return sherpa_onnx.OfflineRecognizer.from_zipformer_ctc(
            model=str(folder / PHONE_FILES[0]), tokens=str(folder / PHONE_FILES[1]), num_threads=threads)

    out = {}
    if "asr" in blocks:
        out["asr"] = kept("asr", Path(job["model"]), lambda folder: recognizer(folder, threads))
    for block, files, load_model in (("vad", VAD_FILES, vad), ("speaker", SPEAKER_FILES, speaker),
                                     ("asr2", ASR2_FILES, moonshine), ("phone", PHONE_FILES, zipa)):
        if block not in blocks or not job.get(block):
            continue
        folder = Path(job[block])
        lacking = [name for name in files if not (folder / name).is_file()]
        try:
            if lacking:
                raise FileNotFoundError(f"{folder} lacks {', '.join(lacking)}")
            out[block] = kept(block, folder, load_model)
        except Exception as e:
            print(f"the {block} block is left out: {type(e).__name__}: {e}", file=sys.stderr)
            if errors is not None:
                errors[block] = f"{type(e).__name__}: {e}"
    return out


def phones(result) -> dict:
    """{"phones", "starts"} of a ZIPA result: its symbols as one string, and the second each one starts at.

    A token that is not one visible character is left out, with its time.
    """
    kept = [(str(token), float(at) * PHONE_TIME_SCALE) for token, at in zip(result.tokens, result.timestamps)
            if len(str(token)) == 1 and not str(token).isspace()]
    return {"phones": "".join(token for token, _ in kept), "starts": [round(at, 2) for _, at in kept]}


def gather(found: dict, blocks: set[str], x, sr: int, tm) -> dict:
    """The record of one file's evidence, without its index.

    found: the models by block, from models().
    blocks: the blocks to gather.
    x, sr: the file's mono samples and sample rate.
    tm: the take_measure module.
    A block other than asr that fails is {"v": n, "error": "..."}.
    """
    record, x16 = {}, None

    def failed(version: int, e: Exception) -> dict:
        return {"v": version, "error": f"{type(e).__name__}: {e}"}

    if "signal" in blocks:
        try:
            record["signal"] = tm.measure(x, sr)
        except Exception as e:
            record["signal"] = failed(tm.SIGNAL_V, e)
    if found.keys() & {"asr", "vad", "speaker", "asr2", "phone"}:
        x16 = at_rate(x, sr)
    if "asr" in found:
        stream = found["asr"].create_stream()
        stream.accept_waveform(RATE, x16)
        found["asr"].decode_stream(stream)
        record.update(heard(stream.result))
    if "vad" in found:
        try:
            record["vad"] = {"v": VAD_V, "engine": ENGINES["vad"], "segments": speech_segments(found["vad"], x16)}
        except Exception as e:
            record["vad"] = failed(VAD_V, e)
    if "speaker" in found:
        try:
            stream = found["speaker"].create_stream()
            stream.accept_waveform(RATE, x16)
            stream.input_finished()
            record["speaker"] = {"v": SPEAKER_V, "engine": ENGINES["speaker"],
                                 **packed(found["speaker"].compute(stream))}
        except Exception as e:
            record["speaker"] = failed(SPEAKER_V, e)
    if "asr2" in found:
        try:
            stream = found["asr2"].create_stream()
            stream.accept_waveform(RATE, x16)
            found["asr2"].decode_stream(stream)
            record["asr2"] = {"v": ASR2_V, "engine": ENGINES["asr2"], "heard": stream.result.text.strip(),
                              "tokens": [str(t) for t in stream.result.tokens]}
        except Exception as e:
            record["asr2"] = failed(ASR2_V, e)
    if "phone" in found:
        try:
            stream = found["phone"].create_stream()
            stream.accept_waveform(RATE, x16)
            found["phone"].decode_stream(stream)
            record["phone"] = {"v": PHONE_V, "engine": ENGINES["phone"], **phones(stream.result)}
        except Exception as e:
            record["phone"] = failed(PHONE_V, e)
    return record


def run(job: dict, emit, cache: dict | None = None) -> int:
    """Does one job.

    job: the job, as the module's docstring describes it.
    emit: emit(record) gives the client one line.
    cache: the models a serving process keeps between jobs, as models() takes them, or None for a process that
        does one job.
    Returns 0, or 2 when the Parakeet folder lacks a file, which a serving process tells the client as "fatal".
    Raises when Parakeet doesn't load or emit fails.
    """
    # Setup
    blocks = set(job["blocks"] if isinstance(job.get("blocks"), list) else ("asr", "signal")) & set(BLOCKS)
    threads = int(job.get("threads") or 4)
    if job.get("check"):
        errors: dict = {}
        loaded = models(job, blocks - {"asr", "signal"}, 1 if cache is None else threads, errors, cache)
        emit({"loaded": sorted(loaded), "errors": errors})
        return 0
    lacking = [name for name in MODEL_FILES if "asr" in blocks and not (Path(job["model"]) / name).is_file()]
    if lacking:
        problem = f"the Parakeet model folder {job['model']} lacks {', '.join(lacking)}"
        if cache is None:
            print(problem, file=sys.stderr)
        else:
            emit({"fatal": problem})
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import take_measure
    start = time.time()
    found = models(job, blocks, threads, None, cache)

    # Hear and measure
    files = job.get("files") or []
    for i, path in enumerate(files):
        try:
            x, sr = load(path)
            record = {"index": i, **gather(found, blocks, x, sr, take_measure)}
        except Exception as e:
            record = {"index": i, "error": f"{type(e).__name__}: {e}"}
        emit(record)
    emit({"done": len(files), "seconds": round(time.time() - start, 2)})
    return 0


def answer(conn: socket.socket, token: str, cache: dict) -> None:
    """Does the job one client sends over conn. Never raises: a job that cannot run gives the client "fatal"."""
    def send(record: dict) -> None:
        conn.sendall((json.dumps(record) + "\n").encode("ascii"))

    try:
        conn.settimeout(SERVE_READ)
        with conn.makefile("rb") as reader:
            job = json.loads(reader.readline().decode("utf-8"))
        conn.settimeout(None)
        if not isinstance(job, dict) or job.get("token") != token:
            send({"fatal": "the job does not have this process's token"})
            return
        run(job, send, cache)
    except OSError:
        # the client is gone
        pass
    except Exception as e:
        try:
            send({"fatal": f"{type(e).__name__}: {e}"})
        except OSError:
            pass


def serve() -> int:
    """Does the jobs that clients send to a port of this machine, one at a time, and keeps the models between them."""
    # Setup
    config = json.loads(sys.stdin.readline() or "{}")
    token, stay = str(config.get("token") or ""), bool(config.get("stay"))
    # a missing package ends the process here, before it names a port
    import numpy  # noqa: F401
    import scipy.signal  # noqa: F401
    import sherpa_onnx  # noqa: F401
    import soundfile  # noqa: F401
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import take_measure  # noqa: F401
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    emit({"port": listener.getsockname()[1], "pid": os.getpid()})
    if stay:
        listener.settimeout(SERVE_IDLE)
    else:
        def ends_with_parent() -> None:
            try:
                sys.stdin.buffer.read()
            finally:
                os._exit(0)

        threading.Thread(target=ends_with_parent, name="parent-watch", daemon=True).start()

    # Serve
    cache: dict = {}
    while True:
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            return 0
        with conn:
            answer(conn, token, cache)


def main() -> int:
    if SERVE_FLAG in sys.argv[1:]:
        return serve()
    return run(json.loads(sys.stdin.read()), emit)


if __name__ == "__main__":
    sys.exit(main())
