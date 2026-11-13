"""Join a chapter's Breeze TTS 2 takes into one mastered audio file.

Run as `python -I breeze_join.py <job.json>` under the audio Python, which has numpy, scipy and soundfile.
`python -I breeze_join.py --level <file>...` brings each audio file to TARGET_LUFS in place instead.
The job is {"output": path, "cap_seconds": s, "takes": [{"line": n, "file": path, "voice": id, "effect": spec or
null, "offset": dB, "cuts": [[start, end], ...], "gap": seconds}, ...]} with the takes in script order. offset is how
far from its voice's ordinary level the take belongs, cuts are the spans of the take's file to leave out, and gap is
the silence after the take. All three are optional. Prints a JSON summary on its last line.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import minimum_filter1d, uniform_filter1d

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fx  # noqa: E402
import take_measure as tm  # noqa: E402

TARGET_LUFS = -18.0
LOUDNESS_TOLERANCE = 0.1
CEILING_DB = -1.0
CODEC_ROOM = 1.0
CODEC_LOSS = 0.15
DELIVER_PASSES = 3
LIMIT_MARGIN = 0.1
LOOKAHEAD = 0.005
RELEASE = 0.05
MAX_CORRECTION = 6.0
FULL_TRUST = 1.5
COMPRESS_OVER = 6.0
COMPRESS_KNEE = 2.0
COMPRESS_RATIO = 3.0
CUT_FADE = 0.01
MAX_GAP = 10.0
OPUS_KBPS = 40


def windows_rms(x: np.ndarray, sr: int) -> np.ndarray:
    w = max(1, int(fx.SILENCE_WINDOW * sr))
    n = len(x) // w
    return np.sqrt((x[: n * w].reshape(n, w) ** 2).mean(1)) if n else np.zeros(0)


def edges(x: np.ndarray, sr: int) -> np.ndarray:
    """x without leading and trailing silence, judged the way fx.trim_pauses judges it."""
    loud = np.nonzero(windows_rms(x, sr) >= fx.SILENCE_RMS)[0]
    if loud.size == 0:
        return x
    w = max(1, int(fx.SILENCE_WINDOW * sr))
    return x[loud[0] * w: (loud[-1] + 1) * w]


def finite(value) -> bool:
    """Whether a value of a job is a number that isn't NaN or infinite."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and bool(np.isfinite(value))


def cut(x: np.ndarray, sr: int, cuts: list) -> tuple[np.ndarray, int]:
    """x without the spans in cuts.

    x: mono audio.
    sr: its sample rate.
    cuts: [[start, end], ...] in seconds of x.
    Returns (audio, count). The audio fades across each cut over CUT_FADE, and is shorter than x by the length of
    the cuts made. A cut that ends less than CUT_FADE from the end of x, before or past it, ends the audio with a
    fade out over CUT_FADE, and no later cut is made. count is how many cuts were made. A cut that isn't two finite
    numbers in order, that ends further past x, or that starts within CUT_FADE of the cut before it is left out.
    Returns x itself when no cut is made.
    """
    fade = max(1, int(CUT_FADE * sr))
    spans = sorted((int(round(c[0] * sr)), int(round(c[1] * sr))) for c in (cuts if isinstance(cuts, list) else [])
                   if isinstance(c, (list, tuple)) and len(c) == 2 and all(finite(n) for n in c))
    ramp = (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, fade))).astype(x.dtype)
    parts, at, made, ended = [], 0, 0, False
    for start, end in spans:
        if start - fade < at or end - start < fade or end >= len(x) + fade:
            continue
        parts.append(x[at: start - fade])
        made += 1
        if end > len(x) - fade:
            parts.append(x[start - fade: start] * ramp[::-1])
            ended = True
            break
        parts.append(x[start - fade: start] * ramp[::-1] + x[end - fade: end] * ramp)
        at = end
    if not made:
        return x, 0
    return np.concatenate(parts + ([] if ended else [x[at:]])), made


def speech(x: np.ndarray, sr: int) -> tuple[float | None, float]:
    """The K-weighted level of the speech in mono x, in LKFS, and its seconds of speech. (None, 0.0) without speech."""
    mask, level = tm.speech_frames(tm.frame_power(tm.k_weighted(x, sr), sr))
    return level, float(mask.sum() * tm.FRAME)


def take_gains(measured: list[tuple[str | None, float | None, float, float]]) -> tuple[list[float], dict[str, float]]:
    """The gain for each take, in dB.

    measured: one (voice, level, offset, active) per take: the level of its speech in LKFS or None, how far from
        its voice's ordinary level it belongs in dB, and its seconds of speech.
    Returns (gains, voice_gains). A voice's gain brings its median take to TARGET_LUFS, with each take counted at
    its level less its offset. A take's gain is its voice's gain and a correction toward TARGET_LUFS plus its
    offset. The correction is MAX_CORRECTION at most either way, and it shrinks in proportion for a take with
    under FULL_TRUST seconds of speech. A take without a voice or without speech gets 0.
    """
    by_voice: dict[str, list[float]] = {}
    for voice, level, offset, _ in measured:
        if voice and level is not None:
            by_voice.setdefault(voice, []).append(level - offset)
    voices = {voice: TARGET_LUFS - float(np.median(levels)) for voice, levels in by_voice.items()}
    gains = []
    for voice, level, offset, active in measured:
        if not voice or level is None:
            gains.append(0.0)
            continue
        short = TARGET_LUFS + offset - (level + voices[voice])
        correction = min(MAX_CORRECTION, max(-MAX_CORRECTION, short)) * min(1.0, active / FULL_TRUST)
        gains.append(voices[voice] + correction)
    return gains, voices


def scaled(x: np.ndarray, sr: int, frame_db: np.ndarray) -> np.ndarray:
    """x with a gain that moves through one value per FRAME.

    x: mono audio.
    frame_db: the gain in dB at the middle of each FRAME of x.
    Returns a float32 copy. The gain runs in a straight line from one frame's middle to the next.
    """
    n = max(1, int(round(tm.FRAME * sr)))
    middles = (np.arange(len(frame_db)) + 0.5) * n
    y = x.astype(np.float32)
    step = max(1, int(round(tm.STRETCH * sr)))
    for start in range(0, len(y), step):
        at = np.arange(start, min(start + step, len(y)))
        y[start: start + len(at)] *= (10 ** (np.interp(at, middles, frame_db) / 20)).astype(np.float32)
    return y


def compress(x: np.ndarray, sr: int, normal: float = TARGET_LUFS) -> tuple[np.ndarray, float]:
    """x with its loudest passages turned down.

    x: mono audio whose ordinary speech is at normal.
    sr: its sample rate.
    normal: the level of ordinary speech, in LKFS.
    Returns (audio, most). The loudness of each tm.BLOCK of x is compared with normal plus COMPRESS_OVER. A passage
    over that comes down by its excess less its excess over COMPRESS_RATIO, and the gain bends in over
    COMPRESS_KNEE around that level. most is the largest reduction in dB. Returns x itself when no passage reaches
    the knee.
    """
    power = tm.weighted_power(x, sr)
    if not power.size:
        return x, 0.0
    size = int(round(tm.BLOCK / tm.FRAME))
    block = uniform_filter1d(power, size=size, mode="constant")
    over = tm.LKFS_OFFSET + 10 * np.log10(np.maximum(block, 1e-20)) - (normal + COMPRESS_OVER)
    slope, half = 1 - 1 / COMPRESS_RATIO, COMPRESS_KNEE / 2
    down = np.where(over >= half, slope * over, slope * (over + half) ** 2 / (2 * COMPRESS_KNEE))
    down[over <= -half] = 0.0
    if not down.any():
        return x, 0.0
    return scaled(x, sr, -down), float(down.max())


def limit(x: np.ndarray, sr: int, ceiling_db: float = CEILING_DB) -> tuple[np.ndarray, float]:
    """x with its true peaks at ceiling_db or under.

    x: mono audio.
    sr: its sample rate.
    ceiling_db: the highest true peak to leave, in dB relative to full scale.
    Returns (audio, most). The gain comes down over twice LOOKAHEAD ahead of a peak over the ceiling, stays down
    for RELEASE after it and goes back over twice LOOKAHEAD. Samples further from every such peak are unchanged.
    most is the largest reduction in dB. Returns x itself when no peak is over the ceiling. Peaks are looked for
    where x is within tm.PEAK_NEAR of the ceiling.
    """
    ceiling = 10 ** (ceiling_db / 20)
    near = ceiling * 10 ** (-tm.PEAK_NEAR / 20)
    envelope = tm.peak_envelope(x, sr, near)
    if not envelope.size or envelope.max() <= ceiling:
        return x, 0.0
    aim = 10 ** ((ceiling_db - LIMIT_MARGIN) / 20)
    np.maximum(envelope, np.float32(1e-9), out=envelope)
    np.divide(np.float32(aim), envelope, out=envelope)
    np.minimum(envelope, np.float32(1.0), out=envelope)
    ahead = max(1, int(LOOKAHEAD * sr))
    hold = max(ahead, int(RELEASE * sr))
    hold += (ahead + hold) % 2
    # the window reaches hold samples back and ahead samples forward, and the mean over ahead either way stays under need
    low = minimum_filter1d(envelope, size=ahead + hold + 1, origin=(hold - ahead) // 2, mode="nearest")
    del envelope
    gain = uniform_filter1d(low, size=2 * ahead + 1, mode="nearest")
    del low
    most = float(-20 * np.log10(gain.min()))
    np.multiply(gain, x, out=gain)
    left = float(tm.peak_envelope(gain, sr, near).max())
    if left > ceiling:
        gain *= np.float32(ceiling / left)
    return gain, most


def deliver(x: np.ndarray, sr: int, path) -> tuple[np.ndarray, float | None, float | None, float]:
    """Writes x to path at TARGET_LUFS with its true peaks at CEILING_DB or under, measured on the file as it plays.

    x: the chapter as mono audio.
    sr: its sample rate.
    path: where to write. A .opus path gets Ogg Opus at OPUS_KBPS.
    Returns (audio, loudness, true_peak, limited): the audio that was encoded, the gated loudness in LUFS and the
    true peak in dB of the file read back, and the limiter's largest reduction in dB. The audio for an Opus file
    goes to the encoder CODEC_LOSS louder and with CODEC_ROOM under the ceiling. The gain is raised once for what
    the limiter took. When the file read back misses the target by over LOUDNESS_TOLERANCE or goes over the
    ceiling, the gain and the limiter's ceiling are corrected from it and it is written again, DELIVER_PASSES times
    at most. Any other format is measured as written. Audio without a block above the gate is written as it is.
    """
    lossy = Path(path).suffix.lower() == ".opus"
    start = tm.loudness(x, sr)
    if start is None:
        fx.write_audio(path, x, sr, kbps=OPUS_KBPS)
        return x, None, tm.true_peak(x, sr), 0.0
    gain = TARGET_LUFS - start + (CODEC_LOSS if lossy else 0.0)
    ceiling = CEILING_DB - (CODEC_ROOM if lossy else 0.0)
    for _ in range(DELIVER_PASSES):
        y, limited = limit((x * 10 ** (gain / 20)).astype(np.float32), sr, ceiling)
        taken = TARGET_LUFS + (CODEC_LOSS if lossy else 0.0) - (tm.loudness(y, sr) or 0.0)
        if abs(taken) > LOUDNESS_TOLERANCE / 2:
            gain += taken
            y, limited = limit((x * 10 ** (gain / 20)).astype(np.float32), sr, ceiling)
        fx.write_audio(path, y, sr, kbps=OPUS_KBPS)
        heard, rate = y, sr
        if lossy:
            decoded, rate = sf.read(path, dtype="float32", always_2d=True)
            heard = decoded.mean(1)
        loud, peak = tm.loudness(heard, rate), tm.true_peak(heard, rate)
        if loud is None or (abs(loud - TARGET_LUFS) <= LOUDNESS_TOLERANCE and (peak is None or peak <= CEILING_DB)):
            break
        gain += TARGET_LUFS - loud
        ceiling -= max(0.0, (peak if peak is not None else CEILING_DB) - CEILING_DB + LIMIT_MARGIN)
    return y, loud, peak, limited


def join(job: dict) -> dict:
    """Writes job["output"] from the takes.

    job: the output path, the take length limit in seconds, and the takes in script order.
    Each take loses its cuts, has its clicks filled in, is trimmed to its sound with its pauses capped at
    fx.MAX_PAUSE, and fades in and out. Its gain is take_gains'. The takes are joined with each take's gap after it,
    or fx.TAKE_GAP for a gap that isn't a number from 0 to MAX_GAP. An offset that isn't a finite number counts as
    0. The chapter is then compressed and written by deliver.
    Returns {"takes", "treated_lines", "raw_seconds", "seconds", "sample_rate", "capped", "clicks", "voice_gains",
    "cuts", "cut_seconds", "loudness", "true_peak", "compressed", "limited"}. capped lists the lines whose take
    reached the length limit, clicks counts the clicks filled in, voice_gains maps each voice to its gain in dB, cuts
    counts the cuts made and cut_seconds their length. loudness is the gated loudness in LUFS and true_peak the
    true peak in dB of the file as it plays. compressed and limited are the largest reduction of each in dB.
    """
    # Read and repair each take
    cleaned, measured, gaps = [], [], []
    sr, treated, capped, clicks, cuts, cut_samples, raw = None, 0, [], 0, 0, 0, 0
    cap = float(job.get("cap_seconds") or 0)
    for take in job["takes"]:
        audio, rate = sf.read(take["file"], dtype="float32", always_2d=True)
        if sr is None:
            sr = rate
        elif rate != sr:
            raise SystemExit(f"take for line {take['line']} is {rate} Hz, the others are {sr} Hz")
        if cap and len(audio) >= (cap - 0.5) * sr:
            capped.append(take["line"])
        gap = take.get("gap")
        gaps.append(round((gap if finite(gap) and 0 <= gap <= MAX_GAP else fx.TAKE_GAP) * sr))
        raw += len(audio) + gaps[-1]
        x, made = cut(audio.mean(1), sr, take.get("cuts") or [])
        cuts += made
        cut_samples += len(audio) - len(x)
        x, fixed = fx.declick(x, sr)
        clicks += fixed
        x = edges(x, sr)
        spec = take.get("effect")
        if spec:
            wet = fx.whisper_effect(x, sr, np.random.default_rng(take["line"]), voice=float(spec.get("voice", 0.35)))
            x = wet * np.sqrt(np.mean(x ** 2)) / (np.sqrt(np.mean(wet[: len(x)] ** 2)) + 1e-9)
            treated += 1
        # the pauses are found before any gain, which would move speech across the threshold
        x = fx.fade_edges(np.concatenate([x[start:end] for start, end in fx.pause_pieces(x, sr)]), sr)
        offset = take.get("offset")
        level, active = speech(x, sr)
        measured.append((take.get("voice"), level, float(offset) if finite(offset) else 0.0, active))
        cleaned.append(x)
    if not cleaned:
        raise SystemExit("no takes to join")

    # Level each take and join
    gains, voices = take_gains(measured)
    parts = []
    for x, gain, gap in zip(cleaned, gains, gaps):
        parts += [(x * 10 ** (gain / 20)).astype(np.float32), np.zeros(gap, dtype=np.float32)]
    joined = np.concatenate(parts[:-1])
    del parts, cleaned

    # Master the chapter
    mastered, compressed = compress(joined, sr)
    final, after, peak, limited = deliver(mastered, sr, job["output"])
    return {"takes": len(job["takes"]), "treated_lines": treated, "raw_seconds": round((raw - gaps[-1]) / sr, 2),
            "seconds": round(len(final) / sr, 2), "sample_rate": sr, "capped": capped, "clicks": clicks,
            "voice_gains": {voice: round(db, 1) for voice, db in sorted(voices.items())},
            "cuts": cuts, "cut_seconds": round(cut_samples / sr, 2),
            "loudness": None if after is None else round(after, 1), "true_peak": None if peak is None else round(peak, 1),
            "compressed": round(compressed, 1), "limited": round(limited, 1)}


def level_file(path) -> dict:
    """Brings a mono audio file to TARGET_LUFS, in place, with its true peaks at CEILING_DB or under.

    path: the file. It keeps its format, its sample rate and its sample type.
    Returns {"file", "before", "after", "peak"}: the gated loudness before and after in LUFS, and the true peak
    after in dB. The gain is raised for what the limiter takes, DELIVER_PASSES times at most. A file that is not
    mono, or that doesn't have a block above the gate, is left as it is, and its after is its before.
    """
    info = sf.info(str(path))
    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    before = tm.loudness(audio.mean(1), sr) if audio.shape[1] == 1 else None
    if before is None:
        measured = tm.loudness(audio.mean(1), sr)
        return {"file": str(path), "before": measured, "after": measured, "peak": tm.true_peak(audio.mean(1), sr)}
    x, gain = audio[:, 0], TARGET_LUFS - before
    for _ in range(DELIVER_PASSES):
        y, _ = limit((x * 10 ** (gain / 20)).astype(np.float32), sr)
        taken = TARGET_LUFS - (tm.loudness(y, sr) or TARGET_LUFS)
        if abs(taken) <= LOUDNESS_TOLERANCE / 2:
            break
        gain += taken
    sf.write(str(path), y, sr, format=info.format, subtype=info.subtype)
    return {"file": str(path), "before": before, "after": tm.loudness(y, sr), "peak": tm.true_peak(y, sr)}


if __name__ == "__main__" and sys.argv[1:2] == ["--level"]:
    print(json.dumps([level_file(name) for name in sys.argv[2:]]))
    sys.exit(0)

if __name__ == "__main__":
    print(json.dumps(join(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))))
