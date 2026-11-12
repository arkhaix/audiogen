# Phase 1: use what the pipeline already has

This phase adds no model, and it should need no host change. Read `README.md` in this folder first. Its shared contract binds every package here.

Checked on the laptop on 2026-11-12, under the audio Python:

* sherpa-onnx 2.0.0 returns `tokens`, `timestamps`, `durations` and `ys_log_probs` for the Parakeet model. The times are on an 80 ms grid. On a 12.8 s narrator take the longest gaps were at the commas.
* soundfile 0.14.0 with libsndfile 1.2.2 accepts `compression_level` and `bitrate_mode` in `sf.write`. Chapter 33's Opus file works out to about 36 kbps.
* The Mac's versions are not recorded. Package F1 checks them first.

## Order

Do S1 first, then F1 and F2. After that the four tracks can run side by side:

* Track A: A1, A2, A3, A4, in that order.
* Track B: B1, B2.
* Track C: C1, C2, C3.
* Track D: D1 after A1, then D2 and D3.

`serial_audio/breeze.py` is shared by F1, A4, B1 and C2. Let one package at a time edit it.

## S1: a render keeps the chapter's seed

* Today `jobs.build()` gives a render seed 1 unless the request has `seed`, and `nomad._takes_needed` counts reuse with seed 1 unless the job's args name one. A render of chapters 13, 16 or 22 of The Hundred Reigns from the chapter page would make every take again, because they were rendered with seeds 3, 2 and 2.
* Use the seed in the chapter's `render` record when the request doesn't name one and the last render was Breeze. Do the same in the scheduler's estimate.
* It is done when a render of chapter 13 queued without a seed, in a scratch app, plans to reuse its takes, and a test in `tests/test_cluster.py` pins that.

## F1: the foundation

Everything in tracks A and D reads what this package stores.

* Check the Mac before anything else. Over ssh, under `/Users/aj/serial-audio/venv`, print the sherpa-onnx, soundfile and libsndfile versions, and decode one take to confirm the result has `tokens`, `timestamps` and `durations`. If the Mac falls short, stop and ask AJ, because a pip change in that venv is a host change.
* Make `parakeet_asr.py` emit, for each file, the text, the raw tokens with their start times, durations and log-probabilities, and the `signal` block. Measure the signal on the audio as loaded, before `load()` resamples it to 16 kHz. Keep the top-level imports of `parakeet_asr.py` in the standard library, because `asr.py` imports `MODEL_FILES` from it under the plain Python.
* Put the measures in a new module, `serial_audio/take_measure.py`, that runs under the audio Python and holds pure functions over arrays. The `signal` block has the take's length, the K-weighted level of its active frames, the peak, the longest run of full-scale samples, the silent runs inside the take as offsets in the raw file, how far the last second falls below the body, and the spread of loudness inside the take. `CLAUDE.md` says `fx.py` and `breeze_join.py` import only each other. Change that sentence to include the new module.
* Give `asr.py` one function that yields a record per file, and make the check call it. `heard_file_text` and the `hears` fake in `tests/test_asr.py`, and `heard_for` in `tests/test_casting.py`, patch `takecheck.transcripts` today. Move them to the record format and keep every assertion.
* In `takecheck.py`, make `entry()` and `check_takes` keep the blocks, make `reusable()` ask for the current block versions, and add `judge()`. Store each voice's baseline for the chapter at the top of `take_check.json`.
* Add `voice_text(line, bible)` and use it in `breeze.plan_takes` and `takecheck.expected_words`. Add the test that pins the keys of a fixed script, and run it before and after the change.
* Make `takecheck.badness()` return the rank tuple of contract point 4.
* Put the three limits of contract point 9 in `breeze.retake_flagged`: the ceiling per render, and the voice breaker with its finding in the chapter's `render` record.
* Add the read-only command `measure-takes <slug> <chapters>`. It prints each voice's distribution of every measure, as a table and as JSON. It never writes into the library.
* It is done when a scratch render of chapter 33 stores both blocks for every take it hears, a second `check-takes` hears the takes whose blocks are missing and none of the others, every key in the library is unchanged, and `measure-takes` has run over The Hundred Reigns with its output saved for the tracks to use.

## F2: the backlog lane

* Today the scheduler has no priority. On 2026-11-12 the nine backlog renders were queued by a script that waited for an empty queue before each one.
* Let a job be marked as backlog in its params. Start a backlog job only when no other job is queued or running. Don't interrupt a backlog job that is already running. Show the mark in the job list.
* Add a command that lists the chapters a render would change, with the count of takes it would make or retake and whether the join's version is older than the current one. This is the definition of "findings" in contract point 10.
* It is done when two backlog renders and one ordinary job, queued together on the fake Nomad, run with the ordinary job first, and the list command on a scratch copy of The Hundred Reigns matches `reusable_takes` chapter by chapter.

## A1: word rules

* Make `takecheck.align` return the edits and not only the counts. The other rules need to know which word was dropped or replaced.
* Flag a take with heavy substitution outside names. On 2026-11-12 only 3 unflagged takes of 9,383 had three or more substituted words that made up 30% or more of the line, so the rule should cost a handful of retakes.
* Flag a take whose last word was dropped. Today an ending needs three dropped words to flag.
* Judge a line of one to three words on an exact match after normalization. When the recognizer hears nothing in such a line, judge it on the length of its speech, because Parakeet gives an empty transcript for lines like "Oh..." and "Hey!".
* Bound what a name can absorb. The token map gives a name's place in the line. Let it match at most two heard words within a bounded spelling distance. A dropped name counts as a dropped word. A line with fewer than two words outside names is judged on its length. Without these bounds a garbled take could hide behind its names.
* Record how each name was heard, in `names`. D1 reads it.
* Fix the gaps in `asr.words`: "Mr." against "mister", accented letters ("fiancée" splits into "fianc" and "e" today), hyphens ("self-aware" against "selfaware") and possessives ("Thalas'" against "Thalas's"). `casting.transcript_match` also calls `words`, so run `tests/test_casting.py` and look at how the cast scores move.
* It is done when `measure-takes` shows what each rule would flag in The Hundred Reigns, the lead has read a sample of those takes' transcripts and set the thresholds, and the counts are in `DECISIONS.md`.

## A2: pauses

* Find the pauses that no punctuation explains. In read speech people pause at 94% of full stops, 45% of commas and only 6% of phrase boundaries with no punctuation (the report, "Silences inside a take").
* Cut only measured silence. A gap between two token times doesn't prove silence. `take_measure` finds the silent runs in the raw audio, and the token times only say which two words a run sits between. When the script has no punctuation between those words and both words matched the script, add a cut to `repairs` that would shorten the run to about 0.2 s.
* Store a cut as offsets in the raw file. The join trims a take's edges before anything else, so an offset in the trimmed take would be wrong.
* Don't cut a line that has a vocal event (`breeze.VOCAL_EVENTS`) or a direction about pace or hesitation.
* Leave sound after the last word alone. Without a speech detector an energy rule can't tell a breath from the end of a word. Phase 2 handles it.
* It is done when the cuts for chapter 33 are listed with the words on each side, and none of them sits at punctuation or inside a word.

## A3: signal rules

* Retake a clipped take. Use the rule the cast uses for candidates, `casting.full_scale_run` with `CLIP_RUN`. It works on a cpp take as it is, a 16-bit mono WAV.
* Retake a take whose ending fades. One is on record for Breeze: a final sentence 7.5 dB down in the 300 to 3,000 Hz band (the report, "Odd volume changes").
* Retake a take with a loudness swing inside it, and a take whose speaking rate is far from its voice's own. Use the voice's baseline from the check.
* Repair level in the join. Don't retake for it, unless the take is further off than a gain can fix.
* All of these run under the voice breaker of contract point 9. A voice whose cast clip is clipped would otherwise have every take retaken.
* It is done when `measure-takes` shows each rule's count per voice, no voice trips the breaker without a cause the lead can name, and the thresholds are in `DECISIONS.md`.

## A4: long lines in pieces

* A line of 58 words and one of 64 lost 25 and 30 of them in The Hundred Reigns, and new seeds fixed both. Most long lines pass, so don't render every long line in pieces. That would replace good audio.
* When a line over about 60 words is flagged, make its retake render the line in pieces at clause boundaries and join the pieces into the line's one take. Record the pieces on the take. Don't add them to the key.
* Apply the length guard to each piece, and keep `usable()` in `render_breeze` in step with it, so a take made in pieces isn't judged out of range on the next render.
* This is for breeze-server only.
* It is done when a test with the fake server shows a flagged long line retaken in pieces and a passing long line left alone, and a scratch render shows the same on a real line.

## B1: mastering

* Today the join gives each voice one gain per chapter on RMS, puts 0.3 s of digital silence between takes, caps every pause at 0.75 s, limits with a tanh curve on sample peaks, and writes Opus with no bitrate set.
* Apply the cuts from `repairs` first, as offsets in the raw file, at the quietest point of each run, with a fade on both sides.
* Level each take on K-weighted loudness toward its voice's target, with an offset by the class of its direction. The classes are whisper, soft, normal, raised and shout, read from the direction's words. Start from -6, -3, 0, +2 and +3 LU, and bound the gain at 6 dB either way. The offset keeps a whisper a whisper.
* Bring the chapter to -18 LUFS, measured as mono. Limit true peaks at -1 dB. Compress gently above the normal level, so a shout is capped and an ordinary line isn't touched.
* Choose the gap after a take from the script: whether the speaker changes, and what punctuation ends the line.
* Pin the Opus bitrate. `fx.write_audio` is shared with the Drama 3 render, so make the new arguments optional.
* Record the join's version in the chapter's `render` record. The backlog lane reads it.
* The join job needs more for each take: the class of its direction, its cuts and the gap after it. `render_breeze` has the line and the plan at hand where it writes the job.
* Make two short clips of one chapter for AJ, the old join and the new one. He listens once and sets the offsets and the gaps. Don't run the backlog pass before that.
* It is done when `tests/test_fx.py` covers the loudness measure, the cuts and the limiter with known answers, a scratch join of chapter 33 measures -18 LUFS and stays under -1 dB true peak, and AJ has heard the clips.

## B2: cast clips

* A Breeze take comes out at the level of its voice's cast clip, and the clips ran from -11 to -29 dB RMS. BreezeBlue's own platform normalizes a clone reference to -18 LUFS.
* Bring a newly filed cast clip to -18 LUFS. Don't touch an existing voice, because the clip's hash is part of every take key.
* It is done when a cast in a scratch copy files a clip at -18 LUFS and every key of an existing voice is unchanged.

## C1: script fields

* A script can't be written again without moving its line numbers, and a moved line number makes every later take again. So record now the fields that phases 2 and 3 will read.
* Add four fields to a script line: the author's emphasized words, a focus word where the line has a contrast or a correction, the sense of a heteronym such as "read" or "lead", and the direction split into emotion, energy and pace. The schema sets `additionalProperties` to false and requires every field, so add them as required with an empty default, the way `direction` works.
* Nothing reads the fields yet, and none of them enters the key.
* The Royal Road parser keeps italics as asterisks. The EPUB parser drops all inline markup, so teach it italics. The screenplay prompt says to drop the asterisks at one place and to keep italics at another. Fix that, and have the pass move the marked words into the new field.
* It is done when a screenplay pass in a scratch copy writes the fields for a chapter that has italics, the script's text has no asterisks, and every take key of an existing script is unchanged.

## C2: text repair

* Measure first. Over the saved checks, compare the word-check results of lines that have digits, symbols, all-caps words, parentheses, ellipses or dashes with the lines that don't. The prompt already expands abbreviations and spells out Roman numerals, and it tells the pass to leave digits as digits. Nobody has measured how Breeze reads them.
* Repair in `voice_text()` only what the data supports. `speakable` turns `(x)` into `, x,` today unless x is a vocal event, and it passes an unclosed parenthesis, asterisks and `[image: ...]` through.
* A repair changes the text of the lines it touches, so their keys change and a render makes them again. Count those lines before the deploy, by contract point 7.
* It is done when the measurement is in `DECISIONS.md`, each repair has a test, and the count of lines a render would make again is known and explained.

## C3: the script against the chapter

* Today the only guard is a word-count ratio of 0.9 to 1.2, so a script that drops a paragraph passes.
* Diff the script's words against the chapter's. Send a dropped or repeated run back to the model in the pass's retry loop, as `validate_script` does with its other problems.
* Add a read-only audit that lists existing scripts with gaps. Don't write an existing script again. AJ decides that chapter by chapter, because it remakes the whole chapter.
* It is done when a test shows a dropped paragraph caught and sent back, and the audit has run over The Hundred Reigns with its output given to AJ.

## D1: unstable names

* On 2026-11-12, within half a minute of chapter 33, the recognizer wrote Belzemine as "Belzemine", "Basmin", "Belzamine" and "Expezamine". That can be the recognizer and not the voice, so this list is a hint and not a verdict.
* From the `names` field of the saved checks, list each term of the bible with the spellings heard and how often, across chapters.
* It is done when the view returns the list for The Hundred Reigns and the terms with the most spellings are on top.

## D2: pronunciation options

* Add a job in two stages, like a cast. The first stage runs on the `llm` node: Claude proposes four to six respellings of a term, from its bible entry and two or three script lines that name it. `casting.bio_pass` is the model for a small pass with its own prompt and schema. The second stage runs on a breeze node and renders each candidate inside a real line of the script, in the voice that says the line.
* Always include the term as written among the candidates.
* Never file a pronunciation from this job. A new say-as changes the text of every line that names the term, in every chapter, so a render makes each of those takes again. That should happen once, on AJ's pick.
* Record the candidates under the term in `bible/pronunciations.yaml`, and write their audio under the node's output folder in `breeze/say/<slug>/`. A later job needs that audio, so teach `worker.media_files` to find it.
* A new job kind has to be added in about a dozen hard-coded lists: `HEAVY` and the other kind sets and `build()` in `web/jobs.py`, `LLM_KINDS`, `conflicts`, `job_stages` and `_duration` in `web/nomad.py`, `comfy_marker` in `worker.py`, the tabs in `app.js`, and the fake for Claude in `tests/test_casting.py`, which treats any schema without `bios` as the casting pass. `BiosJobTest` in `tests/test_cluster.py` is the template for the tests.
* Do this after A1. A respelling such as "Laurie Anne" is two words where the recognizer writes one, and only A1's name rule keeps that from flagging the take.
* It is done when the job runs on a throwaway serial on the cluster, the candidates play from the mirror on the other node, and the throwaway serial is deleted.

## D3: the Names page

* Add a tab beside Cast. Put the unstable names on top. Give each term a play button for each candidate, "Options" to queue D2, "Use this" to file a candidate through the `say-as` job that exists today, and the free-text respelling the Bible page already has.
* `castTab`, `voiceCard`, `openDialog`, `playBtn` and `queue` in `app.js` are the parts to reuse.
* The picks AJ makes in one sitting go into one backlog pass, with the chapters ahead of him first.
* It is done when a pick on a second instance of the app changes the term's `say`, the next render in the scratch copy makes only the lines that name the term, and phone-width screenshots of the page are taken as `CLAUDE.md` describes.

## The end of the phase

* Ask AJ which chapter he is on. Run the backlog pass over the chapters ahead of him, then the older ones.
* The phase is done when a render of a new chapter stores both evidence blocks for every take, retakes on the new reasons within the three limits, joins with the new chain, the Names page files a pick that the next render uses, and the backlog pass has run over the chapters ahead of AJ.

## What AJ does in this phase

* He listens to the two clips of B1 and sets the offsets and the gaps.
* He uses the Names page for the first time and says what is wrong with it.
* He says which chapter he is on when the backlog pass starts.
