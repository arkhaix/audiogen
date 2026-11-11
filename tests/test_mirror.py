"""Tests for serial_audio.web.mirror: the library backup, the audiobook folders and the schedule that runs both."""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from serial_audio import library as lib
from serial_audio.web import mirror, views

SLUG = "the-test-serial"
TITLE = "The Test: Serial?"
FOLDER = "The Test Serial"


class MirrorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / "library"
        patch = mock.patch.object(lib, "LIBRARY", self.library)
        patch.start()
        self.addCleanup(patch.stop)
        views._cache.clear()
        lib.save_serial(SLUG, {"title": TITLE})
        lib.save_bible(SLUG, {"characters": {"ann": {"name": "Ann"}}, "pronunciations": {}})
        for n, title in ((1, "Arrival"), (2, "The Door"), (3, "Not Rendered")):
            meta = {"number": n, "label": f"Chapter {n}", "title": title, "status": "scripted"}
            if n < 3:
                meta.update(status="rendered", render={"audio": f"audio/ch{n:04d}.opus"})
                self.audio(n, f"audio of chapter {n}")
            lib.save_chapter_meta(SLUG, n, meta)
            (lib.chapter_dir(SLUG, n) / "text.md").write_text(f"# {title}\n", encoding="utf-8")

    def audio(self, n: int, content: str) -> Path:
        path = lib.serial_dir(SLUG) / "audio" / f"ch{n:04d}.opus"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class BackupTest(MirrorCase):
    def setUp(self):
        super().setUp()
        self.dest = self.root / "backup"
        self.dest.mkdir()
        (self.dest / mirror.MARKER).write_text("", encoding="utf-8")
        self.incoming = self.root / "incoming"
        (self.incoming / SLUG).mkdir(parents=True)
        (self.incoming / SLUG / "saved.html").write_text("<html>", encoding="utf-8")
        self.extra = {"incoming": self.incoming, "state/placement.json": self.root / "placement.json",
                      "state/missing.json": self.root / "missing.json"}
        (self.root / "placement.json").write_text("{}", encoding="utf-8")

    def names(self, snapshot: str) -> set[str]:
        with tarfile.open(self.dest / "snapshots" / snapshot) as tar:
            return set(tar.getnames())

    def test_the_snapshot_holds_everything_but_the_audio(self):
        report = mirror.backup(self.library, self.dest, self.extra)
        names = self.names(report["snapshot"])
        self.assertIn(f"library/{SLUG}/serial.yaml", names)
        self.assertIn(f"library/{SLUG}/bible/characters.yaml", names)
        self.assertIn(f"library/{SLUG}/chapters/0002/text.md", names)
        self.assertIn(f"incoming/{SLUG}/saved.html", names)
        self.assertIn("state/placement.json", names)
        self.assertFalse([n for n in names if "/audio/" in n or n.endswith(".part") or "missing" in n], names)
        self.assertEqual((report["files"], report["skipped"]), (len(names), []))

        # A restored file reads as the library's.
        with tarfile.open(self.dest / "snapshots" / report["snapshot"]) as tar:
            restored = tar.extractfile(f"library/{SLUG}/serial.yaml").read()
        self.assertEqual(restored, (lib.serial_dir(SLUG) / "serial.yaml").read_bytes())

    def test_audio_is_copied_once_and_again_when_it_changes(self):
        self.assertEqual(mirror.backup(self.library, self.dest, now=1_000_000)["audio_copied"], 2)
        copy = self.dest / "library" / SLUG / "audio" / "ch0001.opus"
        self.assertEqual(copy.read_text(encoding="utf-8"), "audio of chapter 1")

        # Unchanged audio is not copied again. A chapter rendered again is.
        self.assertEqual(mirror.backup(self.library, self.dest, now=1_000_100)["audio_copied"], 0)
        again = self.audio(1, "a second render")
        os.utime(again, (time.time() + 5, time.time() + 5))
        self.assertEqual(mirror.backup(self.library, self.dest, now=1_000_200)["audio_copied"], 1)
        self.assertEqual(copy.read_text(encoding="utf-8"), "a second render")

        # Audio that is gone from the library keeps its copy.
        again.unlink()
        mirror.backup(self.library, self.dest, now=1_000_300)
        self.assertTrue(copy.is_file())

    def test_the_restore_steps_bring_back_every_file(self):
        # deploy/README.md: unpack the snapshot over the app's folder, then copy backup/library over library.
        report = mirror.backup(self.library, self.dest, self.extra)
        restored = self.root / "restored"
        with tarfile.open(self.dest / "snapshots" / report["snapshot"]) as tar:
            tar.extractall(restored)
        shutil.copytree(self.dest / "library", restored / "library", dirs_exist_ok=True)
        want = {p.relative_to(self.library).as_posix(): p.read_bytes() for p in self.library.rglob("*") if p.is_file()}
        got = {p.relative_to(restored / "library").as_posix(): p.read_bytes()
               for p in (restored / "library").rglob("*") if p.is_file()}
        self.assertEqual(got, want)
        self.assertIn(f"{SLUG}/audio/ch0002.opus", got)
        self.assertTrue((restored / "incoming" / SLUG / "saved.html").is_file())
        self.assertTrue((restored / "state" / "placement.json").is_file())

    def test_a_snapshot_that_fails_leaves_no_file(self):
        # A write that fails partway raises, and neither a snapshot nor its temporary file stays.
        with mock.patch.object(tarfile.TarFile, "addfile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                mirror.backup(self.library, self.dest)
        self.assertEqual(list((self.dest / "snapshots").iterdir()), [])

        # The temporary file of a backup that was killed goes with the next backup.
        (self.dest / "snapshots" / "library-20200101-000000.tar.gz.part").write_text("half", encoding="utf-8")
        report = mirror.backup(self.library, self.dest)
        self.assertEqual([p.name for p in (self.dest / "snapshots").iterdir()], [report["snapshot"]])

    def test_only_the_newest_snapshots_stay(self):
        for i in range(4):
            report = mirror.backup(self.library, self.dest, keep=2, now=1_000_000 + 100 * i)
        self.assertEqual(report["removed"], 1)
        kept = [path.name for _, path in mirror.snapshots(self.dest)]
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[-1], report["snapshot"])

    def test_a_folder_without_the_marker_is_refused(self):
        # An unmounted backup disk leaves an empty folder, which must not take a backup.
        (self.dest / mirror.MARKER).unlink()
        with self.assertRaises(mirror.MirrorError):
            mirror.backup(self.library, self.dest)
        self.assertFalse((self.dest / "snapshots").exists())


class PublishTest(MirrorCase):
    def setUp(self):
        super().setUp()
        self.dest = self.root / "audiobooks"
        (self.dest / "Some Other Book").mkdir(parents=True)

    def files(self) -> list[str]:
        return sorted(p.name for p in (self.dest / FOLDER).iterdir() if not p.name.startswith("."))

    def test_each_rendered_chapter_lands_in_its_serials_folder(self):
        self.assertEqual(mirror.publish(self.dest), {"copied": 2, "errors": []})
        self.assertEqual(self.files(), ["001 - Arrival.opus", "002 - The Door.opus"])
        self.assertEqual((self.dest / FOLDER / "002 - The Door.opus").read_text(encoding="utf-8"), "audio of chapter 2")
        self.assertEqual(mirror.publish(self.dest), {"copied": 0, "errors": []})
        self.assertEqual(os.listdir(self.dest / "Some Other Book"), [])

    def test_the_serial_page_gets_the_same_folder_and_file_names(self):
        # The page's save button writes the names the folder export writes.
        mirror.publish(self.dest)
        detail = views.serial_detail(SLUG, 0.5)
        self.assertEqual(detail["folder"], FOLDER)
        self.assertEqual([c["save_name"] for c in detail["chapters"]], self.files() + [None])

    def test_a_deleted_copy_comes_back_only_after_a_new_render(self):
        # The listener deletes a finished chapter, and it stays deleted.
        mirror.publish(self.dest)
        (self.dest / FOLDER / "001 - Arrival.opus").unlink()
        self.assertEqual(mirror.publish(self.dest)["copied"], 0)
        self.assertEqual(self.files(), ["002 - The Door.opus"])

        # A new render of it is copied again.
        again = self.audio(1, "a second render")
        os.utime(again, (time.time() + 5, time.time() + 5))
        self.assertEqual(mirror.publish(self.dest)["copied"], 1)
        self.assertEqual((self.dest / FOLDER / "001 - Arrival.opus").read_text(encoding="utf-8"), "a second render")

    def test_a_new_title_renames_the_copy(self):
        mirror.publish(self.dest)
        meta = lib.load_chapter(SLUG, 2)[0]
        lib.save_chapter_meta(SLUG, 2, {**meta, "title": "The Second Door"})
        views._cache.clear()
        self.assertEqual(mirror.publish(self.dest)["copied"], 0)
        self.assertEqual(self.files(), ["001 - Arrival.opus", "002 - The Second Door.opus"])

    def test_a_folder_the_app_did_not_make_is_left_alone(self):
        # A real audiobook with the serial's title already sits in the share.
        (self.dest / FOLDER).mkdir()
        (self.dest / FOLDER / "book.mp3").write_text("someone else's", encoding="utf-8")
        report = mirror.publish(self.dest)
        self.assertEqual(report["copied"], 0)
        self.assertIn(SLUG, report["errors"][0])
        self.assertEqual(self.files(), ["book.mp3"])

    def test_a_skipped_serial_and_an_unmounted_folder(self):
        self.assertEqual(mirror.publish(self.dest, skip=[SLUG]), {"copied": 0, "errors": []})
        self.assertFalse((self.dest / FOLDER).exists())
        empty = self.root / "empty"
        empty.mkdir()
        for dest in (empty, self.root / "absent"):
            with self.assertRaises(mirror.MirrorError):
                mirror.publish(dest)


class ScheduleTest(MirrorCase):
    def setUp(self):
        super().setUp()
        self.backups = self.root / "backup"
        self.backups.mkdir()
        (self.backups / mirror.MARKER).write_text("", encoding="utf-8")
        self.books = self.root / "audiobooks"
        (self.books / "Some Other Book").mkdir(parents=True)
        self.m = mirror.Mirror(self.library, self.backups, self.books)
        self.now = time.time()

    def test_the_backup_runs_once_a_day(self):
        # The first step backs up. A step an hour later does not, and one a day later does.
        self.m.step(self.now)
        self.assertEqual(len(mirror.snapshots(self.backups)), 1)
        self.assertTrue(self.m.status()["backup"]["ok"])
        self.m.step(self.now + 3600)
        self.assertEqual(len(mirror.snapshots(self.backups)), 1)
        self.m.step(self.now + mirror.BACKUP_EVERY + 60)
        self.assertEqual(len(mirror.snapshots(self.backups)), 2)

        # A restarted app sees the last snapshot and waits for it to be a day old.
        fresh = mirror.Mirror(self.library, self.backups)
        fresh.step(self.now + mirror.BACKUP_EVERY + 3600)
        self.assertEqual(len(mirror.snapshots(self.backups)), 2)
        self.assertEqual(fresh.status()["backup"]["ok"], True)

    def test_a_failed_backup_is_reported_and_tried_again(self):
        (self.backups / mirror.MARKER).unlink()
        self.m.step(self.now)
        status = self.m.status()["backup"]
        self.assertEqual((status["ok"], status["last"]), (False, None))
        self.assertIn(mirror.MARKER, status["error"])
        json.dumps(self.m.status())

        # The next try comes after RETRY_AFTER, not on the next step.
        (self.backups / mirror.MARKER).write_text("", encoding="utf-8")
        self.m.step(self.now + 60)
        self.assertFalse(self.m.status()["backup"]["ok"])
        self.m.step(self.now + mirror.RETRY_AFTER + 1)
        self.assertTrue(self.m.status()["backup"]["ok"])

    def test_a_backup_that_stops_running_reads_as_failed(self):
        # A backup that hangs on its disk never reports, so its age alone marks it.
        self.m.step(self.now)
        self.assertTrue(self.m.status(self.now + mirror.BACKUP_EVERY)["backup"]["ok"])
        late = self.m.status(self.now + 3 * mirror.BACKUP_EVERY)["backup"]
        self.assertEqual(late["ok"], False)
        self.assertIn("two days", late["error"])

    def test_a_chapter_record_that_does_not_parse_stops_nothing(self):
        # A chapter.yaml caught half written fails that serial's folders for one pass. The backup still runs.
        record = lib.chapter_dir(SLUG, 2) / "chapter.yaml"
        good = record.read_text(encoding="utf-8")
        record.write_text("render: {audio: [", encoding="utf-8")
        views._cache.clear()
        self.m.step(self.now)
        status = self.m.status()
        self.assertEqual((status["backup"]["ok"], status["publish"]["ok"]), (True, False))
        self.assertIn(SLUG, status["publish"]["errors"][0])

        # The next pass, with the record whole again, copies the chapters.
        record.write_text(good, encoding="utf-8")
        views._cache.clear()
        self.m.step(self.now + mirror.PUBLISH_EVERY + 1)
        self.assertTrue(self.m.status()["publish"]["ok"])
        self.assertEqual(len(list((self.books / FOLDER).glob("*.opus"))), 2)

    def test_the_audiobook_folders_follow_new_renders(self):
        self.m.step(self.now)
        self.assertEqual(len(list((self.books / FOLDER).glob("*.opus"))), 2)
        meta = lib.load_chapter(SLUG, 3)[0]
        self.audio(3, "audio of chapter 3")
        lib.save_chapter_meta(SLUG, 3, {**meta, "status": "rendered", "render": {"audio": "audio/ch0003.opus"}})
        views._cache.clear()
        self.m.step(self.now + 60)
        self.assertEqual(len(list((self.books / FOLDER).glob("*.opus"))), 2)
        self.m.step(self.now + mirror.PUBLISH_EVERY + 1)
        self.assertEqual(len(list((self.books / FOLDER).glob("*.opus"))), 3)
        self.assertTrue(self.m.status()["publish"]["ok"])


if __name__ == "__main__":
    unittest.main()
