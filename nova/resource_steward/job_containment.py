"""
NOVA Compute — Windows Job Object containment for the Option A CLI harness.

Places the *current* harness process in a Job Object configured with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. Child processes started afterwards (llama-cli.exe)
are created inside the same job automatically, so there is no window in which the
child runs outside containment. The only handle to the job is held by this module;
when the harness process exits for any reason (normal exit, crash, Task Manager kill),
the kernel closes that handle and terminates every process still in the job.

Never close the returned handle while the harness is running: doing so would terminate
the harness itself.
"""

import ctypes
import sys
from ctypes import wintypes
from typing import Optional

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9  # JobObjectExtendedLimitInformation

_job_handle: Optional[int] = None


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.GetCurrentProcess.argtypes = []
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def contain_current_process() -> int:
    """
    Puts the current process (and therefore every child it starts afterwards) into a
    kill-on-close Job Object. Idempotent. Raises OSError on any failure, so callers can
    fail closed and refuse to start the child.
    """
    global _job_handle
    if _job_handle is not None:
        return _job_handle
    if sys.platform != "win32":
        raise OSError("Job Object containment is only available on Windows.")

    k32 = _kernel32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(f"CreateJobObjectW failed: Win32 error {ctypes.get_last_error()}")

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(
        job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        err = ctypes.get_last_error()
        k32.CloseHandle(job)
        raise OSError(f"SetInformationJobObject(KILL_ON_JOB_CLOSE) failed: Win32 error {err}")

    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        err = ctypes.get_last_error()
        k32.CloseHandle(job)
        raise OSError(f"AssignProcessToJobObject(current process) failed: Win32 error {err}")

    # Deliberately never closed: the kernel closes it when this process exits.
    _job_handle = job
    return job
