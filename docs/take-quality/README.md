# Take quality checks

AJ asked on 2026-11-12 for better checks on each Breeze take: silences, mispronounced words, names said different ways, the wrong word emphasized, and odd volume changes. This folder holds the plan for that work in three phases. Each phase file is written so a session that has only this folder and the repo can do it.

The research behind the plan is in `reports/TTS take quality checks.md`, with the researchers' notes and a survey of the code in `research_notes/TTS take quality checks/`. Read the report's summary and its section on chapter 33 before you start a phase.

## Files

* `README.md`: how to use this folder, what AJ decided, the shared contract, and where things stand.
* `phase-1.md`: use what the pipeline already has. No new model.
* `phase-2.md`: three small CPU models and a second recognizer. It changes hosts.
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
* The app queued a render with seed 1 unless the request named one. Chapters 13, 16 and 22 of The Hundred Reigns were rendered with seeds 3, 2 and 2. Package S1 of phase 1 fixes this. Until it is deployed, queue a render of an old chapter through `/api/jobs` with `seed` in the params.
* A render on a node that didn't make a chapter's takes fetches them from the app's mirror. A file that a later job needs from a node has to go to the mirror, in `worker.media_files`.

## Where things stand

As of 2026-11-12:

* Done and deployed: a render retakes a saved take when a check flagged it (`DECISIONS.md`, "A render takes a saved take again when a check flagged it"). The Hundred Reigns had no flagged take in 9,383 afterwards.
* Phase 1: not started.
* Phase 2: not started.
* Phase 3: not started.
