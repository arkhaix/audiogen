"""The serial-audio task. serial-audio.nomad.hcl embeds this file and runs it with the node's Python.

Fetches the job's bundle from the web app, unpacks it into the task folder and runs the worker it contains, so a
node runs the code the app runs. Exits 0 without doing anything when the app stopped the job before it started.
"""
import io
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

TRIES = 12


def fetch(url, headers):
    """The bundle bytes, None when the job no longer wants to run. Raises SystemExit when the app refuses it."""
    req = urllib.request.Request(url, headers=headers)
    problem = None
    for attempt in range(TRIES):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 410:
                return None
            if e.code < 500:
                body = e.read()[:500].decode("utf-8", "replace")
                print(f"the web app refused the bundle: {e.code} {body}", file=sys.stderr, flush=True)
                raise SystemExit(2)
            problem = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            problem = str(getattr(e, "reason", e))
        print(f"bundle not fetched ({problem}), retry {attempt + 1} of {TRIES}", file=sys.stderr, flush=True)
        time.sleep(min(60, 5 * (attempt + 1)))
    print(f"gave up fetching the bundle: {problem}", file=sys.stderr, flush=True)
    raise SystemExit(3)


def main():
    app = os.environ["SERIAL_AUDIO_APP"].rstrip("/")
    job, key = os.environ["NOMAD_META_job"], os.environ["NOMAD_META_key"]
    node = os.environ.get("SERIAL_AUDIO_NODE", "")
    work = os.path.join(os.environ["NOMAD_TASK_DIR"], "work")
    headers = {"X-Serial-Audio": "1", "X-Serial-Audio-Key": key, "X-Serial-Audio-Node": node}
    data = fetch(f"{app}/api/worker/{job}/bundle", headers)
    if data is None:
        print(f"job {job} was stopped in the web app before it started", flush=True)
        return 0
    zipfile.ZipFile(io.BytesIO(data)).extractall(work)
    sys.path.insert(0, work)
    from serial_audio import worker
    return worker.main(app, job, key, work)


if __name__ == "__main__":
    sys.exit(main())
