# Machines

What each machine holds for the pipeline and how to rebuild it. `deploy/README.md` covers the app on containers,
the Nomad jobs and the node meta. Facts about the hosts themselves are in `~/projects/homelab` on the Mac.

---

## aj-legion5, the Windows laptop

The fast GPU node: an RTX 3070 Laptop with 8 GB, 64 GB of RAM, Windows 11. It offers `breeze` and `comfyui`.

### ComfyUI

Portable ComfyUI 0.41 in `D:\comfy`, with an embedded Python 3.13 (`D:\comfy\python_embeded\python.exe`) and torch
2.14 for CUDA 13.0. That Python is also the pipeline's audio Python on the laptop. Node packs in
`D:\comfy\ComfyUI\custom_nodes`, as of 2026-11-08:

* ComfyUI-Drama3 8cb3c64: Drama 3 generation and the codec. Models `drama3_int8experts_bf16` in `models/diffusion_models`, `drama3_codec` in `models/vae`, and `qwen3_asr_1.7b_bf16` in `models/asr` for the transcript checks.
* ComfyUI-Qwen-TTS a1328c8: Qwen3-TTS-12Hz-1.7B-VoiceDesign and its tokenizer in `models/qwen-tts`, for casting.
* ComfyUI-Breeze-TTS-2 53d899b (v1.4.7): the `comfyui` Breeze runner. Weights in `models/breezetts2/drbaph_Breeze-TTS-2-comfyui`.

The pipeline and the cluster worker talk to ComfyUI on 127.0.0.1:8188. AJ's own launcher (`comfy-launch.ps1`, then
`comfy-awake.ps1`, then `comfy-serve.bat`) listens only on the tailnet address, so the pipeline needs its own
instance:

```
cd D:\comfy
python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --disable-auto-launch --port 8188
```

Add `--highvram` only for the `comfyui` Breeze runner. Run `--quick-test-for-ci --cpu` on a spare port to check that
a node pack loads without disturbing a running ComfyUI.

### breeze-server

`D:\comfy\breeze-cpp` holds `src` (Breeze-TTS-2.cpp at tag v0.1.6), `bin\breeze-server.exe` with its DLLs, copied from
the Vulkan build, and `models\breeze-tts-2-q8_0.gguf`. The server uses about 3.8 GB of VRAM. Build it with Visual
Studio 2022, Ninja and the Vulkan SDK 1.4.363.0 (`C:\VulkanSDK\1.4.363.0`):

```
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
set "PATH=C:\VulkanSDK\1.4.363.0\Bin;%PATH%"
cd /d D:\comfy\breeze-cpp\src
cmake -B build-vulkan -G Ninja -DCMAKE_BUILD_TYPE=Release -DBREEZE_VULKAN=ON -DBREEZE_CUDA=OFF -DBREEZE_MP3=OFF
cmake --build build-vulkan -j 12
```

The weights are `breeze-tts-2-q8_0.gguf` from the Hugging Face repo HoppouAI/Breeze-TTS-2.cpp, revision 81b22bad:
3,568,844,480 bytes, sha256 a02bcc4b69b0601032727f8040c4942149b1b73aa0f69022fe5aaa6a8f0ef879.

### Nomad

The client config is `C:\nomad\config\client.hcl`, with a copy in the ansible repo at `nomad/aj-legion5/client.hcl`.
It runs as the Windows service `Nomad` under LocalSystem, with data in `D:\nomad\data` and the log in
`C:\nomad\logs\nomad.log`. Starting or restarting the service needs elevation: from the Mac, `ssh
aj-legion5-tailscale`, then `pwsh -NoProfile -Command "Restart-Service Nomad"`. The client takes about 30 s to
fingerprint before the node shows ready.

### Parakeet

Since 2026-11-08 the laptop checks takes and casting candidates with Parakeet, so its checks no longer need ComfyUI.
sherpa-onnx 2.0.0 and sherpa-onnx-core 2.0.0 are pip-installed in the embedded Python. The model is in
`D:\comfy\models\parakeet-tdt-0.6b-v2-int8`, the default `SERIAL_AUDIO_PARAKEET_MODEL`, so the node config sets
nothing. It was copied from the Mac, and its sha256 hashes match:

* `encoder.int8.onnx`: a32b12d17bbbc309d0686fbbcc2987b5e9b8333a7da83fa6b089f0a2acd651ab
* `decoder.int8.onnx`: b6bb64963457237b900e496ee9994b59294526439fbcc1fecf705b31a15c6b4e
* `joiner.int8.onnx`: 7946164367946e7f9f29a122407c3252b680dbae9a51343eb2488d057c3c43d2
* `tokens.txt`: ec182b70dd42113aff6c5372c75cac58c952443eb22322f57bbd7f53977d497d

To check that it runs, hear a few takes with `serial_audio.asr.records(files, engine="parakeet",
comfy_url="")`. Three takes took 4.8 s, including the model load.

---

## jsc-m1, the Mac

An M1 with 8 cores and 16 GB, macOS 26. It offers `breeze` only. Everything the worker uses is in
`/Users/aj/serial-audio`:

* `breeze-cpp/bin/breeze-server`: a static Metal build. It compiles its Metal shaders at startup, which takes about 40 s.
* `breeze-cpp/src`: Breeze-TTS-2.cpp at tag v0.1.6, unpatched.
* `breeze-cpp/src-metal`: a copy of `src` with the ggml patch applied, which the server is built from.
* `breeze-cpp/models/breeze-tts-2-q8_0.gguf`: the same weights as the laptop's.
* `venv`: Homebrew Python 3.12 with pyyaml, numpy, scipy, librosa, soundfile and sherpa-onnx. It is the node's `serial_audio_python` and its audio Python.
* `models/parakeet-tdt-0.6b-v2-int8`: the Parakeet model, cloned from `~/projects/meetings/models`.
* `output/`: takes and joined audio.
* `node.json`: the node's `serial_audio_config`.

`node.json` holds:

```
{
  "env": {
    "SERIAL_AUDIO_COMFY_OUTPUT": "/Users/aj/serial-audio/output",
    "SERIAL_AUDIO_AUDIO_PYTHON": "/Users/aj/serial-audio/venv/bin/python",
    "SERIAL_AUDIO_BREEZE_HOME": "/Users/aj/serial-audio/breeze-cpp",
    "SERIAL_AUDIO_PARAKEET_MODEL": "/Users/aj/serial-audio/models/parakeet-tdt-0.6b-v2-int8"
  },
  "comfy_url": "http://127.0.0.1:8188"
}
```

Nothing answers on that ComfyUI address on the Mac. Every path is absolute, because Nomad tasks get no HOME.

### Rebuild breeze-server

At the pinned ggml, the Metal backend rejects the left padding in Breeze's vocoder, so a stock Metal build aborts at
warm-up with "unsupported op 'PAD'". `deploy/macos/ggml-metal-left-pad.patch` is upstream ggml commit 46fc5b3b,
rebased onto the pin. Check that the clone is at commit 87b43667 with tree d04e01e9, and that the submodules are at
ggml 36da5713 and shine ab5e3526, before you build:

```
export PATH=/opt/homebrew/bin:$PATH
cd /Users/aj/serial-audio/breeze-cpp
git clone https://github.com/HoppouAI/Breeze-TTS-2.cpp src
git -C src checkout v0.1.6
git -C src submodule update --init
rsync -a --exclude 'build-*' src/ src-metal/
git -C src-metal/third_party/ggml apply <clone>/deploy/macos/ggml-metal-left-pad.patch
cd src-metal
cmake -S . -B build-metal-static -G Ninja -DCMAKE_BUILD_TYPE=Release -DBREEZE_VULKAN=OFF -DBREEZE_CUDA=OFF \
  -DGGML_METAL=ON -DBUILD_SHARED_LIBS=OFF -DBREEZE_BUILD_SHARED=OFF -DBREEZE_BUILD_CLI=OFF
cmake --build build-metal-static
install -m 755 build-metal-static/breeze-server ../bin/breeze-server
```

The build takes about 15 s. On 2026-11-07, plain lines ran at 0.39x realtime and directed lines at 0.31x.

### Nomad

The client config is `/opt/homebrew/etc/nomad.d/client.hcl`, a copy of `nomad/jsc-m1/client.hcl` in the ansible
repo. Its meta block offers the class:

```
serial_audio        = "breeze"
serial_audio_python = "/Users/aj/serial-audio/venv/bin/python"
serial_audio_config = "/Users/aj/serial-audio/node.json"
```

After a change, copy the ansible file over the live one and run `launchctl kickstart -k
gui/$(id -u)/net.ajgray.nomad`. The agent runs as aj under that LaunchAgent and listens on the tailnet address, not
on 127.0.0.1, so query it with `NOMAD_ADDR=http://containers.goblin-truck.ts.net:4646`.

---

## The take check's other models

Phase 2 of the take quality plan (`docs/take-quality/phase-2.md`) adds four sherpa-onnx models beside Parakeet on
each breeze node. AJ approved the list on 2026-11-12, and that evening the four folders were copied to both nodes
from the copies below, every file checked against these hashes, and each node's take check loaded all three blocks of that day. The code reads a folder only
when it holds all its files and its model loads. A node without them gathers Parakeet's two blocks as before, and the
rules that need the other blocks don't fire there. The tail rule and the cut of a tail need only those two blocks,
so they work without the models.

| folder | files | source | licence |
|---|---|---|---|
| `silero-vad-v5` | `silero_vad_v5.onnx`, 2,313,101 bytes, sha256 6b99cbfd39246b6706f98ec13c7c50c6b299181f2474fa05cbc8046acc274396 | https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad_v5.onnx | MIT |
| `titanet-small` | `nemo_en_titanet_small.onnx`, 40,257,283 bytes, sha256 ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e | https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx | the NeMo toolkit's, Apache 2.0 |
| `moonshine-base-en-2026-02-27` | `encoder_model.ort`, 31,326,816 bytes, sha256 7c66495948d0d08ec1af454cd4b5514862ae6511e94712a60e6d83eaec8dc8cf. `decoder_model_merged.ort`, 109,424,400 bytes, sha256 d9d7b333af34bc552580576ddcf248a1c6c839e0d3b43b09afb9376ed009899d. `tokens.txt`, 549,350 bytes, sha256 2870d843e14c1e187bf1913a521562a63b53933814bd7f2145120468f494a049 | `sherpa-onnx-moonshine-base-en-quantized-2026-02-27.tar.bz2` in https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models | MIT, for the English models |
| `zipa-small-crctc-300k` | `model.int8.onnx`, 70,677,672 bytes, sha256 8f0505173e4606b4afe041f19477b38d6a72a98a19863562749066dc496e86ae. `tokens.txt`, 769 bytes, sha256 f8e042a0c9130532b22d03ec7cae2f75a23fbec70c450c31a8efb51787b2b8fe | https://huggingface.co/anyspeech/zipa-small-crctc-300k, revision 2f458a4d | CC BY 4.0 |

* The four folders hold 255 MB. On the laptop they go in `D:\comfy\models`, beside `parakeet-tdt-0.6b-v2-int8`. On
  the Mac they go in `/Users/aj/serial-audio/models`, beside its Parakeet folder.
* `machine.py` defaults each folder to its name beside the Parakeet folder, so neither node's `serial_audio_config`
  needs a line, and no Nomad client config changes. `SERIAL_AUDIO_VAD_MODEL`, `SERIAL_AUDIO_SPEAKER_MODEL`,
  `SERIAL_AUDIO_ASR2_MODEL` and `SERIAL_AUDIO_PHONE_MODEL` move one.
* No package changes. sherpa-onnx 2.0.0 is in both audio Pythons and loads all four. ZIPA goes through its
  Zipformer CTC loader, so neither node needs torch or lhotse.
* A copy of each folder, with the hashes above, is in `D:\fish.wt\scratch\models` on the laptop, and the files the
  spike used are in `/Users/aj/serial-audio/scratch/spike/models` on the Mac. Installing them is a copy and a hash
  check.
* ZIPA gives the `phone` block since package P4 of 2026-11-13. The take check's name rule, the screening of a
  say-as option and the Names tab read it. On the laptop it decoded 826 takes, 7,930 s of audio, in 137 s.
* Once a node has the folders, the first render or check of each chapter there hears the chapter's saved takes for
  the new blocks, at about 0.5 s a take on the laptop's CPU, or about 2 minutes for a chapter of 220 takes.
  aj-legion5 hard-reset three times on 2026-11-12, and two of those came while a scratch gathering of these blocks
  ran on its CPU. With the folders on the Mac first, the chapters the Mac checks get the blocks there, and the
  laptop hears only the rest once it has them.

---

## containers and dev-arch

`deploy/README.md` covers both: the app's folder, deploys, logs and the dispatch token on containers, and dev-arch's
dynamic node meta and the Claude token its jobs sign in with.
