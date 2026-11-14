"""Files between the web app and a cluster worker: the job bundle the app sends and the result it takes back.

A bundle is a zip of the pipeline code, the prompts and schemas, and one serial's library folder without its audio,
plus bundle.json. bundle.json names the job and its CLI steps and holds a manifest of every file in the serial's
folder, shipped or not, by SHA-256. A result is a zip of result.json and files/<path> for each file the worker
created or changed. The app writes a file back only when its copy still has the hash the worker started from.
The app keeps a media mirror of the audio the library names by a path on a node: Breeze takes and cast candidates.
A worker sends the mirror every take and candidate its node made, one request per file, and fetches from it the takes
another node made, so no job needs one node's files. A result may also hold media/<path>, which is how a worker sent
that audio before.
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path, PurePosixPath

from .. import library as lib

BUNDLE_FILE = "bundle.json"
RESULT_FILE = "result.json"
CODE_DIRS = ("serial_audio", "prompts", "schemas")
NOT_SHIPPED = ("audio/*",)
NOT_CODE = ("*/__pycache__/*", "*.pyc", "serial_audio/web/static/*", "*/._*")
RESUMABLE = ("chapters/*/breeze_takes.json", "chapters/*/*.log", "voices/_briefs.yaml")
MEDIA_SUFFIXES = {".flac", ".wav", ".opus", ".ogg", ".mp3", ".m4a"}
# what the mirror keeps: audio, and the Drama 3 voice file of a Qwen cast candidate
MIRROR_SUFFIXES = MEDIA_SUFFIXES | {".safetensors"}
TAKES_FILE = "breeze_takes.json"
MAX_FILES = 20_000

_hash_cache: dict[str, tuple[int, int, str]] = {}


class ExchangeError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    """The file's SHA-256 in hex. Reuses the last hash while size and mtime are unchanged."""
    st = path.stat()
    key = str(path)
    cached = _hash_cache.get(key)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    _hash_cache[key] = (st.st_mtime_ns, st.st_size, digest)
    return digest


def check_rel(rel: str) -> str:
    """rel when it is a plain relative path with forward slashes and no '..', else raises ExchangeError."""
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel or ":" in rel:
        raise ExchangeError(f"bad path {rel!r}")
    parts = PurePosixPath(rel).parts
    if rel.startswith("/") or any(p in ("", ".", "..") for p in parts) or "/".join(parts) != rel:
        raise ExchangeError(f"bad path {rel!r}")
    return rel


def mirror_rel(raw: str) -> str | None:
    """Where the media mirror keeps a file named by its absolute path on a node, or None for a path it cannot keep.

    A Windows path keeps its drive letter as the first folder, and a POSIX path loses its leading slash.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.replace("\\", "/")
    if len(text) > 1 and text[1] == ":":
        text = text[0] + text[2:]
    rel = "/".join(part for part in text.split("/") if part)
    try:
        return check_rel(rel)
    except ExchangeError:
        return None


def local_path(raw: str, root: str | Path, anchor: str) -> str | None:
    """Where a file that a node wrote under its output folder belongs under another node's output folder.

    raw: the file's absolute path on the node that wrote it, in that node's path style.
    root: the output folder of the node that asks.
    anchor: the folders every node keeps the file under inside its output folder, such as breeze/<slug>/ch0007.
    Returns root, the anchor and the rest of raw after its last anchor, or None when raw does not hold the anchor.
    """
    if not isinstance(raw, str):
        return None
    text = raw.replace("\\", "/")
    at = text.rfind(f"/{anchor}/")
    if at < 0:
        return None
    try:
        tail = check_rel(text[at + 1:])
    except ExchangeError:
        return None
    return str(Path(root).joinpath(*tail.split("/")))


def same_path(a: str | Path, b: str | Path) -> bool:
    """Whether two paths name one file on this machine, by spelling alone."""
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def take_files(chapter: Path) -> dict[str, str]:
    """{line: the take's path on the node that made it} from a chapter folder's breeze_takes.json.

    Returns {} when the chapter has no takes file or it does not parse.
    """
    try:
        takes = json.loads((chapter / TAKES_FILE).read_text(encoding="utf-8")).get("takes") or {}
        return {k: t["file"] for k, t in takes.items() if isinstance(t, dict) and isinstance(t.get("file"), str)
                and t["file"]}
    except (OSError, ValueError, AttributeError):
        return {}


def _matches(rel: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(rel, p) for p in patterns)


def manifest(folder: Path) -> dict[str, str]:
    """{path relative to folder: sha256} for every file under folder, with forward slashes."""
    out = {}
    if not folder.is_dir():
        return out
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            path = Path(root) / name
            if path.is_file():
                out[path.relative_to(folder).as_posix()] = sha256_file(path)
            if len(out) > MAX_FILES:
                raise ExchangeError(f"more than {MAX_FILES} files under {folder}")
    return out


def code_files(root: Path = lib.ROOT) -> list[tuple[str, Path]]:
    """The pipeline code, prompts and schemas a worker needs, as (archive path, file) pairs."""
    out = []
    for top in CODE_DIRS:
        base = root / top
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if path.is_file() and not _matches(rel, NOT_CODE):
                out.append((rel, path))
    return out


def code_version(root: Path = lib.ROOT) -> str:
    h = hashlib.sha256()
    for rel, path in code_files(root):
        h.update(rel.encode() + b"\0" + sha256_file(path).encode() + b"\0")
    return h.hexdigest()[:16]


def build_bundle(slug: str, info: dict, root: Path = lib.ROOT) -> bytes:
    """The bundle zip for one job step.

    slug: the serial the step works on.
    info: what bundle.json carries besides the manifest: job, stage, steps, class.
    root: the repository root holding the code and library/.
    Returns the zip bytes. The manifest gives each shipped file the hash of the bytes the zip holds, also when the
    file changes during the call, and it leaves out a file that is gone by the time it is read. Raises
    ExchangeError when the serial folder is missing.
    """
    folder = root / "library" / slug
    if not (folder / "serial.yaml").is_file():
        raise ExchangeError(f"no serial {slug!r} in the library")
    files = manifest(folder)
    shipped = {rel for rel in files if not _matches(rel, NOT_SHIPPED)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, path in code_files(root):
            z.write(path, rel)
        for rel in sorted(shipped):
            # a job's result can replace the file between two reads of it, and its temporary file is gone by then
            try:
                data = (folder / rel).read_bytes()
            except FileNotFoundError:
                del files[rel]
                continue
            files[rel] = hashlib.sha256(data).hexdigest()
            z.writestr(f"library/{slug}/{rel}", data)
        meta = {**info, "slug": slug, "code": code_version(root),
                "manifest": {rel: {"sha256": h, "shipped": rel in shipped} for rel, h in files.items()}}
        z.writestr(BUNDLE_FILE, json.dumps(meta, ensure_ascii=False, indent=1))
    return buf.getvalue()


def changes(folder: Path, before: dict[str, dict]) -> dict[str, dict]:
    """Files under folder that are new or differ from the bundle manifest.

    folder: the worker's copy of the serial folder.
    before: the bundle manifest, {path: {"sha256", "shipped"}}.
    Returns {path: {"base": the app's hash when the bundle was made or None, "sha256": the new hash}}.
    Files the worker deleted are not reported.
    """
    out = {}
    for rel, digest in manifest(folder).items():
        base = (before.get(rel) or {}).get("sha256")
        if digest != base:
            out[rel] = {"base": base, "sha256": digest}
    return out


def build_result(folder: Path, info: dict, changed: dict[str, dict], media: list[str] | None = None) -> bytes:
    """The result zip: result.json with info and the change list, files/<path> for each changed file, and
    media/<mirror path> for each node path in media."""
    buf = io.BytesIO()
    kept = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel in sorted(changed):
            z.write(folder / rel, f"files/{rel}")
        for raw in sorted(set(media or [])):
            rel = mirror_rel(raw)
            if rel and Path(raw).is_file():
                z.write(raw, f"media/{rel}")
                kept.append(rel)
        z.writestr(RESULT_FILE, json.dumps({**info, "changes": changed, "media": kept}, ensure_ascii=False, indent=1))
    return buf.getvalue()


def read_result(data: bytes) -> tuple[dict, zipfile.ZipFile]:
    """result.json and the open archive. Raises ExchangeError on a damaged upload."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        info = json.loads(z.read(RESULT_FILE).decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise ExchangeError(f"the result upload is not a result archive: {e}") from None
    if not isinstance(info, dict) or not isinstance(info.get("changes"), dict):
        raise ExchangeError("result.json has no change list")
    for rel, change in info["changes"].items():
        if (not isinstance(change, dict) or not isinstance(change.get("sha256"), str)
                or not isinstance(change.get("base"), (str, type(None)))):
            raise ExchangeError(f"result.json lists {rel!r} without its hashes")
    media = info.get("media")
    if media is not None and (not isinstance(media, list) or not all(isinstance(m, str) for m in media)):
        raise ExchangeError("result.json has a media list that is not a list of paths")
    return info, z


def apply_result(slug: str, info: dict, z: zipfile.ZipFile, *, success: bool, conflicts_dir: Path,
                 media_dir: Path | None = None, root: Path = lib.ROOT) -> dict:
    """Writes a worker's changed files into the serial's library folder.

    slug: the serial the job worked on.
    info, z: what read_result returned.
    success: the job succeeded. A failed or stopped job writes back only resumable files, such as Breeze takes lists.
    conflicts_dir: where a file goes when the app's copy changed after the bundle was made.
    media_dir: the media mirror. None skips the result's media.
    root: the repository root holding library/.
    Returns {"applied", "unchanged", "conflicts", "held"}, each a list of paths, and "media", the number of media
    files written. Writes each file atomically.
    Raises ExchangeError on a bad path or a file whose bytes do not match its hash, before writing anything.
    """
    # Check every file before writing any
    folder = (root / "library" / slug).resolve()
    plan = []
    for rel, change in sorted(info["changes"].items()):
        check_rel(rel)
        target = (folder / rel).resolve()
        if not target.is_relative_to(folder) or target == folder:
            raise ExchangeError(f"bad path {rel!r}")
        try:
            data = z.read(f"files/{rel}")
        except KeyError:
            raise ExchangeError(f"{rel} is listed but missing from the archive") from None
        if hashlib.sha256(data).hexdigest() != change.get("sha256"):
            raise ExchangeError(f"{rel} does not match its hash")
        plan.append((rel, change, target, data))

    # Library files
    report = {"applied": [], "unchanged": [], "conflicts": [], "held": []}
    for rel, change, target, data in plan:
        if not success and not _matches(rel, RESUMABLE):
            report["held"].append(rel)
            continue
        current = sha256_file(target) if target.is_file() else None
        if current == change["sha256"]:
            report["unchanged"].append(rel)
            continue
        if current != change.get("base"):
            keep = conflicts_dir / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_bytes(data)
            report["conflicts"].append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".incoming")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        report["applied"].append(rel)

    # Media mirror
    report["media"] = 0
    if media_dir is not None:
        for rel in info.get("media") or []:
            if mirror_rel(rel) != rel or os.path.splitext(rel)[1].lower() not in MEDIA_SUFFIXES:
                continue
            try:
                data = z.read(f"media/{rel}")
            except KeyError:
                continue
            target = media_dir / rel
            if target.is_file() and sha256_file(target) == hashlib.sha256(data).hexdigest():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".incoming")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            report["media"] += 1
    return report
