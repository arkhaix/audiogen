"""python -m serial_audio.web [--host 127.0.0.1] [--port 8765] [--comfy-url URL] [--allow-host NAME]
                             [--executor local|nomad] [--nomad-addr URL] [--nomad-token-file PATH] [--state-dir DIR]
                             [--backup-dir DIR] [--publish-dir DIR] [--publish-skip SLUG]"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .. import library as lib
from .jobs import COMFY_URL, INCOMING, STATE_DIR, JobManager, StateInUse
from .mirror import MARKER, Mirror
from .server import serve


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m serial_audio.web", description="serial_audio web app")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on. Use the Tailscale address to reach it from your other devices. "
                         "The app has no login, so avoid 0.0.0.0 on a laptop that joins other networks.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--comfy-url", default=COMFY_URL, help="ComfyUI for render and cast jobs and for the status panel")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="another host name browsers use to reach the app, such as a MagicDNS name (repeatable)")
    ap.add_argument("--state-dir", type=Path, default=STATE_DIR, help="where job records and logs live")
    ap.add_argument("--executor", choices=["local", "nomad"], default="local",
                    help="local runs every job on this machine. nomad dispatches render, cast and Claude jobs to the "
                         "Nomad cluster and runs only quick library jobs here")
    ap.add_argument("--nomad-addr", default=os.environ.get("NOMAD_ADDR") or "http://containers.goblin-truck.ts.net:4646")
    ap.add_argument("--nomad-token-file", help="a file holding the dispatch token. NOMAD_TOKEN in the environment wins")
    ap.add_argument("--nomad-namespace", default="serial-audio")
    ap.add_argument("--nomad-prefix", default="serial-audio", help="the registered jobs are <prefix>-breeze, -comfyui, -llm")
    ap.add_argument("--backup-dir", type=Path,
                    help=f"a folder on another disk for a daily backup of the library. It must hold a file named {MARKER}")
    ap.add_argument("--publish-dir", type=Path,
                    help="a folder that gets every rendered chapter as <serial title>/<NNN> - <chapter title>.opus, "
                         "for an audiobook player that reads one folder per book. It must already hold something, "
                         "which shows that its disk is mounted")
    ap.add_argument("--publish-skip", action="append", default=[], metavar="SLUG",
                    help="a serial to keep out of --publish-dir (repeatable)")
    args = ap.parse_args()
    try:
        if args.executor == "nomad":
            from .nomad import NomadClient, NomadJobManager, read_token
            client = NomadClient(args.nomad_addr, read_token(args.nomad_token_file), args.nomad_namespace)
            manager = NomadJobManager(client, prefix=args.nomad_prefix, comfy_url=args.comfy_url,
                                      state_dir=args.state_dir, autostart=False)
        else:
            manager = JobManager(comfy_url=args.comfy_url, state_dir=args.state_dir, autostart=False)
    except OSError as e:
        print(f"serial_audio web app: {e}", file=sys.stderr)
        return 1
    except StateInUse as e:
        print(f"serial_audio web app: {e}", file=sys.stderr)
        return 1
    mirror = None
    if args.backup_dir or args.publish_dir:
        mirror = Mirror(lib.LIBRARY, args.backup_dir, args.publish_dir, args.publish_skip,
                        extra={"incoming": INCOMING, "state/placement.json": args.state_dir / "placement.json"})
    try:
        serve(args.host, args.port, manager, args.allow_host, mirror)
    except OSError as e:
        print(f"serial_audio web app: cannot listen on {args.host}:{args.port}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
