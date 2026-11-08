"""Windows process listing with command lines, through the Win32 API, plus tree kill."""
from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from dataclasses import dataclass

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_COMMAND_LINE_INFORMATION = 60
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
INVALID_HANDLE = ctypes.c_void_p(-1).value
FILETIME_EPOCH = 116444736000000000
CREATE_NO_WINDOW = 0x08000000

WATCHED ={"python.exe", "pythonw.exe", "pwsh.exe", "powershell.exe"}


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    cmdline: str
    started: float | None


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
_ntdll = ctypes.WinDLL("ntdll") if os.name == "nt" else None
if _k32 is not None:
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                                 ctypes.POINTER(wintypes.ULONG)]
    _ntdll.NtQueryInformationProcess.restype = wintypes.LONG


def _cmdline_and_start(pid: int) -> tuple[str, float | None]:
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "", None
    try:
        started = None
        times = [wintypes.FILETIME() for _ in range(4)]
        if _k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            ft = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            started = (ft - FILETIME_EPOCH) / 1e7
        size = wintypes.ULONG(0)
        buf = ctypes.create_string_buffer(4096)
        status = _ntdll.NtQueryInformationProcess(handle, PROCESS_COMMAND_LINE_INFORMATION, buf, len(buf),
                                                  ctypes.byref(size))
        if (status & 0xFFFFFFFF) == STATUS_INFO_LENGTH_MISMATCH and size.value:
            buf = ctypes.create_string_buffer(size.value)
            status = _ntdll.NtQueryInformationProcess(handle, PROCESS_COMMAND_LINE_INFORMATION, buf, len(buf),
                                                      ctypes.byref(size))
        if status != 0:
            return "", started
        us = _UNICODE_STRING.from_buffer(buf)
        return (ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer and us.Length else ""), started
    finally:
        _k32.CloseHandle(handle)


def snapshot() -> dict[int, Proc]:
    """Every running process by pid. Command line and start time are filled for WATCHED names only."""
    if _k32 is None:
        return {}
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return {}
    out: dict[int, Proc] = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid, name = int(entry.th32ProcessID), entry.szExeFile
            cmdline, started = ("", None)
            if name.lower() in WATCHED:
                cmdline, started = _cmdline_and_start(pid)
            out[pid] = Proc(pid, int(entry.th32ParentProcessID), name, cmdline, started)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return out


def descendants(procs: dict[int, Proc], root: int) -> set[int]:
    """root and every process below it in `procs`."""
    found, frontier = {root}, [root]
    while frontier:
        parent = frontier.pop()
        for p in procs.values():
            if p.ppid == parent and p.pid not in found and p.pid != p.ppid:
                found.add(p.pid)
                frontier.append(p.pid)
    return found


def kill_tree(pid: int) -> bool:
    """Force-kills pid and its children. Returns whether taskkill reported success."""
    done = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True,
                          creationflags=CREATE_NO_WINDOW)
    return done.returncode == 0
