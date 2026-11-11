"""Copies of the library on another disk: a daily backup, and the rendered chapters as audiobook folders.

backup() writes a snapshot archive of everything but the audio and copies new or changed audio beside it.
publish() copies each rendered chapter to <folder>/<serial title>/<NNN> - <chapter title>.opus, the layout of an
audiobook player that reads one folder per book. Mirror runs both on a schedule beside the web app. Neither writes
to the library. Standard library only.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import os
import re
import shutil
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Iterable

from .. import library as lib
from . import views

MARKER = ".serial-audio-backup"
RECORD = ".serial-audio.json"
SNAPSHOT_RE = re.compile(r"^library-(\d{8}-\d{6})\.tar\.gz$")
STAMP = "%Y%m%d-%H%M%S"
KEEP_SNAPSHOTS = 30
BACKUP_EVERY = 24 * 3600.0
PUBLISH_EVERY = 300.0
RETRY_AFTER = 3600.0
STEP_SECONDS = 60.0


class MirrorError(RuntimeError):
    pass


def _copy(src: Path, dst: Path) -> None:
    """Copies src to dst with its modification time. dst appears whole or not at all."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, tmp)
    st = src.stat()
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, dst)


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def snapshots(dest: Path) -> list[tuple[float, Path]]:
    """The snapshot archives under dest as (time made, path), oldest first."""
    out = []
    folder = dest / "snapshots"
    for path in folder.iterdir() if folder.is_dir() else []:
        m = SNAPSHOT_RE.match(path.name)
        if m:
            out.append((time.mktime(time.strptime(m.group(1), STAMP)), path))
    return sorted(out)


def backup(library: Path, dest: Path, extra: dict[str, Path] | None = None, keep: int = KEEP_SNAPSHOTS,
           now: float | None = None) -> dict:
    """Backs the library up under dest.

    library: the library folder.
    dest: the backup folder. It must hold the file MARKER, which shows that the backup disk is mounted there.
    extra: other files and folders for the snapshot, as {name in the archive: path}. A missing one is left out.
    keep: how many snapshots stay under dest.
    now: the time the snapshot is named for. None means the current time.
    Returns {"snapshot", "files", "skipped", "audio_copied", "removed"}. Writes
    snapshots/library-<time>.tar.gz with every library file but the audio, and copies each audio file that is new
    or changed to library/<slug>/audio/. An audio copy is never deleted. A file that cannot be read is named in
    "skipped". A snapshot that fails partway leaves no file behind.
    Raises MirrorError when dest has no marker, and OSError when dest cannot be written.
    """
    if not (dest / MARKER).is_file():
        raise MirrorError(f"{dest} has no {MARKER} file, so the backup disk may not be mounted there")

    # Snapshot
    sources = [(p, "library/" + p.relative_to(library).as_posix()) for p in sorted(library.rglob("*"))
               if p.is_file() and p.relative_to(library).parts[1:2] != ("audio",)]
    for name, path in sorted((extra or {}).items()):
        if path.is_dir():
            sources += [(p, f"{name}/{p.relative_to(path).as_posix()}") for p in sorted(path.rglob("*")) if p.is_file()]
        elif path.is_file():
            sources.append((path, name))
    folder = dest / "snapshots"
    folder.mkdir(exist_ok=True)
    for stale in folder.glob("*.part"):
        stale.unlink()
    target = folder / f"library-{time.strftime(STAMP, time.localtime(now))}.tar.gz"
    tmp = target.with_name(target.name + ".part")
    files, skipped = 0, []
    try:
        with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
            for path, name in sources:
                # read whole, so a file that changes while it is read cannot break the archive
                try:
                    data, mtime = path.read_bytes(), path.stat().st_mtime
                except OSError:
                    skipped.append(name)
                    continue
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(data), int(mtime), 0o644
                tar.addfile(info, io.BytesIO(data))
                files += 1
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)

    # Audio
    copied = 0
    for src in sorted(library.glob("*/audio/*")):
        dst = dest / "library" / src.relative_to(library)
        if not src.is_file():
            continue
        st = src.stat()
        if dst.is_file() and dst.stat().st_size == st.st_size and int(dst.stat().st_mtime) == int(st.st_mtime):
            continue
        _copy(src, dst)
        copied += 1

    # Old snapshots
    old = [path for _, path in snapshots(dest)][:-keep] if keep > 0 else []
    for path in old:
        path.unlink()
    return {"snapshot": target.name, "files": files, "skipped": skipped, "audio_copied": copied, "removed": len(old)}


def _publish_serial(slug: str, dest: Path) -> int:
    """Copies one serial's rendered chapters into its folder under dest. Returns how many it copied.

    Raises MirrorError when the folder exists and this function did not make it for this serial.
    """
    serial = views._yaml(lib.serial_dir(slug) / "serial.yaml", {}) or {}
    numbers = lib.list_chapters(slug)
    rendered = [(n, meta, audio) for n in numbers
                if (audio := views._audio_file(slug, meta := views._chapter_meta(slug, n)))]
    if not rendered:
        return 0
    folder = dest / views.book_folder(serial, slug)
    record_path = folder / RECORD
    if folder.exists() and not record_path.is_file():
        raise MirrorError(f"{folder} exists and this app did not make it")
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {"slug": slug, "chapters": {}}
    if record.get("slug") != slug:
        raise MirrorError(f"{folder} holds the serial {record.get('slug')!r}")
    folder.mkdir(parents=True, exist_ok=True)
    copied, changed = 0, not record_path.is_file()
    for n, meta, audio in rendered:
        name = views.book_file(n, numbers, meta, audio.suffix)
        st = audio.stat()
        source = [st.st_size, st.st_mtime_ns]
        old = record["chapters"].get(str(n)) or {}
        if old.get("source") == source:
            # the same audio under a new title keeps its copy, and a copy deleted from the folder stays deleted
            if old.get("name") != name:
                if (folder / old["name"]).is_file():
                    os.replace(folder / old["name"], folder / name)
                old["name"], changed = name, True
            continue
        if old.get("name") and old["name"] != name:
            (folder / old["name"]).unlink(missing_ok=True)
        _copy(audio, folder / name)
        record["chapters"][str(n)] = {"name": name, "source": source}
        copied, changed = copied + 1, True
    if changed:
        _write_json(record_path, record)
    return copied


def publish(dest: Path, skip: Iterable[str] = ()) -> dict:
    """Copies every rendered chapter to dest/<serial title>/<NNN> - <chapter title>.<suffix>.

    dest: the folder an audiobook player, or the tool that syncs to it, reads. It must exist and hold something,
        which shows that its disk is mounted.
    skip: slugs of serials to leave out.
    Returns {"copied", "errors"}: how many chapters were copied, and one line per serial that failed. A chapter is
    copied when it is new or was rendered again since its last copy. A copy deleted from dest is not made again
    until then. A folder under dest that this function did not make is left alone and reported in "errors". One
    serial's failure, such as a chapter record that does not parse, does not stop the others.
    Raises MirrorError when dest is missing or empty.
    """
    if not dest.is_dir() or not any(dest.iterdir()):
        raise MirrorError(f"{dest} is missing or empty, so its disk may not be mounted there")
    report = {"copied": 0, "errors": []}
    skip = set(skip)
    for slug in views._serial_slugs():
        if slug in skip:
            continue
        try:
            report["copied"] += _publish_serial(slug, dest)
        except Exception as e:
            report["errors"].append(f"{slug}: {type(e).__name__}: {e}")
    return report


def _iso(ts: float | None) -> str | None:
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


class Mirror(threading.Thread):
    """Backs the library up once a day and keeps the audiobook folders current, beside the web app."""

    def __init__(self, library: Path, backup_dir: Path | None = None, publish_dir: Path | None = None,
                 publish_skip: Iterable[str] = (), extra: dict[str, Path] | None = None):
        super().__init__(name="mirror", daemon=True)
        self.library, self.backup_dir, self.publish_dir = library, backup_dir, publish_dir
        self.publish_skip, self.extra = tuple(publish_skip), dict(extra or {})
        self.halt = threading.Event()
        self.next_backup = self.next_publish = 0.0
        self.last_backup: float | None = None
        self.state: dict = {}

    def run(self) -> None:
        while not self.halt.is_set():
            self.step(time.time())
            self.halt.wait(STEP_SECONDS)

    def stop(self) -> None:
        self.halt.set()

    def status(self, now: float | None = None) -> dict:
        """What the API reports: for the backup and the audiobook folders, whether the last run worked and when.

        A backup that is more than two days old reads as failed, whatever the last run said, so a backup that
        hangs on its disk shows too.
        """
        out = dict(self.state)
        now = time.time() if now is None else now
        backup_state = out.get("backup")
        if backup_state and backup_state["ok"] and self.last_backup and now - self.last_backup > 2 * BACKUP_EVERY:
            out["backup"] = {**backup_state, "ok": False, "error": "the last backup is more than two days old"}
        return out

    def step(self, now: float) -> None:
        """Runs the backup and the audiobook folders when each is due. Never raises."""
        if self.backup_dir is not None and now >= self.next_backup:
            self._backup(now)
        if self.publish_dir is not None and now >= self.next_publish:
            self._publish(now)

    def _backup(self, now: float) -> None:
        try:
            made = snapshots(self.backup_dir)
            self.last_backup = made[-1][0] if made else None
            if self.last_backup is not None and now - self.last_backup < BACKUP_EVERY:
                self.next_backup = self.last_backup + BACKUP_EVERY
                self.state["backup"] = {"ok": True, "last": _iso(self.last_backup)}
                return
            report = backup(self.library, self.backup_dir, self.extra, now=now)
            self.next_backup, self.last_backup = now + BACKUP_EVERY, now
            self.state["backup"] = {"ok": True, "last": _iso(now), **report}
            print(f"[mirror] backup {report['snapshot']}: {report['files']} files, {report['audio_copied']} audio "
                  f"file(s) copied, {len(report['skipped'])} skipped", file=sys.stderr, flush=True)
        except Exception as e:
            self.next_backup = now + RETRY_AFTER
            self.state["backup"] = {"ok": False, "last": _iso(self.last_backup), "error": f"{type(e).__name__}: {e}"}
            print(f"[mirror] backup failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    def _publish(self, now: float) -> None:
        self.next_publish = now + PUBLISH_EVERY
        try:
            report = publish(self.publish_dir, self.publish_skip)
        except Exception as e:
            report = {"copied": 0, "errors": [f"{type(e).__name__}: {e}"]}
        was = self.state.get("publish") or {}
        self.state["publish"] = {"ok": not report["errors"], "checked": _iso(now), "errors": report["errors"],
                                 "last": _iso(now) if report["copied"] else was.get("last")}
        if report["copied"] or report["errors"] != was.get("errors", []):
            print(f"[mirror] audiobook folders: {report['copied']} chapter(s) copied"
                  + (f", errors: {'; '.join(report['errors'])}" if report["errors"] else ""), file=sys.stderr, flush=True)
