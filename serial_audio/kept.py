"""The processes a cluster worker keeps up between the jobs it runs back to back: breeze-server and the recognizer.

A worker that holds the machine's GPU lock names a registry file in SERIAL_AUDIO_KEEP for its steps. A step that
starts breeze-server or the recognizer under that variable records the process there and leaves it running when the
step ends, and the next step uses it. The worker ends every recorded process when the app has no further job for it,
when something outside the job wants the card, and when it finds one that an earlier worker left behind. On Windows
a kept process also joins a job object the worker holds, so it ends with the worker however the worker ends.

The registry is {"owner": the worker's process id, "job": the name of its Windows job object or None, "kept": {name:
{"pid", "born", "port", ...}}}. born is when the process started, which tells it from a later process with its id.
Standard library only.
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from . import machine

ENV = "SERIAL_AUDIO_KEEP"
WINDOWS = os.name == "nt"
END_GRACE = 10.0
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_ASSIGN_PROCESS = 0x0001
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.OpenJobObjectW.restype = wintypes.HANDLE
    k32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def registry() -> Path | None:
    """The registry file this process keeps processes in, or None when no worker keeps any for it."""
    value = os.environ.get(ENV, "").strip()
    return Path(value) if value else None


def load(path: Path) -> dict:
    """The registry at path, with a dict under "kept". Never raises: a file that is missing or does not parse is
    an empty registry."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("kept"), dict):
        data["kept"] = {}
    return data


def _save(path: Path, data: dict) -> None:
    tmp = Path(path).with_name(Path(path).name + ".tmp")
    # the registry holds the recognizer's token, and on macOS and Linux only its owner reads it
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=1))
    os.replace(tmp, path)


def recorded(name: str) -> dict | None:
    """What this process's registry records under name, while that process runs.

    name: "breeze" or "asr".
    Returns the entry, with its "pid" and "port", or None: without a registry, without an entry, or when the
    process is gone. Never raises.
    """
    path = registry()
    if path is None:
        return None
    found = load(path)["kept"].get(name)
    return found if runs(found) else None


def entry(name: str) -> dict | None:
    """recorded(name), when the process also accepts a connection on its port, so a step can use it."""
    found = recorded(name)
    usable = found and 0 < found["port"] < 65536 and machine.listening(f"http://127.0.0.1:{found['port']}")
    return found if usable else None


def runs(found) -> bool:
    """Whether the process a registry entry records still runs: its id is a process that started when the entry
    says. Never raises."""
    if not isinstance(found, dict) or any(type(found.get(k)) is not int for k in ("pid", "port")):
        return False
    return alive(found["pid"]) and born(found["pid"]) == found.get("born")


def record(name: str, pid: int, port: int, **facts) -> bool:
    """Hands a process this process started to the worker, to keep up after this process ends.

    name: "breeze" or "asr".
    pid: the process.
    port: the port of this machine it listens on.
    facts: what else a later step needs to use it.
    Returns whether the worker keeps it. False without a registry, and on Windows when the process could not join
    the worker's job object, so the caller stops the process itself. Never raises.
    """
    path = registry()
    if path is None:
        return False
    try:
        data = load(path)
        data["kept"][name] = {"pid": pid, "born": born(pid), "port": port, **facts}
        json.dumps(data)
        if type(pid) is not int or type(port) is not int:
            return False
        if WINDOWS and not (isinstance(data.get("job"), str) and join_job(data["job"], pid)):
            return False
        _save(path, data)
        return True
    except (OSError, TypeError, ValueError):
        return False


def forget(name: str) -> None:
    """Drops name from this process's registry. Never raises."""
    path = registry()
    if path is None:
        return
    try:
        data = load(path)
        if data["kept"].pop(name, None) is not None:
            _save(path, data)
    except OSError:
        pass


def alive(pid: int) -> bool:
    """Whether a process with this id runs on this machine. On macOS and Linux an ended child of this process is
    waited for, so its exit status is gone afterwards. Never raises."""
    if type(pid) is not int or pid <= 0:
        return False
    if WINDOWS:
        k32 = _kernel32()
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        # a child of this process that has ended stays in the table until it is waited for
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    # an ended process that its parent has not waited for is still in the table
    return not _ps(pid, "stat").startswith("Z")


def _ps(pid: int, field: str) -> str:
    """One field of ps for a process on macOS or Linux, or "" when ps gives none. Never raises."""
    try:
        return subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def born(pid: int) -> str | None:
    """When a process started, as text that stays the same for as long as it runs, or None when nobody can say.
    Never raises."""
    if type(pid) is not int or pid <= 0:
        return None
    if not WINDOWS:
        return _ps(pid, "lstart") or None
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        k32.CloseHandle(handle)


def end(pid: int, grace: float = END_GRACE) -> bool:
    """Ends a process, and waits until it is gone.

    pid: the process.
    grace: seconds a process on macOS or Linux gets to end after SIGTERM before SIGKILL, and seconds to wait for a
        killed process to be gone.
    Returns whether it is gone. Never raises.
    """
    def gone(within: float) -> bool:
        deadline = time.time() + within
        while alive(pid):
            if time.time() > deadline:
                return False
            time.sleep(0.1)
        return True

    try:
        if not alive(pid):
            return True
        os.kill(pid, signal.SIGTERM)
        if WINDOWS or gone(grace):
            return gone(grace)
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return gone(grace)


def open_registry(path: Path) -> object | None:
    """Starts an empty registry at path for the worker that calls it.

    path: the registry file.
    Returns the handle of the Windows job object that kept processes join, which the worker holds until it ends, or
    None elsewhere and when Windows gave none. Raises OSError when the file cannot be written.
    """
    name, handle = None, None
    if WINDOWS:
        name = f"serial-audio-kept-{os.getpid()}"
        handle = hold_job(name)
        if handle is None:
            name = None
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _save(path, {"owner": os.getpid(), "job": name, "kept": {}})
    return handle


def end_all(path: Path) -> list[str]:
    """Ends every process the registry at path records.

    path: the registry file.
    Returns "<name> (pid <n>)" for each process that ran and was ended. An entry whose process is gone is dropped.
    A process that could not be ended stays in the registry, so a later call tries again. Never raises.
    """
    data = load(path)
    ended, left = [], {}
    for name, found in data["kept"].items():
        if not runs(found):
            continue
        if end(found["pid"]):
            ended.append(f"{name} (pid {found['pid']})")
        else:
            left[name] = found
    if data["kept"] != left:
        data["kept"] = left
        try:
            _save(path, data)
        except OSError:
            pass
    return ended


def hold_job(name: str | None = None):
    """A Windows job object that kills its processes when its last handle closes.

    name: a name other processes can open it by, or None for a job without one.
    Returns the handle, or None when Windows refused.
    """
    k32 = _kernel32()
    job = k32.CreateJobObjectW(None, name)
    if not job:
        return None
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                       ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return None
    return job


def join_job(name: str, pid: int) -> bool:
    """Puts a process into the Windows job object another process holds under name. Returns whether it joined."""
    k32 = _kernel32()
    job = k32.OpenJobObjectW(JOB_OBJECT_ASSIGN_PROCESS, False, name)
    if not job:
        return False
    try:
        proc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not proc:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(job, proc))
        finally:
            k32.CloseHandle(proc)
    finally:
        k32.CloseHandle(job)


def close_job(handle) -> None:
    """Closes a handle from hold_job or open_registry, which ends the processes in the job. Never raises."""
    if handle is not None:
        try:
            _kernel32().CloseHandle(handle)
        except OSError:
            pass
