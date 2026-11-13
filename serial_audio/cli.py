"""Command line: python -m serial_audio.cli <command> ..."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import epub as epub_mod
from . import library as lib
from . import royalroad as rr
from .llm import LLMError
from .passes import character_pass

DEFAULT_CASTING_POLICY = {
    "dedicated_voice_for": ["major", "recurring"],
    "pool_voice_for": ["minor", "one_off"],
    "narrator_reads_one_off": False,
}


def cmd_add_epub(args) -> int:
    meta, chapters = epub_mod.extract(args.epub)
    slug = args.slug or lib.slugify(meta["title"])
    folder = lib.serial_dir(slug)
    if folder.exists() and not args.force:
        print(f"{folder} exists; pass --force to overwrite chapter text", file=sys.stderr)
        return 1
    serial = lib.load_yaml(folder / "serial.yaml", {}) or {}
    serial.update({
        "slug": slug,
        "title": args.title or meta["title"],
        "author": args.author or meta["author"],
        "language": meta["language"] or "en",
        "source": {"type": "epub", "file": str(Path(args.epub).resolve()), "sha256": epub_mod.sha256(args.epub)},
        "chapters": len(chapters),
        "created_at": serial.get("created_at") or lib.now_iso(),
    })
    serial.setdefault("narration", None)
    serial.setdefault("narrator_voice_id", "")
    serial.setdefault("casting_policy", DEFAULT_CASTING_POLICY)
    lib.save_serial(slug, serial)
    for ch in chapters:
        cdir = lib.chapter_dir(slug, ch.number)
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "text.md").write_text(ch.text, encoding="utf-8")
        existing = lib.load_yaml(cdir / "chapter.yaml", {}) or {}
        existing.update({
            "number": ch.number, "part": ch.part, "part_title": ch.part_title,
            "label": ch.label, "title": ch.title, "source_file": ch.source_file,
            "word_count": len(ch.text.split()), "status": existing.get("status", "fetched"),
            "ingested_at": lib.now_iso(),
        })
        lib.save_chapter_meta(slug, ch.number, existing)
    words = sum(len(c.text.split()) for c in chapters)
    print(f"{serial['title']} by {serial['author']}: {len(chapters)} chapters, {words:,} words -> {folder}")
    for ch in chapters:
        print(f"  {ch.number:3d}  part {ch.part}  {ch.label}: {ch.title}  ({len(ch.text.split())} words)")
    return 0


def _write_chapter(slug: str, ch: rr.Chapter) -> dict:
    cdir = lib.chapter_dir(slug, ch.number)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "text.md").write_text(ch.text, encoding="utf-8")
    notes_path = cdir / "author_notes.md"
    if ch.author_notes:
        notes_path.write_text("\n\n---\n\n".join(ch.author_notes) + "\n", encoding="utf-8")
    elif notes_path.exists():
        notes_path.unlink()
    meta = lib.load_yaml(cdir / "chapter.yaml", {}) or {}
    meta.update({
        "number": ch.number, "part": ch.part, "part_title": ch.part_title, "label": ch.label, "title": ch.title,
        "source_id": ch.source_id, "source_url": ch.source_url, "published": ch.published,
        "word_count": len(ch.text.split()), "status": meta.get("status", "fetched"), "ingested_at": lib.now_iso(),
    })
    lib.save_chapter_meta(slug, ch.number, meta)
    return meta


def _save_index(slug: str, info: dict) -> None:
    lib.save_yaml(lib.serial_dir(slug) / "source_index.yaml", {
        "fiction_id": info["fiction_id"], "url": info["url"], "checked_at": lib.now_iso(),
        "volumes": info["volumes"], "chapters": info["chapters"]})


def _fetch_chapters(slug: str, index: list[dict], numbers: list[int], *, force: bool, html: str | None = None) -> int:
    """Fetch the numbered chapters from the source index, or parse `html` for a single one. Returns how many were written."""
    by_number = {c["number"]: c for c in index}
    done = 0
    for n in numbers:
        entry = by_number.get(n)
        if entry is None:
            print(f"chapter {n}: not in the source index", file=sys.stderr)
            continue
        if (lib.chapter_dir(slug, n) / "text.md").exists() and not force:
            print(f"chapter {n}: already fetched, skipping (use --force)")
            continue
        try:
            ch = rr.chapter(entry, html)
        except (rr.RoyalRoadError, OSError) as e:
            print(f"chapter {n}: {e}", file=sys.stderr)
            continue
        meta = _write_chapter(slug, ch)
        notes = f", {len(ch.author_notes)} author note(s) set aside" if ch.author_notes else ""
        print(f"  {n:4d}  {ch.label}: {ch.title}  ({meta['word_count']} words{notes})")
        done += 1
    return done


def cmd_add_royalroad(args) -> int:
    info = rr.fiction(args.url)
    slug = args.slug or lib.slugify(info["title"])
    folder = lib.serial_dir(slug)
    serial = lib.load_yaml(folder / "serial.yaml", {}) or {}
    if serial and (serial.get("source") or {}).get("type") != "royalroad":
        print(f"{folder} already holds a serial from a different source", file=sys.stderr)
        return 1
    serial.update({
        "slug": slug,
        "title": args.title or info["title"],
        "subtitle": info["subtitle"],
        "author": args.author or info["author"],
        "language": "en",
        "source": {"type": "royalroad", "url": info["url"], "fiction_id": info["fiction_id"],
                   "tags": info["tags"], "checked_at": lib.now_iso()},
        "description": info["description"],
        "chapters": len(info["chapters"]),
        "created_at": serial.get("created_at") or lib.now_iso(),
    })
    serial.setdefault("narration", None)
    serial.setdefault("narrator_voice_id", "")
    serial.setdefault("casting_policy", DEFAULT_CASTING_POLICY)
    lib.save_serial(slug, serial)
    _save_index(slug, info)
    print(f"{serial['title']} by {serial['author']}: {len(info['chapters'])} chapters on Royal Road -> {folder}")
    available = [c["number"] for c in info["chapters"]]
    numbers = [] if args.chapters == "none" else _chapter_range(args.chapters, available)
    _fetch_chapters(slug, info["chapters"], numbers, force=args.force)
    return 0


def cmd_fetch(args) -> int:
    serial = lib.load_serial(args.slug)
    source = serial.get("source") or {}
    if source.get("type") != "royalroad":
        print(f"{args.slug}: source type {source.get('type')!r} cannot be fetched", file=sys.stderr)
        return 1
    index = lib.load_yaml(lib.serial_dir(args.slug) / "source_index.yaml", {}) or {}
    html = None
    if args.html:
        if not args.chapters.isdigit():
            print("--html needs exactly one chapter number", file=sys.stderr)
            return 1
        if not index.get("chapters"):
            print(f"{args.slug}: no saved chapter index; --html needs one", file=sys.stderr)
            return 1
        html = Path(args.html).read_text(encoding="utf-8", errors="replace")
    if not (args.no_refresh or html) or not index.get("chapters"):
        info = rr.fiction(source.get("url") or str(source["fiction_id"]))
        _save_index(args.slug, info)
        index = {"chapters": info["chapters"]}
        serial["chapters"] = len(info["chapters"])
        source.update({"checked_at": lib.now_iso(), "tags": info["tags"], "url": info["url"]})
        serial["source"] = source
        lib.save_serial(args.slug, serial)
    chapters = index["chapters"]
    available = [c["number"] for c in chapters]
    have = set(lib.list_chapters(args.slug))
    if args.chapters == "none":
        numbers = []
    elif args.chapters == "new":
        numbers = [n for n in available if n not in have]
        if args.limit:
            numbers = numbers[:args.limit]
    else:
        numbers = _chapter_range(args.chapters, available)
    if not numbers:
        print(f"{args.slug}: nothing to fetch ({len(available)} chapters known, {len(have)} on disk)")
        return 0
    print(f"{args.slug}: fetching {len(numbers)} of {len(available)} known chapters")
    done = _fetch_chapters(args.slug, chapters, numbers, force=args.force, html=html)
    return 0 if done == len(numbers) else 1


def cmd_chapters(args) -> int:
    for n in lib.list_chapters(args.slug):
        meta, _ = lib.load_chapter(args.slug, n)
        present = ", ".join(meta.get("characters_present") or [])
        print(f"{n:4d}  {meta['status']:<10} {meta['label']}: {meta['title']}  [{meta['word_count']} w]  {present}")
    return 0


def _chapter_range(spec: str, available: list[int]) -> list[int]:
    if spec == "all":
        return available
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(n for n in available if int(a) <= n <= int(b))
        else:
            out.append(int(part))
    return out


def cmd_character_pass(args) -> int:
    numbers = _chapter_range(args.chapters, lib.list_chapters(args.slug))
    total = 0.0
    for n in numbers:
        meta, _ = lib.load_chapter(args.slug, n)
        if meta.get("status") not in (None, "fetched") and not args.redo:
            print(f"chapter {n}: already at status {meta['status']}, skipping (use --redo)")
            continue
        try:
            result = character_pass(args.slug, n, backend=args.backend, model=args.model, effort=args.effort,
                                    on_limit=args.on_limit)
        except LLMError as e:
            print(f"chapter {n}: {e}", file=sys.stderr)
            return 1
        d = result.data
        total += result.cost_estimate_usd or 0.0
        print(f"chapter {n}: {len(d['new_characters'])} new, {len(d['present'])} returning, "
              f"{len(d['updates'])} updates, {len(d['pronunciations'])} pronunciations; "
              f"{result.backend}/{result.model}, est ${result.cost_estimate_usd or 0:.4f}")
        for c in d["new_characters"]:
            print(f"   + {c['id']} ({c['importance']}, {c['gender']}/{c['age_group']}): {c['role']}")
        for u in d["updates"]:
            if u["new_aliases"] or u["new_facts"] or u["voice_brief_update"]:
                print(f"   ~ {u['id']}: aliases {u['new_aliases']}, {len(u['new_facts'])} facts"
                      f"{', new voice brief' if u['voice_brief_update'] else ''}")
    if len(numbers) > 1:
        print(f"estimated total ${total:.4f}")
    return 0


def cmd_reset_bible(args) -> int:
    """Throw away everything the character pass produced so it can be re-run from chapter 1."""
    folder = lib.serial_dir(args.slug)
    for name in ("characters.yaml", "pronunciations.yaml"):
        path = folder / "bible" / name
        if path.exists():
            path.unlink()
    serial = lib.load_serial(args.slug)
    serial["narration"] = None
    lib.save_serial(args.slug, serial)
    count = 0
    for n in lib.list_chapters(args.slug):
        meta, _ = lib.load_chapter(args.slug, n)
        raw = lib.chapter_dir(args.slug, n) / "character_pass.json"
        if raw.exists():
            raw.unlink()
        for key in ("characters_present", "narration", "summary"):
            meta.pop(key, None)
        if meta.get("passes"):
            meta["passes"].pop("character", None)
        if meta.get("status") == "characters":
            meta["status"] = "fetched"
            count += 1
        lib.save_chapter_meta(args.slug, n, meta)
    print(f"bible cleared; {count} chapters reset to 'fetched'")
    return 0


def cmd_cast(args) -> int:
    from .casting import cast
    from .comfy import ComfyError
    only = [s.strip() for s in args.characters.split(",")] if args.characters else None
    try:
        cast(args.slug, only=only, candidates=args.candidates, seed0=args.seed, backend=args.backend,
             model=args.model, effort=args.effort, on_limit=args.on_limit, comfy_url=args.comfy_url,
             dry_run=args.dry_run, recast=args.recast, instruct=args.instruct, redo_brief=args.redo_brief,
             note=args.note, engine=args.engine, asr=args.asr)
    except (LLMError, ComfyError) as e:
        print(f"cast: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_bios(args) -> int:
    from .casting import write_bios
    only = [s.strip() for s in args.characters.split(",") if s.strip()] if args.characters else None
    try:
        write_bios(args.slug, only=only, redo=args.redo, backend=args.backend, model=args.model, effort=args.effort,
                   on_limit=args.on_limit)
    except LLMError as e:
        print(f"bios: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_voices(args) -> int:
    folder = lib.serial_dir(args.slug) / "voices"
    for path in sorted(folder.glob("*.yaml")):
        if path.name.startswith("_"):
            continue  # _briefs.yaml is the casting-pass cache, not a voice
        v = lib.load_yaml(path)
        best = v["candidates"][v["chosen"]]
        print(f"{v['id']}: {v['status']} seed {v['seed']} match {best['score']:.2f}  clip voices/{v['clip']}")
        if args.verbose:
            print(f"    instruct: {v['instruct']}")
            print(f"    sample:   {v['sample_text']}")
            for i, c in enumerate(v["candidates"]):
                mark = "*" if i == v["chosen"] else " "
                print(f"  {mark} seed {c['seed']} match {c['score']:.2f}: {c['transcript']}")
    return 0


def cmd_screenplay(args) -> int:
    from .screenplay import screenplay_pass
    numbers = _chapter_range(args.chapters, lib.list_chapters(args.slug))
    for n in numbers:
        meta, _ = lib.load_chapter(args.slug, n)
        if meta.get("status") in ("scripted", "rendered", "published") and not args.redo:
            print(f"chapter {n}: already {meta['status']}, skipping (use --redo)")
            continue
        try:
            screenplay_pass(args.slug, n, backend=args.backend, model=args.model, effort=args.effort,
                            on_limit=args.on_limit, marks=args.marks)
        except LLMError as e:
            print(f"chapter {n}: {e}", file=sys.stderr)
            return 1
    return 0


def cmd_marks(args) -> int:
    from .screenplay import marks_pass
    for n in _chapter_range(args.chapters, lib.list_chapters(args.slug)):
        if not (lib.chapter_dir(args.slug, n) / "script.json").exists():
            print(f"chapter {n}: no script, skipping")
            continue
        try:
            marks_pass(args.slug, n, backend=args.backend, model=args.model, effort=args.effort, on_limit=args.on_limit)
        except LLMError as e:
            print(f"chapter {n}: {e}", file=sys.stderr)
            return 1
    return 0


def cmd_audit_scripts(args) -> int:
    from .screenplay import MAX_ADDED_RUN, MAX_DROPPED_RUN, long_runs
    numbers = _chapter_range(args.chapters, lib.list_chapters(args.slug))
    rows, scripts = [], 0
    for n in numbers:
        path = lib.chapter_dir(args.slug, n) / "script.json"
        if not path.exists():
            continue
        scripts += 1
        _, text = lib.load_chapter(args.slug, n)
        rows += [{"chapter": n, **run} for run in long_runs(text, lib.load_json(path)["lines"])]
    print(f"{args.slug}: {len({r['chapter'] for r in rows})} of {scripts} scripts have a run of more than "
          f"{MAX_DROPPED_RUN} dropped or {MAX_ADDED_RUN} added words")
    if rows:
        print(f"{'chapter':>8}  {'kind':<8}{'words':>6}{'line':>6}  quote")
    for r in rows:
        print(f"{r['chapter']:>8}  {r['kind']:<8}{r['words']:>6}{r['line']:>6}  \"{r['quote']}\"")
    return 0


def cmd_render(args) -> int:
    from .comfy import ComfyError
    from .render import render
    numbers = _chapter_range(args.chapters, lib.list_chapters(args.slug))
    for n in numbers:
        try:
            render(args.slug, n, seed=args.seed, comfy_url=args.comfy_url, skip_generate=args.decode_only,
                   engine=args.engine, runner=args.runner)
        except ComfyError as e:
            print(f"chapter {n}: {e}", file=sys.stderr)
            return 1
    return 0


def cmd_check_takes(args) -> int:
    from .comfy import ComfyError
    from .takecheck import check_takes
    numbers = _chapter_range(args.chapters, lib.list_chapters(args.slug))
    for n in numbers:
        try:
            check_takes(args.slug, n, comfy_url=args.comfy_url, redo=args.redo, asr=args.asr)
        except ComfyError as e:
            print(f"chapter {n}: {e}", file=sys.stderr)
            return 1
    return 0


def cmd_measure_takes(args) -> int:
    import json
    from .comfy import ComfyError
    from .take_report import measure_takes, table
    numbers = _chapter_range(args.chapters, lib.list_chapters(args.slug))
    quiet = (lambda *a, **k: print(*a, file=sys.stderr, **k)) if args.json else print
    try:
        report = measure_takes(args.slug, numbers, log=quiet, stored=args.stored)
    except ComfyError as e:
        print(f"measure-takes: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False) if args.json else table(report))
    return 0


def cmd_backlog(args) -> int:
    import json
    from .breeze import render_changes
    numbers = _chapter_range(args.chapters, lib.list_chapters(args.slug))
    rows, skipped = [], {}
    for n in numbers:
        try:
            rows.append({"chapter": n, **render_changes(args.slug, n)})
        except Exception as e:
            skipped[n] = f"{type(e).__name__}: {e}"
    found = [r for r in rows if r["make"] or r["retake"] or r["join"]]
    if args.json:
        print(json.dumps({"slug": args.slug, "chapters": rows if args.all else found, "skipped": skipped}))
        return 0
    print(f"{args.slug}: a render would change {len(found)} of {len(rows)} chapters")
    print(f"{'chapter':>8}{'spoken':>8}{'make':>6}{'retake':>8}{'join':>6}{'unheard':>9}  last render")
    for r in rows if args.all else found:
        print(f"{r['chapter']:>8}{r['spoken']:>8}{r['make']:>6}{r['retake']:>8}{'yes' if r['join'] else '':>6}"
              f"{r['unheard']:>9}  {r['engine'] or 'none'}")
    unheard = sum(1 for r in rows if r["unheard"])
    if unheard:
        print(f"{unheard} chapters have takes without evidence of the current versions. A check-takes of a chapter "
              f"hears them and may flag some.")
    if skipped:
        print("skipped: " + ", ".join(f"chapter {n} ({why})" for n, why in skipped.items()))
    return 0


def cmd_pick_voice(args) -> int:
    from .casting import pick_voice
    try:
        record = pick_voice(args.slug, args.character, seed=args.seed, approve=not args.no_approve)
    except (FileNotFoundError, ValueError) as e:
        print(f"pick-voice: {e}", file=sys.stderr)
        return 1
    print(f"{args.character}: seed {record['seed']} filed, status {record['status']}")
    if record.get("engine") == "qwen" and not record.get("drama3_voice"):
        print("   filed without its Drama 3 voice file, which this machine does not hold, so only Breeze can render "
              "this voice")
    return 0


def cmd_bible(args) -> int:
    bible = lib.load_bible(args.slug)
    for cid, c in bible["characters"].items():
        chapters = c.get("chapters_present") or []
        print(f"{cid}: {c['name']} [{c['importance']}, {c['gender']}/{c['age_group']}] "
              f"chapters {chapters[0] if chapters else '?'}-{chapters[-1] if chapters else '?'} ({len(chapters)}) "
              f"voice={c.get('voice_id') or '-'}")
        if args.verbose:
            print(f"    aliases: {', '.join(c.get('aliases') or [])}")
            print(f"    role: {c['role']}")
            if c.get("bio"):
                print(f"    bio: {c['bio']}")
            print(f"    voice: {c['voice_brief']}")
    if bible["pronunciations"]:
        print("pronunciations: " + ", ".join(
            f"{t} ({p.get('respelling') or '-'}{', say ' + p['say'] if p.get('say') else ''})"
            for t, p in bible["pronunciations"].items() if isinstance(p, dict)))
    return 0


def cmd_say_as(args) -> int:
    from .pronounce import set_say_as
    try:
        entry = set_say_as(args.slug, args.term, args.spoken, note=args.note, clear=args.clear)
    except ValueError as e:
        print(f"say-as: {e}", file=sys.stderr)
        return 2
    if entry is None:
        print(f"{args.term}: override removed, and the entry with it")
    elif args.clear:
        print(f"{args.term}: override removed, read as written")
    else:
        print(f"{args.term}: read as {entry['say']!r}")
    return 0


def cmd_say_options(args) -> int:
    from .comfy import ComfyError
    from .pronounce import add_option, options_pass, render_options
    try:
        had = {}
        if args.add is not None:
            added = add_option(args.slug, args.term, args.add)
            had = {c.get("say"): c.get("clip") for c in added["candidates"] if isinstance(c, dict)}
        elif not args.render_only:
            options_pass(args.slug, args.term, backend=args.backend, model=args.model, effort=args.effort,
                         on_limit=args.on_limit)
        if not args.dry_run:
            options = render_options(args.slug, args.term, comfy_url=args.comfy_url, missing=args.add is not None)
            said = [c for c in options["candidates"] if c.get("clip") and c["clip"] != had.get(c.get("say"))]
            print(f"{args.term}: {len(said)} of {len(options['candidates'])} options said by {options.get('voice')}")
    except (ValueError, FileNotFoundError) as e:
        print(f"say-options: {e}", file=sys.stderr)
        return 2
    except (LLMError, ComfyError) as e:
        print(f"say-options: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_voice_effect(args) -> int:
    from .render import set_voice_effect
    if bool(args.effect) == args.clear:
        print("voice-effect: name an effect, or pass --clear", file=sys.stderr)
        return 2
    try:
        spec = set_voice_effect(args.slug, args.voice, None if args.clear else args.effect, args.level)
    except (FileNotFoundError, ValueError) as e:
        print(f"voice-effect: {e}", file=sys.stderr)
        return 2
    if spec is None:
        print(f"{args.voice}: no effect")
    else:
        print(f"{args.voice}: {spec['effect']} effect" + (f" at voice level {spec['voice']}" if "voice" in spec else ""))
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):   # UTF-8 and line-buffered even when redirected to a log file
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    ap = argparse.ArgumentParser(prog="serial_audio")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-epub", help="ingest an EPUB as a serial")
    p.add_argument("epub")
    p.add_argument("--slug")
    p.add_argument("--title")
    p.add_argument("--author")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_add_epub)

    p = sub.add_parser("add-royalroad", help="register a Royal Road fiction as a serial and fetch chapters")
    p.add_argument("url", help="fiction URL or id")
    p.add_argument("--slug")
    p.add_argument("--title")
    p.add_argument("--author")
    p.add_argument("--chapters", default="1", help="which chapters to fetch now: 1, 1-5, all, none (default 1)")
    p.add_argument("--force", action="store_true", help="re-fetch chapter text that is already on disk")
    p.set_defaults(fn=cmd_add_royalroad)

    p = sub.add_parser("fetch", help="refresh the chapter index from the source and fetch chapters")
    p.add_argument("slug")
    p.add_argument("chapters", nargs="?", default="new", help="new (default), 1, 1-5, all")
    p.add_argument("--limit", type=int, help="with 'new': fetch at most this many")
    p.add_argument("--no-refresh", action="store_true", help="use the saved index without asking the site")
    p.add_argument("--html", help="parse this saved chapter page instead of downloading it (one chapter; uses the saved index)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("chapters", help="list a serial's chapters and their status")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_chapters)

    p = sub.add_parser("character-pass", help="run the character pass over chapters (e.g. 1, 1-5, all)")
    p.add_argument("slug")
    p.add_argument("chapters")
    p.add_argument("--backend", choices=["auto", "subscription", "api"], default="auto")
    p.add_argument("--model", choices=["opus", "sonnet", "haiku"], default="opus")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--on-limit", choices=["api", "wait", "fail"], default="api",
                   help="what to do when the subscription limit is hit (auto backend only)")
    p.add_argument("--redo", action="store_true", help="re-run even if the chapter already has a pass")
    p.set_defaults(fn=cmd_character_pass)

    p = sub.add_parser("cast", help="design, check, and file a voice for each character that needs one")
    p.add_argument("slug")
    p.add_argument("--characters", help="comma-separated ids to cast (default: every uncast major/recurring)")
    p.add_argument("--candidates", type=int, default=3)
    p.add_argument("--seed", type=int, default=1000, help="first seed; candidates use seed, seed+1, ...")
    p.add_argument("--backend", choices=["auto", "subscription", "api"], default="auto")
    p.add_argument("--model", choices=["opus", "sonnet", "haiku"], default="opus")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--on-limit", choices=["api", "wait", "fail"], default="api")
    p.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    p.add_argument("--dry-run", action="store_true", help="run the casting pass and print briefs; no audio")
    p.add_argument("--recast", action="store_true", help="replace the voices named in --characters (old one kept in history)")
    p.add_argument("--instruct", help="use this voice description instead of the brief's (one character only)")
    p.add_argument("--redo-brief", action="store_true", help="ask the casting pass for a fresh brief first")
    p.add_argument("--note", help="why the voice is being replaced, recorded with the old one")
    p.add_argument("--engine", choices=["qwen", "breeze"],
                   help="qwen: Qwen3-TTS Voice Design and a Drama 3 voice, in ComfyUI. breeze: Breeze TTS 2 voice "
                        "design in breeze-server, no Drama 3 voice. Default: serial.yaml cast_engine, else breeze")
    p.add_argument("--asr", choices=["qwen3", "parakeet"],
                   help="the recognizer that scores the candidates. Default: SERIAL_AUDIO_ASR, else parakeet when "
                        "its model is installed, else qwen3")
    p.set_defaults(fn=cmd_cast)

    p = sub.add_parser("bios", help="write the casting bio of each voiced character that doesn't have one: who "
                                    "they are, without the plot")
    p.add_argument("slug")
    p.add_argument("--characters", help="comma-separated ids (default: every character that has a voice)")
    p.add_argument("--redo", action="store_true", help="write a bio again where the character already has one")
    p.add_argument("--backend", choices=["auto", "subscription", "api"], default="auto")
    p.add_argument("--model", choices=["opus", "sonnet", "haiku"], default="opus")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--on-limit", choices=["api", "wait", "fail"], default="api")
    p.set_defaults(fn=cmd_bios)

    p = sub.add_parser("screenplay", help="run the screenplay pass over chapters (text -> script.json)")
    p.add_argument("slug")
    p.add_argument("chapters")
    p.add_argument("--backend", choices=["auto", "subscription", "api"], default="auto")
    p.add_argument("--model", choices=["opus", "sonnet", "haiku"], default="opus")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--on-limit", choices=["api", "wait", "fail"], default="api")
    p.add_argument("--redo", action="store_true")
    p.add_argument("--marks", action="store_true", help="run the marks pass on each script too")
    p.set_defaults(fn=cmd_screenplay)

    p = sub.add_parser("marks", help="write the emphasis, focus, heteronyms, emotion, energy and pace of each line "
                                     "into the scripts of chapters. The lines and their takes stay as they are")
    p.add_argument("slug")
    p.add_argument("chapters")
    p.add_argument("--backend", choices=["auto", "subscription", "api"], default="auto")
    p.add_argument("--model", choices=["opus", "sonnet", "haiku"], default="opus")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--on-limit", choices=["api", "wait", "fail"], default="api")
    p.set_defaults(fn=cmd_marks)

    p = sub.add_parser("audit-scripts",
                       help="list the scripts that leave out or add a run of words against their chapter's text. "
                            "Read-only")
    p.add_argument("slug")
    p.add_argument("chapters", nargs="?", default="all")
    p.set_defaults(fn=cmd_audit_scripts)

    p = sub.add_parser("render", help="render scripted chapters with Breeze TTS 2 or Drama 3")
    p.add_argument("slug")
    p.add_argument("chapters")
    p.add_argument("--seed", type=int,
                   help="default: the seed of the chapter's last Breeze render, so its saved takes are reused, else 1")
    p.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    p.add_argument("--engine", choices=["drama3", "breeze"], help="default: serial.yaml render_engine, else breeze")
    p.add_argument("--runner", choices=["cpp", "comfyui"],
                   help="Breeze only. cpp starts Breeze-TTS-2.cpp's breeze-server on the GPU; comfyui needs ComfyUI "
                        "started with --highvram. Default: serial.yaml breeze_runner, else cpp")
    p.add_argument("--decode-only", action="store_true",
                   help="skip generation: decode the saved Drama 3 codes, or join the saved Breeze takes, again")
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("check-takes",
                       help="transcribe a Breeze chapter's takes and flag the ones that do not say their line")
    p.add_argument("slug")
    p.add_argument("chapters")
    p.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    p.add_argument("--redo", action="store_true", help="transcribe every take again instead of reusing saved transcripts")
    p.add_argument("--asr", choices=["qwen3", "parakeet"],
                   help="qwen3: Qwen3-ASR in ComfyUI. parakeet: Parakeet TDT 0.6B v2 on the CPU. Default: "
                        "SERIAL_AUDIO_ASR, else parakeet when its model is installed, else qwen3")
    p.set_defaults(fn=cmd_check_takes)

    p = sub.add_parser("measure-takes",
                       help="print each voice's distribution of the take measures over chapters and what the take "
                            "check's rules flag. It hears with Parakeet the takes that don't have saved evidence, "
                            "and it doesn't write anything")
    p.add_argument("slug")
    p.add_argument("chapters")
    p.add_argument("--stored", action="store_true",
                   help="report from the evidence take_check.json holds, from either recognizer, and don't hear a take")
    p.add_argument("--json", action="store_true",
                   help="print the report as JSON, with one row per take, in place of the table")
    p.set_defaults(fn=cmd_measure_takes)

    p = sub.add_parser("backlog",
                       help="list the chapters a render would change: the takes it would make or take again, and "
                            "whether it would join audio the chapter doesn't have. Read-only")
    p.add_argument("slug")
    p.add_argument("chapters", nargs="?", default="all")
    p.add_argument("--all", action="store_true", help="list every chapter, also the ones a render would leave as they are")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_backlog)

    p = sub.add_parser("pick-voice", help="after listening, file a different candidate (or approve the current one)")
    p.add_argument("slug")
    p.add_argument("character")
    p.add_argument("--seed", type=int, help="candidate seed to use (default: keep the current choice)")
    p.add_argument("--no-approve", action="store_true", help="leave the status as it is instead of marking approved")
    p.set_defaults(fn=cmd_pick_voice)

    p = sub.add_parser("voices", help="list the cast voices")
    p.add_argument("slug")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_voices)

    p = sub.add_parser("reset-bible", help="delete the bible and character-pass outputs so the pass can be re-run")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_reset_bible)

    p = sub.add_parser("bible", help="print the character bible")
    p.add_argument("slug")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_bible)

    p = sub.add_parser("say-as", help="set, change or clear the spelling a voice reads for a term")
    p.add_argument("slug")
    p.add_argument("term", help="the word or name as the text spells it, matched whole-word and case-sensitive")
    p.add_argument("spoken", nargs="?", help="the spelling the voice reads")
    p.add_argument("--note", help="replace the entry's note, or remove it with an empty value")
    p.add_argument("--clear", action="store_true", help="remove the override")
    p.set_defaults(fn=cmd_say_as)

    p = sub.add_parser("say-options", help="propose respellings of a term and have its line's voice say each one. "
                                           "It doesn't file any of them")
    p.add_argument("slug")
    p.add_argument("term", help="the word or name as the text spells it")
    p.add_argument("--backend", choices=["auto", "subscription", "api"], default="auto")
    p.add_argument("--model", choices=["opus", "sonnet", "haiku"], default="opus")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--on-limit", choices=["api", "wait", "fail"], default="api")
    p.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    stage = p.add_mutually_exclusive_group()
    stage.add_argument("--dry-run", action="store_true", help="propose and store the respellings, and say none")
    stage.add_argument("--render-only", action="store_true", help="say the stored respellings again without a new pass")
    stage.add_argument("--add", metavar="SPELLING", help="add this spelling to the options without a pass, and say "
                                                         "the options that don't have a take yet")
    p.set_defaults(fn=cmd_say_options)

    p = sub.add_parser("voice-effect", help="set or remove the effect that later renders apply to one voice")
    p.add_argument("slug")
    p.add_argument("voice", help="the voice id")
    p.add_argument("effect", nargs="?", help="the effect: whisper")
    p.add_argument("--level", type=float,
                   help="whisper: how much of the dry voice stays under the effect, 0 to 1 (default 0.35)")
    p.add_argument("--clear", action="store_true", help="remove the voice's effect")
    p.set_defaults(fn=cmd_voice_effect)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
