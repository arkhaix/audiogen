"""The measure-takes report: each voice's distribution of the take measures, and what the take check's rules flag.

Reads the evidence take_check.json holds, from either recognizer, and hears the other takes with Parakeet. It
doesn't write anything.
"""
from __future__ import annotations

from .breeze import voice_text
from .comfy import ComfyError
from .takecheck import (DRIFT_MAD_FLOOR, SIGNAL_V, VERDICTS, baselines, chapter_evidence, current, evidence,
                        judged)

MEASURES = ("level", "peak", "rate", "tail_drop", "swing", "jump", "longest_silence", "silences", "clip_run",
            "words", "errors", "errors2", "tail", "similarity", "drift")
QUANTILES = (("p5", 0.05), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("p95", 0.95))


def take_row(chapter: int, k: str, take: dict, entry: dict, line: dict, said: str,
             baseline: dict | None = None) -> dict:
    """One take's measures and verdict as a flat record.

    chapter, k: the chapter and the line.
    take: the take's breeze_takes.json entry.
    entry: its judged take_check.json entry.
    line: its script line.
    said: the text the voice was sent.
    baseline: the voice's baseline for the chapter, or None.
    Returns the record. A measure the take doesn't have evidence for is None. names_said and missed are the
    names the take says, in phones, and the ones it says with an added sound. drift is how many median absolute
    deviations the take's similarity lies from its voice's median in the chapter, below being negative, with the
    deviation at least DRIFT_MAD_FLOOR as the drift rule takes it.
    """
    asr, signal = evidence(entry)
    signal = signal if current(signal, SIGNAL_V) else {}
    runs = signal.get("silences") if isinstance(signal.get("silences"), list) else None
    active = signal.get("active")
    row = {"chapter": chapter, "line": int(k), "voice": take.get("voice"), "speaker": line.get("speaker"),
           "direction": line.get("direction") or "", "said": said, "heard": (asr or {}).get("heard"),
           "verdict": entry["verdict"], "reasons": entry["reasons"], "words": entry["words"],
           "errors": entry["sub"] + entry["del"] + entry["ins"], "sub": entry["sub"], "del": entry["del"],
           "ins": entry["ins"], "names": entry["names"], "cuts": entry["repairs"]["cuts"],
           "heard2": ((entry.get("asr2") or {}).get("heard") if isinstance(entry.get("asr2"), dict) else None)}
    for name in ("errors2", "tail", "similarity", "missed"):
        row[name] = entry.get(name)
    row["names_said"] = entry.get("said")
    middle = (baseline or {}).get("similarity")
    row["drift"] = (round((row["similarity"] - middle[0]) / max(middle[1], DRIFT_MAD_FLOOR), 1)
                    if isinstance(row["similarity"], (int, float)) and isinstance(middle, list) and len(middle) == 2
                    else None)
    for name in ("seconds", "level", "peak", "clip_run", "tail_drop", "swing", "jump"):
        row[name] = signal.get(name)
    row["rate"] = round(active / entry["words"], 3) if isinstance(active, (int, float)) and entry["words"] else None
    row["silences"] = len(runs) if runs is not None else None
    row["longest_silence"] = round(max((b - a for a, b in runs), default=0.0), 2) if runs is not None else None
    return row


def distribution(values: list[float]) -> dict | None:
    """{"n", "min", "p5", "p25", "p50", "p75", "p95", "max"} of values, or None for no values."""
    if not values:
        return None
    ordered = sorted(values)

    def at(q: float) -> float:
        pos = q * (len(ordered) - 1)
        low = int(pos)
        high = min(low + 1, len(ordered) - 1)
        return round(ordered[low] + (ordered[high] - ordered[low]) * (pos - low), 3)

    return {"n": len(ordered), "min": ordered[0], **{name: at(q) for name, q in QUANTILES}, "max": ordered[-1]}


def measure_takes(slug: str, numbers: list[int], *, log=print, stored: bool = False) -> dict:
    """The report over the Breeze takes of chapters. It doesn't write anything.

    slug: the serial.
    numbers: the chapters.
    log: receives the progress lines.
    stored: report from the evidence take_check.json holds, whichever recognizer heard it, and don't hear a take.
        Without it, a take that doesn't have current evidence from Parakeet is heard with Parakeet, on the CPU.
    Returns {"slug", "chapters", "skipped", "takes", "heard", "without_evidence", "verdicts", "reasons", "voices",
    "rows"}. voices maps each voice to its take count, its verdicts, the reasons that flag its takes and the
    distribution of each of MEASURES. reasons maps each reason to its [chapter, line] pairs. rows has one take_row
    per judged take. Each take is judged as check_takes would judge it now. Raises ComfyError when a take has to be
    heard and Parakeet cannot run here or fails.
    """
    rows, skipped, heard, missing, total = [], {}, 0, 0, 0
    for n in numbers:
        try:
            entries, takes, lines, bible, new = chapter_evidence(slug, n, engine="parakeet", comfy_url="", log=log,
                                                                 listen=not stored)
        except ComfyError as e:
            if "has no Breeze takes" not in str(e):
                raise
            skipped[n] = str(e)
            continue
        except FileNotFoundError as e:
            skipped[n] = str(e)
            continue
        heard += new
        total += len(takes)
        missing += len(takes) - len(entries)
        voices = baselines(entries, takes, lines, bible)
        for k in sorted(entries, key=int):
            baseline = voices.get(takes[k].get("voice"))
            entry = judged(entries[k], lines[int(k)], bible, baseline)
            rows.append(take_row(n, k, takes[k], entry, lines[int(k)], voice_text(lines[int(k)], bible)[0], baseline))

    by_voice: dict[str, list[dict]] = {}
    for row in rows:
        by_voice.setdefault(row["voice"] or "", []).append(row)
    reasons: dict[str, list[list[int]]] = {}
    for row in rows:
        for reason in row["reasons"]:
            reasons.setdefault(reason, []).append([row["chapter"], row["line"]])

    def count(items: list[str]) -> dict[str, int]:
        return {name: items.count(name) for name in sorted(set(items))}

    return {"slug": slug, "chapters": [n for n in numbers if n not in skipped], "skipped": skipped, "takes": total,
            "heard": heard, "without_evidence": missing,
            "verdicts": {v: sum(r["verdict"] == v for r in rows) for v in VERDICTS},
            "reasons": dict(sorted(reasons.items())),
            "voices": {voice: {"takes": len(found), "verdicts": count([r["verdict"] for r in found]),
                               "reasons": count([reason for r in found for reason in r["reasons"]]),
                               "measures": {m: distribution([r[m] for r in found if isinstance(r[m], (int, float))])
                                            for m in MEASURES}}
                       for voice, found in sorted(by_voice.items())},
            "rows": rows}


def table(report: dict) -> str:
    """The report as text: the totals, then one block per voice with a line per measure."""
    out = [f"{report['slug']}: {report['takes']} takes in {len(report['chapters'])} chapters, {report['heard']} heard "
           f"now, {report['without_evidence']} without evidence"]
    if report["skipped"]:
        out.append("skipped: " + ", ".join(f"chapter {n} ({why})" for n, why in report["skipped"].items()))
    out.append("verdicts: " + ", ".join(f"{v} {n}" for v, n in report["verdicts"].items()))
    for reason, places in report["reasons"].items():
        out.append(f"reason {reason}: {len(places)} takes: " + " ".join(f"{c}:{line}" for c, line in places[:40])
                   + (" ..." if len(places) > 40 else ""))
    head = f"  {'measure':<16}{'n':>6}" + "".join(f"{name:>9}" for name in ("min", "p5", "p25", "p50", "p75", "p95", "max"))
    for voice, found in report["voices"].items():
        flags = ", ".join(f"{r} {n}" for r, n in found["reasons"].items()) or "none"
        out += ["", f"{voice or '(no voice)'}: {found['takes']} takes, "
                    + ", ".join(f"{v} {n}" for v, n in found["verdicts"].items()) + f", reasons: {flags}", head]
        for name, d in found["measures"].items():
            if d:
                out.append(f"  {name:<16}{d['n']:>6}" + "".join(f"{d[q]:>9.2f}" for q in ("min", "p5", "p25", "p50", "p75", "p95", "max")))
    return "\n".join(out)
