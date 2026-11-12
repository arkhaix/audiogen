# serial_audio

Turns serialized fiction into multi-voice audio performances: a character bible that grows chapter by
chapter, designed voices per character, a screenplay per chapter, and a Fish Audio Drama 3 or Breeze
TTS 2 render.

## Layout

```
library/<slug>/serial.yaml               title, source, narration, casting policy
library/<slug>/bible/characters.yaml     id -> character (aliases, facts by chapter, voice brief, casting bio, voice id)
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
python -m serial_audio.cli cast <slug> [--characters a,b] [--candidates 3] [--dry-run] [--engine qwen|breeze] [--asr qwen3|parakeet]
python -m serial_audio.cli bios <slug> [--characters a,b] [--redo]   # casting bios: who each voiced character is, without the plot
python -m serial_audio.cli voices <slug> [-v]            # cast voices and their candidates
python -m serial_audio.cli pick-voice <slug> <id> [--seed N]   # after listening: re-file / approve
python -m serial_audio.cli cast <slug> --characters <id> --recast [--seed N] [--instruct "..."] [--redo-brief] [--note "..."]
python -m serial_audio.cli voice-effect <slug> <voice> whisper [--level 0.35] | --clear   # effect on one voice in later renders
python -m serial_audio.cli screenplay <slug> <chapters>  # text -> script.json
python -m serial_audio.cli render <slug> <chapters> [--engine drama3|breeze] [--runner cpp|comfyui] [--decode-only]   # script -> audio
python -m serial_audio.cli check-takes <slug> <chapters> [--asr qwen3|parakeet] [--redo]   # flag Breeze takes that do not say their line
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

Run a local instance from a copy of the code that has its own `library/`, with its own `--state-dir`. The app shows
each serial's chapters on the way to audio and plays rendered chapters,
remembering the listening position per chapter. On the Cast tab you can play every candidate, file or approve
one, recast with a new description, or cast the characters who have no voice yet. Each voice card shows the
character's casting bio: age, race, station, temperament and habits of speech, without the plot. Write bios queues a
`bios` job for the voices that don't have one, and Rewrite bio on a card writes that one again from the bible as it
stands. The bible and pronunciations are searchable. The app has no login, so bind it only to an address you trust. It answers only to localhost,
the machine's name, the `--host` address and any `--allow-host` name, and refuses POSTs from other web pages.
A second instance on the same `.webapp/` or port refuses to start.

With `--backup-dir <folder>` the app backs the library up once a day. It writes `snapshots/library-<time>.tar.gz`
with every library file but the audio, the uploads in `incoming/` and `placement.json`, and keeps the newest 30. It
copies new or changed audio to `library/<slug>/audio/` and never deletes an audio copy. The folder must hold a file named
`.serial-audio-backup`, so an unmounted disk doesn't get a backup. The header says "backup failing" when the last try
failed, and its tooltip gives the time of the last backup. `deploy/README.md` has the restore steps.

On a browser that can write to a folder (one with `showDirectoryPicker`), the serial page has a Save to this device
button. The first time, it asks for a folder, such as the root folder of Smart AudioBook Player. It writes every
rendered chapter that the device doesn't have yet to `<serial title>/<NNN> - <chapter title>.opus` there. A chapter you
delete after listening is written again only after a new render. The browser remembers the folder and the saved
chapters, so another browser, or the same one after its site data is cleared, starts over.

With `--publish-dir <folder>` the app copies every rendered chapter to `<serial title>/<NNN> - <chapter title>.opus`
in that folder within five minutes of its render, for an audiobook player that reads one folder per book. A chapter
is copied again only after it is rendered again, so a copy you delete after listening stays deleted. The app doesn't
write into a folder it did not make, and the `--publish-dir` folder must already hold something, which shows that its
disk is mounted. `--publish-skip <slug>` keeps a serial out. The header says "audiobook folders failing" when a pass failed.

Every change goes through `python -m serial_audio.cli` as a background job, under `serial_audio/web/runjob.py`,
so a job keeps running when the server stops and is picked up again when it starts. Job records and logs live in
`.webapp/`. The rest of this paragraph is the one-machine executor (`--executor local`), and `deploy/README.md` has
the cluster's rules, with the media mirror that lets a Breeze job run on either GPU node. Heavy jobs (character pass, screenplay, cast, casting bios, render) run one at a time, in order, and wait while
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
On a Royal Road serial, Import saved pages has a box named process the chapters after import. With it ticked, the
import queues one batch: the imports, then each imported chapter's character pass, a cast of new voices, its
screenplay and its render, with the Process dialog's defaults and the LLM settings last chosen in a dialog. Only a
chapter that nothing has been made from yet is processed: a new chapter, or one on disk at status `fetched` whose text
is replaced. A replaced chapter that has a character pass, a script or audio from its old text is left for the Process
dialog and its redo boxes. The browser remembers the box.
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
3. `cast` designs a voice for each uncast major/recurring character. A casting pass writes a
   voice-design instruction and sample line, and breeze-server designs N candidates from it with Breeze TTS 2
   voice design. Each is transcribed with the same recognizer as the take check (step 5), and the best
   transcript match that isn't clipped is filed under `voices/` (status `auto` until you listen and `pick-voice`). A Breeze
   voice has no Drama 3 voice file, so it serves only Breeze renders. A third-person serial also
   gets a narrator, cast first as the voice `narrator` from the blurb, tags, and opening of chapter 1
   (set `narrator_brief` in `serial.yaml` to steer it). Recurring characters who have not spoken yet
   are cast once they do. With `cast_engine: qwen` in `serial.yaml`, or `--engine qwen`, ComfyUI makes the
   candidates with Qwen3-TTS Voice Design instead and encodes each as a Drama 3 voice. Beside each brief the
   casting pass writes, a bio pass writes the character's casting bio into the bible under `bio`, from the whole
   record with its facts: who the character is, in 50 words at most, for the listener who judges the voice. It
   leaves out events and what a character conceals. `bios` writes the bios of voices cast before, or again with
   `--redo` after later chapters changed the record.
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
   Before the join, when the recognizer is Parakeet, `render` transcribes the new takes while breeze-server is still
   up. It takes a flagged line again with new seeds, in up to two rounds, and keeps a new take only when its transcript is
   closer to the line. `breeze_takes.json` marks a replaced take with `retaken`, and `render.check` lists those lines.
   A saved take that an earlier check flagged goes through the same check and rounds, and it stays unless a new take
   is closer to the line. `retake_rounds` on a take records the last round its line went through, so a line that
   stays flagged after every round is left alone.
   After the join, `render` checks the new takes, and reuses the transcripts made before the join: a speech recognizer transcribes each one, the words are aligned
   with the script line, and a take with three or more inserted or dropped words is flagged, in `take_check.json`
   beside the script and as `render.check` in `chapter.yaml`. A take with a high word error rate but fewer slips is
   suspect, usually a name the recognizer spelled its own way. When the recognizer can't run, the check is skipped and
   recorded as such. `check-takes <slug> <chapters>` runs it on its own and reuses the saved transcript of an unchanged take.
   `--redo` transcribes everything again. A take the length guard kept out of range is listed beside the flagged ones.
   The recognizer is Parakeet TDT 0.6B v2 through sherpa-onnx when its model folder exists
   (`SERIAL_AUDIO_PARAKEET_MODEL`), and Qwen3-ASR in ComfyUI otherwise. `--asr` or `SERIAL_AUDIO_ASR` picks one.
   `docs/machines.md` has the Parakeet install on the Mac and on the laptop.
   `breeze_join.py` joins them under the audio Python, which is ComfyUI's embedded Python on the laptop, applies voice effects, and matches the loudness
   of the Drama 3 chapters. It gives each voice one gain, so that the median take of every voice in the chapter sits
   at the same level, because a Breeze take comes out at the level of its voice's cast clip. Before the join, each take's clicks are filled in from the audio around them, and each
   take fades in and out over 3 ms, because Breeze sometimes starts a take at full level on its first sample. The
   chapter's `render.clicks_filled` counts the clicks filled in.

A Drama 3 render and a Qwen cast need ComfyUI (portable, `D:\comfy`) running with the ComfyUI-Drama3 and
ComfyUI-Qwen-TTS node packs. A Breeze render under breeze-server and a Breeze cast don't. Every ComfyUI step runs as its own prompt with `POST /free` in between: on an 8 GB card the
Drama 3 codec must load alone, or ComfyUI loads it partially and it fails with a device mismatch.
The `cpp` runner uses about 3.8 GB of the card and generates at about 2x realtime. It asks ComfyUI to
unload its models first. The `comfyui` runner needs the ComfyUI-Breeze-TTS-2 node pack and ComfyUI
started with `--highvram`: under the default dynamic VRAM its CUDA graph capture fails and its codec ends
up split across CPU and GPU. It uses about 7.7 GB of the card and generates at about 0.5x realtime.

## LLM backends

`subscription` runs headless Claude Code (`claude -p`) under AJ's Claude subscription, with the token in
`CLAUDE_CODE_OAUTH_TOKEN` when the environment has one and the claude.ai login otherwise, so it draws on
subscription usage. `api` uses the Anthropic SDK and `ANTHROPIC_API_KEY`. `auto` tries the
subscription first and, on a usage-limit message, falls back to the API, waits for the reset, or fails,
per `--on-limit`. Both backends get the same prompt, input, and schema; every chapter records which one ran.
