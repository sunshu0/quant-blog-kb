"""判断一个 pid 是不是"还活着的那个进程"（任务锁、停止按钮用）。

只看 pid 不够：Windows 会复用 pid，进程退出后同一个号码可能分给了别的程序。
所以同时比较**进程的创建时间**：锁里记下持有者的 pid 和创建时间，检查时两者都对得上才算同一个进程。

- Windows：ctypes 调 OpenProcess + GetExitCodeProcess + GetProcessTimes（不需要额外依赖）；
  打不开（不存在，或者是别的用户的进程）都算"不是我们的"；
- 其他系统：os.kill(pid, 0) 看是否存在，创建时间取不到时返回 0.0（只按存活判断）。
"""

from __future__ import annotations

import os

STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# FILETIME（1601-01-01 起的 100 纳秒数）和 Unix 时间的差
_EPOCH_DIFF_100NS = 116444736000000000
# 创建时间比较的容差（秒）
START_TOLERANCE_S = 1.0


def _windows_start_time(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        if code.value != STILL_ACTIVE:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        created = times[0]
        value = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return (value - _EPOCH_DIFF_100NS) / 10_000_000
    finally:
        kernel32.CloseHandle(handle)


def start_time(pid: int) -> float | None:
    """进程的创建时间（Unix 秒）；进程不存在 / 已退出 / 不是当前用户的，返回 None。

    非 Windows 系统上进程存在但取不到创建时间时返回 0.0。
    """
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            return _windows_start_time(pid)
        except OSError:
            return None
    try:
        os.kill(pid, 0)  # 信号 0 只检查，不发信号（POSIX）
    except (ProcessLookupError, PermissionError):
        return None
    except OSError:
        return None
    return 0.0


def same_process(pid: int, recorded_start: float | None) -> bool:
    """pid 现在对应的进程，是不是当初记下创建时间 recorded_start 的那个进程。"""
    now = start_time(pid)
    if now is None:
        return False
    if now == 0.0 or recorded_start is None or recorded_start == 0.0:
        return True  # 取不到创建时间：只能按"活着"判断
    return abs(now - recorded_start) <= START_TOLERANCE_S


def self_start_time() -> float | None:
    return start_time(os.getpid())
