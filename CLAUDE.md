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
* `docs/take-quality/`: the plan for better checks on each take, in three phases, with the contract they share and where each phase stands. `p4-name-check.md` there has what the name check misses and the steps to refine it.
* `deploy/macos/ggml-metal-left-pad.patch`: the ggml fix the Mac's Metal breeze-server needs.
* `D:\fish.wt\scratch\` on the laptop, outside the repo: the scratch copy of The Hundred Reigns with every take and its evidence, the ten labelled takes and the two clips of the join, for the take quality plan (`docs/take-quality/README.md`, Scratch data). Delete it when phase 3 is deployed.
* `tests/`: unit tests for the cluster, casting and the screening of say-as options, the speech recognizers, the take check with its name rule and the retakes of flagged takes, the backup and the audiobook folders, the parsers and the Claude passes, the Breeze join's click repair and the take measures, and a fake Nomad end-to-end test. `tests/data/name_marks.jsonl` has the sayings AJ marked on the name rule's listening pages, and a test pins the rule's score on them.

## Where things run

* containers, a Linux VM: the web app in Docker under `/opt/serial-audio`, and the library of record in `/opt/serial-audio/library`. It runs quick library jobs and voice picks itself and dispatches the rest. Its `state/media` holds a copy of every Breeze take and cast candidate the nodes made, so a Breeze job runs on either GPU node. It backs the library up once a day to `/mnt/nfs/serial-audio/backup` on storage's NFS share.
* aj-legion5, AJ's Windows laptop with an 8 GB RTX 3070: the fast GPU node, with the classes `breeze` and `comfyui`. ComfyUI, its models and breeze-server are in `D:\comfy`, and `D:\fish` is a dev tree. Its Nomad tasks run as SYSTEM. Only it runs Drama 3 and Qwen jobs, which need ComfyUI.
* jsc-m1, AJ's M1 Mac with 16 GB: a slower `breeze` node, about 0.4x realtime against the laptop's 2x. Its tools are in `/Users/aj/serial-audio`, outside this repo. It has no ComfyUI, so it checks takes and casting candidates with Parakeet.
* dev-arch, a Linux VM: the `llm` node. The Claude passes run aj's install of `claude` there and sign in with AJ's subscription token, which the job reads from the Nomad variable `nomad/jobs/serial-audio-llm`.

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
* Audio processing runs under a separate audio Python with numpy, scipy, librosa and soundfile. That is ComfyUI's embedded Python on the laptop and the venv on the Mac. `fx.py`, `breeze_join.py` and `take_measure.py` run there and import only each other. `parakeet_asr.py` runs there too and imports only `take_measure.py`, inside `main`, because `asr.py` and `takecheck.py` import its constants under the plain Python. `machine.py` uses the standard library only for the same reason.
* A take's evidence is in `take_check.json`, in versioned blocks: `asr` from the recognizer, `signal` from `take_measure.py`, and on a machine that has their models `vad`, `speaker`, `asr2` and `phone` from Silero VAD, TitaNet small, Moonshine and ZIPA. `takecheck.judge()` derives every verdict from the blocks and uses the standard library only, so the app can call it. Bump `ASR_V` or `SIGNAL_V` when a block's content changes, and every saved take is then heard again on its chapter's next render or check. Bump `VAD_V`, `SPEAKER_V`, `ASR2_V` or `PHONE_V` in `parakeet_asr.py` for the other four, and a saved take is then heard for that block alone. Bump `JUDGE_V` when a rule or a threshold changes, and a render then judges the saved evidence again without hearing anything. `takecheck.SIGNAL_V` has to equal `take_measure.SIGNAL_V`, and a test checks that.
* `breeze.JOIN_VERSION` is the version of the join's output, and a render records it as `render.join`. Bump it when a change to `breeze_join.py`, `fx.py` or the join job changes how a chapter sounds, and the `backlog` list then names every chapter for a join. A change to the cuts the take check gives is a change to its rules, so it bumps `JUDGE_V` and not `JOIN_VERSION`. A render records a hash of the cuts its join made as `render.cuts`, and the list names a chapter whose cuts would change.
* `phones.py` has what the `name` rule, the screening of a say-as option and the Names tab read from a take's phones, and it uses the standard library only. `phones.adds()` is the rule: the sounds that both Parakeet's spelling and ZIPA's phones add to the spelling the voice is sent. A name is held to its say-as, so the rule needs no reference clip, and a name without a say-as is not judged. The Names tab sorts sayings with the older distance between phone strings, which is a hint and judges nothing. `said` on an entry of `pronunciations.yaml` is the usual saying of its spelling, for that tab alone. `pronounce.hold()` keeps it, so set a say-as through `pronounce.set_say_as` and not by editing the file. `takecheck.NAME_FLAGS` and `pronounce.SCREENS` switch the flag and the screening off. A rule that judges how something sounds goes live only after AJ has marked a listening page for it (`DECISIONS.md`, the entry on the name rule): the first version passed every test and agreed with his ear on 23 of 40 clips. Bump `pronounce.SCREEN_V` when what a screen counts changes.
* `breeze.voice_text()` builds the text a voice is sent and the line's token map. A change to that text changes take keys, and a render makes every line with a new key again. `VoiceTextTest` in `tests/test_asr.py` pins the keys of a fixed script. `breeze.name_marks()` keeps what `voice_text()` reads of a bible's names, by each term's say-as and each character's name. A change that makes `name_terms()` or `say_as_map()` read another field of the bible has to add the field to what `name_marks()` keeps by.
* A line's `emphasis` holds the words its chapter sets in italics for stress. `voice_text()` puts each between asterisks in the text and leaves the token map as it is without them, so the field changes the line's take key and nothing the judge reads. `screenplay.italic_emphasis()` finds the words without a Claude pass, and a render that makes takes writes the field into a script that doesn't record `screenplay.EMPHASIS_V`. A join-only run doesn't, because its takes were made from the script as it is. `render_changes()` plans with the field in memory, so the `backlog` list counts those lines before any script has them. Bump `EMPHASIS_V` when the rule marks other words, and each chapter's next render then makes the lines whose field changed.
* `serial_audio/machine.py` reads each machine's paths from the environment. Its defaults are the laptop's `D:\comfy` layout, so the laptop sets nothing. A node's `serial_audio_config` JSON sets them for its jobs:
  * `SERIAL_AUDIO_COMFY_OUTPUT`: ComfyUI's output folder. On a node without ComfyUI, the folder for takes and joined audio.
  * `SERIAL_AUDIO_AUDIO_PYTHON`: the audio Python.
  * `SERIAL_AUDIO_BREEZE_HOME`: the Breeze-TTS-2.cpp folder with `bin/`, `models/`, `refs/` and `voices/`.
  * `SERIAL_AUDIO_PARAKEET_MODEL`: the Parakeet TDT 0.6B v2 int8 folder for sherpa-onnx.
  * `SERIAL_AUDIO_ASR`: `qwen3` or `parakeet` for the take check and the casting check. Unset means Parakeet when its folder exists, else Qwen3-ASR.
  * `SERIAL_AUDIO_VAD_MODEL`, `SERIAL_AUDIO_SPEAKER_MODEL`, `SERIAL_AUDIO_ASR2_MODEL` and `SERIAL_AUDIO_PHONE_MODEL`: the folders of the take check's Silero VAD, TitaNet small, Moonshine and ZIPA models. Each defaults to its folder beside the Parakeet folder, so a node that has them there sets nothing. A machine without a folder, or whose folder lacks a file or doesn't load, doesn't gather that block, so the rule that reads it doesn't fire. The tail rule doesn't need any of them. The tests in `tests/test_asr.py` pin the folders off.
* The web app reads library files directly and writes only through `python -m serial_audio.cli` jobs, so every change is logged and a job survives a server restart.
* A take's `file` in `breeze_takes.json` and a candidate's `clip` and `voice_file` in a voice record are absolute paths on the node that last worked on them. On any other machine the file is the app's copy at `state/media/<exchange.mirror_rel(path)>`. The app sets `SERIAL_AUDIO_MEDIA_MIRROR` to that folder for the jobs it runs itself, and a worker fetches a chapter's takes from it before its steps (`deploy/README.md`, The media mirror). A new kind of file that a later job needs from a node has to go to the mirror too, in `worker.media_files`.
* A hook rejects an en dash or an em dash anywhere. It also rejects a semicolon on any line that starts like a comment (`#`, `//`, `/*`, `--`, `* `), which includes Markdown bullets and headings. A CSS custom property on a line of its own starts with `--`, so put custom properties on one-line rules that start with the selector. Test data that needs a dash has to hold it as a Python escape, and a `\u` escape typed into an Edit or a Bash command arrives as the character itself. Write the escape from a script that builds it with `chr(92) + "u2014"`.

## Testing

* Run `python -m unittest tests.test_cluster tests.test_casting tests.test_asr tests.test_mirror tests.test_text` and `python tests/e2e_fake_nomad.py` after any change. The end-to-end test covers the cluster exchange and scheduling.
* After a change to `fx.py`, `breeze_join.py` or `take_measure.py`, also run `tests/test_fx.py` under the audio Python, with `D:\comfy\python_embeded\python.exe -I tests/test_fx.py` on the laptop. The plain Python skips it.
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

  Then run the step you changed against that copy. On the laptop the plain `python` needs no node settings, so only export `SERIAL_AUDIO_COMFY_OUTPUT`.

* A scratch render or check of a rendered chapter needs its own copies of the takes, because a retake replaces a take's file in place. Every take is in the app's mirror, `/opt/serial-audio/state/media` on containers, under its path on the node that made it. Copy the chapter's takes into the scratch copy and point each `file` in its `breeze_takes.json` at the copy before the first run.

* Take phone-width screenshots of the web app over the Chrome DevTools Protocol. Start headless Chrome or Edge with `--remote-debugging-port`, then send `Emulation.setDeviceMetricsOverride` at 390 by 844. A headless window doesn't go narrower than about 477 px on its own. Node 24 has a global WebSocket for the protocol. `Page.navigate` to the page that is already open doesn't load it again, so send `Page.reload` with `ignoreCache` after a change to a static file.

## Gotchas

* Royal Road has answered the pipeline with HTTP 403 since 2026-11-05, and probing it counts as getting around a bot check. Don't try. Ingest a chapter from the page AJ saves in his browser, with `fetch <slug> <n> --html <file>` or an upload in the app.
* The subscription backend runs `claude -p` with `--json-schema`, which needs `--max-turns` of 3 or more because the answer arrives as a tool call. The child's environment must not hold `ANTHROPIC_API_KEY`, because it outranks the token and the login.
* On macOS and Linux, a process started in the background from a non-interactive shell ignores SIGINT. Stop a CLI render with SIGTERM to its process group. breeze-server stays in that group, so it stops too. On Windows the CLI ties breeze-server to itself with a job object.
* Over a non-interactive ssh session the Mac's PATH lacks `/opt/homebrew/bin`, where nomad, cmake and the Homebrew Pythons live. Export it first.
* zsh doesn't word-split a variable that holds `env A=1 B=2`. Export the settings instead.
* On the laptop, every ComfyUI step is its own prompt with `POST /free` between them, because the Drama 3 codec fails on 8 GB unless it loads alone. Only the ComfyUI Breeze runner needs ComfyUI started with `--highvram`.
* ComfyUI loads node pack changes only on a restart. The auto-mode classifier blocks Claude from stopping AJ's own ComfyUI, so ask him. An instance that a Claude session started itself can be restarted.
* Connecting to ComfyUI's `/ws` with another client's clientId steals that client's socket. Read progress from the logs instead.
* While ComfyUI renders, the laptop's RAM is nearly full, and Claude Code's low-memory reaper kills background Bash commands. Launch long work detached with PowerShell `Start-Process`, and watch its log with a Monitor tail through `grep -a --line-buffered`, not `tr`.
* On the laptop a cluster worker waits while a `python -m serial_audio.cli` render, cast, check-takes or Claude pass that it did not start is running, because it counts each as GPU work. A scratch `check-takes` therefore holds every GPU job on the laptop until it ends, though Parakeet doesn't use the card. For a long scratch check, call `takecheck.check_takes` from a script of your own, or use `measure-takes`, which hears with Parakeet only and which the worker doesn't wait for.
* Windows PowerShell 5.1's `*>>` appends UTF-16 to a UTF-8 log. Use pwsh.
* On the laptop, `tests/e2e_fake_nomad.py` runs its fake node's worker on this machine, and that worker waits for GPU work while a real breeze-server runs. So while a chapter renders there, the test can fail at its backlog phase or at cleanup, with a PermissionError on the job's log, and it leaves a `python -u local/bootstrap.py` worker running. Run it when nothing renders on the laptop, and stop a leftover worker of your own run.
* On the laptop, a doubled backslash in a heredoc of a Bash tool command arrives as one, also when the heredoc's word is quoted. A Python string with a Windows path in such a heredoc then reads the path's `\f` as a form feed and its `\t` as a tab, and two docs carried those characters for an hour on 2026-11-12. Write a script that has backslashes to a file and run the file. A quoted heredoc was also cut off at a triple quote on 2026-11-13, and the command then failed on the half-written script.
* ZIPA writes the dictionary's reading of a real word, whatever the take says: "lived" as "lyved", "wind" as "wynd", "lead" as "led". Its phones are right for plain words and can't judge a word whose spelling has two readings (`DECISIONS.md`, "A heteronym gets no respelling and no check"). A name with an invented spelling doesn't have a dictionary reading.
* The `Scripts\*.exe` launchers in ComfyUI's embedded Python, such as `hf.exe`, are broken. Download models with the `huggingface_hub` API from a script. Dry-run a pip change to the embedded Python first.
* Git on the laptop has `core.autocrlf` true in its system config, so 19 of the deployed files had CRLF in the working tree on 2026-11-11, and a plain `tar` of the tree sends them to containers that way. A `git archive` does too unless `core.autocrlf` is off for that command. Deploy with the command in `deploy/README.md`, which sends LF. To check what is deployed, strip `\r` before comparing hashes.
* A tar made on the Mac adds `._*` AppleDouble files unless `COPYFILE_DISABLE=1` is set. `/opt/serial-audio/src` held 56 of them on 2026-11-11. `exchange.code_files` leaves `._*` files out of a worker bundle. The `git archive` deploy doesn't add any.

## Updating

A session that learns something lasting records it in the same session. Put a convention or a gotcha here, a
decision in `DECISIONS.md`, a machine fact in `docs/machines.md` and a casting lesson in `docs/casting.md`. Facts about
the hosts themselves belong in `~/projects/homelab`. Keep each file to its current state, and date any snapshot of
live state.
