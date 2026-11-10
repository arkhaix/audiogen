"""Casting: design a voice for each character that needs one, check it, and file it.

For every character whose importance qualifies for a dedicated voice and who has no voice yet:
  1. A casting pass (LLM) turns the bible record into a voice-design instruction and a sample line.
  2. A designer makes N candidate clips of the sample line from the instruction. Engine "qwen" runs Qwen3-TTS
     Voice Design in ComfyUI and encodes each clip as a Drama 3 voice. Engine "breeze" runs Breeze TTS 2's voice
     design in breeze-server, with no ComfyUI, and makes no Drama 3 voice.
  3. A speech recognizer (asr.py) transcribes the candidates. The one whose transcript best matches the sample
     line is chosen, copied into the library, and recorded in the bible with status "auto", to approve or recast
     after listening. A candidate whose clip is clipped is chosen only when every candidate is clipped.
"""
from __future__ import annotations

import array
import difflib
import shutil
import sys
import time
import wave
from pathlib import Path

from . import asr as recognizer
from . import library as lib
from . import machine
from .comfy import ComfyClient, ComfyError, design_graph, encode_voice_graph
from .llm import run_pass

CASTING_PROMPT = lib.PROMPTS / "casting_pass.system.md"
CASTING_SCHEMA = lib.SCHEMAS / "casting_pass.schema.json"
COMFY_OUTPUT = machine.COMFY_OUTPUT
VOICES_OUTPUT = COMFY_OUTPUT / "drama3_voices"
BREEZE_CAST_OUTPUT = COMFY_OUTPUT / "breeze" / "cast"
ENGINES = ("qwen", "breeze")
SOURCES = {"qwen": "qwen3-tts-12hz-1.7b-voicedesign", "breeze": "breeze-tts-2-voicedesign"}
DESIGN_CFG = 4.0
MATCH_THRESHOLD = 0.85
CLIP_RUN = 3
NARRATOR_ID = "narrator"       # the voice of a third-person serial's narration; not a bible character
TONE_SAMPLE_WORDS = 400
BATCH_KEYS = ("instruct", "sample_text", "seed", "status", "created_at", "candidates", "engine", "asr")


def plan_cast_engine(serial: dict, engine: str | None = None) -> str:
    """The voice design engine a cast would use.

    serial: the serial.yaml record.
    engine: "qwen" or "breeze" to force one.
    Returns engine, else serial.yaml cast_engine, else breeze. Raises ComfyError when it is unknown.
    """
    engine = engine or serial.get("cast_engine") or "breeze"
    if engine not in ENGINES:
        raise ComfyError(f"unknown cast engine {engine!r}; known: {', '.join(ENGINES)}")
    return engine


def needs_narrator(serial: dict) -> bool:
    """A third-person serial narrates in a voice that belongs to no character, so one must be designed."""
    nar = serial.get("narration") or {}
    return nar.get("point_of_view") == "third_person" and not serial.get("narrator_voice_id")


def narrator_record(slug: str, serial: dict) -> dict:
    """A casting record for the narrator, built from the serial's blurb, tags, and the opening of chapter 1.
    `narrator_brief` in serial.yaml, when set, steers the design; otherwise the casting pass chooses."""
    numbers = lib.list_chapters(slug)
    sample = ""
    if numbers:
        _, text = lib.load_chapter(slug, numbers[0])
        body = text.split("\n", 1)[1] if text.startswith("#") else text
        paras = [p for p in body.split("\n\n") if p.strip() and not p.startswith(">")]
        sample = " ".join(" ".join(paras).split()[:TONE_SAMPLE_WORDS])
    tags = ", ".join((serial.get("source") or {}).get("tags") or [])
    title = serial.get("title", slug) + (f" ({serial['subtitle']})" if serial.get("subtitle") else "")
    brief = serial.get("narrator_brief") or (
        "Not specified. Choose the narrator this story calls for: sex, age, pitch, pace and register that suit "
        "its genre, tone and protagonist, and that a listener can live with for hundreds of chapters.")
    description = title + (f"; tags: {tags}" if tags else "") + ".\nBlurb: " + (serial.get("description") or "").strip()
    if sample:
        description += "\nOpening of chapter 1: " + sample
    return {"name": "the narrator", "gender": "unspecified", "age_group": "unspecified", "importance": "narrator",
            "role": "Narrator of a third-person story: reads all narration and the lines of anyone without a voice.",
            "description": description, "voice_brief": brief, "facts": []}


def characters_needing_voices(serial: dict, bible: dict, only: list[str] | None = None) -> list[str]:
    """Ids to cast: the narrator first when the serial needs one, then every uncast character the policy
    covers who has spoken (silent recurring characters wait until they speak), or exactly `only`."""
    wanted = set(serial.get("casting_policy", {}).get("dedicated_voice_for", ["major", "recurring"]))
    out = []
    if needs_narrator(serial) and (not only or NARRATOR_ID in only):
        out.append(NARRATOR_ID)
    for cid, c in bible["characters"].items():
        if only and cid not in only:
            continue
        if c.get("voice_id"):
            continue
        if only or (c.get("importance") in wanted and c.get("speaks", True)):
            out.append(cid)
    return out


def deferred_characters(serial: dict, bible: dict) -> list[str]:
    """Policy-covered characters without a voice who have not spoken yet."""
    wanted = set(serial.get("casting_policy", {}).get("dedicated_voice_for", ["major", "recurring"]))
    return [cid for cid, c in bible["characters"].items()
            if not c.get("voice_id") and c.get("importance") in wanted and not c.get("speaks", True)]


def casting_records(slug: str, serial: dict, bible: dict, ids: list[str]) -> dict[str, dict]:
    return {cid: narrator_record(slug, serial) if cid == NARRATOR_ID else bible["characters"][cid] for cid in ids}


def casting_pass(serial: dict, records: dict[str, dict], *, backend="auto", model="opus", effort=None,
                 on_limit="api", log=print) -> dict[str, dict]:
    """id -> {instruct, sample_text} for the given records (characters or the narrator), in one LLM call."""
    lines = [f"# Casting for {serial['title']} by {serial.get('author') or 'unknown'}\n"]
    for cid, c in records.items():
        facts = "; ".join(f["text"] for f in c.get("facts", [])[:8])
        lines.append(f"## {cid}\nname: {c['name']}\ngender: {c['gender']}\nage_group: {c['age_group']}\n"
                     f"importance: {c['importance']}\nrole: {c['role']}\ndescription: {c['description']}\n"
                     f"voice_brief: {c['voice_brief']}\nfacts: {facts}\n")
    result = run_pass(CASTING_PROMPT.read_text(encoding="utf-8"), "\n".join(lines), lib.load_json(CASTING_SCHEMA),
                      model=model, effort=effort, backend=backend, on_limit=on_limit, log=log,
                      instruction="Write the voice description and sample line for every character above and answer "
                                  "with the JSON object only.")
    log(f"casting pass: {len(result.data['voices'])} briefs via {result.backend}/{result.model}, "
        f"est ${result.cost_estimate_usd or 0:.4f}")
    return {v["id"]: v for v in result.data["voices"]}


def transcript_match(expected: str, heard: str) -> float:
    a, b = recognizer.words(expected), recognizer.words(heard)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _newest(folder: Path, pattern: str, seen: set[Path]) -> Path | None:
    files = [p for p in folder.glob(pattern) if p not in seen]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def generate_candidates(client: ComfyClient, *, slug: str, cid: str, brief: dict, count: int, seed0: int,
                        log=print) -> list[dict]:
    """Designs `count` candidates with Qwen3-TTS Voice Design and encodes each as a Drama 3 voice.

    client: the ComfyUI that designs and encodes.
    slug: the serial.
    cid: the voice id.
    brief: holds instruct and sample_text.
    count, seed0: the candidates use seeds seed0, seed0 + 1, ...
    log: receives one line per candidate.
    Returns one record per candidate, {seed, clip, voice_file}, unscored. Raises ComfyError when ComfyUI fails or a
    clip or voice file does not appear.
    """
    voice_dir = VOICES_OUTPUT / "cast" / slug
    voice_dir.mkdir(parents=True, exist_ok=True)
    seen = set(voice_dir.glob(f"{cid}_*.safetensors"))
    candidates = []
    for i in range(count):
        seed = seed0 + i
        t0 = time.time()
        client.free()
        entry = client.run(design_graph(text=brief["sample_text"], instruct=brief["instruct"], seed=seed,
                                        clip_prefix=f"drama3/cast/{slug}/{cid}"))
        clip_items = entry.get("outputs", {}).get("save_clip", {}).get("audio") or []
        if not clip_items:
            raise ComfyError(f"no clip saved for {cid} seed {seed}: outputs {list(entry.get('outputs', {}))}")
        clip = client.output_path(clip_items[0])
        clip_ref = client.output_ref(clip_items[0])
        client.free()
        client.run(encode_voice_graph(clip_ref=clip_ref, transcript=brief["sample_text"], voice_name=f"cast/{slug}/{cid}"))
        voice_file = _newest(voice_dir, f"{cid}_*.safetensors", seen)
        if voice_file is None:
            raise ComfyError(f"no Drama 3 voice file appeared in {voice_dir} for {cid} seed {seed}")
        seen.add(voice_file)
        candidates.append({"seed": seed, "clip": str(clip), "voice_file": str(voice_file)})
        log(f"   {cid} seed {seed}: designed in {time.time() - t0:.0f} s  {clip.name}")
    client.free()
    return candidates


def _free_name(folder: Path, stem: str, suffix: str) -> Path:
    path, n = folder / f"{stem}{suffix}", 2
    while path.exists():
        path, n = folder / f"{stem}_{n}{suffix}", n + 1
    return path


def design_breeze(slug: str, plan: list[tuple[str, dict]], *, count: int, seed0: int, comfy_url: str,
                  log=print) -> tuple[dict[str, list[dict]], ComfyError | None]:
    """Designs `count` candidates per voice with Breeze TTS 2 in breeze-server, from each brief's instruction.

    slug: the serial.
    plan: (voice id, brief) pairs, the brief holding instruct and sample_text.
    count, seed0: the candidates use seeds seed0, seed0 + 1, ...
    comfy_url: the ComfyUI that is asked to free the card first.
    log: receives one line per candidate.
    Returns (designed, failure). designed maps each voice whose candidates all succeeded to one record per
    candidate, {seed, clip}, unscored, with each clip a 16-bit WAV under BREEZE_CAST_OUTPUT. failure is the error
    that stopped the designs, or None, and no voice after it is designed. The server is stopped before this returns.
    Raises ComfyError when breeze-server cannot start or another one runs.
    """
    from .breeze import CPP_FIELDS, CppServer, free_comfy, wait_comfy_idle

    # Setup
    folder = BREEZE_CAST_OUTPUT / slug
    folder.mkdir(parents=True, exist_ok=True)
    server = CppServer(folder / "breeze_server.log", log)
    server.refuse_if_busy()
    wait_comfy_idle(comfy_url, log)
    free_comfy(comfy_url)
    server.start()

    # Design
    out: dict[str, list[dict]] = {}
    failure = None
    try:
        for cid, brief in plan:
            cands = []
            for i in range(count):
                seed = seed0 + i
                t0 = time.time()
                fields = {"text": brief["sample_text"], "instruction": brief["instruct"], "seed": seed,
                          "cfg_scale": DESIGN_CFG, **CPP_FIELDS}
                try:
                    pcm = server.take(fields, None, f"{cid} seed {seed}", log)
                except ComfyError as e:
                    failure = ComfyError(f"designing {cid} seed {seed} failed: {e}")
                    break
                clip = _free_name(folder, f"{cid}_s{seed}", ".wav")
                with wave.open(str(clip), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(server.sample_rate)
                    w.writeframes(pcm)
                cands.append({"seed": seed, "clip": str(clip)})
                log(f"   {cid} seed {seed}: designed {len(pcm) / 2 / server.sample_rate:.1f} s of audio in "
                    f"{time.time() - t0:.0f} s  {clip.name}")
            if failure:
                break
            out[cid] = cands
    finally:
        server.stop()
    return out, failure


def full_scale_run(clip: Path) -> int:
    """The longest run of consecutive full-scale samples in a clip.

    clip: a 16-bit PCM WAV.
    Returns the run length in samples, or 0 for any file that is not a readable 16-bit PCM WAV.
    """
    if clip.suffix.lower() != ".wav":
        return 0
    try:
        with wave.open(str(clip), "rb") as w:
            if w.getsampwidth() != 2:
                return 0
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return 0
    samples = array.array("h", frames[: len(frames) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    longest = run = 0
    for s in samples:
        run = run + 1 if s >= 32767 or s <= -32767 else 0
        if run > longest:
            longest = run
    return longest


def score_candidates(cid: str, brief: dict, candidates: list[dict], *, asr: str, comfy_url: str, log=print) -> None:
    """Transcribes each candidate's clip and adds its transcript and its match with the sample line, in place.

    asr: the recognizer, "qwen3" or "parakeet".
    A candidate whose clip has CLIP_RUN or more full-scale samples in a row also gets the key clipped, set to the
    length of its longest run.
    Raises ComfyError when the recognizer fails.
    """
    files = [Path(c["clip"]) for c in candidates]
    for i, heard in recognizer.transcripts(files, engine=asr, comfy_url=comfy_url):
        c = candidates[i]
        score = transcript_match(brief["sample_text"], heard)
        c.update(transcript=heard, score=round(score, 3))
        run = full_scale_run(files[i])
        if run >= CLIP_RUN:
            c["clipped"] = run
        log(f"   {cid} seed {c['seed']}: match {score:.2f}{'  clipped' if run >= CLIP_RUN else ''}  {files[i].name}")


def file_voice(slug: str, cid: str, brief: dict, candidates: list[dict], chosen: int, engine: str = "qwen") -> dict:
    """Copies the chosen candidate into the library and returns the voice record, which is also saved.

    engine: the engine that designed the candidates. "qwen" also copies the Drama 3 voice file into the library and
        into ComfyUI's voice folder. "breeze" copies only the clip.
    """
    voices_dir = lib.serial_dir(slug) / "voices"
    voices_dir.mkdir(parents=True, exist_ok=True)
    best = candidates[chosen]
    clip_src = Path(best["clip"])
    clip_dst = voices_dir / f"{cid}{clip_src.suffix}"
    shutil.copy2(clip_src, clip_dst)
    record = {
        "id": cid, "character_id": None if cid == NARRATOR_ID else cid, "status": "auto", "engine": engine,
        "source": SOURCES[engine], "instruct": brief["instruct"], "sample_text": brief["sample_text"],
        "seed": best["seed"], "clip": clip_dst.name,
    }
    if engine == "qwen":
        voice_src = Path(best["voice_file"])
        voice_dst = voices_dir / f"{cid}.safetensors"
        shutil.copy2(voice_src, voice_dst)
        comfy_dst = VOICES_OUTPUT / slug / f"{cid}.safetensors"   # what Load Drama 3 Voice will list
        comfy_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(voice_src, comfy_dst)
        record.update(drama3_voice=voice_dst.name,
                      comfy_voice=str(comfy_dst.relative_to(VOICES_OUTPUT)).replace("\\", "/"))
    record.update(candidates=candidates, chosen=chosen, created_at=lib.now_iso())
    lib.save_yaml(voices_dir / f"{cid}.yaml", record)
    return record


def pick_voice(slug: str, cid: str, *, seed: int | None = None, approve: bool = True) -> dict:
    """Re-file a cast voice from another of its candidates (by seed), and/or mark it approved."""
    path = lib.serial_dir(slug) / "voices" / f"{cid}.yaml"
    record = lib.load_yaml(path)
    if record is None:
        raise FileNotFoundError(f"no cast voice for {cid}")
    chosen = record["chosen"]
    batch = record
    if seed is not None:
        # the current batch first, then earlier casts kept under history (their clips are still on disk)
        batches = [record] + list(reversed(record.get("history") or []))
        found = next(((b, i) for b in batches for i, c in enumerate(b["candidates"]) if c["seed"] == seed), None)
        if found is None:
            seeds = [c["seed"] for b in batches for c in b["candidates"]]
            raise ValueError(f"{cid} has no candidate with seed {seed}; seeds: {seeds}")
        batch, chosen = found
    brief = {"instruct": batch["instruct"], "sample_text": batch["sample_text"]}
    new = file_voice(slug, cid, brief, batch["candidates"], chosen, engine=batch.get("engine") or "qwen")
    new["status"] = "approved" if approve else record["status"]
    new["created_at"] = batch.get("created_at", new["created_at"])
    if batch.get("asr"):
        new["asr"] = batch["asr"]
    history = [h for h in (record.get("history") or []) if h is not batch]
    if batch is not record:
        old = {k: record.get(k) for k in BATCH_KEYS}
        old["replaced_at"] = lib.now_iso()
        old["why_replaced"] = f"re-filed seed {seed} from an earlier cast"
        history.append(old)
    new["history"] = history
    lib.save_yaml(path, new)
    bible = lib.load_bible(slug)
    if cid in bible["characters"]:
        bible["characters"][cid]["voice_status"] = new["status"]
        lib.save_bible(slug, bible)
    return new


def cast(slug: str, *, only: list[str] | None = None, candidates: int = 3, seed0: int = 1000, backend="auto",
         model="opus", effort=None, on_limit="api", comfy_url="http://127.0.0.1:8188", dry_run=False,
         recast: bool = False, instruct: str | None = None, redo_brief: bool = False, note: str | None = None,
         engine: str | None = None, asr: str | None = None, log=print) -> list[dict]:
    """Design and file voices. `recast` replaces the voices of `only` (the old record is kept under `history`);
    `instruct` overrides the voice description for a single id; `redo_brief` asks the casting pass again.
    `engine` is the designer, "qwen" or "breeze", from plan_cast_engine. `asr` is the recognizer that scores the
    candidates, "qwen3" or "parakeet", from asr.choose. Both, and ComfyUI for qwen, are checked before the casting
    pass runs. When a Breeze design fails, the voices designed before it are filed and then the error is raised."""
    serial = lib.load_serial(slug)
    bible = lib.load_bible(slug)
    if recast:
        if not only:
            raise ComfyError("--recast needs --characters: say which voices to replace")
        for cid in only:            # forget the current voice in memory only; disk changes when the new one is filed
            if cid == NARRATOR_ID:
                serial["narrator_voice_id"] = ""
            elif cid in bible["characters"]:
                bible["characters"][cid]["voice_id"] = ""
    ids = characters_needing_voices(serial, bible, only)
    deferred = deferred_characters(serial, bible) if not only else []
    if deferred:
        log(f"not yet speaking, cast later: {', '.join(deferred)}")
    if not ids:
        log("nothing to cast")
        return []
    log(f"casting {len(ids)}: {', '.join(ids)}")
    if not dry_run:
        engine = plan_cast_engine(serial, engine)
        asr = recognizer.choose(asr)
        recognizer.check_ready(asr, comfy_url)
        if engine == "qwen":
            ComfyClient(comfy_url).system_stats()
    # briefs are cached so a ComfyUI failure or a re-run does not repeat the LLM call
    briefs_path = lib.serial_dir(slug) / "voices" / "_briefs.yaml"
    briefs = lib.load_yaml(briefs_path, {}) or {}
    if redo_brief:
        for cid in ids:
            briefs.pop(cid, None)
    missing = [cid for cid in ids if cid not in briefs]
    if missing:
        briefs.update(casting_pass(serial, casting_records(slug, serial, bible, missing), backend=backend,
                                   model=model, effort=effort, on_limit=on_limit, log=log))
    if instruct:
        if len(ids) != 1:
            raise ComfyError("--instruct applies to exactly one voice; name it with --characters")
        briefs[ids[0]]["instruct"] = instruct.strip()
    if missing or instruct:
        lib.save_yaml(briefs_path, briefs)
    for cid in ids:
        b = briefs.get(cid)
        log(f"-- {cid}\n   instruct: {b['instruct'] if b else '(missing)'}\n   sample:   {b['sample_text'] if b else ''}")
    if dry_run:
        return [briefs[c] for c in ids if c in briefs]

    # Design
    log(f"   designing with {engine}, checking with {asr}")
    plan = []
    for cid in ids:
        if briefs.get(cid):
            plan.append((cid, briefs[cid]))
        else:
            log(f"   {cid}: casting pass returned no brief, skipped")
    designed, failure, client = {}, None, None
    if engine == "breeze":
        designed, failure = design_breeze(slug, plan, count=candidates, seed0=seed0, comfy_url=comfy_url, log=log)
        if failure:
            log(f"   {failure}; filing the {len(designed)} voice(s) designed before it")
            plan = [(cid, brief) for cid, brief in plan if cid in designed]
    else:
        client = ComfyClient(comfy_url, output_dir=COMFY_OUTPUT)

    # Score and file
    records = []
    for cid, brief in plan:
        if engine == "breeze":
            cands = designed[cid]
        else:
            cands = generate_candidates(client, slug=slug, cid=cid, brief=brief, count=candidates, seed0=seed0, log=log)
        score_candidates(cid, brief, cands, asr=asr, comfy_url=comfy_url, log=log)
        order = sorted(range(len(cands)), key=lambda i: (not cands[i].get("clipped"), cands[i]["score"]), reverse=True)
        chosen = order[0]
        if cands[chosen].get("clipped"):
            log(f"   {cid}: every candidate is clipped; filed anyway, flagged for review")
        elif cands[chosen]["score"] < MATCH_THRESHOLD:
            log(f"   {cid}: best transcript match among the unclipped candidates, {cands[chosen]['score']:.2f}, is "
                f"below {MATCH_THRESHOLD}; filed anyway, flagged for review")
        record_path = lib.serial_dir(slug) / "voices" / f"{cid}.yaml"
        previous = lib.load_yaml(record_path) if recast else None
        record = file_voice(slug, cid, brief, cands, chosen, engine=engine)
        record["status"] = ("auto" if cands[chosen]["score"] >= MATCH_THRESHOLD and not cands[chosen].get("clipped")
                            else "review")
        record["asr"] = asr
        if previous:
            old = {k: previous.get(k) for k in BATCH_KEYS}
            old["replaced_at"] = lib.now_iso()
            if note:
                old["why_replaced"] = note
            record["history"] = (previous.get("history") or []) + [old]
        lib.save_yaml(record_path, record)
        if cid == NARRATOR_ID:
            serial["narrator_voice_id"] = NARRATOR_ID
            lib.save_serial(slug, serial)
        else:
            bible["characters"][cid]["voice_id"] = cid
            bible["characters"][cid]["voice_status"] = record["status"]
            lib.save_bible(slug, bible)
            nar = serial.get("narration") or {}
            if nar.get("narrator_character_id") == cid and not serial.get("narrator_voice_id"):
                serial["narrator_voice_id"] = cid
                lib.save_serial(slug, serial)
        log(f"   {cid}: chose seed {cands[chosen]['seed']} (match {cands[chosen]['score']:.2f}) -> voices/{cid}.yaml")
        records.append(record)
    if failure:
        raise failure
    return records
