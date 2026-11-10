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
from scipy.ndimage import median_filter
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

CLICK_BAND = 5000
CLICK_FRAME = 0.002
CLICK_CONTEXT = 0.2
CLICK_OVER_DB = 25.0
CLICK_FLOOR_DB = -55.0
CLICK_WIDTH = 0.001
CLICK_ENVELOPE = 0.0005
CLICK_SPAN = 0.004
CLICK_AUDIBLE = 1.5
CLICK_HALF = 0.001
CLICK_PAD = 0.00025
DECLICK_PASSES = 2
AR_ORDER = 32
AR_CONTEXT = 0.02
EDGE_FADE = 0.003


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


def find_clicks(x: np.ndarray, sr: int) -> list[tuple[int, int]]:
    """The (start, end) sample span of each click in mono x, in order.

    A click is a burst above CLICK_BAND Hz that stands CLICK_OVER_DB over the median of the CLICK_CONTEXT around it
    and is louder than CLICK_FLOOR_DB. It stays above half its peak for under CLICK_WIDTH, and its CLICK_ENVELOPE
    moving RMS stays above a quarter of its peak for under CLICK_SPAN. It peaks at CLICK_AUDIBLE times the level of
    the louder of the two stretches from 2 to 20 ms before and after it, or more. The span covers that quarter-peak
    stretch and CLICK_HALF on each side of the peak, plus CLICK_PAD, and is never longer than CLICK_SPAN plus twice
    CLICK_PAD. Spans don't overlap. Returns no clicks when sr is too low for CLICK_BAND.
    """
    if sr <= 2 * CLICK_BAND:
        return []
    hp = sosfilt(butter(4, CLICK_BAND, "highpass", fs=sr, output="sos"), x)
    n = max(1, int(CLICK_FRAME * sr))
    count = len(hp) // n
    if count < 3:
        return []
    db = 10 * np.log10((hp[: count * n].reshape(count, n) ** 2).mean(1) + 1e-12)
    over = db - median_filter(db, size=int(CLICK_CONTEXT / CLICK_FRAME) | 1, mode="nearest")
    m = max(1, int(CLICK_ENVELOPE * sr))
    env = np.sqrt(np.convolve(hp ** 2, np.ones(m) / m, mode="same"))
    half, pad, limit = max(1, int(CLICK_HALF * sr)), max(1, int(CLICK_PAD * sr)), int(CLICK_SPAN * sr)
    found, last_end = [], 0
    for i in np.nonzero((over >= CLICK_OVER_DB) & (db >= CLICK_FLOOR_DB))[0]:
        start, stop = max(0, (i - 5) * n, last_end), (i + 10) * n
        if stop <= start:
            continue
        burst = np.abs(hp[start:stop])
        peak = start + int(np.argmax(burst))
        if (burst > burst.max() / 2).sum() >= CLICK_WIDTH * sr:
            continue
        near = max(0, peak - half)
        top = near + int(np.argmax(env[near: peak + half]))
        lo = hi = top
        while lo > 0 and env[lo - 1] > env[top] / 4 and hi - lo < limit:
            lo -= 1
        while hi < len(env) - 1 and env[hi + 1] > env[top] / 4 and hi - lo < limit:
            hi += 1
        a, b = max(0, min(lo, peak - half) - pad), min(len(x), max(hi + 1, peak + half) + pad)
        if hi - lo >= limit or b - a > limit + 2 * pad:
            continue
        sides = (x[max(0, peak - 20 * half): max(0, peak - 2 * half)], x[peak + 2 * half: peak + 20 * half])
        level = max((float(np.sqrt((s ** 2).mean())) for s in sides if s.size), default=0.0)
        if np.abs(hp[max(0, peak - half): peak + half]).max() >= CLICK_AUDIBLE * (level + 1e-4):
            found.append((max(a, last_end), b))
            last_end = b
    return found


def fill_gap(x: np.ndarray, start: int, end: int, sr: int) -> bool:
    """Replaces x[start:end] in place with the samples that an AR_ORDER model of the audio on both sides predicts
    best, and returns True. Falls back to a straight line between the neighbours when the model can't be fitted.
    Leaves x unchanged and returns False when the gap has AR_ORDER samples of audio or fewer on either side."""
    p = AR_ORDER
    ctx = max(2 * p, int(AR_CONTEXT * sr))
    lo, hi = max(0, start - ctx), min(len(x), end + ctx)
    if start - lo <= p or hi - end <= p:
        return False
    seg = x[lo:hi].astype(np.float64)
    g0, g1 = start - lo, end - lo
    try:
        a = librosa.lpc(np.r_[seg[:g0], seg[g1:]], order=p)
    except (FloatingPointError, ValueError):
        a = None
    if a is not None and np.all(np.isfinite(a)):
        cols = np.arange(g0 - p, g1 + p)
        rows = np.zeros((g1 - g0 + p, cols.size))
        for r in range(rows.shape[0]):
            rows[r, r: r + p + 1] = a[::-1]
        missing = (cols >= g0) & (cols < g1)
        fill = np.linalg.lstsq(rows[:, missing], -rows[:, ~missing] @ seg[cols[~missing]], rcond=None)[0]
        if np.all(np.isfinite(fill)):
            x[start:end] = fill.astype(x.dtype)
            return True
    x[start:end] = np.linspace(x[start - 1], x[end], end - start + 2)[1:-1].astype(x.dtype)
    return True


def declick(x: np.ndarray, sr: int) -> tuple[np.ndarray, int]:
    """Mono x with each click that find_clicks reports filled in by fill_gap, and the number filled in. Runs up to
    DECLICK_PASSES passes, so a click that sits beside a louder one is found once the louder one is filled. The audio
    is a copy when any click was filled in, and x itself otherwise."""
    y, filled = x, 0
    for _ in range(DECLICK_PASSES):
        clicks = find_clicks(y, sr)
        if not clicks:
            break
        if y is x:
            y = x.copy()
        done = sum(fill_gap(y, start, end, sr) for start, end in clicks)
        if not done:
            break
        filled += done
    return (y, filled) if filled else (x, 0)


def fade_edges(x: np.ndarray, sr: int) -> np.ndarray:
    """A copy of x with an EDGE_FADE raised-cosine fade in and out, so it starts and ends at zero. Returns x itself
    when it is shorter than four samples."""
    n = min(len(x) // 2, int(EDGE_FADE * sr))
    if n < 2:
        return x
    ramp = (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, n))).astype(x.dtype)
    y = x.copy()
    y[:n] *= ramp
    y[-n:] *= ramp[::-1]
    return y


def trim_pauses(x: np.ndarray, sr: int) -> np.ndarray:
    """drama3/audio.py trim_pauses on mono audio: no silence before the first sound, pauses cut to MAX_PAUSE, TAIL at the end."""
    return np.concatenate([x[start:end] for start, end in pause_pieces(x, sr)])


def pause_pieces(x: np.ndarray, sr: int) -> list[tuple[int, int]]:
    """The sample ranges of mono audio that trim_pauses keeps, in order.

    x: mono audio.
    sr: its sample rate.
    Returns [(start, end), ...]. All of x when it has no sound.
    """
    window = max(1, int(SILENCE_WINDOW * sr))
    count = -(-len(x) // window)
    padded = np.pad(x, (0, count * window - len(x)))
    sums = (padded.reshape(count, window) ** 2).sum(1)
    sizes = np.full(count, window)
    sizes[-1] = len(x) - (count - 1) * window
    quiet = np.sqrt(sums / sizes) < SILENCE_RMS
    if quiet.all():
        return [(0, len(x))]
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
    return [(start, min(end, limit)) for start, end in pieces if start < limit]


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
