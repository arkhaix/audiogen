"""Render a scripted chapter with Drama 3 through ComfyUI, or hand it to breeze.py for Breeze TTS 2.

Two ComfyUI runs with the card freed between them: generate the codes with the cast voices and save
them, then decode the saved codes to audio. Splitting keeps the Drama 3 backbone and the codec from
competing for an 8 GB card at decode time.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from . import library as lib
from . import machine
from .comfy import ComfyClient, ComfyError

COMFY_OUTPUT = machine.COMFY_OUTPUT
COMFY_PYTHON = machine.AUDIO_PYTHON
FX_SCRIPT = Path(__file__).with_name("fx.py")
AUDIO_SUFFIX = ".opus"
# must match fx.py EFFECTS
VOICE_EFFECTS = {"whisper"}
VOICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
MAX_SLOTS = 16
GENERATE_TIMEOUT = 6 * 3600


def plan_slots(serial: dict, bible: dict, lines: list[dict]) -> tuple[dict[str, int], dict[int, str], list[str]]:
    """speaker -> slot, slot -> voice id, and notes. Narrator is slot 0; unvoiced speakers are read by the narrator."""
    chars = bible.get("characters") or {}
    narrator_voice = serial.get("narrator_voice_id") or ""
    if not narrator_voice:
        raise ComfyError("the serial has no narrator voice (serial.yaml narrator_voice_id); cast one first")
    speakers = []
    for line in lines:
        if line["speaker"] not in speakers:
            speakers.append(line["speaker"])
    speaker_slot: dict[str, int] = {"narrator": 0}
    slot_voice: dict[int, str] = {0: narrator_voice}
    notes = []
    for sp in speakers:
        if sp == "narrator":
            continue
        voice = (chars.get(sp) or {}).get("voice_id") or ""
        if voice == narrator_voice:
            speaker_slot[sp] = 0           # first-person narrator speaking as himself
            continue
        if not voice:
            speaker_slot[sp] = 0           # no voice yet: the narrator reads the line, with its direction
            notes.append(f"{sp} has no voice; narrator reads their lines")
            continue
        existing = next((s for s, v in slot_voice.items() if v == voice), None)
        if existing is not None:
            speaker_slot[sp] = existing
            continue
        slot = len(slot_voice)
        if slot >= MAX_SLOTS:
            raise ComfyError(f"more than {MAX_SLOTS} voices in one chapter; segmenting is not implemented yet")
        slot_voice[slot] = voice
        speaker_slot[sp] = slot
    return speaker_slot, slot_voice, notes


def slot_effects(serial: dict, slot_voice: dict[int, str]) -> dict[int, dict]:
    """slot -> effect spec for the voices that serial.yaml voice_effects names. Raises on an unknown effect."""
    wanted = serial.get("voice_effects") or {}
    for voice, spec in wanted.items():
        if (spec or {}).get("effect") not in VOICE_EFFECTS:
            raise ComfyError(f"serial.yaml voice_effects.{voice}: unknown effect {(spec or {}).get('effect')!r}; "
                             f"known: {sorted(VOICE_EFFECTS)}")
    return {slot: dict(wanted[voice]) for slot, voice in slot_voice.items() if voice in wanted}


def set_voice_effect(slug: str, voice: str, effect: str | None, level: float | None = None) -> dict | None:
    """Sets or removes the effect that serial.yaml voice_effects gives one voice in every later render.

    slug: the serial.
    voice: the voice id. Setting an effect needs a cast voice of that id.
    effect: an effect in VOICE_EFFECTS, or None to remove the voice's effect.
    level: whisper only, how much of the dry voice stays under the effect, from 0 to 1. None stores no level, so the
        render uses 0.35.
    Returns the voice's effect spec, or None when it has none. Removing the effect of a voice without one leaves
    serial.yaml as it was. Raises ValueError on a malformed voice id, an unknown effect, a level outside 0 to 1, or
    a voice with no cast record. Raises FileNotFoundError when the serial does not exist.
    """
    if not VOICE_ID_RE.match(voice):
        raise ValueError(f"bad voice id {voice!r}")
    serial = lib.load_serial(slug)
    effects = dict(serial.get("voice_effects") or {})
    if effect is None:
        if voice not in effects:
            return None
        effects.pop(voice)
    else:
        if effect not in VOICE_EFFECTS:
            raise ValueError(f"unknown effect {effect!r}; known: {', '.join(sorted(VOICE_EFFECTS))}")
        if not (lib.serial_dir(slug) / "voices" / f"{voice}.yaml").exists():
            raise ValueError(f"{slug} has no cast voice {voice!r}")
        spec = {"effect": effect}
        if level is not None:
            if not 0 <= level <= 1:
                raise ValueError(f"the level is from 0 to 1, not {level}")
            spec["voice"] = level
        effects[voice] = spec
    if effects:
        serial["voice_effects"] = effects
    else:
        serial.pop("voice_effects", None)
    lib.save_serial(slug, serial)
    return effects.get(voice)


def encode_audio(src: Path, dst: Path) -> None:
    """Writes src to dst in the format dst's suffix names, through fx.py. Raises ComfyError when that fails."""
    done = subprocess.run([str(COMFY_PYTHON), "-I", str(FX_SCRIPT), "--encode", str(src), str(dst)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise ComfyError(f"encoding {src.name} failed: {(done.stderr or done.stdout).strip()[-2000:]}")


def apply_effects(*, raw: Path, codes: Path, effects: dict[int, dict], output: Path, job_path: Path) -> dict:
    """Writes `output` from the raw decode with `effects` applied and pauses trimmed. Returns fx.py's summary."""
    job = {"raw": str(raw), "codes": str(codes), "output": str(output), "effects": {str(s): e for s, e in effects.items()}}
    job_path.write_text(json.dumps(job, indent=1), encoding="utf-8")
    done = subprocess.run([str(COMFY_PYTHON), "-I", str(FX_SCRIPT), str(job_path)], capture_output=True, text=True)
    if done.returncode != 0:
        raise ComfyError(f"voice effects failed: {(done.stderr or done.stdout).strip()[-2000:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def say_as_map(bible: dict) -> dict[str, str]:
    """Term -> the spelling the voice reads, from the bible's pronunciation entries that carry a `say`."""
    return {term: entry["say"] for term, entry in (bible.get("pronunciations") or {}).items()
            if isinstance(entry, dict) and entry.get("say")}


def apply_say_as(text: str, say: dict[str, str]) -> str:
    """text with every whole-word occurrence of a term replaced by its spoken spelling, in one pass.

    A longer term wins over a shorter one it contains, and a replacement is never matched again.
    """
    if not say:
        return text
    pattern = "|".join(re.escape(term) for term in sorted(say, key=len, reverse=True))
    return re.sub(rf"(?<!\w)(?:{pattern})(?!\w)", lambda m: say[m.group(0)], text)


def drama3_text(lines: list[dict], speaker_slot: dict[str, int]) -> str:
    out = []
    for line in lines:
        text = " ".join(line["text"].split())
        direction = " ".join((line.get("direction") or "").split())
        prefix = f"[{direction}] " if direction else ""
        out.append(f"<|speaker:{speaker_slot[line['speaker']]}|> {prefix}{text}")
    return "\n".join(out)


def _combo_value(client: ComfyClient, node: str, input_name: str, wanted: str) -> str:
    """The exact string ComfyUI lists for a file (it may use backslashes), or raise."""
    info = client._get(f"/object_info/{node}")[node]
    spec = info["input"]["required"][input_name]
    # v1 nodes list the options directly; v3 (io.Schema) nodes report ["COMBO", {"options": [...]}]
    options = spec[1].get("options", []) if spec[0] == "COMBO" else spec[0]
    norm = wanted.replace("\\", "/")
    for opt in options:
        if opt.replace("\\", "/") == norm:
            return opt
    raise ComfyError(f"{node}: {wanted!r} is not in its list (refresh? {len(options)} entries)")


def generate_graph(client: ComfyClient, *, text: str, slot_voice: dict[int, str], slug: str, seed: int,
                   codes_name: str, model_name: str = "drama3_int8experts_bf16.safetensors") -> dict:
    graph = {
        "model": {"class_type": "Drama3Loader", "inputs": {
            "model_name": model_name, "experts": "in RAM", "expert_cache_gb": 0, "second_gpu": "none", "split_layer": 0}},
    }
    gen_inputs = {"model": ["model", 0], "text": text, "seed": seed, "temperature": 1.0, "top_p": 0.9, "top_k": 0,
                  "max_frames": 600, "split_long_text": True, "speakers": "one take per line"}
    for slot, voice in sorted(slot_voice.items()):
        node = f"voice{slot}"
        graph[node] = {"class_type": "Drama3LoadVoice", "inputs": {
            "voice": _combo_value(client, "Drama3LoadVoice", "voice", f"{slug}/{voice}.safetensors")}}
        gen_inputs[f"voices.voice{slot}"] = [node, 0]
    graph["generate"] = {"class_type": "Drama3Generate", "inputs": gen_inputs}
    graph["save_codes"] = {"class_type": "Drama3SaveCodes", "inputs": {"codes": ["generate", 0], "name": codes_name}}
    return graph


def decode_graph(client: ComfyClient, *, codes_file: str, audio_prefix: str, shorten_long_pauses: bool = True,
                 codec_name: str = "drama3_codec.safetensors") -> dict:
    return {
        "codes": {"class_type": "Drama3LoadCodes", "inputs": {"codes": _combo_value(client, "Drama3LoadCodes", "codes", codes_file)}},
        "codec": {"class_type": "Drama3CodecLoader", "inputs": {"codec_name": codec_name}},
        "decode": {"class_type": "Drama3Decode", "inputs": {"codec": ["codec", 0], "codes": ["codes", 0],
                                                            "shorten_long_pauses": shorten_long_pauses}},
        "save": {"class_type": "SaveAudio", "inputs": {"audio": ["decode", 0], "filename_prefix": audio_prefix}},
    }


def _newest(folder: Path, pattern: str, seen: set[Path]) -> Path | None:
    files = [p for p in folder.glob(pattern) if p not in seen]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def stage_drama3_voices(slug: str, voices: set[str]) -> list[str]:
    """Copies each voice's Drama 3 voice file from the library to where Load Drama 3 Voice lists it.

    slug: the serial.
    voices: the voice ids a render uses.
    Returns the voices whose file in ComfyUI's voice folder was missing or differed and is now the library's. A
    voice whose record names no Drama 3 voice file is skipped.
    """
    vdir = lib.serial_dir(slug) / "voices"
    staged = []
    for voice in sorted(voices):
        name = (lib.load_yaml(vdir / f"{voice}.yaml") or {}).get("drama3_voice")
        src = vdir / name if name else None
        if src is None or not src.is_file():
            continue
        dst = COMFY_OUTPUT / "drama3_voices" / slug / f"{voice}.safetensors"
        if not dst.is_file() or dst.read_bytes() != src.read_bytes():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            staged.append(voice)
    return staged


ENGINES = ("drama3", "breeze")


def plan_engine(serial: dict, last: dict | None, *, skip_generate: bool = False, engine: str | None = None,
                runner: str | None = None) -> tuple[str, str | None]:
    """The engine and Breeze runner a render would use.

    serial: the serial.yaml record.
    last: the chapter's render record, or None.
    skip_generate: reuse the saved generation, which keeps the engine and runner that made it.
    engine: "drama3" or "breeze" to force one.
    runner: "cpp" or "comfyui" to force one. Breeze only.
    Returns (engine, runner). The runner is None for Drama 3. Raises ComfyError on an unknown engine or runner.
    """
    if engine is None and skip_generate and last:
        engine = last.get("engine") or "drama3"
    engine = engine or serial.get("render_engine") or "breeze"
    if engine not in ENGINES:
        raise ComfyError(f"unknown render engine {engine!r}; known: {', '.join(ENGINES)}")
    if engine != "breeze":
        return engine, None
    from .breeze import choose_runner
    return engine, choose_runner(serial, last, join_only=skip_generate, runner=runner)


def render(slug: str, number: int, *, seed: int | None = None, comfy_url: str = "http://127.0.0.1:8188", log=print,
           skip_generate: bool = False, engine: str | None = None, runner: str | None = None) -> dict:
    """Renders a scripted chapter to audio/chNNNN.opus and marks it rendered.

    slug: the serial.
    number: the chapter.
    seed: the generation seed. Breeze derives each voice's seed from it. None is 1 for Drama 3. For Breeze it is
        the seed of the chapter's last Breeze render, else 1.
    comfy_url: the ComfyUI that renders.
    log: receives the progress lines.
    skip_generate: reuse the saved generation, decoding Drama 3 codes or joining Breeze takes again.
    engine: "drama3" or "breeze". None reuses the chapter's last engine when skip_generate is set, and otherwise
        uses serial.yaml render_engine, else Breeze.
    runner: Breeze only, "cpp" or "comfyui". None uses the runner the saved takes were made with when skip_generate
        is set, else serial.yaml breeze_runner, else cpp.
    Returns the chapter's render record. Raises ComfyError when the render cannot run or fails.
    """
    serial = lib.load_serial(slug)
    meta, _ = lib.load_chapter(slug, number)
    previous = meta.get("render") if isinstance(meta.get("render"), dict) else None
    engine, runner = plan_engine(serial, previous, skip_generate=skip_generate, engine=engine, runner=runner)
    if engine == "breeze":
        from .breeze import render_breeze
        return render_breeze(slug, number, seed=seed, comfy_url=comfy_url, log=log, join_only=skip_generate,
                             runner=runner)
    seed = 1 if seed is None else seed
    bible = lib.load_bible(slug)
    script_path = lib.chapter_dir(slug, number) / "script.json"
    if not script_path.exists():
        raise ComfyError(f"chapter {number} has no script.json; run the screenplay pass first")
    script = lib.load_json(script_path)
    lines = script["lines"]
    speaker_slot, slot_voice, notes = plan_slots(serial, bible, lines)
    if not skip_generate:
        voices_dir = lib.serial_dir(slug) / "voices"
        no_voice = sorted(v for v in set(slot_voice.values())
                          if not (lib.load_yaml(voices_dir / f"{v}.yaml") or {}).get("drama3_voice"))
        if no_voice:
            raise ComfyError(f"voices {', '.join(no_voice)} have no Drama 3 voice file, which a cast with --engine "
                             "breeze does not make; render with --engine breeze, or recast them with --engine qwen")
        # a voice picked on the app's machine is filed only in the library
        staged = stage_drama3_voices(slug, set(slot_voice.values()))
        if staged:
            log(f"   copied the Drama 3 voice of {', '.join(staged)} from the library to ComfyUI")
    effects = slot_effects(serial, slot_voice)
    say = say_as_map(bible)
    text = drama3_text([{**line, "text": apply_say_as(line["text"], say)} for line in lines], speaker_slot)
    folder = lib.chapter_dir(slug, number)
    (folder / "script.drama3.txt").write_text(text + "\n", encoding="utf-8")
    for n in notes:
        log(f"   {n}")
    log(f"render: {slug} chapter {number}: {len(lines)} lines, {len(slot_voice)} voice slots "
        f"{ {s: v for s, v in slot_voice.items()} }")

    client = ComfyClient(comfy_url, output_dir=COMFY_OUTPUT)
    client.system_stats()
    codes_dir = COMFY_OUTPUT / "drama3_codes" / slug
    codes_dir.mkdir(parents=True, exist_ok=True)
    stem = f"ch{number:04d}"
    render_info = meta.get("render") or {}

    if skip_generate:
        codes_file = render_info.get("codes_file")
        if not codes_file:
            # the generate step finished inside ComfyUI but this process never saw it (killed, crashed)
            newest = _newest(codes_dir, f"{stem}_*.safetensors", set())
            if newest is None:
                raise ComfyError(f"no saved codes for chapter {number} in {codes_dir}; run a full render")
            codes_file = f"{slug}/{newest.name}"
        log(f"   reusing codes {codes_file}")
    else:
        seen = set(codes_dir.glob(f"{stem}_*.safetensors"))
        client.free()
        log("   generating codes (this is the slow part)...")
        client.run(generate_graph(client, text=text, slot_voice=slot_voice, slug=slug, seed=seed,
                                  codes_name=f"{slug}/{stem}"), timeout=GENERATE_TIMEOUT, log=log, report_every=600)
        newest = _newest(codes_dir, f"{stem}_*.safetensors", seen)
        if newest is None:
            raise ComfyError(f"no codes file appeared in {codes_dir}")
        codes_file = f"{slug}/{newest.name}"
        log(f"   codes saved: {codes_file}")

    client.free()
    log("   decoding...")
    entry = client.run(decode_graph(client, codes_file=codes_file, audio_prefix=f"{slug}/{stem}{'_raw' if effects else ''}",
                                    shorten_long_pauses=not effects), timeout=3600)
    items = entry.get("outputs", {}).get("save", {}).get("audio") or []
    if not items:
        raise ComfyError(f"decode produced no audio: outputs {list(entry.get('outputs', {}))}")
    src = client.output_path(items[0])
    audio_dir = lib.serial_dir(slug) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    dst = audio_dir / f"{stem}{AUDIO_SUFFIX}"
    fx_summary = None
    if effects:
        log(f"   voice effects: { {slot_voice[s]: e['effect'] for s, e in effects.items()} }")
        fx_summary = apply_effects(raw=src, codes=COMFY_OUTPUT / "drama3_codes" / Path(codes_file), effects=effects,
                                   output=dst, job_path=folder / "fx_job.json")
        log(f"   treated {fx_summary['treated_lines']} lines; {fx_summary['raw_seconds']} s raw -> {fx_summary['seconds']} s")
    else:
        encode_audio(src, dst)

    meta["render"] = {"codes_file": codes_file, "comfy_audio": str(src), "audio": f"audio/{dst.name}", "seed": seed,
                      "slots": {str(s): v for s, v in slot_voice.items()}, "notes": notes, "at": lib.now_iso()}
    if effects:
        meta["render"]["effects"] = {slot_voice[s]: e for s, e in effects.items()}
        meta["render"]["fx"] = fx_summary
    meta["status"] = "rendered"
    lib.save_chapter_meta(slug, number, meta)
    log(f"   audio: {dst}")
    return meta["render"]
