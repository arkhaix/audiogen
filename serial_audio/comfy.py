"""A small client for ComfyUI's HTTP API and the graphs this pipeline queues.

Graphs are in the API format (`{node_id: {"class_type", "inputs"}}`), not the UI's workflow JSON.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class ComfyError(RuntimeError):
    pass


class _ProgressWatch(threading.Thread):
    """Listens on ComfyUI's websocket (as the client that queued the prompt, which is who progress events
    go to) and keeps the latest progress bar value. A node that works in chunks resets the bar per chunk;
    the rate and the time left refer to the current chunk. Standard library only; failures are silent
    because progress is a courtesy, not a result."""

    def __init__(self, base_url: str, client_id: str):
        super().__init__(daemon=True)
        self.url = urllib.parse.urlparse(base_url)
        self.client_id = client_id
        self.lock = threading.Lock()
        self.latest: tuple[int, int, float] | None = None   # value, max, time
        self.segment_start: tuple[int, float] | None = None  # value, time at the start of the current chunk
        self.chunk = 0
        self.node = ""
        self._stopped = threading.Event()

    def stop(self):
        self._stopped.set()

    def run(self):
        try:
            sock = socket.create_connection((self.url.hostname, self.url.port or 80), timeout=10)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((f"GET /ws?clientId={self.client_id} HTTP/1.1\r\nHost: {self.url.netloc}\r\n"
                          f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                          f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, buf = buf.split(b"\r\n\r\n", 1)
            if b" 101 " not in head.split(b"\r\n")[0]:
                return
            sock.settimeout(2.0)

            def recv_exact(n: int) -> bytes:
                nonlocal buf
                while len(buf) < n:
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("closed")
                    buf += chunk
                out, buf = buf[:n], buf[n:]
                return out

            while not self._stopped.is_set():
                try:
                    b1, b2 = recv_exact(2)
                except socket.timeout:
                    continue
                opcode, length = b1 & 0x0F, b2 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", recv_exact(8))[0]
                mask = recv_exact(4) if b2 & 0x80 else None
                data = recv_exact(length)
                if mask:
                    data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
                if opcode == 8:
                    return
                if opcode != 1:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except ValueError:
                    continue
                if msg.get("type") == "progress":
                    d = msg.get("data") or {}
                    value, maximum = d.get("value"), d.get("max")
                    if value is None or maximum is None:
                        continue
                    now = time.time()
                    with self.lock:
                        if self.latest is None or value < self.latest[0]:
                            self.chunk += 1
                            self.segment_start = (value, now)
                        self.latest = (value, maximum, now)
                        self.node = str(d.get("node") or "")
            try:
                sock.close()
            except OSError:
                pass
        except (OSError, ConnectionError, ValueError):
            return

    def describe(self) -> str:
        with self.lock:
            if self.latest is None:
                return "no progress reported yet (loading?)"
            value, maximum, t = self.latest
            start_value, start_t = self.segment_start
            text = f"chunk {self.chunk}: step {value}/{maximum}"
            if value > start_value and t > start_t:
                per = (t - start_t) / (value - start_value)
                text += f", {per * 1000:.0f} ms/step, ~{(maximum - value) * per / 60:.0f} min left in this chunk"
            return text


class ComfyClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8188", output_dir: Path | None = None):
        self.base_url = base_url.rstrip("/")
        self.client_id = uuid.uuid4().hex
        self.output_dir = output_dir

    def _get(self, path: str, timeout: float = 30):
        try:
            with urllib.request.urlopen(f"{self.base_url}{path}", timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise ComfyError(f"ComfyUI not reachable at {self.base_url}: {e}") from e

    def _post(self, path: str, body: dict, timeout: float = 30):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}   # /free answers with an empty body
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise ComfyError(f"ComfyUI rejected the request ({e.code}): {detail[:2000]}") from e
        except urllib.error.URLError as e:
            raise ComfyError(f"ComfyUI not reachable at {self.base_url}: {e}") from e

    def system_stats(self) -> dict:
        return self._get("/system_stats")

    def queue(self, graph: dict) -> str:
        res = self._post("/prompt", {"prompt": graph, "client_id": self.client_id})
        if res.get("node_errors"):
            raise ComfyError(f"graph validation failed: {json.dumps(res['node_errors'])[:2000]}")
        return res["prompt_id"]

    def wait(self, prompt_id: str, timeout: float = 900, poll: float = 2.0, log=None,
             report_every: float = 600.0) -> dict:
        """Block until the prompt finishes; returns the history entry (outputs + status).
        With `log`, a progress line (rate, time left) is logged every `report_every` seconds."""
        start = time.time()
        watch = None
        if log is not None:
            watch = _ProgressWatch(self.base_url, self.client_id)
            watch.start()
        last_report = start
        try:
            while True:
                hist = self._get(f"/history/{prompt_id}")
                entry = hist.get(prompt_id)
                if entry and entry.get("status", {}).get("completed", False):
                    return entry
                if entry and entry.get("status", {}).get("status_str") == "error":
                    msgs = [m for m in entry["status"].get("messages", []) if m[0] == "execution_error"]
                    detail = msgs[-1][1] if msgs else entry["status"]
                    raise ComfyError(f"ComfyUI execution error: {json.dumps(detail)[:3000]}")
                now = time.time()
                if now - start > timeout:
                    raise ComfyError(f"timed out after {timeout:.0f}s waiting for prompt {prompt_id}")
                if watch is not None and now - last_report >= report_every:
                    last_report = now
                    log(f"   [{(now - start) / 60:.0f} min] {watch.describe()}")
                time.sleep(poll)
        finally:
            if watch is not None:
                watch.stop()

    def run(self, graph: dict, timeout: float = 900, log=None, report_every: float = 600.0) -> dict:
        return self.wait(self.queue(graph), timeout=timeout, log=log, report_every=report_every)

    def free(self, settle: float = 2.0) -> None:
        """Ask ComfyUI to unload every model and empty the CUDA cache; the queue worker applies the
        flags when idle, so give it a moment before queueing the next prompt."""
        self._post("/free", {"unload_models": True, "free_memory": True})
        time.sleep(settle)

    @staticmethod
    def output_ref(item: dict) -> str:
        """A saved output as a LoadAudio/LoadImage path: '<subfolder>/<filename> [output]'."""
        sub = item.get("subfolder", "").replace("\\", "/").strip("/")
        return f"{sub + '/' if sub else ''}{item['filename']} [output]"

    def output_path(self, item: dict) -> Path:
        """Resolve a {filename, subfolder, type} output item to a path under the ComfyUI output folder."""
        if self.output_dir is None:
            raise ComfyError("output_dir not configured")
        return self.output_dir / item.get("subfolder", "") / item["filename"]


#
# The casting graphs are split in three so that only one large model is on the 8 GB card at a time.
# Running Voice Design, ASR, and the Drama 3 codec in one graph made ComfyUI load the codec partially,
# which its code does not survive ("Expected all tensors to be on the same device").
#

def design_graph(*, text: str, instruct: str, seed: int, clip_prefix: str, language: str = "English") -> dict:
    """Qwen3-TTS Voice Design -> clip saved under the output folder."""
    return {
        "design": {"class_type": "FB_Qwen3TTSVoiceDesign", "inputs": {
            "text": text, "instruct": instruct, "model_choice": "1.7B", "device": "auto", "precision": "bf16",
            "language": language, "seed": seed, "max_new_tokens": 2048, "top_p": 1.0, "top_k": 50,
            "temperature": 0.9, "repetition_penalty": 1.05, "attention": "auto", "unload_model_after_generate": True,
        }},
        "save_clip": {"class_type": "SaveAudio", "inputs": {"audio": ["design", 0], "filename_prefix": clip_prefix}},
    }


def transcribe_graph(*, clip_ref: str, asr_model: str = "qwen3_asr_1.7b_bf16.safetensors") -> dict:
    """Saved clip -> Qwen3-ASR -> transcript readable from the history outputs."""
    return {
        "clip": {"class_type": "LoadAudio", "inputs": {"audio": clip_ref}},
        "asr_model": {"class_type": "Qwen3ASRLoader", "inputs": {"model_name": asr_model}},
        "asr": {"class_type": "Qwen3ASRTranscribe", "inputs": {
            "model": ["asr_model", 0], "audio": ["clip", 0], "language": "auto", "context": "", "max_new_tokens": 4096,
        }},
        "transcript": {"class_type": "PreviewAny", "inputs": {"source": ["asr", 0]}},
    }


def encode_voice_graph(*, clip_ref: str, transcript: str, voice_name: str,
                       codec_name: str = "drama3_codec.safetensors") -> dict:
    """Saved clip -> Drama 3 Encode Voice -> Save Drama 3 Voice."""
    return {
        "clip": {"class_type": "LoadAudio", "inputs": {"audio": clip_ref}},
        "codec": {"class_type": "Drama3CodecLoader", "inputs": {"codec_name": codec_name}},
        "encode": {"class_type": "Drama3EncodeVoice", "inputs": {"codec": ["codec", 0], "audio": ["clip", 0], "transcript": transcript}},
        "save_voice": {"class_type": "Drama3SaveVoice", "inputs": {"voice": ["encode", 0], "name": voice_name}},
    }
