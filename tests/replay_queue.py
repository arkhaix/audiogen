"""Dumps what the queue's rules answer for a copy of the app's state, to compare before and after a change.

Run: python tests/replay_queue.py dump <root> <out.json> [--queues N]
     python tests/replay_queue.py compare <before.json> <after.json>

<root> is a folder with library/, state/jobs.json and state/placement.json, copied from containers. The library
needs no audio, no images and no logs. An import builds only when <root>/incoming/<slug>/ has a file of the
uploaded page's name, which can be empty. The dump has five parts:

* build: what the app builds for the kind and params of each job record, or how it refuses them.
* requests: the same for made-up requests of every kind, good and bad, with the stages of what each builds.
* stages: the stages and the lane of each job record as it is stored.
* conflicts: for a sample of the records and for made-up records of every kind, which waits for which.
* queues: N made-up queues of the records, each with the jobs that every queued job needs, what one scheduling
  pass starts, on which node, why the others wait, which running job it asks to stop, and the forecast after the
  pass.

Two dumps of one root are equal when the rules answer the same, so a change that should keep every answer is
checked by a dump before it and one after it. The dump starts no process and no Nomad job. It writes out.json and
a temporary state folder, and nothing under <root>.
"""
from __future__ import annotations

import copy
import json
import random
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000.0
KINDS = ("character-pass", "screenplay", "render", "check-takes", "cast", "bios", "look-pass", "draw", "pick-image",
         "pick-voice", "say-as", "say-options", "voice-effect", "fetch", "add-royalroad", "add-epub", "reset-bible",
         "sync-media", "prune-media")
NODES = (("laptop", ["breeze", "comfyui"], "windows", "nvidia"), ("mac", ["breeze"], "darwin", "metal"),
         ("arch", ["llm", "breeze"], "linux", "none"))
SAMPLE = 120


def load(root: Path):
    """The app's modules with <root> as their root, and the job records of its state copy."""
    sys.path.insert(0, str(REPO))
    from serial_audio import library as lib
    # the web modules read the root when they are imported
    lib.ROOT, lib.LIBRARY = root, root / "library"
    from serial_audio.web import jobs, nomad
    records = json.loads((root / "state" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
    return jobs, nomad, [r for r in records if isinstance(r, dict) and r.get("id") and r.get("kind")]


def workers(nomad, rng: random.Random | None = None) -> list[dict]:
    """The three nodes as summaries. With rng, some are not ready, busy or held."""
    out = []
    for name, classes, os_name, gpu in NODES:
        w = {"name": name, "id": name, "classes": list(classes), "os": os_name, "gpu": gpu, "status": "ready",
             "eligible": True, "busy": False, "held": False, "until_back": False}
        if rng is not None:
            roll = rng.random()
            w.update({"status": "down"} if roll < 0.08 else {"busy": True} if roll < 0.14 else
                     {"held": True} if roll < 0.2 else {})
        out.append({**w, "ready": nomad._ready(w)})
    return out


def manager(nomad, root: Path, state: Path):
    """A cluster manager on a state folder of its own, with the placement of the copy, that starts nothing."""
    state.mkdir(parents=True, exist_ok=True)
    placement = root / "state" / "placement.json"
    if placement.is_file():
        (state / "placement.json").write_bytes(placement.read_bytes())
    m = nomad.NomadJobManager(nomad.NomadClient("http://127.0.0.1:9", "token"), state_dir=state, autostart=False)
    m.workers = workers(nomad)
    m.nomad_state["up"] = True

    def launched(job):
        job.update({"status": "running", "pid": None, "started": "t", "note": None})

    m._launch_local = launched
    m._append_log = lambda job, text: None
    m._save = lambda: None
    m._save_placement = lambda: None
    return m


def answer(fn):
    """What a rule answers, or how it fails, as something a dump can hold."""
    try:
        return {"ok": fn()}
    except ValueError as e:
        return {"refused": str(e)}
    except Exception as e:
        return {"raised": f"{type(e).__name__}: {e}"}


def made_up(kind: str, slug: str, chapters: list[int], seq: int) -> dict:
    """A job record of a kind as the conflict rules read one, with the flags that change what they answer."""
    args = [kind, slug] + [str(n) for n in chapters[:1]]
    meta: dict = {"chapters": chapters}
    if kind == "fetch" and chapters:
        args += ["--html", "page.html"]
    if kind == "cast" and chapters:
        meta = {"from_chapter": chapters[0], "characters": []}
    if kind in ("draw", "pick-image"):
        meta = {"characters": ["hero"] if chapters else [], "settings": [], "subject": "characters/hero"}
    if kind == "render":
        meta["engine"] = "breeze"
    return {"id": f"made-{kind}-{slug}-{'-'.join(map(str, chapters)) or 'all'}", "kind": kind, "slug": slug,
            "args": args, "meta": meta, "seq": seq, "status": "queued", "heavy": True, "comfy": False,
            "title": f"{kind} {chapters}"}


def requests(root: Path) -> list[tuple[object, object]]:
    """Requests of every kind as (kind, params), good ones and ones the app refuses, for the copy's first serial.

    Each branch of jobs.build() is reached by one request or more, so a dump of what the app builds for them
    covers the kinds that the copy's records don't have.
    """
    serials = sorted(p.name for p in (root / "library").iterdir() if (p / "serial.yaml").is_file())
    s = serials[0] if serials else "none"
    pages = sorted(p.name for p in (root / "incoming" / s).glob("*.htm*")) if (root / "incoming" / s).is_dir() else []
    page = pages[0] if pages else "page.html"
    llm = [{}, {"backend": "api"}, {"model": "sonnet", "effort": "high"}, {"on_limit": "wait"}, {"model": "gpt"},
           {"backend": 7}, {"effort": "none"}]
    out: list[tuple[object, object]] = []

    def add(kind, *params):
        out.extend((kind, {"slug": s, **p}) for p in params)

    for kind in ("character-pass", "screenplay", "look-pass"):
        add(kind, *({"chapters": c} for c in ("7", "7-12", "3,5,8", "all", "new", "", "x", "7 - 9", None)),
            {"chapters": "7", "redo": True}, *({"chapters": "2", **extra} for extra in llm))
    add("render", {"chapter": 7}, {"chapter": "7", "seed": 5}, {"chapter": 7, "decode_only": True}, {}, {"chapter": "x"},
        {"chapter": 0}, {"chapter": 7, "seed": -1}, {"chapter": True}, {"chapter": 99999}, {"chapter": 100000})
    add("check-takes", {"chapter": 7}, {"chapter": 7, "redo": True}, {})
    add("cast", {}, {"characters": ["ann", "bo"]}, {"candidates": 5, "seed": 42}, {"recast": True},
        {"recast": True, "characters": ["ann"]}, {"instruct": "a low voice", "characters": ["ann"]},
        {"instruct": "a low voice", "characters": ["ann", "bo"]}, {"redo_brief": True, "note": "older"},
        {"dry_run": True}, {"from_chapter": 5}, {"from_chapter": 5, "characters": ["ann"]}, {"candidates": 99},
        {"characters": ["Bad Id"]}, {"characters": "ann"}, {"note": "x" * 501}, *llm)
    add("bios", {}, {"characters": ["ann"]}, {"redo": True}, {"characters": ["a", "b", "c", "d"], "redo": True})
    add("draw", {}, {"characters": ["ann"], "settings": ["hall"]}, {"candidates": 3, "seed": 9}, {"redraw": True},
        {"redraw": True, "settings": ["hall"]}, {"prompt": "a tall woman", "characters": ["ann"]},
        {"prompt": "a tall woman", "characters": ["ann"], "settings": ["hall"]}, {"redo_brief": True, "note": "older"},
        {"style": "film"}, {"style": "characters"}, {"style_text": "oil on canvas"}, {"image_model": "qwen21"},
        {"image_model": "nope"}, {"dry_run": True}, {"candidates": 9}, {"characters": ["a", "b", "c", "d"]})
    add("pick-image", {"subject": "characters/ann"}, {"subject": "settings/hall", "seed": 3, "approve": False},
        {"subject": "characters/ann", "style": "film"}, {"subject": "ann"}, {"subject": "things/ann"}, {})
    add("pick-voice", {"character": "ann"}, {"character": "ann", "seed": 3, "approve": False}, {"character": "Bad Id"},
        {})
    add("say-as", {"term": "Aeol", "say": "Ay-ohl"}, {"term": "Aeol", "clear": True}, {"say": "Ay-ohl"},
        {"term": "Aeol"}, {"term": "Aeol", "say": "Ay-ohl", "note": "as in the audiobook"},
        {"term": "Aeol", "say": "Ay-ohl", "note": "x" * 601}, {"term": "-dash", "say": "--flag"})
    add("say-options", {"term": "Aeol"}, {"term": "Aeol", "say": "Ay-ohl"}, {"term": "Aeol", "render_only": True},
        {"term": "Aeol", "say": "Ay-ohl", "render_only": True}, {"term": "Aeol", "say": "123"}, {},
        {"term": "Aeol", "model": "haiku"})
    add("voice-effect", {"voice": "ann", "effect": "whisper"}, {"voice": "ann", "effect": "whisper", "level": 0.5},
        {"voice": "ann", "effect": "whisper", "level": 2}, {"voice": "ann", "clear": True},
        {"voice": "ann", "effect": "echo"}, {"voice": "Bad Id", "effect": "whisper"}, {"voice": "ann"})
    add("fetch", {}, {"chapters": "new", "limit": 5}, {"chapters": "3-4", "limit": 5, "no_refresh": True, "force": True},
        {"html": page, "chapter": 7}, {"html": page, "chapter": 7, "force": True}, {"html": page},
        {"html": "missing.html", "chapter": 7}, {"html": "../serial.yaml", "chapter": 7}, {"chapters": "x"})
    add("reset-bible", {"confirm": s}, {"confirm": "no"}, {})
    add("sync-media", {"node": "laptop"}, {"node": "arch"}, {"node": "nobody"}, {})
    for params in ({"url": "12345"}, {"url": "https://www.royalroad.com/fiction/12345/a-title"}, {"url": "http://x.y"},
                   {"url": "12345", "chapters": "none"}, {"url": "12345", "chapters": "1-3", "slug": "new-one"},
                   {"url": "12345", "slug": "Bad Slug"}, {"file": "missing.epub"}, {"file": "a.txt"}, {},
                   {"file": "book.epub", "slug": "new-one", "title": "A Book", "author": "A. Writer", "force": True}):
        out.extend((kind, dict(params)) for kind in ("add-royalroad", "add-epub"))
    out += [("prune-media", {}), ("prune-media", {"days": 30}), ("prune-media", {"dry_run": True}),
            ("prune-media", {"days": 0}), ("prune-media", {"days": "x"}), ("render", {"slug": "no-such-serial", "chapter": 1}),
            ("render", {"slug": "Bad Slug", "chapter": 1}), ("no-such-kind", {"slug": s}), ("render", "not params"),
            (None, {}), ("render", {"slug": s, "chapter": 7, "backlog": True})]
    return out


def sample(records: list[dict]) -> list[dict]:
    """The records that differ in what the conflict rules read, SAMPLE at most, and made-up ones of every kind."""
    seen, out = set(), []
    for r in records:
        meta = r.get("meta") or {}
        flags = tuple(a for a in r.get("args") or [] if isinstance(a, str) and a.startswith("--") and "=" not in a)
        key = json.dumps([r["kind"], r.get("slug"), meta.get("chapters"), meta.get("from_chapter"), flags,
                          meta.get("subject"), meta.get("characters"), meta.get("settings"), meta.get("engine")],
                         sort_keys=True, default=str)
        if key not in seen and len(out) < SAMPLE:
            seen.add(key)
            out.append(r)
    seq = 10_000
    for kind in KINDS:
        for slug in ("s1", "s2"):
            for chapters in ([], [1], [2], [1, 2]):
                seq += 1
                out.append(made_up(kind, slug, chapters, seq))
    return out


def other_serials() -> list[dict]:
    """Job records of six serials the copy doesn't have, so a made-up queue has jobs that don't wait for each
    other and reach the choice of a node: renders, take checks, a draw, a cast and the quick jobs of each."""
    out = []

    def add(kind, slug, name, args, meta, gpu=False, comfy=False, heavy=True):
        out.append({"id": f"other-{kind}-{slug}-{name}", "kind": kind, "slug": slug, "args": [kind, slug] + args,
                    "meta": meta, "heavy": heavy, "gpu": gpu, "comfy": comfy, "title": f"{kind} · {slug} {name}",
                    "params": {}})

    for s in range(1, 7):
        slug = f"other-{s}"
        for n in range(1, 9):
            add("render", slug, n, [str(n), "--engine", "breeze", "--runner", "cpp"],
                {"chapters": [n], "lines": 40 + 45 * n, "decode_only": False, "engine": "breeze", "runner": "cpp"},
                gpu=True)
        add("render", slug, "join", ["9", "--engine", "breeze", "--runner", "cpp", "--decode-only"],
            {"chapters": [9], "lines": 200, "decode_only": True, "engine": "breeze", "runner": "cpp"})
        add("check-takes", slug, 1, ["1"], {"chapters": [1], "lines": 120}, gpu=True, comfy=True)
        add("draw", slug, "all", ["--candidates", "2"],
            {"characters": [], "settings": [], "candidates": 2, "dry_run": False, "subjects": 4 + s}, gpu=True,
            comfy=True)
        add("draw", slug, "redraw", ["--candidates", "2", "--characters", "hero", "--redraw"],
            {"characters": ["hero"], "settings": [], "candidates": 2, "dry_run": False}, gpu=True, comfy=True)
        add("cast", slug, "all", ["--candidates", "3", "--engine", "breeze"],
            {"characters": [], "candidates": 3, "dry_run": False}, gpu=True, comfy=True)
        add("say-options", slug, "term", ["--render-only", "--", "Aeol"], {"term": "Aeol"}, gpu=True)
        add("screenplay", slug, 3, ["3"], {"chapters": [3]})
        add("look-pass", slug, 3, ["3"], {"chapters": [3]})
        add("pick-voice", slug, "hero", ["hero"], {"characters": ["hero"]}, heavy=False)
        add("say-as", slug, "term", ["--", "Aeol", "Ay-ohl"], {"term": "Aeol"}, heavy=False)
    return out


def queue(records: list[dict], nomad, rng: random.Random) -> tuple[list[dict], list[dict], bool, dict]:
    """A made-up queue: some records queued in a new order, some running on the nodes, the nodes, whether the
    queue is on hold, and the takes that most of its Breeze renders were counted to make and hear."""
    nodes = workers(nomad, rng)
    picked = rng.sample(records, min(len(records), rng.randint(4, 45)))
    running_n = rng.randint(0, min(5, len(picked)))
    out, counts = [], {}
    for seq, record in enumerate(picked, 1):
        job = copy.deepcopy(record)
        for key in ("nomad", "stopping", "evicted", "gave_way", "asked_stop", "taken_back", "lost_node", "paused"):
            job.pop(key, None)
        job.update({"seq": seq, "status": "queued", "started": None, "finished": None, "exit_code": None,
                    "note": None, "pid": None, "backlog": bool(job.get("backlog")) or rng.random() < 0.25,
                    "created": f"2026-11-01T00:{seq // 60:02d}:{seq % 60:02d}+00:00"})
        job.setdefault("heavy", False)
        job.setdefault("gpu", False)
        job.setdefault("comfy", False)
        job.setdefault("title", job["kind"])
        job.setdefault("meta", {})
        job.setdefault("args", [])
        if rng.random() < 0.05:
            job["paused"] = True
        stages = answer(lambda: nomad.job_stages(job)).get("ok")
        if seq <= running_n:
            job.update({"status": "running", "started": "2026-11-01T00:00:00+00:00"})
            if stages:
                cls = stages[0]["class"]
                offering = [w["name"] for w in nodes if cls in w["classes"]]
                node = rng.choice(offering) if offering else None
                contact = "t" if rng.random() < 0.8 else None
                job["nomad"] = {"key": f"key-{seq}", "stage": 0, "stages": stages, "dispatched": f"task-{seq}",
                                "target": node if cls != "llm" else None, "node": node, "contact": contact,
                                "log_got": 16 if contact else 0, "dispatching": False, "marks": {}}
        if job["kind"] == "render" and (job.get("meta") or {}).get("engine") == "breeze" and rng.random() < 0.85:
            lines = (job.get("meta") or {}).get("lines") or 200
            counts[job["id"]] = (rng.choice([0, 3, lines // 4, lines]), rng.choice([0, 0, lines // 2]))
        out.append(job)
    return out, nodes, rng.random() < 0.06, counts


def play(m, nomad, jobs_now: list[dict], nodes: list[dict], paused: bool, counts: dict, mirrored: bool) -> dict:
    """One scheduling pass over a made-up queue and the forecast after it."""
    m.jobs = {j["id"]: j for j in jobs_now}
    m._wire_old(set(m.jobs))
    m.workers, m.paused = nodes, paused
    for cache in (m._picked, m._plan, m._takes_cache, m._mirror_cache, m._work, m._words_cache, m.wait_reasons):
        cache.clear()
    m._work.update({jid: (NOW, needed, unheard) for jid, (needed, unheard) in counts.items()})
    dispatch: list[str] = []
    patches = [mock.patch.object(nomad.time, "time", lambda: NOW)]
    if mirrored:
        patches.append(mock.patch.object(m, "_mirrored", lambda slug, n: True))
    for p in patches:
        p.start()
    try:
        passed = answer(lambda: m._schedule(NOW, dispatch))
        after = {}
        for j in jobs_now:
            n = j.get("nomad") or {}
            st = n["stages"][n["stage"]] if n.get("stages") else None
            after[j["id"]] = {k: v for k, v in {
                "status": j["status"], "wait": m.wait_reasons.get(j["id"]), "note": j.get("note"),
                "needs": j.get("needs") or None, "lacks": j.get("lacks"),
                "target": n.get("target"), "class": st["class"] if st else None, "cancel": n.get("cancel"),
                "evict": n.get("evict"), "asked_stop": j.get("asked_stop"), "follows": n.get("follows")}.items()
                if v not in (None, False)}
        forecast = answer(lambda: {jid: {k: (round(v, 1) if isinstance(v, float) else v) for k, v in o.items()}
                                   for jid, o in m._forecast(NOW).items()})
    finally:
        for p in patches:
            p.stop()
    return {"pass": passed, "dispatch": dispatch, "jobs": after, "forecast": forecast,
            "troubles": dict(getattr(m, "troubles", {}))}


def dump(root: Path, queues: int) -> dict:
    jobs, nomad, records = load(root)
    with tempfile.TemporaryDirectory() as tmp:
        m = manager(nomad, root, Path(tmp) / "state")
        try:
            # Build and stages
            out: dict = {"records": len(records), "build": {}, "requests": [], "stages": {}, "conflicts": {},
                         "queues": []}
            for r in records:
                out["build"][r["id"]] = answer(lambda: m._build(r["kind"], copy.deepcopy(r.get("params") or {}), {}))
                out["stages"][r["id"]] = {"stages": answer(lambda: nomad.job_stages(r)),
                                          "lane": answer(lambda: m.lane(r))}

            # Requests of every kind, and the stages of what each builds
            for kind, params in requests(root):
                got = answer(lambda: m._build(kind, copy.deepcopy(params), {"other": {3}}))
                built = got.get("ok")
                staged = answer(lambda: nomad.job_stages({"kind": kind, **built})) if built else None
                out["requests"].append({"kind": kind, "params": params, "built": got, "stages": staged})

            # Conflicts
            some = sample(records)
            out["conflicts"]["jobs"] = [f"{r['id']} {r['kind']} {r.get('slug')} {(r.get('meta') or {}).get('chapters')}"
                                        for r in some]
            def waits(a: dict, b: dict) -> str:
                got = answer(lambda: nomad.conflicts(a, b))
                return "x" if "ok" not in got else "1" if got["ok"] else "0"

            out["conflicts"]["rows"] = ["".join(waits(a, b) for b in some) for a in some]

            # Queues: the even ones of the copy's records and the others', the odd ones of the others' alone
            others = other_serials()
            for seed in range(queues):
                rng = random.Random(seed)
                jobs_now, nodes, paused, counts = queue(others if seed % 2 else records + others, nomad, rng)
                out["queues"].append({"seed": seed, "paused": paused,
                                      "nodes": [f"{w['name']} {'ready' if w['ready'] else 'not ready'}" for w in nodes],
                                      **play(m, nomad, jobs_now, nodes, paused, counts, mirrored=seed % 4 < 2)})
        finally:
            m._lock_file.close()
    # a path under the root would make two copies of one state differ
    text = json.dumps(out, sort_keys=True, default=str)
    for spelled in (json.dumps(str(root))[1:-1], str(root).replace("\\", "/")):
        text = text.replace(spelled, "<root>")
    return json.loads(text)


def differences(before, after, where: str = "") -> list[str]:
    """Where two dumps differ, as lines."""
    if type(before) is not type(after):
        return [f"{where}: {json.dumps(before)[:200]} became {json.dumps(after)[:200]}"]
    if isinstance(before, dict):
        out = []
        for key in sorted(set(before) | set(after)):
            if key not in before or key not in after:
                out.append(f"{where}/{key}: only in the {'second' if key not in before else 'first'} dump")
            else:
                out += differences(before[key], after[key], f"{where}/{key}")
        return out
    if isinstance(before, list) and len(before) == len(after):
        return [line for i, (a, b) in enumerate(zip(before, after)) for line in differences(a, b, f"{where}[{i}]")]
    return [] if before == after else [f"{where}: {json.dumps(before)[:200]} became {json.dumps(after)[:200]}"]


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "dump":
        queues = int(argv[argv.index("--queues") + 1]) if "--queues" in argv else 200
        out = dump(Path(argv[1]).resolve(), queues)
        Path(argv[2]).write_text(json.dumps(out, sort_keys=True, indent=1), encoding="utf-8")
        started = sum(1 for q in out["queues"] for j in q["jobs"].values() if j["status"] == "running")
        failed = sum(1 for q in out["queues"] if "ok" not in q["pass"] or "ok" not in q["forecast"] or q["troubles"])
        print(f"{out['records']} records, {len(out['conflicts']['jobs'])} jobs weighed against each other, "
              f"{len(out['queues'])} queues with {started} jobs running after their pass, {failed} with a failure")
        return 0
    if len(argv) == 3 and argv[0] == "compare":
        before, after = (json.loads(Path(p).read_text(encoding="utf-8")) for p in argv[1:])
        lines = differences(before, after)
        for line in lines[:60]:
            print(line)
        print(f"{len(lines)} difference(s)")
        return 1 if lines else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
