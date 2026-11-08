"""Per-voice effects on a decoded Drama 3 chapter.

Run as `python -I fx.py <job.json>`, or `python -I fx.py --encode <src> <dst>` to re-encode a file by the
destination's suffix, under ComfyUI's embedded Python, which has numpy, scipy, librosa and soundfile.
It imports nothing from serial_audio.

Takes a raw decode (Drama3Decode with shorten_long_pauses off) and its codes file. Replaces each line of an affected
voice slot with its effect at the original line's loudness, then trims pauses as Drama3Decode does. Every other
sample matches an untreated render.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from scipy.signal import butter, fftconvolve, resample_poly, sosfilt

# Drama3Decode and drama3/audio.py constants
FRAME_LENGTH = 2048
TAKE_GAP = 0.3
MAX_PAUSE = 0.75
SILENCE_RMS = 0.003
SILENCE_WINDOW = 0.05
TAIL = 0.15

N_FFT, HOP = 1024, 256
OPUS_RATES = (8000, 12000, 16000, 24000, 48000)
EFFECTS = {"whisper"}


def codes_metadata(path: Path) -> tuple[list[dict], list[int]]:
    """The lines and takes that Drama3SaveCodes stored in a codes file's safetensors header."""
    with open(path, "rb") as f:
        size = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(size))
    meta = header.get("__metadata__") or {}
    return json.loads(meta["lines"]), json.loads(meta["takes"])


def line_spans(lines: list[dict], takes: list[int], sample_rate: int) -> list[tuple[dict, int, int]]:
    """(line, start sample, end sample) in the raw decode, which joins the non-empty takes with TAKE_GAP of silence."""
    gap = round(TAKE_GAP * sample_rate)
    bounds, frame, joined = [], 0, 0
    for n in takes:
        bounds.append((frame, frame + n, joined))
        frame += n
        if n:
            joined += 1
    spans = []
    for line in lines:
        if line["end"] <= line["start"]:
            continue
        gaps = next(j for first, last, j in bounds if first <= line["start"] < last)
        spans.append((line, line["start"] * FRAME_LENGTH + gaps * gap, line["end"] * FRAME_LENGTH + gaps * gap))
    return spans


def write_audio(path, x: np.ndarray, sr: int) -> None:
    """Writes mono x to path. A .opus path gets Ogg Opus, resampled to 48 kHz when sr is not a rate Opus takes."""
    x = x.astype(np.float32)
    if Path(path).suffix.lower() != ".opus":
        sf.write(path, x, sr)
        return
    if sr not in OPUS_RATES:
        g = np.gcd(48000, sr)
        x, sr = resample_poly(x, 48000 // g, sr // g).astype(np.float32), 48000
    sf.write(path, x, sr, format="OGG", subtype="OPUS")


def highpass(x, hz, sr):
    return sosfilt(butter(2, hz, "highpass", fs=sr, output="sos"), x)


def whispered(x, rng):
    X = librosa.stft(x, n_fft=N_FFT, hop_length=HOP)
    return librosa.istft(np.abs(X) * np.exp(2j * np.pi * rng.random(X.shape)), hop_length=HOP, length=len(x))


def room(x, sr, rng, seconds=0.9, wet=0.2, predelay=0.025):
    """x through a small synthetic room; the result is longer than x by the room's tail."""
    n = int(seconds * sr)
    ir = rng.standard_normal(n) * np.exp(-6.9 * np.arange(n) / n)
    ir = sosfilt(butter(2, 5000, fs=sr, output="sos"), ir)
    ir = np.concatenate([np.zeros(int(predelay * sr)), ir / np.sqrt(np.sum(ir ** 2))])
    tail = fftconvolve(x, ir)
    return (1 - wet) * np.pad(x, (0, len(tail) - len(x))) + wet * tail


def whisper_effect(x, sr, rng, voice=0.35):
    """Mostly whisper, with `voice` of the original underneath, in a small room."""
    return room(highpass(whispered(x, rng), 250, sr) + voice * x, sr, rng)


def soft_limit(x, knee=0.8, ceiling=0.99):
    """x unchanged below `knee`; louder samples bend smoothly toward `ceiling` and never reach it."""
    over = np.abs(x) > knee
    room_left = ceiling - knee
    x = x.copy()
    x[over] = np.sign(x[over]) * (knee + room_left * np.tanh((np.abs(x[over]) - knee) / room_left))
    return x


def trim_pauses(x: np.ndarray, sr: int) -> np.ndarray:
    """drama3/audio.py trim_pauses on mono audio: no silence before the first sound, pauses cut to MAX_PAUSE, TAIL at the end."""
    window = max(1, int(SILENCE_WINDOW * sr))
    count = -(-len(x) // window)
    padded = np.pad(x, (0, count * window - len(x)))
    sums = (padded.reshape(count, window) ** 2).sum(1)
    sizes = np.full(count, window)
    sizes[-1] = len(x) - (count - 1) * window
    quiet = np.sqrt(sums / sizes) < SILENCE_RMS
    if quiet.all():
        return x
    pieces, held, started = [], 0, False
    for index, silent in enumerate(quiet):
        start = index * window
        if not silent:
            started, held = True, 0
            pieces.append((start, min(start + window, len(x))))
        elif started:
            keep = min(window, max(0, int(MAX_PAUSE * sr) - held))
            if keep:
                pieces.append((start, start + keep))
            held += keep
    last = int(np.nonzero(~quiet)[0][-1])
    limit = (last + 1) * window + int(TAIL * sr)
    return np.concatenate([x[start:min(end, limit)] for start, end in pieces if start < limit])


def apply(job: dict) -> dict:
    """Writes job["output"] from job["raw"] with job["effects"] ({slot: {"effect": name, ...}}) applied. Returns a summary."""
    audio, sr = sf.read(job["raw"], dtype="float32", always_2d=True)
    raw = audio.mean(1)
    out = raw.copy()
    lines, takes = codes_metadata(Path(job["codes"]))
    effects = {int(slot): spec for slot, spec in job["effects"].items()}
    treated = 0
    for index, (line, start, end) in enumerate(line_spans(lines, takes, sr)):
        spec = effects.get(line["speaker"])
        if not spec:
            continue
        rng = np.random.default_rng(index)
        segment = raw[start:end]
        wet = whisper_effect(segment, sr, rng, voice=float(spec.get("voice", 0.35)))
        wet *= np.sqrt(np.mean(segment ** 2)) / (np.sqrt(np.mean(wet[:len(segment)] ** 2)) + 1e-9)
        stop = min(len(out), start + len(wet))
        out[start:stop] = soft_limit(np.concatenate([np.zeros(end - start), out[end:stop]]) + wet[:stop - start])
        treated += 1
    final = trim_pauses(out, sr) if job.get("trim_pauses", True) else out
    write_audio(job["output"], final, sr)
    return {"treated_lines": treated, "raw_seconds": round(len(raw) / sr, 2), "seconds": round(len(final) / sr, 2)}


if __name__ == "__main__" and sys.argv[1:2] == ["--encode"]:
    audio, rate = sf.read(sys.argv[2], dtype="float32", always_2d=True)
    write_audio(sys.argv[3], audio.mean(1), rate)
    print(json.dumps({"seconds": round(len(audio) / rate, 2)}))
    sys.exit(0)

if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    for spec in job["effects"].values():
        if spec.get("effect") not in EFFECTS:
            sys.exit(f"fx: unknown effect {spec.get('effect')!r}; known: {sorted(EFFECTS)}")
    print(json.dumps(apply(job)))
