# Decision record: audiogen

Decisions for this repo, current state only. Each entry has the date, the context and the decision. Check the
existing entries for a conflict before adding one, and rewrite an entry when AJ changes it.

---

### Two LLM backends: the Claude subscription first, the API as the fallback
**Date:** 2026-10-16
**Context:** AJ asked for both at the start.
**Decision:** The character pass, the casting pass and the screenplay run through headless Claude Code under AJ's login by default (`--backend auto`). On a usage-limit message they fall back to the Anthropic API, wait for the reset, or fail, per `--on-limit`. Every chapter records which backend ran. On the cluster the API fallback can't run, because `/usr/bin/python3` on dev-arch doesn't have the `anthropic` package (checked 2026-11-09), so a pass that hits the limit with `--on-limit api`, the app's default, fails. AJ said on 2026-11-09 to leave that until it causes a problem.

---

### Breeze TTS 2 through breeze-server is the default renderer
**Date:** 2026-11-07
**Context:** After a side-by-side test on chapter 6 of The Hundred Reigns, AJ said "I like breeze a bit better than drama". After hearing the Breeze-TTS-2.cpp Vulkan build, he said "Vulkan directed sounds great. Let's make that the default". On the laptop it renders at about 2x realtime, against about 0.25x for Drama 3.
**Decision:** `render` uses Breeze with the `cpp` runner unless `serial.yaml` names another engine or runner. A line with a direction uses Voice Direction, and every other line uses a plain clone of the speaker's cast clip. AJ heard little difference between the two, with a slight preference for directed. Drama 3 stays available with `--engine drama3`. Chapters 1 to 6 of The Hundred Reigns are Drama 3 renders.

---

### breeze-server is built from source
**Date:** 2026-11-07
**Context:** Windows Defender flagged the Breeze-TTS-2.cpp v0.1.6 Windows release zip as Trojan:Win32/Malgent. The maintainer uploads that zip by hand, with no CI build and no attestation. A review of the source rated it low risk.
**Decision:** Every breeze-server is built from tag v0.1.6 (commit 87b43667) with its pinned submodules: ggml 36da5713 and shine ab5e3526. The Mac's build adds one upstream ggml commit, `deploy/macos/ggml-metal-left-pad.patch` (`docs/machines.md`).

---

### Every render writes Ogg Opus
**Date:** 2026-11-07
**Context:** AJ: "this should always output .opus files instead of .flac files".
**Decision:** Both engines write `audio/chNNNN.opus`. Breeze audio stays at 24 kHz, and Drama 3 audio is resampled to 48 kHz, because Opus takes only a few sample rates. The Breeze join asks for 40 kbps, and a Drama 3 render leaves the bitrate to the encoder.

---

### Every Breeze render checks its takes
**Date:** 2026-11-07
**Context:** Breeze sometimes loops, repeating a word, or drops the end of a line. The take-length guard catches only the gross cases. AJ asked for the transcript check to be part of the process.
**Decision:** After the join, `render` transcribes each new take and aligns it with the script line. A take with three or more inserted or dropped words is flagged. The result goes to `take_check.json` and `render.check`, and the chapter page lists the takes to listen to. When no recognizer is available, the check is recorded as skipped. It doesn't fail the render.

---

### A render takes a flagged take again before the join
**Date:** 2026-11-11
**Context:** The take check ran after the join and only listed the flagged takes for AJ to listen to, and he listens while driving. In the 34 checked chapters of The Hundred Reigns, 17 of 7,602 takes were flagged, about one in every two chapters. Claude proposed taking them again, and AJ said "good idea".
**Decision:** After the takes and before the join, a Breeze render under breeze-server transcribes its new takes with Parakeet while the server is still up. It takes a flagged line again with new seeds, in up to two rounds, and keeps a new take only when its transcript is closer to the line. A take from an earlier render is taken again only when an earlier check flagged it (the next entry). With Qwen3-ASR as the recognizer nothing is taken again, because Qwen3-ASR needs the card that breeze-server holds. A saved take records a hash of its audio, and the check reuses a transcript only for the same audio, because a take that is made again keeps its key. In a scratch copy on the laptop on 2026-11-11, the eight flagged takes of chapters 16, 19, 24, 27, 36 and 37 came out flagged again from their seeds, and the first new take replaced each one. Two of the eight were lines of 58 and 64 words that had lost 25 and 30 of them.

---

### A render takes a saved take again when a check flagged it
**Date:** 2026-11-12
**Context:** AJ heard a garbled narrator line in chapter 33 of The Hundred Reigns at 8:55. The take check had flagged it, but the chapter was rendered before the retake before the join reached containers, and a render checked only the takes it made, so no later render would replace it. Ten flagged takes sat in nine rendered chapters. AJ said yes to a way to take them again.
**Decision:** Under breeze-server with Parakeet, a render puts a saved take through the check before the join when `take_check.json` holds a flagged verdict for that take and that audio. Parakeet hears the saved take again, and when it is still flagged the line is taken again with new seeds, and a new take replaces the saved one only when it is closer to the line. The saved take is not made again from its own seed first, because on the other node or without bit-exact seeds the remake would differ and could replace a better take unseen. Every line taken again records its last round as `retake_rounds`, and a line that has been through every round is left alone, because the same seeds would give the same takes. A join-only run changes nothing, and neither does a machine whose Parakeet cannot run. In a scratch copy on the laptop on 2026-11-12, with its own copies of the takes, a render of chapter 33 made no line again, heard the saved take of line 72 as flagged, and kept the first new take, whose transcript matched the line but for the name Eole. A second render took nothing. After the deploy on 2026-11-12 the nine chapters were rendered one at a time on an empty queue, about a minute each on the laptop. Nine flagged lines got a new take in the first round, and line 62 of chapter 30 was made new with 14 other lines whose takes were out of date. The library then held no flagged take in 9,383.

---

### A render keeps the chapter's seed
**Date:** 2026-11-12
**Context:** The app queued a render with seed 1 unless the request named one, and the scheduler counted a render's reuse with seed 1 too. Chapters 13, 16 and 22 of The Hundred Reigns were rendered with seeds 3, 2 and 2, so a render from the chapter page would have made every one of their takes again. This is package S1 of `docs/take-quality/phase-1.md`.
**Decision:** A render that doesn't name a seed uses the seed in the chapter's `render` record when the last render was Breeze, and 1 otherwise. `breeze.render_seed` decides it for the render and for the scheduler's estimate. The app passes `--seed` to the CLI only when the request names one. The seed box of the Process dialog is empty by default, and the batch an import queues doesn't name a seed. A Drama 3 render still uses 1. In a scratch copy on 2026-11-12, chapters 13, 16 and 22 counted 229, 223 and 166 reusable takes of as many without a seed, and none with seed 1.

---

### A take has one evidence record, and one function judges it
**Date:** 2026-11-12
**Context:** The check kept a take's transcript and its word counts, and threw away the recognizer's token times and everything the join measured. The checks AJ asked for on 2026-11-12 (silences, level, endings, names) all need that evidence, at the one point where a finding costs a single retake: before the join, while breeze-server is up. This is package F1 of `docs/take-quality/phase-1.md`, and the shared contract in `docs/take-quality/README.md` sets the form.
**Decision:** `take_check.json` keeps one entry per take with two versioned blocks. `asr` has the transcript and Parakeet's raw tokens with their start times, durations and log-probabilities. `signal` has what `take_measure.py` reads from the audio at its own sample rate. `takecheck.judge()` derives the word counts, the verdict, the reasons and the repairs from the blocks, and it uses the standard library only. The verdicts and the rule for a flag are the ones from 2026-11-07, so this change by itself doesn't flag a take that the old check passed. A saved entry is reused only when both blocks have the current version, so each of the 9,383 takes checked before this is heard again on its chapter's next render or check. A render hears those saved takes before the join, with the new takes, so a finding in one of them is taken again in the same render. That costs about two minutes of Parakeet on the laptop for a chapter of 190 takes, once per chapter. The rest of this entry is Claude's choice, and AJ hasn't confirmed it.

* A frame of 10 ms is speech when it is within 30 dB of the take's own K-weighted level. Over 626 takes of The Hundred Reigns the frame levels have two peaks, speech near the take's level and pauses about 48 dB below it, and the fewest frames lie 25 to 35 dB below. A silent run is 0.15 s or more without a speech frame, inside the take.
* The level is K-weighted over the speech frames, and the scale is set at each sample rate so that a full-scale 997 Hz sine reads -3.01 LKFS. Without that, the filter made for 24 kHz read 0.03 dB high.
* The names in a line's token map are the words of the bible's pronunciation terms and the capitalized words of each character's name. An alias is not a name, because the aliases include "Father", "Boss" and "the bastard".
* The spread and the largest step of loudness inside a take are measured between windows of one second, over the speech frames only. Over 626 takes, windows of 0.4 s gave a median spread of 10 dB and a median largest step of 15 dB, so ordinary speech filled the range. Windows of one second give a median of 6 dB for both, and 99th percentiles of 11 and 12 dB.
* A render takes lines again at most 8 times, or for an eighth of the chapter's takes when that is more, the worst lines first. A line goes on from the round its take reached in an earlier render, so no render asks for a seed a second time. A voice finding needs four flagged takes of the voice and over a quarter of its takes, for one reason. A take a voice finding covers records `retake_held`, and a later render leaves it alone. The numbers are starting points, to be set again when tracks A1 and A3 add their reasons.
* `take_check.json` has one line per take. Chapter 33 is 302 KB that way and 63 KB in a bundle's zip, against 723 KB with every token on a line of its own.

No take key changed. `VoiceTextTest` pins the keys of a fixed script, and on 2026-11-12 every planned key of the 10,724 lines in 48 chapters of The Hundred Reigns was the same before and after. A render would reuse 9,160 of the 9,383 saved takes before and after. The other 223 are lines of voices that were recast or cast after their chapters were rendered, in chapters 7 to 10, 15 and 18. In a scratch copy on the laptop, a render of chapter 33 without its take of line 72 heard the 188 saved takes before the join, made line 72 again from its seed, flagged it and kept the first new take, which is the take the library holds. Every entry then had both blocks, and a second check heard none of them. On 40 takes of chapter 38 the Mac and the laptop measured the same `signal` block for every take, and Parakeet gave the same transcript for 35. The other five differ in how a name is spelled, so a take's word counts can depend on the node that heard it.

---

### The take check flags on words, endings, short lines and the signal, and cuts long pauses
**Date:** 2026-11-12
**Context:** AJ asked on 2026-11-12 for checks on silences, mispronounced words, names said different ways and odd volume, and he can't review a flagged take, so each check has to end in a retake or a repair. These are packages A1 to A3 of `docs/take-quality/phase-1.md`. Every number here was set from the 9,383 takes of chapters 7 to 48 of The Hundred Reigns, heard by Parakeet on the laptop in a scratch copy on 2026-11-12. Under the rules of 2026-11-07 those takes had 216 suspect and 1 flagged.
**Decision:** `takecheck.judge()` flags a take for the reasons listed in `README.md`, Pipeline per chapter, step 5, and a render takes a flagged line again within the three limits. Over the same takes the rules flag 66 (short 24, swing 21, fading 14, ending 5, level 3, and 1 each for clipped, dropped, rate and substituted), 4 in a chapter at most and 1.6 on average, and leave 22 suspect. No voice comes near a voice finding. The rest of this entry is Claude's choice, and AJ hasn't confirmed it.

* A name in the line matches the recognizer's spelling of it when the spelling is within 0.75 of the name's, by edit distance over the longer of the two, in at most two heard words. Of the name substitutions in the first 1,539 takes, 90% were within 0.57 and the legitimate ones ran to 0.75 ("Eole" heard as "yo"). This turned about 190 suspect takes into passes. The entry records the spellings: Eole was heard 102 ways in 340 takes, Vouivre 88 ways in 158, Casval 33 ways in 247. The cost is that a name also stands for an unrelated short word in its place, so "Thalas" heard as "the" passes. A pronunciation term of one word is a name with its first letter in either case, and a word of a longer term only as the term writes it, so "Your Grace" is not the "grace" of "coup de grace".
* A line of one to three words is flagged for an added or a missing word, and a changed word only makes it suspect. The plan said to require an exact match. The changed words in short lines were the recognizer's homophones: "Simon guessed" came back as "Simon Guest" in five takes, with "read" as "Reed" and "Saber" as "Sabre". The added words were the voice's: "Ah, sure." for "Sure.", "Leonard replied. You." for "Leonard replied.".
* The alignment prefers the substitution between the more similar words, two words match one heard word that is their spellings run together or within 0.25 of it, a contraction matches its two words, and the fragment of a stutter is not a word. Each of these removed false flags that the first run showed.
* A pause is cut only when it is 0.6 s or longer, lies between two words of the line that were heard right, and has no punctuation in the script between them. It is cut to 0.25 s. Breeze pauses without punctuation about as readers do: of 7,031 silent runs placed between two words, 1,542 had no punctuation, with a median of 0.30 s and a 99th percentile of 0.70 s, against medians of 0.41 s at commas and 0.52 s at full stops. So only the longest are cut: 93 in 89 takes, 38 s in 42 chapters. The join applies the cuts in package B1.
* The signal thresholds sit near the 99.8th percentile of each measure, so each costs a handful of retakes a book: an ending 16 dB down (14 takes), a spread of 13.5 dB or a step of 16 dB between windows of one second (21 takes). The rate rule, 0.45 to 2.6 times the voice's median, flags 1 take, and it is there for a take that stops early. The level rule allows 12 dB from the voice's median after the direction's offset, because the takes furthest from their voice were muttered, whispered and shouted lines, and it flags 3.
* A direction's class comes from its words (`breeze.direction_class`), and the offsets are the plan's starting values: whisper -6 dB, soft -3, raised +2, shout +3. AJ sets them when he hears the clips of package B1.
* `take_check.json` records the version of the rules. A render judges the saved evidence again, without hearing it, when the rules are newer, so a new rule reaches a saved take on its chapter's next render. The chapter page and the `backlog` list judge again the same way. The scheduler's estimate of a render reads the stored verdicts, because judging a chapter again took about 1.2 s on the laptop and the estimate runs under the scheduler's lock.
* A vocal event in parentheses is not a word of the line, so a take that ends in "(laugh)" is not missing its ending. A line of one to three words that the recognizer hears nothing in is judged on its length alone, and its dropped words don't flag it.

In a scratch copy on the laptop, a render of chapter 33 judged its 189 saved takes again, took lines 39 and 41 again and kept the new take of both. Line 39 had come out as "Ah. Ah. I emancipated her." and its new take says the line. The chapter then had no flagged take and 1 suspect, against 4 before. Its one cut is a pause of 0.61 s between "corpse" and "at" in line 3.

---

### The take check's other models are Silero VAD v5, TitaNet small, Moonshine base and ZIPA small
**Date:** 2026-11-12
**Context:** Phase 2 of the take quality plan needs a speech detector, a speaker embedding, a second recognizer from another family than Parakeet and a phoneme recognizer, all on the CPU of both breeze nodes (package P0 of `docs/take-quality/phase-2.md`). None had a published speed on this hardware. The spike ran in scratch folders on both nodes on 2026-11-12, over 434 saved takes of The Hundred Reigns (24 takes of each of its 14 busiest voices, 40 lines of one to three words, 58 suspect or flagged takes) and 15 clips without speech (silence, pink and white noise, and the last 0.35 s of ten takes). Times are a share of the audio's length, with 4 threads, and 2 for the embeddings. The laptop was rendering scratch chapters for phase 1 then, so its times run high.
**Decision:** The four models in `docs/machines.md`, "The take check's other models". AJ approved the list on 2026-11-12 and chose to put the files on both nodes at once, and they are there. The rest of this entry is Claude's choice.

* Silero VAD v5 (0.018 on the laptop, 0.010 on the Mac) never fired on noise, and it put the end of speech within about 0.2 s of Parakeet's last token on 95% of takes. TEN VAD called white noise at -50 and -35 dB speech. The smaller Silero file in the sherpa-onnx release ran speech on about 0.9 s past the last token in takes where the others agree it had ended.
* TitaNet small told the 14 voices apart best: 97.3% of takes lie nearest their own voice's centre, with an equal error rate of 0.113, at 0.009 on both nodes and 40 MB. TitaNet large (96.4%, 0.025) and ERes2Net from VoxCeleb (97.0%, 0.026) did as well at three times the time. ERes2NetV2 (97.0% but a margin of 0.17 against 0.29), WeSpeaker ResNet34 (90.2%) and ResNet221 (71.1%) and both CAM++ models (72.3% and 29.2%) did worse.
* Moonshine base English, from 2026-02-27, had a WER of 0.042 on the sample takes the check judged ok, where 3.0% were over 0.2, at 0.028 on the laptop and 0.020 on the Mac. It wrote text for 1 of the 15 clips without speech. The Zipformer from GigaSpeech was close on words (0.036) and faster, and wrote "THANK YOU" on silence. Whisper base.en wrote text for all 15. Qwen3-ASR 0.6B runs on the CPU in sherpa-onnx 2.0.0, so the plan's reason against it no longer holds, but it was five to eight times slower than Moonshine and wrote Chinese for three take tails. Parakeet's own WER on those takes was 0.023, low because its check chose them. On the 58 suspect or flagged takes, both recognizers heard 42 wrong. Moonshine heard several of the others right where Parakeet split or joined words, "Simon guessed" as "Simon Guest" and "Her mark" as "Hermark".
* ZIPA small CR-CTC 300k loads in sherpa-onnx's Zipformer CTC loader from ZIPA's own ONNX export, so neither node needs torch or lhotse. It gives IPA with token times at 0.014 on the laptop and 0.009 on the Mac. The models trained with noisy students are CC BY-NC-SA, so the CC BY one is the pick.
* The output depends on the node. Of 449 clips, the laptop and the Mac gave the same transcript for 416 with Parakeet and 413 with Moonshine, and the same phones for 312 of 434 with ZIPA. P4 has to compare a take with the pick on the node that heard it, or allow for this.
* A scratch pass over all 9,383 takes, on the laptop's CPU in 27 minutes, gave the starting data for packages P1 to P3: Silero hears speech more than 0.3 s past the last token in 110 takes. Over takes with 3 s of speech or more and no direction, the takes furthest from their voice's centre across the book are Anna Paimon's lines in chapters 7, 8 and 10 and Leonard Decarabia's chapter 7 line 133, which were rendered before those voices were recast.

---

### The take check flags a tail, a drifting voice and a take both recognizers hear wrong, and cuts a tail
**Date:** 2026-11-12
**Context:** Packages P1 to P3 of `docs/take-quality/phase-2.md`: sound after a take's last word, a take whose timbre drifts from its voice, and a second opinion on the suspect takes. Every number here comes from the 9,383 takes of chapters 7 to 48 of The Hundred Reigns in a scratch copy on 2026-11-12, heard with the four models of the entry before this one: chapters 7 to 26 on the laptop and 27 to 48 on the Mac. Under the rules of phase 1 those takes had 66 flagged and 22 suspect.
**Decision:** `takecheck.judge()` gains three reasons and a cut (`README.md`, Pipeline per chapter, step 5), and `JUDGE_V` is 4. Over the same takes the rules flag 81 and leave 20 suspect: `tail` 3, `drift` 10 and `garbled` 2 beside phase 1's reasons, and 21 takes in 16 chapters get a cut of 11.7 s in all. Version 3 of the rules, deployed first on 2026-11-12, also cut the tail of a line directed "purring with delight", and version 4 adds purring and 18 more sounds a direction can ask for, such as cackling, choking and hissing. The rest of this entry is Claude's choice, and AJ hasn't confirmed it.

* A tail is the sound after the line's last word that Parakeet's token times place, by the signal block's speech frames or the speech detector's segments, whichever ends later. 596 takes have sound more than 0.3 s past that word and 77 more than 0.5 s.
* Words heard after the line count only when the sound goes on more than 0.5 s past its last word. At 0.3 s the rule flagged 6 takes in chapters 7 to 26, and 4 of them were the second recognizer's way of hearing a last word: "keep, Anna" as "keep on, huh?", "right…" as "right there". At 0.5 s it flags 3 in the book, and each is the voice going on: "The stranger was a woman. Freeze.", "Lady Shabram flipped to another page. Jabbas", "Duchar replied with a sigh. Fossen.". A short line with an added word was already flagged as short, so the tail rule leaves lines of one to three words alone.
* The cut of a tail needs Parakeet, and the second recognizer when the take has its transcript, to hear the line's last word and no word after it, and a silent run between that word and the sound. It ends the take inside that run with a fade out, keeping half of PAUSE_KEEP. A line that asks for a sound after its words, by a vocal event or a direction such as laughing, coughing, sighing, weeping, purring or hissing, keeps its tail, and so does a line that keeps its pauses. The join's cut now takes a cut that reaches the end of the file. `JOIN_VERSION` stays 2, because AJ's backlog pass of 2026-11-12 was running, a new version would list every chapter for one more join, and the cuts touch 21 takes in 16 chapters. Instead a render records a hash of the cuts its join made, as `render.cuts`, and `breeze.render_changes` counts a join as due when the cuts a join would make now differ. A render from before that counts as having made every cut but one that ends a take, because only phase 2 makes such a cut. So the `backlog` list names the 16 chapters for their tails, and the app's skip of a repeated backlog render doesn't skip one whose tail is still to cut.
* The voice's centre is per chapter, over its takes with 3 s of speech or more of a line without a direction or a vocal event. A direction such as "echoing through the hall" or "deep, armored voice" moves the embedding on purpose, and the first run flagged those. A voice needs 8 such takes for a centre. A per chapter centre doesn't see a recast, because the chapter's takes all come from one clip, and the keys already send those lines to a render. The threshold is 10 median absolute deviations below the voice's median similarity, with the deviation at least 0.01, which flags 10 takes, 7 of them the narrator's, at 10.3 to 13.6 deviations. The centre counts every such take whatever its verdict, because the verdicts are judged against the baselines, and it leaves out the takes more than 4 deviations below a first centre, so a drifted take doesn't pull it.
* The second recognizer flags a suspect take of four words or more that it also hears wrong, by the word rules Parakeet's transcript goes through, names included. Of the 22 suspect takes, 2 are flagged that way: "I… I don't understand" heard by both as "I not understand", and "Duchar Honorius?" heard as "We saw Honorius" and "Do we sound Honorius". The other 20 are lines of one to three words. Moonshine hears 5 of those right ("Cry me a river", "Dark Saber", "Not thunder", "I will", "I see") and hears the same homophone as Parakeet in most of the rest: "Simon guessed" was heard as "Simon guest" nine times. So a short line stays suspect, as phase 1 decided for a changed word in one.
* Two takes rank by both recognizers' word errors when both have the second transcript, and by Parakeet's alone otherwise.
* Without the models a node gathers Parakeet's two blocks as before. The tail rule and the cut need only those, so they work there. `drift` and `garbled` need the speaker block and the second transcript. A take heard on a node with the models and one heard without them can get different verdicts, as a name's spelling already can between the two nodes.
* A model folder that lacks a file, or whose model doesn't load when the process first tries it, leaves its block out, and the check logs why. The take check still runs, and a saved take isn't heard again for a block the machine can't gather.
* `take_check.json` grows with the speaker block, from 302 KB to about 420 KB for a chapter of 190 takes.
* A scratch render on the Mac, with the models and the chapters' own copies of their takes, took lines 12, 13 (`tail`), 45 (`drift`) and 60 of chapter 44, lines 19, 187 and 199 (`drift`) of chapter 42, and lines 39 and 41 of chapter 33 again, and kept a new take for each. The new take of line 13 has no word after the line, and line 45's lies at 0.90 from its voice's centre against 0.75. Chapter 42's join cut the tails of lines 30 and 36 and came out at -18.1 LUFS with a true peak of -1.2 dB. A second render of the three would change nothing. Chapter 44 line 29, `garbled` in the gathering, was heard again on the Mac as "Duissar Honorius. Is that you?" and passed, so this verdict hangs on the node that hears the take, as a name's spelling does.

---

### A flagged long line is taken again in pieces
**Date:** 2026-11-12
**Context:** A line of 58 words and one of 64 lost 25 and 30 of their words in The Hundred Reigns, and new seeds fixed both. Most long lines pass: of the 287 lines over 60 words in chapters 7 to 48, the rules of 2026-11-12 flag 4, one for dropped words and three for a loudness swing. This is package A4 of `docs/take-quality/phase-1.md`.
**Decision:** When the check flags the take of a line over 60 words, its retake asks breeze-server for the line in pieces and joins them into the line's one take, with 0.25 s of silence between them. A piece has about 40 words and ends at a sentence. A sentence over 40 words is split after a comma, a semicolon, a colon or a dash, and a piece of under 8 words joins a neighbour, because the shortest takes are the least reliable. The 320 lines over 60 words in The Hundred Reigns split into 2 to 4 pieces, and 5 of them have a piece of 41 to 45 words. Each piece has its own length guard and its own three tries in a round, so a line in pieces can use more than the nine generations a whole line gets. The take records how many pieces it has under `pieces`, so the next render allows it their length. The key doesn't change, and a long line whose take passes is never split, because that would replace good audio. Only breeze-server does this. The three numbers are Claude's choice.

In a scratch copy on the laptop, a render of chapter 43 took line 48 again in 2 pieces. Its saved take of 64 words had a step of 16.8 dB between two seconds, and the new take passes with a step of 12.0 dB. The chapter's seven other lines over 60 words kept their takes, and a second render had nothing to change.

---

### A script line records what the later checks will read, from a pass of its own
**Date:** 2026-11-12
**Context:** Phases 2 and 3 of the take quality plan need to know which words the author stressed, which word a contrast stresses, how a heteronym sounds, and how loud and how fast a direction makes its line. The plan (package C1) asked the screenplay pass to write these as required fields of every line, because a script can't be written again without moving its line numbers. Built that way, the pass over chapter 6 of The Hundred Reigns, 276 lines, took 53,405 output tokens on 2026-11-12, and 51,185 with the fields in one object that is null for most lines. The pass without the fields took 29,776 the same day. The subscription's pass can't answer with more than about 64,000, so a chapter of over about 340 lines would have failed, and every pass would have cost three quarters more.
**Decision:** The screenplay pass answers with speaker, text and direction as before. A marks pass reads a saved script beside the chapter's text and writes six fields on each line: `emphasis`, `focus`, `heteronyms`, `emotion`, `energy` and `pace` (`README.md`, Pipeline per chapter, step 4). Phases 2 and 3 read these names. Nothing runs the marks pass by itself yet: a screenplay pass runs it only with `--marks`, and no job asks for it, because nothing reads the fields and each run costs AJ's subscription about a third of a screenplay pass again. The phase that first reads the fields turns it on and adds its job. The plan asked for four fields, and its fourth, the direction in three parts, is three fields so that the schema can limit `energy` and `pace` to fixed values. This is Claude's change to the plan, and AJ hasn't seen it.

* The marks pass over the same chapter took 11,166 output tokens and marked 55 lines: emphasis on 9, a focus on 3, heteronyms on 12, an emotion on 29 and an energy on 4. The two passes take about 41,000 together, and the screenplay pass is as far from its limit as it was.
* The marks pass names lines by number and never returns a line's text, so it can't move a line. It can run for a script that exists already, which the fields in the screenplay pass could not, so the 48 scripts from before 2026-11-12 can get their fields when a phase needs them. `marks <slug> <chapters>` does that from the CLI. No job kind runs it yet, and the phase that first reads the fields adds one.
* A screenplay pass with `--marks` doesn't fail for its marks. When the marks pass fails, the script stays as the screenplay pass wrote it, and the log says so. A word the marks name that its line doesn't have is left out, and so is an emotion, energy or pace on a line without a direction, and a value outside the schema.
* Both parsers keep italics as `*...*` in `text.md`, which is where the marks pass finds the stressed words. An EPUB chapter ingested before 2026-11-12 doesn't have them. A script's `text` has no italic markers, and the screenplay pass sends an answer with a pair of asterisks around words back once. An asterisk that the book has itself, as in a censored word or a scene break of `* * *`, stays, in the EPUB parser and in the check. The Royal Road parser still turns a paragraph of `* * *` into `*`, as it did before, and takes `f***` for `f*`.
* The API backend now asks for up to 64,000 output tokens, where it asked for 32,000, which chapter 6 would have come within 2,300 of.

---

### The screenplay pass compares the script's words with the chapter's
**Date:** 2026-11-12
**Context:** The only guard on a script was a word count within 0.9 to 1.2 of the chapter's, so a script that dropped a paragraph passed. This is package C3.
**Decision:** The pass compares the chapter's words with the script's in order. A run of more than 4 chapter words that the script leaves out, or of more than 4 script words that the chapter doesn't have there, goes back to the model once with the run quoted, as its other problems do. The heading line is left out of the comparison on both sides, because the pass rewrites it for speech, so "Ch. 5" is read "Chapter Five". The script's first line is its heading unless it has the chapter's opening words. The limit of 4 is Claude's choice: limits from 2 to 6 flag the same scripts in The Hundred Reigns, and the speech normalizations make shorter runs. `audit-scripts <slug> [chapters]` lists the existing scripts with such a run and writes nothing. AJ decides chapter by chapter whether to write a script again, because that makes the whole chapter's takes again.

On 2026-11-12 the audit over the 48 scripts of The Hundred Reigns in a scratch copy listed one: chapter 31 leaves out the 7 words "Simon didn't like the sound of that" before line 151.

---

### Backlog work runs only on an empty queue
**Date:** 2026-11-12
**Context:** A new check finds takes to fix in chapters AJ may have finished. He said to fix them for a later listen and never to let that work hold up another job. The scheduler had no priority, so on 2026-11-12 a script queued the nine renders of flagged chapters one at a time, each when the queue was empty. This is package F2 of `docs/take-quality/phase-1.md`.
**Decision:** A job whose request has `"backlog": true` in its params is backlog work. It starts only when no other job is running and no job that is not backlog work is queued, on the cluster and on one machine. A job that is not backlog work never waits for queued backlog work, even on the same chapter. A backlog job that is already running is not stopped for another job. `backlog <slug>` lists the chapters a render would change, with the takes it would make or take again and whether it would join audio that the chapter doesn't have from the current Breeze join. A render records the join's version as `render.join`, and a record without it counts as version 1. The rest of this entry is Claude's choice, and AJ hasn't confirmed it. One backlog job runs at a time, though two GPU nodes could each take one, so a job that arrives waits for one backlog job at most, or takes the other node. The list also shows each chapter's last engine, because a Breeze render of chapters 1 to 6 would replace their Drama 3 audio, and whether to render those again is AJ's open question below. The list doesn't count a saved take that a render would only hear, because hearing it changes nothing unless the check then flags it.

---

### Say-as overrides fix how names are read
**Date:** 2026-11-07
**Context:** AJ: Louis is always said "Lewis", and Lauriane is always said "Laurie Anne".
**Decision:** A pronunciation entry may have `say`, the spelling the voice reads. It replaces the term as a whole word in the text sent to the voice only, and the chapter text and the script keep the original spelling. The character pass never overwrites an existing entry. AJ edits overrides on the bible page, which runs the `say-as` command as a job.

---

### The Names tab shows how each name was heard and lets AJ hear respellings before he picks one
**Date:** 2026-11-12
**Context:** AJ asked on 2026-11-12 for a check on names that are said different ways. He can't review a flagged take, so the check has to end in something he can fix in one sitting. Within half a minute of chapter 33 the recognizer wrote Belzemine four ways, and that can be the recognizer and not the voice. These are packages D1 to D3 of `docs/take-quality/phase-1.md`.
**Decision:** The take check records how each name was heard, and the Names tab lists the names with the most spellings first (`README.md`, Web app). Over the 9,383 takes of chapters 7 to 48 in a scratch copy, 154 names were heard, 63 of them in three spellings or more: Eole in 87 spellings over 340 takes, Vouivre in 81 over 158, Casval in 28 over 247. A possessive counts as the name's own spelling. The list is a hint, and the page says so.

* A `say-options` job proposes respellings with Claude and has Breeze say each one inside a real line, in the voice, with the direction and from the seed that the chapter's render uses for the line, so the spelling is what differs between the takes. A take whose length doesn't fit the line is taken from the next seed, as in a render, and each option records its seed. The line is from a chapter with a Breeze render when one has the name. The name as written is always the first option.
* The job never files a say-as. A say-as changes the text of every line that has the name, in every chapter, so a render makes each of those takes again, and that should happen once, on AJ's pick. Eole is in 348 lines of 31 chapters, so a pick for it makes 348 takes again. In the scratch copy a pick of "Ay-ohl" left 3, 17 and 9 takes to make in chapters 12, 33 and 40, which is the lines of those chapters that have the name.
* The options are stored on the name's entry in `bible/pronunciations.yaml` under `options`, and their audio under the node's output folder in `breeze/say/<slug>/`. `worker.media_files` sends that audio to the app's mirror. An entry with options stays when its say-as is removed. A new pass replaces the options before the takes are made, so when the takes fail the tab offers Say them, which makes the takes alone.
* A chapter job doesn't wait for a `say-options` job and the other way round, because the options of a name are not its say-as and a chapter job doesn't read them.
* AJ asked on 2026-11-12, after his first use of the tab, for a way to make a clip of a spelling of his own. Hear it beside the spelling box queues `say-options` with the spelling: it joins the name's options, and the takes that are missing are made on a breeze node without a Claude pass. A name without options gets them this way too, with the name as written beside the spelling. A clip is kept only while the line, its voice, the voice's reference, the direction, the seed and the other say-as spellings in the line are what the clip was made with. Otherwise every option is said again, so the options still differ only in their spelling. Options whose script line was rewritten move to the line as it is now for the same reason. New options replaces the options, the typed ones too.
* AJ picked spellings for two names in a row on 2026-11-12 and queued the render offer after each, so the 20 chapters that say both names were queued twice. A backlog render is now not queued while a backlog render with the same arguments waits and no other kind of backlog job is queued after it. Backlog work starts on an empty queue, so the render that waits reads every change made before it starts. A queued backlog render is skipped when a render with the same arguments started after it was queued, no job that changes what a render reads has run since, and `render_changes` finds nothing to make, take again, hear or join. The tab also remembers the chapter AJ typed.
* After a pick the tab offers one backlog pass over the chapters whose takes say the name, with the chapters from the one AJ is on first. The tab fills that chapter in from the listening positions the browser keeps, and AJ corrects it there, because he also listens to copies on his phone.
* A chapter has names only when its check is from the rules of 2026-11-12 or later. The page names the chapters that an older check heard.

In the scratch copy, the options job for Eole cost one Claude pass of about $0.09 and six takes: the name as written and Ay-ohl, Ayoal, Eh-ohl, Ay-oll and Ay-yole. The recognizer wrote them as eo, ayal, ahil, all, aiol and aioli.

---

### Breeze joins fill clicks and fade every take
**Date:** 2026-11-08
**Context:** AJ heard occasional loud clicks and pops in the Breeze chapters 7 to 10. They come from Breeze, not from the join. Some takes hold bursts under 1 ms long, about 20 to 50 a chapter, and a few takes start at full level on their first sample. They don't sit on Breeze's 80 ms frame boundaries. AJ heard the ten most prominent repairs and said "repaired samples sound fine".
**Decision:** The join fills each click from an autoregressive model of the 20 ms on each side of it and fades every take in and out over 3 ms. A burst counts as a click only while it is that short, so consonants are left alone. A review found that the detector also takes some sharp word-final stop releases, which the fill softens. That stays until AJ hears a dull consonant.

---

### The library of record lives on containers
**Date:** 2026-11-07
**Context:** AJ wanted one entry point usable from any tailnet machine, with jobs running on whichever node is set up for them. He approved moving the library on 2026-11-07 at 22:08.
**Decision:** The library is `/opt/serial-audio/library` on containers, behind the web app with the Nomad executor. `D:\fish\library` is retired. `library/` and `incoming/` stay out of git, because they hold chapter text and saved Royal Road pages.

---

### The app backs the library up to storage once a day
**Date:** 2026-11-11
**Context:** `/opt/serial-audio/library` on containers' local disk was the only copy. The bibles, the picked voices, the say-as overrides and the saved chapter pages can't be made again the way audio can. AJ said to use the NFS mount of storage, which containers has at `/mnt/nfs`.
**Decision:** The app backs the library up itself, once a day, to `/mnt/nfs/serial-audio/backup` (`--backup-dir`). A snapshot archive holds everything but the audio, with `incoming/` and `placement.json`, and the newest 30 stay. Audio is copied beside the snapshots, and a later render of a chapter replaces its copy, because audio can be rendered again. The app backs up only into a folder that holds the file `.serial-audio-backup`, so it doesn't write a backup to the local disk when the share is not mounted. The header says "backup failing" when the last try failed or the last backup is more than two days old. Rejected: a timer on containers, because it is a host change and the app could not show that it failed. The first backup, on 2026-11-11, was a 21 MB snapshot of 660 files and 284 MB of audio in 44 files. `deploy/README.md` has the restore steps.

---

### Chapters reach the phone through a button on the serial page
**Date:** 2026-11-11
**Context:** AJ listens in Smart AudioBook Player on Android, which reads one folder per book from the phone's storage. He had been downloading each chapter and copying it by hand. Claude first built a folder export for a sync tool to pick up (`--publish-dir`), and AJ said "i don't like syncthing very much".
**Decision:** The serial page has a Save to this device button on a browser that can write to a folder (`showDirectoryPicker`). It asks for the folder once, then writes each rendered chapter that the device doesn't have to `<serial title>/<NNN> - <chapter title>.opus`. A chapter deleted from the folder is written again only after a new render. The browser keeps the folder and the list of saved chapters, so the app doesn't keep anything per device. On 2026-11-11 it passed in headless Edge at phone width, with the browser's private file system in place of a picked folder. It was not tried on AJ's phone. The folder export stays in the code and is off on containers.

---

### Workers get their code and files from the app over HTTP
**Date:** 2026-11-07
**Context:** A cluster job needs the pipeline code and one serial's folder on its node, and the changed files back in the library. The meetings pipeline on the same cluster moves its files through a Garage bucket, and its workers run a git checkout.
**Decision:** The app serves each job a bundle: the code it runs, the prompts and schemas, and the serial's folder without its audio. The worker sends back every file it created or changed, and the app writes a file only where the library copy still has the hash it had when the bundle was made. Workers need no bucket key and no checkout, and they always run the app's code. Rejected: Garage as the exchange, because the app is always on and can serve the files itself. Rejected: a git checkout on each node, because a node would drift from the app, and every change would need a pull on every node before a job could use it.

---

### The app picks the node for every GPU job
**Date:** 2026-11-07
**Context:** The first design reserved static port 19137 in every GPU job, so Nomad would place one GPU job per node. Two renders queued together both landed on the laptop, because each node has an IPv4 and an IPv6 tailnet address and Nomad checks a static port per address. Joins, take checks and voice picks also need the node that holds their files.
**Decision:** The app chooses the node and dispatches a copy of the job pinned to it, `serial-audio-<class>-<node>`, registered once per GPU class per GPU node. It records which node holds each chapter's takes in `state/placement.json`. Each worker also takes a machine-wide GPU lock file. Rejected: a Nomad `host_network` limited to the IPv4 tailnet range, because it needs a client config change on every GPU node and still can't send a join to the right node.

---

### The Claude passes run on dev-arch
**Date:** 2026-11-07
**Context:** The laptop's Nomad client runs as LocalSystem, so its tasks can't use AJ's claude login. dev-arch already runs `claude -p` as aj for the meetings jobs and is always on.
**Decision:** The `llm` class runs only on Linux nodes, as aj, and dev-arch offers it through dynamic node meta. A cast runs its briefs there first and then designs the voices on a GPU node, which finds the briefs cached. Rejected: running the laptop's Nomad service as AJ's user, because that changes a host service account for one class of job.

---

### The Claude passes sign in with a token from a Nomad variable
**Date:** 2026-11-11
**Context:** The passes used the login AJ did by hand on dev-arch, which is the CLI's own session file and was not in the vault. AJ minted a one-year token with `claude setup-token`, filed it as the vault item `homelab-claude-subscription-token`, and said to use it. A Nomad job can't unlock the vault, so the cluster needs a working copy.
**Decision:** The working copy is the Nomad variable `nomad/jobs/serial-audio-llm`, item `claude_oauth_token`. AJ chose it over a file on dev-arch. A template in the `llm` job reads it into `CLAUDE_CODE_OAUTH_TOKEN` when a task starts, through the job's own workload identity, so the app never holds it. On dev-arch the token is in the task's environment and in `secrets/claude.env` of its allocation, a file on tmpfs that only root can read and that stays until Nomad collects the finished allocation. The job also sets `SERIAL_AUDIO_CLAUDE_AUTH=token`, and the worker refuses a job that asks for a token and has none, so a pass can't fall back to the dev-arch login without saying so. A request without a token and the app's dispatch token both get HTTP 403 for the variable. On dev-arch a made-up token in the environment got a 401 with the login present, so the token outranks the login. The first cluster run used a dummy value and failed with a 401, and the run with the real token wrote a screenplay for a throwaway serial. The `llm` class still runs only on dev-arch, as aj, because it uses aj's install of `claude`. The token expires about 2027-11-11, and `deploy/README.md` has the steps to replace it.

---

### jsc-m1 renders Breeze, and the laptop is preferred
**Date:** 2026-11-07, revised 2026-11-08
**Context:** AJ asked for jsc-m1 to be usable as a worker. Its patched Metal breeze-server runs at about 0.4x realtime, against the laptop's 2x. The CPU backend and Vulkan through MoltenVK were slower, at 0.26x and 0.28x. In the render records of 2026-11-08 the Mac made a take in 17.5 s and the laptop in 2.8 s, about 6x faster.
**Decision:** jsc-m1 offers the `breeze` class, one GPU job per node. A job that needs a node's saved files goes back to that node only while the app has no copy of them (the next entry). Any other GPU job goes to the ready node where its estimated finish is earliest: the node's running job's remaining time from its progress log, plus the queued jobs already waiting for that node, plus the job's own estimate, which is its takes times the node's seconds per take (the mean of the last three Breeze renders that finished on the node and made twenty takes or more and at least half of their chapter's lines, kept in `placement.json`, else 3 s for NVIDIA and 18 s for Metal), counting only the takes the holder lacks, plus the join and the check. A render whose reuse cannot be worked out is costed at all its takes, because sending it to the slow node by mistake costs an hour and the other mistake costs ten minutes. It waits for a busy node when that beats every free one by two minutes or more, and a GPU job no longer waits behind a blocked job in its lane. With the join and the check the same on both nodes and the margin, the Mac wins a fresh 255-line render only when four or five are ahead of it on the laptop. AJ set the conditions: estimate the completion times, wait only when it saves time, and account for the other queued jobs. Rejected on 2026-11-08: sending a render back to the node that holds its takes whatever it would reuse there, and then the 90% reuse threshold that replaced it for a few hours, because both ignore how long the fast node stays busy (JOURNAL.md, 2026-11-08).

---

### The app keeps a copy of every take and candidate, so no Breeze job needs one node
**Date:** 2026-11-12
**Context:** A join, a take check and a voice pick ran only on the node that held their takes or candidates, and a render on the other node made every line again. With the laptop off on 2026-11-12, AJ could not finish a recast, because "some files were needed that were only stored there". He asked that any job run while dev-arch and one of jsc-m1 and aj-legion5 are up.
**Decision:** The app's media mirror, `state/media/`, keeps a copy of every Breeze take and every cast candidate under its path on the node that made it. A worker sends the app the audio its node made, as one request per file, and only the files the app lacks. Before its steps, a GPU job fetches the chapter's takes that its node lacks and points its copy of `breeze_takes.json` at them, so `render` and `check-takes` read local files as before. A voice pick runs on containers and files the clip from the mirror. A take check is a `breeze` job. The scheduler ties a join or a take check to one node only while the mirror lacks a take of the chapter. A `sync-media` job sends what a node already holds. The rest of this entry is Claude's choice, and AJ hasn't confirmed it. Drama 3 renders, Drama 3 decodes and Qwen casts still need the laptop, because the Mac has no ComfyUI and installing one is a host change AJ did not ask for. A Qwen pick on containers files the Drama 3 voice in the library only, and a Drama 3 render copies it to ComfyUI before it generates. The mirror is not in the daily backup, because the nodes hold the takes too, and it keeps a copy of a chapter's takes for each node that worked on the chapter. Rejected: a share on storage as every node's output folder, because each node needs a mount and the laptop's tasks run as SYSTEM. Rejected: takes inside the result archive, which is 70 MB a chapter held in memory on both ends. Rejected: node-independent paths in `breeze_takes.json`, because every reader of a take's `file` would change at once.

---

### Parakeet checks takes and casting candidates
**Date:** 2026-11-07, extended to the laptop 2026-11-08
**Context:** jsc-m1 has no ComfyUI for Qwen3-ASR, and on the laptop a check failed whenever ComfyUI was down. AJ preferred Parakeet to whisper, and his meetings project had already chosen Parakeet TDT 0.6B v2 int8 through sherpa-onnx.
**Decision:** The take check and the casting check use Parakeet when its model folder exists and Qwen3-ASR otherwise. `SERIAL_AUDIO_ASR` overrides the choice. Both `breeze` nodes have Parakeet, so no check needs ComfyUI.

---

### Breeze voice design is the default cast engine
**Date:** 2026-11-08
**Context:** AJ heard a Breeze cast of the Keeper and Lauriane beside their Qwen voices and picked a Breeze candidate for both. He said "i think we should make breeze voices the default for new casting going forward". A Breeze cast needs no ComfyUI, and on jsc-m1 breeze-server designs the same clip again from the same brief and seed.
**Decision:** `cast` uses Breeze voice design unless `serial.yaml` sets `cast_engine: qwen` or the cast passes `--engine qwen`. On the cluster a Breeze cast takes the NVIDIA node first, like any GPU job. Voices cast before this keep their Qwen clips and Drama 3 voice files. Rejected on 2026-11-08: sending a Breeze cast to a node without ComfyUI first. It only kept the check off Qwen3-ASR, and the laptop checks with Parakeet now (JOURNAL.md, 2026-11-08).

---

### Breeze briefs use the story age, and Qwen briefs an older one
**Date:** 2026-11-05, revised 2026-11-09
**Context:** AJ found the first Qwen VoiceDesign cast too young across the board, including characters of 20, so briefs named an older age and a low register. Breeze became the default cast engine on 2026-11-08. On 2026-11-09 the aged-up Qwen voices of Casval and Anna sounded too old to AJ for the academy arc, and he picked Breeze candidates from "about twenty" briefs for both.
**Decision:** A brief for a Breeze cast names the character's story age. AJ said "use the story age from now on". A brief for a Qwen cast still names an older age and a low register. `docs/casting.md` has the details.

---

### The text a voice is sent gets no new repair
**Date:** 2026-11-12
**Context:** Package C2 of `docs/take-quality/phase-1.md` asked how Breeze reads digits, symbols, all-caps words, parentheses, ellipses and dashes, before `breeze.voice_text()` repairs any of them. A repair changes the text of the lines it touches, so a render makes those takes again. The measurement is over the 9,383 takes of chapters 7 to 48 of The Hundred Reigns, with the rules of 2026-11-12, in word errors per 100 words of the line.
**Decision:** `voice_text()` stays as it is, so the keys don't change and a render doesn't make any line again. The data doesn't support a repair.

* The 8,442 lines without any of these marks have 0.50 errors per 100 words, and 0.63% of them are flagged.
* Digits read well: 126 lines, 0.38 errors per 100 words. The screenplay prompt's rule to leave digits as digits stays.
* A dash doesn't matter: 108 lines, 0.45 errors. Neither does a colon or a semicolon (297 lines, 0.47) or a hyphenated word (469 lines, 0.56).
* An ellipsis does: 488 lines, 0.77 errors, and 2.0% flagged. In lines over three words that is 0.66 errors against 0.45, and 1.75% flagged against 0.45%. The word errors are mostly a repeated word that comes back once, as in "A... a comet?" heard as "Uh comet?", and four of the seven flags are a loudness swing in a line that trails off and starts again. Both can be the performance the ellipsis asks for, and nothing shows that a comma or a full stop in its place would read better. The measurement that would show it is the same lines taken from the same seeds with and without the ellipsis.
* Parentheses read well. `speakable` turns them into commas before the voice sees them, and the 41 lines with takes that have them, such as "(passive)" in a perk's name, have 0.09 errors per 100 words and no flag.
* The book has too little of the rest to measure: two all-caps lines, both read right, and one line with a symbol. A script in the library doesn't have an asterisk, a bracket or an unclosed parenthesis anywhere: The Hundred Reigns has 10,724 lines and Twenty Thousand Leagues 35. A new script can't have italic markers, because the screenplay pass sends a pair of asterisks around words back. A lone asterisk, as in a censored word, still reaches the voice.

---

### A new cast clip is leveled to -18 LUFS
**Date:** 2026-11-12
**Context:** A Breeze take comes out at the level of its voice's cast clip, and the clips of The Hundred Reigns ran from -11 to -29 dB RMS. The join corrects that per voice, but a quiet clip also puts the voice's takes nearer the noise. BreezeBlue's own platform levels a clone reference to -18 LUFS. This is package B2 of `docs/take-quality/phase-1.md`.
**Decision:** A cast brings every candidate's clip to -18 LUFS, in place, after the candidates are scored and before one is filed, so a later `pick-voice` of another candidate files a leveled clip too. The leveling runs under the audio Python on the node that made the clips, limits true peaks to -1 dB and keeps the clip's format. The clipping check reads the clip before it is leveled. A cast doesn't fail when the leveling does. The clips stay as they are, and the log says so. An existing voice keeps its clip, because the clip's hash is part of every take key, and a candidate made before 2026-11-12 is filed at the level it has.

In a scratch library on the laptop, a cast of a throwaway serial's one character made three candidates at -20.3, -18.7 and -19.9 LUFS, and the clip it filed measures -18.0 LUFS with a true peak of -2.9 dB.

---

### The Breeze join levels each voice and each take, and masters the chapter
**Date:** 2026-11-12
**Context:** AJ found a new Lorimor candidate quiet beside the other characters on 2026-11-09, and the join got one gain per voice. On 2026-11-12 he asked for checks on odd volume and on silences. He listens while driving and while falling asleep, so a soft line has to stay audible and a shout must not jump out. Until then the join put 0.3 s between all takes, bent sample peaks with a tanh curve and left the Opus bitrate to the encoder. The old join of chapter 33 measured -18.9 LUFS with a true peak of +0.5 dB, which is over full scale. This is package B1 of `docs/take-quality/phase-1.md`.
**Decision:** `breeze_join.py` does what `README.md`, Pipeline per chapter, step 5 describes, and `breeze.JOIN_VERSION` is 2. AJ heard the two joins of chapter 33, lines 12 to 26, against each other on 2026-11-12 and keeps the new one: "The difference between them is subtle. Honestly, either is fine, so I think we should keep the new join". He didn't change an offset or a gap, so the numbers here stay as Claude chose them.

* A voice's gain still comes from the median of its takes in the chapter, and not from the cast clip, because an older chapter's takes can come from a clip that has since been recast. The level is now K-weighted loudness of the speech frames, the measure the take check uses.
* A take moves toward its voice's target plus the offset of its direction's class, by 6 dB at most. In the 9,246 takes of chapters 7 to 48 that have a baseline, Breeze made the soft lines 7.4 dB under their voice's normal lines at the median, the whispers 6.9 dB under, the raised lines 2.8 dB over and the shouts 5.6 dB over. The offsets of -6, -3, +2 and +3 dB pull those in. The correction shrinks for a take with under 1.5 s of speech, because the level of a short take says less: normal takes with under 0.8 s of speech spread 3.1 to 3.4 dB around their voice, and takes over 6 s spread 1.3 dB.
* The gaps are 0.15 s, 0.35 s and 0.5 s. Breeze's own pauses inside a take have a median of 0.41 s at a comma and 0.52 s at a full stop, and a take keeps up to 0.05 s of quiet at each edge. Of the 10,675 gaps in the 48 scripts, 7,389 are before another speaker, 1,749 before the same one, and 1,537 where the sentence goes on. A chapter gets about 2% longer.
* The compressor works on the loudness of 0.4 s and starts 6 dB over the target, with a ratio of 3 to 1. In chapter 33 the loudness of 0.4 s is over the target by 6 dB in 0.45% of the speech, so an ordinary line is not touched: two lines of 189 came down by over 1 dB, by 1.7 dB at most.
* The limiter looks 5 ms ahead and holds for 50 ms. At -18 LUFS the narrator's peaks reach +4.7 dB, so it works about ten times a minute in chapter 33, by 1 dB at the median and 7.5 dB at most.
* The loudness and the true peak are measured on the Opus file read back, because the encoder changes both: at 40 kbps it took 0.1 to 0.15 LU off chapter 33 and put its peaks 0.6 to 1.0 dB higher. The audio goes to the encoder 0.15 dB louder and limited to -2 dB, with the gain raised once for the 0.3 dB the limiter took, and the file is written again when it is over 0.1 LU off the target or over -1 dB. Chapter 33 came out at -18.1 LUFS and -1.2 dB from one encode.
* The bitrate is 40 kbps, a little over the 35 kbps the encoder chose by itself. `soundfile` sets it through libsndfile's compression level and can't set the bitrate mode. An encode of a 23 minute chapter takes 16 s on the laptop, and the join of chapter 33 takes 42 s where the old one took 21 s.
* The pause cap of 0.75 s and the click repair stay as they were. The pauses are found before any gain, because a gain moves soft speech and room noise across the silence threshold.
* A render records the join's version, and the `backlog` list names every chapter whose audio is from an older join. Bump `JOIN_VERSION` when the join's output changes.

---

### Web chapters come from pages AJ saves
**Date:** 2026-11-05
**Context:** Royal Road started answering the pipeline with HTTP 403, and probing it counts as getting around a bot check.
**Decision:** The pipeline doesn't contact Royal Road for chapters. AJ saves a chapter page in his browser, and `fetch <slug> <n> --html <file>` or an upload in the app ingests it.

---

### A cast, a pick, a say-as or a voice effect starts beside a chapter job of its serial
**Date:** 2026-11-08
**Context:** A cast of eole and lorimor-ancitif sat queued behind the chapter 12 render on the Mac for over an hour while the laptop was idle, because every serial-wide job waited for any running job on its serial. AJ asked why a cast should ever wait for the Mac. A cast, a pick, a say-as or a voice effect writes `voices/`, the bible or `serial.yaml`, a render, screenplay or take check writes its chapter's files, and a cluster worker takes the voices, pronunciations and effects in its bundle when it starts, so neither changes a file the other sends back.
**Decision:** On the cluster a cast, a voice pick, a say-as or a voice effect does not wait for a render, a screenplay or a take check of its serial, whether running, queued ahead of it, or waiting ahead of it in its lane. It waits only until a running chapter job has fetched its bundle, because the app builds the bundle outside its lock and a result landing during the build could tear it. A chapter job still waits for any of them, so "cast, then render" renders with the new voice. A cast's briefs stage waited for a running screenplay until 2026-11-12, while the app ran one Claude job at a time (Three Claude jobs run at once, below). The one-machine executor is unchanged. The rule first covered casts and picks, and AJ extended it to say-as and voice-effect the same day. A `bios` job, which writes only the bible, follows it since 2026-11-11.

---

### A batch job waits only for the jobs it needs
**Date:** 2026-11-09
**Context:** On the cluster every job of a Process batch waited for every earlier job of the batch. The rule came from the one-machine app, where heavy jobs ran one at a time anyway. A render waited for the screenplays of the other chapters, so the batch of chapters 13 to 17 on 2026-11-08 kept the laptop idle for 12.5 minutes with chapter 12 ready to render, and the batch of chapters 18 to 23 on 2026-11-09 kept it idle for 12.7 minutes. Renders of one batch never ran on two nodes at once, and one failed job skipped every later job, including the renders of chapters that were already scripted. AJ asked why only dev-arch was working and said to drop the wait.
**Decision:** On the cluster a batch job waits by the same rules as any other job: for the jobs of its chapter, and for a job that changes the whole serial. A chapter renders as soon as its screenplay is written, beside the other chapters' screenplays, and a batch's renders can run on both GPU nodes. When a batch job fails or is lost, only the queued jobs of the batch that wait for it are skipped. Stopping a batch job still skips every queued job of the batch, now as soon as the stop is asked for, because a queued job could otherwise start before the worker confirms the stop. A job of the batch that already runs on another node keeps running until it is stopped too. The one-machine executor is unchanged. In a scratch run of the scheduler on the 18 to 23 batch, the first chapter was ready after 23 minutes, against 36, and all six after 95, against 108.

---

### A voice card shows a casting bio without the plot
**Date:** 2026-11-11
**Context:** The bio on a voice card was the bible's role and description, which the character pass keeps current for attribution, so it had the plot in it. Leonard's read "killed again in the seventh reign by the Berwick Lich's aura beneath Castle Carcas". AJ listens on the Cast tab to judge whether a voice matches the character. He said "i don't need too many story details. i need to know age, race, temperament, characteristics", that "telling me that someone is hiding something is fine if that's a key part of their character that would show up in the way they speak", and that "telling me that someone will betray the main character later is too much".
**Decision:** A bio pass with its own prompt writes each character's casting bio into the bible under `bio`, in 50 words at most: sex and age, race or species, station, temperament and habits of speech. It reads the whole record with every fact, and it leaves out events, story mechanics and the voice itself. It says that a character hides something and doesn't say what. It describes a character who passes as something else the way they pass, so Casval reads as a young man and not as a dragon. That last rule is Claude's reading of AJ's words, and AJ hasn't confirmed it. The bio pass runs beside the casting pass, for the characters that pass briefs, so the voices stage of a cast, which finds its briefs cached, doesn't call Claude for a bio either. A bio pass that fails doesn't stop the cast. A `bios` job writes the bios of the voices that don't have one, or one again with `--redo`. On the cluster it is a Claude job that follows a cast's rule beside a chapter job (2026-11-08 entry). It also doesn't wait for a fetch or an import of its serial, nor they for it, because they don't write a file in common. The first `bios` job for The Hundred Reigns sat behind five queued imports that waited for the chapter 38 render, and AJ asked why a bios job would wait for a render. The role and description stay as they were, because the character pass reads the role and the casting pass reads both. A card without a bio still shows them.

---

### The import of a saved page waits only for jobs on its chapter
**Date:** 2026-11-11
**Context:** Five imports of chapters 39 to 43 of The Hundred Reigns sat queued for the length of the chapter 38 render on jsc-m1, because a fetch of any sort counted as a job that changes the whole serial. An import with `--html` writes only its chapter's `text.md`, `chapter.yaml` and `author_notes.md`, and it doesn't refresh the index or `serial.yaml`. After asking why a bios job would wait for a render, AJ asked "and the import jobs?". Claude read that as: the imports should not wait for it either.
**Decision:** On the cluster an import counts as a job on its chapter. It doesn't wait for a render, a screenplay, a take check or a character pass of another chapter, and they don't wait for it. It and a cast, casting bios, a voice pick, a say-as or a voice effect don't wait for each other either, because those write only `voices/`, the bible and `serial.yaml`. An import passes a job that waits ahead of it in its lane. It still waits for a job on its own chapter, for a fetch from the site and for a bible reset, and they wait for it. The app builds a bundle outside its lock, so an import starts only once each running job of its serial has fetched its bundle, a job that gets a bundle starts after a running import, and the imports of one serial run one after the other. A fetch from the site still changes the whole serial, because it writes the index and `serial.yaml`. The one-machine executor is unchanged. AJ asked on 2026-11-11 whether imports "will not wait for renders" from now on, and the cast part was added then so that the answer is yes for a batch with a queued cast as well.

---

### A character pass waits for a chapter job only on its own chapter
**Date:** 2026-11-11
**Context:** AJ queued the Process batch for chapters 39 to 43 of The Hundred Reigns while the chapter 38 render of the batch before it still had half an hour to run on jsc-m1. The batch's first job, the character pass for chapter 39, waited for that render, and the other sixteen jobs waited for the character pass. AJ asked "why is everything still waiting behind the render?". A character pass writes the bible and its own chapter's files, and a render, a screenplay or a take check writes its own chapter's files, so two of them on different chapters don't write a file in common. This was an open question since 2026-11-09, and Claude took AJ's question as the answer.
**Decision:** On the cluster a character pass doesn't wait for a render, a screenplay or a take check of another chapter, whether running or queued ahead of it, nor for an import of another chapter. It waits until a running job of its serial has fetched its bundle, as a cast does. It still waits for a job on its own chapter, for another character pass and for a cast, so the bible grows in chapter order. A chapter job still waits for a character pass of any chapter, because it reads the bible when it builds its bundle. A GPU node can therefore sit idle for the rest of one character pass before a queued render starts. The 26 character passes in the app's job list on 2026-11-11 took 45 to 99 seconds. The one-machine executor is unchanged.

---

### Three Claude jobs run at once
**Date:** 2026-11-12
**Context:** The batch of chapters 49 to 53 of The Hundred Reigns wrote its five screenplays one after the other on dev-arch, 2.2 to 2.7 minutes each, from 21:32 to 21:45, and the queue showed chapters 51 to 53 as "queued behind Screenplay · ch 50". AJ asked "didn't we enable other machines to run screenplay jobs? do they really need to run sequentially?". No other machine runs them. The `llm` class is still dev-arch alone (2026-11-07 entry), and the jobs that run on either node since 2026-11-11 are the Breeze jobs. The passes ran one at a time because `LLM_SLOTS` in `serial_audio/web/nomad.py` was 1, and the records give no reason for that. A screenplay pass writes only its chapter's `script.json` and `chapter.yaml`, so two of them on different chapters write no file in common. On 2026-11-11 dev-arch ran two scratch screenplay passes beside a live one, and the two live passes that overlapped them took 148 and 146 seconds, against 133 and 158 for the two before. On 2026-11-12 dev-arch, with 12 cores and 24 GB, had a load of 0.08 while one pass ran. Whether to raise the limit was an open question since 2026-11-11. That AJ wants it raised is Claude's reading of his question.
**Decision:** `LLM_SLOTS` is 3, the number that was measured, so three Claude jobs run at once and a fourth waits with the reason "waiting for a running Claude job to finish, 3 run at once". After the deploy, four screenplay jobs of a throwaway serial were queued together: three started on dev-arch in the same second, and the fourth waited with that reason and started 2 seconds after the first of them ended (`JOURNAL.md`, 2026-11-12). The rules for which jobs wait for each other are unchanged: the character passes and the cast of one serial still run one after the other, and a screenplay still waits for them and for a job on its own chapter. A Claude job also no longer waits behind a blocked Claude job that it shares no file with, as a GPU job doesn't. Until then a screenplay that waited for its chapter's render held every screenplay queued after it, of any serial. A cast's briefs, casting bios and the respellings of a name start beside running screenplays while a place is free. The renders still set the pace of a batch that renders. The five screenplays were all written 12 minutes after the first one started, and the laptop, which renders one chapter at a time, was estimated to finish chapter 53 about an hour after that. Three at once have five scripts ready about 7 minutes sooner, so a render starts that much sooner at most. It also helps a Claude job that AJ waits for, such as the respellings of a name. More than three passes at once is not measured. No second `llm` node was added, because dev-arch is idle while a pass waits for Claude's answer. The one-machine executor is unchanged. Rejected: one Claude job at a time, which no record gives a reason for (`JOURNAL.md`, 2026-11-12).

---

### An import can queue the processing of its chapters
**Date:** 2026-11-11
**Context:** An imported chapter got its character pass, voices, screenplay and render only from the Process dialog or its next-step button, after the import had finished. AJ asked for "a checkbox to automatically process the chapters after import".
**Decision:** Import saved pages has a box, process the chapters after import, and the browser remembers it. With it ticked, the import and the processing are one batch: the imports, then for each imported chapter the jobs the Process dialog queues by default, with 3 cast candidates, the next free cast seed, no render seed, so a chapter keeps its own, and the LLM settings last chosen in a dialog. The Process dialog doesn't open. The rest of this entry is Claude's choice, and AJ hasn't confirmed it. Only a chapter that nothing has been made from yet is processed: a new chapter, or a chapter on disk at status `fetched` when "replace chapter text already on disk" is ticked. A chapter on disk without that box isn't processed, because its import fails, and that import is queued outside the batch. A replaced chapter that already has a character pass, a script or audio isn't processed either. A forced import keeps the chapter's status, so the default jobs would render the script of the old text. The Process dialog's redo boxes cover that case. When a fetched chapter before the last processed one isn't processed, the import asks first, because the bible would miss its characters. A character pass or a screenplay queued behind the import of its chapter names that chapter in its job record before the chapter is on disk, so on the cluster the chapter's jobs wait for the import, and a failed import skips them and the jobs that wait for them. On the one-machine executor a failed import skips the rest of the batch, as any failed batch job does there.

---

## Not decided yet

As of 2026-11-11:


* Whether to re-render the chapters of The Hundred Reigns that use old voices. Chapters 1 to 6 are Drama 3 renders, so Louis and Lauriane sound different there, and they take about 85 minutes on the laptop. AJ was asked about them on 2026-11-07 and hasn't answered. Chapter 12 has Lauriane's old Qwen voice, because it was rendered before the recast on 2026-11-08.
* Whether to recast the other academy-age voices that were cast with aged-up Qwen briefs: simon, leonard-decarabia, meredith-dose, cassandra, antonine-de-shax and tiella-decarabia. Casval and Anna were recast with Breeze at their story age on 2026-11-09 (`docs/casting.md`), and AJ was asked about the rest that day.
* Whether to render chapters 7, 8 and 10 of The Hundred Reigns again. They have the Qwen voices of Casval and Anna that AJ replaced on 2026-11-09, and they were joined before the join leveled each voice. AJ had finished them, so they were left alone.
* Whether storage's share needs a backup of its own. The library's daily backup lives only there, and silver has no backup job.
* Whether the Save to this device button works on AJ's phone. Chrome's notes say the folder API reached Android in Chrome 132, and the button shows only where the browser has `showDirectoryPicker`. AJ was asked on 2026-11-11 to try it.
* Whether to remove the folder export (`--publish-dir`). Nothing uses it since AJ said on 2026-11-11 that he doesn't want a sync tool.
* Whether to prune anything. On 2026-11-11 the laptop's saved takes were 2.06 GB in 7,409 files with 289 GB free on `D:`, and containers held 301 MB of library and 99 MB of mirrored media with 235 GB free. AJ said that day that pruning is good. Nothing prunes yet, and a saved take still saves render time when a chapter is rendered again. Since 2026-11-12 the app's mirror also holds every take, 2.9 GB that day with 233 GB free on containers. It gains a copy of a chapter's takes, about 70 MB, each time the other node works on that chapter, and nothing removes the copy the library no longer names.
* Whether to derive the scheduler's waits from the files each job kind reads and writes, in place of the exceptions in `conflicts()` in `serial_audio/web/nomad.py`. AJ said on 2026-11-11 that it is probably a good idea. It waits for a change of its own.
* Whether to check a screenplay against its chapter word for word. The coverage check is a word-count ratio of 0.9 to 1.2, so a script that drops a paragraph passes. The check would compare text with text and use no recognizer. AJ's answer on 2026-11-11 was about trusting the transcriber, so he was told that and asked again.
* lorimor-ancitif and eole were cast on 2026-11-08 (Breeze seeds 17000 to 17002, 17000 filed with status auto for both) from the cached briefs, not the older, lower descriptions planned in `docs/casting.md`, and wait for AJ's pick or a recast. The narrator reads their lines in chapter 12, rendered before the cast, so it needs a render afterwards, which remakes only their 3 lines.
* The take quality work is under way. AJ approved a plan in three phases on 2026-11-12, and it is in `docs/take-quality/`, with where each package stands. He said all three phases will be built, that he picks a name's pronunciation from rendered candidates as with voice casting, and that candidates are rendered when he asks and for the names the check hears several ways.
