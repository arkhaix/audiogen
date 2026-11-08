# serial_audio on the cluster

The web app runs on containers and holds the library. It runs quick library jobs (fetch, add a serial, reset the
bible) itself and dispatches everything else to the Nomad cluster. A node takes a job when its client meta offers
the job's class:

* `breeze`: Breeze renders through breeze-server, and joining saved takes again. The laptop (aj-legion5).
* `comfyui`: Drama 3 renders, casting voices, picking a voice and checking takes, which need ComfyUI or its output folder. The laptop.
* `llm`: the character pass, the screenplay and the casting briefs, which need AJ's claude login. dev-arch.

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

Deploy a code change from the repo root on the laptop:

```
tar cf - --exclude=__pycache__ serial_audio prompts schemas deploy | ssh aj@containers 'cd /opt/serial-audio/src && tar xf -'
ssh aj@containers 'cd /opt/serial-audio && cp src/deploy/docker/compose.yml . && docker compose up -d --build'
```

Logs: `ssh aj@containers docker logs --tail 50 serial-audio`. A worker always runs the code the app runs, because it
fetches the code with its job, so a deploy needs nothing on the nodes.

---

## Nomad

* Namespace `serial-audio`.
* Jobs `serial-audio-breeze`, `serial-audio-comfyui` and `serial-audio-llm`: one parameterized batch job per class, from `deploy/nomad/serial-audio.nomad.hcl` with its task `deploy/nomad/bootstrap.py`.
* Pinned jobs `serial-audio-<class>-<node>`, one for each GPU class a GPU node offers: today `serial-audio-breeze-aj-legion5`, `serial-audio-comfyui-aj-legion5` and `serial-audio-breeze-jsc-m1`. The app picks the node for every GPU job and dispatches the pinned job.
* Policy `serial-audio-dispatch` (`deploy/nomad/serial-audio-dispatch.policy.hcl`): list-jobs, read-job, dispatch-job and read-logs in the namespace, and node read. It can't register, change or stop a job.
* Token `serial-audio dispatch (containers)`, accessor 682ddbca-a2ae-377b-7141-d2c07f58886c. The copy of record is the vault item `homelab-nomad-serial-audio-dispatch-token`. The working copy is `/opt/serial-audio/secrets/nomad-token`.

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

---

## Nodes

A node offers classes through client meta:

* `serial_audio` lists the classes, such as `"breeze,comfyui"`.
* `serial_audio_python` names the Python that runs the task. It needs pyyaml, plus anthropic for the API fallback of the Claude passes.
* `serial_audio_config` optionally names a JSON file, `{"env": {...}, "comfy_url": "..."}`. The worker adds `env` to the CLI's environment, asks `comfy_url` whether ComfyUI is busy, and cancels its own prompts there when it stops.
* `busy` must be `"false"`, because every job constrains on it. A node sets `busy = "true"` to stop taking jobs.

A GPU node also needs its pinned jobs registered (above).

The laptop sets them in `C:\nomad\config\client.hcl` (copy in the ansible repo, `nomad/aj-legion5/client.hcl`), then
`Restart-Service Nomad`. Its tasks run as SYSTEM. That works for renders and casting, which need no login, and
rules out the Claude passes there. Its defaults are the pipeline's built-in `D:\comfy` paths, so it has no settings
file. dev-arch has dynamic meta, set with
`nomad node meta apply -node-id <id> serial_audio=llm serial_audio_python=/usr/bin/python3`. Remove it with
`-unset serial_audio,serial_audio_python`. `llm` jobs run as aj there, so they use the claude login in
`/home/aj/.claude`.

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
so does any job beside one that changes a whole serial (the character pass, a cast, picking a voice, fetching). The
app runs one Claude job at a time. A laptop that sleeps through part of a render has 2 hours to come back before Nomad
gives the allocation up.

The app picks the node for a GPU job, one GPU job per node:

* A join, a take check and picking a voice need files that live on one node, so they run there and wait for it. A take check or a pick on a node without ComfyUI runs as a `breeze` job.
* A render goes to the node that holds its saved takes when that node is free, so it reuses them. Otherwise it takes a free NVIDIA node, then any free node.
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
* Tasks on the laptop run as SYSTEM, so the laptop can't run the Claude passes.

---

## Tests

```
python -m unittest tests.test_cluster
python tests/e2e_fake_nomad.py
```

The second test copies the code and one serial into a temporary folder and runs the app against a fake Nomad that
starts `bootstrap.py` as a local process for each dispatch.
