# serial_audio

Turns serialized fiction into multi-voice audio performances: a character bible that grows chapter by
chapter, designed voices per character, a screenplay per chapter, and a Fish Audio Drama 3 or Breeze
TTS 2 render.

## Layout

```
library/<slug>/serial.yaml               title, source, narration, casting policy
library/<slug>/bible/characters.yaml     id -> character (aliases, facts by chapter, voice brief, voice id)
library/<slug>/bible/pronunciations.yaml term -> respelling
library/<slug>/voices/                   designed voices: brief, clip (wav), encoded Drama 3 voice
library/<slug>/chapters/0001/text.md     cleaned chapter text
library/<slug>/chapters/0001/chapter.yaml status, characters present, pass provenance
library/<slug>/chapters/0001/character_pass.json  raw pass output, for audit
library/<slug>/chapters/0001/author_notes.md      the author's notes around a web chapter (never narrated)
library/<slug>/source_index.yaml         web serials: the chapter index as last seen on the site
prompts/                                  system prompts, one per pass
schemas/                                  JSON schemas the passes must return
```

## Commands

```powershell
python -m serial_audio.cli add-epub <file.epub> --slug <slug>
python -m serial_audio.cli add-royalroad <fiction url> --slug <slug> [--chapters 1|1-5|all|none]
python -m serial_audio.cli fetch <slug> [new|1|1-5|all] [--limit N]   # refresh the index, pull chapters
python -m serial_audio.cli chapters <slug>
python -m serial_audio.cli character-pass <slug> <1|1-5|all> [--backend auto|subscription|api] [--model opus|sonnet|haiku] [--on-limit api|wait|fail]
python -m serial_audio.cli bible <slug> [-v]
python -m serial_audio.cli reset-bible <slug>            # throw the bible away to re-run the pass
python -m serial_audio.cli cast <slug> [--characters a,b] [--candidates 3] [--dry-run]
python -m serial_audio.cli voices <slug> [-v]            # cast voices and their candidates
python -m serial_audio.cli pick-voice <slug> <id> [--seed N]   # after listening: re-file / approve
python -m serial_audio.cli cast <slug> --characters <id> --recast [--seed N] [--instruct "..."] [--redo-brief] [--note "..."]
python -m serial_audio.cli screenplay <slug> <chapters>  # text -> script.json
python -m serial_audio.cli render <slug> <chapters> [--engine drama3|breeze] [--runner cpp|comfyui] [--decode-only]   # script -> audio
python -m serial_audio.cli check-takes <slug> <chapters> [--redo]     # flag Breeze takes that do not say their line
```

## Web app

The library of record and the app live on the containers VM since 2026-11-07, at https://audiogen.grayfam.us
(http://containers.goblin-truck.ts.net:8765 over the tailnet). There the app runs with `--executor nomad` and
dispatches renders, casting and the Claude passes to the Nomad cluster. `deploy/README.md` has the deployment, the
node setup and how a job runs. `D:\fish\library` is a retired copy, so don't run the CLI or a local app against it.

```powershell
python -m serial_audio.web                          # http://127.0.0.1:8765/, every job on this machine
python -m serial_audio.web --host 100.81.138.51     # reachable over Tailscale, e.g. from a phone
```

Run it from `D:\fish`. It shows each serial's chapters on the way to audio and plays rendered chapters,
remembering the listening position per chapter. On the Cast tab you can play every candidate, file or approve
one, recast with a new description, or cast the characters who have no voice yet. The bible and pronunciations
are searchable. The app has no login, so bind it only to an address you trust. It answers only to localhost,
the machine's name, the `--host` address and any `--allow-host` name, and refuses POSTs from other web pages.
A second instance on the same `.webapp/` or port refuses to start.

Every change goes through `python -m serial_audio.cli` as a background job, under `serial_audio/web/runjob.py`,
so a job keeps running when the server stops and is picked up again when it starts. Job records and logs live in
`.webapp/`. Heavy jobs (character pass, screenplay, cast, render) run one at a time, in order, and wait while
any pipeline process the app did not start is running, including a PowerShell script that calls the CLI, or
while ComfyUI is busy with a prompt the app did not queue. A render or cast that queues ComfyUI prompts also waits
for ComfyUI to answer. A Breeze render under `breeze-server` does not, so it runs with ComfyUI down. A render job is
queued with the engine and runner `serial.yaml` names at that moment, as explicit `--engine` and `--runner` flags.
Quick jobs (pick a voice, import a saved page, add a serial) run straight away, except beside a job that rewrites
the bible. A batch from the Process dialog skips its
remaining jobs when one fails. A queued render is skipped when the chapter was rendered after it was queued.
Stopping a running render or cast also cancels its ComfyUI prompt by id. Nothing else in ComfyUI is touched.
Stopping a Breeze render under `breeze-server` stops that server too, and its finished takes stay for the next render.
The chapter page shows the take check, with each flagged or out-of-range take's script line, transcript and a
player, and a Check takes button that runs it as a job.
Every chapter with audio has a download button on the serial page and the chapter page. `GET /download/<slug>/<n>`
sends the file as an attachment named `<serial> - <NNN> - <chapter title>.opus`, so an audiobook reader on a phone
sorts the chapters in order.
The bible page lists the pronunciations with their say-as overrides first. Edit, Override and Add an override queue a
quick `say-as` job, and the table refreshes when it finishes. `say-as <slug> <term> <spoken>` does the same from the
command line, and `--clear` removes an override. A term added only for an override has no respelling, and clearing
its override removes the entry.

## Pipeline per chapter

1. `add-epub` or `add-royalroad`/`fetch` writes `text.md`; status `fetched`. Royal Road pages are read
   with a browser user agent, one request every two seconds; the per-page hidden decoy paragraph and
   ads are dropped, blockquotes (LitRPG system text, letters) become `> ` blocks, and the author's
   notes go to `author_notes.md`.
2. `character-pass` updates the bible with who appears, who is new, voice briefs; status `characters`.
3. `cast` designs a voice for each uncast major/recurring character: a casting pass writes a
   voice-design instruction and sample line, ComfyUI makes N candidates with Qwen3-TTS Voice Design,
   each is transcribed with Qwen3-ASR and encoded as a Drama 3 voice; the best transcript match is
   filed under `voices/` (status `auto` until you listen and `pick-voice`). A third-person serial also
   gets a narrator, cast first as the voice `narrator` from the blurb, tags, and opening of chapter 1
   (set `narrator_brief` in `serial.yaml` to steer it). Recurring characters who have not spoken yet
   are cast once they do.
4. `screenplay` turns the chapter into ordered lines with speakers and sparse directions, verbatim
   apart from speech normalizations; a coverage check guards against dropped text; status `scripted`.
5. `render` maps speakers to Drama 3 slots (narrator = 0, unvoiced speakers read by the narrator),
   generates codes with the cast voices in one ComfyUI run, frees the card, decodes in a second run,
   and encodes the audio as Ogg Opus to `audio/chNNNN.opus`; status `rendered`. Every render writes Opus,
   at 24 kHz from Breeze and resampled to 48 kHz from Drama 3, because Opus takes only a few sample rates.
   With `render_engine: breeze` in `serial.yaml`, or `--engine breeze`, which is the default when
   `serial.yaml` names no engine, `render` instead makes one Breeze TTS 2 take per line. Each take clones the
   speaker's cast clip, and a line with a direction gets it as the instruction. The default runner, `cpp`,
   starts Breeze-TTS-2.cpp's `breeze-server` (Vulkan build in `D:\comfy\breeze-cpp\bin`, Q8_0 weights in
   `D:\comfy\breeze-cpp\models`) on 127.0.0.1:8137 for the render and stops it after. `--runner comfyui`,
   or `breeze_runner: comfyui` in `serial.yaml`, queues the ComfyUI-Breeze-TTS-2 nodes instead, one prompt per
   line. The `cpp` runner refuses to start while any other breeze-server runs, and a take whose length
   does not fit its line is taken again with new seeds, up to twice. `breeze_takes.json` in the chapter folder lists the
   takes, so a rerun renders only new or changed lines and `--decode-only` joins the saved takes again.
   After the join, `render` checks the new takes: Qwen3-ASR in ComfyUI transcribes each one, the words are aligned
   with the script line, and a take with three or more inserted or dropped words is flagged, in `take_check.json`
   beside the script and as `render.check` in `chapter.yaml`. A take with a high word error rate but fewer slips is
   suspect, usually a name the recognizer spelled its own way. With ComfyUI down the check is skipped and recorded
   as such. `check-takes <slug> <chapters>` runs it on its own and reuses the saved transcript of an unchanged take.
   `--redo` transcribes everything again. A take the length guard kept out of range is listed beside the flagged ones.
   `breeze_join.py` joins them under ComfyUI's Python, applies voice effects, and matches the loudness
   of the Drama 3 chapters.

ComfyUI (portable, `D:\comfy`) must be running with the ComfyUI-Drama3 and ComfyUI-Qwen-TTS node
packs. Every ComfyUI step runs as its own prompt with `POST /free` in between: on an 8 GB card the
Drama 3 codec must load alone, or ComfyUI loads it partially and it fails with a device mismatch.
The `cpp` runner uses about 3.8 GB of the card and generates at about 2x realtime. It asks ComfyUI to
unload its models first. The `comfyui` runner needs the ComfyUI-Breeze-TTS-2 node pack and ComfyUI
started with `--highvram`: under the default dynamic VRAM its CUDA graph capture fails and its codec ends
up split across CPU and GPU. It uses about 7.7 GB of the card and generates at about 0.5x realtime.

## LLM backends

`subscription` runs headless Claude Code (`claude -p`) under the claude.ai login, so it draws on
subscription usage; `api` uses the Anthropic SDK and `ANTHROPIC_API_KEY`. `auto` tries the
subscription first and, on a usage-limit message, falls back to the API, waits for the reset, or fails,
per `--on-limit`. Both backends get the same prompt, input, and schema; every chapter records which one ran.
