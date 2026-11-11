# Decision record: audiogen

Decisions for this repo, current state only. Each entry has the date, the context and the decision. Check the
existing entries for a conflict before adding one, and rewrite an entry when AJ changes it.

---

### Two LLM backends: the Claude subscription first, the API as the fallback
**Date:** 2026-10-16
**Context:** AJ asked for both at the start.
**Decision:** The character pass, the casting pass and the screenplay run through headless Claude Code under AJ's login by default (`--backend auto`). On a usage-limit message they fall back to the Anthropic API, wait for the reset, or fail, per `--on-limit`. Every chapter records which backend ran. On the cluster the API fallback can't run, because `/usr/bin/python3` on dev-arch doesn't have the `anthropic` package (checked 2026-11-09), so a pass that hits the limit with `--on-limit api`, the app's default, fails. AJ said on 2026-11-09 to leave that until it causes a problem.

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

### Breeze joins fill clicks and fade every take
**Date:** 2026-11-08
**Context:** AJ heard occasional loud clicks and pops in the Breeze chapters 7 to 10. They come from Breeze, not from the join. Some takes hold bursts under 1 ms long, about 20 to 50 a chapter, and a few takes start at full level on their first sample. They don't sit on Breeze's 80 ms frame boundaries. AJ heard the ten most prominent repairs and said "repaired samples sound fine".
**Decision:** The join fills each click from an autoregressive model of the 20 ms on each side of it and fades every take in and out over 3 ms. A burst counts as a click only while it is that short, so consonants are left alone. A review found that the detector also takes some sharp word-final stop releases, which the fill softens. That stays until AJ hears a dull consonant.

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

### The Claude passes sign in with a token from a Nomad variable
**Date:** 2026-11-11
**Context:** The passes used the login AJ did by hand on dev-arch, which is the CLI's own session file and was not in the vault. AJ minted a one-year token with `claude setup-token`, filed it as the vault item `homelab-claude-subscription-token`, and said to use it. A Nomad job can't unlock the vault, so the cluster needs a working copy.
**Decision:** The working copy is the Nomad variable `nomad/jobs/serial-audio-llm`, item `claude_oauth_token`. AJ chose it over a file on dev-arch. A template in the `llm` job reads it into `CLAUDE_CODE_OAUTH_TOKEN` when a task starts, through the job's own workload identity, so the app never holds it. On dev-arch the token is in the task's environment and in `secrets/claude.env` of its allocation, a file on tmpfs that only root can read and that stays until Nomad collects the finished allocation. The job also sets `SERIAL_AUDIO_CLAUDE_AUTH=token`, and the worker refuses a job that asks for a token and has none, so a pass can't fall back to the dev-arch login without saying so. A request without a token and the app's dispatch token both get HTTP 403 for the variable. On dev-arch a made-up token in the environment got a 401 with the login present, so the token outranks the login. The first cluster run used a dummy value and failed with a 401, and the run with the real token wrote a screenplay for a throwaway serial. The `llm` class still runs only on dev-arch, as aj, because it uses aj's install of `claude`. The token expires about 2027-11-11, and `deploy/README.md` has the steps to replace it.

---

### jsc-m1 renders Breeze, and the laptop is preferred
**Date:** 2026-11-07, revised 2026-11-08
**Context:** AJ asked for jsc-m1 to be usable as a worker. Its patched Metal breeze-server runs at about 0.4x realtime, against the laptop's 2x. The CPU backend and Vulkan through MoltenVK were slower, at 0.26x and 0.28x. In the render records of 2026-11-08 the Mac made a take in 17.5 s and the laptop in 2.8 s, about 6x faster.
**Decision:** jsc-m1 offers the `breeze` class, one GPU job per node. A job that needs a node's saved files goes back to that node, and a voice pick goes to the node that made the candidate it files. Any other GPU job goes to the ready node where its estimated finish is earliest: the node's running job's remaining time from its progress log, plus the queued jobs already waiting for that node, plus the job's own estimate, which is its takes times the node's seconds per take (the mean of the last three Breeze renders that finished on the node and made twenty takes or more and at least half of their chapter's lines, kept in `placement.json`, else 3 s for NVIDIA and 18 s for Metal), counting only the takes the holder lacks, plus the join and the check. A render whose reuse cannot be worked out is costed at all its takes, because sending it to the slow node by mistake costs an hour and the other mistake costs ten minutes. It waits for a busy node when that beats every free one by two minutes or more, and a GPU job no longer waits behind a blocked job in its lane. With the join and the check the same on both nodes and the margin, the Mac wins a fresh 255-line render only when four or five are ahead of it on the laptop. AJ set the conditions: estimate the completion times, wait only when it saves time, and account for the other queued jobs. Rejected on 2026-11-08: sending a render back to the node that holds its takes whatever it would reuse there, and then the 90% reuse threshold that replaced it for a few hours, because both ignore how long the fast node stays busy (JOURNAL.md, 2026-11-08).

---

### Parakeet checks takes and casting candidates
**Date:** 2026-11-07, extended to the laptop 2026-11-08
**Context:** jsc-m1 has no ComfyUI for Qwen3-ASR, and on the laptop a check failed whenever ComfyUI was down. AJ preferred Parakeet to whisper, and his meetings project had already chosen Parakeet TDT 0.6B v2 int8 through sherpa-onnx.
**Decision:** The take check and the casting check use Parakeet when its model folder exists and Qwen3-ASR otherwise. `SERIAL_AUDIO_ASR` overrides the choice. Both `breeze` nodes have Parakeet, so no check needs ComfyUI.

---

### Breeze voice design is the default cast engine
**Date:** 2026-11-08
**Context:** AJ heard a Breeze cast of the Keeper and Lauriane beside their Qwen voices and picked a Breeze candidate for both. He said "i think we should make breeze voices the default for new casting going forward". A Breeze cast needs no ComfyUI, and on jsc-m1 breeze-server designs the same clip again from the same brief and seed.
**Decision:** `cast` uses Breeze voice design unless `serial.yaml` sets `cast_engine: qwen` or the cast passes `--engine qwen`. On the cluster a Breeze cast takes the NVIDIA node first, like any GPU job. Voices cast before this keep their Qwen clips and Drama 3 voice files. Rejected on 2026-11-08: sending a Breeze cast to a node without ComfyUI first. It only kept the check off Qwen3-ASR, and the laptop checks with Parakeet now (JOURNAL.md, 2026-11-08).

---

### Breeze briefs use the story age, and Qwen briefs an older one
**Date:** 2026-11-05, revised 2026-11-09
**Context:** AJ found the first Qwen VoiceDesign cast too young across the board, including characters of 20, so briefs named an older age and a low register. Breeze became the default cast engine on 2026-11-08. On 2026-11-09 the aged-up Qwen voices of Casval and Anna sounded too old to AJ for the academy arc, and he picked Breeze candidates from "about twenty" briefs for both.
**Decision:** A brief for a Breeze cast names the character's story age. AJ said "use the story age from now on". A brief for a Qwen cast still names an older age and a low register. `docs/casting.md` has the details.

---

### The Breeze join levels each voice
**Date:** 2026-11-09
**Context:** AJ found a new Lorimor candidate quiet beside the other characters. A Breeze take comes out at the level of its voice's cast clip, the cast clips ran from -11 to -29 dB of active speech RMS, and the join leveled only the chapter as a whole. In chapter 17 the old Lorimor takes had a median of -12.6 dB against -26.1 dB for the narrator.
**Decision:** `breeze_join.py` gives each voice one gain per chapter, which brings the median level of its takes to the chapter target of -18 dB. One gain per voice keeps the distance between a voice's loud and soft lines. The level comes from the chapter's takes and not from the cast clip, because an older chapter's takes can come from a clip that has since been recast. A voice with one or two lines in a chapter is leveled from those lines alone, so a single whispered line comes out at full level. The pauses to trim are found before the gains are applied, because a gain moves soft speech and room noise across the silence threshold.

---

### Web chapters come from pages AJ saves
**Date:** 2026-11-05
**Context:** Royal Road started answering the pipeline with HTTP 403, and probing it counts as getting around a bot check.
**Decision:** The pipeline doesn't contact Royal Road for chapters. AJ saves a chapter page in his browser, and `fetch <slug> <n> --html <file>` or an upload in the app ingests it.

---

### A cast, a pick, a say-as or a voice effect starts beside a chapter job of its serial
**Date:** 2026-11-08
**Context:** A cast of eole and lorimor-ancitif sat queued behind the chapter 12 render on the Mac for over an hour while the laptop was idle, because every serial-wide job waited for any running job on its serial. AJ asked why a cast should ever wait for the Mac. A cast, a pick, a say-as or a voice effect writes `voices/`, the bible or `serial.yaml`, a render, screenplay or take check writes its chapter's files, and a cluster worker takes the voices, pronunciations and effects in its bundle when it starts, so neither changes a file the other sends back.
**Decision:** On the cluster a cast, a voice pick, a say-as or a voice effect does not wait for a render, a screenplay or a take check of its serial, whether running, queued ahead of it, or waiting ahead of it in its lane. It waits only until a running chapter job has fetched its bundle, because the app builds the bundle outside its lock and a result landing during the build could tear it. A chapter job still waits for any of them, so "cast, then render" renders with the new voice, and a cast's briefs stage still waits for a running screenplay, because the app runs one Claude job at a time. The one-machine executor is unchanged. The rule first covered casts and picks, and AJ extended it to say-as and voice-effect the same day. A `bios` job, which writes only the bible, follows it since 2026-11-11.

---

### A batch job waits only for the jobs it needs
**Date:** 2026-11-09
**Context:** On the cluster every job of a Process batch waited for every earlier job of the batch. The rule came from the one-machine app, where heavy jobs ran one at a time anyway. A render waited for the screenplays of the other chapters, so the batch of chapters 13 to 17 on 2026-11-08 kept the laptop idle for 12.5 minutes with chapter 12 ready to render, and the batch of chapters 18 to 23 on 2026-11-09 kept it idle for 12.7 minutes. Renders of one batch never ran on two nodes at once, and one failed job skipped every later job, including the renders of chapters that were already scripted. AJ asked why only dev-arch was working and said to drop the wait.
**Decision:** On the cluster a batch job waits by the same rules as any other job: for the jobs of its chapter, and for a job that changes the whole serial. A chapter renders as soon as its screenplay is written, beside the other chapters' screenplays, and a batch's renders can run on both GPU nodes. When a batch job fails or is lost, only the queued jobs of the batch that wait for it are skipped. Stopping a batch job still skips every queued job of the batch, now as soon as the stop is asked for, because a queued job could otherwise start before the worker confirms the stop. A job of the batch that already runs on another node keeps running until it is stopped too. The one-machine executor is unchanged. In a scratch run of the scheduler on the 18 to 23 batch, the first chapter was ready after 23 minutes, against 36, and all six after 95, against 108.

---

### A voice card shows a casting bio without the plot
**Date:** 2026-11-11
**Context:** The bio on a voice card was the bible's role and description, which the character pass keeps current for attribution, so it had the plot in it. Leonard's read "killed again in the seventh reign by the Berwick Lich's aura beneath Castle Carcas". AJ listens on the Cast tab to judge whether a voice matches the character. He said "i don't need too many story details. i need to know age, race, temperament, characteristics", that "telling me that someone is hiding something is fine if that's a key part of their character that would show up in the way they speak", and that "telling me that someone will betray the main character later is too much".
**Decision:** A bio pass with its own prompt writes each character's casting bio into the bible under `bio`, in 50 words at most: sex and age, race or species, station, temperament and habits of speech. It reads the whole record with every fact, and it leaves out events, story mechanics and the voice itself. It says that a character hides something and doesn't say what. It describes a character who passes as something else the way they pass, so Casval reads as a young man and not as a dragon. That last rule is Claude's reading of AJ's words, and AJ hasn't confirmed it. The bio pass runs beside the casting pass, for the characters that pass briefs, so the voices stage of a cast, which finds its briefs cached, doesn't call Claude for a bio either. A bio pass that fails doesn't stop the cast. A `bios` job writes the bios of the voices that don't have one, or one again with `--redo`. On the cluster it is a Claude job that follows a cast's rule beside a chapter job (2026-11-08 entry). It also doesn't wait for a fetch or an import of its serial, nor they for it, because they don't write a file in common. The first `bios` job for The Hundred Reigns sat behind five queued imports that waited for the chapter 38 render, and AJ asked why a bios job would wait for a render. The role and description stay as they were, because the character pass reads the role and the casting pass reads both. A card without a bio still shows them.

---

### The import of a saved page waits only for jobs on its chapter
**Date:** 2026-11-11
**Context:** Five imports of chapters 39 to 43 of The Hundred Reigns sat queued for the length of the chapter 38 render on jsc-m1, because a fetch of any sort counted as a job that changes the whole serial. An import with `--html` writes only its chapter's `text.md`, `chapter.yaml` and `author_notes.md`, and it doesn't refresh the index or `serial.yaml`. After asking why a bios job would wait for a render, AJ asked "and the import jobs?". Claude read that as: the imports should not wait for it either.
**Decision:** On the cluster an import counts as a job on its chapter. It doesn't wait for a render, a screenplay, a take check or a character pass of another chapter, and they don't wait for it. It and a cast, casting bios, a voice pick, a say-as or a voice effect don't wait for each other either, because those write only `voices/`, the bible and `serial.yaml`. An import passes a job that waits ahead of it in its lane. It still waits for a job on its own chapter, for a fetch from the site and for a bible reset, and they wait for it. The app builds a bundle outside its lock, so an import starts only once each running job of its serial has fetched its bundle, a job that gets a bundle starts after a running import, and the imports of one serial run one after the other. A fetch from the site still changes the whole serial, because it writes the index and `serial.yaml`. The one-machine executor is unchanged. AJ asked on 2026-11-11 whether imports "will not wait for renders" from now on, and the cast part was added then so that the answer is yes for a batch with a queued cast as well.

---

### A character pass waits for a chapter job only on its own chapter
**Date:** 2026-11-11
**Context:** AJ queued the Process batch for chapters 39 to 43 of The Hundred Reigns while the chapter 38 render of the batch before it still had half an hour to run on jsc-m1. The batch's first job, the character pass for chapter 39, waited for that render, and the other sixteen jobs waited for the character pass. AJ asked "why is everything still waiting behind the render?". A character pass writes the bible and its own chapter's files, and a render, a screenplay or a take check writes its own chapter's files, so two of them on different chapters don't write a file in common. This was an open question since 2026-11-09, and Claude took AJ's question as the answer.
**Decision:** On the cluster a character pass doesn't wait for a render, a screenplay or a take check of another chapter, whether running or queued ahead of it, nor for an import of another chapter. It waits until a running job of its serial has fetched its bundle, as a cast does. It still waits for a job on its own chapter, for another character pass and for a cast, so the bible grows in chapter order. A chapter job still waits for a character pass of any chapter, because it reads the bible when it builds its bundle. A GPU node can therefore sit idle for the rest of one character pass before a queued render starts. The 26 character passes in the app's job list on 2026-11-11 took 45 to 99 seconds. The one-machine executor is unchanged.

---

## Not decided yet

As of 2026-11-09:


* Whether to re-render the chapters of The Hundred Reigns that use old voices. Chapters 1 to 6 are Drama 3 renders, so Louis and Lauriane sound different there, and they take about 85 minutes on the laptop. AJ was asked about them on 2026-11-07 and hasn't answered. Chapter 12 has Lauriane's old Qwen voice, because it was rendered before the recast on 2026-11-08.
* Whether to recast the other academy-age voices that were cast with aged-up Qwen briefs: simon, leonard-decarabia, meredith-dose, cassandra, antonine-de-shax and tiella-decarabia. Casval and Anna were recast with Breeze at their story age on 2026-11-09 (`docs/casting.md`), and AJ was asked about the rest that day.
* Whether to render chapters 7, 8 and 10 of The Hundred Reigns again. They have the Qwen voices of Casval and Anna that AJ replaced on 2026-11-09, and they were joined before the join leveled each voice. AJ had finished them, so they were left alone.
* How the library is backed up. `/opt/serial-audio/library` on containers' local disk has no backup, and silver has no backup job.
* lorimor-ancitif and eole were cast on 2026-11-08 (Breeze seeds 17000 to 17002, 17000 filed with status auto for both) from the cached briefs, not the older, lower descriptions planned in `docs/casting.md`, and wait for AJ's pick or a recast. The narrator reads their lines in chapter 12, rendered before the cast, so it needs a render afterwards, which remakes only their 3 lines.
* Whether to raise `LLM_SLOTS` in `serial_audio/web/nomad.py` above 1, so two screenplays run at once. The records don't give a reason for the limit. On 2026-11-11 dev-arch ran two scratch screenplay passes beside a live one, and all three finished. The two live passes that overlapped them took 148 and 146 seconds, against 133 and 158 for the two before. With `LLM_SLOTS = 2` in a scratch copy the scheduler started two screenplays at once. Within one serial's batch the character passes and the cast still ran one at a time. Outside a batch that did not hold: character passes of two serials ran together, casts of two serials ran together, and a cast's briefs stage started beside a running screenplay of its serial, which the 2026-11-08 entry on casts and `deploy/README.md` say it waits for. Only `test_a_batch_job_waits_only_for_the_jobs_it_needs` failed, because it expects one slot, and the tests don't cover a cast beside a screenplay. It would not shorten a batch that renders, because a screenplay took about 2.5 minutes in the batch of chapters 29 to 38 and a render on the laptop took 12 to 17 in the app's job list that day, so the renders set the pace. AJ was told on 2026-11-11.
