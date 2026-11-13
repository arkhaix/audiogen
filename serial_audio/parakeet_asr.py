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
"""
from __future__ import annotations

import base64
import json
import math
import sys
import time
from pathlib import Path

RATE = 16000
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


def models(job: dict, blocks: set[str], threads: int, errors: dict | None = None) -> dict:
    """The model of each block in blocks that the job names a folder for, by block.

    job: the job from stdin.
    blocks: the blocks to gather.
    threads: the CPU threads each model may use.
    errors: receives {block: error} for each block that is left out.
    Returns {block: model}. A block other than asr whose folder lacks a file or whose model doesn't load is left
    out, with a line on stderr, so the other blocks are still gathered. Raises when Parakeet doesn't load.
    """
    import sherpa_onnx

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
        out["asr"] = recognizer(Path(job["model"]), threads)
    for block, files, load_model in (("vad", VAD_FILES, vad), ("speaker", SPEAKER_FILES, speaker),
                                     ("asr2", ASR2_FILES, moonshine), ("phone", PHONE_FILES, zipa)):
        if block not in blocks or not job.get(block):
            continue
        folder = Path(job[block])
        lacking = [name for name in files if not (folder / name).is_file()]
        try:
            if lacking:
                raise FileNotFoundError(f"{folder} lacks {', '.join(lacking)}")
            out[block] = load_model(folder)
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


def main() -> int:
    # Setup
    job = json.loads(sys.stdin.read())
    blocks = set(job["blocks"] if isinstance(job.get("blocks"), list) else ("asr", "signal")) & set(BLOCKS)
    if job.get("check"):
        errors: dict = {}
        loaded = models(job, blocks - {"asr", "signal"}, 1, errors)
        emit({"loaded": sorted(loaded), "errors": errors})
        return 0
    lacking = [name for name in MODEL_FILES if "asr" in blocks and not (Path(job["model"]) / name).is_file()]
    if lacking:
        print(f"the Parakeet model folder {job['model']} lacks {', '.join(lacking)}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import take_measure
    start = time.time()
    found = models(job, blocks, int(job.get("threads") or 4))

    # Hear and measure
    files = job.get("files") or []
    for i, path in enumerate(files):
        try:
            x, sr = load(path)
            emit({"index": i, **gather(found, blocks, x, sr, take_measure)})
        except Exception as e:
            emit({"index": i, "error": f"{type(e).__name__}: {e}"})
    emit({"done": len(files), "seconds": round(time.time() - start, 2)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
