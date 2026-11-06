"""
Phase 3C.0 hardening tests for the Option A CLI harness:
  1. nvidia-smi "[N/A]" per-process memory (Windows WDDM) is parsed as unknown, not as
     malformed telemetry; genuinely malformed values still fail closed.
  2. The supervisor aborts on a hard wall-clock limit and on GPU temperature, and fails
     closed when the temperature reading is missing.
  3. Job Object containment kills a child process when its parent harness dies.

No model, llama-cli or llama-server is started.
"""

import ctypes
import os
import queue
import subprocess
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from nova.resource_steward.models import TelemetryStatus  # noqa: E402
from nova.resource_steward.monitor import ResourceMonitor  # noqa: E402
from nova.resource_steward.policy import ResourcePolicy  # noqa: E402
from nova.resource_steward.supervisor import supervise_child_process  # noqa: E402

GPU_ROW = "NVIDIA GeForce RTX 4050 Laptop GPU, 6141, 5900, 241, 3, 54, 5.16, 581.57"


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class TestWddmGpuProcessParsing(unittest.TestCase):

    def _query(self, procs_stdout: str):
        responses = [_completed(GPU_ROW), _completed(procs_stdout)]
        with mock.patch("nova.resource_steward.monitor.subprocess.run", side_effect=responses):
            return ResourceMonitor.get_gpu_telemetry()

    def test_wddm_na_memory_rows_are_success_with_unknown_memory(self):
        snap = self._query(
            "1128, [Insufficient Permissions], [N/A]\n"
            "31360, C:\\Apps\\Telegram.exe, [N/A]\n"
        )
        self.assertEqual(snap.status, TelemetryStatus.SUCCESS, snap.error_message)
        self.assertEqual(len(snap.compute_processes), 2)
        self.assertEqual(snap.compute_processes[0].pid, 1128)
        self.assertEqual(snap.compute_processes[0].process_name, "[Insufficient Permissions]")
        self.assertIsNone(snap.compute_processes[0].used_memory_mib)

    def test_numeric_memory_still_parsed(self):
        snap = self._query("4242, llama-cli.exe, 4100\n")
        self.assertEqual(snap.status, TelemetryStatus.SUCCESS)
        self.assertEqual(snap.compute_processes[0].used_memory_mib, 4100.0)

    def test_garbage_memory_value_still_fails_closed(self):
        snap = self._query("4242, llama-cli.exe, lots\n")
        self.assertEqual(snap.status, TelemetryStatus.FAILED)
        self.assertIsNone(snap.compute_processes)

    def test_non_numeric_pid_still_fails_closed(self):
        snap = self._query("[N/A], something.exe, [N/A]\n")
        self.assertEqual(snap.status, TelemetryStatus.FAILED)


class _NeverExits:
    """Stand-in for a hung child process."""
    pid = 0

    def poll(self):
        return None


class _FakeClock:
    def __init__(self, start: float = 100.0, step: float = 0.0):
        self.t = start
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def _sample(sample_id, mono, temperature_c=50.0, include_temperature=True):
    gpu = {"vram_free_mib": 5900.0, "vram_used_mib": 241.0, "status": "SUCCESS", "error": None}
    if include_temperature:
        gpu["temperature_c"] = temperature_c
    return {
        "sample_id": sample_id,
        "timestamp_monotonic": mono,
        "system_ram": {"avail_phys_gib": 12.0, "commit_pct": 40.0, "status": "SUCCESS", "error": None},
        "gpu": gpu,
    }


class TestSupervisorHardLimits(unittest.TestCase):

    def _run(self, samples, clock, policy=None):
        sq: queue.Queue = queue.Queue()
        for s in samples:
            sq.put(s)
        return supervise_child_process(
            proc=_NeverExits(),
            sample_queue=sq,
            output_queue=queue.Queue(),
            policy=policy or ResourcePolicy(),
            clock=clock,
            stale_timeout_sec=1e9,
            poll_sleep_sec=0,
        )

    def test_policy_defaults(self):
        pol = ResourcePolicy()
        self.assertEqual(pol.max_run_duration_seconds, 600.0)
        self.assertEqual(pol.critical_gpu_temp_c, 87.0)

    def test_hung_child_aborts_at_wall_clock_limit(self):
        clock = _FakeClock(start=100.0, step=1.0)
        res = self._run([], clock, ResourcePolicy(max_run_duration_seconds=5.0))
        self.assertTrue(res.abort_triggered)
        self.assertIn("maximum run duration", res.abort_reason)

    def test_gpu_temperature_at_limit_aborts(self):
        clock = _FakeClock(start=100.0)
        res = self._run([_sample(1, 100.0, temperature_c=87.0)], clock)
        self.assertTrue(res.abort_triggered)
        self.assertIn("GPU temperature reached 87", res.abort_reason)

    def test_missing_temperature_fails_closed(self):
        clock = _FakeClock(start=100.0)
        res = self._run([_sample(1, 100.0, include_temperature=False)], clock)
        self.assertTrue(res.abort_triggered)
        self.assertIn("temperature reading invalid or missing", res.abort_reason)

    def test_normal_temperature_does_not_abort_before_limit(self):
        # Clock advances 1 s per call; with a 3 s limit the only abort must be the time limit.
        clock = _FakeClock(start=100.0, step=1.0)
        res = self._run([_sample(1, 101.0, temperature_c=60.0)], clock,
                        ResourcePolicy(max_run_duration_seconds=3.0))
        self.assertTrue(res.abort_triggered)
        self.assertIn("maximum run duration", res.abort_reason)
        self.assertEqual(res.processed_samples_count, 1)


# Middle process: contains itself, starts a sleeping grandchild, prints its PID, then waits.
_MIDDLE_SCRIPT = r"""
import subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from nova.resource_steward.job_containment import contain_current_process
contain_current_process()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""


@unittest.skipUnless(sys.platform == "win32", "Job Objects are Windows-only")
class TestJobContainment(unittest.TestCase):

    def test_child_is_killed_when_contained_parent_dies(self):
        SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        k32.WaitForSingleObject.restype = ctypes.c_ulong
        k32.CloseHandle.argtypes = [ctypes.c_void_p]

        middle = subprocess.Popen(
            [sys.executable, "-c", _MIDDLE_SCRIPT, PROJECT_ROOT],
            stdout=subprocess.PIPE, text=True,
        )
        grandchild_handle = None
        try:
            line = middle.stdout.readline().strip()
            self.assertTrue(line.isdigit(), f"Middle process did not report a PID: {line!r}")
            grandchild_pid = int(line)
            grandchild_handle = k32.OpenProcess(SYNCHRONIZE, 0, grandchild_pid)
            self.assertTrue(grandchild_handle, "Could not open grandchild process")

            # Grandchild is alive while the contained parent lives.
            self.assertNotEqual(k32.WaitForSingleObject(grandchild_handle, 200), WAIT_OBJECT_0)

            # Simulate a harness crash.
            middle.kill()
            middle.wait(timeout=5)

            # Kernel must terminate the grandchild when the job handle closes.
            self.assertEqual(
                k32.WaitForSingleObject(grandchild_handle, 5000), WAIT_OBJECT_0,
                "Grandchild survived parent death: Job Object containment failed",
            )
        finally:
            if grandchild_handle:
                k32.CloseHandle(grandchild_handle)
            if middle.poll() is None:
                middle.kill()
            if middle.stdout:
                middle.stdout.close()



class TestDwmExemption(unittest.TestCase):
    """Only the verified system dwm.exe is exempt from the GPU contention gate."""

    DEV = r"\Device\HarddiskVolume3"
    ROOT = r"C:\WINDOWS"

    def test_path_rule(self):
        from nova.resource_steward.system_processes import is_dwm_image_path as ok
        self.assertTrue(ok(self.DEV + r"\Windows\System32\dwm.exe", self.DEV, self.ROOT))
        self.assertTrue(ok(self.DEV.upper() + r"\WINDOWS\SYSTEM32\DWM.EXE", self.DEV, self.ROOT))
        self.assertFalse(ok(self.DEV + r"\Users\x\dwm.exe", self.DEV, self.ROOT))
        self.assertFalse(ok(r"\Device\HarddiskVolume4\Windows\System32\dwm.exe", self.DEV, self.ROOT))
        self.assertFalse(ok(self.DEV + r"\Windows\System32\explorer.exe", self.DEV, self.ROOT))
        self.assertFalse(ok(None, self.DEV, self.ROOT))
        self.assertFalse(ok(self.DEV + r"\Windows\System32\dwm.exe", None, self.ROOT))
        self.assertFalse(ok(self.DEV + r"\Windows\System32\dwm.exe", self.DEV, "bad"))

    def _gpu(self, procs):
        from nova.resource_steward.models import GPUSnapshot
        return GPUSnapshot(
            name="NVIDIA GeForce RTX 4050 Laptop GPU", vram_total_mib=6141.0, vram_free_mib=5900.0,
            vram_used_mib=241.0, utilization_pct=0.0, temperature_c=40.0, power_draw_w=5.0,
            driver_version="581.57", compute_processes=procs, status=TelemetryStatus.SUCCESS,
        )

    def _gate(self, procs, exempt_pids):
        from nova.resource_steward.memory_budget import MemoryBudgetManager
        from nova.resource_steward.models import SystemMemorySnapshot, DiskSnapshot
        ram = SystemMemorySnapshot(
            total_phys_bytes=16 * 1024**3, avail_phys_bytes=12 * 1024**3, total_phys_gib=16.0,
            avail_phys_gib=12.0, commit_total_bytes=40 * 1024**3, commit_avail_bytes=24 * 1024**3,
            committed_bytes=16 * 1024**3, commit_total_gib=40.0, commit_avail_gib=24.0,
            committed_gib=16.0, commit_pct=40.0, memory_load_pct=30, status=TelemetryStatus.SUCCESS,
        )
        disk = DiskSnapshot(drive="C:\\", free_bytes=100 * 1024**3, total_bytes=950 * 1024**3,
                            free_gib=100.0, total_gib=950.0, status=TelemetryStatus.SUCCESS)
        with mock.patch("nova.resource_steward.system_processes.is_exempt_system_gpu_process",
                        side_effect=lambda pid: pid in exempt_pids):
            _, gates = MemoryBudgetManager(ResourcePolicy()).evaluate_gates(ram, self._gpu(procs), disk)
        return gates["no_competing_gpu"]

    def test_gate_passes_with_only_verified_dwm(self):
        from nova.resource_steward.models import GPUProcessInfo
        gate = self._gate([GPUProcessInfo(1128, "[Insufficient Permissions]", None)], {1128})
        self.assertTrue(gate.passed, gate.failure_reason)
        self.assertIn("1 exempt: dwm.exe", gate.measured_str)

    def test_gate_blocks_other_apps_alongside_dwm(self):
        from nova.resource_steward.models import GPUProcessInfo
        gate = self._gate([
            GPUProcessInfo(1128, "[Insufficient Permissions]", None),
            GPUProcessInfo(31360, r"C:\Apps\Telegram.exe", None),
        ], {1128})
        self.assertFalse(gate.passed)
        self.assertIn("Telegram.exe (PID 31360)", gate.failure_reason)

    def test_gate_blocks_unverified_dwm_name(self):
        from nova.resource_steward.models import GPUProcessInfo
        # Named dwm.exe but not verified as the system copy -> still competing.
        gate = self._gate([GPUProcessInfo(5555, r"C:\Users\x\dwm.exe", None)], set())
        self.assertFalse(gate.passed)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only live check")
    def test_live_dwm_is_exempt_and_self_is_not(self):
        from nova.resource_steward.system_processes import is_exempt_system_gpu_process
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq dwm.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
        pids = [int(line.split('","')[1]) for line in out.splitlines() if line.lower().startswith('"dwm.exe"')]
        self.assertTrue(pids, "Expected dwm.exe on an interactive Windows session")
        for pid in pids:
            self.assertTrue(is_exempt_system_gpu_process(pid), f"dwm.exe PID {pid} should be exempt")
        self.assertFalse(is_exempt_system_gpu_process(os.getpid()))
        self.assertFalse(is_exempt_system_gpu_process(0))

if __name__ == "__main__":
    unittest.main()
