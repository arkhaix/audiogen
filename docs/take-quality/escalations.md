# Escalations: takes the retakes could not fix

This is an addendum to the take quality plan. AJ asked for it on 2026-11-13: when a take is still flagged after several more takes, there has to be a place that stores it and shows it to him, so he can look at it and decide. Nothing here is built. Start it with "do the escalations addendum in docs/take-quality".

## Why it is needed

* The plan's rule was that AJ can't review flagged audio, because he listens while driving, so every defect needs an automatic fix (`README.md`, What AJ decided). That stays the first answer. An escalation is what is left when the automatic fixes have run out.
* Today such a take stays in the chapter. The chapter's check lists its flagged lines, and that is all. Nothing collects them across chapters, nothing keeps the other takes that were tried, and nothing records what AJ made of one, so it counts as flagged for good.
* As of 2026-11-13, after the "Éole" batch: 12 takes of the name are still flagged after two rounds, in chapters 20, 22, 23, 34, 35, 36 and 40 (`p4-name-check.md`, N2). A name now gets five rounds. A round fixed about one flagged take in two in that batch, so expect two or three of the 12 to be left. Chapter 22 line 69 and chapter 36 line 22 are flagged too, for other reasons.

## What an escalation is

* A take that is still flagged after every round its reasons allow, `breeze.NAME_RETAKES` for a name and `breeze.CHECK_RETAKES` for anything else, and that no voice finding holds.
* A voice finding is its own kind and already has a place, the Cast tab. An unsteady spelling has the Names tab. An escalation is one take of one line.

## What it has to store

* The line: its chapter, its text as the voice was sent it, its direction and its voice.
* The take that was kept, with its evidence and its reasons, and the rounds and seeds that were tried.
* The other takes. Today a retake that is not better is deleted, and one that is better replaces the take in place, so an escalated line has one take left. For AJ to choose, a render has to keep the best few takes it tried for a line that ends up flagged, by `takecheck.better`, with their evidence. They have to reach the app's mirror (`worker.media_files`), and they go when the line is settled.
* What AJ decided, tied to the take's key and audio, so a new take of the line is judged afresh. The decisions are: this take is fine, use that other take, take it again, or change what the voice is sent. The app writes only through CLI jobs, so a decision is a job.
* A take he calls fine must stop counting as flagged, in `breeze.flagged_before()`, on the chapter page, in the `backlog` list and in the Names tab's count.

## Where it is shown

* One page for a serial, across its chapters, with the chapters ahead of where he is listening first.
* A row has the line's text with the word or name at fault marked, the reasons in plain words, a play button for the kept take and one for each other take, and the decisions as buttons.
* Where the evidence has a time, the clip is a few seconds around it: a name's span, a tail, a pause. Otherwise it is the whole take.
* It has to work at phone width, and the serial page shows how many are waiting.

## How it fits what exists

* A name with many escalations has an unsteady spelling, and the fix is a spelling. The row links to the name's card on the Names tab.
* His decisions are labelled data, as his marks on the listening pages are (`tests/data/name_marks.jsonl`). A reason he calls fine again and again is a rule that is too strict, and the counts say which.
* A decision that needs a take or a join queues backlog work, so it never holds up another job.

## Steps, in order

### E0: count them

* List the takes in the library that are flagged and have used their rounds, by reason and by chapter. `after2.py` in `D:\fish.wt\scratch\n1` reads most of what this needs.
* The count decides the size of the rest. A handful in a book needs a plain list. Hundreds would mean a rule is wrong, and the fix is the rule.
* Counted on 2026-11-14 at 08:20 from the check files on containers, 77 chapters: 24 flagged takes in 18 chapters. A render would take 11 of them again, and those were queued that morning. The other 13 have been through their rounds: 1 for a name, line 119 of chapter 75, and 12 for other reasons, which are `swing` in 5, `level` and `short` in 2 each, and `fading`, `drift`, `dropped` and `garbled` in 1 each. 11 of the 13 are in chapters 56 to 80, ahead of AJ, who was on chapter 55. So it is a handful, and a plain list is enough. The count read `verdict` and `reasons` of each entry and compared with the `backlog` list. It did not use `after2.py`.
* After that morning's backlog renders the book has 16 flagged takes. Two are flagged for a name after all five rounds: line 46 of chapter 22 and line 119 of chapter 75.

### E1: keep the takes that were tried

* A render keeps the best takes of a line that ends flagged, beside the kept one, with their check entries.
* Decide how many, where they live on the node and in the mirror, and when they are removed.

### E2: the record and the job

* A place in the chapter folder for the decisions, and a CLI job that writes one.
* `takecheck` and `breeze.flagged_before()` read it, so a take he calls fine is no longer flagged, and a take he replaces is judged again.

### E3: the page

* The list, the clips and the buttons, on a second instance of the app over a scratch copy first (`CLAUDE.md`, Testing).

### E4: use the decisions

* Add them to the labelled data, with a test that scores each rule on them, as for the name rule.

## Open questions for AJ

* Which reasons escalate: a name alone, or every reason that is left after its rounds, such as `swing`, `drift`, `tail` and `garbled`.
* How many other takes to keep for a line, and for how long.
* Whether a take he calls fine counts as said right in the Names tab's count for its name.
* Whether chapters behind where he is listening escalate at all.
