"""Join a chapter's Breeze TTS 2 takes into one audio file.

Run as `python -I breeze_join.py <job.json>` under ComfyUI's embedded Python, which has numpy and soundfile.
The job is {"output": path, "cap_seconds": s, "takes": [{"line": n, "file": path, "effect": spec or null}, ...]}
with the takes in script order. Prints a JSON summary on its last line.
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


def level(x: np.ndarray, sr: int) -> np.ndarray:
    """x with its active speech at TARGET_DB RMS, the Drama 3 chapters' level, and peaks bent under full scale."""
    rms = windows_rms(x, sr)
    active = rms[rms >= fx.SILENCE_RMS]
    if active.size == 0:
        return x
    return fx.soft_limit(x * (10 ** (TARGET_DB / 20) / np.sqrt((active ** 2).mean())))


def join(job: dict) -> dict:
    """Writes job["output"] from the takes.

    job: the output path, the take length limit in seconds, and the takes in script order.
    Each take has its clicks filled in and fades in and out at its trimmed edges.
    Returns {"takes", "treated_lines", "raw_seconds", "seconds", "sample_rate", "capped", "clicks"}, where capped
    lists the lines whose take reached the length limit and clicks counts the clicks filled in.
    """
    parts, sr, treated, capped, clicks = [], None, 0, [], 0
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
        parts += [fx.fade_edges(x, sr), np.zeros(round(fx.TAKE_GAP * sr), dtype=np.float32)]
    if not parts:
        raise SystemExit("no takes to join")
    raw = np.concatenate(parts[:-1])
    final = level(fx.trim_pauses(raw, sr), sr)
    fx.write_audio(job["output"], final, sr)
    return {"takes": len(job["takes"]), "treated_lines": treated, "raw_seconds": round(len(raw) / sr, 2),
            "seconds": round(len(final) / sr, 2), "sample_rate": sr, "capped": capped, "clicks": clicks}


if __name__ == "__main__":
    print(json.dumps(join(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))))
