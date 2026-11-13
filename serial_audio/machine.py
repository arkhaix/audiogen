"""Where this machine keeps the pipeline's tools and outputs.

Each path comes from an environment variable and defaults to the Windows laptop's layout, so the laptop needs no
settings. A cluster node sets them in the env of its serial_audio_config file.

SERIAL_AUDIO_COMFY_OUTPUT: ComfyUI's output folder. On a node without ComfyUI, the folder for Breeze takes and joined
    audio.
SERIAL_AUDIO_AUDIO_PYTHON: the Python that runs fx.py and breeze_join.py, with numpy, scipy, librosa and soundfile.
SERIAL_AUDIO_BREEZE_HOME: the Breeze-TTS-2.cpp folder, with bin/, models/, refs/ and voices/.
SERIAL_AUDIO_PARAKEET_MODEL: the Parakeet TDT 0.6B v2 int8 folder for sherpa-onnx, with encoder.int8.onnx,
    decoder.int8.onnx, joiner.int8.onnx and tokens.txt. The audio Python needs sherpa-onnx to use it.
SERIAL_AUDIO_ASR: the speech recognizer the take check and the casting check use, "qwen3" or "parakeet". Unset means
    parakeet when the Parakeet folder exists, else qwen3.
SERIAL_AUDIO_VAD_MODEL: the Silero VAD v5 folder, with silero_vad_v5.onnx.
SERIAL_AUDIO_SPEAKER_MODEL: the TitaNet small folder, with nemo_en_titanet_small.onnx.
SERIAL_AUDIO_ASR2_MODEL: the Moonshine base English folder, with encoder_model.ort, decoder_model_merged.ort and
    tokens.txt. It is the take check's second recognizer.
SERIAL_AUDIO_PHONE_MODEL: the ZIPA small CR-CTC folder, with model.int8.onnx and tokens.txt.
    Each of these four defaults to the folder of its name beside the Parakeet folder. The take check uses a model
    only when its folder exists, and the audio Python needs sherpa-onnx for them.
SERIAL_AUDIO_MEDIA_MIRROR: the web app's copy of the audio the nodes made, which the app sets for the jobs it runs on
    its own machine. Unset on a node.

Standard library only, because fx.py and breeze_join.py run under the audio Python without the package.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

WINDOWS = os.name == "nt"


def _path(name: str, default: str) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else Path(default)


COMFY_OUTPUT = _path("SERIAL_AUDIO_COMFY_OUTPUT", r"D:\comfy\ComfyUI\output")
AUDIO_PYTHON = _path("SERIAL_AUDIO_AUDIO_PYTHON", r"D:\comfy\python_embeded\python.exe")
BREEZE_HOME = _path("SERIAL_AUDIO_BREEZE_HOME", r"D:\comfy\breeze-cpp")
BREEZE_SERVER_NAME = "breeze-server.exe" if WINDOWS else "breeze-server"
PARAKEET_MODEL = _path("SERIAL_AUDIO_PARAKEET_MODEL", r"D:\comfy\models\parakeet-tdt-0.6b-v2-int8")


def _beside_parakeet(name: str, folder: str) -> Path:
    if os.environ.get(name, "").strip():
        return _path(name, "")
    if os.environ.get("SERIAL_AUDIO_PARAKEET_MODEL", "").strip():
        return PARAKEET_MODEL.parent / folder
    return Path(r"D:\comfy\models") / folder


VAD_MODEL = _beside_parakeet("SERIAL_AUDIO_VAD_MODEL", "silero-vad-v5")
SPEAKER_MODEL = _beside_parakeet("SERIAL_AUDIO_SPEAKER_MODEL", "titanet-small")
ASR2_MODEL = _beside_parakeet("SERIAL_AUDIO_ASR2_MODEL", "moonshine-base-en-2026-02-27")
PHONE_MODEL = _beside_parakeet("SERIAL_AUDIO_PHONE_MODEL", "zipa-small-crctc-300k")
ASR = os.environ.get("SERIAL_AUDIO_ASR", "").strip().lower()
MEDIA_MIRROR_ENV = "SERIAL_AUDIO_MEDIA_MIRROR"
MEDIA_MIRROR = Path(os.environ[MEDIA_MIRROR_ENV].strip()) if os.environ.get(MEDIA_MIRROR_ENV, "").strip() else None


def breeze_server_pids() -> list[int]:
    """The ids of every breeze-server process on this machine. Never raises."""
    try:
        if WINDOWS:
            listed = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {BREEZE_SERVER_NAME}", "/FO", "CSV", "/NH"],
                                    capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout
            rows = [row.split('","') for row in listed.splitlines() if row.startswith(f'"{BREEZE_SERVER_NAME}"')]
            return [int(row[1]) for row in rows if len(row) > 1 and row[1].isdigit()]
        listed = subprocess.run(["pgrep", "-x", BREEZE_SERVER_NAME], capture_output=True, text=True).stdout
        return [int(p) for p in listed.split() if p.isdigit()]
    except (OSError, subprocess.SubprocessError):
        return []
