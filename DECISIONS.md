# Decision record: audiogen

Decisions for this repo, current state only. Each entry has the date, the context and the decision. Check the
existing entries for a conflict before adding one, and rewrite an entry when AJ changes it.

---

### Two LLM backends: the Claude subscription first, the API as the fallback
**Date:** 2026-10-16
**Context:** AJ asked for both at the start.
**Decision:** The character pass, the casting pass and the screenplay run through headless Claude Code under AJ's login by default (`--backend auto`). On a usage-limit message they fall back to the Anthropic API, wait for the reset, or fail, per `--on-limit`. Every chapter records which backend ran.

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
**Decision:** Both engines write `audio/chNNNN.opus`. Breeze audio stays at 24 kHz, and Drama 3 audio is resampled to 48 kHz, because Opus takes only a few sample rates.

---

### Every Breeze render checks its takes
**Date:** 2026-11-07
**Context:** Breeze sometimes loops, repeating a word, or drops the end of a line. The take-length guard catches only the gross cases. AJ asked for the transcript check to be part of the process.
**Decision:** After the join, `render` transcribes each new take and aligns it with the script line. A take with three or more inserted or dropped words is flagged. The result goes to `take_check.json` and `render.check`, and the chapter page lists the takes to listen to. When no recognizer is available, the check is recorded as skipped. It doesn't fail the render.

---

### Say-as overrides fix how names are read
**Date:** 2026-11-07
**Context:** AJ: Louis is always said "Lewis", and Lauriane is always said "Laurie Anne".
**Decision:** A pronunciation entry may have `say`, the spelling the voice reads. It replaces the term as a whole word in the text sent to the voice only, and the chapter text and the script keep the original spelling. The character pass never overwrites an existing entry. AJ edits overrides on the bible page, which runs the `say-as` command as a job.

---

### The library of record lives on containers
**Date:** 2026-11-07
**Context:** AJ wanted one entry point usable from any tailnet machine, with jobs running on whichever node is set up for them. He approved moving the library on 2026-11-07 at 22:08.
**Decision:** The library is `/opt/serial-audio/library` on containers, behind the web app with the Nomad executor. `D:\fish\library` is retired. `library/` and `incoming/` stay out of git, because they hold chapter text and saved Royal Road pages.

---

### Workers get their code and files from the app over HTTP
**Date:** 2026-11-07
**Context:** A cluster job needs the pipeline code and one serial's folder on its node, and the changed files back in the library. The meetings pipeline on the same cluster moves its files through a Garage bucket, and its workers run a git checkout.
**Decision:** The app serves each job a bundle: the code it runs, the prompts and schemas, and the serial's folder without its audio. The worker sends back every file it created or changed, and the app writes a file only where the library copy still has the hash it had when the bundle was made. Workers need no bucket key and no checkout, and they always run the app's code. Rejected: Garage as the exchange, because the app is always on and can serve the files itself. Rejected: a git checkout on each node, because a node would drift from the app, and every change would need a pull on every node before a job could use it.

---

### The app picks the node for every GPU job
**Date:** 2026-11-07
**Context:** The first design reserved static port 19137 in every GPU job, so Nomad would place one GPU job per node. Two renders queued together both landed on the laptop, because each node has an IPv4 and an IPv6 tailnet address and Nomad checks a static port per address. Joins, take checks and voice picks also need the node that holds their files.
**Decision:** The app chooses the node and dispatches a copy of the job pinned to it, `serial-audio-<class>-<node>`, registered once per GPU class per GPU node. It records which node holds each chapter's takes and each voice's candidates in `state/placement.json`. Each worker also takes a machine-wide GPU lock file. Rejected: a Nomad `host_network` limited to the IPv4 tailnet range, because it needs a client config change on every GPU node and still can't send a join to the right node.

---

### The Claude passes run on dev-arch
**Date:** 2026-11-07
**Context:** The laptop's Nomad client runs as LocalSystem, so its tasks can't use AJ's claude login. dev-arch already runs `claude -p` as aj for the meetings jobs and is always on.
**Decision:** The `llm` class runs only on Linux nodes, as aj, and dev-arch offers it through dynamic node meta. A cast runs its briefs there first and then designs the voices on a GPU node, which finds the briefs cached. Rejected: running the laptop's Nomad service as AJ's user, because that changes a host service account for one class of job.

---

### jsc-m1 renders Breeze, and the laptop is preferred
**Date:** 2026-11-07
**Context:** AJ asked for jsc-m1 to be usable as a worker. Its patched Metal breeze-server runs at about 0.4x realtime, against the laptop's 2x. The CPU backend and Vulkan through MoltenVK were slower, at 0.26x and 0.28x.
**Decision:** jsc-m1 offers the `breeze` class. The app sends a GPU job to a free NVIDIA node first and to any free node second, one GPU job per node. A job that needs a node's saved files goes back to that node.

---

### Parakeet checks takes and casting candidates where ComfyUI is missing
**Date:** 2026-11-07
**Context:** jsc-m1 has no ComfyUI for Qwen3-ASR. AJ preferred Parakeet to whisper, and his meetings project had already chosen Parakeet TDT 0.6B v2 int8 through sherpa-onnx.
**Decision:** The take check and the casting check use Parakeet when its model folder exists and Qwen3-ASR otherwise. `SERIAL_AUDIO_ASR` overrides the choice.

---

### Cast voices older and lower than the story says
**Date:** 2026-11-05
**Context:** AJ found the first Qwen VoiceDesign cast too young across the board, including characters of 20.
**Decision:** Briefs name an age older than the character's story age and a low register. `docs/casting.md` has the details.

---

### Web chapters come from pages AJ saves
**Date:** 2026-11-05
**Context:** Royal Road started answering the pipeline with HTTP 403, and probing it counts as getting around a bot check.
**Decision:** The pipeline doesn't contact Royal Road for chapters. AJ saves a chapter page in his browser, and `fetch <slug> <n> --html <file>` or an upload in the app ingests it.

---

## Not decided yet

As of 2026-11-08:

* Whether Breeze voice design becomes the cast engine for The Hundred Reigns. Today `cast_engine` defaults to `qwen`.
* Whether to re-render chapters 1 to 6 of The Hundred Reigns with Breeze, so Louis and Lauriane sound the same as in the later chapters. That takes about 85 minutes on the laptop. AJ was asked on 2026-11-07 and hasn't answered.
* How the library is backed up. `/opt/serial-audio/library` on containers' local disk has no backup, and silver has no backup job.
* Chapters 11 and 12 of The Hundred Reigns are scripted but not rendered, and chapter 12 needs two voices cast first. AJ cancelled both on 2026-11-07 for lack of time. `docs/casting.md` has the planned briefs.
