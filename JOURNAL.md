# Journal: audiogen

Superseded decisions and the cases behind them, moved here from `DECISIONS.md`. Read this only when a current entry
cites it or the history is needed.

---

## 2026-11-08: node choice for renders and Breeze casts

### The case

The batch of chapters 10 to 12 of The Hundred Reigns ran one chapter after another. Chapters 10 and 11 rendered on
aj-legion5 at about 2.8 s per take, 12 minutes each. Chapter 12 started at 13:10, 2 s after chapter 11 ended, with
both nodes free. `state/placement.json` named jsc-m1 as the holder of its takes from a seed 1 render that morning, so
the app sent it to the Mac. The new render used seed 2, so it reused none of its 255 takes and ran at about 18 s per
take, about 80 minutes. On a scratch copy, `breeze.reusable_takes` gave (0, 255) at seed 2 and (231, 255) at seed 1.
The 24 lines missing at seed 1 are Lauriane's, who was recast that morning.

The same day a cast of eole and lorimor-ancitif failed on the laptop because ComfyUI was down for its Qwen3-ASR
check. AJ had Parakeet installed on the laptop, which removed the reason a Breeze cast preferred the Mac.

### Superseded text

From "jsc-m1 renders Breeze, and the laptop is preferred" (2026-11-07):

> **Decision:** jsc-m1 offers the `breeze` class. The app sends a GPU job to a free NVIDIA node first and to any free node second, one GPU job per node. A Breeze cast is the exception and takes a node without ComfyUI first, as the entry on the cast engine says. A job that needs a node's saved files goes back to that node, and a voice pick goes to the node that made the candidate it files.

From "Parakeet checks takes and casting candidates where ComfyUI is missing" (2026-11-07), the heading and:

> **Context:** jsc-m1 has no ComfyUI for Qwen3-ASR. AJ preferred Parakeet to whisper, and his meetings project had already chosen Parakeet TDT 0.6B v2 int8 through sherpa-onnx.

From "Breeze voice design is the default cast engine" (2026-11-08):

> **Decision:** `cast` uses Breeze voice design unless `serial.yaml` sets `cast_engine: qwen` or the cast passes `--engine qwen`. On the cluster a Breeze cast takes a node without ComfyUI first, because a ComfyUI node checks the candidates with Qwen3-ASR, which fails while ComfyUI is down. Voices cast before this keep their Qwen clips and Drama 3 voice files.

### Superseded the same day: the 90% reuse rule

The first fix for the chapter 12 case, deployed at about 13:40 and replaced that evening by the finish-time estimate:

> A render goes back to the free node that holds its saved takes only when it would reuse at least 90% of them there. At 6x the break-even is near 84%.

It picked the right node when both were free and said nothing about a busy one, so a render that became eligible while the laptop ran a short job still went to the Mac for an hour.

---

## 2026-11-12: jobs tied to the node that held their files

### The case

With the laptop off, AJ "wanted to recast a voice, but couldn't because aj-legion5 was turned off and some files were
needed that were only stored there". The job was no longer in the app's job list that afternoon, so which job waited
is not recorded. Three kinds of job were tied to one node then: a voice pick, a join and a take check. A render ran on
either node, but on the other node it made every line again.

Before the change, on 2026-11-12, the app's mirror held all 297 candidate clips, 269 of the 9,383 takes of The
Hundred Reigns and none of the 141 Drama 3 voice files. None of its 42 Breeze chapters could be joined or checked
without the node that made its takes.

After the deploy, one `sync-media` job per node copied the rest. The laptop sent 8,660 files, 2.6 GB, in 67 s, and the
Mac sent 614 files, 193 MB, in about 20 s. The mirror then held every take, 2.9 GB.

The cluster test used a throwaway serial of one 11-line chapter, deleted afterwards:

* A pick of a candidate the laptop made ran on containers in under a second, and the filed clip matched the mirror's copy byte for byte.
* A join and a take check of the chapter the laptop rendered ran on the Mac while the laptop was busy. The Mac fetched the 11 takes, and the join had the same length, 36.8 s.
* After a say-as change, a render on the Mac made 5 of the 11 lines and reused 6.
* The laptop then joined that chapter while the Mac was busy. It fetched the 5 takes the Mac had made and already held the other 6.

### Superseded text

From "jsc-m1 renders Breeze, and the laptop is preferred" (2026-11-07, revised 2026-11-08):

> A job that needs a node's saved files goes back to that node, and a voice pick goes to the node that made the candidate it files.

From "The app picks the node for every GPU job" (2026-11-07):

> It records which node holds each chapter's takes and each voice's candidates in `state/placement.json`.

From the known limits in `deploy/README.md`:

> Saved Breeze takes stay on the node that made them. A rerun on another node renders every line again.

---

## 2026-11-12: one Claude job at a time

### The case

The five screenplays of the batch of chapters 49 to 53 of The Hundred Reigns ran one after the other on dev-arch from
21:32:58 to 21:45:17, 2.2 to 2.7 minutes each, while the queue showed chapters 51 to 53 as "queued behind
Screenplay · ch 50". The laptop started the chapter 49 render at 21:35:41. At 21:46 the app estimated the chapter 53
render about 62 minutes out on aj-legion5, against about 67 on jsc-m1, and kept all five renders for the laptop.
dev-arch has 12 cores and 24 GB, and its load was 0.08 while one pass ran.

The cluster test after the deploy used a throwaway serial of four chapters of 498 words, deleted afterwards. Its four
screenplay jobs were queued in one request at 22:07:

* Chapters 1 to 3 started at 22:07:11 on dev-arch, and `ps` there showed three `claude` processes of about 246 MB each. Chapter 4 waited with "waiting for a running Claude job to finish, 3 run at once".
* The three ended after 50, 51 and 58 seconds. Chapter 4 started at 22:08:03, 2 seconds after the first of them ended.
* Chapters 1 and 2 wrote their `script.json` and `chapter.yaml` back while another pass ran. No pass reported a limit.
* Chapters 3 and 4 failed, and the cause was the test's chapters. Their `text.md` had no `# ` heading line, so the check of the script's words counted the spoken heading as five added words, and those two passes kept the heading in both attempts.

### Superseded text

From "A cast, a pick, a say-as or a voice effect starts beside a chapter job of its serial" (2026-11-08):

> A chapter job still waits for any of them, so "cast, then render" renders with the new voice, and a cast's briefs stage still waits for a running screenplay, because the app runs one Claude job at a time.

From "Not decided yet" in `DECISIONS.md` (2026-11-11):

> Whether to raise `LLM_SLOTS` in `serial_audio/web/nomad.py` above 1, so two screenplays run at once. The records don't give a reason for the limit. On 2026-11-11 dev-arch ran two scratch screenplay passes beside a live one, and all three finished. The two live passes that overlapped them took 148 and 146 seconds, against 133 and 158 for the two before. With `LLM_SLOTS = 2` in a scratch copy the scheduler started two screenplays at once. Within one serial's batch the character passes and the cast still ran one at a time. Outside a batch that did not hold: character passes of two serials ran together, casts of two serials ran together, and a cast's briefs stage started beside a running screenplay of its serial, which the 2026-11-08 entry on casts and `deploy/README.md` say it waits for. Only `test_a_batch_job_waits_only_for_the_jobs_it_needs` failed, because it expects one slot, and the tests don't cover a cast beside a screenplay. It would not shorten a batch that renders, because a screenplay took about 2.5 minutes in the batch of chapters 29 to 38 and a render on the laptop took 12 to 17 in the app's job list that day, so the renders set the pace. AJ was told on 2026-11-11.

From How a job runs in `deploy/README.md`:

> A render queued after one of them still waits for it, and a cast's briefs stage still waits for a running screenplay, because the app runs one Claude job at a time.
