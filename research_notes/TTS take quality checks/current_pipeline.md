# The current pipeline's per-take checks

Notes for the report on better per-take quality checks, written on 2026-11-12 from the code and docs in `D:\fish`
(branch main, last commit b3f044f, with `docs/casting.md` modified in the working tree). Paths are relative to
`D:\fish`. Each fact carries a `file:line` source or quotes the output of the statistics script in section 8. Where the
repo does not say something, these notes say so. "Inferred" marks a conclusion drawn from the cited code rather than
stated in it.

Terms: a take is one Breeze TTS 2 rendering of one script line. A render makes the takes a chapter lacks, joins them
into one Ogg Opus file and checks them. The laptop is aj-legion5 (RTX 3070 Laptop, 8 GB) and the Mac is jsc-m1 (M1,
16 GB), the two `breeze` nodes (`deploy/README.md:7`).

---

## 1. The checks and repairs, their rules, and their order

### 1.1 The order of one Breeze render under breeze-server

The default runner is `cpp`, Breeze-TTS-2.cpp's breeze-server (`serial_audio/breeze.py:562`, `DECISIONS.md:18`).

1. Setup: load the script, the bible, the speaker to voice slots, the say-as map and the voice effects (`serial_audio/breeze.py:645-666`).
2. Load the saved takes and stage each voice's cast clip as a 16-bit WAV (`breeze.py:668-671`, `breeze.py:123-164`).
3. Plan each line: the text sent to the voice is `speakable(apply_say_as(text, say))`, the direction has its whitespace collapsed, the voice gets its seed, and the take gets a key (`breeze.py:573-598`). A line with no word character is not spoken (`breeze.py:675`).
4. Reuse: a saved take stands when its key matches and its file exists. Under `cpp`, outside a join-only run, a reused take must also have a length inside the length bounds, unless it is marked `out_of_range` (`breeze.py:677-685`). Every other spoken line is taken again.
5. Start breeze-server: refuse when another one runs, wait for ComfyUI to go idle, ask ComfyUI to free the card (`breeze.py:700-705`).
6. Make the takes in script order, each through the length guard (1.3), and save `breeze_takes.json` after each take (`breeze.py:713-734`).
7. Check the new takes with Parakeet and take a flagged line again (1.5), while breeze-server is still up (`breeze.py:746-758`).
8. Stop breeze-server (`breeze.py:759-761`).
9. Join the takes under the audio Python (1.6): declick, trim each take's edges, apply a voice effect, one gain per voice, fades, gaps, pause trimming, the chapter level and a soft limit, then Ogg Opus (`breeze.py:768-791`, `serial_audio/breeze_join.py:72-120`).
10. Write the `render` block of `chapter.yaml` (`breeze.py:793-808`).
11. Check after the join (1.4): transcribe every take in `breeze_takes.json`, reusing the transcript of an unchanged take, and write `take_check.json` and `render.check` (`breeze.py:810-813`, `serial_audio/takecheck.py:175-225`). It runs when the render made takes or the last render has no check, and it never fails the render (`takecheck.py:228-239`).

The `comfyui` runner has neither the length guard nor the check before the join (`breeze.py:715-722`,
`breeze.py:746`). `check-takes <slug> <chapters>` runs step 11 alone (`README.md:41`, `serial_audio/cli.py:339-348`).

### 1.2 The text a voice is sent

* Say-as: every whole-word, case-sensitive occurrence of a term becomes its `say` spelling, in one pass, longest term first (`serial_audio/render.py:138-146`, `serial_audio/pronounce.py:28`). Only entries with a `say` count (`render.py:132-135`). The `respelling` the character pass writes is never sent to the voice (same lines).
* `speakable` collapses whitespace, turns a parenthesis that is not a vocal event into commas, keeps `(laugh)`, `(cough)`, `(clears throat)` and `(sigh)`, and tidies the commas (`breeze.py:39`, `breeze.py:103-111`). It changes nothing else. Asterisks, capitals and other punctuation pass through.
* A line with a direction is sent with the instruction "Perform the line with this direction: {direction}." at cfg 4.0. A line without one is a plain clone at cfg 1.0 (`breeze.py:40-41`, `breeze.py:114-115`, `breeze.py:416-419`).
* Sampling: temperature 0.9, top_k 50, top_p 1.0, repetition_penalty 1.1, max_new_tokens 1500 (`breeze.py:56`).
* Seed: every line of one voice in one chapter starts from the same seed, `1 + crc32("{base}:{voice}") % (2**31 - 2)` (`breeze.py:118-120`, `breeze.py:592`). The retired chapter 7 takes show seed 2129663508 on narrator lines 0 to 4 (`library/the-hundred-reigns/chapters/0007/breeze_takes.json`).

### 1.3 The length guard (cpp only)

* Words: `count_words` counts runs of word characters, joins apostrophe parts, and counts each CJK character as one (`breeze.py:78-80`).
* Bounds for a line of w words: shortest 0.12 x w seconds, longest 2.25 + 0.75 x w seconds (`breeze.py:64-66`). Computed from that: 1 word 0.12 to 3.0 s, 10 words 1.2 to 9.75 s, 80 words 9.6 to 62.25 s.
* Length of a take: PCM bytes / 2 / sample rate (`breeze.py:425`).
* Tries: up to RETAKES + 1 = 3, stopping at the first one inside the bounds (`breeze.py:59`, `breeze.py:422-430`). Try n uses seed `1 + (vseed - 1 + n x 7919) % (2**31 - 2)` (`breeze.py:60`, `breeze.py:423`).
* Keep: the try with the smallest miss ratio, which is seconds / longest above the range, shortest / seconds below it and 1.0 inside (`breeze.py:69-75`, `breeze.py:431`). When no try fits, the entry gets `out_of_range: true` and the log says "listen to it" (`breeze.py:441-442`, `breeze.py:731-733`).
* Server faults: breeze-server's HTTP 409 is waited out. Any other error, a cut body, or an empty or odd-length answer raises, and the take restarts breeze-server and is tried once more (`breeze.py:374-399`). "A take the server ended early with a short clean answer is not detected here" (`breeze.py:363`), which leaves it to the length guard.

### 1.4 The transcript check

Recognizer: `SERIAL_AUDIO_ASR`, else Parakeet when its model folder exists, else Qwen3-ASR (`serial_audio/asr.py:32-42`).
Both breeze nodes have Parakeet (`DECISIONS.md:123`).

Normalization, `asr.words` (`asr.py:184-301`):

* Lower case, and curly and prime apostrophes made straight (`asr.py:202`).
* Thousands commas removed, `3.5` read as `3 point 5`, `10:30` as `10 30`, `$5` as `5 dollars`, `£5` as `5 pounds`, `%` as percent, `°` as degrees, and a `'` or `"` after degrees as minutes or seconds (`asr.py:203-210`).
* Tokens are runs of `[a-z0-9']` or one punctuation mark, with apostrophes stripped from their ends (`asr.py:197`, `asr.py:300`).
* Punctuation is dropped, number words become digits, "a hundred" is 100, an "and" inside a number is skipped, an ordinal becomes `<n>th`, and a two-digit group joins a one- or two-digit group before it, so "eighteen sixty six" is 1866 (`asr.py:213-291`).
* Gaps the code and the data show: a letter outside a to z splits a word, so "fiancée" becomes `fianc` and `e` (section 8). A hyphen splits a word. Abbreviations are not expanded, so the script's "Mister" against a transcript's "Mr." is a substitution (section 8), and the screenplay pass expands `Mr.` to `Mister` (`prompts/screenplay_pass.system.md:28`).

Alignment: a word-level Levenshtein alignment that counts substitutions, deletions and insertions, taking the minimum
over (cost, sub, del, ins) tuples (`takecheck.py:30-44`). The expected words are
`words(speakable(apply_say_as(text, say)))` (`takecheck.py:69-71`).

Verdicts, tested in this order (`takecheck.py:23-24`, `takecheck.py:47-66`):

1. `unreadable`: the transcript has a letter but no letter from A to Z, which means the recognizer picked another language.
2. `flagged`: 3 or more insertions, or 3 or more deletions (MAX_SLIPS = 3).
3. `suspect`: word error rate over 0.34, when the line has 2 or more words or the transcript is empty.
4. `ok`: anything else.

The word error rate is (sub + del + ins) / max(1, expected words), rounded to 3 places (`takecheck.py:56`).
Substitutions alone never flag a take, however many there are (`takecheck.py:59-62`). A saved transcript is reused
when the take's key, its audio hash and the recognizer all match, and its verdict is worked out again under the
current rules (`takecheck.py:82-85`, `takecheck.py:208-209`). The summary in `render.check` lists the suspect,
unreadable, flagged and retaken lines (`takecheck.py:140-153`).

### 1.5 Taking a flagged line again before the join

`retake_flagged` (`breeze.py:454-517`):

* It runs only when the recognizer is Parakeet and the render made takes (`breeze.py:477-479`), because Qwen3-ASR needs the card that breeze-server holds (`DECISIONS.md:46`).
* It checks only the takes this render made (`breeze.py:482`, `breeze.py:747`, `breeze.py:757`). A take from an earlier render is never taken again (`DECISIONS.md:46`).
* Only `flagged` starts a retake. `suspect` and `unreadable` do not (`breeze.py:485`).
* Up to CHECK_RETAKES = 2 rounds (`breeze.py:61`, `breeze.py:484`). Round r calls the length guard with first = r x 3, so round 1 uses tries 3 to 5 and round 2 uses tries 6 to 8 (`breeze.py:749-755`, `breeze.py:422`). Computed from that: at most 9 generations per line.
* Keep if closer: badness is (the verdict's place in ok, suspect, unreadable, flagged, then sub + del + ins) (`takecheck.py:26`, `takecheck.py:88-90`). A new take replaces the old one only when its badness is strictly lower (`breeze.py:495-498`). A replaced entry gets `retaken` set to the round, the new seed and audio hash, and the summed `takes_tried` (`breeze.py:494`, `breeze.py:499-500`).
* It never raises. On a failure the join uses the takes as they are (`breeze.py:512-516`).
* Results recorded: in a scratch copy on 2026-11-11, eight flagged takes came out flagged again from their seeds, and the first new take replaced each one (`DECISIONS.md:46`).

### 1.6 The join

`breeze_join.py` runs under the audio Python with the constants of `fx.py`. For each take, in script order
(`breeze_join.py:85-101`):

* Read it as float32 and mix it to mono (`breeze_join.py:86`, `breeze_join.py:93`). Every take must have the same sample rate, or the join fails (`breeze_join.py:87-90`).
* Cap: a take of (120 - 0.5) s or more is listed as capped (`breeze.py:44`, `breeze_join.py:91-92`), logged as "may be cut off" and stored as `render.capped_lines` (`breeze.py:788-789`, `breeze.py:799-800`). Inferred: 1500 max_new_tokens x 80 ms frames is 120 s.
* Declick (`fx.py:125-218`): a 4th-order Butterworth high-pass at 5 kHz, energy in 2 ms frames, and a candidate where a frame stands 25 dB over the median of the 101 frames (about 200 ms) around it and above -55 dB (`fx.py:36-40`, `fx.py:137-148`). It is a click only when fewer than 1 ms of samples sit above half its peak, its 0.5 ms moving RMS stays above a quarter of its peak for under 4 ms, and its peak is at least 1.5 times the RMS of the louder of the stretches 2 to 20 ms before and after it (`fx.py:41-46`, `fx.py:149-170`). Each span, padded by 0.25 ms, is filled from an order-32 autoregressive (LPC) model of the 20 ms on each side, solved by least squares, with a straight line as the fallback (`fx.py:48-49`, `fx.py:174-200`). Two passes (`fx.py:47`, `fx.py:203-218`). It needs a sample rate over 10 kHz (`fx.py:135-136`).
* Edges: leading and trailing silence is cut, in 50 ms windows whose RMS is under 0.003 (`breeze_join.py:28-34`, `fx.py:28-29`).
* A voice effect (whisper) when the voice has one, brought to the take's RMS (`breeze_join.py:96-100`, `fx.py:111-113`).

Then for the chapter:

* One gain per voice: -18 dB minus the median, over that voice's takes in the chapter, of each take's active-speech RMS in dB. Active speech is the 50 ms windows with RMS of 0.003 or more (`breeze_join.py:19`, `breeze_join.py:37-48`, `breeze_join.py:59-69`, `breeze_join.py:106`).
* Each take fades in and out over 3 ms with a raised cosine (`fx.py:50`, `fx.py:221-231`), with 0.3 s of silence between takes (`fx.py:26`, `breeze_join.py:107-111`).
* Pause trimming, worked out on the audio before the gains: no silence before the first sound, every silent run cut to 0.75 s, and 0.15 s kept after the last sound (`fx.py:27`, `fx.py:30`, `fx.py:239-268`, `breeze_join.py:113-116`).
* The chapter brought to -18 dB of active-speech RMS, then a soft limit that leaves samples under 0.8 alone and bends louder ones by tanh toward 0.99 (`breeze_join.py:51-56`, `fx.py:116-122`).
* The join returns takes, treated_lines, raw_seconds, seconds, sample_rate, capped, clicks and voice_gains (`breeze_join.py:118-120`). `breeze.py` logs the voice gains and does not store them (`breeze.py:790-791`, `breeze.py:794-798`).

### 1.7 Where the verdicts surface

* The chapter page lists each take whose verdict is not ok, and each out_of_range take, worst first, with the script line, the transcript, the counts, the take's length and a player (`serial_audio/web/views.py:286-317`).
* A cluster worker sends back the audio of each take whose verdict is not ok or that is out_of_range (`serial_audio/worker.py:186-211`), and the app keeps it in `state/media/` (`deploy/README.md:187-188`).
* DECISIONS records why: the check flags, and AJ listens while driving (`DECISIONS.md:45`).

### 1.8 Related checks outside the takes

* Casting: a candidate's match is the difflib ratio of `asr.words` of the sample line and of its transcript (`serial_audio/casting.py:203-207`), and 0.85 or more files it as `auto` (`casting.py:41`, `casting.py:535`). A candidate with 3 or more full-scale 16-bit samples in a row is marked clipped (`casting.py:42`, `casting.py:320-343`, `casting.py:359-361`). Inferred from a search of the package: `full_scale_run` is called only for cast candidates, never for takes.
* Screenplay: each speaker must be `narrator` or a cast id, no line may be empty, and the script must hold 0.9 to 1.2 times the chapter's words, with one retry (`serial_audio/screenplay.py:42-64`, `screenplay.py:77-87`). A check of the script against the chapter word for word is undecided (`DECISIONS.md:210`).

### 1.9 What no check covers today

* Silence inside a take. The pause trim cuts any silent run to 0.75 s and records nothing per take (`fx.py:239-268`, `breeze_join.py:118-120`).
* Loudness of one take against its voice, or a level change inside a take. Each take's active-speech level is computed for the gain and then dropped (`breeze_join.py:106`).
* Clipping in a take. Breeze copies a clipped cast clip's distortion into every take (`docs/casting.md:33`).
* Pronunciation, stress, emphasis, or whether a name is said the same way in every take. A substitution never flags (`takecheck.py:59-62`).
* Clicks per take. Only a chapter total is kept (`breeze_join.py:94`, `breeze.py:798`).
* Word timings (section 4).

---

## 2. The data at hand for each take and chapter

### `breeze_takes.json`, in the chapter folder

Top level: `engine` ("breeze"), `runner`, `takes` keyed by the line number as a string (`breeze.py:203-206`).

| Field | Meaning | Source |
|---|---|---|
| `key` | sha1 of the text sent, voice, cast clip's sha1, direction, seed, model, controls, cfgs and runner | `breeze.py:186-187`, `breeze.py:593-597` |
| `file` | the take's path on the node that made it | `breeze.py:725-727` |
| `voice` | voice id | `breeze.py:727` |
| `direction` | the direction sent | `breeze.py:727` |
| `seconds` | wall time to make the take, not its length | `breeze.py:714`, `breeze.py:728` |
| `seed` | seed of the try kept (cpp) | `breeze.py:438` |
| `audio` | first 16 hex digits of the sha1 of its PCM (cpp) | `breeze.py:438` |
| `takes_tried` | only when more than 1 | `breeze.py:439-440` |
| `out_of_range` | only when no try fit the bounds | `breeze.py:441-442` |
| `retaken` | the round whose take replaced a flagged one | `breeze.py:500` |

Not stored: the take's length, its sample rate, the text sent (only hashed into the key) and its word count. The text
can be rebuilt from `script.json` and the say-as map, as the check does (`breeze.py:590`, `takecheck.py:69-71`). The
retired copy's 824 entries predate the `audio` field (section 8).

### `take_check.json`, in the chapter folder

Top level: `at`, `model`, `asr`, `takes` (`takecheck.py:137`, `takecheck.py:220`). Each entry: `key`, `asr`, `heard`
(the raw transcript), `words` (the count of expected words), `sub`, `del`, `ins`, `wer`, `verdict`, and `audio` when
the take has a hash (`takecheck.py:65-66`, `takecheck.py:74-79`). A file without `asr` reads as Qwen3-ASR
(`takecheck.py:85`).

### `script.json`, in the chapter folder

Top level: `lines`, `notes`, `stats`, `provenance` (`screenplay.py:90-91`). A line has `speaker`, `text` and
`direction` and nothing else (`schemas/screenplay_pass.schema.json:9-15`). All 2,686 lines in the retired copy have
exactly those three keys (`script_fields.py`, section 8). `stats` holds source_words, script_words, coverage, lines
and speakers (`screenplay.py:58-60`). `notes` is the pass's free-text note for a reviewer (`prompts/screenplay_pass.system.md:38`).

### The `render` block of `chapter.yaml`

`engine`, `runner`, `model`, `takes_file`, `comfy_audio`, `audio`, `seed`, `slots`, `notes`, `generated_takes`,
`generate_seconds`, `clicks_filled`, `at`, and when they apply `capped_lines`, `effects` and
`fx` {treated_lines, raw_seconds, seconds} (`breeze.py:794-804`). `check` is {at, file, checked, suspect, unreadable,
flagged, retaken} (`takecheck.py:147-153`) or {at, skipped} (`takecheck.py:233`). `chapter.yaml` also holds the
script stats, the pass provenance, the characters present and a summary
(`library/the-hundred-reigns/chapters/0007/chapter.yaml`).

### The bible

* `bible/pronunciations.yaml`: term -> `respelling` (plain syllables, stressed syllable in capitals), `note`, `chapter`, and an optional `say` (`serial_audio/passes.py:99-102`, `pronounce.py:24-67`, `schemas/character_pass.schema.json:72-84`, `prompts/character_pass.system.md:33-35`). The character pass never overwrites a term it already has (`passes.py:101`). In the retired copy the only `say` entries are Lauriane and Laurianne as "Laurie Anne" and Louis as "Lewis" (`library/the-hundred-reigns/bible/pronunciations.yaml:1-5`, `:250-257`).
* `bible/characters.yaml`: id -> name, aliases, gender, age_group, role, description, voice_brief, importance, facts by chapter, voice_id, bio (`README.md:11`, `schemas/character_pass.schema.json:30-50`, `passes.py:85-97`).
* `voices/<id>.yaml`: the cast clip and its `sample_text` (`breeze.py:137-154`).
* `serial.yaml`: `narrator_voice_id`, `voice_effects`, `render_engine`, `breeze_runner` (`render.py:32`, `render.py:67`, `render.py:222`, `breeze.py:562`).

### Other files in the chapter folder

* `breeze_join_job.json`: output, cap_seconds, and takes as {line, file, voice, effect} (`breeze.py:773-777`).
* `breeze_server.log`, appended on each render (`breeze.py:317`, `breeze.py:701`). Per take it logs the mode (clone or direction), the text's length in UTF-8 bytes, the cfg, the seed, a progress bar that starts from the server's own length estimate, the frame count at 80 ms per frame, and the time to first audio (`library/the-hundred-reigns/chapters/0007/breeze_server.log:4`, `:9-15`, `:57`, `DECISIONS.md:59`). The first take there was estimated at 2.7 s and came out 4.7 s, 59 frames (`breeze_server.log:10-11`). The pipeline reads none of this log.
* `render.log`: one per render run (`library/the-hundred-reigns/chapters/0007/render.log`).

### What a new check could use

* The expected text of each take, rebuilt exactly as the check rebuilds it (`takecheck.py:69-71`).
* Speaker, voice and direction, from the take entry and the script line.
* A list of names: the pronunciation terms and their `say` spellings, and the characters' names and aliases. These mark the name tokens among the expected words. The `respelling` gives the intended stress in capitals, which no code uses for the voice today.
* The voice id of every take across chapters, for a baseline per voice (level, rate, pitch).
* The take's audio, on its node only (section 5).
* The transcript, as plain text only (section 4).
* The key and audio hash, so a new verdict can be cached per take the way transcripts are (`takecheck.py:82-85`).

---

## 3. Emphasis, directions and line length

### The source parsers

* Royal Road: `<em>` and `<i>` become `*` markers in the chapter text, because "the screenplay pass uses them to spot unquoted thoughts" (`serial_audio/royalroad.py:208-215`, `royalroad.py:247-252`, test `tests/test_text.py:22`, `:41`). An empty or punctuation-only italic run is dropped (`royalroad.py:305-308`). `handle_starttag` has no branch for `<strong>` or `<b>` (`royalroad.py:174-224`), so bold is lost and its words are kept. Capitals pass through unchanged (`royalroad.py:295-296`). A blockquote becomes `> ` lines and `<hr>` becomes `* * *` (`royalroad.py:9-10`, `royalroad.py:196-201`).
* EPUB: "Output is plain paragraphs: no markup" (`serial_audio/epub.py:6`). `handle_starttag` has no branch for em, i, b or strong (`epub.py:65-88`), so all emphasis is lost and the words are kept. Capitals in the body are kept. Only an all-caps heading is title-cased (`epub.py:129-133`, `epub.py:169-179`).
* Measured in the retired copy: `*` italic markers on 8 to 34 lines of each of the 12 Hundred Reigns `text.md` files (Royal Road), and none in the 8 Twenty Thousand Leagues files (EPUB).

### The screenplay pass

* "Italics may be marked `*like this*`. The asterisks are never spoken: drop them." An unquoted italic sentence that reads as the point-of-view character's thought becomes that character's line with the direction `thought, quietly`. "italics used for emphasis, titles, or set-off text stay with whoever speaks the surrounding words" (`prompts/screenplay_pass.system.md:14`). Nothing tells the pass to carry the emphasis on.
* "Keep italics, capitalization, and foreign phrases as they are" (`screenplay_pass.system.md:30`), which sits beside the rule above to drop the asterisks. Measured: no `text` of any script line in the retired copy holds an asterisk. Asterisks appear only in `notes`. So italic emphasis is gone by the script. Capitals survive, for example "Anna! ANNA!" (`library/the-hundred-reigns/chapters/0011/script.json:905`).
* A `> ` system notification becomes a narrator line with the direction `system notification, flat and formal` (`screenplay_pass.system.md:15`).
* Directions: only when the text says how a line is delivered, two to five words, most lines without one, never on narration unless the narrator quotes a sound (`screenplay_pass.system.md:32-34`). Measured: 633 of 2,651 Hundred Reigns lines have a direction, 301 distinct, led by "thought, quietly" (84), "system notification, flat and formal" (35) and "chuckling" (11).
* The schema allows `speaker`, `text` and `direction` only (`schemas/screenplay_pass.schema.json:9-15`). There is no field for emphasis, stress or a pronunciation.
* The pass gets up to 40 pronunciation entries "for reference" (`screenplay.py:31-33`), and the prompt says the list "is for the voice, not for you to rewrite" (`screenplay_pass.system.md:30`).

### What reaches the voice

`speakable` leaves asterisks and capitals in place (`breeze.py:103-111`). So capitals reach Breeze, and italics would
reach it if a script kept them, which none does. The direction is the only performance control
(`breeze.py:114-115`). The repo does not say how Breeze reads capitals.

### Line length

* The prompt: one narrator line per paragraph, a paragraph longer than about eighty words split at sentence boundaries, never inside a sentence, and one utterance per dialogue line however short (`screenplay_pass.system.md:18-20`).
* No code limits the words in a line. `validate_script` does not look at length (`screenplay.py:42-64`). The hard limits are the 120 s take cap (`breeze.py:44`), max_new_tokens 1500 (`breeze.py:56`) and the length guard's longest bound of 2.25 + 0.75 x w s (`breeze.py:66`).
* Measured: the longest line has 99 words (Hundred Reigns chapter 12, line 30), and 8 Hundred Reigns lines and 5 Twenty Thousand Leagues lines run over 80 (section 8).
* Long lines fail worst: two flagged lines of 58 and 64 words had lost 25 and 30 words (`DECISIONS.md:46`).

---

## 4. The recognizer

* Parakeet TDT 0.6B v2 int8 through sherpa-onnx: `OfflineRecognizer.from_transducer`, `model_type="nemo_transducer"`, `decoding_method="greedy_search"`, 4 threads, on the CPU (`serial_audio/parakeet_asr.py:42-47`, `asr.py:3-5`, `asr.py:25`). Each file is resampled to 16 kHz mono with `resample_poly` (`parakeet_asr.py:19`, `parakeet_asr.py:29-39`) and decoded in its own stream (`parakeet_asr.py:63-68`).
* It reads only `stream.result.text` (`parakeet_asr.py:69`) and emits one JSON line per file, `{"index", "text"}` or `{"index", "error"}`, in ASCII (`parakeet_asr.py:7`, `parakeet_asr.py:23-26`, `parakeet_asr.py:69-71`). The parent passes on only the text (`asr.py:160`). No hotwords or context are passed (`parakeet_asr.py:44-47`).
* Qwen3-ASR: a ComfyUI graph, LoadAudio, Qwen3ASRLoader, Qwen3ASRTranscribe with language "auto", context "" and 4096 new tokens at most, then PreviewAny (`serial_audio/comfy.py:233-242`). The code joins the text and keeps nothing else (`asr.py:107-108`). The files must lie under ComfyUI's output folder (`asr.py:95-102`), and it needs the card (`DECISIONS.md:46`).
* Tokens, timestamps or confidences: no code reads them. The repo does not say whether the sherpa-onnx 2.0.0 installed on the laptop (`docs/machines.md:62`) exposes them for this model.
* Word timings: none anywhere. A search of the `.py`, `.md` and `.js` files for timestamp, tokens, confidence and word timing found nothing tied to audio. The only timing below a chapter is Drama 3's line spans from its codes file (`fx.py:53-77`), which Breeze renders do not have. The join does not record where each take lands in the chapter (`breeze_join.py:118-120`).

---

## 5. Take audio

* A `cpp` take is a mono 16-bit PCM WAV (`breeze.py:446-451`, `breeze.py:726`) at the rate breeze-server reports on `/health`, 24000 Hz when it reports none (`breeze.py:335`). The server answers with s16le PCM (`breeze.py:357-358`). The retired logs show "backend: Vulkan0, sample rate: 24000" (`library/the-hundred-reigns/chapters/0007/breeze_server.log:4`) and "joined 182 takes into 1146.7 s at 24000 Hz" (`library/the-hundred-reigns/chapters/0007/render.log:7`).
* A `comfyui` take is whatever ComfyUI's SaveAudio writes (`breeze.py:182`, `breeze.py:722`). The repo does not say its format.
* Where takes live: `<output>/breeze/<slug>/chNNNN/cpp/lNNNN.wav`, and a retake as `lNNNN.retake.wav`, moved over the take when it is kept (`breeze.py:709`, `breeze.py:725`, `breeze.py:753`, `breeze.py:498`). The output folder is `D:\comfy\ComfyUI\output` on the laptop (`serial_audio/machine.py:31`) and `/Users/aj/serial-audio/output` on the Mac (`docs/machines.md:87`, `docs/machines.md:95`). Takes stay on the node that made them, and a rerun on another node renders every line again (`deploy/README.md:196`). The app holds only the takes a check did not pass, in `state/media/` (`deploy/README.md:187-188`). On 2026-11-11 the laptop held 2.06 GB of takes in 7,409 files (`DECISIONS.md:208`).
* Final audio: mono, written by `fx.write_audio` through soundfile with `format="OGG", subtype="OPUS"` and no bitrate or other setting (`fx.py:80-89`). Breeze audio stays at 24 kHz because 24000 is an Opus rate (`fx.py:33`, `DECISIONS.md:32`). The bitrate is soundfile's default, and the repo does not say what that is. The file goes to `<output>/<slug>/chNNNN_breeze.opus` and is copied to `library/<slug>/audio/chNNNN.opus` (`breeze.py:770-785`).
* Level: -18 dB of active-speech RMS and a soft limit toward 0.99 (1.6).

---

## 6. Timing and capacity

| | aj-legion5 (laptop) | jsc-m1 (Mac) |
|---|---|---|
| Hardware | RTX 3070 Laptop 8 GB, 64 GB RAM (`docs/machines.md:10`) | M1, 8 cores, 16 GB (`docs/machines.md:78`) |
| Breeze render speed | about 2x realtime (`DECISIONS.md:17`), 2.8 s per take (`DECISIONS.md:115`, `JOURNAL.md:13`) | about 0.4x realtime, 17.5 s per take (`DECISIONS.md:115`), plain 0.39x and directed 0.31x (`docs/machines.md:128`), about 18 s per take (`JOURNAL.md:15`) |
| Scheduler's default seconds per take | 3.0 (`serial_audio/web/nomad.py:49`) | 18.0 (`nomad.py:49`) |
| breeze-server memory | about 3.8 GB of VRAM (`docs/machines.md:37`, `README.md:170`) | the repo does not say |
| ComfyUI Breeze runner | about 7.7 GB at 0.5x realtime (`README.md:171-173`) | none |
| breeze-server start | warmed up in 2.0 s (`library/the-hundred-reigns/chapters/0007/breeze_server.log:6`) | about 40 s of Metal shader compiling (`docs/machines.md:81`) |
| Check per take | Qwen3-ASR 2.3 to 3.4 s on 2026-11-07 (`library/the-hundred-reigns/chapters/0007/render.log:9-15`, `library/the-hundred-reigns/chapters/0010/render.log:10-18`). Parakeet: three takes in 4.8 s with the model load (`docs/machines.md:72`) | the repo does not say |
| Audio Python | ComfyUI's embedded Python 3.13 with torch 2.14 for CUDA 13.0 (`docs/machines.md:14-15`), numpy, scipy, librosa and soundfile (`CLAUDE.md:47`, `fx.py:4`), sherpa-onnx 2.0.0 (`docs/machines.md:62`) | a venv of Homebrew Python 3.12 with pyyaml, numpy, scipy, librosa, soundfile and sherpa-onnx (`docs/machines.md:85`) |

* The scheduler costs the join at 90 s and the check at 0.5 s per take (`nomad.py:54-55`, `nomad.py:186-188`).
* Measured from the retired takes on the laptop: the wall time to make a take had a median of 2.0 s, a 90th percentile of 7.4 s and a maximum of 14.88 s (section 8).
* Rules: one GPU job per machine, and nothing that takes VRAM on the laptop while a chapter generates (`CLAUDE.md:40`), enforced with a machine-wide lock (`deploy/README.md:149`). While ComfyUI renders, the laptop's RAM is nearly full (`CLAUDE.md:89`).
* The CLI, and so `breeze.py`, runs under the node's `serial_audio_python` (`serial_audio/worker.py:260`, `deploy/README.md:125`). The laptop's user Python has no numpy (`CLAUDE.md:46`). Audio work runs as a subprocess under the audio Python, as `breeze_join.py` and `parakeet_asr.py` do (`breeze.py:778`, `asr.py:121`).
* `requirements.txt` lists only anthropic and pyyaml (`requirements.txt:1-2`). The containers image adds only pyyaml (`CLAUDE.md:46`).

---

## 7. Quality problems on record

| Date | Problem | What was done | Source |
|---|---|---|---|
| 2026-11-05 | AJ found the first Qwen VoiceDesign cast too young across the board | Qwen briefs name an older age and a low register | `DECISIONS.md:136-137`, `docs/casting.md:17` |
| 2026-11-07 | Names: "AJ: Louis is always said "Lewis", and Lauriane is always said "Laurie Anne"" | Say-as overrides in the bible, applied only to the text sent to the voice | `DECISIONS.md:50-53` |
| 2026-11-07 | Breeze sometimes loops, repeating a word, or drops the end of a line, and the length guard catches only the gross cases | AJ asked for the transcript check. Every Breeze render checks its takes | `DECISIONS.md:36-39` |
| 2026-11-08 | AJ heard occasional loud clicks and pops in Breeze chapters 7 to 10: bursts under 1 ms, about 20 to 50 a chapter, and takes that start at full level | Clicks filled from an autoregressive model, 3 ms fades. AJ: "repaired samples sound fine". A review found the detector also softens some sharp word-final stop releases, which stays until AJ hears a dull consonant | `DECISIONS.md:57-60` |
| 2026-11-08 | Trial notes: one Keeper scene dropped the end of two lines, and Parakeet heard "Yes." as "Yeah." | Trial only, nothing filed | `docs/casting.md:205` |
| 2026-11-08 | A Breeze clone of a whispered clip drifts back toward a full voice on about half its lines | A whisper direction on every line held it, in a scratch script | `docs/casting.md:24` |
| 2026-11-09 | AJ found a new Lorimor candidate quiet beside the other characters. Takes come out at the level of their cast clip, -11 to -29 dB. In chapter 17 old Lorimor takes sat at -12.6 dB against -26.1 dB for the narrator | The join gives each voice one gain per chapter | `DECISIONS.md:141-144`, `docs/casting.md:31` |
| 2026-11-09 | AJ said Casval and Anna sound too old for the academy arc | Recast with Breeze at story age, chapters 11 and 17 rendered again | `DECISIONS.md:136`, `docs/casting.md:161-180` |
| 2026-11-09 | AJ heard echo or reverb on Casval's new voice in chapter 11, "like he's in a room with hard walls and has stepped back from the mic" | Three scratch rounds, then seed 22200 from a brief with "clear" and "speaking at a normal conversational volume". No measurement told a roomy clip from a dry one | `docs/casting.md:124-144`, `docs/casting.md:19-20` |
| 2026-11-09 | AJ read Lorimor's bio and heard no fanaticism or madness in the voice | Recast, seed 19000 | `docs/casting.md:148-157` |
| by 2026-11-10 | The Keeper's seed 13000 clip had a run of 6 full-scale samples, and its chapter 11 lines sounded clipped after the join leveled them (who heard it is not stated) | Since 2026-11-10 a cast marks a candidate with 3 or more full-scale samples in a row as clipped | `docs/casting.md:33` |
| 2026-11-11 | 17 of 7,602 takes in 34 checked chapters were flagged, about one every two chapters, and the check only listed them | A flagged take is taken again before the join | `DECISIONS.md:43-46` |
| 2026-11-11 to 2026-11-12 | AJ kept hearing a French accent on Argas Septic across seven casts | A scratch trial of six briefs, AJ picked seed 41402 | `docs/casting.md:41-67` |
| undated | AJ hears a slight southern accent in leticia-septic's seed 26002 | None recorded | `docs/casting.md:35` |
| undated | AJ turned down "weathered, gravelly" for a church leader | Wanted a performative voice | `docs/casting.md:25` |

Open as of 2026-11-11: chapters 7, 8 and 10 of The Hundred Reigns have replaced voices and were joined before the
per-voice leveling (`DECISIONS.md:204`), and chapters 1 to 6 are Drama 3 renders with older voices
(`DECISIONS.md:202`). `JOURNAL.md` holds scheduling history only and records no listening problem
(`JOURNAL.md:1-42`).

---

## 8. Measured data from the retired library

`D:\fish\library` is the copy retired on 2026-11-07 (`DECISIONS.md:67`), read here and not changed. It holds four
`take_check.json` files, chapters 7 to 10 of The Hundred Reigns, all made by Qwen3-ASR on 2026-11-07, in the older
format without `asr` and `audio`. None of its takes is flagged, so the flagged table is empty. The live library's
figure is 17 flagged of 7,602 (`DECISIONS.md:45`).

The script is `C:\Users\aj\AppData\Local\Temp\claude\1e6b582862f5269e\6e735ee5-1cfc-4e77-9211-18eef7d7669b\scratchpad\take_stats.py`,
run with `python -B` so it writes no bytecode into the repo. It imports `asr.words`, `breeze.speakable`,
`breeze.count_words`, `breeze.take_bounds`, `render.apply_say_as`, `render.say_as_map`, `takecheck.align` and
`takecheck.classify`, and reads only. Its output, quoted in full from
`C:\Users\aj\AppData\Local\Temp\claude\1e6b582862f5269e\6e735ee5-1cfc-4e77-9211-18eef7d7669b\scratchpad\take_stats_output.txt`:

```
== 1. take_check.json verdicts (as stored)
  the-hundred-reigns ch0007 model=qwen3_asr_1.7b_bf16.safetensors asr=None takes=182 verdicts={'ok': 173, 'suspect': 8, 'unreadable': 1}
  the-hundred-reigns ch0008 model=qwen3_asr_1.7b_bf16.safetensors asr=None takes=169 verdicts={'ok': 161, 'suspect': 8}
  the-hundred-reigns ch0009 model=qwen3_asr_1.7b_bf16.safetensors asr=None takes=227 verdicts={'ok': 223, 'suspect': 4}
  the-hundred-reigns ch0010 model=qwen3_asr_1.7b_bf16.safetensors asr=None takes=246 verdicts={'ok': 239, 'suspect': 7}
  TOTAL takes=824 ok=796, suspect=27, unreadable=1, flagged=0
  stored verdict -> verdict under the current code (classify on today's words() and say map):
    ok         -> ok         796
    suspect    -> suspect    27
    unreadable -> unreadable 1

== 2. Script words the recognizer got wrong in suspect and flagged takes
  entries: 27, whose stored sub/del/ins match takecheck.align on today's words(): 27
  wrong script words: 39 ({'sub': 33, 'del': 6})
  by kind: {'bible name': 24, 'other': 10, 'capitalized': 5}  (bible name = a capitalized word of a character name or alias, or a word of a pronunciation term or say-as, capitalized = only ever capitalized mid-sentence in the scripts)
  most often wrong script words (word: count, kind):
    casval: 6 bible name, duchar: 4 bible name, thalas: 4 bible name, mister: 4 capitalized, adrissant: 4 bible name, anna: 2 bible name, forneus: 2 bible name, demonbarrows: 1 other, oh: 1 other, antonine: 1 bible name, shax: 1 bible name, fianc: 1 other, e: 1 other, scalefolks: 1 other, youths: 1 other, patriate's: 1 other, y: 1 other, thal: 1 capitalized, ass: 1 other, hey: 1 other
  suspect or flagged takes with an empty transcript: 3 [('0007', '95', 'Oh…'), ('0007', '170', 'Thalas?'), ('0010', '221', 'Hey!')]
  most common substitutions (script -> heard: count):
    mister->mr: 4, duchar->duchard: 3, casval->kazval: 3, anna->eda: 2, thalas->thalos: 2, adrissant->adrissin: 2, adrissant->adrassant: 2, forneus->fornius: 2, demonbarrows->barrows: 1, antonine->antonyne: 1, shax->chasse: 1, fianc->thalass's: 1, e->fiance: 1, scalefolks->folks: 1, casval->caswell: 1, duchar->douchard: 1, youths->youth: 1, casval->kaysval: 1, casval->caswall: 1, patriate's->patriots: 1, ass->thalos: 1
  across ALL checked takes (ok included), substituted script words by kind: {'bible name': 192, 'other': 83, 'capitalized': 21}
    top: thalas: 55, duchar: 27, casval: 21, kish: 7, lewis: 6, mister: 6, adrissant: 6, balzam: 5, magnos: 5, telluria: 5, e: 5, dassein: 5, demonbarrows: 4, fianc: 4, shadowchain: 4, forneus: 4, patriate: 4, beastmen: 3, a: 3, duchar's: 3
  examples (chapter, line, verdict, words, wer, script, heard):
     ('0007', '21', 'suspect', 2, 0.5, 'Duchar chuckled.', 'Duchard chuckled.')
     ('0007', '24', 'suspect', 3, 0.667, 'What about demonbarrows?', 'What about demon barrows?')
     ('0007', '95', 'suspect', 1, 1.0, 'Oh…', '')
     ('0007', '104', 'suspect', 2, 0.5, 'Duchar explained.', 'Duchard explained.')
     ('0007', '145', 'suspect', 2, 0.5, 'Anna said.', 'Eda said.')
     ('0007', '148', 'suspect', 14, 0.357, 'Simon snorted. Antonine de Shax was Thalas’ fiancée, and a spoiled, arrogant bitch.', "Simon snorted. Antonyne de Chasse was Thalass's fiance, and a spoiled, arrogant bitch.")
     ('0007', '168', 'suspect', 2, 0.5, 'Prince Thalas?', 'Prince Thalos.')
     ('0007', '170', 'suspect', 1, 1.0, 'Thalas?', '')
     ('0008', '40', 'suspect', 2, 0.5, 'Anna said.', 'Eda said.')
     ('0008', '76', 'suspect', 3, 0.667, 'Mister Adrissant explained.', 'Mr. Adrissin explained.')
     ('0008', '85', 'suspect', 3, 0.667, 'Mister Adrissant explained.', 'Mr. Adrissin explained.')
     ('0008', '99', 'suspect', 3, 0.667, 'Mister Adrissant said.', 'Mr. Adrassant said.')

== 3. Words per script line (breeze.count_words on the text Breeze is sent)
  the-hundred-reigns: 2651 lines in 12 scripts, {'narrator': 1362, 'character': 1289}, 633 with a direction
    mean 16.3, median 9.0, p90 43.0, p99 71.0, max 99 (chapter 0012 line 30)
    buckets: {'0': 1, '1-1': 111, '2-5': 715, '6-10': 619, '11-20': 478, '21-40': 423, '41-60': 232, '61-80': 64, '81-100': 8}
    over 80 words: 8; take_bounds(99) = (11.88, 76.5) s
  twenty-thousand-leagues: 35 lines in 1 scripts, {'narrator': 34, 'character': 1}, 1 with a direction
    mean 57.2, median 60.0, p90 84.2, p99 87.7, max 88 (chapter 0001 line 17)
    buckets: {'2-5': 1, '6-10': 1, '21-40': 4, '41-60': 14, '61-80': 10, '81-100': 5}
    over 80 words: 5; take_bounds(88) = (10.56, 68.25) s

== 4. breeze_takes.json entries
  entries 824, field counts {'key': 824, 'file': 824, 'voice': 824, 'direction': 824, 'seconds': 824, 'seed': 659, 'takes_tried': 1}
  takes_tried {1: 823, 2: 1}, out_of_range 0, retaken 0, with a direction 155
  'seconds' (wall time to make the take): median 2.0, p90 7.4, max 14.88

== 5. breeze_server.log: generated take lengths (frames x 80 ms) against the chapter's lines
  gen records 839, matched to one line by UTF-8 length 414
  against take_bounds: {'inside': 412, 'above': 2}
  seconds per word (lines of 5+ words): median 0.4, p5 0.3, p95 0.5, min 0.229, max 0.723
```

A second script,
`C:\Users\aj\AppData\Local\Temp\claude\1e6b582862f5269e\6e735ee5-1cfc-4e77-9211-18eef7d7669b\scratchpad\script_fields.py`,
printed: all 2,686 script lines have exactly the keys direction, speaker and text, there are 301 distinct
directions, and breeze-server's "N chars" is the UTF-8 byte length (chapter 7 line 1 is 60 characters and 62 bytes,
logged as "62 chars").

### Reading the numbers

* Verdicts: 796 ok, 27 suspect, 1 unreadable, 0 flagged in 824 takes. Today's rules give the same verdict for every one, and the stored sub, del and ins counts match `takecheck.align` for all 27 suspects.
* Every non-ok take, read by hand from the same files: 21 of the 27 suspects are a name heard another way (Casval in 6 takes, Duchar 4, Adrissant 4, Anna 2, Thalas 2, Forneus 1, Patriate 1, and one line with Antonine de Shax and Thalas). 2 are invented compounds the recognizer split ("demonbarrows", "scalefolks"). 3 are one-word lines with an empty transcript ("Oh…", "Thalas?", "Hey!"). 1 is a stutter, "Y-yes," heard as "Yes.". The unreadable take is "The kish?" transcribed in Devanagari script (`library/the-hundred-reigns/chapters/0007/take_check.json`, the entry for script line 34).
* Proper names among the 39 wrong script words: the script's test finds 24 in the bible's names, aliases and pronunciation terms. By hand, "patriate's" (a title in the pronunciation list, `library/the-hundred-reigns/bible/pronunciations.yaml:54-56`, missed because of the possessive) and "thal" (half of the pun "Thal-Ass" on Thalas) are names too, which makes 26. The other 13: "mister" 4 (the transcript wrote "Mr."), "fianc" and "e" 2 (the split of "fiancée"), "demonbarrows" and "scalefolks" 2 (invented words), "oh" and "hey" 2 (empty transcripts), "youths" 1, "y" 1 (stutter), "ass" 1 (the other half of the pun).
* The ok takes carry names wrong too: 192 of the 296 substituted script words across all 824 takes are bible-name words, led by Thalas (55), Duchar (27) and Casval (21). The 0.34 word error rate absorbs most of them, so the check does not see names.
* One name, many spellings: Casval was heard as kazval (3), caswell, kaysval and caswall, and Adrissant as adrissin (2) and adrassant (2). The repo does not say whether that spread is in the audio or in the recognizer.
* Words per line: the Hundred Reigns median is 9 and the 90th percentile 43, and 72 of its 2,651 lines run over 60 words. The Twenty Thousand Leagues median is 60, because it is mostly narration.
* Take lengths: of the 414 generated takes matched to a line, 412 fell inside the length bounds and 2 above. Speech ran at a median of 0.4 s per word (5th to 95th percentile 0.3 to 0.5), against bounds of 0.12 s per word below and 0.75 s per word plus 2.25 s above, so the guard sits far from normal speech on both sides.

---

## Where new per-take checks could attach

1. Beside the length guard in `cpp_take` (`breeze.py:420-432`). It holds the PCM, the sample rate, the word count and the seed, and a failed check can roll a new seed at once, for one take's cost (about 2.8 s on the laptop, 17.5 s on the Mac, section 6). It runs under the node's Python, which on the laptop has no numpy (`CLAUDE.md:46`), so a check there must use the standard library, as `casting.full_scale_run` does (`casting.py:320-343`), or call the audio Python as a subprocess.
2. In the check before the join (`breeze.py:454-517`). It runs under Parakeet on the CPU while breeze-server holds the card. A new verdict there gets retakes only if the filter at `breeze.py:485` takes it in, and the keep-if-closer rule ranks it through `VERDICTS` and `badness` (`takecheck.py:26`, `takecheck.py:88-90`). Any verdict other than ok already reaches the chapter page and ships its audio to the app (`views.py:301-317`, `worker.py:205-207`).
3. In `classify` and `words` (`takecheck.py:47-66`, `asr.py:294-301`): a rule that knows which expected words are names, from the bible, and normalization for "Mr." against "Mister" and for letters outside a to z.
4. In `parakeet_asr.py` (`parakeet_asr.py:63-71`): the emitted record could carry more than the text. `asr._parakeet` yields only the text (`asr.py:160`), so it would have to pass the rest on, and `take_check.json` could store it.
5. In the join (`breeze_join.py:85-117`), under numpy, scipy and librosa: each take's audio, its active-speech level, already computed (`breeze_join.py:106`), its click count (`breeze_join.py:93-94`) and its pauses before trimming. The summary could return per-take measurements and offsets. Inferred from the order in 1.1: breeze-server has stopped by then (`breeze.py:759-761`, `breeze.py:768`), so a finding there cannot take a line again without starting breeze-server again.
6. As a job of its own, like `check-takes` (`takecheck.py:175-225`): after the fact, on any chapter, reusing what is cached. On the cluster such a job runs on the node that holds the takes (`deploy/README.md:180`).
7. At script time, in `validate_script` (`screenplay.py:42-64`) or the schema (`schemas/screenplay_pass.schema.json:9-15`), for line length or for emphasis, which the script cannot carry today (section 3).
