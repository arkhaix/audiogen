"""A read-only stand-in for the web app, for checking a change to the page against the live app's data.

    python tests/page_proxy.py serial_audio/web/static https://audiogen.grayfam.us 8790

It serves the static files of the folder, passes every GET on to the live app and refuses every POST, so nothing
a page does here can queue a job or change the library. It keeps each answer under /api/serials after the first
fetch, so a test run costs the live app one request a page.
"""
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json"}
PASSED_ON = ("Content-Type", "Content-Range", "Accept-Ranges", "ETag")
KEPT: dict[str, bytes] = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    static: Path
    live: str

    def log_message(self, fmt, *args):
        if self.command != "GET":
            sys.stderr.write("%s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel: str) -> None:
        path = (self.static / rel).resolve()
        if self.static.resolve() not in path.parents or not path.is_file():
            self._send(404, b"", {})
            return
        self._send(200, path.read_bytes(), {"Content-Type": TYPES.get(path.suffix, "application/octet-stream")})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._static("index.html")
            return
        if path.startswith("/static/"):
            self._static(path[len("/static/"):])
            return
        if self.path in KEPT:
            self._send(200, KEPT[self.path], {"Content-Type": "application/json; charset=utf-8"})
            return
        request = urllib.request.Request(self.live + self.path, headers={"User-Agent": "page-proxy"})
        if self.headers.get("Range"):
            request.add_header("Range", self.headers["Range"])
        try:
            answer = urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as e:
            answer = e
        with answer:
            body = answer.read()
            status = answer.status if hasattr(answer, "status") else answer.code
            if status == 200 and self.path.startswith("/api/serials"):
                KEPT[self.path] = body
            self._send(status, body, {n: answer.headers[n] for n in PASSED_ON if answer.headers.get(n)})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._send(403, b'{"error": "the page proxy refuses every POST"}', {"Content-Type": "application/json"})
        sys.stderr.write("refused POST %s\n" % self.path)


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    Handler.static = Path(sys.argv[1])
    Handler.live = sys.argv[2].rstrip("/")
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[3])), Handler).serve_forever()


if __name__ == "__main__":
    main()
