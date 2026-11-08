"""Transcribes audio files with NVIDIA Parakeet TDT 0.6B v2 through sherpa-onnx, on the CPU.

    python -I parakeet_asr.py < job.json

The job on stdin is {"model": <folder>, "files": [<path>, ...], "threads": <n>}. The model folder holds the int8
export from the k2-fsa asr-models release: encoder.int8.onnx, decoder.int8.onnx, joiner.int8.onnx and tokens.txt.
Each file gives one JSON line on stdout, in order, {"index": i, "text": "..."} or {"index": i, "error": "..."}.
A last line {"done": n, "seconds": s} follows. Runs under the audio Python (machine.AUDIO_PYTHON) with sherpa-onnx,
numpy, scipy and soundfile, and imports nothing from the package.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

RATE = 16000
MODEL_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")


def emit(record: dict) -> None:
    # ASCII only: under -I a piped stdout on Windows is cp1252, and the parent reads UTF-8.
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def load(path: str):
    """The file as 16 kHz mono float32 samples."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != RATE:
        g = math.gcd(int(sr), RATE)
        x = resample_poly(x, RATE // g, int(sr) // g).astype(np.float32)
    return x


def recognizer(model: Path, threads: int):
    import sherpa_onnx
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(model / MODEL_FILES[0]), decoder=str(model / MODEL_FILES[1]), joiner=str(model / MODEL_FILES[2]),
        tokens=str(model / MODEL_FILES[3]), num_threads=threads, model_type="nemo_transducer",
        decoding_method="greedy_search")


def main() -> int:
    # Setup
    job = json.loads(sys.stdin.read())
    model = Path(job["model"])
    missing = [name for name in MODEL_FILES if not (model / name).is_file()]
    if missing:
        print(f"the Parakeet model folder {model} lacks {', '.join(missing)}", file=sys.stderr)
        return 2
    start = time.time()
    rec = recognizer(model, int(job.get("threads") or 4))

    # Transcribe
    files = job.get("files") or []
    for i, path in enumerate(files):
        try:
            samples = load(path)
            stream = rec.create_stream()
            stream.accept_waveform(RATE, samples)
            rec.decode_stream(stream)
            emit({"index": i, "text": stream.result.text.strip()})
        except Exception as e:
            emit({"index": i, "error": f"{type(e).__name__}: {e}"})
    emit({"done": len(files), "seconds": round(time.time() - start, 2)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
