"""
NOVA Compute — Resource Steward Automated Test Suite
Tests memory budgeting, fail-closed telemetry gates, mock process termination,
and recovery reporting.
STRICT SAFETY: Never launches actual models, never kills actual user processes.
"""

import unittest
from unittest.mock import MagicMock, patch
import time
import subprocess

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

class TestResourceSteward(unittest.TestCase):

    def setUp(self):
        self.policy = ResourcePolicy(
            minimum_inference_ram_gib=10.0,
            preferred_inference_ram_gib=12.0,
            workspace_reserve_gib=4.0,
            critical_ram_gib=2.0,
            minimum_vram_mib=5500.0,
            minimum_disk_free_gib=25.0
        )
        self.budget_mgr = MemoryBudgetManager(self.policy)

    def _make_healthy_ram(self, avail_gib: float = 12.0) -> SystemMemorySnapshot:
        return SystemMemorySnapshot(
            total_phys_bytes=int(16 * 1024**3),
            avail_phys_bytes=int(avail_gib * 1024**3),
            total_phys_gib=16.0,
            avail_phys_gib=avail_gib,
            commit_total_bytes=int(40 * 1024**3),
            commit_avail_bytes=int(25 * 1024**3),
            committed_bytes=int(15 * 1024**3),
            commit_total_gib=40.0,
            commit_avail_gib=25.0,
            committed_gib=15.0,
            commit_pct=37.5,
            memory_load_pct=30,
            status=TelemetryStatus.SUCCESS
        )

    def _make_healthy_gpu(self, free_mib: float = 5900.0, compute_apps: list = None) -> GPUSnapshot:
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
            status=TelemetryStatus.SUCCESS
        )

    def _make_healthy_disk(self, free_gb: float = 100.0) -> DiskSnapshot:
        return DiskSnapshot(
            drive="C:\\",
            free_bytes=int(free_gb * 1024**3),
            total_bytes=int(950 * 1024**3),
            free_gb=free_gb,
            total_gb=950.0,
            status=TelemetryStatus.SUCCESS
        )

    # -----------------------------------------------------------------------
    # Test 1: RAM Sufficient
    # -----------------------------------------------------------------------
    def test_01_ram_sufficient(self):
        ram = self._make_healthy_ram(avail_gib=12.5)
        gpu = self._make_healthy_gpu(free_mib=5900.0)
        disk = self._make_healthy_disk(free_gb=100.0)

        all_passed, gates = self.budget_mgr.evaluate_gates(ram, gpu, disk)
        self.assertTrue(all_passed)
        self.assertTrue(gates["avail_ram"].passed)
        self.assertEqual(gates["avail_ram"].measured_str, "12.500 GiB")

    # -----------------------------------------------------------------------
    # Test 2: RAM Below Preflight Gate
    # -----------------------------------------------------------------------
    def test_02_ram_below_preflight_gate(self):
        ram = self._make_healthy_ram(avail_gib=9.486)
        gpu = self._make_healthy_gpu(free_mib=5900.0)
        disk = self._make_healthy_disk(free_gb=100.0)

        all_passed, gates = self.budget_mgr.evaluate_gates(ram, gpu, disk)
        self.assertFalse(all_passed)
        self.assertFalse(gates["avail_ram"].passed)
        self.assertIn("deficit: 0.514 GiB", gates["avail_ram"].failure_reason)

    # -----------------------------------------------------------------------
    # Test 3: RAM Falls Below Workspace Reserve
    # -----------------------------------------------------------------------
    def test_03_ram_falls_below_workspace_reserve(self):
        ram = self._make_healthy_ram(avail_gib=3.5)
        self.assertLess(ram.avail_phys_gib, self.policy.workspace_reserve_gib)
        # Should trigger advisory warning in advisor
        procs = [
            ProcessResourceSnapshot(pid=101, name="msedge.exe", working_set_bytes=1500*1024**2,
                                    peak_working_set_bytes=1500*1024**2, private_commit_bytes=1500*1024**2,
                                    process_commit_charge_bytes=1500*1024**2, page_fault_count=100, is_user_application=True)
        ]
        recs = MemoryAdvisor.analyze_processes(procs, ram_deficit_gib=self.policy.workspace_reserve_gib - ram.avail_phys_gib)
        self.assertTrue(len(recs) > 0)
        self.assertEqual(recs[0].process_name, "msedge.exe")

    # -----------------------------------------------------------------------
    # Test 4: RAM Falls Below Critical Threshold
    # -----------------------------------------------------------------------
    def test_04_ram_falls_below_critical_threshold(self):
        ram = self._make_healthy_ram(avail_gib=1.8)
        self.assertLess(ram.avail_phys_gib, self.policy.critical_ram_gib)
        all_passed, gates = self.budget_mgr.evaluate_gates(ram, self._make_healthy_gpu(), self._make_healthy_disk())
        self.assertFalse(all_passed)
        self.assertFalse(gates["avail_ram"].passed)

    # -----------------------------------------------------------------------
    # Test 5: GPU Telemetry Unavailable (Fail-Closed)
    # -----------------------------------------------------------------------
    def test_05_gpu_telemetry_unavailable(self):
        ram = self._make_healthy_ram(avail_gib=12.0)
        gpu = GPUSnapshot(
            name="Unknown", vram_total_mib=0.0, vram_free_mib=0.0, vram_used_mib=0.0,
            utilization_pct=0.0, temperature_c=0.0, power_draw_w=0.0, driver_version="Unknown",
            compute_processes=None, status=TelemetryStatus.FAILED, error_message="nvidia-smi timed out"
        )
        disk = self._make_healthy_disk(free_gb=100.0)

        all_passed, gates = self.budget_mgr.evaluate_gates(ram, gpu, disk)
        self.assertFalse(all_passed)
        self.assertFalse(gates["telemetry_integrity"].passed)
        self.assertFalse(gates["avail_vram"].passed)
        self.assertFalse(gates["no_competing_gpu"].passed)
        # Must fail-closed without inventing values
        self.assertEqual(gates["avail_vram"].measured_str, "UNAVAILABLE")

    # -----------------------------------------------------------------------
    # Test 6: GPU Process Query Unavailable (Fail-Closed)
    # -----------------------------------------------------------------------
    def test_06_gpu_process_query_unavailable(self):
        ram = self._make_healthy_ram(avail_gib=12.0)
        # GPU telemetry works, but query-compute-apps returned error/None
        gpu = self._make_healthy_gpu(free_mib=5800.0)
        gpu.compute_processes = None

        all_passed, gates = self.budget_mgr.evaluate_gates(ram, gpu, self._make_healthy_disk())
        self.assertFalse(all_passed)
        self.assertFalse(gates["no_competing_gpu"].passed)
        self.assertIn("UNKNOWN", gates["no_competing_gpu"].measured_str)

    # -----------------------------------------------------------------------
    # Test 7: Disk Telemetry Unavailable (Fail-Closed)
    # -----------------------------------------------------------------------
    def test_07_disk_telemetry_unavailable(self):
        ram = self._make_healthy_ram(avail_gib=12.0)
        gpu = self._make_healthy_gpu()
        disk = DiskSnapshot(
            drive="C:\\", free_bytes=0, total_bytes=0, free_gb=0.0, total_gb=0.0,
            status=TelemetryStatus.FAILED, error_message="GetDiskFreeSpaceExW access denied"
        )

        all_passed, gates = self.budget_mgr.evaluate_gates(ram, gpu, disk)
        self.assertFalse(all_passed)
        self.assertFalse(gates["disk_free"].passed)
        self.assertFalse(gates["telemetry_integrity"].passed)

    # -----------------------------------------------------------------------
    # Test 8: Model Child Exits Normally
    # -----------------------------------------------------------------------
    def test_08_model_child_exits_normally(self):
        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        status, ok = SafeReclamationManager.terminate_child_process(mock_proc, grace_period_sec=0.1)
        self.assertTrue(ok)
        self.assertIn("ALREADY_EXITED", status)
        mock_proc.kill.assert_not_called()

    # -----------------------------------------------------------------------
    # Test 9: Model Child Exits with CUDA OOM
    # -----------------------------------------------------------------------
    def test_09_model_child_exits_with_cuda_oom(self):
        mock_proc = MagicMock()
        mock_proc.pid = 9998
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1

        error_line = "CUDA error: out of memory in cudaMalloc(&ptr, size)"
        has_cuda_oom = "CUDA error" in error_line or "out of memory" in error_line.lower()
        self.assertTrue(has_cuda_oom)

        status, ok = SafeReclamationManager.terminate_child_process(mock_proc, grace_period_sec=0.1)
        self.assertTrue(ok)

    # -----------------------------------------------------------------------
    # Test 10: Child Termination Succeeds within Grace Period
    # -----------------------------------------------------------------------
    def test_10_child_termination_succeeds(self):
        mock_proc = MagicMock()
        mock_proc.pid = 9997
        # First poll: running, second poll after terminate: exited
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0

        status, ok = SafeReclamationManager.terminate_child_process(mock_proc, grace_period_sec=1.0)
        self.assertTrue(ok)
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_not_called()

    # -----------------------------------------------------------------------
    # Test 11: Child Termination Times Out and Escalates to Kill
    # -----------------------------------------------------------------------
    def test_11_child_termination_times_out(self):
        mock_proc = MagicMock()
        mock_proc.pid = 9996
        # Always running during grace period
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0

        status, ok = SafeReclamationManager.terminate_child_process(mock_proc, grace_period_sec=0.2)
        self.assertTrue(ok)
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        self.assertEqual(status, "KILLED_AFTER_TIMEOUT")

    # -----------------------------------------------------------------------
    # Test 12: Recovery Reporting Succeeds
    # -----------------------------------------------------------------------
    def test_12_recovery_reporting_succeeds(self):
        pre_ram = self._make_healthy_ram(avail_gib=12.0)
        pre_gpu = self._make_healthy_gpu(free_mib=5900.0)

        mock_proc = MagicMock()
        mock_proc.pid = 9995
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("nova.resource_steward.monitor.ResourceMonitor.get_system_memory") as mock_ram, \
             patch("nova.resource_steward.monitor.ResourceMonitor.get_gpu_telemetry") as mock_gpu, \
             patch("subprocess.run") as mock_run:

            mock_ram.return_value = self._make_healthy_ram(avail_gib=11.8)
            mock_gpu.return_value = self._make_healthy_gpu(free_mib=5890.0)
            mock_run.return_value = MagicMock(stdout="", returncode=0)

            report = SafeReclamationManager.perform_post_inference_recovery(
                pre_ram=pre_ram,
                pre_gpu=pre_gpu,
                peak_ram_gib=10.5,
                peak_vram_mib=4200.0,
                child_proc=mock_proc,
                grace_period_sec=0.1
            )

            self.assertTrue(report.recovery_successful)
            self.assertEqual(report.child_pid, 9995)
            self.assertEqual(report.pre_avail_ram_gib, 12.0)
            self.assertEqual(report.post_avail_ram_gib, 11.8)
            self.assertEqual(report.peak_pressure_ram_gib, 10.5)
            self.assertIn("Governance preserved", report.notes[-1])

if __name__ == "__main__":
    unittest.main()
