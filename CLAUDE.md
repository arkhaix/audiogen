# audiogen

audiogen, the Python package `serial_audio`, turns web serials and EPUBs into multi-voice audio performances that
AJ listens to while driving or falling asleep. A Claude pass grows a character bible chapter by chapter, each
speaking character is cast a designed voice, a second Claude pass writes each chapter as a screenplay, and Breeze
TTS 2 renders it to Ogg Opus. A web app at https://audiogen.grayfam.us queues the work and dispatches it to the
home Nomad cluster.

## Files

* `README.md`: the library layout, every CLI command, the web app, the pipeline per chapter and the LLM backends.
* `deploy/README.md`: the app on containers, deploying a change, the Nomad jobs, node setup and how a cluster job runs.
* `DECISIONS.md`: why things are the way they are, and what AJ hasn't decided yet.
* `docs/machines.md`: what each machine holds and how to rebuild it.
* `docs/casting.md`: how to write a voice brief that works, the cast so far and the voices still to cast.
* `deploy/macos/ggml-metal-left-pad.patch`: the ggml fix the Mac's Metal breeze-server needs.
* `tests/`: the cluster tests. The pipeline itself has none.

## Where things run

* containers, a Linux VM: the web app in Docker under `/opt/serial-audio`, and the library of record in `/opt/serial-audio/library`. It runs quick library jobs itself and dispatches the rest.
* aj-legion5, AJ's Windows laptop with an 8 GB RTX 3070: the fast GPU node, with the classes `breeze` and `comfyui`. ComfyUI, its models and breeze-server are in `D:\comfy`, and `D:\fish` is a dev tree. Its Nomad tasks run as SYSTEM.
* jsc-m1, AJ's M1 Mac with 16 GB: a slower `breeze` node, about 0.4x realtime against the laptop's 2x. Its tools are in `/Users/aj/serial-audio`, outside this repo. It has no ComfyUI, so it checks takes and casting candidates with Parakeet.
* dev-arch, a Linux VM: the `llm` node, because the Claude passes need AJ's claude login, which lives there.

Reach them by tailnet name: `ssh aj@containers`, `ssh aj@dev-arch`, `ssh jsc-m1`. From the Mac, `ssh
aj-legion5-tailscale` reaches the laptop as an elevated user. Facts about the hosts themselves are in
`~/projects/homelab` on the Mac.

Workers run the code the app sends with each job, not the code in a clone. A change reaches the cluster only when it
is deployed to containers.

## Rules

* Treat the library on containers as the only copy. Change it only through the app's jobs. The repo ignores `library/` and `incoming/`, so a clone has no library. `D:\fish\library` on the laptop is a copy retired on 2026-11-07, so don't run the CLI or a local app against it.
* Never test on AJ's chapters or voices. A re-render replaces a chapter's audio, and a cast files a voice. Test the pipeline in a scratch copy (Testing, below), the web app as a second instance on another port with its own `--state-dir`, and the cluster with a throwaway serial that you delete afterwards.
* Don't commit or push. AJ commits. Draft the commit message when he asks.
* Change a host only when AJ asks for that change directly in the conversation. That covers Nomad client configs, services, Homebrew and pip installs, and ComfyUI's embedded Python. The Nomad client configs live in `~/projects/ansible`, and host facts go to `~/projects/homelab`. Registering Nomad jobs, policies or tokens with the management token needs AJ's approval in the conversation.
* Never print a token. Tokens live in the aj+claude vaultwarden account, and `bw` works on the Mac.
* Run one GPU job per machine. Don't start anything that takes VRAM on the laptop while a chapter generates. Pressure on the card slows the rest of that generation for good.
* After a change to the app or the pipeline, deploy it to containers (`deploy/README.md`). The nodes need no update. A change to the Nomad job spec or to `deploy/nomad/bootstrap.py` also needs the jobs registered again.
* Several Claude sessions often work in this repo at once. Before you edit a shared module, ask the others which files they hold. Read a file right before each edit, and keep edits to small string replacements.

## Code

* The web app in `serial_audio/web/` uses the standard library only. The containers image adds only pyyaml, and the laptop's user Python has no numpy.
* Audio processing runs under a separate audio Python with numpy, scipy, librosa and soundfile. That is ComfyUI's embedded Python on the laptop and the venv on the Mac. `fx.py` and `breeze_join.py` run there and import only each other. `machine.py` uses the standard library only for the same reason.
* `serial_audio/machine.py` reads each machine's paths from the environment. Its defaults are the laptop's `D:\comfy` layout, so the laptop sets nothing. A node's `serial_audio_config` JSON sets them for its jobs:
  * `SERIAL_AUDIO_COMFY_OUTPUT`: ComfyUI's output folder. On a node without ComfyUI, the folder for takes and joined audio.
  * `SERIAL_AUDIO_AUDIO_PYTHON`: the audio Python.
  * `SERIAL_AUDIO_BREEZE_HOME`: the Breeze-TTS-2.cpp folder with `bin/`, `models/`, `refs/` and `voices/`.
  * `SERIAL_AUDIO_PARAKEET_MODEL`: the Parakeet TDT 0.6B v2 int8 folder for sherpa-onnx.
  * `SERIAL_AUDIO_ASR`: `qwen3` or `parakeet` for the take check and the casting check. Unset means Parakeet when its folder exists, else Qwen3-ASR.
* The web app reads library files directly and writes only through `python -m serial_audio.cli` jobs, so every change is logged and a job survives a server restart.
* A hook rejects an en dash or an em dash anywhere. It also rejects a semicolon on any line that starts like a comment (`#`, `//`, `/*`, `--`, `* `), which includes Markdown bullets and headings. A CSS custom property on a line of its own starts with `--`, so put custom properties on one-line rules that start with the selector.

## Testing

* `python -m unittest tests.test_cluster` and `python tests/e2e_fake_nomad.py` cover the cluster exchange and scheduling.
* Check a pipeline change in a scratch copy with the layout a worker gets: the code, and one serial's folder without its audio, copied from containers. Point the output folder at the scratch copy too, so its takes can't land beside the node's real ones. On the Mac, from the clone:

```
S=/Users/aj/serial-audio/scratch/try
PY=/Users/aj/serial-audio/venv/bin/python
mkdir -p "$S"
tar cf - --exclude=__pycache__ serial_audio prompts schemas | tar xf - -C "$S"
ssh aj@containers 'cd /opt/serial-audio && tar cf - --exclude=audio library/twenty-thousand-leagues' | tar xf - -C "$S"
cd "$S"
eval "$("$PY" -c 'import json, shlex; print("\n".join(f"export {k}={shlex.quote(v)}" for k, v in json.load(open("/Users/aj/serial-audio/node.json"))["env"].items()))')"
export SERIAL_AUDIO_COMFY_OUTPUT="$S/output"
"$PY" -m serial_audio.cli chapters twenty-thousand-leagues
```

  Then run the step you changed against that copy.

* Take phone-width screenshots of the web app over the Chrome DevTools Protocol. Start headless Chrome or Edge with `--remote-debugging-port`, then send `Emulation.setDeviceMetricsOverride` at 390 by 844. A headless window doesn't go narrower than about 477 px on its own. Node 24 has a global WebSocket for the protocol.

## Gotchas

* Royal Road has answered the pipeline with HTTP 403 since 2026-11-05, and probing it counts as getting around a bot check. Don't try. Ingest a chapter from the page AJ saves in his browser, with `fetch <slug> <n> --html <file>` or an upload in the app.
* The subscription backend runs `claude -p` with `--json-schema`, which needs `--max-turns` of 3 or more because the answer arrives as a tool call. The child's environment must not hold `ANTHROPIC_API_KEY`, because it outranks the login.
* On macOS and Linux, a process started in the background from a non-interactive shell ignores SIGINT. Stop a CLI render with SIGTERM to its process group. breeze-server stays in that group, so it stops too. On Windows the CLI ties breeze-server to itself with a job object.
* Over a non-interactive ssh session the Mac's PATH lacks `/opt/homebrew/bin`, where nomad, cmake and the Homebrew Pythons live. Export it first.
* zsh doesn't word-split a variable that holds `env A=1 B=2`. Export the settings instead.
* On the laptop, every ComfyUI step is its own prompt with `POST /free` between them, because the Drama 3 codec fails on 8 GB unless it loads alone. Only the ComfyUI Breeze runner needs ComfyUI started with `--highvram`.
* ComfyUI loads node pack changes only on a restart. The auto-mode classifier blocks Claude from stopping AJ's own ComfyUI, so ask him. An instance that a Claude session started itself can be restarted.
* Connecting to ComfyUI's `/ws` with another client's clientId steals that client's socket. Read progress from the logs instead.
* While ComfyUI renders, the laptop's RAM is nearly full, and Claude Code's low-memory reaper kills background Bash commands. Launch long work detached with PowerShell `Start-Process`, and watch its log with a Monitor tail through `grep -a --line-buffered`, not `tr`.
* Windows PowerShell 5.1's `*>>` appends UTF-16 to a UTF-8 log. Use pwsh.
* The `Scripts\*.exe` launchers in ComfyUI's embedded Python, such as `hf.exe`, are broken. Download models with the `huggingface_hub` API from a script. Dry-run a pip change to the embedded Python first.

## Updating

A session that learns something lasting records it in the same session. Put a convention or a gotcha here, a
decision in `DECISIONS.md`, a machine fact in `docs/machines.md` and a casting lesson in `docs/casting.md`. Facts about
the hosts themselves belong in `~/projects/homelab`. Keep each file to its current state, and date any snapshot of
live state.
