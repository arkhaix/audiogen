"""Measures of one take's signal, for the take check and the join, and of a joined chapter.

Pure functions over mono float arrays. Runs under the audio Python (machine.AUDIO_PYTHON) with numpy and scipy, and
imports nothing from serial_audio, like fx.py and breeze_join.py.

Levels are K-weighted, as ITU-R BS.1770 defines the weighting, over FRAME frames. A frame is speech when it is
within GATE_BELOW of the take's own level. A chapter's loudness is the gated loudness of BS.1770, and its true peak
is the peak of the signal between its samples too.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, firwin, resample_poly, sosfilt, sosfreqz

SIGNAL_V = 1
FRAME = 0.01
ABS_GATE = -70.0
GATE_BELOW = 30.0
MIN_SILENCE = 0.15
FULL_SCALE = 32767 / 32768
BAND = (300.0, 3000.0)
TAIL_SECONDS = 1.0
TAIL_MIN = 0.3
BODY_MIN = 1.0
PHRASE = 1.0
PHRASE_HOP = 0.25
PHRASE_SPEECH = 0.6
PHRASE_MIN = 8

# the analog prototypes of the BS.1770 K-weighting filters
SHELF_HZ, SHELF_DB, SHELF_Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
HIGHPASS_HZ, HIGHPASS_Q = 38.13547087602444, 0.5003270373238773
LKFS_OFFSET = -0.691
CALIBRATION_HZ = 997.0

# the gating blocks of BS.1770
BLOCK = 0.4
BLOCK_HOP = 0.1
RELATIVE_GATE = -10.0
# the rate BS.1770 oversamples to for a true peak
PEAK_RATE = 192000
PEAK_BLOCK = 256
PEAK_PAD = 32
PEAK_ROWS = 512
PEAK_NEAR = 9.0
STRETCH = 60.0


def k_sos(sr: int) -> np.ndarray:
    """The K-weighting filter for sample rate sr, as second-order sections for scipy's sosfilt."""
    k = np.tan(np.pi * SHELF_HZ / sr)
    vh = 10 ** (SHELF_DB / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / SHELF_Q + k * k
    shelf = [(vh + vb * k / SHELF_Q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / SHELF_Q + k * k) / a0,
             1.0, 2 * (k * k - 1) / a0, (1 - k / SHELF_Q + k * k) / a0]
    k = np.tan(np.pi * HIGHPASS_HZ / sr)
    a0 = 1 + k / HIGHPASS_Q + k * k
    highpass = [1.0, -2.0, 1.0, 1.0, 2 * (k * k - 1) / a0, (1 - k / HIGHPASS_Q + k * k) / a0]
    return np.array([shelf, highpass])


def k_weighted(x: np.ndarray, sr: int) -> np.ndarray:
    """Mono x through the K-weighting filter of its sample rate.

    x: mono samples.
    sr: their sample rate.
    Returns x filtered and scaled so that lkfs() of its frame powers reads a full-scale 997 Hz sine as -3.01 LKFS at
    any sample rate.
    """
    if not len(x):
        return np.zeros(0)
    sos = k_sos(sr)
    return sosfilt(sos, x) * k_scale(sos, sr)


def k_scale(sos: np.ndarray, sr: int) -> float:
    """The factor that calibrates the output of the K-weighting filter sos at sample rate sr."""
    return 10 ** (-LKFS_OFFSET / 20) / abs(sosfreqz(sos, worN=[CALIBRATION_HZ], fs=sr)[1][0])


def frame_power(x: np.ndarray, sr: int, frame: float = FRAME) -> np.ndarray:
    """The mean square of each whole frame of x. Samples after the last whole frame are left out."""
    n = max(1, int(round(frame * sr)))
    count = len(x) // n
    if not count:
        return np.zeros(0)
    return (x[: count * n].astype(np.float64).reshape(count, n) ** 2).mean(1)


def weighted_power(x: np.ndarray, sr: int) -> np.ndarray:
    """The K-weighted power of each whole FRAME of mono x.

    x: mono samples.
    sr: their sample rate.
    Returns what frame_power(k_weighted(x, sr), sr) returns.
    """
    sos = k_sos(sr)
    scale = k_scale(sos, sr)
    n = max(1, int(round(FRAME * sr)))
    step = n * max(1, int(round(STRETCH / FRAME)))
    state = np.zeros((len(sos), 2))
    out = []
    for start in range(0, len(x) - len(x) % n, step):
        piece, state = sosfilt(sos, x[start: min(start + step, len(x) - len(x) % n)], zi=state)
        out.append(frame_power(piece * scale, sr))
    return np.concatenate(out) if out else np.zeros(0)


def integrated(power: np.ndarray) -> float | None:
    """The gated loudness of a programme of one channel, in LUFS, as BS.1770 defines it.

    power: weighted_power of the programme.
    Blocks of BLOCK start BLOCK_HOP apart. A block counts when it is above ABS_GATE and within RELATIVE_GATE of
    the loudness of the blocks above ABS_GATE. A programme shorter than a block is one block. Returns None when no
    block counts.
    """
    size, hop = int(round(BLOCK / FRAME)), int(round(BLOCK_HOP / FRAME))
    if not power.size:
        return None
    if len(power) < size:
        blocks = np.array([power.mean()])
    else:
        total = np.concatenate(([0.0], np.cumsum(power, dtype=np.float64)))
        starts = np.arange(0, len(power) - size + 1, hop)
        blocks = (total[starts + size] - total[starts]) / size
    blocks = blocks[LKFS_OFFSET + 10 * np.log10(np.maximum(blocks, 1e-20)) >= ABS_GATE]
    if not blocks.size:
        return None
    gate = LKFS_OFFSET + 10 * np.log10(blocks.mean()) + RELATIVE_GATE
    blocks = blocks[LKFS_OFFSET + 10 * np.log10(blocks) >= gate]
    return float(LKFS_OFFSET + 10 * np.log10(blocks.mean()))


def loudness(x: np.ndarray, sr: int) -> float | None:
    """The gated loudness of mono x in LUFS, or None when x doesn't hold a block above ABS_GATE."""
    return integrated(weighted_power(x, sr))


def peak_envelope(x: np.ndarray, sr: int, floor: float = 0.0) -> np.ndarray:
    """The largest magnitude the signal of mono x reaches around each of its samples.

    x: mono samples.
    sr: their sample rate.
    floor: the magnitude under which a stretch of x is not looked into. 0.0 looks into all of x.
    Returns one value per sample, as float32: the peak of x oversampled to PEAK_RATE or above, from the sample
    before to the sample after. It is never under the sample's own magnitude. A PEAK_BLOCK of samples that are all
    under floor, between two more such blocks, gets each sample's own magnitude.
    """
    own = np.abs(x).astype(np.float32)
    up = max(1, -(-PEAK_RATE // sr))
    if up == 1 or not len(x):
        return own
    count = -(-len(x) // PEAK_BLOCK)
    padded = np.zeros((count + 2) * PEAK_BLOCK, dtype=np.float32)
    padded[PEAK_BLOCK: PEAK_BLOCK + len(x)] = x
    loud = np.abs(padded).reshape(count + 2, PEAK_BLOCK).max(1) >= floor
    blocks = np.flatnonzero(loud[:-2] | loud[1:-1] | loud[2:])
    # resample_poly scales the taps it is given by up
    taps = firwin(20 * up + 1, 1 / up, window=("kaiser", 5.0))
    groups = np.zeros(count * PEAK_BLOCK, dtype=np.float32)
    groups[: len(x)] = own
    groups = groups.reshape(count, PEAK_BLOCK)
    reach = np.arange(-PEAK_PAD, PEAK_BLOCK + PEAK_PAD)
    for first in range(0, len(blocks), PEAK_ROWS):
        some = blocks[first: first + PEAK_ROWS]
        rows = padded[(some[:, None] + 1) * PEAK_BLOCK + reach[None, :]].astype(np.float64)
        over = np.abs(resample_poly(rows, up, 1, axis=1, window=taps))[:, PEAK_PAD * up: (PEAK_PAD + PEAK_BLOCK) * up]
        groups[some] = over.reshape(len(some), PEAK_BLOCK, up).max(2)
    del padded
    groups = groups.reshape(-1)[: len(x)]
    looked = np.zeros(count, dtype=bool)
    looked[blocks] = True
    looked = np.repeat(looked, PEAK_BLOCK)[: len(x)]
    np.maximum(own, groups, out=own, where=looked)
    np.maximum(own[1:], groups[:-1], out=own[1:], where=looked[1:])
    return own


def true_peak(x: np.ndarray, sr: int) -> float | None:
    """The true peak of mono x in dB relative to full scale, or None for digital silence.

    The peak is looked for where x is within PEAK_NEAR of its largest sample.
    """
    most = float(np.abs(x).max()) if len(x) else 0.0
    if most <= 0:
        return None
    return 20 * np.log10(float(peak_envelope(x, sr, most * 10 ** (-PEAK_NEAR / 20)).max()))


def lkfs(power: np.ndarray) -> float | None:
    """The loudness of K-weighted frame powers, in LKFS. None for no frames or digital silence."""
    mean = float(power.mean()) if power.size else 0.0
    return LKFS_OFFSET + 10 * np.log10(mean) if mean > 0 else None


def speech_frames(power: np.ndarray) -> tuple[np.ndarray, float | None]:
    """Which K-weighted frames are speech, and the take's level.

    power: frame_power of the K-weighted take.
    Returns (mask, level). A frame is speech when it is above ABS_GATE and within GATE_BELOW of the level of the
    frames above ABS_GATE. The level is that of the speech frames, in LKFS, or None when no frame is speech.
    """
    db = LKFS_OFFSET + 10 * np.log10(np.maximum(power, 1e-20))
    loud = db >= ABS_GATE
    first = lkfs(power[loud])
    if first is None:
        return np.zeros(len(power), dtype=bool), None
    mask = loud & (db >= first - GATE_BELOW)
    return mask, lkfs(power[mask])


def level(x: np.ndarray, sr: int) -> float | None:
    """The K-weighted level of the speech in mono x, in LKFS, or None when x doesn't hold speech."""
    return speech_frames(frame_power(k_weighted(x, sr), sr))[1]


def clip_run(x: np.ndarray) -> int:
    """The longest run of consecutive samples at full scale of 16-bit audio."""
    full = np.abs(x) >= FULL_SCALE
    if not full.any():
        return 0
    edges = np.flatnonzero(np.diff(np.concatenate(([0], full.view(np.int8), [0]))))
    return int((edges[1::2] - edges[::2]).max())


def silent_runs(mask: np.ndarray, frame: float = FRAME, shortest: float = MIN_SILENCE) -> list[list[float]]:
    """The runs of frames without speech between the first and the last speech frame.

    mask: which frames are speech.
    Returns [[start, end], ...] in seconds from the start of the take, for each run of shortest seconds or more.
    """
    on = np.flatnonzero(mask)
    if on.size < 2:
        return []
    inside = ~mask[on[0]: on[-1] + 1]
    edges = np.flatnonzero(np.diff(np.concatenate(([0], inside.view(np.int8), [0])))) + on[0]
    return [[round(float(a * frame), 2), round(float(b * frame), 2)] for a, b in zip(edges[::2], edges[1::2])
            if (b - a) * frame >= shortest - 1e-9]


def tail_drop(x: np.ndarray, sr: int, mask: np.ndarray) -> float | None:
    """How far the last second of speech sits below the speech before it, in dB within BAND.

    x: the mono take, unweighted.
    mask: which FRAME frames are speech.
    Returns the last TAIL_SECONDS of the speech span against the rest, speech frames only. Negative means the
    ending is quieter. None when the ending holds under TAIL_MIN of speech or the rest under BODY_MIN.
    """
    on = np.flatnonzero(mask)
    if not on.size or BAND[1] >= sr / 2:
        return None
    power = frame_power(sosfilt(butter(4, BAND, "bandpass", fs=sr, output="sos"), x), sr)[: len(mask)]
    split = on[-1] + 1 - int(round(TAIL_SECONDS / FRAME))
    tail, body = on[on >= split], on[on < split]
    if tail.size * FRAME < TAIL_MIN or body.size * FRAME < BODY_MIN:
        return None
    a, b = float(power[tail].mean()), float(power[body].mean())
    return round(10 * np.log10(a / b), 1) if a > 0 and b > 0 else None


def phrases(power: np.ndarray, mask: np.ndarray) -> dict[int, float]:
    """The loudness of the speech in each PHRASE window, PHRASE_HOP apart, in LKFS, by the window's first frame.

    power: frame_power of the K-weighted take.
    mask: which frames are speech.
    A window counts when PHRASE_SPEECH of its frames or more are speech, and only its speech frames are measured.
    """
    size, hop = int(round(PHRASE / FRAME)), int(round(PHRASE_HOP / FRAME))
    out = {}
    for start in range(0, len(power) - size + 1, hop):
        window = slice(start, start + size)
        if mask[window].mean() >= PHRASE_SPEECH:
            out[start] = LKFS_OFFSET + 10 * float(np.log10(power[window][mask[window]].mean()))
    return out


def measure(x: np.ndarray, sr: int) -> dict:
    """The signal block of a take's evidence record.

    x: the take as mono float samples at its own sample rate, as soundfile loads a 16-bit file.
    sr: that sample rate.
    Returns {"v", "seconds", "level", "peak", "clip_run", "speech", "active", "silences", "tail_drop", "swing",
    "jump"}. level is in LKFS and peak in dBFS. speech is the [start, end] of the span from the first speech frame
    to the last, in seconds, and active the seconds of speech frames. silences are the silent runs inside that
    span. swing is the spread from the 10th to the 95th percentile of the loudness of the PHRASE windows, and jump
    the largest step between two windows PHRASE apart. Both need PHRASE_MIN windows. A measure the take is too short
    or too quiet for is None.
    """
    x = np.asarray(x, dtype=np.float64)
    power = frame_power(k_weighted(x, sr), sr)
    mask, loudness = speech_frames(power)
    on = np.flatnonzero(mask)
    peak = float(np.abs(x).max()) if x.size else 0.0
    loud = phrases(power, mask)
    levels = np.array(list(loud.values()))
    size = int(round(PHRASE / FRAME))
    steps = [abs(loud[start + size] - value) for start, value in loud.items() if start + size in loud]
    enough = len(loud) >= PHRASE_MIN
    return {
        "v": SIGNAL_V,
        "seconds": round(len(x) / sr, 2),
        "level": None if loudness is None else round(loudness, 1),
        "peak": round(20 * np.log10(peak), 1) if peak > 0 else None,
        "clip_run": clip_run(x),
        "speech": [round(float(on[0] * FRAME), 2), round(float((on[-1] + 1) * FRAME), 2)] if on.size else None,
        "active": round(float(on.size * FRAME), 2),
        "silences": silent_runs(mask),
        "tail_drop": tail_drop(x, sr, mask),
        "swing": round(float(np.percentile(levels, 95) - np.percentile(levels, 10)), 1) if enough else None,
        "jump": round(float(max(steps)), 1) if enough and steps else None,
    }
