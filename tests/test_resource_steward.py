"""
NOVA Compute — Resource Steward Comprehensive Safety & Integration Test Suite
Validates:
- Supervisor integration (supervise_child_process) with deterministic fake clocks and queues
- Monotonic critical RAM timers and sample ordering/gap detection
- Hardened preflight commit gates (zero limit, NaN, inf, contradictory counters)
- CLI mutual exclusivity and preflight block guarantees
- Real Windows console break (Fixture A) and forced escalation (Fixture B)
- Truthful recovery reporting (thread timeouts, residual VRAM leaks, exact PID matching)
- Inference outcome verification (SUCCESS, UNVERIFIED, FAILED, ABORTED)
"""

import os
import sys
import time
import math
import queue
import tempfile
import signal
import subprocess
import unittest
from typing import Optional, List, Dict, Any, Callable
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
    SupervisorResult,
    InferenceOutcome,
    RecoveryReport
)
from nova.resource_steward.policy import ResourcePolicy
from nova.resource_steward.memory_budget import MemoryBudgetManager
from nova.resource_steward.advisor import MemoryAdvisor
from nova.resource_steward.recovery import SafeReclamationManager
from nova.resource_steward.monitor import ResourceMonitor
from nova.resource_steward.supervisor import supervise_child_process
from scripts.run_inference_with_telemetry import verify_inference_success

class FakeClock:
    """Deterministic monotonic clock for precise supervisor testing."""
    def __init__(self, start_time: float = 100.0):
        self._current_time = start_time

    def __call__(self) -> float:
        return self._current_time

    def advance(self, seconds: float):
        self._current_time += seconds

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

    def _make_healthy_ram(self, avail_gib: float = 12.0, commit_pct: float = 40.0,
                           commit_total_bytes: int = int(40 * 1024**3),
                           committed_bytes: Optional[int] = None,
                           status: TelemetryStatus = TelemetryStatus.SUCCESS) -> SystemMemorySnapshot:
        if committed_bytes is None:
            if math.isnan(commit_pct) or math.isinf(commit_pct):
                committed_bytes = 0
            else:
                committed_bytes = int(commit_total_bytes * (commit_pct / 100.0))
        avail_commit = max(0, commit_total_bytes - committed_bytes)
        return SystemMemorySnapshot(
            total_phys_bytes=int(16 * 1024**3),
            avail_phys_bytes=int(avail_gib * 1024**3),
            total_phys_gib=16.0,
            avail_phys_gib=avail_gib,
            commit_total_bytes=commit_total_bytes,
            commit_avail_bytes=avail_commit,
            committed_bytes=committed_bytes,
            commit_total_gib=round(commit_total_bytes / (1024**3), 3),
            commit_avail_gib=round(avail_commit / (1024**3), 3),
            committed_gib=round(committed_bytes / (1024**3), 3),
            commit_pct=commit_pct,
            memory_load_pct=30,
            status=status
        )

    def _make_healthy_gpu(self, free_mib: float = 5900.0, compute_apps: list = None,
                           status: TelemetryStatus = TelemetryStatus.SUCCESS) -> GPUSnapshot:
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

    def _make_sample(self, sample_id: int, mono_time: float, avail_ram_gib: float = 12.0,
                     commit_pct: float = 40.0, vram_free_mib: float = 5900.0,
                     ram_status: str = "SUCCESS", gpu_status: str = "SUCCESS") -> dict:
        return {
            "sample_id": sample_id,
            "timestamp_monotonic": mono_time,
            "system_ram": {
                "avail_phys_gib": avail_ram_gib,
                "commit_pct": commit_pct,
                "status": ram_status,
                "error": "RAM failure" if ram_status != "SUCCESS" else None
            },
            "gpu": {
                "vram_free_mib": vram_free_mib,
                "vram_used_mib": 6141.0 - vram_free_mib,
                "status": gpu_status,
                "error": "GPU failure" if gpu_status != "SUCCESS" else None
            }
        }

    # =======================================================================
    # SECTION A: CLI SAFETY & PREFLIGHT GATING
    # =======================================================================

    def test_01_default_invocation_never_starts_child(self):
        cmd = [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "run_inference_with_telemetry.py")]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertIn("[MODE] PREFLIGHT ONLY", res.stdout)
        self.assertNotIn("Spawning NOVA inference child process", res.stdout)

    def test_02_dry_run_never_starts_child(self):
        cmd = [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "run_inference_with_telemetry.py"), "--dry-run"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0)
        self.assertIn("[MODE] DRY RUN VALIDATION", res.stdout)
        self.assertIn("Dry run completed successfully. (No inference was started).", res.stdout)

    def test_03_failed_preflight_blocks_execution(self):
        from scripts.run_inference_with_telemetry import execute_inference
        from nova.resource_steward import ResourceSteward

        steward = ResourceSteward(self.policy)
        failing_ram = self._make_healthy_ram(avail_gib=8.5)
        failing_report = self.budget_mgr.generate_preflight_report(
            failing_ram, self._make_healthy_gpu(), self._make_healthy_disk(),
            top_processes=[], recommendations=[]
        )

        with patch.object(steward, "run_preflight", return_value=failing_report), \
             patch("subprocess.Popen") as mock_popen, \
             patch("sys.exit", side_effect=SystemExit(1)) as mock_exit:
            with self.assertRaises(SystemExit):
                execute_inference(steward)
            mock_popen.assert_not_called()
            mock_exit.assert_called_with(1)

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

    # =======================================================================
    # SECTION B: HARDENED SYSTEM COMMIT GATE
    # =======================================================================

    def test_05_commit_usage_at_90_fails_gate(self):
        ram = self._make_healthy_ram(avail_gib=12.0, commit_pct=90.5)
        all_passed, gates = self.budget_mgr.evaluate_gates(ram, self._make_healthy_gpu(), self._make_healthy_disk())
        self.assertFalse(all_passed)
        self.assertFalse(gates["system_commit"].passed)
        self.assertIn("exceeds critical threshold", gates["system_commit"].failure_reason)

    def test_06_missing_or_failed_commit_telemetry_fails_closed(self):
        ram = self._make_healthy_ram(avail_gib=12.0, status=TelemetryStatus.FAILED)
        all_passed, gates = self.budget_mgr.evaluate_gates(ram, self._make_healthy_gpu(), self._make_healthy_disk())
        self.assertFalse(all_passed)
        self.assertFalse(gates["system_commit"].passed)
        self.assertEqual(gates["system_commit"].measured_str, "INVALID / UNAVAILABLE")

    def test_07_invalid_commit_counters_fail_closed(self):
        # 07.1: Zero commit limit
        ram_zero = self._make_healthy_ram(commit_total_bytes=0, commit_pct=0.0)
        all_p, gates = self.budget_mgr.evaluate_gates(ram_zero, self._make_healthy_gpu(), self._make_healthy_disk())
        self.assertFalse(gates["system_commit"].passed)
        self.assertIn("Invalid commit limit", gates["system_commit"].failure_reason)

        # 07.2: Contradictory committed bytes > commit limit
        ram_contra = self._make_healthy_ram(commit_total_bytes=1000, committed_bytes=2000, commit_pct=50.0)
        all_p, gates = self.budget_mgr.evaluate_gates(ram_contra, self._make_healthy_gpu(), self._make_healthy_disk())
        self.assertFalse(gates["system_commit"].passed)
        self.assertIn("Contradictory commit metrics", gates["system_commit"].failure_reason)

        # 07.3: NaN commit percentage
        ram_nan = self._make_healthy_ram(commit_pct=float("nan"))
        all_p, gates = self.budget_mgr.evaluate_gates(ram_nan, self._make_healthy_gpu(), self._make_healthy_disk())
        self.assertFalse(gates["system_commit"].passed)

        # 07.4: Infinite commit percentage
        ram_inf = self._make_healthy_ram(commit_pct=float("inf"))
        all_p, gates = self.budget_mgr.evaluate_gates(ram_inf, self._make_healthy_gpu(), self._make_healthy_disk())
        self.assertFalse(gates["system_commit"].passed)

        # 07.5: Out of bounds (> 100%)
        ram_oob = self._make_healthy_ram(commit_pct=105.0)
        all_p, gates = self.budget_mgr.evaluate_gates(ram_oob, self._make_healthy_gpu(), self._make_healthy_disk())
        self.assertFalse(gates["system_commit"].passed)

    # =======================================================================
    # SECTION C: INTEGRATED SUPERVISOR TESTS (supervise_child_process)
    # =======================================================================

    def test_08_supervisor_ram_telemetry_failure_aborts(self):
        clock = FakeClock(100.0)
        s_queue = queue.Queue()
        o_queue = queue.Queue()
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, None, 0]

        # Put a failed RAM telemetry sample
        s_queue.put(self._make_sample(1, 100.0, ram_status="FAILED"))

        res = supervise_child_process(
            proc=mock_proc,
            sample_queue=s_queue,
            output_queue=o_queue,
            policy=self.policy,
            clock=clock,
            poll_sleep_sec=0.0
        )
        self.assertTrue(res.abort_triggered)
        self.assertIn("Required system RAM telemetry failed", res.abort_reason)

    def test_09_supervisor_gpu_telemetry_failure_aborts(self):
        clock = FakeClock(100.0)
        s_queue = queue.Queue()
        o_queue = queue.Queue()
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, None, 0]

        s_queue.put(self._make_sample(1, 100.0, gpu_status="FAILED"))

        res = supervise_child_process(
            proc=mock_proc,
            sample_queue=s_queue,
            output_queue=o_queue,
            policy=self.policy,
            clock=clock,
            poll_sleep_sec=0.0
        )
        self.assertTrue(res.abort_triggered)
        self.assertIn("Required GPU telemetry failed", res.abort_reason)

    def test_10_supervisor_stale_telemetry_aborts(self):
        clock = FakeClock(100.0)
        s_queue = queue.Queue()
        o_queue = queue.Queue()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None # Process keeps running

        # Sample 1 at 100.0s
        s_queue.put(self._make_sample(1, 100.0))

        def on_sample(s):
            # Advance clock past 3.0s stale threshold
            clock.advance(3.5)

        res = supervise_child_process(
            proc=mock_proc,
            sample_queue=s_queue,
            output_queue=o_queue,
            policy=self.policy,
            clock=clock,
            on_sample_processed=on_sample,
            stale_timeout_sec=3.0,
            poll_sleep_sec=0.0
        )
        self.assertTrue(res.abort_triggered)
        self.assertIn("unresponsive or stale", res.abort_reason)

    def test_11_supervisor_sample_ordering_and_gap_detection(self):
        clock = FakeClock(100.0)
        s_queue = queue.Queue()
        o_queue = queue.Queue()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        # Sample 1 ok, then Sample 3 (missing Sample 2)
        s_queue.put(self._make_sample(1, 100.0))
        s_queue.put(self._make_sample(3, 100.5))

        res = supervise_child_process(
            proc=mock_proc,
            sample_queue=s_queue,
            output_queue=o_queue,
            policy=self.policy,
            clock=clock,
            poll_sleep_sec=0.0
        )
        self.assertTrue(res.abort_triggered)
        self.assertIn("Telemetry sample gap detected: received 3, expected 2", res.abort_reason)

    def test_12_supervisor_low_ram_recovered_resets_timer(self):
        clock = FakeClock(100.0)
        s_queue = queue.Queue()
        o_queue = queue.Queue()
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, None, None, None, 0, 0, 0, 0]

        # Sample 1: Low RAM (1.5 GiB) at 100.0s
        s_queue.put(self._make_sample(1, 100.0, avail_ram_gib=1.5))
        # Sample 2: Low RAM at 103.0s (3s into 5s window)
        s_queue.put(self._make_sample(2, 103.0, avail_ram_gib=1.5))
        # Sample 3: RAM RECOVERED (11.0 GiB) at 104.0s (resets timer)
        s_queue.put(self._make_sample(3, 104.0, avail_ram_gib=11.0))
        # Sample 4: Low RAM again at 105.0s (must restart from 0, not abort)
        s_queue.put(self._make_sample(4, 105.0, avail_ram_gib=1.5))

        res = supervise_child_process(
            proc=mock_proc,
            sample_queue=s_queue,
            output_queue=o_queue,
            policy=self.policy,
            clock=clock,
            poll_sleep_sec=0.0
        )
        # Should not abort because timer was reset at sample 3
        self.assertFalse(res.abort_triggered)
        self.assertEqual(res.processed_samples_count, 4)

    def test_13_supervisor_low_ram_sustained_5s_aborts(self):
        clock = FakeClock(100.0)
        s_queue = queue.Queue()
        o_queue = queue.Queue()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        # Sample 1 at 100.0s (1.8 GiB)
        s_queue.put(self._make_sample(1, 100.0, avail_ram_gib=1.8))
        # Sample 2 at 105.2s (> 5.0s sustained)
        s_queue.put(self._make_sample(2, 105.2, avail_ram_gib=1.8))

        res = supervise_child_process(
            proc=mock_proc,
            sample_queue=s_queue,
            output_queue=o_queue,
            policy=self.policy,
            clock=clock,
            poll_sleep_sec=0.0
        )
        self.assertTrue(res.abort_triggered)
        self.assertIn("Available RAM sustained < 2.0 GiB for 5.20s", res.abort_reason)

    def test_14_supervisor_silent_stdout_does_not_block_telemetry(self):
        clock = FakeClock(100.0)
        s_queue = queue.Queue()
        o_queue = queue.Queue() # Empty output queue
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, None, 0, 0, 0, 0]

        s_queue.put(self._make_sample(1, 100.0))
        s_queue.put(self._make_sample(2, 100.5))

        res = supervise_child_process(
            proc=mock_proc,
            sample_queue=s_queue,
            output_queue=o_queue,
            policy=self.policy,
            clock=clock,
            poll_sleep_sec=0.0
        )
        self.assertFalse(res.abort_triggered)
        self.assertEqual(res.processed_samples_count, 2)
        self.assertEqual(len(res.output_lines), 0)

    def test_15_supervisor_cuda_oom_detected_from_queue(self):
        clock = FakeClock(100.0)
        s_queue = queue.Queue()
        o_queue = queue.Queue()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        s_queue.put(self._make_sample(1, 100.0))
        o_queue.put("ggml_cuda_init: allocating KV buffer\n")
        o_queue.put("CUDA error: out of memory in cudaMalloc\n")

        res = supervise_child_process(
            proc=mock_proc,
            sample_queue=s_queue,
            output_queue=o_queue,
            policy=self.policy,
            clock=clock,
            poll_sleep_sec=0.0
        )
        self.assertTrue(res.abort_triggered)
        self.assertIn("CUDA OOM / Error detected", res.abort_reason)

    # =======================================================================
    # SECTION D: RECOVERY VERIFICATION & REAL WINDOWS FIXTURES
    # =======================================================================

    def test_16_recovery_verification_strictness(self):
        pre_ram = self._make_healthy_ram(avail_gib=12.0)
        pre_gpu = self._make_healthy_gpu(free_mib=5900.0)

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_proc.stdout.closed = True
        mock_proc.stderr.closed = True
        mock_proc.stdin = None

        # 16.1: Leaked VRAM > 100 MiB fails recovery
        with patch("nova.resource_steward.monitor.ResourceMonitor.get_system_memory") as m_ram, \
             patch("nova.resource_steward.monitor.ResourceMonitor.get_gpu_telemetry") as m_gpu, \
             patch("subprocess.run") as m_run:

            m_ram.return_value = self._make_healthy_ram(avail_gib=11.8)
            # VRAM leaked by 150 MiB
            m_gpu.return_value = self._make_healthy_gpu(free_mib=5750.0)
            m_run.return_value = MagicMock(stdout="", returncode=0)

            rep = SafeReclamationManager.perform_post_inference_recovery(
                pre_ram=pre_ram, pre_gpu=pre_gpu, peak_ram_gib=10.0, peak_vram_mib=4200.0,
                child_proc=mock_proc, grace_period_sec=0.1
            )
            self.assertFalse(rep.recovery_successful)
            self.assertEqual(rep.residual_vram_leak_mib, 150.0)

        # 16.2: Unstopped reader or sampler thread fails recovery
        with patch("nova.resource_steward.monitor.ResourceMonitor.get_system_memory") as m_ram, \
             patch("nova.resource_steward.monitor.ResourceMonitor.get_gpu_telemetry") as m_gpu, \
             patch("subprocess.run") as m_run:

            m_ram.return_value = self._make_healthy_ram(avail_gib=11.8)
            m_gpu.return_value = self._make_healthy_gpu(free_mib=5890.0)
            m_run.return_value = MagicMock(stdout="", returncode=0)

            rep_thread_fail = SafeReclamationManager.perform_post_inference_recovery(
                pre_ram=pre_ram, pre_gpu=pre_gpu, peak_ram_gib=10.0, peak_vram_mib=4200.0,
                child_proc=mock_proc, grace_period_sec=0.1,
                reader_thread_stopped=False
            )
            self.assertFalse(rep_thread_fail.recovery_successful)
            self.assertFalse(rep_thread_fail.reader_thread_stopped)

    def test_17_real_windows_fixture_a_graceful_acknowledgement(self):
        """Fixture A: Tests genuine console break handler execution and acknowledgement marker."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            marker_file = tf.name

        try:
            fixture_path = os.path.join(PROJECT_ROOT, "tests", "fixtures", "graceful_child.py")
            cmd = [sys.executable, fixture_path, marker_file]
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=flags
            )

            # Wait for child to be READY
            line = proc.stdout.readline()
            self.assertEqual(line.strip(), "READY")

            term_status, method, ok = SafeReclamationManager.terminate_child_process(proc, grace_period_sec=3.0)
            self.assertTrue(ok)
            self.assertEqual(method, "CTRL_BREAK" if sys.platform == "win32" else "TERMINATE_SIGNAL")
            self.assertEqual(proc.returncode, 0)

            # Assert acknowledgement marker file was written by the signal handler
            with open(marker_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertEqual(content, "ACKNOWLEDGED_SIGBREAK")

        finally:
            if proc.poll() is None:
                proc.kill()
            if os.path.exists(marker_file):
                try: os.remove(marker_file)
                except Exception: pass

    def test_18_real_windows_fixture_b_forced_escalation(self):
        """Fixture B: Tests that an uncooperative process that ignores CTRL_BREAK escalates to kill()."""
        fixture_path = os.path.join(PROJECT_ROOT, "tests", "fixtures", "stubborn_child.py")
        cmd = [sys.executable, fixture_path]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=flags
        )

        try:
            line = proc.stdout.readline()
            self.assertEqual(line.strip(), "READY")

            # Grace period is short (0.3s) so escalation to kill() happens quickly
            term_status, method, ok = SafeReclamationManager.terminate_child_process(proc, grace_period_sec=0.3)
            self.assertTrue(ok)
            self.assertEqual(method, "FORCED_TERMINATION")
            self.assertTrue(proc.poll() is not None)
        finally:
            if proc.poll() is None:
                proc.kill()

    # =======================================================================
    # SECTION E: INFERENCE SUCCESS VERIFIER
    # =======================================================================

    def test_19_inference_verifier_scenarios(self):
        rec_ok = MagicMock()
        rec_ok.recovery_successful = True

        rec_failed = MagicMock()
        rec_failed.recovery_successful = False

        # Scenario 1: Clean successful generation with timing tokens
        sup_ok = SupervisorResult(
            abort_triggered=False,
            abort_reason="",
            exit_code=0,
            peak_ram_gib=10.0,
            peak_vram_mib=4200.0,
            processed_samples_count=5,
            duration_seconds=1.2,
            output_lines=["load time = 500 ms\n", "prompt eval time = 50 ms\n", "eval time = 20 ms\n", "print_timings: done\n"]
        )
        outcome, reason = verify_inference_success(sup_ok, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.SUCCESS)

        # Scenario 2: Ambiguous output (missing eval tokens)
        sup_ambiguous = SupervisorResult(
            abort_triggered=False,
            abort_reason="",
            exit_code=0,
            peak_ram_gib=10.0,
            peak_vram_mib=4200.0,
            processed_samples_count=5,
            duration_seconds=1.2,
            output_lines=["llama_model_loader: loaded 579 tensors\n"]
        )
        outcome, reason = verify_inference_success(sup_ambiguous, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)

        # Scenario 3: Unverified recovery
        outcome, reason = verify_inference_success(sup_ok, rec_failed)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)

        # Scenario 4: Non-zero exit code
        sup_err = SupervisorResult(
            abort_triggered=False,
            abort_reason="",
            exit_code=1,
            peak_ram_gib=10.0,
            peak_vram_mib=4200.0,
            processed_samples_count=5,
            duration_seconds=1.2,
            output_lines=[]
        )
        outcome, reason = verify_inference_success(sup_err, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.FAILED)

        # Scenario 5: Safety abort
        sup_abort = SupervisorResult(
            abort_triggered=True,
            abort_reason="CRITICAL ABORT: Available RAM sustained < 2.0 GiB",
            exit_code=None,
            peak_ram_gib=1.5,
            peak_vram_mib=4200.0,
            processed_samples_count=5,
            duration_seconds=5.1,
            output_lines=[]
        )
        outcome, reason = verify_inference_success(sup_abort, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.ABORTED)

if __name__ == "__main__":
    unittest.main()
