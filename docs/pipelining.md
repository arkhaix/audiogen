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

* The change had two Opus blind reviews, with one medium finding each and no high one, and everything they found that mattered is fixed. The Codex review didn't run then, because Codex had hit its usage limit on 2026-11-13 at 21:31.
* Codex reviewed the whole commit, 936a94d, on 2026-11-14 in 29 minutes. It found nothing high, three medium points and two low ones, and all five hold. Two of the medium ones were new:
  * The result of a render or a take check didn't mark the kept take counts of its chapter, so a second queued render of that chapter kept its old count, and with it its estimate and its node, for up to ten minutes. AJ's queue of that morning had three such pairs.
  * The app marked a handed job as contacted when it built the bundle. When every answer to the worker's six fetches was lost, the job that never started was marked failed and not queued again.
  * The third medium point is the first gap of `docs/scheduling.md`, S2, which was known. One low point is a comment. The other is that `render_progress()` no longer reads a check header in a log without a Breeze line. No real log is like that: a Drama 3 render has no take check, because only `breeze.py` calls it and it refuses a render that is not Breeze, and a Breeze render's log names a Breeze step beside every check. So that point gets no fix. A first fix of 09:10 put the branch back with a test that called it a Drama 3 render's check, and the Opus review of the fix caught that the claim was false.
* The three medium points and the comment are fixed in the tree as of 09:50 that day, in `serial_audio/web/nomad.py`, with tests in `tests/test_kept.py`, and the 329 unit tests pass. An Opus blind review and a Codex review of the first fix ran beside each other. Both found that a handed job which is stopped after its bundle was sent, and whose task then ends without a part of its log, would be queued again and run again. Both found that a bundle whose three builds each saw a result was sent as it was. Neither found anything high. What the fix does now:
  * The result of a render or a take check marks the kept counts of its own chapters for another count, and the result of any other job marks every count of its serial, as before.
  * A handed job is queued again when its worker was never sent the bundle, or was sent it and sent back no part of its log. A job that was asked to stop is not, and a job is queued again twice at most (`TAKE_BACKS`). After that it fails with the end of its task's stderr in its log.
  * The app counts the results it applies to a serial and builds a bundle again when one landed during the build. After three such builds (`BUNDLE_BUILDS`) it answers 503, which `bootstrap.py` and `worker.fetch_next()` both answer with another fetch.
* The fix was deployed on 2026-11-14 at 09:55, when AJ's queue of backlog renders had ended. `tests/e2e_fake_nomad.py` passed on the tree just before, 60 checks, and the 59 deployed files match the tree by hash. The second round of the fix, which is the stop, the limit of two and the 503, got its own Opus and Codex reviews at 10:17. Codex had no finding, and Opus had six low ones. Four were taken: tests for three lines that none pinned, and another key for a job past its first stage that is queued again, so a worker left from the hand-over can't send a result for the stage's next run. That round was deployed at 10:44 with the fix to the script check (`docs/scheduling.md`, S1), after `tests/e2e_fake_nomad.py` passed again, and the 59 deployed files match the tree by hash. The 330 unit tests pass. A throwaway serial's batch of 12 jobs ran on the first round at 10:04, with two renders handed, and a screenplay and a render ran on the second. Nothing is committed.
* Two low points of that review were left as they are. A chapter whose heading and first words tie under the script check counts its first line as the heading, which can report one or two words as dropped, under the limit. And `taken_back` on a job is read as a number without a check of its type, which only `_take_back()` writes.
* The app's log held six `ConnectionResetError` tracebacks after that restart, all within seconds of it: one tailnet address reset idle connections while the app waited for a request line. No job is involved.
* Found beside it and not changed: `nomad._remaining()` adds the time of a take check to what is left of a render that is not Breeze, which has no check. So the scheduler counts a Drama 3 render as longer than it is, by 30 s and a part for each line.

### H3: leftovers of the test serial

* The throwaway serial `pipeline-test` is deleted from the library, the mirror and the laptop. Its three finished jobs are still in the Jobs list, and `state/placement.json` on containers still names its chapters. Clear finished on the Jobs page removes the jobs. The placement entries do no harm.

## Known limits

* A worker's run of handed jobs ends when the app doesn't have a job for its node at that moment, and after a deploy, because a worker asks for a next job only while each job has the code of its first one. The job that comes next is dispatched. By their times, the 10 dispatched renders of the first night are each node's first job under the new code, the jobs after the two later deploys, and two backlog renders that could start only after the batch's last render had ended on another node. Nobody has checked each of the ten.
* While a worker has a job coming, breeze-server holds 3.8 GB of the laptop's card through the join. A step that finds ComfyUI busy stops the kept server before it waits.
* A handed job whose bundle doesn't unpack fails. A handed job whose task ends before it fetched the bundle is queued again. Since the fix of 2026-11-14, a job whose worker was sent the bundle and never sent a part of its log is queued again too, twice at most. That covers a bundle that doesn't unpack, which then fails on its third worker with the error in its log.
* On 2026-11-14 at about 08:33, one request for `/api/state` got an empty answer and the next took 16.3 s, within a minute of AJ queueing about 48 backlog renders in one request. Nobody has found the cause. It did not come back: of 90 requests over the next three minutes, while backlog renders ended and were handed on three nodes, none took over 1.5 s. So a handover doesn't stall the app with 60 renders queued, and a large submit may.
* On macOS and Linux a worker that is killed leaves its kept processes up until the node's next GPU job takes the lock and stops them. The Parakeet process also ends by itself after 30 idle minutes. On Windows a job object ends them with the worker.

## Open questions for AJ

* Whether two choices made without him stand. The handing runs inside the Nomad task a job already has, so the job spec and `bootstrap.py` didn't change and nothing was registered. The other design, a breeze-server that outlives its task, was rejected because it holds the card with no job to answer for it. And `_takes_needed()` was changed in the estimates' code, because its count of every queued render inside the lock stalled the app for 22 s after each result.
* Whether a Claude job is handed to its worker too. It would save about 3.6 s of a 70 s character pass, and a Claude pass doesn't load anything the next one could use. `HAND_CLASSES` and `worker.NEXT_CLASSES` name the classes.
* Whether a handed job's bundle holds only the files that changed since the bundle before it. A bundle of The Hundred Reigns is 35 MB, and building, fetching and unpacking it took 8.5 s of the about 12 s between two jobs on the laptop.
* Whether breeze-server gives up the card during a join while its worker has a job coming. A ComfyUI prompt of his own that starts during a join shares the card with it.
* Whether the laptop's registry of kept processes is closed to other users. `C:\ProgramData\serial-audio\gpu.kept.json` holds the token of the kept Parakeet process, and any user of the laptop can read it.
* Whether the kept Parakeet process outlives the takes of a whole chapter. It ends after 30 minutes without a job (`SERVE_IDLE` in `serial_audio/parakeet_asr.py`), so on jsc-m1 and dev-arch the check before the join starts it again, in about 5 s.

`DECISIONS.md`, "Not decided yet", has the last five with their numbers.
