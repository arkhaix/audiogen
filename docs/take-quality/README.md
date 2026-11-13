# Take quality checks

AJ asked on 2026-11-12 for better checks on each Breeze take: silences, mispronounced words, names said different ways, the wrong word emphasized, and odd volume changes. This folder holds the plan for that work in three phases. Each phase file is written so a session that has only this folder and the repo can do it.

The research behind the plan is in `reports/TTS take quality checks.md`, with the researchers' notes and a survey of the code in `research_notes/TTS take quality checks/`. Read the report's summary and its section on chapter 33 before you start a phase.

## Files

* `README.md`: how to use this folder, what AJ decided, the shared contract, and where things stand.
* `phase-1.md`: use what the pipeline already has. No new model.
* `phase-2.md`: three small CPU models and a second recognizer. It changes hosts.
* `p4-name-check.md`: an addendum to P4 of phase 2. What the name check hears and misses, and the steps to refine it.
* `phase-3.md`: rank takes on delivery. Emphasis, direction and pitch.

## How to use this folder

* Start a phase with "do phase N in docs/take-quality".
* Do the phases in order. Phase 2 needs the evidence record of phase 1. Phase 3 needs the choice among several takes that phase 2 builds.
* The session that does a phase is its lead. The lead sets thresholds from data, keeps the design work, and hands a package to a subagent by the handoff skill only when the package's part of the phase file is enough of a spec.
* Don't reopen a point of the shared contract without AJ. Every number in these files (60 words, the level offsets, 3%) is a starting point that the lead sets from data.
* Update "Where things stand" at the end of this file when a package is deployed.

## What AJ decided

* All three phases will be built. He won't use a partial result, so the order inside a phase is free.
* For a name he picks from rendered candidates, as with casting. Candidates are rendered when he asks, and the page lists the names the check hears several ways.
* He can't review flagged audio, because he listens while driving. Every other defect needs an automatic fix: another seed, a choice among takes, a change to the text sent to the voice, or a repair of the signal.
* Older chapters are backlog. Fix them for a later listen, and never let that work hold up another job. He was on chapter 35 on 2026-11-12.

## Shared contract

These ten points hold for all three phases.

1. A take has one evidence record, in versioned blocks. A `take_check.json` entry keeps `key`, `audio` and the verdict fields. It gains one block per source: `asr` has the transcript and the recognizer's raw tokens, start times, durations and log-probabilities, and `signal` has the numbers measured from the audio. Each block has a version. Store raw tokens, not words, because `asr.words` will change and words can be derived again. The derived fields `names`, `reasons` and `repairs` sit beside the blocks. Today `takecheck.entry()` rebuilds an entry from the transcript and `check_takes` rewrites the file, so both have to keep the blocks.

   ```json
   "72": {"key": "a14e58...", "audio": "f0767d91...",
          "asr": {"v": 2, "engine": "parakeet", "heard": "Heol replied as Agnes returned...",
                  "tokens": [" He", "ol", " re", "pl", "ied"], "starts": [0.0, 0.16, 0.4, 0.48, 0.56],
                  "durations": [0.16, 0.08, 0.08, 0.08, 0.16], "logprobs": [-0.9, -1.4, -0.1, -0.1, -0.2]},
          "signal": {"v": 1, "seconds": 7.4, "level": -27.1, "peak": -6.2, "clip_run": 0,
                     "silences": [[2.31, 2.74]], "tail_drop": -1.2, "swing": 4.1},
          "words": 20, "sub": 1, "del": 0, "ins": 0, "wer": 0.05, "names": {"eole": "heol"},
          "verdict": "ok", "reasons": [], "repairs": {"cuts": []}}
   ```

2. One function judges a take, and it uses the standard library only. `takecheck.judge()` derives the verdict, the reasons and the repairs from the stored blocks, the line's token map (point 6) and the voice's baseline (point 8). A rule never fires when its evidence is missing or has an old version. So a take that Qwen3-ASR heard, which gives a transcript only, is judged on its words. `takecheck.reusable()` has to ask for the current block versions. Without that, the 9,383 entries saved on 2026-11-12 would never be heard again.
3. The verdicts stay `ok`, `suspect`, `unreadable` and `flagged`. A take is flagged when any reason for a retake holds. `retake_flagged`, `flagged_before`, the chapter page and `app.js` then need no new verdict.
4. The rank between two takes is a tuple: verdict, word errors, name score, signal reasons, prosody rank. An earlier slot beats a later one. Compare two takes only on the slots both have. Phase 1 fills the first, second and fourth slots, phase 2 the third, and phase 3 the fifth.
5. Gather evidence before the join, in the batch that transcribes. A finding costs a single retake only then, because breeze-server is still up. Measure the signal at the take's own sample rate, before the recognizer resamples it.
6. One function builds Breeze's text and a token map. `voice_text(line, bible)` returns the text sent to the voice and, for each word, whether it belongs to a name and what punctuation follows it. `breeze.plan_takes` and `takecheck.expected_words` both use it. It needs the bible and not the say-as map, because the map only has the terms with a `say`. The Drama 3 render keeps its own text.
7. Don't change the key of a take whose audio would not change. A take's key is a hash of its text after say-as, its voice, clip, direction, seed, model and controls. Add a field to the key only when the field is set and changes the audio. A package that touches the text or the key has a test that pins the keys of a fixed script. Before each deploy, count `reusable_takes` over the library and explain every take a render would make again.
8. Thresholds and baselines come from AJ's own takes. The read-only `measure-takes` command runs the measures over saved takes and prints each voice's distribution and what a threshold would flag. Run it in a scratch copy on a breeze node. Don't run it on containers, which has no numpy. The check stores each voice's baseline for the chapter in `take_check.json` (the median and spread of level and rate, and the count), because a render that makes a few lines only hears those. A voice with few lines uses fixed floors.
9. The retake budget has three limits. A line gets nine generations at most, as today. A render has a ceiling on retakes. When one reason flags more than a set share of one voice's takes, the cause is the voice, for example clipping copied from its cast clip. The render then reports a voice finding, doesn't retake those takes, and the Cast tab shows the finding.
10. Fix rendered chapters once per phase, through the backlog lane. A package is deployed when it is done, and it affects new renders at once. The pass over rendered chapters runs once, at the end of the phase. A chapter is in the pass only when a render would change something: it would make or retake a take, or the join's version is newer than the one in the chapter's `render` record. A take that has been through every round doesn't count, or its chapter would be queued forever. Ask AJ where he is, do the chapters ahead of him first, and let the app run backlog jobs only when no other job waits.

## Every package ends the same way

* Run the unit tests and the fake Nomad test from `CLAUDE.md`. After a change to `fx.py`, `breeze_join.py` or `take_measure.py`, also run `tests/test_fx.py` under the audio Python.
* Give each new measure a known-answer test. For loudness, a full-scale 997 Hz sine reads -3.01 LKFS.
* Render a scratch copy with its own copies of the chapter's takes, because a retake replaces a take's file in place. Chapter 33 of The Hundred Reigns is the reference chapter. Repoint `file` in the scratch copy's `breeze_takes.json` before the render.
* Run the blind review, and the Codex review beside it, before you report a package done.
* Agree every deploy with any other session in the repo, because a deploy sends the whole working tree. Afterwards, check that the deployed files match the tree by hash.
* Bring `README.md`, `DECISIONS.md` and this folder up to date in the same session.

## Lessons from the first step

* The first version of the retake of saved takes made a flagged take again from its seed and wrote it over the saved file before comparing them. The blind review caught it. Compare before you replace, and assume a seed doesn't give the same audio on the other node.
* The app queued a render with seed 1 unless the request named one. Chapters 13, 16 and 22 of The Hundred Reigns were rendered with seeds 3, 2 and 2. Package S1 of phase 1 fixed this on 2026-11-12, and a render that doesn't name a seed now keeps the chapter's.
* A render on a node that didn't make a chapter's takes fetches them from the app's mirror. A file that a later job needs from a node has to go to the mirror, in `worker.media_files`.

## Lessons from phase 1

* A new limit has to hold across renders and not only inside one. The first version of the retake ceiling left a line for the next render, which then started the line's rounds again and asked for seeds it had rejected. Both blind reviews caught it. A line now goes on from the round its take records.
* A change that makes saved evidence out of date sends every saved take through the check again. Ask what that does to a take that already used up its retakes.
* A scratch `check-takes` on the laptop holds every cluster GPU job there until it ends (`CLAUDE.md`, Gotchas). Gather evidence for a whole book from a script that calls `takecheck.check_takes`, which took 84 minutes for 9,383 takes beside other work.
* A render trusts the verdicts in a chapter's check file, so a new rule didn't reach a saved take until `take_check.json` got a version of the rules. Ask of every new rule how a take that was judged before it gets judged again.
* The token map marks a name by putting a marker word in the text and reading the words back. A rule in `asr.words` that looks at a word's letters, like the possessive after an s or the stutter, doesn't see through the marker. `breeze.placed_names` places the names by their spoken words when the two word lists differ. Test a new rule of `asr.words` on a line with a name.
* Look at a measure's spread over real takes before you store it. The first loudness spread used windows of 0.4 s, and ordinary speech filled its range, so it could not have told a fading take from a normal one.
* The recognizer's words depend on the node. On 40 takes the Mac and the laptop measured the same `signal` block every time, and spelled a name differently in five transcripts. Don't expect a verdict that hangs on one name to be the same on both nodes.

## Lessons from phase 2

* On the laptop, Python's `write_text` writes CRLF, so an edit script that reads a file with `read_text` and writes it back turns an LF file into CRLF. git normalizes the endings and the deploy sends LF, but a diff against a deployed copy then shows every line. Read and write bytes, and keep the file's own endings.
* aj-legion5 hard-reset three times on 2026-11-12, at 08:19, 19:03 and 19:41 (Kernel-Power 41, with no bug check). The last two came while a scratch gathering ran on its CPU beside the app's backlog checks, and the first came before that work. jsc-m1 is there for long CPU work when the laptop is busy: it gathered 22 chapters' blocks in 21 minutes.
* A word that a recognizer hears after a line's last word is often its own way of hearing that word, such as "keep, Anna" as "keep on, huh?". Count it only when the take's sound goes on past the word.
* On a line of one to three words, two recognizers of different families hear the same homophone ("Simon guessed" as "Simon guest" nine times), so a second opinion doesn't settle a short line.
* A direction changes a voice's timbre on purpose ("echoing through the hall", "deep, armored voice"), so judge drift only on lines without one.
* Eight takes say little about a spelling that most takes get right. Three screens of the same six options, with different lines, gave AJ's pick "Eh-ole" 5, 5 and 8 of 8, and the library's 328 takes say it one way 84% of the time. Count what a small sample can tell before a rule hangs on it, and let a second sample add to the first.
* A cloned voice says a spelling its own way. Vouivre's own voice said "Vwee-vruh" as "wih-ver" where the narrator says "vwee-vroo", so measure a name in the voices that say it.
* A reference cut from a few takes of one line carries the line's neighbours. Take it from lines whose words beside the name differ, and keep an end phone only when half of the takes have it.
* A new input to judging needs the same care as a new rule. The name's reference came after a chapter's check, and nothing judged the saved takes again until the check file recorded the references it was judged with. Both reviews caught it.
* Have AJ listen before a rule that judges how something sounds goes live. P4's name rule passed every test and a scratch render, and his marks on 40 clips then agreed with it on 23. The page took him a few minutes. Build the page before the rule, from the first measurements, and hide what the measure says of each clip.
* A measure that lets a reference match any part of a window can't hear what was added. Five of the seven takes it passed and AJ rejected say "ay-oh-lee" or "ay-oh-lay" for "Eh-ole".
* Two recognizers of different kinds that have to agree on the same added sound made a flag worth acting on. One recognizer's phones gave 10 false flags in 17, and the two together gave 2 in 25.
* Compare what the ear compares. AJ accepts a vowel that sounds different and rejects an added syllable or consonant, and the first measure weighed those the other way round. Read the marks for the kind of difference he rejects before choosing a distance.
* Draw a listening page at random within what a rule flags and within what it passes, and keep the count each sample was drawn from. The marks then give how often a flag is right and how often a pass is wrong. The first page was picked near the limit and could give neither.
* Parakeet's word times run about 0.2 s late in running speech against ZIPA's phones, so a word's span holds the next word's first sounds. A sound after a name is the name's only with a second sign.
* Judging 9,971 saved takes again took 69 s on the laptop, about 7 ms a take. A page of the app can't do that on a request, so it reads what a check or a render has written.
* A model whose files are all there can still fail to load. Try the load once in a process before a block counts, or a node hears its takes again on every render.

## Labelled takes

The ten takes that the check had flagged before 2026-11-12 were replaced in the library that day, so no copy of them was left. On 2026-11-12 each was made again from its first seed on the laptop, outside the library. Eight came out flagged again: chapter 16 line 207, chapter 19 line 235, chapter 22 line 67, chapter 24 line 236, chapter 26 line 117, chapter 27 lines 178 and 236, and chapter 33 line 72. Chapter 13 line 44 and chapter 30 line 62 came out with their words right. Seven of the ten are between 12.1 and 13.3 s long, and the long lines among them stop there with their last words missing. The library's takes run to 37 s and don't pile up at that length, so Breeze seems to end a take near 13 s when it loses the line. Chapter 27 line 178 fades by about 15 dB from its start to its end, and chapter 30 line 62, whose words are right, has the largest step in loudness of the ten. The ten takes and their evidence are in `D:\fish.wt\scratch\labelled` on the laptop. The script that made them is not in the repo, and it is short: plan the chapter, then ask breeze-server for the line with its voice seed.

## Scratch data

The laptop has a scratch copy of The Hundred Reigns with the evidence of all 9,383 takes, in `D:\fish.wt\scratch\tq`: the library without audio as it was on containers on 2026-11-12, the takes copied from the app's mirror under `media/`, and in every chapter from 7 to 48 a `take_check.json` with both blocks and the verdicts of the rules of 2026-11-12. Copy `serial_audio`, `prompts` and `schemas` beside it and run from that folder, so a command reads the copy's library. `measure-takes the-hundred-reigns 7-48 --stored` there prints any rule's counts in under a minute. The helper scripts beside it are `gather.py`, which hears chapters without a `check-takes` process, `plan_keys.py`, which hashes every planned take key, `c2_measure.py` for package C2, and `b1_analyse.py` and `b1_clips.py` for the join. To make the copy again, copy the library and the takes from containers, point each `file` at its copy, and hear the chapters with `gather.py`, which took 84 minutes.

## Where things stand

As of 2026-11-13:

* Done and deployed: a render retakes a saved take when a check flagged it (`DECISIONS.md`, "A render takes a saved take again when a check flagged it"). The Hundred Reigns had no flagged take in 9,383 afterwards.
* Phase 1, S1: done and deployed on 2026-11-12 (`DECISIONS.md`, "A render keeps the chapter's seed").
* Phase 1, F1: done and deployed on 2026-11-12 (`DECISIONS.md`, "A take has one evidence record, and one function judges it"). A throwaway serial was cast and rendered on aj-legion5 with the deployed code, and its 11 takes came back with both blocks. The Mac ran the measure tests and the recognizer child in a scratch folder, and no cluster job has run on it since the deploy. The library's 9,383 saved takes don't have the blocks yet. Each chapter gets them on its next render or check.
* Phase 1, F2: done and deployed on 2026-11-12 (`DECISIONS.md`, "Backlog work runs only on an empty queue"). On the cluster, a backlog cast and a backlog render of a throwaway serial ran after a job that was queued behind them, the cast through both its stages, and the render after the cast. `backlog <slug>` on a scratch copy of The Hundred Reigns agreed with `reusable_takes` for all 48 chapters. It lists chapters 1 to 6, which Drama 3 rendered, and chapters 7 to 10, 15 and 18, whose voices were recast or cast after their renders. Whether to render those again is AJ's open question in `DECISIONS.md`, so leave them out of a backlog pass until he answers.
* Phase 1, A1, A2 and A3: done and deployed on 2026-11-12 (`DECISIONS.md`, "The take check flags on words, endings, short lines and the signal, and cuts long pauses"). `measure-takes` over the scratch copy of The Hundred Reigns gave the counts there, and the same counts after the fixes from the blind review. No cluster job has run with these rules yet. The cuts of A2 are recorded in `repairs.cuts` and nothing applies them until B1.
* Phase 1, A4: done and deployed on 2026-11-12 (`DECISIONS.md`, "A flagged long line is taken again in pieces"). A scratch render of chapter 43 took line 48 again in 2 pieces.
* Phase 1, C1 and C3: done and deployed on 2026-11-12 (`DECISIONS.md`, "A script line records what the later checks will read, from a pass of its own" and "The screenplay pass compares the script's words with the chapter's"). C1 was built as a marks pass and not as fields of the screenplay pass, because the fields cost that pass three quarters more output. Nothing runs the marks pass yet. A screenplay pass runs it with `--marks`, and the phase that first reads the fields turns it on and adds its job. The audit lists one script, chapter 31, and AJ hasn't decided whether to write it again.
* Phase 1, B1: done and deployed on 2026-11-12 (`DECISIONS.md`, "The Breeze join levels each voice and each take, and masters the chapter"). A scratch join of chapter 33 measures -18.1 LUFS and a true peak of -1.2 dB on the Opus file, and a render of a throwaway serial on aj-legion5 recorded join 2, -18.1 LUFS and -1.9 dB. Every new render uses this join. AJ heard the two clips on 2026-11-12, found the difference subtle and keeps the new join with its offsets and gaps as they are.
* Phase 1, B2: done and deployed on 2026-11-12 (`DECISIONS.md`, "A new cast clip is leveled to -18 LUFS"). A cast of a throwaway serial on aj-legion5 leveled its three candidates to -18.0 LUFS.
* Phase 1, C2: done (`DECISIONS.md`, "The text a voice is sent gets no new repair"). The measurement doesn't support a repair, so the keys don't change.
* Phase 1, D1, D2 and D3: done and deployed on 2026-11-12 (`DECISIONS.md`, "The Names tab shows how each name was heard and lets AJ hear respellings before he picks one"). On a second instance of the app over the scratch copy, a pick changed the name's say-as and the next render would make only the lines that have the name. On the cluster, a `say-options` job for a word of a throwaway serial ran its two stages, and the app served the takes from its mirror. The throwaway serial is deleted. Hear it, which makes a clip of a spelling AJ types, was added and deployed the same day at his request. AJ picked spellings for Eole and Vouivre on the tab that evening.
* On 2026-11-12 a take check of chapters 36 to 48 and then 11 to 35, without 15 and 18, was queued on the app as backlog work. It hears every take again, writes each chapter's check with both blocks, the new verdicts and the names, and changes no audio. It makes nothing again. The Names tab reads its names from these checks. The 36 checks finished that evening.
* AJ started the backlog renders himself on 2026-11-12 from the Names tab. After his picks for Eole and Vouivre he queued a backlog render of the 31 chapters that say either name, and each render also gives the chapter the new join. Chapters 11, 25, 30, 35 and 47 say neither name and still have the old join, so they still need a backlog render. Phase 1 also waits for what AJ says is wrong with the tab. Chapters 1 to 6 are Drama 3 renders, and chapters 7 to 10, 15 and 18 have takes of voices that were cast again since, so a backlog render of those makes many takes, and they stay out of a pass until he decides.
* Phase 2, P0: the spike is done, and the list of models went to AJ on 2026-11-12 (`docs/machines.md`, "The take check's other models", and `DECISIONS.md`, "The take check's other models are Silero VAD v5, TitaNet small, Moonshine base and ZIPA small"). AJ approved it that evening, and the four folders are on both nodes, checked by hash. Each node's take check loads all three blocks.
* Phase 2, P1, P2 and P3: done and deployed on 2026-11-12, first at 22:04 with version 3 of the rules and then with version 4 (`DECISIONS.md`, "The take check flags a tail, a drifting voice and a take both recognizers hear wrong, and cuts a tail"). Their numbers come from a scratch copy whose takes have all five blocks, in `D:\fish.wt\scratch\p2` on the laptop for chapters 7 to 26 and in `/Users/aj/serial-audio/scratch/p2` on the Mac for 27 to 48. A scratch render of chapters 44, 42 and 33 on the Mac took every flagged line again, kept a new take for each, and cut two tails. With the models on both nodes, the first render or check of each chapter hears its saved takes for the three new blocks. On 2026-11-12 a take check of chapters 40 to 51 on the cluster, the chapters ahead of AJ, gathered the blocks in about a minute a chapter. It flagged `drift` in chapter 44 line 45, chapter 50 line 163 and chapter 51 line 77, and `garbled` in chapter 49 line 129, and it left tail cuts to make in chapters 40, 41, 42 and 46.
* Phase 2, P4: done and deployed on 2026-11-13 at 12:40, in its second version (`DECISIONS.md`, "The take check flags a name when both recognizers hear a sound its spelling doesn't have"). The first version went out at 09:40 and was switched off at 10:13, after AJ's marks on 40 clips agreed with it on 23. The second passed his marks on 55 new clips: of the 25 it flags, 23 sound wrong to him. Its numbers come from `D:\fish.wt\scratch\p4` on the laptop: the library as containers had it that morning, the 826 takes that say Eole, Vouivre, Louis or Lauriane with their phones in `phones.jsonl`, and every take of chapters 21 and 22. `skel.py` there is the prototype, `v2_score.py` scores it on the first marks and counts its flags, and `v2_marks.py` scores it on the second. `listen.py` and `listen2.py` built the two pages, and `listen\key.json`, `answers.txt`, `key2.json` and `answers2.txt` hold each clip's take and his marks.
* What the name rule does to the library as of that deploy: every name with a say-as is held to it, and a chapter's saved takes are judged by it at the chapter's next check or render. A backlog take check of the 40 chapters whose check holds names ran at 12:41 that day for this. It heard nothing again and changed no audio. By it, 140 of the 345 takes that say Eole add a sound, 6 of 157 for Vouivre, and none of 140 for Louis and 186 for Lauriane. At four takes of "Eh-ole" in ten, the breaker will hold Eole's retakes in most chapters, and the Names tab reports the name. Its fix is a spelling that screens clean, which is AJ's to pick on the Names tab. No render has run with the rule yet.
* What the name rule doesn't hear is the open part of P4: a vowel that only sounds different ("vroo" for "vruh", 5 of the 6 clips of Vouivre it passed), an added vowel in a possessive or before a vowel or an h when Parakeet's spelling doesn't show it (3 of the 15 clips of Eole it passed), and a sound the take drops. A rule for any of these needs its own listening page first. `p4-name-check.md` has the steps, N0 to N7, and none is started.
* Phase 2, P5: not started. It measures first, and the chapters scripted before C1 need the marks pass for it, one Claude pass a chapter.
* AJ was on chapter 43 on 2026-11-13. A backlog take check of chapters 43 to 52 and then 11 to 42, without 15 and 18, ran on the app that day and gathered the `phone` block for all 40. It changed no audio.
* Ahead of AJ on 2026-11-12, a render would take a line again in chapters 44, 49, 50 and 51 and cut a tail in 40, 41, 42 and 46. He hasn't queued those.
* Phase 3: not started.
