"""Runs one pipeline command for the web app and records how it ended.

Usage: python -m serial_audio.web.runjob <exit file> <log file> -- <argv...>
Appends the command's output to the log file and writes {"code", "at"} to the exit file when it ends.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys


def main() -> int:
    exit_path, log_path = sys.argv[1], sys.argv[2]
    argv = sys.argv[sys.argv.index("--") + 1:]
    with open(log_path, "ab") as log:
        log.write(f"$ python {subprocess.list2cmdline(argv[1:])}\n".encode("utf-8"))
        log.flush()
        try:
            code = subprocess.call(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except OSError as e:
            log.write(f"could not start: {e}\n".encode("utf-8"))
            code = 127
    tmp = exit_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"code": code, "at": _dt.datetime.now().astimezone().isoformat(timespec="seconds")}, fh)
    os.replace(tmp, exit_path)
    return code


if __name__ == "__main__":
    sys.exit(main())
