# Scheduling: what is left after 2026-11-13

This is an addendum to the scheduling work of 2026-11-13. Two things went live that day. A chapter's jobs wait only for the jobs of its own chapter, with a cast after each chapter's character pass. And dev-arch renders Breeze as the third node. Both are deployed, and both have open ends. This file has what is left to do and what AJ hasn't decided. Start it with "do the scheduling addendum in docs/scheduling.md". `DECISIONS.md` has the reasons, in "A chapter job waits for a character pass only on its own chapter", "A batch casts after each chapter's character pass" and "dev-arch renders Breeze on its Iris Xe".

## Where it stands

As of 2026-11-14 at 08:15:

* A screenplay, a render or a take check waits for a character pass only on its own chapter, and a screenplay doesn't wait for a cast, casting bios, a voice pick or a voice effect. That was deployed on 2026-11-13 at 22:03.
* The Process dialog, and an import that processes its chapters, queue a cast after each chapter's character pass, and a render waits for the casts up to its own chapter. That was deployed at 22:40.
* No batch has been queued since then. The last one, for chapters 53 to 83 of The Hundred Reigns, was queued at 20:52 with one cast after its 30 character passes, and the passes and the cast had ended before either deploy. So on the cluster no screenplay has started beside a character pass, and no cast has followed a chapter's pass. The unit tests and the fake Nomad test cover both.
* That batch ended on 2026-11-14 at 04:44 with every job done. aj-legion5 made 21 of its 31 renders, jsc-m1 made 6 and dev-arch made 4: chapters 59, 65, 72 and 81, in 87 to 110 minutes each. dev-arch also made 2 of the 6 backlog renders that followed. None of its jobs failed.

## Steps, in order

### S1: watch the first batch that casts chapter by chapter

* The first Process batch, or import with processing, that has a character pass for two or more chapters is the first real run. Don't queue one on AJ's chapters to see it. A throwaway serial with three short chapters and a character who needs a voice shows the same. Queue it behind whatever AJ is waiting on, and delete it afterwards.
* Look for these in the queue and in the job logs:
  * The passes and the casts alternate in chapter order, and each cast after the first reads "Cast · ch N".
  * A screenplay starts while the pass or the cast of a later chapter runs.
  * A chapter's render starts when the casts up to its chapter and its screenplay have ended, while later passes and casts are still queued.
  * A cast that finds nobody to cast is skipped with the note "nobody needs a voice, so there is nothing to cast".
  * Every screenplay and render writes back its own files only, and nothing appears in `state/conflicts` on containers.
* Time the chain of passes and casts. A cast's voices need a GPU node, and once the first renders of the batch hold all three nodes, a cast waits for a render to end and the next pass waits with it. The 30 passes of the last batch took 36 minutes with no cast between them.
* Compare what the casts take with their estimate. A cast that follows a chapter's pass is costed as one character at its number of candidates (`_cast_units` in `serial_audio/web/nomad.py`).

AJ's own first batch after the deploys is not that run. He imported chapters 84 to 100 of The Hundred Reigns with processing on 2026-11-14 at 12:11, and the batch has one cast after its 17 character passes. The page of the app builds an import's jobs with `processJobs()`, which queues a cast after each pass, so the browser that queued this batch ran a copy of `app.js` from before 2026-11-13 at 22:40. The batch runs the way batches did until then.

Done on 2026-11-14 from 10:03 to 10:09 with the throwaway serial `cast-order-test`: three chapters of about 100 words, each with a speaker the chapter before didn't have, queued as one batch of 12 jobs the way the Process dialog queues them.

* The passes and the casts alternated in chapter order, and the casts after the first were titled "Cast · ch 2" and "Cast · ch 3".
* A chapter's screenplay started the moment its own character pass ended, beside that chapter's cast. No screenplay ran beside a later chapter's pass, because each of these took 12 to 20 s and was over before the next pass began.
* The renders of chapters 1 and 2 each started in the second their chapter's cast ended, while the passes and casts of later chapters were still queued. Both were handed to the laptop's worker as it finished the cast's voices.
* A fourth cast, queued when everybody had a voice, was skipped with "nobody needs a voice, so there is nothing to cast".
* Nothing appeared in `state/conflicts`.
* The chain took 262 s for three chapters, where each brought someone to cast. A cast took 102 s for two characters and 56 and 61 s for one, of which 21 to 30 s were its briefs on dev-arch and 26 to 64 s its voices on the laptop. The next pass waited for each cast, so a batch whose chapters each bring a new speaker spends about a minute a chapter on casts before its last chapter can be scripted.
* A cast after a chapter's pass is costed at 3 candidates. On the laptop the two such casts took 26 and 34 s for their voices, where the formula without recorded timings gives 77 s. The app has their timings now.
* The screenplay of chapter 3 failed, and its render was skipped. The cause is not the scheduling. The chapter's text opens with the words of its title, "The harbor clerk", so the script check took the spoken heading for repeated text (`screenplay.text_runs()`, `_opens()`). A chapter whose text opens with its title of two words or more failed its screenplay on both attempts. Nobody knew this before. It is fixed and deployed since 10:44 that day: the first line counts as chapter text only when it is closer to the chapter's first words than to its heading (`DECISIONS.md`, "The screenplay pass compares the script's words with the chapter's"). Chapter 3's screenplay and render then passed on the cluster. An Opus review scanned the 86 real chapters on containers, and none changes its result.
* The serial was deleted at 10:50: its folder in the library, its 66 files in `state/media`, the EPUB in `incoming/_new`, and its three output folders on the laptop. Its finished jobs are still in the Jobs list, and `state/placement.json` may still name its chapters, which does no harm.

### S2: the gaps that are known

Each is in `DECISIONS.md`, "A batch casts after each chapter's character pass". None is fixed.

* A render of an earlier chapter can fetch its bundle in the instant a later cast's result is written. If a character of that cast speaks in the render's chapter, the render fails because the voice doesn't have a record yet, and it has to be queued again. Codex reproduced it on 2026-11-14 with a forced order of the two. It was fixed and deployed that day at 09:55: the app counts the results it applies to a serial, and builds a bundle again when a result landed during the build. After three such builds it answers 503 and the worker fetches again. It covers a result from a worker. A job that the app runs itself, such as a voice pick, writes the library outside that count.
* A cast with nobody to cast is skipped only when a Claude place is free for it, so it can wait behind three Claude jobs of another serial.
* The forecast counts a cast that will be skipped as a cast of one character, because nobody knows before the pass whether it brings someone to cast.
* The check for nobody to cast (`JobManager._nobody_to_cast` in `serial_audio/web/jobs.py`) reads the serial and its bible inside the scheduling pass, once per cast. That took 0.75 s for The Hundred Reigns. `CLAUDE.md` says why a slow scheduling pass delays every handover.
* Codex didn't review either change that evening, because it had hit its usage limit. An Opus blind review looked at each. Codex reviewed both on 2026-11-14 with the rest of commit 936a94d, and its one point on them is the gap above (`docs/pipelining.md`, H2, has the review).

### S3: the chapters a later cast leaves with the narrator

* In a batch, a character who becomes castable only by a later chapter's pass has the narrator read their lines in the chapters that rendered before that cast. With one cast at the end of the batch those chapters had the voice.
* A later render of such a chapter makes only those lines again. Nothing queues that render today.
* Check whether the `backlog` list names such a chapter once the character has a voice. A line whose speaker gained a voice should have a new take key, and nobody has looked.
* What happens then is AJ's call (below).

### S4: dev-arch's open ends

* Seen on 2026-11-14 at 08:30, during a backlog render of chapter 47: breeze-server on dev-arch used 32% of one core, and the load was 0.37. That is the card and not the CPU build. Nobody has read a server log of the night before, so for those renders the point below still stands, though their times fit the card.
* Confirm that the renders of that night ran on the Iris Xe and not on the CPU fallback. The job logs don't name the backend, and Claude found no server log from that night under `/home/aj/serial-audio`. A server log starts with `backend: Vulkan0` on the card and with `backend: CPU` without it (`docs/machines.md`, dev-arch). The CPU use during a render shows it too: about a quarter of one core on the card, and six cores on the CPU build.
* dev-arch's clock was 138 s behind containers' on 2026-11-14, and its time sync is off (`timedatectl` shows `NTP=no`). It was 135 s behind the day before. A render record made there has that node's time. Turning the sync on is a change to the host, so it waits for AJ (below).

## Open questions for AJ

* Whether a screenplay may start before its serial's cast, casting bios, voice pick or voice effect. It can since 2026-11-13, by Claude's choice. For it the cast list that a screenplay pass is sent lost its `voiced` column, the one thing a screenplay read that a cast writes.
* Whether the chapters that a later cast leaves with the narrator stay as they are, or the app queues them for a render again once the character has a voice (S3). AJ said yes to a cast for each chapter before he was told of this cost.
* Whether dev-arch stays a Breeze node as it is set up: on the Vulkan build, beside the Claude passes, as the user aj. Those are Claude's choices in "dev-arch renders Breeze on its Iris Xe".
* Whether to turn time sync on for dev-arch.
* Related, and already under "Not decided yet" in `DECISIONS.md`: whether to derive the scheduler's waits from the files each job kind reads and writes. The work of 2026-11-13 added to the exceptions in `conflicts()`.
