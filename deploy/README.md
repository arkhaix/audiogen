# serial_audio on the cluster

The web app runs on containers and holds the library. It runs quick library jobs (fetch, add a serial, reset the
bible) itself and dispatches everything else to the Nomad cluster. A node takes a job when its client meta offers
the job's class:

* `breeze`: Breeze renders through breeze-server, and joining saved takes again. The laptop (aj-legion5).
* `comfyui`: Drama 3 renders, casting voices, picking a voice and checking takes, which need ComfyUI or its output folder. The laptop.
* `llm`: the character pass, the screenplay, the casting briefs and the casting bios, which run `claude` under AJ's subscription. dev-arch.

A cast runs as two dispatches: the briefs on an `llm` node, then the voices on a `comfyui` node, which finds the
briefs cached and doesn't call Claude.

---

## The app on containers

Everything lives in `/opt/serial-audio`, its own compose project beside the house's `/opt/docker-compose.yml`:

* `compose.yml` is a copy of `deploy/docker/compose.yml`.
* `src/` holds the repo's code: `serial_audio`, `prompts`, `schemas` and `deploy`.
* `library/` is the library. It is the copy of record, so nothing else should write to it.
* `state/` holds job records, job logs, `conflicts/` and `media/` (below).
* `incoming/` holds uploaded EPUBs and saved pages.
* `secrets/nomad-token` holds the dispatch token, mode 600, owned by aj.

The app listens on port 8765 on every interface, with host networking. https://audiogen.grayfam.us reaches it
through haproxy on proxy. It answers only to the names in `--allow-host` and to `containers.*`, so a new proxy name
needs a new `--allow-host` in `compose.yml`.

Deploy a code change from the repo root, on the laptop or the Mac:

```
R=$(git stash create)
git -c core.autocrlf=false archive ${R:-HEAD} serial_audio prompts schemas deploy | ssh aj@containers 'cd /opt/serial-audio/src && tar xf -'
ssh aj@containers 'cd /opt/serial-audio && cp src/deploy/docker/compose.yml . && docker compose up -d --build'
```

The archive holds the tracked files with their uncommitted edits, with LF line endings, and it doesn't make a stash
entry. It leaves out an untracked file, so `git add` a new file first. A plain `tar` of the tree sends CRLF files from
the laptop and `._*` files from the Mac (CLAUDE.md, Gotchas).

Logs: `ssh aj@containers docker logs --tail 50 serial-audio`. A worker always runs the code the app runs, because it
fetches the code with its job, so a deploy needs nothing on the nodes.

---

## Nomad

* Namespace `serial-audio`.
* Jobs `serial-audio-breeze`, `serial-audio-comfyui` and `serial-audio-llm`: one parameterized batch job per class, from `deploy/nomad/serial-audio.nomad.hcl` with its task `deploy/nomad/bootstrap.py`.
* Pinned jobs `serial-audio-<class>-<node>`, one for each GPU class a GPU node offers: today `serial-audio-breeze-aj-legion5`, `serial-audio-comfyui-aj-legion5` and `serial-audio-breeze-jsc-m1`. The app picks the node for every GPU job and dispatches the pinned job.
* Policy `serial-audio-dispatch` (`deploy/nomad/serial-audio-dispatch.policy.hcl`): list-jobs, read-job, dispatch-job and read-logs in the namespace, and node read. It can't register, change or stop a job.
* Token `serial-audio dispatch (containers)`, accessor 682ddbca-a2ae-377b-7141-d2c07f58886c. The copy of record is the vault item `homelab-nomad-serial-audio-dispatch-token`. The working copy is `/opt/serial-audio/secrets/nomad-token`.
* Variable `nomad/jobs/serial-audio-llm`, item `claude_oauth_token`: the working copy of AJ's Claude subscription token, which the Claude passes sign in with. The copy of record is the vault item `homelab-claude-subscription-token`. A request without a token and the dispatch token both get HTTP 403 for it (checked 2026-11-11).

Register or update the jobs from the repo root with the management token, because the template reads
`deploy/nomad/bootstrap.py` by relative path:

```
nomad job run -var class=breeze  deploy/nomad/serial-audio.nomad.hcl
nomad job run -var class=comfyui deploy/nomad/serial-audio.nomad.hcl
nomad job run -var class=llm     deploy/nomad/serial-audio.nomad.hcl
nomad job run -var class=breeze  -var node=aj-legion5 deploy/nomad/serial-audio.nomad.hcl
nomad job run -var class=comfyui -var node=aj-legion5 deploy/nomad/serial-audio.nomad.hcl
nomad job run -var class=breeze  -var node=jsc-m1     deploy/nomad/serial-audio.nomad.hcl
```

Register them again after a change to the spec or to `bootstrap.py`, because Nomad keeps the copy it got at
registration. A change anywhere else ships with the app's code.

Rotate the token: `nomad acl token delete <accessor>`, then
`nomad acl token create -name="serial-audio dispatch (containers)" -type=client -policy=serial-audio-dispatch`,
write its secret to the vault item and to `secrets/nomad-token`, and run `docker compose restart`.

The `llm` job's template reads the Claude token from the variable into `CLAUDE_CODE_OAUTH_TOKEN` when a task
starts, and the job sets `SERIAL_AUDIO_CLAUDE_AUTH=token`, so the worker refuses a job that did not get one. A job
log says `Claude signs in with the token in CLAUDE_CODE_OAUTH_TOKEN`. On the node the token is in the task's
environment and in `secrets/claude.env` of its allocation, a file on tmpfs that only root can read and that stays
until Nomad collects the finished allocation. A job reads only the variable at its own id, so a test instance with
its own prefix needs its own variable, `nomad/jobs/<prefix>-llm`, before its Claude jobs can run. The token outranks the claude login in
`/home/aj/.claude` on dev-arch, which the passes no longer use. It came from `claude setup-token` on 2026-11-11 and
is valid for a year. Replace it: mint a new one in a terminal outside Claude Code, store it as the vault item's
password, then write the variable from the vault with the management token, without printing either:

```
bw get password homelab-claude-subscription-token --session "$BW_SESSION" \
  | jq -Rs '{Namespace: "serial-audio", Path: "nomad/jobs/serial-audio-llm", Items: {claude_oauth_token: gsub("[[:space:]]"; "")}}' \
  | nomad var put -in json -force - >/dev/null
```

The next job uses the new token. A pass that is running keeps the one it started with.

---

## Nodes

A node offers classes through client meta:

* `serial_audio` lists the classes, such as `"breeze,comfyui"`.
* `serial_audio_python` names the Python that runs the task. It needs pyyaml, plus anthropic for the API fallback of the Claude passes.
* `serial_audio_config` optionally names a JSON file, `{"env": {...}, "comfy_url": "..."}`. The worker adds `env` to the CLI's environment, asks `comfy_url` whether ComfyUI is busy, and cancels its own prompts there when it stops.
* `busy` must be `"false"`, because every job constrains on it. A node sets `busy = "true"` to stop taking jobs.

A GPU node also needs its pinned jobs registered (above).

The laptop sets them in `C:\nomad\config\client.hcl` (copy in the ansible repo, `nomad/aj-legion5/client.hcl`), then
`Restart-Service Nomad`. Its tasks run as SYSTEM, which works for renders and casting. The laptop's defaults are the
pipeline's built-in `D:\comfy` paths, so it has no settings file. The Claude passes don't run there, because the
`llm` job runs aj's install of `claude` on a Linux node. dev-arch has dynamic meta, set with
`nomad node meta apply -node-id <id> serial_audio=llm serial_audio_python=/usr/bin/python3`. Remove it with
`-unset serial_audio,serial_audio_python`. `llm` jobs run as aj there, because they use aj's install
of `claude`. They sign in with the token in the Nomad variable (above), not with the login in `/home/aj/.claude`.

A node with the class `breeze` needs breeze-server and its Q8_0 weights, and the Python with numpy and soundfile that
joins the takes. A `comfyui` node needs ComfyUI on 127.0.0.1:8188 with the ComfyUI-Drama3 and ComfyUI-Qwen-TTS node
packs.

---

## How a job runs

1. The app dispatches the job with `-meta job=<app job id> -meta key=<random per-job key>`. A Claude job goes to `serial-audio-llm`. For a GPU job the app picks the node and dispatches its pinned job (below).
2. The task's `bootstrap.py` fetches the job's bundle from the app: the code, the prompts and schemas, and the serial's folder without its audio, about 1.5 MB for Twenty Thousand Leagues.
3. A GPU job takes the machine's GPU lock first (`C:\ProgramData\serial-audio\gpu.lock`, or `/tmp/serial-audio-gpu.lock`), so two GPU jobs on one machine run one after the other, whoever dispatched them. It also waits while ComfyUI is busy with another prompt and, on Windows, while a render or pipeline script started by hand runs. Then the worker runs the CLI steps in its copy of the serial and sends the log to the app every 3 seconds.
4. At the end the worker sends back every file the steps created or changed. The app writes a file only when the library copy still has the hash it had when the bundle was made. A file that changed meanwhile goes to `state/conflicts/<job id>/` instead, and the job's note says so. A failed or stopped job writes back only the files a rerun can use, such as `breeze_takes.json` and the casting briefs. A failed Claude pass writes back the chapters it finished, because the passes save each chapter as it completes.
5. Stop in the app asks the worker to stop. It kills the CLI within a few seconds, cancels the job's ComfyUI prompts and reports. A job no node has started yet is cancelled at once, and its dispatch exits as soon as a node starts it. A dispatch that fails or times out is tried again with the same idempotency token, so Nomad never runs it twice.

Jobs on different chapters, serials or nodes run at the same time. Jobs on the same chapter wait for each other, and
so does any job beside one that changes a whole serial (the character pass, a cast, casting bios, picking a voice,
fetching from the site), with one exception: a cast, casting bios, a voice pick, a say-as or a voice effect starts beside a running
render, screenplay or take check of its serial once that job has fetched its bundle, because it writes no chapter file
and the bundle holds the chapter job's copy of the bible, voices, pronunciations and effects. It also passes a chapter job that waits ahead of it in
its lane. A render queued after one of them still waits for it, and a cast's briefs stage still waits for a running
screenplay, because the app runs one Claude job at a time. Casting bios and a fetch or an import don't wait for each
other, because a fetch writes chapter files, the index and `serial.yaml` and the bios write only the bible. The import
of a saved page counts as a job on its chapter, because it writes only that chapter's files. It runs beside the renders,
screenplays and take checks of other chapters, and beside a cast, casting bios, a voice pick, a say-as or a voice
effect, once each running job of its serial has fetched its bundle, and a job that gets a bundle starts after a running
import. Imports of one serial run one after the other. A character pass
starts beside the renders, screenplays and take checks of other chapters on the same terms, because it writes the bible
and its own chapter's files. It waits for a job on its own chapter, and every chapter job still waits for a character
pass, because it reads the bible. A laptop that sleeps through part of a render has 2 hours to come back before Nomad
gives the allocation up.

A job of a Process batch waits by the same rules and for nothing else, so a chapter renders as soon as its screenplay
is written, beside the other chapters' screenplays. When a batch job fails or is lost, the queued jobs of the batch
that wait for it are skipped and the rest run. Stopping a batch job skips every queued job of the batch as soon as
the stop is asked for. A job of the batch that already runs on another node keeps running until it is stopped too.

The app picks the node for a GPU job, one GPU job per node:

* A join, a take check and picking a voice need files that live on one node, so they run there and wait for it. A take check or a pick on a node without ComfyUI runs as a `breeze` job.
* Any other GPU job goes to the ready node where its estimated finish is earliest. The estimate is the node's running job's remaining time, from its progress log, plus the queued jobs already waiting for that node, plus the job itself: its takes times the node's seconds per take, the mean of the last three Breeze renders that finished on that node and made twenty takes or more and at least half of their chapter's lines, kept in `placement.json` (3 s for NVIDIA and 18 s for Metal until then), counting only the takes the holder lacks, plus the join and the check. A render whose reuse cannot be worked out is costed at all its takes. The job waits for a busy node when that beats every free node by two minutes or more, and its wait reason shows both estimates. A node whose running job's log has not changed for ten minutes counts as busy for a day, so nothing waits for it. A GPU job does not wait behind a blocked job in its lane.
* The app records the node of each chapter's takes and each voice's candidates in `state/placement.json` as results come back. For a chapter or voice from before that record, it reads the node from the path style of its files, which works while one node runs each OS.

The static port 19137 that GPU jobs reserve doesn't keep them apart, because Nomad checks it per address and the
nodes have an IPv4 and an IPv6 tailnet address.

Cast candidates and the takes a check didn't pass live in the node's ComfyUI output folder. The worker sends them
with the result, and the app keeps them in `state/media/`, so the Cast tab and the take check can play them.

---

## Known limits

* A job's key is in its dispatch meta, which every tailnet device can read through Nomad's anonymous policy. It ties a worker's uploads to its own job. It is not a secret, and the app has no login either, so the tailnet stays the boundary.
* The worker doesn't send back deleted files. Only fetch and reset-bible delete library files, and they run on the app's machine.
* Saved Breeze takes stay on the node that made them. A rerun on another node renders every line again.
* The Claude passes run only on a Linux node with aj's install of `claude`, which today is dev-arch.

---

## Tests

```
python -m unittest tests.test_cluster
python tests/e2e_fake_nomad.py
```

The second test copies the code into a temporary folder, writes a one-chapter serial there, and runs the app against
a fake Nomad that starts `bootstrap.py` as a local process for each dispatch. Neither test needs a library.
