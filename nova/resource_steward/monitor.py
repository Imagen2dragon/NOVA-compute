"""
NOVA Compute — Hardware & Process Resource Monitor
Collects system memory, storage, GPU telemetry, and process working sets
using native Windows APIs, PDH performance counters, and fail-closed diagnostics.
"""

import os
import sys
import time
import io
import ctypes
from ctypes import wintypes
import subprocess
import csv
import queue
import threading
from typing import List, Dict, Optional, Tuple, Callable
from .models import (
    TelemetryStatus,
    SystemMemorySnapshot,
    ProcessResourceSnapshot,
    GPUSnapshot,
    GPUProcessInfo,
    DiskSnapshot,
    current_iso_utc
)

# ---------------------------------------------------------------------------
# Win32 API Type Definitions
# ---------------------------------------------------------------------------
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ('dwLength', ctypes.c_ulong),
        ('dwMemoryLoad', ctypes.c_ulong),
        ('ullTotalPhys', ctypes.c_ulonglong),
        ('ullAvailPhys', ctypes.c_ulonglong),
        ('ullTotalPageFile', ctypes.c_ulonglong),
        ('ullAvailPageFile', ctypes.c_ulonglong),
        ('ullTotalVirtual', ctypes.c_ulonglong),
        ('ullAvailVirtual', ctypes.c_ulonglong),
        ('sullAvailExtendedVirtual', ctypes.c_ulonglong),
    ]

class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ('cb', ctypes.c_ulong),
        ('PageFaultCount', ctypes.c_ulong),
        ('PeakWorkingSetSize', ctypes.c_size_t),
        ('WorkingSetSize', ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
        ('PagefileUsage', ctypes.c_size_t),
        ('PeakPagefileUsage', ctypes.c_size_t),
        ('PrivateUsage', ctypes.c_size_t),
    ]

class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('CommitTotal', ctypes.c_size_t),
        ('CommitLimit', ctypes.c_size_t),
        ('CommitPeak', ctypes.c_size_t),
        ('PhysicalTotal', ctypes.c_size_t),
        ('PhysicalAvailable', ctypes.c_size_t),
        ('SystemCache', ctypes.c_size_t),
        ('KernelTotal', ctypes.c_size_t),
        ('KernelPaged', ctypes.c_size_t),
        ('KernelNonpaged', ctypes.c_size_t),
        ('PageSize', ctypes.c_size_t),
        ('HandleCount', wintypes.DWORD),
        ('ProcessCount', wintypes.DWORD),
        ('ThreadCount', wintypes.DWORD),
    ]

class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [('CStatus', wintypes.DWORD), ('doubleValue', ctypes.c_double)]

KNOWN_USER_PROCESSES = {
    "msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe",
    "code.exe", "antigravity.exe", "claude.exe", "slack.exe", "discord.exe",
    "telegram.exe", "steam.exe", "spotify.exe", "devenv.exe", "pycharm64.exe",
    "ollama.exe", "llama-server.exe", "cmd.exe", "powershell.exe", "wt.exe"
}

class WindowsPDHCollector:
    """Collects system-wide memory paging and disk I/O performance counters."""
    def __init__(self):
        self.initialized = False
        self.h_query = wintypes.HANDLE()
        self.counters = {}
        try:
            pdh = ctypes.windll.pdh
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.h_query)) == 0:
                spec = {
                    'pages_input_sec': '\\Memory\\Pages Input/sec',
                    'pages_output_sec': '\\Memory\\Pages Output/sec',
                    'page_reads_sec': '\\Memory\\Page Reads/sec',
                    'page_writes_sec': '\\Memory\\Page Writes/sec',
                    'disk_read_bytes_sec': '\\PhysicalDisk(_Total)\\Disk Read Bytes/sec',
                    'disk_write_bytes_sec': '\\PhysicalDisk(_Total)\\Disk Write Bytes/sec',
                }
                for key, path in spec.items():
                    h_cnt = wintypes.HANDLE()
                    if pdh.PdhAddCounterW(self.h_query, path, 0, ctypes.byref(h_cnt)) == 0:
                        self.counters[key] = h_cnt
                if self.counters:
                    pdh.PdhCollectQueryData(self.h_query)
                    self.initialized = True
        except Exception:
            self.initialized = False

    def collect(self) -> Dict[str, Optional[float]]:
        results = {k: None for k in ['pages_input_sec', 'pages_output_sec', 'page_reads_sec',
                                     'page_writes_sec', 'disk_read_bytes_sec', 'disk_write_bytes_sec']}
        if not self.initialized:
            return results
        try:
            pdh = ctypes.windll.pdh
            if pdh.PdhCollectQueryData(self.h_query) == 0:
                PDH_FMT_DOUBLE = 0x00000200
                for key, h_cnt in self.counters.items():
                    val = PDH_FMT_COUNTERVALUE()
                    if pdh.PdhGetFormattedCounterValue(h_cnt, PDH_FMT_DOUBLE, None, ctypes.byref(val)) == 0:
                        if val.CStatus == 0:
                            results[key] = round(val.doubleValue, 2)
        except Exception:
            pass
        return results

    def close(self):
        if self.initialized and self.h_query:
            try:
                ctypes.windll.pdh.PdhCloseQuery(self.h_query)
            except Exception:
                pass
            self.initialized = False

_global_pdh_collector: Optional[WindowsPDHCollector] = None

def get_pdh_collector() -> WindowsPDHCollector:
    global _global_pdh_collector
    if _global_pdh_collector is None:
        _global_pdh_collector = WindowsPDHCollector()
    return _global_pdh_collector

class ResourceMonitor:
    """Hardware and process diagnostics provider using native Win32 APIs and safe tooling."""

    @staticmethod
    def get_system_memory() -> SystemMemorySnapshot:
        try:
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                err = ctypes.GetLastError()
                return SystemMemorySnapshot(
                    total_phys_bytes=0, avail_phys_bytes=0, total_phys_gib=0.0, avail_phys_gib=0.0,
                    commit_total_bytes=0, commit_avail_bytes=0, committed_bytes=0,
                    commit_total_gib=0.0, commit_avail_gib=0.0, committed_gib=0.0,
                    commit_pct=0.0, memory_load_pct=0,
                    status=TelemetryStatus.FAILED, error_message=f"GlobalMemoryStatusEx Win32 error: {err}"
                )

            committed_bytes = stat.ullTotalPageFile - stat.ullAvailPageFile
            commit_pct = (committed_bytes / stat.ullTotalPageFile) * 100.0 if stat.ullTotalPageFile > 0 else 0.0

            system_cache_bytes = None
            try:
                pi = PERFORMANCE_INFORMATION()
                pi.cb = ctypes.sizeof(PERFORMANCE_INFORMATION)
                if ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
                    system_cache_bytes = pi.SystemCache * pi.PageSize
            except Exception:
                pass

            pdh = get_pdh_collector().collect()

            return SystemMemorySnapshot(
                total_phys_bytes=stat.ullTotalPhys,
                avail_phys_bytes=stat.ullAvailPhys,
                total_phys_gib=round(stat.ullTotalPhys / (1024**3), 3),
                avail_phys_gib=round(stat.ullAvailPhys / (1024**3), 3),
                commit_total_bytes=stat.ullTotalPageFile,
                commit_avail_bytes=stat.ullAvailPageFile,
                committed_bytes=committed_bytes,
                commit_total_gib=round(stat.ullTotalPageFile / (1024**3), 3),
                commit_avail_gib=round(stat.ullAvailPageFile / (1024**3), 3),
                committed_gib=round(committed_bytes / (1024**3), 3),
                commit_pct=round(commit_pct, 1),
                memory_load_pct=stat.dwMemoryLoad,
                system_cache_bytes=system_cache_bytes,
                pages_input_per_sec=pdh.get("pages_input_sec"),
                pages_output_per_sec=pdh.get("pages_output_sec"),
                page_reads_per_sec=pdh.get("page_reads_sec"),
                page_writes_per_sec=pdh.get("page_writes_sec"),
                disk_read_bytes_per_sec=pdh.get("disk_read_bytes_sec"),
                disk_write_bytes_per_sec=pdh.get("disk_write_bytes_sec"),
                status=TelemetryStatus.SUCCESS
            )
        except Exception as e:
            return SystemMemorySnapshot(
                total_phys_bytes=0, avail_phys_bytes=0, total_phys_gib=0.0, avail_phys_gib=0.0,
                commit_total_bytes=0, commit_avail_bytes=0, committed_bytes=0,
                commit_total_gib=0.0, commit_avail_gib=0.0, committed_gib=0.0,
                commit_pct=0.0, memory_load_pct=0,
                status=TelemetryStatus.FAILED, error_message=str(e)
            )

    @staticmethod
    def get_disk_space(path: str = "C:\\") -> DiskSnapshot:
        try:
            drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
            fb = ctypes.c_ulonglong(0)
            tb = ctypes.c_ulonglong(0)
            tf = ctypes.c_ulonglong(0)
            if not ctypes.windll.kernel32.GetDiskFreeSpaceExW(drive, ctypes.byref(fb), ctypes.byref(tb), ctypes.byref(tf)):
                err = ctypes.GetLastError()
                return DiskSnapshot(
                    drive=drive, free_bytes=0, total_bytes=0, free_gib=0.0, total_gib=0.0,
                    status=TelemetryStatus.FAILED, error_message=f"GetDiskFreeSpaceExW Win32 error: {err}"
                )
            return DiskSnapshot(
                drive=drive,
                free_bytes=fb.value,
                total_bytes=tb.value,
                free_gib=float(fb.value) / (1024**3),
                total_gib=float(tb.value) / (1024**3),
                status=TelemetryStatus.SUCCESS
            )
        except Exception as e:
            return DiskSnapshot(
                drive=path, free_bytes=0, total_bytes=0, free_gib=0.0, total_gib=0.0,
                status=TelemetryStatus.FAILED, error_message=str(e)
            )

    @staticmethod
    def get_gpu_telemetry() -> GPUSnapshot:
        """
        Queries NVIDIA GPU state via nvidia-smi with strict fail-closed diagnostics and robust CSV parsing.
        If nvidia-smi fails or produces malformed output, returns status=FAILED and compute_processes=None.
        """
        try:
            q_gpu = [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,memory.used,utilization.gpu,temperature.gpu,power.draw,driver_version",
                "--format=csv,noheader,nounits"
            ]
            res_gpu = subprocess.run(q_gpu, capture_output=True, text=True, timeout=5)
            if res_gpu.returncode != 0:
                return GPUSnapshot(
                    name="Unknown", vram_total_mib=0.0, vram_free_mib=0.0, vram_used_mib=0.0,
                    utilization_pct=0.0, temperature_c=0.0, power_draw_w=0.0, driver_version="Unknown",
                    compute_processes=None, status=TelemetryStatus.FAILED,
                    error_message=f"nvidia-smi query-gpu returned code {res_gpu.returncode}: {res_gpu.stderr.strip()}"
                )

            gpu_reader = csv.reader(io.StringIO(res_gpu.stdout.strip()))
            rows = [r for r in gpu_reader if r]
            if not rows or len(rows[0]) < 8:
                return GPUSnapshot(
                    name="Unknown", vram_total_mib=0.0, vram_free_mib=0.0, vram_used_mib=0.0,
                    utilization_pct=0.0, temperature_c=0.0, power_draw_w=0.0, driver_version="Unknown",
                    compute_processes=None, status=TelemetryStatus.FAILED,
                    error_message=f"nvidia-smi GPU query output malformed: expected 8 fields, got {len(rows[0]) if rows else 0}"
                )

            row = [c.strip() for c in rows[0]]
            name = row[0]
            try:
                vram_total = float(row[1])
                vram_free = float(row[2])
                vram_used = float(row[3])
                util = float(row[4])
                temp = float(row[5])
                pwr = float(row[6]) if row[6] != "[N/A]" else 0.0
                drv = row[7]
            except ValueError as e:
                return GPUSnapshot(
                    name=name, vram_total_mib=0.0, vram_free_mib=0.0, vram_used_mib=0.0,
                    utilization_pct=0.0, temperature_c=0.0, power_draw_w=0.0, driver_version="Unknown",
                    compute_processes=None, status=TelemetryStatus.FAILED,
                    error_message=f"nvidia-smi numeric parsing error: {e}"
                )

            # Query active compute processes with strict CSV validation
            q_procs = ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"]
            res_procs = subprocess.run(q_procs, capture_output=True, text=True, timeout=5)
            if res_procs.returncode != 0:
                return GPUSnapshot(
                    name=name, vram_total_mib=vram_total, vram_free_mib=vram_free, vram_used_mib=vram_used,
                    utilization_pct=util, temperature_c=temp, power_draw_w=pwr, driver_version=drv,
                    compute_processes=None, status=TelemetryStatus.FAILED,
                    error_message=f"nvidia-smi query-compute-apps returned code {res_procs.returncode}: {res_procs.stderr.strip()}"
                )

            compute_apps: List[GPUProcessInfo] = []
            stdout_text = res_procs.stdout.strip()
            if stdout_text:
                proc_reader = csv.reader(io.StringIO(stdout_text))
                for line_idx, p_parts in enumerate(proc_reader, start=1):
                    if not p_parts:
                        continue
                    p_parts = [p.strip() for p in p_parts]
                    if len(p_parts) < 3:
                        # Malformed row: fail closed!
                        return GPUSnapshot(
                            name=name, vram_total_mib=vram_total, vram_free_mib=vram_free, vram_used_mib=vram_used,
                            utilization_pct=util, temperature_c=temp, power_draw_w=pwr, driver_version=drv,
                            compute_processes=None, status=TelemetryStatus.FAILED,
                            error_message=f"Malformed GPU compute process row {line_idx}: expected 3 fields, got {len(p_parts)} ({p_parts})"
                        )
                    try:
                        pid = int(p_parts[0])
                        p_name = p_parts[1]
                        used_mem = float(p_parts[2])
                        compute_apps.append(GPUProcessInfo(
                            pid=pid,
                            process_name=p_name,
                            used_memory_mib=used_mem
                        ))
                    except ValueError as e:
                        # Malformed numeric value: fail closed! Never discard malformed rows.
                        return GPUSnapshot(
                            name=name, vram_total_mib=vram_total, vram_free_mib=vram_free, vram_used_mib=vram_used,
                            utilization_pct=util, temperature_c=temp, power_draw_w=pwr, driver_version=drv,
                            compute_processes=None, status=TelemetryStatus.FAILED,
                            error_message=f"Malformed GPU compute process numeric field in row {line_idx} ({p_parts}): {e}"
                        )

            return GPUSnapshot(
                name=name,
                vram_total_mib=vram_total,
                vram_free_mib=vram_free,
                vram_used_mib=vram_used,
                utilization_pct=util,
                temperature_c=temp,
                power_draw_w=pwr,
                driver_version=drv,
                compute_processes=compute_apps,
                status=TelemetryStatus.SUCCESS
            )
        except Exception as e:
            return GPUSnapshot(
                name="Unknown", vram_total_mib=0.0, vram_free_mib=0.0, vram_used_mib=0.0,
                utilization_pct=0.0, temperature_c=0.0, power_draw_w=0.0, driver_version="Unknown",
                compute_processes=None, status=TelemetryStatus.FAILED, error_message=str(e)
            )

    @staticmethod
    def get_process_memory(pid: int) -> Optional[ProcessResourceSnapshot]:
        """Queries precise working set, private commit, and commit charge for an owned PID."""
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        h_proc = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h_proc:
            err = ctypes.GetLastError()
            return ProcessResourceSnapshot(
                pid=pid, name=f"pid_{pid}", working_set_bytes=0, peak_working_set_bytes=None,
                private_commit_bytes=None, process_commit_charge_bytes=None, page_fault_count=None,
                status=TelemetryStatus.FAILED, error_message=f"OpenProcess failed: Win32 error {err}"
            )
        try:
            pmc = PROCESS_MEMORY_COUNTERS_EX()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
            if ctypes.windll.psapi.GetProcessMemoryInfo(h_proc, ctypes.byref(pmc), pmc.cb):
                return ProcessResourceSnapshot(
                    pid=pid,
                    name=f"pid_{pid}",
                    working_set_bytes=pmc.WorkingSetSize,
                    peak_working_set_bytes=pmc.PeakWorkingSetSize,
                    private_commit_bytes=pmc.PrivateUsage,
                    process_commit_charge_bytes=pmc.PagefileUsage,
                    page_fault_count=pmc.PageFaultCount,
                    status=TelemetryStatus.SUCCESS
                )
            return ProcessResourceSnapshot(
                pid=pid, name=f"pid_{pid}", working_set_bytes=0, peak_working_set_bytes=None,
                private_commit_bytes=None, process_commit_charge_bytes=None, page_fault_count=None,
                status=TelemetryStatus.FAILED, error_message="GetProcessMemoryInfo call failed"
            )
        finally:
            ctypes.windll.kernel32.CloseHandle(h_proc)

    @staticmethod
    def list_top_processes(limit: int = 15) -> List[ProcessResourceSnapshot]:
        """
        Collects running processes and groups them by executable name.
        Queries private commit across all accessible PIDs in each group.
        Tracks queried vs measured instances, explicitly marking partial results.
        """
        try:
            cmd = ["tasklist", "/FO", "CSV", "/NH"]
            res = subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=8)
            if res.returncode != 0:
                return []
            reader = csv.reader(res.stdout.strip().splitlines())
            grouped_mem: Dict[str, Dict[str, Any]] = {}
            for row in reader:
                if len(row) >= 5:
                    name = row[0]
                    try:
                        pid = int(row[1])
                    except ValueError:
                        continue
                    mem_str = row[4].replace(" K", "").replace(",", "").strip()
                    try:
                        mem_kb = int(mem_str)
                    except ValueError:
                        continue
                    if name not in grouped_mem:
                        grouped_mem[name] = {"pids": [], "total_ws_kb": 0}
                    grouped_mem[name]["pids"].append(pid)
                    grouped_mem[name]["total_ws_kb"] += mem_kb

            sorted_groups = sorted(grouped_mem.items(), key=lambda x: x[1]["total_ws_kb"], reverse=True)[:limit]
            snapshots = []
            for name, data in sorted_groups:
                pids = data["pids"]
                total_ws_bytes = data["total_ws_kb"] * 1024
                is_user = name.lower() in KNOWN_USER_PROCESSES

                total_instances = len(pids)
                measured_instances = 0
                priv_sum = 0

                # Query all PIDs in group to get complete private commit where accessible
                for pid in pids:
                    p_info = ResourceMonitor.get_process_memory(pid)
                    if p_info and p_info.status == TelemetryStatus.SUCCESS and p_info.private_commit_bytes is not None:
                        priv_sum += p_info.private_commit_bytes
                        measured_instances += 1

                is_partial = (0 < measured_instances < total_instances)
                total_priv_commit: Optional[int] = priv_sum if measured_instances > 0 else None

                snapshots.append(ProcessResourceSnapshot(
                    pid=pids[0] if pids else 0,
                    name=name,
                    working_set_bytes=total_ws_bytes,
                    peak_working_set_bytes=None,
                    private_commit_bytes=total_priv_commit,
                    process_commit_charge_bytes=None,
                    page_fault_count=None,
                    pids=pids,
                    instances_total=total_instances,
                    instances_measured=measured_instances,
                    is_partial_private_commit=is_partial,
                    is_user_application=is_user,
                    status=TelemetryStatus.SUCCESS
                ))
            return snapshots
        except Exception:
            return []

class LiveTelemetrySampler:
    """Background sampling thread delivering ordered telemetry samples to an internal thread-safe queue."""
    def __init__(self, target_pid: Optional[int], interval_ms: int = 500, on_sample: Optional[Callable[[Dict], None]] = None):
        self.target_pid = target_pid
        self.interval_sec = max(0.1, interval_ms / 1000.0)
        self.on_sample = on_sample
        self.sample_queue: queue.Queue = queue.Queue()
        self.samples = []
        self._sample_id = 0
        self._stop_event = threading.Event()
        self._thread = None
        self.t_start = 0.0

    def start(self):
        self.t_start = time.time()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, timeout_sec: float = 2.0) -> bool:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout_sec)
            return not self._thread.is_alive()
        return True

    def _loop(self):
        while not self._stop_event.is_set():
            t_now = time.time() - self.t_start
            t_mono = time.monotonic()
            ram = ResourceMonitor.get_system_memory()
            gpu = ResourceMonitor.get_gpu_telemetry()
            proc = ResourceMonitor.get_process_memory(self.target_pid) if self.target_pid else None

            self._sample_id += 1
            sample = {
                "sample_id": self._sample_id,
                "timestamp_monotonic": t_mono,
                "elapsed_s": round(t_now, 3),
                "timestamp_utc": current_iso_utc(),
                "system_ram": {
                    "avail_phys_gib": ram.avail_phys_gib,
                    "committed_gib": ram.committed_gib,
                    "commit_pct": ram.commit_pct,
                    "system_cache_gib": round(ram.system_cache_bytes / (1024**3), 3) if ram.system_cache_bytes else None,
                    "pages_input_per_sec": ram.pages_input_per_sec,
                    "pages_output_per_sec": ram.pages_output_per_sec,
                    "status": ram.status.value,
                    "error": ram.error_message
                },
                "gpu": {
                    "vram_free_mib": gpu.vram_free_mib,
                    "vram_used_mib": gpu.vram_used_mib,
                    "gpu_util_pct": gpu.utilization_pct,
                    "temperature_c": gpu.temperature_c,
                    "power_draw_w": gpu.power_draw_w,
                    "status": gpu.status.value,
                    "error": gpu.error_message
                },
                "process": {
                    "working_set_mib": proc.working_set_mb if proc else None,
                    "private_commit_mib": proc.private_commit_mb if proc else None,
                    "page_fault_count": proc.page_fault_count if proc else None,
                    "status": proc.status.value if proc else None,
                    "error": proc.error_message if proc else None
                } if proc else None
            }
            self.samples.append(sample)
            self.sample_queue.put(sample)
            if self.on_sample:
                try:
                    self.on_sample(sample)
                except Exception:
                    pass
            time.sleep(self.interval_sec)
