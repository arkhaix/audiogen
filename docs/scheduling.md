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

### S2: the gaps that are known

Each is in `DECISIONS.md`, "A batch casts after each chapter's character pass". None is fixed.

* A render of an earlier chapter can fetch its bundle in the instant a later cast's result is written. If a character of that cast speaks in the render's chapter, the render fails because the voice doesn't have a record yet, and it has to be queued again.
* A cast with nobody to cast is skipped only when a Claude place is free for it, so it can wait behind three Claude jobs of another serial.
* The forecast counts a cast that will be skipped as a cast of one character, because nobody knows before the pass whether it brings someone to cast.
* The check for nobody to cast (`JobManager._nobody_to_cast` in `serial_audio/web/jobs.py`) reads the serial and its bible inside the scheduling pass, once per cast. That took 0.75 s for The Hundred Reigns. `CLAUDE.md` says why a slow scheduling pass delays every handover.
* Codex didn't review either change, because it had hit its usage limit that evening. An Opus blind review looked at each.

### S3: the chapters a later cast leaves with the narrator

* In a batch, a character who becomes castable only by a later chapter's pass has the narrator read their lines in the chapters that rendered before that cast. With one cast at the end of the batch those chapters had the voice.
* A later render of such a chapter makes only those lines again. Nothing queues that render today.
* Check whether the `backlog` list names such a chapter once the character has a voice. A line whose speaker gained a voice should have a new take key, and nobody has looked.
* What happens then is AJ's call (below).

### S4: dev-arch's open ends

* Confirm that the renders of that night ran on the Iris Xe and not on the CPU fallback. The job logs don't name the backend, and Claude found no server log from that night under `/home/aj/serial-audio`. A server log starts with `backend: Vulkan0` on the card and with `backend: CPU` without it (`docs/machines.md`, dev-arch). The CPU use during a render shows it too: about a quarter of one core on the card, and six cores on the CPU build.
* dev-arch's clock was 138 s behind containers' on 2026-11-14, and its time sync is off (`timedatectl` shows `NTP=no`). It was 135 s behind the day before. A render record made there has that node's time. Turning the sync on is a change to the host, so it waits for AJ (below).

## Open questions for AJ

* Whether a screenplay may start before its serial's cast, casting bios, voice pick or voice effect. It can since 2026-11-13, by Claude's choice. For it the cast list that a screenplay pass is sent lost its `voiced` column, the one thing a screenplay read that a cast writes.
* Whether the chapters that a later cast leaves with the narrator stay as they are, or the app queues them for a render again once the character has a voice (S3). AJ said yes to a cast for each chapter before he was told of this cost.
* Whether dev-arch stays a Breeze node as it is set up: on the Vulkan build, beside the Claude passes, as the user aj. Those are Claude's choices in "dev-arch renders Breeze on its Iris Xe".
* Whether to turn time sync on for dev-arch.
* Related, and already under "Not decided yet" in `DECISIONS.md`: whether to derive the scheduler's waits from the files each job kind reads and writes. The work of 2026-11-13 added to the exceptions in `conflicts()`.
