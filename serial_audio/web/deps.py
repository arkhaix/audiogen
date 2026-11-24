"""Which job needs which, and what follows when a needed job ends.

A job needs another when it reads what the other makes: a render its chapter's screenplay, a screenplay its
chapter's character pass, a character pass the pass of the chapter before it. needs() is that rule, and wire()
gives a job the jobs it needs among the ones queued before it. A job's record keeps the ids of the jobs it still
needs, from the moment it is queued until it starts. The job starts when each of them has ended well, and it is
skipped when one has not. Which jobs can't run at the same time is another rule, nomad.conflicts(). Standard
library only, beside kinds.
"""
from __future__ import annotations

from . import kinds

# the statuses of a job that has not ended
OPEN = ("queued", "running")
# the kinds that read a chapter's text, which the import of its saved page writes
TEXT_READERS = frozenset({"character-pass", "look-pass", "screenplay", "render", "check-takes"})
# the kinds that read the bible, which a reset empties
BIBLE_READERS = frozenset({"character-pass", "cast", "bios", "look-pass", "screenplay", "render", "check-takes",
                           "draw"})
CHAPTER_KINDS = kinds.having("chapter")
VOICE_READERS = kinds.having("voice_reader")
# how a job that did not end well is named in the note of a job that needed it
ENDED = {"failed": "failed", "lost": "was lost", "stopped": "was stopped", "cancelled": "was cancelled",
         "skipped": "was skipped"}


def _meta(job: dict) -> dict:
    meta = job.get("meta")
    return meta if isinstance(meta, dict) else {}


def chapters(job: dict) -> set[int]:
    """The chapters a job's record names."""
    named = _meta(job).get("chapters")
    return {n for n in named if isinstance(n, int)} if isinstance(named, list) else set()


_chapters = chapters


def is_import(job: dict) -> bool:
    """Whether a job imports one saved chapter page. It writes only that chapter's files, on the app's machine."""
    return job["kind"] == "fetch" and "--html" in (job.get("args") or [])


def _share(a: set[int], b: set[int]) -> bool:
    """Whether two jobs with these chapters may work on one chapter. A job that names none may work on any."""
    return not a or not b or bool(a & b)


def needs(job: dict, other: dict) -> bool:
    """Whether job reads what other makes, so that it starts only after other has ended well.

    job: a job record, with kind, slug and meta.
    other: a job that was queued before it.
    Returns True for these pairs on one serial, and False for any other pair:

    * A job that reads a chapter needs the import of that chapter's saved page. A character pass or a screenplay
      that names no chapter needs a fetch from the site.
    * A character pass needs the passes of earlier chapters.
    * A cast for everyone needs the character passes, and one that follows a chapter's pass needs the passes up
      to its chapter.
    * A look pass, a screenplay, a render and a take check need a character pass on a chapter of theirs. One of
      the two that names no chapter counts as working on every chapter.
    * A render and a take check need a cast, unless it only writes briefs or follows the pass of a chapter after
      theirs. They need a voice pick and a voice effect.
    * A screenplay, a render and a take check need a say-as.
    * A render and a take check need their chapter's screenplay, and a take check its chapter's render.
    * A job that reads the bible needs a reset of it.

    Also False for a job that is not backlog work beside backlog work, which waits for it. Raises KeyError on a
    record without kind.
    """
    slug = job.get("slug")
    if not slug or slug != other.get("slug"):
        return False
    if other.get("backlog") and not job.get("backlog"):
        return False
    kind, made = job["kind"], other["kind"]
    mine, theirs = _chapters(job), _chapters(other)
    if made == "fetch":
        if is_import(other):
            return kind in TEXT_READERS and bool(mine & theirs)
        # a pass that names no chapter was queued before its chapter was on disk, and the fetch brings it
        return kind in ("character-pass", "screenplay") and not mine
    if made == "character-pass":
        if kind == "character-pass":
            return bool(mine) and bool(theirs) and max(theirs) < min(mine)
        if kind == "cast":
            first = _meta(job).get("from_chapter")
            return not _meta(job).get("characters") and (not isinstance(first, int) or not theirs
                                                         or min(theirs) <= first)
        return (kind == "look-pass" or kind in CHAPTER_KINDS) and _share(mine, theirs)
    if made == "cast":
        first = _meta(other).get("from_chapter")
        return (kind in VOICE_READERS and not _meta(other).get("dry_run")
                and not (isinstance(first, int) and bool(mine) and max(mine) < first))
    if made in ("pick-voice", "voice-effect"):
        return kind in VOICE_READERS
    if made == "say-as":
        return kind in CHAPTER_KINDS
    if made == "screenplay":
        return kind in ("render", "check-takes") and bool(mine & theirs)
    if made == "render":
        return kind == "check-takes" and bool(mine & theirs)
    if made == "reset-bible":
        return kind in BIBLE_READERS
    return False


def _ending(job: dict) -> bool:
    """Whether a job was asked to stop and doesn't go back into the queue, so it ends as stopped."""
    n = job.get("nomad")
    n = n if isinstance(n, dict) else {}
    if n.get("cancel"):
        return not n.get("requeue") and not (n.get("evict") and not n.get("by_user"))
    return bool(job.get("stopping")) and not n


def _makes(job: dict):
    """What a needed job makes, as a key that two jobs share when the later one makes the same thing again: a
    chapter's text, its characters, its script or its audio, and the voices of everyone who needs one."""
    kind, named = job["kind"], tuple(sorted(chapters(job)))
    if named and (kind in ("character-pass", "screenplay", "render") or is_import(job)):
        return kind, named
    if kind == "cast" and not _meta(job).get("characters"):
        return (kind,)
    return job["id"]


def wire(job: dict, ahead) -> list[str]:
    """The ids of the jobs that a job needs.

    job: the job, which is being queued.
    ahead: the jobs that were queued before it, in the order of the queue.
    Returns the ids in the order of ahead, of the jobs that needs() names, with three kinds of job left out. A
    job that has ended, and one that was asked to stop for good. A job that makes what a later one of them makes
    again, such as the first of two screenplays of a chapter, or a cast for everyone before the last one. And a
    character pass that a later needed pass needs, so a pass names the pass of the chapter before it, and that
    one names the one before.
    """
    got = [o for o in ahead if o is not job and o.get("status") in OPEN and not _ending(o) and needs(job, o)]
    last = {_makes(o): o["id"] for o in got}
    got = [o for o in got if last[_makes(o)] == o["id"]]
    reached: set[str] = set()
    later = None
    for o in reversed(got):
        named = chapters(o)
        if o["kind"] != "character-pass" or not named:
            continue
        if later is not None and needs(later, o):
            reached.add(o["id"])
        if later is None or min(named) > min(chapters(later)):
            later = o
    return [o["id"] for o in got if o["id"] not in reached]


def met(need: dict) -> bool:
    """Whether a needed job ended well: it is done, or it was skipped because nothing was left for it to do."""
    return need.get("status") == "done" or (need.get("status") == "skipped" and not need.get("lacks"))


def _why(need: dict | None) -> str:
    if need is None:
        return "needs a job whose record is gone"
    return f"needs {need.get('title') or need.get('id')}, which {ENDED.get(need.get('status'), 'did not end well')}"


def settle(jobs: dict[str, dict], at: str) -> list[dict]:
    """Brings the queued jobs up to date with the jobs they need.

    jobs: every job record by id. The records are changed in place.
    at: the ISO time that a skipped job gets as the time it finished.
    Returns the jobs it skipped. A need that ended well is taken off the record of a job that waits for it. A
    queued job that needs a job which did not end well, or whose record is gone, is skipped: its lacks names that
    job and its note says so. A job that needs a skipped job is skipped with it.
    """
    skipped: list[dict] = []
    again = True
    while again:
        again = False
        for job in jobs.values():
            queued = job.get("status") == "queued"
            if not job.get("needs") or not (queued or (job.get("status") == "skipped" and job.get("lacks"))):
                continue
            job["needs"] = [nid for nid in job["needs"] if not (nid in jobs and met(jobs[nid]))]
            gone = next((nid for nid in job["needs"] if (jobs.get(nid) or {}).get("status") not in OPEN), None)
            if queued and gone is not None:
                job.update({"status": "skipped", "finished": at, "lacks": gone, "note": _why(jobs.get(gone))})
                skipped.append(job)
                again = True
    return skipped


def revive(jobs: dict[str, dict], was: str, now: str) -> list[dict]:
    """Queues again the jobs that were skipped for a job, when that job is queued again as another.

    jobs: every job record by id. The records are changed in place.
    was: the id of the job that did not end well.
    now: the id of the job that runs in its place.
    Returns the jobs it queued again, in the order of their seq. Every job that was skipped for a need gets the
    new job in the place of the old one among its needs. A job that was skipped for the old job comes back when
    each job it still needs is queued or running, and so does a job that was skipped for one that comes back. A
    job that still needs a job which did not end well stays skipped, and its lacks then names that job. A job
    that was itself queued again, which its `again` says, stays skipped.
    """
    back: list[dict] = []
    alive = {was}
    for job in jobs.values():
        if job.get("status") == "skipped" and job.get("lacks") and was in (job.get("needs") or ()):
            job["needs"] = list(dict.fromkeys(now if nid == was else nid for nid in job["needs"]))
    again = True
    while again:
        again = False
        for job in sorted(jobs.values(), key=lambda j: j.get("seq", 0)):
            if job.get("status") != "skipped" or job.get("lacks") not in alive or job.get("again"):
                continue
            ids = [nid for nid in job.get("needs") or [] if not (nid in jobs and met(jobs[nid]))]
            job["needs"] = ids
            gone = next((nid for nid in ids if (jobs.get(nid) or {}).get("status") not in OPEN), None)
            if gone is not None:
                job.update({"lacks": gone, "note": _why(jobs.get(gone))})
                continue
            job.update({"status": "queued", "finished": None, "note": None})
            alive.add(job["id"])
            del job["lacks"]
            back.append(job)
            again = True
    return back


def leans_on(job: dict, other: dict, jobs: dict[str, dict]) -> bool:
    """Whether job needs other, directly or through the jobs between them.

    job, other: two job records.
    jobs: every job record by id.
    """
    seen: set[str] = set()
    stack = list(job.get("needs") or [])
    while stack:
        nid = stack.pop()
        if nid == other.get("id"):
            return True
        if nid not in seen:
            seen.add(nid)
            stack += (jobs.get(nid) or {}).get("needs") or []
    return False


def waits_for(job: dict, jobs: dict[str, dict]) -> str | None:
    """Why a queued job can't start for the jobs it needs, or None when it needs none that is still open.

    job: the queued job.
    jobs: every job record by id.
    """
    ids = [nid for nid in job.get("needs") or [] if not (nid in jobs and met(jobs[nid]))]
    if not ids:
        return None
    known = [jobs[nid] for nid in ids if nid in jobs]
    if not known:
        return _why(None)
    first = min(known, key=lambda j: j.get("seq", 0))
    more = len(ids) - 1
    state = ", which is paused" if first.get("paused") else ""
    return f"needs {first.get('title') or first.get('id')}{state}" + (
        f" and {more} more job{'s' if more > 1 else ''}" if more else "")
