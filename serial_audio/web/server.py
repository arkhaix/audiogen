"""The web app's HTTP server: JSON API, audio with byte ranges, uploads and the static front end.

Requests must name an allowed host. POSTs must carry the X-Serial-Audio header and, when the browser sends an
Origin, an allowed origin. That keeps other web pages in a local browser from driving the API.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import traceback
import unicodedata
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import library as lib
from ..render import COMFY_OUTPUT
from . import views
from .jobs import FINISHED, INCOMING, JobManager, read_tail
from .nomad import WorkerError

STATIC = Path(__file__).with_name("static")
MAX_UPLOAD = 300 * 1024 * 1024
MAX_MEDIA = 64 * 1024 * 1024
MAX_JSON = 1024 * 1024
RECENT_FINISHED = 40
CSRF_HEADER = "X-Serial-Audio"
KEY_HEADER = "X-Serial-Audio-Key"
NODE_HEADER = "X-Serial-Audio-Node"
STAGE_HEADER = "X-Serial-Audio-Stage"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
         ".opus": "audio/ogg; codecs=opus", ".flac": "audio/flac", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
         ".json": "application/json", ".txt": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8"}


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _host_name(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "serial_audio_web/0.1"
    timeout = 60
    manager: JobManager
    mirror = None
    allowed_names: set[str] = set()
    machine_name = ""

    # Plumbing

    def log_message(self, fmt, *args):
        if self.command != "GET":
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def log_request(self, code="-", size="-"):
        # a worker sends a chapter's takes as some 250 requests
        if "/media/file" in (getattr(self, "path", None) or "") and str(getattr(code, "value", code)) == "200":
            return
        super().log_request(code, size)

    def _name_ok(self, name: str) -> bool:
        return name in self.allowed_names or bool(self.machine_name and name.startswith(self.machine_name + "."))

    def _check_request(self) -> None:
        if not self._name_ok(_host_name(self.headers.get("Host") or "")):
            raise HTTPError(421, "this address is not one the web app answers to (see --allow-host)")
        if self.command != "POST":
            return
        if self.headers.get(CSRF_HEADER) != "1":
            raise HTTPError(403, f"missing {CSRF_HEADER} header")
        origin = self.headers.get("Origin")
        if origin is not None and (origin == "null" or not self._name_ok(_host_name(urllib.parse.urlsplit(origin).netloc))):
            raise HTTPError(403, "cross-site request refused")

    def _send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self, limit: int) -> bytes:
        """The whole request body, up to limit bytes. Raises HTTPError when it is larger or cut short."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            self.close_connection = True
            raise HTTPError(413, "the request body is too large")
        chunks, left = [], length
        while left > 0:
            chunk = self.rfile.read(min(262_144, left))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        self._body = b"".join(chunks)
        if left:
            self.close_connection = True
            raise HTTPError(400, "the request body was cut short")
        return self._body

    def raw_body(self) -> bytes:
        if self._body is None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_JSON:
                self.close_connection = True
                raise HTTPError(413, "the request body is too large")
            self._body = self.rfile.read(length) if length > 0 else b""
        return self._body

    def body_json(self) -> dict:
        raw = self.raw_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise HTTPError(400, "the request body is not JSON") from None
        if not isinstance(data, dict):
            raise HTTPError(400, "the request body must be a JSON object")
        return data

    def _drain(self) -> None:
        """Reads an unread request body so the next request on the connection starts at its own first byte."""
        if self._body is not None:
            return
        length = int(self.headers.get("Content-Length") or 0)
        if 0 < length <= MAX_JSON:
            self._body = self.rfile.read(length)
        elif length > MAX_JSON:
            self.close_connection = True

    def _dispatch(self, routes) -> None:
        self._body = None
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            self._check_request()
            for pattern, fn in routes:
                m = re.fullmatch(pattern, path)
                if m:
                    fn(self, query, *m.groups())
                    break
            else:
                raise HTTPError(404, f"no route for {self.command} {path}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            self.close_connection = True
            return
        except HTTPError as e:
            self._error(e.status, str(e))
        except ValueError as e:
            self._error(400, str(e))
        except (KeyError, FileNotFoundError) as e:
            self._error(404, f"not found: {e}")
        except Exception as e:
            traceback.print_exc()
            self._error(500, f"{type(e).__name__}: {e}")
        if self.command == "POST":
            try:
                self._drain()
            except OSError:
                self.close_connection = True

    def _error(self, status: int, message: str) -> None:
        try:
            self._send_json({"error": message}, status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            self.close_connection = True

    def do_GET(self):
        self._dispatch(GET_ROUTES)

    def do_POST(self):
        self._dispatch(POST_ROUTES)

    # Files

    def send_file(self, path: Path, cache: bool = False, download: str | None = None) -> None:
        if not path.is_file():
            raise HTTPError(404, f"no file {path.name}")
        st = path.stat()
        size = st.st_size
        etag = f'"{int(st.st_mtime)}-{size}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        self.send_response(status)
        self.send_header("Content-Type", TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-cache")
        if download:
            self.send_header("Content-Disposition", content_disposition(download))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(262_144, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def _under(root: Path, rel: str, suffixes: set[str] | None = None) -> Path:
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()):
        raise HTTPError(403, "outside the media folder")
    if suffixes is not None and path.suffix.lower() not in suffixes:
        raise HTTPError(403, f"{path.suffix} files are not served")
    return path


def content_disposition(name: str) -> str:
    """An attachment header value that saves the file as name, with an ASCII name for clients without RFC 5987."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    plain = re.sub(r'["\\\x00-\x1f]', "", plain).strip() or "download"
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


def safe_name(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/")).strip().strip(".")
    cleaned = "".join(ch for ch in base if ch not in '<>:"/\\|?*' and ord(ch) >= 32)
    return cleaned[:180] or "upload"


# GET

def get_index(h: Handler, q):
    h.send_file(STATIC / "index.html")


def get_static(h: Handler, q, rel):
    h.send_file(_under(STATIC, rel))


def get_state(h: Handler, q):
    snap = h.manager.snapshot()
    active = [j for j in snap["jobs"] if j["status"] not in FINISHED]
    finished = [j for j in snap["jobs"] if j["status"] in FINISHED]
    snap["finished_total"] = len(finished)
    snap["jobs"] = active + (finished if q.get("all") else finished[:RECENT_FINISHED])
    if h.mirror is not None:
        snap["mirror"] = h.mirror.status()
    h._send_json(snap)


def get_serials(h: Handler, q):
    h._send_json({"serials": views.serials()})


def get_serial(h: Handler, q, slug):
    h._send_json(views.serial_detail(slug, h.manager.minutes_per_line()))


def get_chapter(h: Handler, q, slug, n):
    data = views.chapter_detail(slug, int(n))
    ours = h.manager.latest_log("render", slug, int(n))
    outside = lib.chapter_dir(slug, int(n)) / "render.log"
    try:
        outside_mtime = outside.stat().st_mtime
    except OSError:
        outside_mtime = None
    if ours and (outside_mtime is None or ours[1] > outside_mtime):
        data["render_log"] = read_tail(ours[0], 40_000)
    h._send_json(data)


def get_voices(h: Handler, q, slug):
    h._send_json(views.voices(slug))


def get_bible(h: Handler, q, slug):
    h._send_json(views.bible(slug))


def get_log(h: Handler, q, jid):
    h._send_json(h.manager.read_log(jid, int(q.get("offset") or 0)))


def get_lib_media(h: Handler, q, rel):
    h.send_file(_under(lib.LIBRARY, rel, views.AUDIO_SUFFIXES))


def get_download(h: Handler, q, slug, n):
    path, name = views.chapter_download(slug, int(n))
    h.send_file(path, download=name)


def get_comfy_media(h: Handler, q, rel):
    h.send_file(_under(COMFY_OUTPUT, rel, views.AUDIO_SUFFIXES), cache=True)


def get_mirror_media(h: Handler, q, rel):
    if views.MEDIA_MIRROR is None:
        raise HTTPError(404, "this web app keeps no media mirror")
    h.send_file(_under(views.MEDIA_MIRROR, rel, views.AUDIO_SUFFIXES), cache=True)


# POST

def post_jobs(h: Handler, q):
    body = h.body_json()
    requests = body.get("jobs")
    if not isinstance(requests, list) or not requests:
        raise HTTPError(400, "give a list of jobs")
    jobs = h.manager.submit(requests, stop_on_failure=body.get("stop_on_failure", True) is not False)
    h._send_json({"jobs": jobs})


def post_cancel(h: Handler, q, jid):
    h._send_json({"job": h.manager.cancel(jid)})


def post_move(h: Handler, q, jid):
    direction = h.body_json().get("direction")
    if direction not in ("up", "down"):
        raise HTTPError(400, "direction must be up or down")
    h.manager.move(jid, direction)
    h._send_json({"ok": True})


def post_retry(h: Handler, q, jid):
    h._send_json({"job": h.manager.retry(jid)})


def post_clear(h: Handler, q):
    h._send_json({"cleared": h.manager.clear_finished()})


def post_pause(h: Handler, q):
    paused = h.body_json().get("paused")
    if not isinstance(paused, bool):
        raise HTTPError(400, "paused must be true or false")
    h.manager.set_paused(paused)
    h._send_json({"paused": h.manager.paused})


def post_upload(h: Handler, q):
    slug = q.get("slug") or None
    if slug and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", slug):
        raise HTTPError(400, "bad slug")
    name = safe_name(q.get("name") or "")
    length = int(h.headers.get("Content-Length") or 0)
    if length <= 0:
        raise HTTPError(400, "empty upload")
    if length > MAX_UPLOAD:
        h.close_connection = True
        raise HTTPError(413, "the file is too large")
    folder = INCOMING / (slug or "_new")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    tmp = path.with_name(path.name + ".part")
    left = length
    h._body = b""
    with tmp.open("wb") as fh:
        while left > 0:
            chunk = h.rfile.read(min(262_144, left))
            if not chunk:
                break
            fh.write(chunk)
            left -= len(chunk)
    if left:
        tmp.unlink(missing_ok=True)
        h.close_connection = True
        raise HTTPError(400, "the upload was cut short")
    tmp.replace(path)
    h._send_json({"name": path.name, "bytes": length})


# The cluster workers

def _worker_manager(h: Handler):
    if not hasattr(h.manager, "worker_bundle"):
        raise HTTPError(404, "this web app does not run cluster jobs")
    return h.manager


def _worker_call(fn):
    try:
        return fn()
    except WorkerError as e:
        raise HTTPError(e.status, str(e)) from None


def get_worker_bundle(h: Handler, q, jid):
    m = _worker_manager(h)
    data = _worker_call(lambda: m.worker_bundle(jid, h.headers.get(KEY_HEADER) or "", h.headers.get(NODE_HEADER) or ""))
    h.send_bytes(data, "application/zip")


def post_worker_log(h: Handler, q, jid):
    m = _worker_manager(h)
    try:
        offset = int(q.get("offset") or 0)
    except ValueError:
        raise HTTPError(400, "offset must be a whole number") from None
    data = h.raw_body()
    h._send_json(_worker_call(lambda: m.worker_log(jid, h.headers.get(KEY_HEADER) or "", h.headers.get(STAGE_HEADER),
                                                   offset, data)))


def post_worker_result(h: Handler, q, jid):
    m = _worker_manager(h)
    data = h.read_body(MAX_UPLOAD)
    h._send_json(_worker_call(lambda: m.worker_result(jid, h.headers.get(KEY_HEADER) or "", h.headers.get(STAGE_HEADER),
                                                      data)))


def post_worker_media_have(h: Handler, q, jid):
    m = _worker_manager(h)
    paths = h.body_json().get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise HTTPError(400, "give a list of media mirror paths")
    h._send_json(_worker_call(lambda: m.worker_media_have(jid, h.headers.get(KEY_HEADER) or "", paths)))


def post_worker_media_file(h: Handler, q, jid):
    m = _worker_manager(h)
    length = int(h.headers.get("Content-Length") or 0)
    if length <= 0:
        raise HTTPError(400, "empty upload")
    if length > MAX_MEDIA:
        h.close_connection = True
        raise HTTPError(413, "the file is too large")
    target = _worker_call(lambda: m.worker_media_target(jid, h.headers.get(KEY_HEADER) or "", q.get("path") or ""))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{threading.get_ident()}.part")
    left = length
    h._body = b""
    with tmp.open("wb") as fh:
        while left > 0:
            chunk = h.rfile.read(min(262_144, left))
            if not chunk:
                break
            fh.write(chunk)
            left -= len(chunk)
    if left:
        tmp.unlink(missing_ok=True)
        h.close_connection = True
        raise HTTPError(400, "the upload was cut short")
    tmp.replace(target)
    m.worker_media_stored()
    h._send_json({"bytes": length})


SLUG = r"([a-z0-9][a-z0-9-]*)"
JOB = r"([0-9]{8}-[0-9]{6}-[0-9a-f]{6})"
GET_ROUTES = [
    (r"/", get_index),
    (r"/static/(.+)", get_static),
    (r"/api/state", get_state),
    (r"/api/serials", get_serials),
    (rf"/api/serials/{SLUG}", get_serial),
    (rf"/api/serials/{SLUG}/chapters/(\d+)", get_chapter),
    (rf"/api/serials/{SLUG}/voices", get_voices),
    (rf"/api/serials/{SLUG}/bible", get_bible),
    (rf"/api/jobs/{JOB}/log", get_log),
    (r"/media/lib/(.+)", get_lib_media),
    (rf"/download/{SLUG}/(\d+)", get_download),
    (r"/media/comfy/(.+)", get_comfy_media),
    (r"/media/mirror/(.+)", get_mirror_media),
    (rf"/api/worker/{JOB}/bundle", get_worker_bundle),
]
POST_ROUTES = [
    (r"/api/jobs", post_jobs),
    (rf"/api/jobs/{JOB}/cancel", post_cancel),
    (rf"/api/jobs/{JOB}/move", post_move),
    (rf"/api/jobs/{JOB}/retry", post_retry),
    (r"/api/jobs/clear", post_clear),
    (r"/api/queue/pause", post_pause),
    (r"/api/upload", post_upload),
    (rf"/api/worker/{JOB}/log", post_worker_log),
    (rf"/api/worker/{JOB}/result", post_worker_result),
    (rf"/api/worker/{JOB}/media/have", post_worker_media_have),
    (rf"/api/worker/{JOB}/media/file", post_worker_media_file),
]


class Server(ThreadingHTTPServer):
    """Refuses to share its port, so a second instance fails to start instead of binding beside the first.
    Restarts right away on POSIX, where the old instance's closed connections do not hold the port."""

    # On Windows SO_REUSEADDR lets a second listener share the port. Elsewhere it only skips TIME_WAIT.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(host: str, port: int, manager: JobManager, allow_hosts: list[str] | None = None, mirror=None) -> None:
    """Serves the app until interrupted.

    host, port: the address to listen on.
    manager: the job manager the API drives.
    allow_hosts: extra host names browsers may use to reach the app.
    mirror: a mirror.Mirror to run beside the app, or None.
    """
    Handler.manager = manager
    Handler.mirror = mirror
    if getattr(manager, "media_dir", None) is not None:
        views.MEDIA_MIRROR = manager.media_dir
    Handler.machine_name = socket.gethostname().lower()
    Handler.allowed_names = {"127.0.0.1", "localhost", "[::1]", host.lower(), Handler.machine_name,
                             *(h.lower() for h in allow_hosts or [])}
    httpd = Server((host, port), Handler)
    manager.start()
    if mirror is not None:
        mirror.start()
    print(f"serial_audio web app: http://{host}:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if mirror is not None:
            mirror.stop()
        manager.stop()
        httpd.server_close()
