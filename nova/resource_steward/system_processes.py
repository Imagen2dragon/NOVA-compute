"""
NOVA Compute — identification of exempt system GPU processes.

Under Windows WDDM, the NVIDIA driver lists graphics users (not only compute workloads)
as "compute apps". The Desktop Window Manager (dwm.exe) is always present, so a strict
"zero competing GPU apps" gate could never pass. Project Owner decision (2026-11-06):
exempt dwm.exe ONLY, and only when its image path is verified to be the system copy.

dwm.exe cannot be opened by an ordinary user (OpenProcess -> ERROR_ACCESS_DENIED), so the
image path is read with NtQuerySystemInformation(SystemProcessIdInformation), which needs
no process handle. Any failure means "not exempt" (fail closed).
"""

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Optional

SYSTEM_PROCESS_ID_INFORMATION = 88


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


class _SYSTEM_PROCESS_ID_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ProcessId", ctypes.c_void_p),
        ("ImageName", _UNICODE_STRING),
    ]


def is_dwm_image_path(nt_image_path: Optional[str], system_drive_device: Optional[str],
                      system_root: Optional[str]) -> bool:
    """
    Pure check: True only if `nt_image_path` is exactly
    <system drive device>\\<SystemRoot without drive>\\System32\\dwm.exe (case-insensitive).
    Example: \\Device\\HarddiskVolume3 + \\WINDOWS\\System32\\dwm.exe
    """
    if not nt_image_path or not system_drive_device or not system_root:
        return False
    if len(system_root) < 3 or system_root[1] != ":":
        return False
    expected = system_drive_device.rstrip("\\") + system_root[2:].rstrip("\\") + "\\System32\\dwm.exe"
    return nt_image_path.lower() == expected.lower()


def query_nt_image_path(pid: int) -> Optional[str]:
    """NT image path of `pid` (e.g. \\Device\\HarddiskVolume3\\...), or None on any failure."""
    if sys.platform != "win32" or pid <= 0:
        return None
    try:
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtQuerySystemInformation.argtypes = [
            ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)
        ]
        ntdll.NtQuerySystemInformation.restype = ctypes.c_long
        buf = ctypes.create_unicode_buffer(1024)
        info = _SYSTEM_PROCESS_ID_INFORMATION()
        info.ProcessId = pid
        info.ImageName.Length = 0
        info.ImageName.MaximumLength = ctypes.sizeof(buf)
        info.ImageName.Buffer = ctypes.cast(buf, ctypes.c_void_p).value
        status = ntdll.NtQuerySystemInformation(
            SYSTEM_PROCESS_ID_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None
        )
        if status != 0 or info.ImageName.Length == 0:
            return None
        return ctypes.wstring_at(info.ImageName.Buffer, info.ImageName.Length // 2)
    except (OSError, AttributeError, ValueError):
        return None


def query_system_drive_device() -> Optional[str]:
    """NT device for the system drive (e.g. \\Device\\HarddiskVolume3), or None."""
    if sys.platform != "win32":
        return None
    drive = os.environ.get("SystemDrive")
    if not drive:
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.QueryDosDeviceW.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        if k32.QueryDosDeviceW(drive, buf, 1024) == 0:
            return None
        return buf.value or None
    except (OSError, AttributeError):
        return None


def is_exempt_system_gpu_process(pid: int) -> bool:
    """True only for the verified system dwm.exe. Fails closed (False) on any doubt."""
    return is_dwm_image_path(
        query_nt_image_path(pid),
        query_system_drive_device(),
        os.environ.get("SystemRoot"),
    )
