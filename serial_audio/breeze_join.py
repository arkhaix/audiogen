"""Join a chapter's Breeze TTS 2 takes into one audio file.

Run as `python -I breeze_join.py <job.json>` under ComfyUI's embedded Python, which has numpy and soundfile.
The job is {"output": path, "cap_seconds": s, "takes": [{"line": n, "file": path, "voice": id, "effect": spec or
null}, ...]} with the takes in script order. Prints a JSON summary on its last line.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fx  # noqa: E402

TARGET_DB = -18.0


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


def active_db(x: np.ndarray, sr: int) -> float | None:
    """The RMS of the active speech in dB.

    x: mono audio.
    sr: its sample rate.
    Returns the level, or None when x has no active speech.
    """
    rms = windows_rms(x, sr)
    active = rms[rms >= fx.SILENCE_RMS]
    if active.size == 0:
        return None
    return float(10 * np.log10((active ** 2).mean()))


def level(x: np.ndarray, sr: int) -> np.ndarray:
    """x with its active speech at TARGET_DB RMS, the Drama 3 chapters' level, and peaks bent under full scale."""
    db = active_db(x, sr)
    if db is None:
        return x
    return fx.soft_limit(x * 10 ** ((TARGET_DB - db) / 20))


def voice_gains(levels: list[tuple[str | None, float | None]]) -> dict[str, float]:
    """The gain in dB that brings each voice's median take to TARGET_DB.

    levels: one (voice, active_db) per take. A take with no voice or no active speech is not counted.
    Returns voice -> gain in dB.
    """
    by_voice: dict[str, list[float]] = {}
    for voice, db in levels:
        if voice and db is not None:
            by_voice.setdefault(voice, []).append(db)
    return {voice: TARGET_DB - float(np.median(dbs)) for voice, dbs in by_voice.items()}


def join(job: dict) -> dict:
    """Writes job["output"] from the takes.

    job: the output path, the take length limit in seconds, and the takes in script order.
    Each take has its clicks filled in and fades in and out at its trimmed edges. Each voice gets one gain that
    brings its median take to TARGET_DB. A take without a voice gets no voice gain.
    Returns {"takes", "treated_lines", "raw_seconds", "seconds", "sample_rate", "capped", "clicks", "voice_gains"},
    where capped lists the lines whose take reached the length limit, clicks counts the clicks filled in and
    voice_gains maps each voice to its gain in dB.
    """
    # Read and repair each take
    cleaned, sr, treated, capped, clicks = [], None, 0, [], 0
    cap = float(job.get("cap_seconds") or 0)
    for take in job["takes"]:
        audio, rate = sf.read(take["file"], dtype="float32", always_2d=True)
        if sr is None:
            sr = rate
        elif rate != sr:
            raise SystemExit(f"take for line {take['line']} is {rate} Hz, the others are {sr} Hz")
        if cap and len(audio) >= (cap - 0.5) * sr:
            capped.append(take["line"])
        x, fixed = fx.declick(audio.mean(1), sr)
        clicks += fixed
        x = edges(x, sr)
        spec = take.get("effect")
        if spec:
            wet = fx.whisper_effect(x, sr, np.random.default_rng(take["line"]), voice=float(spec.get("voice", 0.35)))
            x = wet * np.sqrt(np.mean(x ** 2)) / (np.sqrt(np.mean(wet[: len(x)] ** 2)) + 1e-9)
            treated += 1
        cleaned.append((take.get("voice"), x))
    if not cleaned:
        raise SystemExit("no takes to join")

    # Level each voice
    gains = voice_gains([(voice, active_db(x, sr)) for voice, x in cleaned])
    gap = round(fx.TAKE_GAP * sr)
    parts, factors = [], []
    for voice, x in cleaned:
        parts += [fx.fade_edges(x, sr), np.zeros(gap, dtype=np.float32)]
        factors += [np.full(len(x), 10 ** (gains.get(voice, 0.0) / 20), dtype=np.float32), np.ones(gap, dtype=np.float32)]

    # Join and level the chapter. The pauses are found before the gains, which would move speech across the threshold.
    raw = np.concatenate(parts[:-1])
    gained = raw * np.concatenate(factors[:-1])
    final = level(np.concatenate([gained[start:end] for start, end in fx.pause_pieces(raw, sr)]), sr)
    fx.write_audio(job["output"], final, sr)
    return {"takes": len(job["takes"]), "treated_lines": treated, "raw_seconds": round(len(raw) / sr, 2),
            "seconds": round(len(final) / sr, 2), "sample_rate": sr, "capped": capped, "clicks": clicks,
            "voice_gains": {voice: round(db, 1) for voice, db in sorted(gains.items())}}


if __name__ == "__main__":
    print(json.dumps(join(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))))
