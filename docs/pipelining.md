# Job handing: what is left

This is an addendum to the job handing of 2026-11-13. Since then a cluster worker runs its node's Breeze jobs back to back in one Nomad task, and breeze-server and the Parakeet process stay up between them. `DECISIONS.md`, "A worker runs its node's Breeze jobs back to back, and what it loaded stays up", has what was built and why. `deploy/README.md`, How a job runs, point 6, has how a handover goes. This file has what is not checked, what is not built and what AJ hasn't decided. Start it with "continue the job handing in docs/pipelining.md".

## Where it stands

As of 2026-11-14 at 08:30:

* Four changes are deployed since 2026-11-13 at 23:11: the half-second probe of a local port (`machine.listening()`), the one Parakeet process a job's passes share (`asr.serving()`), the handing itself (`nomad._hand_next()` and `worker.fetch_next()`), and the scheduler's count of takes outside the app's lock (`nomad._takes_needed()`). The workers ran code version `138be1755db0c0ac` that night.
* AJ's Process batch for chapters 53 to 83 of The Hundred Reigns ended at 04:44, and every chapter has its audio. Of the 36 renders that ended after the first deploy, at 22:12, none failed and 26 ran handed: 19 of 22 on aj-legion5, 4 of 8 on jsc-m1 and 3 of 6 on dev-arch. Each handed render used the breeze-server that was up.
* The time from the end of the job before to the first finished take, without that take itself, at the median: 12.6 s handed on aj-legion5, 8.3 s on jsc-m1 and 6.9 s on dev-arch. A dispatched render took 24.9 s, 30.3 s and 31.7 s from its dispatch, and a dispatch comes some seconds after the job before ends. So a handover saves about 12 s a job on the laptop and 22 to 25 s on the two slow nodes.
* A whole chapter takes about 1,300 s on the laptop, so the handover saves it about 2% with the recognizer's starts. On jsc-m1 and dev-arch a whole chapter takes 70 to 100 minutes, so it saves them about half a percent. The gain is in touch-up renders, which take a few minutes each.
* The one job that failed since the deploy is "Say the options for Grius again" at 08:01 on 2026-11-14, with "'Grius' has no options yet". The Names tab offered Screen again for a name without options. The handing has no part in it, and the session "next" fixed the button that morning.
* The 326 unit tests and `tests/e2e_fake_nomad.py` passed on the laptop on 2026-11-14 at 08:10, on the tree with every session's work of the day before. The end-to-end test has the scenario "Two breeze jobs queued together run in one task". It also passed on Linux with the deployed code.
* `HAND_NEXT` in `serial_audio/web/nomad.py` switches the handing off, and `SERVE` in `serial_audio/asr.py` the serving process. Each needs a deploy of its file.

## What is left to do

### H1: measure a batch of touch-up renders

* A touch-up render of 272 s should save about a seventh: 17 s at the handover and 20 s of recognizer starts. That is worked out from the parts and not measured. Six touch-up renders ran in the night of 2026-11-13, on three nodes, which is too few.
* After the next backlog batch of twenty renders or more on one node, compare the handed renders with the dispatched ones, by takes made. A handed job's record in `state/jobs.json` has `nomad.follows`, `nomad.marks` has the time each part of the job started, and the job's log has "first take done in".
* A render's log says "breeze-server is still up at" when it used the server of the job before.

### H2: the Codex review

* The change had two Opus blind reviews, with one medium finding each and no high one, and everything they found that mattered is fixed. The Codex review didn't run, because Codex had hit its usage limit on 2026-11-13 at 21:31. Run the codex-review skill on the change in a later session.

### H3: leftovers of the test serial

* The throwaway serial `pipeline-test` is deleted from the library, the mirror and the laptop. Its three finished jobs are still in the Jobs list, and `state/placement.json` on containers still names its chapters. Clear finished on the Jobs page removes the jobs. The placement entries do no harm.

## Known limits

* A worker's run of handed jobs ends when the app doesn't have a job for its node at that moment, and after a deploy, because a worker asks for a next job only while each job has the code of its first one. The job that comes next is dispatched. By their times, the 10 dispatched renders of the first night are each node's first job under the new code, the jobs after the two later deploys, and two backlog renders that could start only after the batch's last render had ended on another node. Nobody has checked each of the ten.
* While a worker has a job coming, breeze-server holds 3.8 GB of the laptop's card through the join. A step that finds ComfyUI busy stops the kept server before it waits.
* A handed job whose bundle doesn't unpack fails. A handed job whose task ends before it fetched the bundle is queued again.
* On macOS and Linux a worker that is killed leaves its kept processes up until the node's next GPU job takes the lock and stops them. The Parakeet process also ends by itself after 30 idle minutes. On Windows a job object ends them with the worker.

## Open questions for AJ

* Whether two choices made without him stand. The handing runs inside the Nomad task a job already has, so the job spec and `bootstrap.py` didn't change and nothing was registered. The other design, a breeze-server that outlives its task, was rejected because it holds the card with no job to answer for it. And `_takes_needed()` was changed in the estimates' code, because its count of every queued render inside the lock stalled the app for 22 s after each result.
* Whether a Claude job is handed to its worker too. It would save about 3.6 s of a 70 s character pass, and a Claude pass doesn't load anything the next one could use. `HAND_CLASSES` and `worker.NEXT_CLASSES` name the classes.
* Whether a handed job's bundle holds only the files that changed since the bundle before it. A bundle of The Hundred Reigns is 35 MB, and building, fetching and unpacking it took 8.5 s of the about 12 s between two jobs on the laptop.
* Whether breeze-server gives up the card during a join while its worker has a job coming. A ComfyUI prompt of his own that starts during a join shares the card with it.
* Whether the laptop's registry of kept processes is closed to other users. `C:\ProgramData\serial-audio\gpu.kept.json` holds the token of the kept Parakeet process, and any user of the laptop can read it.
* Whether the kept Parakeet process outlives the takes of a whole chapter. It ends after 30 minutes without a job (`SERVE_IDLE` in `serial_audio/parakeet_asr.py`), so on jsc-m1 and dev-arch the check before the join starts it again, in about 5 s.

`DECISIONS.md`, "Not decided yet", has the last five with their numbers.
