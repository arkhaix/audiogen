# serial_audio on the cluster

The web app runs on containers and holds the library. It runs quick library jobs (fetch, add a serial, reset the
bible, pick a voice) itself and dispatches everything else to the Nomad cluster. A node takes a job when its client
meta offers the job's class:

* `breeze`: Breeze renders through breeze-server, Breeze voice design, joining saved takes again and checking takes. The laptop (aj-legion5), the Mac (jsc-m1) and dev-arch.
* `comfyui`: Drama 3 renders, Qwen voice design and the bible's images, which need ComfyUI. The laptop.
* `llm`: the character pass, the look pass, the screenplay, the casting briefs, the casting bios and the image descriptions, which run `claude` under AJ's subscription. dev-arch.

A cast runs as two dispatches: the briefs on an `llm` node, then the voices on a GPU node, which finds the briefs
cached and doesn't call Claude. The voices stage is a `breeze` job, or a `comfyui` job for a Qwen cast. The options of
a name (`say-options`) run the same way: the respellings on an `llm` node, then the takes on a `breeze` node. Hear it
and Say them on the Names tab don't ask for respellings, so each is one `breeze` job. The takes go to the app's media
mirror with the job's result, so the Names tab plays them whichever node made them.

A `draw` job runs as two dispatches too: the descriptions on an `llm` node, then the images on a `comfyui` node,
which asks no LLM (`--briefed`). A draw from a description typed in its dialog is the `comfyui` dispatch alone. A
draw whose descriptions stage finds nothing to draw ends there and doesn't wait for the card. Its candidates come
back as library files under `images/`, so picking one is a quick job on the app's machine.

Every Breeze job runs while dev-arch is up, because it offers `llm` and `breeze` and the app keeps a copy of the
audio a later job needs from a node (The media mirror, below). A Drama 3 render, a Qwen cast and a draw need the
laptop.

---

## The app on containers

Everything lives in `/opt/serial-audio`, its own compose project beside the house's `/opt/docker-compose.yml`:

* `compose.yml` is a copy of `deploy/docker/compose.yml`.
* `src/` holds the repo's code: `serial_audio`, `prompts`, `schemas` and `deploy`.
* `library/` is the library. It is the copy of record, so nothing else should write to it.
* `state/` holds job records, job logs, `conflicts/` and `media/`, the media mirror (below). `jobs.json` and `placement.json` each have a copy beside them, `<name>.bak`, which the app writes once every ten minutes. When one of the two can't be read at a start, the app moves it to `<name>.unreadable`, loads the copy, writes the file again and says so on the page. The copy can be ten minutes old, so look at what the queue shows. Each later start says that `<name>.unreadable` is still there, and removes no job log while `jobs.json.unreadable` is. Remove the file when you have looked at it.
* `incoming/` holds uploaded EPUBs and saved pages.
* `secrets/nomad-token` holds the dispatch token, mode 600, owned by aj.

The daily backup is outside that folder, in `/mnt/nfs/serial-audio/backup` on storage's NFS share (Backup, below).

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

`GET /api/version` says what the app runs: `code`, the version a worker's bundle has, and `files`, the SHA-256 of
every file under `serial_audio`, `prompts` and `schemas` in the image. A deployed file has LF line endings, so
compare a hash with `git show HEAD:<path> | sha256sum`, not with the hash of a CRLF file in the laptop's tree. The
list doesn't have `deploy/`, which is not in the image.

`/api/state` has `tick`, which says how the app's job loop fares: when its last pass completed, how long ago, and the
failure of the newest pass. The page shows a banner when no pass has completed for 30 s or the newest one failed,
and the failure is in the container's log with its traceback. `notices` there lists what the app had to say about
its own state at its start, such as a state file it could not read.

### Backup

The app backs the library up once a day to `/mnt/nfs/serial-audio/backup`, which `compose.yml` mounts at `/backup`
and names with `--backup-dir`. `snapshots/library-<time>.tar.gz` holds `library/` without the audio and the images,
`incoming/` and `state/placement.json`, and the newest 30 stay. `library/<slug>/audio/` holds a copy of every
chapter's audio, which a later render of the chapter replaces, and `library/<slug>/images/` a copy of every image. A line that starts with `[mirror]` in the app's log reports each backup.

The app backs up only into a folder that holds the file `.serial-audio-backup`. When the share is not mounted as the
container starts, Docker makes an empty folder on the local disk in its place. The backup then fails, and the app's
header says "backup failing". Mount the share and run `docker compose restart`.

Restore on containers:

```
cd /opt/serial-audio && docker compose stop
tar xzf /mnt/nfs/serial-audio/backup/snapshots/library-<time>.tar.gz -C /opt/serial-audio
cp -a /mnt/nfs/serial-audio/backup/library/. library/
docker compose start
```

`--publish-dir` copies rendered chapters into audiobook folders the same way (`README.md`). It is off on containers.

---

## Nomad

* Namespace `serial-audio`.
* Jobs `serial-audio-breeze`, `serial-audio-comfyui` and `serial-audio-llm`: one parameterized batch job per class, from `deploy/nomad/serial-audio.nomad.hcl` with its task `deploy/nomad/bootstrap.py`.
* Pinned jobs `serial-audio-<class>-<node>`, one for each GPU class a GPU node offers: today `serial-audio-breeze-aj-legion5`, `serial-audio-comfyui-aj-legion5`, `serial-audio-breeze-jsc-m1` and `serial-audio-breeze-dev-arch`. The app picks the node for every GPU job and dispatches the pinned job.
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
nomad job run -var class=breeze  -var node=dev-arch -var user=aj deploy/nomad/serial-audio.nomad.hcl
```

`-var user=<name>` runs the task as that user, with that user's home. dev-arch's Nomad client runs as root, so its
pinned job names aj. Without the variable a task runs as the client's own user, and an `llm` task as aj.

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

The Jobs page can keep a node out without a change to Nomad. Hold on the node's row stops the app from sending it jobs
until Resume, and Going offline does the same until the node was away and is ready again. The node's running job
carries on either way, and its worker is handed no next job. The hold is the app's own, in `state/placement.json`
under `held`. Nomad still lists the node as eligible, and the app's token could not change that. From a shell:

```
curl -s -X POST https://audiogen.grayfam.us/api/nodes/hold -H 'Content-Type: application/json' -H 'X-Serial-Audio: 1' \
  -d '{"node": "aj-legion5", "held": true, "until_back": false}'
```

A GPU node also needs its pinned jobs registered (above).

The laptop sets them in `C:\nomad\config\client.hcl` (copy in the ansible repo, `nomad/aj-legion5/client.hcl`), then
`Restart-Service Nomad`. Its tasks run as SYSTEM, which works for renders and casting. The laptop's defaults are the
pipeline's built-in `D:\comfy` paths, so it has no settings file. The Claude passes don't run there, because the
`llm` job runs aj's install of `claude` on a Linux node. dev-arch has dynamic meta, set with
`nomad node meta apply -node-id <id> serial_audio=llm,breeze serial_audio_python=/usr/bin/python3
serial_audio_config=/home/aj/serial-audio/node.json`. Remove it with
`-unset serial_audio,serial_audio_python,serial_audio_config`, and take a class away by setting `serial_audio` without
it. `llm` jobs run as aj there, because they use aj's install
of `claude`. They sign in with the token in the Nomad variable (above), not with the login in `/home/aj/.claude`.
Its `breeze` jobs run as aj too, on the Iris Xe, beside the Claude passes (`docs/machines.md`, dev-arch).

A node with the class `breeze` needs breeze-server and its Q8_0 weights, and the Python with numpy and soundfile that
joins the takes. A `comfyui` node needs ComfyUI on 127.0.0.1:8188 with the ComfyUI-Drama3 and ComfyUI-Qwen-TTS node
packs. A draw needs the image models of `comfy.IMAGE_MODELS` in ComfyUI's models folder and no node pack. It uses
the ComfyUI that is up on the node, on port 8188 of any of the node's addresses, and starts one itself only when
none is (`SERIAL_AUDIO_COMFY_MAIN`, `docs/machines.md`).

---

## How a job runs

1. The app dispatches the job with `-meta job=<app job id> -meta key=<random per-job key>`. A Claude job goes to `serial-audio-llm`. For a GPU job the app picks the node and dispatches its pinned job (below).
2. The task's `bootstrap.py` fetches the job's bundle from the app: the code, the prompts and schemas, and the serial's folder without its audio and its images, about 1.5 MB for Twenty Thousand Leagues.
3. A GPU job takes the machine's GPU lock first (`C:\ProgramData\serial-audio\gpu.lock`, or `/tmp/serial-audio-gpu.lock`), so two GPU jobs on one machine run one after the other, whoever dispatched them. It also waits while ComfyUI is busy with another prompt and, on Windows, while a render or pipeline script started by hand runs. It then fetches from the app the chapter's saved takes that its node lacks (The media mirror, below). Then the worker runs the CLI steps in its copy of the serial and sends the log to the app every 3 seconds.
4. At the end the worker sends the app the takes and cast candidates its node made, and then every file the steps created or changed. The app writes a file only when the library copy still has the hash it had when the bundle was made. A file that changed meanwhile goes to `state/conflicts/<job id>/` instead, and the job's note says so. A failed or stopped job writes back only the files a rerun can use, such as `breeze_takes.json`, the casting briefs, the images a draw finished and the chapters a look pass read. The look pass and the draw write each of those files whole, under another name first, so a stop can't send back half a file. A failed Claude pass writes back the chapters it finished, because the passes save each chapter as it completes.
5. Stop in the app asks the worker to stop. It kills the CLI within a few seconds, cancels the job's ComfyUI prompts and reports. A job no node has started yet is cancelled at once, and its dispatch exits as soon as a node starts it. A dispatch that fails or times out is tried again with the same idempotency token, so Nomad never runs it twice. A plain Stop ends the job as stopped, and the queued jobs that need it are skipped once it has ended. Stop and queue again, in the Stop dialog of a job on the cluster, asks for the same stop and then puts the job back in its place in the queue, and the jobs that need it stay queued. Pause on a running job does that too and keeps the job out until Resume. Pause is offered only for a job that keeps what it made: a Breeze render before its join, which keeps its takes and hears the new ones again, and a draw that is not a redraw (`_pausable()` in `serial_audio/web/nomad.py`). Either way the stage that was stopped runs again under another key, and the stages before it stand. A job whose node is disconnected goes back into the queue at once, because no worker can confirm. A worker that doesn't confirm the stop within 90 seconds leaves the job stopped, because its task may still run, and the jobs that need it are then skipped as for a stop.
6. A worker that finishes a `breeze` job sends the version of its code with the result. The app answers with the job it would dispatch to that node next, when that is a `breeze` job too and the worker runs the app's code. The worker fetches that job's bundle and runs it in the same task, so the app doesn't dispatch a Nomad job for it, and the job's log says which task it runs in. The worker keeps the GPU lock from its first job to its last. The breeze-server and the Parakeet process its steps started stay up between the jobs, recorded in a registry beside the lock (`gpu.kept.json`, `serial_audio/kept.py`), so the next job doesn't load a model. The worker stops them when the app doesn't hand it a job, when a job fails or is stopped, when a handed job has other code than its own, and before it waits for something outside the job. A step that finds ComfyUI busy stops the kept breeze-server before it waits, so ComfyUI has the card. On Windows they also sit in a job object the worker holds, so they end with it however it ends. A worker that finds processes in the registry when it takes the lock stops them first. A handed job whose task ends before it fetched the bundle is queued again. `HAND_NEXT` in `serial_audio/web/nomad.py` turns the handing off.
7. A job whose node goes away is queued again. Nomad calls the allocation lost when its node has been disconnected for 30 minutes (`lost_after` in the job spec, which `LOST_AFTER_MINUTES` in `serial_audio/web/nomad.py` has to match for the note a waiting job shows), and the app also counts a task that failed after it saw the node disconnected. The stage that did not end runs again on any node that offers it, under another key, so the worker that went away can't send anything for it. The stages before it stand, and the jobs that need the job stay queued. A job that was asked to stop is not queued again. A job is queued again twice in a row at most, and a result of any stage starts that count again (`LOST_BACKS` in `serial_audio/web/nomad.py`, where 0 switches the rule off).

Jobs on different chapters, serials or nodes run at the same time. Jobs on the same chapter wait for each other, and
so does any job beside one that changes a whole serial (a cast, casting bios, picking a voice, fetching from the
site), with the exceptions that follow. The character pass has rules of its own, further down. A cast, casting bios, a voice pick, a say-as or a voice effect starts beside a running
render, screenplay or take check of its serial once that job has fetched its bundle, because it writes no chapter file
and the bundle holds the chapter job's copy of the bible, voices, pronunciations and effects. It also passes a chapter job that waits ahead of it in
its lane. A render or a take check queued after one of them still waits for it. Of the five a screenplay waits only
for a say-as, because the others change nothing a screenplay reads. Three Claude jobs run at once on the `llm` node
(`LLM_SLOTS` in `serial_audio/web/nomad.py`), so the screenplays of three chapters are written together and a cast's
briefs stage starts beside a running screenplay. A fourth Claude job waits for one of them to finish. A Claude job
does not wait behind a blocked job in its lane. Casting bios and a fetch or an import don't wait for each
other, because a fetch writes chapter files, the index and `serial.yaml` and the bios write only the bible. The import
of a saved page counts as a job on its chapter, because it writes only that chapter's files. It runs beside the renders,
screenplays and take checks of other chapters, and beside a cast, casting bios, a voice pick, a say-as or a voice
effect, once each running job of its serial has fetched its bundle, and a job that gets a bundle starts after a running
import. Imports of one serial run one after the other. A character pass
starts beside the renders, screenplays and take checks of other chapters on the same terms, because it writes the bible
and its own chapter's files. It waits for a job on its own chapter, for another character pass and for a cast. A
chapter job waits for a character pass only on its own chapter, because a pass of another chapter takes nothing out
of the bible that the job reads. A job whose record names no chapter counts as working on every chapter. A
chapter's screenplay therefore starts when its own character pass has ended, beside the later passes and the casts.
The Process dialog queues a cast after each chapter's character pass, ahead of the screenplays and renders. The first cast is for
everyone who needs a voice, and each later one names the chapter whose pass it follows (`from_chapter` in the job's
meta, "Cast · ch 55" in the queue). A render or a take check waits for a cast, which gives its speakers their voices,
but not for one that follows a later chapter's pass. So a chapter renders once the passes and casts up to its own
chapter and its screenplay are done. A cast for everyone that finds nobody who needs a voice is skipped when its turn
comes, without a dispatch. A laptop that sleeps through part of a render has 30 minutes to come back before Nomad
gives the allocation up, and the render is then queued again (point 7, above).

A job also waits for the jobs it needs, and a need is the only tie between two jobs that a failure follows.
`needs()` in `serial_audio/web/deps.py` names them from what each kind reads and writes. A render needs its chapter's
screenplay, its chapter's character pass and the cast that follows that pass. A screenplay needs its chapter's
character pass, a character pass the pass of the chapter before it, a cast the pass of its chapter, and each of
them the import of its chapter. Of two jobs that make the same thing, such as two screenplays of one chapter, a job
needs the later one, and it doesn't need a job that was asked to stop for good. A job's record keeps the ids of the jobs it still needs (`needs`), from the moment it
is queued, among every job that has not ended. That two jobs were queued in one request says nothing about them. So a
chapter renders as soon as its voices are cast and its screenplay is written, beside the other chapters' passes,
casts and screenplays. When a job fails, is lost, is stopped or is cancelled, the queued jobs that need it are
skipped, and so are the jobs that need those. Every other job runs. A skipped job's record names the job it lacks
(`lacks`). Run again on that job queues it again and brings the skipped jobs back behind it, each with the new job
among its needs. A job that is asked to stop skips nothing until it has ended, and until then the jobs that need it
wait. Every pair that `needs()` names also waits by the rules above, which a test checks, so a need doesn't add a
wait.
A look pass, a draw and an image pick write only `bible/looks.yaml`, `bible/settings.yaml`, the chapters' `look_pass.json` and `images/`, which no other job reads (`LOOK_KINDS` in `serial_audio/web/nomad.py`). So none of them waits for a render, a screenplay, a take check, a cast or a character pass of another chapter, and none of those waits for them. A look pass and the character pass or the import of a chapter wait for each other, and a look pass waits for another look pass of its serial. A draw waits for another draw, and an image pick for another pick. A pick and a draw wait for each other only when the draw names the pick's subject, so a pick starts beside a draw for everyone. A look pass and a draw don't wait for each other. Each of them and a bible reset wait for each other. The Process dialog queues a chapter's look pass after its character pass and its cast.
An import with "process the chapters after import" ticked queues the same jobs behind its imports. A character pass
or a screenplay queued behind the import of its chapter counts as a job on that chapter before the chapter is on disk,
so it needs the import, and a failed import skips it and the jobs that need it.

A job queued with `"backlog": true` in its params is backlog work, such as a render of an old chapter that a new
check wants to fix. It starts only when no job that is not backlog work is running or queued. Two backlog renders or take
checks run beside each other, each on a GPU node of its own, and a backlog job of any other kind runs alone. A job
that is not backlog work never waits for queued backlog work. A running backlog
job is left to finish, so the next job waits for it by the usual rules. A backlog cast that waits between its briefs
and its voices counts as running. The job list shows queued jobs in the order they start. Queue backlog work through
`/api/jobs`:

```
curl -s -X POST https://audiogen.grayfam.us/api/jobs -H 'Content-Type: application/json' -H 'X-Serial-Audio: 1' \
  -d '{"jobs": [{"kind": "render", "params": {"slug": "<slug>", "chapter": <n>, "backlog": true}}]}'
```

The app picks the node for a GPU job, one GPU job per node:

* A join and a take check need the chapter's takes. While the media mirror lacks one of them, the job runs on the node that holds them and waits for it. The wait reason then names that node as the one "which holds the files this job needs".
* Any other GPU job, and a join or a take check whose takes are all in the mirror, goes to the ready node where its estimated finish is earliest. The estimate is the node's running job's remaining time, from its progress log, plus the queued jobs already waiting for that node, plus the job itself: its takes times the node's seconds per take, the mean of the last three Breeze renders that finished on that node and made twenty takes or more and at least half of their chapter's lines, kept in `placement.json` (3 s for NVIDIA, 18 s for Metal and 25 s for a node without a GPU label until then), plus the parts around the takes (the next point). A Breeze render counts only the takes it has to make, on the node that holds the saved ones and, once the mirror holds them, on any node. A job on another node than the holder adds 0.05 s per take for fetching the takes and sending them back, so with both nodes free it stays where the takes are. A render whose reuse cannot be worked out is costed at all its takes. The job waits for a busy node when that beats every free node by two minutes or more, and its wait reason shows both estimates. A node whose running job's log has not changed for ten minutes counts as busy for a day, so nothing waits for it. A GPU job does not wait behind a blocked job in its lane.
* A Breeze render is costed part by part: 6 s to reach the node, its setup, which runs to the end of the first take and so has the model's warm-up in it, the takes after that, the check before the join, the join by the chapter's words and the sending back. The app notes when the first log line of each part arrives, and a stage that succeeds adds each part's seconds to `timing` in `placement.json` for its node, the last 12 of a kind. A part costs its default until the node has three timings of it, and from then on the default scaled by the median ratio of the node's timings to it. A render that makes under half of its chapter's lines is a touch-up, and its takes and its check are costed from the node's touch-ups, because its takes run longer and are flagged more often than a whole chapter's. A cast, a take check, the takes of a name's options and each Claude pass are timed from their step to their result in the same way. Delete a node's entry under `timing` to send its estimates back to the defaults.
* The Jobs page reads the same estimates. A running job shows the time to the end of its last stage, and it has none while its worker waits for the machine. Each queued job shows how long it takes, on which node, and when it is expected to end, from a forecast that places the queue by the rules above in outline. The app counts the takes of two queued renders a tick, so a render shows no time for up to a minute after it is queued or the app starts. A job also has no time while the queue is on hold or no ready node offers its class.
* The app records the node whose paths each chapter's takes file names in `state/placement.json` as results come back. For a chapter from before that record, it reads the node from the path style of its files, which works while one node runs each OS.

The static port 19137 that GPU jobs reserve doesn't keep them apart, because Nomad checks it per address and the
nodes have an IPv4 and an IPv6 tailnet address.

### The media mirror

A node writes Breeze takes and cast candidates to its own output folder, and the library names each by its path on
that node. The app keeps a copy of all of them in `state/media/`, under that path, so no job needs one node:

* After its steps, a worker sends the app every take of each chapter it changed a file of, and the candidates of each voice record it changed, with a Qwen candidate's Drama 3 voice file. It sends only the files the app lacks or holds with other contents.
* Before its steps, a take check, a Breeze render or a Breeze join fetches the chapter's takes whose files its node lacks. It puts each under its own output folder, at `breeze/<slug>/chNNNN/...`, and its copy of `breeze_takes.json` then names that file. The takes file goes back with the result, so the library names the paths of the node that last worked on the chapter. A fetched take whose audio is not what its entry's `audio` names is left out, and a render takes that line again.
* A node never replaces a take it made and still holds with the app's copy.
* Picking a voice runs on the app's machine and files the candidate's clip from the mirror. A Qwen candidate's Drama 3 voice file is filed in the library too, and a Drama 3 render copies it to ComfyUI's voice folder before it generates.
* The Cast tab and the take check play candidates and takes from the mirror.

A `sync-media` job sends the app everything one node holds for a serial that the app lacks. It has no CLI step and
no button. It writes no library file, so it does not wait for the serial's other jobs the way a cast or a render
does. It waits for its node to be free and for a running import of its serial, and a cast, a pick, a say-as or a
voice effect waits the few seconds until it has fetched its bundle. Queue one per GPU node after a failed upload,
or for audio from before the mirror kept takes:

```
curl -s -X POST http://containers.goblin-truck.ts.net:8765/api/jobs -H 'X-Serial-Audio: 1' \
  -d '{"jobs": [{"kind": "sync-media", "params": {"slug": "<slug>", "node": "aj-legion5"}}]}'
```

Its log ends with how many files it sent. While the app is unreachable, a worker tries a request again for 5 minutes
before it gives up on the audio and goes on to the result. A job's log says so when a file did not arrive, and the
next job on that chapter on the same node sends it again.

A chapter's takes get a path on the node that last worked on them, so each move of a chapter to another node leaves
the old node's copies in the mirror. A `prune-media` job removes the mirror's files that no takes file, voice
record or say-as option of any serial names. It keeps a file that changed in the last `days` days (7, and 1 at the
least), because a worker sends its audio ahead of the result that names it, and the app dates a copy again when
it tells a worker that it holds it. It leaves a file that is not audio. It removes nothing when a record of a
serial can't be read, or when the mirror and the library are not apart. It runs on the app's machine, has no button, and nothing queues it by itself. Count first
with a dry run, and read the job's log:

```
curl -s -X POST http://containers.goblin-truck.ts.net:8765/api/jobs -H 'X-Serial-Audio: 1' \
  -d '{"jobs": [{"kind": "prune-media", "params": {"dry_run": true}}]}'
```

---

## Known limits

* A job's key is in its dispatch meta, which every tailnet device can read through Nomad's anonymous policy. It ties a worker's uploads to its own job. It is not a secret, and the app has no login either, so the tailnet stays the boundary.
* The worker doesn't send back deleted files. Only fetch and reset-bible delete library files, and they run on the app's machine.
* A Drama 3 render, a Drama 3 decode, a Qwen cast and a draw need ComfyUI, which only the laptop has.
* A join or a take check of a chapter whose takes the mirror lacks waits for the node that holds them.
* The mirror keeps one copy of a chapter's takes for each node that worked on the chapter, about 70 MB each, until a `prune-media` job removes the copies the library no longer names (The media mirror, above). The daily backup leaves the mirror out, because the nodes hold the takes too.
* The Claude passes run only on a Linux node with aj's install of `claude`, which today is dev-arch.

---

## Tests

```
python -m unittest tests.test_cluster tests.test_mirror
python tests/e2e_fake_nomad.py
```

The end-to-end test copies the code into a temporary folder, writes a one-chapter serial there, and runs the app against
a fake Nomad that starts `bootstrap.py` as a local process for each dispatch. Neither test needs a library.
