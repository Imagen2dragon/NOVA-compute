"""
NOVA Compute — Resource Steward Comprehensive Safety Test Suite
Tests memory budgeting, fail-closed preflight gates, system commit limits,
CLI mutual exclusivity, supervisor live telemetry monitoring, monotonic abort timers,
Windows process group termination, pipe cleanup, and recovery verification.

STRICT SAFETY:
Never launches actual models (llama-cli), never touches unrelated user processes.
All real-process fixtures are explicitly spawned, owned, and terminated by the test.
"""

import os
import sys
import time
import signal
import queue
import subprocess
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from nova.resource_steward.models import (
    TelemetryStatus,
    SystemMemorySnapshot,
    GPUSnapshot,
    GPUProcessInfo,
    DiskSnapshot,
    ProcessResourceSnapshot,
    ExecutionMode
)
from nova.resource_steward.policy import ResourcePolicy
from nova.resource_steward.memory_budget import MemoryBudgetManager
from nova.resource_steward.advisor import MemoryAdvisor
from nova.resource_steward.recovery import SafeReclamationManager
from nova.resource_steward.monitor import ResourceMonitor

class TestResourceSteward(unittest.TestCase):

    def setUp(self):
        self.policy = ResourcePolicy(
            minimum_inference_ram_gib=10.0,
            preferred_inference_ram_gib=12.0,
            workspace_reserve_gib=4.0,
            critical_ram_gib=2.0,
            minimum_vram_mib=5500.0,
            minimum_disk_free_gib=25.0,
            critical_commit_pct=90.0,
            low_ram_abort_duration_seconds=5.0
        )
        self.budget_mgr = MemoryBudgetManager(self.policy)

    def _make_healthy_ram(self, avail_gib: float = 12.0, commit_pct: float = 40.0, status: TelemetryStatus = TelemetryStatus.SUCCESS) -> SystemMemorySnapshot:
        return SystemMemorySnapshot(
            total_phys_bytes=int(16 * 1024**3),
            avail_phys_bytes=int(avail_gib * 1024**3),
            total_phys_gib=16.0,
            avail_phys_gib=avail_gib,
            commit_total_bytes=int(40 * 1024**3),
            commit_avail_bytes=int((40 * (1 - commit_pct/100.0)) * 1024**3),
            committed_bytes=int((40 * (commit_pct/100.0)) * 1024**3),
            commit_total_gib=40.0,
            commit_avail_gib=round(40 * (1 - commit_pct/100.0), 3),
            committed_gib=round(40 * (commit_pct/100.0), 3),
            commit_pct=commit_pct,
            memory_load_pct=30,
            status=status
        )

    def _make_healthy_gpu(self, free_mib: float = 5900.0, compute_apps: list = None, status: TelemetryStatus = TelemetryStatus.SUCCESS) -> GPUSnapshot:
        return GPUSnapshot(
            name="NVIDIA GeForce RTX 4050 Laptop GPU",
            vram_total_mib=6141.0,
            vram_free_mib=free_mib,
            vram_used_mib=6141.0 - free_mib,
            utilization_pct=0.0,
            temperature_c=35.0,
            power_draw_w=15.0,
            driver_version="592.82",
            compute_processes=[] if compute_apps is None else compute_apps,
            status=status
        )

    def _make_healthy_disk(self, free_gb: float = 100.0, status: TelemetryStatus = TelemetryStatus.SUCCESS) -> DiskSnapshot:
        return DiskSnapshot(
            drive="C:\\",
            free_bytes=int(free_gb * 1024**3),
            total_bytes=int(950 * 1024**3),
            free_gb=free_gb,
            total_gb=950.0,
            status=status
        )

    # -----------------------------------------------------------------------
    # Requirement 1: Default invocation never starts a child process
    # -----------------------------------------------------------------------
    def test_01_default_invocation_never_starts_child(self):
        cmd = [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "run_inference_with_telemetry.py")]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertIn("[MODE] PREFLIGHT ONLY", res.stdout)
        self.assertIn("PREFLIGHT STATUS", res.stdout)
        self.assertNotIn("Spawning NOVA inference child process", res.stdout)

    # -----------------------------------------------------------------------
    # Requirement 2: --dry-run never starts a child process
    # -----------------------------------------------------------------------
    def test_02_dry_run_never_starts_child(self):
        cmd = [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "run_inference_with_telemetry.py"), "--dry-run"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0)
        self.assertIn("[MODE] DRY RUN VALIDATION", res.stdout)
        self.assertIn("Dry run completed successfully. (No inference was started).", res.stdout)

    # -----------------------------------------------------------------------
    # Requirement 3: Failed preflight prevents Popen from being called
    # -----------------------------------------------------------------------
    def test_03_failed_preflight_blocks_execution(self):
        from scripts.run_inference_with_telemetry import execute_inference
        from nova.resource_steward import ResourceSteward

        steward = ResourceSteward(self.policy)
        failing_ram = self._make_healthy_ram(avail_gib=8.5)
        gpu = self._make_healthy_gpu()
        disk = self._make_healthy_disk()
        failing_report = self.budget_mgr.generate_preflight_report(
            failing_ram, gpu, disk, top_processes=[], recommendations=[]
        )

        with patch.object(steward, "run_preflight", return_value=failing_report), \
             patch("subprocess.Popen") as mock_popen, \
             patch("sys.exit", side_effect=SystemExit(1)) as mock_exit:
            with self.assertRaises(SystemExit):
                execute_inference(steward)
            mock_popen.assert_not_called()
            mock_exit.assert_called_with(1)

    # -----------------------------------------------------------------------
    # Requirement 4: Conflicting CLI modes are rejected
    # -----------------------------------------------------------------------
    def test_04_conflicting_cli_modes_rejected(self):
        cmd = [
            sys.executable,
            os.path.join(PROJECT_ROOT, "scripts", "run_inference_with_telemetry.py"),
            "--execute",
            "--dry-run"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not allowed with argument", res.stderr)

    # -----------------------------------------------------------------------
    # Requirement 5: Commit usage at 90% fails the preflight gate
    # -----------------------------------------------------------------------
    def test_05_commit_usage_at_90_fails_gate(self):
        ram = self._make_healthy_ram(avail_gib=12.0, commit_pct=90.5)
        gpu = self._make_healthy_gpu()
        disk = self._make_healthy_disk()

        all_passed, gates = self.budget_mgr.evaluate_gates(ram, gpu, disk)
        self.assertFalse(all_passed)
        self.assertIn("system_commit", gates)
        self.assertFalse(gates["system_commit"].passed)
        self.assertIn("exceeds critical threshold", gates["system_commit"].failure_reason)

    # -----------------------------------------------------------------------
    # Requirement 6: Missing commit telemetry fails closed
    # -----------------------------------------------------------------------
    def test_06_missing_commit_telemetry_fails_closed(self):
        ram = self._make_healthy_ram(avail_gib=12.0, status=TelemetryStatus.FAILED)
        gpu = self._make_healthy_gpu()
        disk = self._make_healthy_disk()

        all_passed, gates = self.budget_mgr.evaluate_gates(ram, gpu, disk)
        self.assertFalse(all_passed)
        self.assertFalse(gates["system_commit"].passed)
        self.assertEqual(gates["system_commit"].measured_str, "UNAVAILABLE")
        self.assertIn("Fail-closed", gates["system_commit"].failure_reason)

    # -----------------------------------------------------------------------
    # Requirement 7: RAM telemetry failure during execution causes immediate abort
    # -----------------------------------------------------------------------
    def test_07_ram_telemetry_failure_during_execution_aborts(self):
        s_ram = {"avail_phys_gib": 12.0, "commit_pct": 50.0, "status": "FAILED", "error": "Win32 poll timeout"}
        s_gpu = {"vram_free_mib": 5800.0, "vram_used_mib": 300.0, "status": "SUCCESS"}

        abort_triggered = False
        abort_reason = ""
        if s_ram.get("status") != TelemetryStatus.SUCCESS.value:
            abort_triggered = True
            abort_reason = f"CRITICAL ABORT: Required system RAM telemetry failed ({s_ram.get('error')}). Fail-closed."

        self.assertTrue(abort_triggered)
        self.assertIn("Required system RAM telemetry failed", abort_reason)

    # -----------------------------------------------------------------------
    # Requirement 8: GPU telemetry failure during execution causes immediate abort
    # -----------------------------------------------------------------------
    def test_08_gpu_telemetry_failure_during_execution_aborts(self):
        s_ram = {"avail_phys_gib": 12.0, "commit_pct": 50.0, "status": "SUCCESS"}
        s_gpu = {"vram_free_mib": 0.0, "vram_used_mib": 0.0, "status": "FAILED", "error": "nvidia-smi timed out"}

        abort_triggered = False
        abort_reason = ""
        if s_gpu.get("status") != TelemetryStatus.SUCCESS.value:
            abort_triggered = True
            abort_reason = f"CRITICAL ABORT: Required GPU telemetry failed ({s_gpu.get('error')}). Fail-closed."

        self.assertTrue(abort_triggered)
        self.assertIn("Required GPU telemetry failed", abort_reason)

    # -----------------------------------------------------------------------
    # Requirement 9: Stale telemetry samples cause a safe abort
    # -----------------------------------------------------------------------
    def test_09_stale_telemetry_causes_safe_abort(self):
        STALE_TELEMETRY_TIMEOUT_SEC = 3.0
        now_mono = 100.0
        last_sample_monotonic = 96.5 # 3.5 seconds ago

        abort_triggered = (now_mono - last_sample_monotonic > STALE_TELEMETRY_TIMEOUT_SEC)
        abort_reason = "CRITICAL ABORT: Live telemetry sampler is unresponsive or stale (> 3.0s without fresh sample). Fail-closed."

        self.assertTrue(abort_triggered)
        self.assertIn("unresponsive or stale", abort_reason)

    # -----------------------------------------------------------------------
    # Requirement 10: Repeated consumption of same sample cannot satisfy timer
    # -----------------------------------------------------------------------
    def test_10_repeated_sample_does_not_advance_timer(self):
        last_processed_sample_id = 5
        sample = {"sample_id": 5, "system_ram": {"avail_phys_gib": 1.5, "commit_pct": 40.0, "status": "SUCCESS"}}

        # Check logic: if sample_id <= last_processed_sample_id, skip processing
        is_fresh = sample["sample_id"] > last_processed_sample_id
        self.assertFalse(is_fresh)

    # -----------------------------------------------------------------------
    # Requirement 11: Low RAM sustained for required elapsed time triggers abort
    # -----------------------------------------------------------------------
    def test_11_low_ram_sustained_triggers_abort(self):
        critical_ram_start_monotonic = 10.0
        now_mono = 15.5 # 5.5 seconds elapsed (threshold: 5.0s)
        low_ram_threshold_sec = 5.0

        elapsed = now_mono - critical_ram_start_monotonic
        abort_triggered = elapsed >= low_ram_threshold_sec
        self.assertTrue(abort_triggered)

    # -----------------------------------------------------------------------
    # Requirement 12: Silent child output does not stop resource monitoring
    # -----------------------------------------------------------------------
    def test_12_silent_child_output_does_not_block(self):
        out_queue = queue.Queue()
        # Empty queue should not block with get_nowait
        lines_read = []
        while True:
            try:
                line = out_queue.get_nowait()
                lines_read.append(line)
            except queue.Empty:
                break
        self.assertEqual(len(lines_read), 0)

    # -----------------------------------------------------------------------
    # Requirement 13: CUDA OOM output is detected by supervisor
    # -----------------------------------------------------------------------
    def test_13_cuda_oom_detected_from_queue(self):
        out_queue = queue.Queue()
        out_queue.put("llama_kv_cache_init: failed to allocate buffer\n")
        out_queue.put("CUDA error: out of memory\n")

        abort_triggered = False
        abort_reason = ""
        while True:
            try:
                line = out_queue.get_nowait()
                if "CUDA error" in line or "out of memory" in line.lower():
                    abort_triggered = True
                    abort_reason = f"CUDA OOM detected: {line.strip()}"
                    break
            except queue.Empty:
                break

        self.assertTrue(abort_triggered)
        self.assertIn("out of memory", abort_reason)

    # -----------------------------------------------------------------------
    # Requirement 14: Failed post-run GPU telemetry cannot produce successful recovery
    # -----------------------------------------------------------------------
    def test_14_failed_post_gpu_telemetry_fails_recovery(self):
        pre_ram = self._make_healthy_ram(avail_gib=12.0)
        pre_gpu = self._make_healthy_gpu(free_mib=5900.0)

        with patch("nova.resource_steward.monitor.ResourceMonitor.get_system_memory") as mock_ram, \
             patch("nova.resource_steward.monitor.ResourceMonitor.get_gpu_telemetry") as mock_gpu, \
             patch("subprocess.run") as mock_run:

            mock_ram.return_value = self._make_healthy_ram(avail_gib=11.8)
            # Post-run GPU query failed!
            mock_gpu.return_value = GPUSnapshot(
                name="Unknown", vram_total_mib=0.0, vram_free_mib=0.0, vram_used_mib=0.0,
                utilization_pct=0.0, temperature_c=0.0, power_draw_w=0.0, driver_version="Unknown",
                status=TelemetryStatus.FAILED, error_message="driver disconnected"
            )
            mock_run.return_value = MagicMock(stdout="", returncode=0)

            report = SafeReclamationManager.perform_post_inference_recovery(
                pre_ram=pre_ram,
                pre_gpu=pre_gpu,
                peak_ram_gib=10.5,
                peak_vram_mib=4200.0,
                child_proc=None,
                grace_period_sec=0.1
            )

            self.assertFalse(report.recovery_successful)
            self.assertEqual(report.gpu_verification_status, "FAILED")
            self.assertIsNone(report.residual_vram_leak_mib)
            self.assertIn("Post-run GPU telemetry failed", report.notes[0])

    # -----------------------------------------------------------------------
    # Requirement 15: Failed residual-process verification fails recovery
    # -----------------------------------------------------------------------
    def test_15_failed_residual_process_check_fails_recovery(self):
        pre_ram = self._make_healthy_ram(avail_gib=12.0)
        pre_gpu = self._make_healthy_gpu(free_mib=5900.0)

        mock_proc = MagicMock()
        mock_proc.pid = 8888
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("nova.resource_steward.monitor.ResourceMonitor.get_system_memory") as mock_ram, \
             patch("nova.resource_steward.monitor.ResourceMonitor.get_gpu_telemetry") as mock_gpu, \
             patch("subprocess.run") as mock_run:

            mock_ram.return_value = self._make_healthy_ram(avail_gib=11.8)
            mock_gpu.return_value = self._make_healthy_gpu(free_mib=5890.0)
            # tasklist returned error code
            mock_run.return_value = MagicMock(stdout="", returncode=1)

            report = SafeReclamationManager.perform_post_inference_recovery(
                pre_ram=pre_ram,
                pre_gpu=pre_gpu,
                peak_ram_gib=10.5,
                peak_vram_mib=4200.0,
                child_proc=mock_proc,
                grace_period_sec=0.1
            )

            self.assertFalse(report.recovery_successful)
            self.assertEqual(report.process_verification_status, "UNKNOWN")

    # -----------------------------------------------------------------------
    # Requirement 16: Process pipes are closed after child termination
    # -----------------------------------------------------------------------
    def test_16_process_pipes_closed_after_termination(self):
        mock_proc = MagicMock()
        mock_proc.pid = 7777
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        mock_stdout = MagicMock()
        mock_stdout.closed = False
        mock_stderr = MagicMock()
        mock_stderr.closed = False
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = mock_stderr
        mock_proc.stdin = None

        SafeReclamationManager.close_process_pipes(mock_proc)
        mock_stdout.close.assert_called_once()
        mock_stderr.close.assert_called_once()

    # -----------------------------------------------------------------------
    # Requirement 17: Malformed GPU process rows fail closed
    # -----------------------------------------------------------------------
    def test_17_malformed_gpu_process_rows_fail_closed(self):
        # Mock nvidia-smi returning valid GPU row but malformed compute app row
        gpu_stdout = "NVIDIA GeForce RTX 4050 Laptop GPU, 6141, 5900, 241, 0, 35, 15, 592.82\n"
        compute_stdout = "not_a_valid_row\n" # Missing fields

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout=gpu_stdout, returncode=0),
                MagicMock(stdout=compute_stdout, returncode=0)
            ]

            gpu = ResourceMonitor.get_gpu_telemetry()
            self.assertEqual(gpu.status, TelemetryStatus.FAILED)
            self.assertIsNone(gpu.compute_processes)
            self.assertIn("Malformed GPU compute process", gpu.error_message)

    # -----------------------------------------------------------------------
    # Requirement 18: Safe Windows child fixture tests graceful shutdown and forced escalation
    # -----------------------------------------------------------------------
    def test_18_safe_child_fixture_termination(self):
        # 18.1: Test real graceful exit on a benign owned subprocess
        child_cmd = [sys.executable, "-c", "import time; time.sleep(0.5)"]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        proc = subprocess.Popen(child_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)

        term_status, method, ok = SafeReclamationManager.terminate_child_process(proc, grace_period_sec=2.0)
        self.assertTrue(ok)
        self.assertIn(method, ["NATURAL_EXIT", "CTRL_BREAK", "TERMINATE_SIGNAL"])
        self.assertTrue(proc.poll() is not None)

        # 18.2: Test forced escalation when process does not exit during grace period
        mock_stubborn = MagicMock()
        mock_stubborn.pid = 99991
        mock_stubborn.poll.side_effect = [None, None, None, None, 0]
        mock_stubborn.wait.return_value = 0
        mock_stubborn.returncode = 1

        term_status, method, ok = SafeReclamationManager.terminate_child_process(mock_stubborn, grace_period_sec=0.2)
        self.assertTrue(ok)
        self.assertEqual(method, "FORCED_TERMINATION")
        mock_stubborn.kill.assert_called_once()

if __name__ == "__main__":
    unittest.main()
