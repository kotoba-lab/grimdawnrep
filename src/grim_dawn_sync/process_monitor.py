"""Process inspection behind a deliberately small, injectable boundary.

The launcher must make safety decisions from a process executable path and a
creation marker, not merely from a recycled PID or an executable basename.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from typing import Iterable, Protocol


@dataclass(frozen=True)
class ProcessInfo:
    """Identity information needed to safely follow one process instance."""

    pid: int
    name: str
    executable_path: Path | None
    creation_marker: int | None


@dataclass(frozen=True)
class ProcessScan:
    """A scan result.  ``complete=False`` is unsafe for launcher decisions."""

    processes: tuple[ProcessInfo, ...]
    complete: bool = True


class ProcessMonitor(Protocol):
    def scan(self) -> ProcessScan:
        """Return currently running processes without starting any process."""


class WindowsProcessMonitor:
    """Windows-only stdlib adapter using Toolhelp and process identity APIs."""

    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self, watched_names: Iterable[str]) -> None:
        self.watched_names = frozenset(name.casefold() for name in watched_names)

    def scan(self) -> ProcessScan:
        if os.name != "nt":
            return ProcessScan((), complete=False)
        try:
            return self._scan_windows()
        except OSError:
            return ProcessScan((), complete=False)

    def _scan_windows(self) -> ProcessScan:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
            ]

        create = kernel32.CreateToolhelp32Snapshot
        create.argtypes = [wintypes.DWORD, wintypes.DWORD]
        create.restype = wintypes.HANDLE
        handle = create(self._TH32CS_SNAPPROCESS, 0)
        if handle == self._INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        items: list[ProcessInfo] = []
        try:
            entry = PROCESSENTRY32W(); entry.dwSize = ctypes.sizeof(entry)
            first = kernel32.Process32FirstW
            next_item = kernel32.Process32NextW
            first.argtypes = next_item.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
            first.restype = next_item.restype = wintypes.BOOL
            ok = first(handle, ctypes.byref(entry))
            while ok:
                pid = int(entry.th32ProcessID)
                name = entry.szExeFile
                details = self._details_if_watched(kernel32, pid, name)
                if details is not None:
                    items.append(details)
                entry.dwSize = ctypes.sizeof(entry)
                ok = next_item(handle, ctypes.byref(entry))
            error = ctypes.get_last_error()
            if error != 18:  # ERROR_NO_MORE_FILES
                raise ctypes.WinError(error)
        finally:
            kernel32.CloseHandle(handle)
        return ProcessScan(tuple(items))

    def _details_if_watched(self, kernel32: object, pid: int, name: str) -> ProcessInfo | None:
        if name.casefold() not in self.watched_names:
            return None
        return self._details(kernel32, pid, name)

    def _details(self, kernel32: object, pid: int, name: str) -> ProcessInfo:
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        handle = open_process(self._PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ProcessInfo(pid, name, None, None)
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            query = kernel32.QueryFullProcessImageNameW
            query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            query.restype = wintypes.BOOL
            if not query(handle, 0, buffer, ctypes.byref(size)):
                return ProcessInfo(pid, name, None, None)
            creation = wintypes.FILETIME(); exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME(); user_time = wintypes.FILETIME()
            get_times = kernel32.GetProcessTimes
            get_times.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
            get_times.restype = wintypes.BOOL
            if not get_times(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return ProcessInfo(pid, name, Path(buffer.value), None)
            marker = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return ProcessInfo(pid, name, Path(buffer.value), marker)
        finally:
            kernel32.CloseHandle(handle)
