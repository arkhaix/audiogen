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
library/<slug>/chapters/0001/breeze_takes.json    the Breeze take of each script line: key, file, voice, seed
library/<slug>/chapters/0001/take_check.json      one entry per take: its evidence and its verdict
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
python -m serial_audio.cli screenplay <slug> <chapters> [--marks]  # text -> script.json
python -m serial_audio.cli marks <slug> <chapters>   # each script line's emphasis, focus, heteronyms, emotion, energy and pace, again
python -m serial_audio.cli audit-scripts <slug> [chapters]   # the scripts that leave out or repeat a run of the chapter's words, read-only
python -m serial_audio.cli render <slug> <chapters> [--seed N] [--engine drama3|breeze] [--runner cpp|comfyui] [--decode-only]   # script -> audio
python -m serial_audio.cli check-takes <slug> <chapters> [--asr qwen3|parakeet] [--redo]   # flag Breeze takes that do not say their line
python -m serial_audio.cli measure-takes <slug> <chapters> [--stored] [--json]   # each voice's take measures and what the check flags, read-only
python -m serial_audio.cli backlog <slug> [chapters] [--all] [--json]   # the chapters a render would change, read-only
python -m serial_audio.cli say-options <slug> <term> [--dry-run | --render-only | --add <spelling>]   # respellings of a term, each said in a line of the script. It doesn't file any
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
A job whose request has `"backlog": true` in its params is backlog work, and the job list marks it. It starts only
when no other job is running and no job that is not backlog work is queued, on either executor, so backlog jobs run one
at a time. A job that is not backlog work never waits for queued backlog work, and a backlog job that is already
running is left to finish. A backlog render isn't queued while a backlog render with the same arguments waits, because
the one that waits reads every change made before it starts, and a queued one is skipped when a render with the same
arguments has left it nothing to do. `backlog <slug>` lists the chapters a render would change: the takes it would make, the
flagged takes it would take again, whether it would join audio the chapter doesn't have from the current Breeze
join, the saved takes it would only hear, and the engine of the last render. A render records a hash of the cuts its
join made, so a join counts as due when the cuts a join would make now differ, such as a tail that newer rules cut. A
render from before it recorded the hash counts as having made every cut but one that ends a take.
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
The Names tab lists the names of the bible by how many ways the take check's recognizer spelled them, the most
first, with the spellings and their counts. The recognizer can be what varies, so the list is a hint. Get options
queues a `say-options` job for a name: a Claude pass proposes four to six respellings, and the voice that says one
of the script's lines with the name says that line once for the name as written and once for each respelling, from
the seed the chapter's render uses for the line. Each option has a play button, how the recognizer wrote the name in
it, and Use this, which queues the `say-as` job for it. The job never files an option itself. Say them makes the
takes again for options that don't have any. The tab also has a box for a spelling of your own. Hear it adds the
spelling to the options and has the voice say it, without a Claude pass, and Save files it as the say-as. New options
replaces the options, the typed ones too. After a change it
offers to queue a render of every chapter whose takes say the name, as backlog work, with the chapters from the one
you are on first. A chapter whose check is from before 2026-11-12 doesn't have names until a take check or a render
checks it again.

## Pipeline per chapter

1. `add-epub` or `add-royalroad`/`fetch` writes `text.md`; status `fetched`. Royal Road pages are read
   with a browser user agent, one request every two seconds; the per-page hidden decoy paragraph and
   ads are dropped, blockquotes (LitRPG system text, letters) become `> ` blocks, and the author's
   notes go to `author_notes.md`. Both parsers keep italics inside a paragraph as `*...*`, and an EPUB chapter
   ingested before 2026-11-12 doesn't have them.
2. `character-pass` updates the bible with who appears, who is new, voice briefs; status `characters`.
3. `cast` designs a voice for each uncast major/recurring character. A casting pass writes a
   voice-design instruction and sample line, and breeze-server designs N candidates from it with Breeze TTS 2
   voice design. Each is transcribed with the same recognizer as the take check (step 5), and the best
   transcript match that isn't clipped is filed under `voices/` (status `auto` until you listen and `pick-voice`).
   Before one is filed, every candidate's clip is brought to -18 LUFS under the audio Python, because a Breeze take
   comes out at the level of its voice's cast clip. A voice cast before 2026-11-12 keeps the clip it has. A Breeze
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
   apart from speech normalizations, and sets status `scripted`. The pass checks its answer and sends the problems
   back once. A line's text can't have a pair of asterisks around words, which is how `text.md` marks italics. The
   script's word count has to be within 0.9 to 1.2 of the
   chapter's, and the chapter's words are compared with the script's in order, without the heading lines. A run of
   more than 4 chapter words that the script leaves out, or of more than 4 script words that the chapter doesn't
   have there, such as a repeated passage, is a problem that quotes the run. `audit-scripts <slug> [chapters]` lists
   the existing scripts that have such a run, and it writes nothing.
   A second pass, the marks pass, reads a saved script beside the chapter's text and gives each line six
   fields that nothing reads yet and that no take key holds: `emphasis` (the words the author set in italics for
   stress), `focus` (the word a contrast or a correction stresses), `heteronyms` (each word whose reading the
   context decides, with a respelling of how it sounds here), and `emotion`, `energy` and `pace`, which restate the
   direction. Its answer names only the lines that have a mark. A word it names that its line doesn't have is left
   out. A screenplay pass runs it only with `--marks`, and no job asks for it yet, because nothing reads the
   fields and the pass costs about a third of a screenplay pass again. `marks <slug> <chapters>` runs it on a
   chapter that has a script. It never changes a line's speaker, text or direction, so it never changes a take.
   Until a job runs it, use it only in a scratch copy.
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
   takes, so a rerun renders only new or changed lines and `--decode-only` joins the saved takes again. A render
   without `--seed` uses the seed of the chapter's last Breeze render, so it reuses those takes, and 1 for a chapter
   that has none.
   Before the join, when the recognizer is Parakeet, `render` hears the new takes while breeze-server is still
   up. It takes a flagged line again with new seeds, in up to two rounds, and keeps a new take only when it ranks
   above the old one. `breeze_takes.json` marks a replaced take with `retaken`, and `render.check` lists those lines.
   A saved take that an earlier check flagged goes through the same check and rounds, and it stays unless a new take
   ranks above it. `retake_rounds` on a take records the last round its line went through. A later render goes on
   from that round, and a line that stays flagged after every round is left alone. A flagged line of more than 60
   words is taken again in pieces of about 40 words, split at its sentences and clauses and joined into its one
   take with 0.25 s between them, and the take records the count under `pieces`. A saved take that doesn't have
   evidence from the current check is heard before the join too, so a finding costs one retake while the server is
   up. Three limits bound the retakes. A line gets three rounds of three tries at most, over every render of its take,
   which is nine generations. A line in pieces is taken in the two retake rounds, with three tries for each piece
   in a round. A
   render takes lines again 8 times or for an eighth of its takes, whichever is more, the worst lines first, and
   leaves the rest for a later render. When one reason flags four or more takes
   of a voice and over a quarter of them, the cause is the voice: the render records a voice finding under
   `render.voice_findings`, marks those takes `retake_held` and doesn't take them again.
   The check keeps one entry per take in `take_check.json` beside the script. An entry holds the take's evidence in
   two versioned blocks: `asr` has the transcript and Parakeet's raw tokens with their start times, durations and
   log-probabilities, and `signal` has what `take_measure.py` reads from the audio at its own sample rate (length,
   K-weighted level, peak, the longest run of full-scale samples, the span of speech, the silent runs inside it, how
   far the last second falls below the rest, and the spread and the largest step of loudness between windows of one
   second). On a machine that has their models, three more blocks join them: `vad` has the speech that Silero VAD v5
   finds, `speaker` has TitaNet small's embedding of the voice, packed as int8 values in base64, and `asr2` has the
   transcript of a second recognizer from another family, Moonshine base English. A saved take that lacks only these
   is heard for them alone, and its transcript and signal stay. `takecheck.judge()`
   derives the rest from the blocks: the word counts, the verdict, the reasons for a retake and the repairs for the
   join. The words of the line come from `breeze.voice_text()`, which builds the text the voice is sent and a token
   map that says which words are names and what punctuation follows each word. A name matches the recognizer's own
   spelling of it, in one word or two, and the entry records each spelling under `names`. Two words heard as one, one
   heard as two, and a contraction heard as its two words all count as heard. A vocal event in parentheses is not a
   word of the line. A take is flagged when a reason for a retake holds:
   * `inserted`, `dropped`: three or more added or missing words.
   * `substituted`: three or more changed words outside names that are 30% of the line or more.
   * `ending`: the last word is missing.
   * `short`: a line of one to three words with an added or a missing word.
   * `length`: a line of one to three words, or with fewer than two words outside its names, whose speech per word is under 0.1 s or over 2 s. When the recognizer hears nothing in such a line, this is the only rule that judges it.
   * `clipped`: three or more full-scale samples in a row.
   * `fading`: an ending 16 dB or more below the speech before it.
   * `swing`: a spread of 13.5 dB or a step of 16 dB in loudness between windows of one second.
   * `rate`: speech per word under 0.45 or over 2.6 times the voice's median in the chapter, for a line of four words or more.
   * `level`: a level over 12 dB from the voice's median in the chapter, after the offset its direction calls for.
   * `tail`: Parakeet or the second recognizer heard a word after the line's last word, and the take's sound goes on more than 0.5 s past that word, in a line of four words or more that doesn't ask for a sound after its words, such as "(laugh)" or a direction that names a sound like laughing, a cough, a sigh, weeping or purring (`takecheck.TAIL_SOUNDS`).
   * `drift`: the voice's embedding lies more than 10 median absolute deviations, and at least 0.1, below the median similarity of the voice's takes in the chapter to their centre. Only a take with 3 s of speech or more, of a line without a direction or a vocal event, is judged on this, and only a voice with 8 such takes in the chapter has a centre.
   * `garbled`: a take of a line of four words or more that would be suspect, and that the second recognizer hears wrong too, by the same word rules.

   A take with a high word error rate and no reason is suspect. `repairs.cuts` lists each pause of 0.6 s or more
   between two words of the line that have no punctuation between them, cut down to 0.25 s. When the take's sound goes on more than 0.5 s past the line's last word, Parakeet
   heard that word and no word after it, and so did the second recognizer when the take has its transcript, and a
   silent run sets the sound apart, it also lists a cut from
   inside that run to the end of the file, which ends the take with a fade out. A line with a vocal event
   or a direction about pace keeps its pauses. The file also holds each voice's baseline for the chapter, the median
   and spread of its level and of its seconds of speech per word, the centre of its embeddings with the median and
   spread of their similarity to it, and the version of the rules. A render judges the
   saved evidence again when the rules are newer than the file's. `render.check` in `chapter.yaml` has the summary.
   After the join, `render` runs the check over the whole chapter and reuses the evidence gathered before the join.
   When the recognizer can't run, the check is skipped and recorded as such. `check-takes <slug> <chapters>` runs it
   on its own. It reuses the saved evidence of a take whose audio and block versions are unchanged, hears the other
   takes, and judges every take again. `--redo` hears everything again. A take the length guard kept out of range is
   listed beside the flagged ones. `measure-takes <slug> <chapters>` prints each voice's distribution of every measure
   and the takes each reason flags, and `--json` adds one row per take. It hears with Parakeet the takes that
   don't have saved evidence. With `--stored` it doesn't hear a take, and it reports from the saved evidence of
   either recognizer. It doesn't write anything.
   The recognizer is Parakeet TDT 0.6B v2 through sherpa-onnx when its model folder exists
   (`SERIAL_AUDIO_PARAKEET_MODEL`), and Qwen3-ASR in ComfyUI otherwise. `--asr` or `SERIAL_AUDIO_ASR` picks one.
   The child that runs Parakeet gathers the other blocks too, each from a model folder beside Parakeet's:
   `silero-vad-v5`, `titanet-small` and `moonshine-base-en-2026-02-27`. A machine without a folder, or with a folder
   that lacks a file or whose model doesn't load, doesn't gather that block, and the check logs the folder that lacks
   a file. `drift` needs the speaker block and `garbled` the second transcript. `tail` and the cut of a tail need
   only Parakeet and the signal block, and a speech detector's block can only move the end of the sound later.
   `docs/machines.md` has the Parakeet install on the Mac and on the laptop, and the list of the other models.
   `breeze_join.py` joins the takes under the audio Python, which is ComfyUI's embedded Python on the laptop, and
   masters the chapter. For each take it makes the cuts the check recorded under `repairs.cuts`, fills the clicks in
   from the audio around them, applies the voice's effect, caps a pause at 0.75 s and fades the take in and out over
   3 ms, because Breeze sometimes starts a take at full level on its first sample. It then levels the takes on
   K-weighted loudness. Each voice gets one gain that brings its median take to the target, because a Breeze take
   comes out at the level of its voice's cast clip. Each take then moves toward the target plus the offset of its
   direction's class, by 6 dB at most and by less when it has under 1.5 s of speech: whisper -6 dB, soft -3,
   normal 0, raised +2 and shout +3. The gap after a take comes from the script: 0.15 s when the sentence goes on
   into the next line, 0.35 s before the same speaker and 0.5 s before another one. The chapter is compressed 3 to 1
   where 0.4 s of it is over 6 dB above the target, brought to -18 LUFS as one channel, limited to a true peak of
   -1 dB and written as Opus at 40 kbps. The loudness and the peak are measured on the Opus file as it plays, and
   the file is written again when it misses either. `render.join` is the version of the join that made the audio,
   and `render.loudness`, `render.true_peak`, `render.pauses_cut` and `render.clicks_filled` say what it measured
   and did.

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
