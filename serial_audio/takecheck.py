"""Checks a Breeze chapter's takes against the script with a speech recognizer, Qwen3-ASR in ComfyUI or Parakeet.

Breeze TTS 2 sometimes loops, repeating a word or phrase, or drops the end of a line, and the take-length guard
catches only the gross cases. This transcribes every take, aligns the words with the script line and records the
verdicts in take_check.json beside the script, with a summary under render.check in chapter.yaml. A take with three
or more inserted or dropped words is flagged. A take with a high word error rate but fewer slips is suspect, which
is usually a name the recognizer spelled its own way. A transcript with no Latin letters is unreadable: the
recognizer picked the wrong language, so the take is unverified rather than wrong.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import library as lib
from .asr import check_ready, choose, model_name, transcripts, words
from .breeze import TAKES_FILE, speakable
from .comfy import ComfyError
from .render import apply_say_as, say_as_map

CHECK_FILE = "take_check.json"
MAX_SLIPS = 3
SUSPECT_WER = 0.34
REPORT_EVERY = 60
VERDICTS = ("ok", "suspect", "unreadable", "flagged")
LABELS = {"qwen3": "Qwen3-ASR", "parakeet": "Parakeet"}


def align(ref: list[str], hyp: list[str]) -> tuple[int, int, int]:
    """Substitutions, deletions and insertions that turn ref into hyp, from a word-level Levenshtein alignment."""
    rows = [[(j, 0, 0, j) for j in range(len(hyp) + 1)]]
    for i in range(1, len(ref) + 1):
        row = [(i, 0, i, 0)]
        for j in range(1, len(hyp) + 1):
            if ref[i - 1] == hyp[j - 1]:
                row.append(rows[i - 1][j - 1])
                continue
            s, d, n = rows[i - 1][j - 1], rows[i - 1][j], row[j - 1]
            row.append(min((s[0] + 1, s[1] + 1, s[2], s[3]), (d[0] + 1, d[1], d[2] + 1, d[3]),
                           (n[0] + 1, n[1], n[2], n[3] + 1)))
        rows.append(row)
    _, sub, dele, ins = rows[-1][-1]
    return sub, dele, ins


def classify(expected: list[str], heard_text: str) -> dict:
    """The verdict for one take.

    expected: the script line's words as the voice says them, from words(speakable(apply_say_as(text, say))).
    heard_text: the recognizer's transcript of the take.
    Returns {"heard", "words", "sub", "del", "ins", "wer", "verdict"} with a verdict from VERDICTS.
    """
    heard = words(heard_text)
    sub, dele, ins = align(expected, heard)
    wer = round((sub + dele + ins) / max(1, len(expected)), 3)
    if re.search(r"[^\W\d_]", heard_text) and not re.search(r"[A-Za-z]", heard_text):
        verdict = "unreadable"
    elif ins >= MAX_SLIPS or dele >= MAX_SLIPS:
        verdict = "flagged"
    elif wer > SUSPECT_WER and (len(expected) >= 2 or not heard):
        verdict = "suspect"
    else:
        verdict = "ok"
    return {"heard": heard_text, "words": len(expected), "sub": sub, "del": dele, "ins": ins, "wer": wer,
            "verdict": verdict}


def summarize(results: dict) -> dict:
    """The render.check record for a set of per-take results."""
    out = {"at": lib.now_iso(), "file": CHECK_FILE, "checked": len(results)}
    for verdict in VERDICTS[1:]:
        out[verdict] = sorted(int(k) for k, r in results.items() if r["verdict"] == verdict)
    return out


def describe(summary: dict) -> str:
    """One log line for a check summary."""
    if summary.get("skipped"):
        return f"take check skipped: {summary['skipped']}"
    flagged = summary.get("flagged") or []
    bits = []
    if flagged:
        bits.append(f"{len(flagged)} of {summary['checked']} takes flagged (lines {', '.join(map(str, flagged))}), listen to them")
    else:
        bits.append(f"no takes flagged in {summary['checked']}")
    if summary.get("suspect"):
        bits.append(f"{len(summary['suspect'])} suspect, usually name spellings")
    if summary.get("unreadable"):
        bits.append(f"{len(summary['unreadable'])} unreadable")
    return "take check: " + ", ".join(bits)


def check_takes(slug: str, number: int, *, comfy_url: str = "http://127.0.0.1:8188", log=print,
                redo: bool = False, asr: str | None = None) -> dict:
    """Transcribes the chapter's Breeze takes and records which ones do not say their line.

    slug: the serial.
    number: the chapter.
    comfy_url: the ComfyUI that runs Qwen3-ASR.
    log: receives the progress lines.
    redo: transcribe every take again instead of reusing the saved transcript of an unchanged take.
    asr: "qwen3" or "parakeet". None uses this machine's recognizer, from asr.choose.
    Returns the summary, which is also stored as render.check in chapter.yaml. A saved transcript is reused only
    when the take and the recognizer are unchanged. Raises ComfyError when the chapter has no Breeze takes, the
    recognizer cannot run here, a take file lies outside the ComfyUI output folder for Qwen3-ASR, or the
    recognizer fails.
    """
    # Setup
    engine = choose(asr)
    meta, _ = lib.load_chapter(slug, number)
    folder = lib.chapter_dir(slug, number)
    render = meta.get("render") if isinstance(meta.get("render"), dict) else {}
    if render.get("engine") != "breeze" or not (folder / TAKES_FILE).is_file():
        raise ComfyError(f"chapter {number} has no Breeze takes to check")
    lines = lib.load_json(folder / "script.json")["lines"]
    say = say_as_map(lib.load_bible(slug))
    takes = lib.load_json(folder / TAKES_FILE)["takes"]
    check_path = folder / CHECK_FILE
    previous = (lib.load_json(check_path).get("takes") or {}) if check_path.is_file() and not redo else {}
    results = {}
    todo = []
    for k in sorted(takes, key=int):
        take = takes[k]
        expected = words(speakable(apply_say_as(lines[int(k)]["text"], say)))
        old = previous.get(k) or {}
        same_asr = (old.get("asr") or "qwen3") == engine
        if old.get("key") == take.get("key") and same_asr and isinstance(old.get("heard"), str):
            results[k] = {"key": take["key"], "asr": engine, **classify(expected, old["heard"])}
        else:
            todo.append((k, take, expected))
    log(f"   checking {len(todo)} takes with {LABELS[engine]}"
        + (f", {len(results)} already checked" if results else "") + "...")

    # Transcribe
    if todo:
        check_ready(engine, comfy_url)
        start = last_report = time.time()
        files = [Path(take["file"]) for _, take, _ in todo]
        for done, (i, heard) in enumerate(transcripts(files, engine=engine, comfy_url=comfy_url), 1):
            k, take, expected = todo[i]
            results[k] = {"key": take["key"], "asr": engine, **classify(expected, heard)}
            now = time.time()
            if now - last_report >= REPORT_EVERY:
                last_report = now
                per = (now - start) / done
                log(f"   [{(now - start) / 60:.0f} min] checked {done}/{len(todo)}, {per:.1f} s/take, "
                    f"~{per * (len(todo) - done) / 60:.0f} min left")

    # Record
    summary = summarize(results)
    lib.save_json(check_path, {"at": summary["at"], "model": model_name(engine), "asr": engine, "takes": results})
    meta, _ = lib.load_chapter(slug, number)
    meta["render"]["check"] = summary
    lib.save_chapter_meta(slug, number, meta)
    log("   " + describe(summary))
    return summary


def check_after_render(slug: str, number: int, *, comfy_url: str, log=print, asr: str | None = None) -> dict:
    """Runs check_takes for a chapter that just rendered. Never raises: a failure is logged and recorded as skipped."""
    try:
        return check_takes(slug, number, comfy_url=comfy_url, log=log, asr=asr)
    except Exception as e:
        summary = {"at": lib.now_iso(), "skipped": str(e) if isinstance(e, ComfyError) else f"{type(e).__name__}: {e}"}
        meta, _ = lib.load_chapter(slug, number)
        if isinstance(meta.get("render"), dict):
            meta["render"]["check"] = summary
            lib.save_chapter_meta(slug, number, meta)
        log("   " + describe(summary))
        return summary
