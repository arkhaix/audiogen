"""What the app knows of each kind of job, in one place.

A kind is a CLI command the app runs as a job, or SYNC, which has no command. KINDS has one entry for each: the
groups it belongs to, which the queue's rules read, how its ComfyUI prompts are marked, and the stages it runs as
on the cluster. jobs.py and nomad.py make their sets of kinds from the entries, and jobs.BUILDERS has the builder
of each kind. A rule about one kind alone still names that kind where the rule is. Standard library only, beside
the library module.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Callable

from .. import library as lib

# a job with no CLI step: its worker sends the app every take and candidate its node holds for the serial
SYNC = "sync-media"


def _one(cls: str) -> Callable:
    """The stages of a kind that runs as one dispatch of a class, with the job's own arguments."""
    return lambda job, args: [{"class": cls, "args": args}]


def _cast_engine(job: dict) -> str | None:
    """The voice design engine a cast job asks for: its --engine flag, else the serial's cast_engine, else breeze.

    An unknown cast_engine comes back as written, so the CLI refuses it before the casting pass. None when the
    serial can't be read.
    """
    args = list(job.get("args") or [])
    if "--engine" in args and args.index("--engine") + 1 < len(args):
        return args[args.index("--engine") + 1]
    from ..casting import plan_cast_engine
    from ..comfy import ComfyError
    try:
        serial = lib.load_serial(job.get("slug") or "")
    except Exception:
        return None
    try:
        return plan_cast_engine(serial)
    except ComfyError:
        return str(serial.get("cast_engine"))


def _cast(job: dict, args: list) -> list[dict]:
    if "--dry-run" in args:
        return [{"class": "llm", "args": args}]
    engine = _cast_engine(job)
    flag = [] if engine is None or "--engine" in args else ["--engine", engine]
    voices = [a for a in args if a != "--redo-brief"] + flag
    return [{"class": "llm", "args": args + flag + ["--dry-run"], "label": "casting briefs"},
            {"class": "comfyui" if engine == "qwen" else "breeze", "args": voices, "label": "voices"}]


def _say_options(job: dict, args: list) -> list[dict]:
    cut = args.index("--") if "--" in args else len(args)
    if any(a == "--render-only" or a.startswith("--add=") for a in args[:cut]):
        return [{"class": "breeze", "args": args}]
    return [{"class": "llm", "args": args[:cut] + ["--dry-run"] + args[cut:], "label": "respellings"},
            {"class": "breeze", "args": args[:cut] + ["--render-only"] + args[cut:], "label": "takes"}]


def _draw(job: dict, args: list) -> list[dict]:
    if "--dry-run" in args:
        return [{"class": "llm", "args": args}]
    images = [a for a in args if a != "--redo-brief"] + ["--briefed"]
    if any(a.startswith("--prompt=") for a in args):
        return [{"class": "comfyui", "args": images}]
    return [{"class": "llm", "args": args + ["--dry-run"], "label": "descriptions"},
            {"class": "comfyui", "args": images, "label": "images"}]


def _render(job: dict, args: list) -> list[dict]:
    return [{"class": "comfyui" if job.get("comfy") else "breeze", "args": args}]


@dataclass(frozen=True)
class Kind:
    """One kind of job. A flag that is not named is False."""
    # One machine (jobs.py)
    heavy: bool = False             # a CLI run of it that the app did not start holds the machine's heavy jobs
    writes_bible: bool = False      # it rewrites the bible, so a quick job doesn't start beside it
    writes_serial: bool = False     # it rewrites serial.yaml in place, so it waits for any job on that serial
    render_neutral: bool = False    # it changes nothing a render reads beyond what render_changes shows
    backlog_beside: bool = False    # as backlog work it writes only its chapter's files, so two run side by side
    starts_comfy: bool = False      # it starts ComfyUI itself when none answers

    # The cluster's wait rules (nomad.conflicts)
    llm: bool = False               # a Claude pass
    look: bool = False              # it writes only the serial's looks, settings and images, which no other reads
    serial_wide: bool = False       # it works on the whole serial, where another works on its chapters
    config_writer: bool = False     # it writes only what a chapter job took in its bundle: voices/, the bible, serial.yaml
    chapter: bool = False           # it writes only its chapters' files
    no_chapter_input: bool = False  # it changes nothing a chapter job reads
    no_script_input: bool = False   # it changes nothing a screenplay reads
    voice_reader: bool = False      # a chapter job that reads the voices a cast files

    # The text that marks its ComfyUI prompts in their graphs, with {slug} and {chapter}, or None when it queues none
    marker: str | None = None
    # stages(job, args) gives its Nomad dispatches in order. None runs it on the app's machine
    stages: Callable | None = None


KINDS: dict[str, Kind] = {
    "character-pass": Kind(heavy=True, writes_bible=True, llm=True, serial_wide=True, stages=_one("llm")),
    "screenplay": Kind(heavy=True, llm=True, chapter=True, stages=_one("llm")),
    "render": Kind(heavy=True, render_neutral=True, backlog_beside=True, chapter=True, voice_reader=True,
                   marker="{slug}/ch{chapter:04d}", stages=_render),
    "check-takes": Kind(heavy=True, backlog_beside=True, chapter=True, voice_reader=True,
                        marker="breeze/{slug}/ch{chapter:04d}/", stages=_one("breeze")),
    "cast": Kind(heavy=True, writes_bible=True, serial_wide=True, config_writer=True, no_script_input=True,
                 marker="cast/{slug}/", stages=_cast),
    "bios": Kind(heavy=True, writes_bible=True, llm=True, serial_wide=True, config_writer=True, no_script_input=True,
                 stages=_one("llm")),
    "look-pass": Kind(heavy=True, llm=True, look=True, no_chapter_input=True, stages=_one("llm")),
    "draw": Kind(heavy=True, starts_comfy=True, look=True, no_chapter_input=True, marker="images/{slug}/",
                 stages=_draw),
    "pick-image": Kind(look=True, no_chapter_input=True),
    "pick-voice": Kind(serial_wide=True, config_writer=True, no_script_input=True),
    "say-as": Kind(serial_wide=True, config_writer=True),
    "say-options": Kind(heavy=True, render_neutral=True, serial_wide=True, config_writer=True, no_chapter_input=True,
                        stages=_say_options),
    "voice-effect": Kind(writes_serial=True, serial_wide=True, config_writer=True, no_script_input=True),
    "fetch": Kind(serial_wide=True),
    "add-royalroad": Kind(),
    "add-epub": Kind(),
    "reset-bible": Kind(serial_wide=True),
    "prune-media": Kind(),
    SYNC: Kind(stages=_one("breeze")),
}


FLAGS = frozenset(f.name for f in fields(Kind) if f.type == "bool")


def having(flag: str) -> frozenset[str]:
    """The kinds whose entry has a flag set. Raises AttributeError for a name that is not one of FLAGS."""
    if flag not in FLAGS:
        raise AttributeError(f"a kind has no flag {flag!r}")
    return frozenset(name for name, kind in KINDS.items() if getattr(kind, flag))


def stages(job: dict) -> list[dict] | None:
    """The Nomad dispatches a job runs as, in order, or None when it runs on the app's machine.

    A cast writes its casting briefs on a Claude node first and then designs the voices on a GPU node, which finds
    the briefs cached and calls no LLM. Only a Qwen cast needs a ComfyUI node. The options of a term are proposed on
    a Claude node and then said on a breeze node, and a job that only says options, again or with one spelling
    added, is the breeze stage alone. A take check runs on any breeze node, and picking a voice runs on the app's
    machine, from the media mirror. A draw writes its descriptions on a Claude node and paints on a ComfyUI node,
    which asks no LLM, and a draw from a description that came with the job is the ComfyUI stage alone.
    """
    name, args = job["kind"], list(job["args"])
    kind = KINDS.get(name)
    return kind.stages(job, args) if kind is not None and kind.stages is not None else None


def comfy_marker(kind, slug: str, chapters) -> str | None:
    """The text that marks a job's ComfyUI prompts in their graphs.

    kind, slug: the job's kind and serial.
    chapters: the job's chapters, of which the first counts.
    Returns None for a kind that queues no prompt, and for a kind whose marker names a chapter when the job has
    none.
    """
    entry = KINDS.get(kind) if isinstance(kind, str) else None
    if entry is None or entry.marker is None:
        return None
    if "{chapter" not in entry.marker:
        return entry.marker.format(slug=slug)
    return entry.marker.format(slug=slug, chapter=int(chapters[0])) if chapters else None
