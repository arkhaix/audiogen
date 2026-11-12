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
