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
import json
import queue
import tempfile
import signal
import subprocess
import threading
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
    RecoveryReport,
    MemoryPressureLevel,
    ReclamationStatus,
    MemoryReclaimReport
)
from nova.resource_steward.policy import ResourcePolicy
from nova.resource_steward.memory_budget import MemoryBudgetManager
from nova.resource_steward.advisor import MemoryAdvisor
from nova.resource_steward.recovery import SafeReclamationManager
from nova.resource_steward.monitor import ResourceMonitor
from nova.resource_steward.supervisor import supervise_child_process
from nova.resource_steward.adaptive_memory import (
    MemoryPressureDetector,
    ReclamationCooldownManager,
    SafeMemoryReclaimer,
    AdaptiveMemoryController
)
from nova.resource_steward import ResourceSteward
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
        # Isolate all tests to a temporary directory so tracked reports are never modified
        self._orig_env_report_dir = os.environ.get("NOVA_REPORT_DIR")
        self._orig_env_amr_dir = os.environ.get("NOVA_AMR_REPORT_DIR")
        self.temp_test_dir = tempfile.TemporaryDirectory()
        self.inference_reports_dir = os.path.join(self.temp_test_dir.name, "first_inference")
        self.amr_reports_dir = os.path.join(self.temp_test_dir.name, "memory_reclamation")
        os.makedirs(self.inference_reports_dir, exist_ok=True)
        os.makedirs(self.amr_reports_dir, exist_ok=True)
        os.environ["NOVA_REPORT_DIR"] = self.inference_reports_dir
        os.environ["NOVA_AMR_REPORT_DIR"] = self.amr_reports_dir

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

    def tearDown(self):
        # Restore original environment variables rather than unconditionally deleting
        if self._orig_env_report_dir is not None:
            os.environ["NOVA_REPORT_DIR"] = self._orig_env_report_dir
        else:
            os.environ.pop("NOVA_REPORT_DIR", None)

        if self._orig_env_amr_dir is not None:
            os.environ["NOVA_AMR_REPORT_DIR"] = self._orig_env_amr_dir
        else:
            os.environ.pop("NOVA_AMR_REPORT_DIR", None)

        self.temp_test_dir.cleanup()

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
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=dict(os.environ))
        self.assertIn("[MODE] PREFLIGHT ONLY", res.stdout)
        self.assertNotIn("Spawning NOVA inference child process", res.stdout)

    def test_02_dry_run_never_starts_child(self):
        cmd = [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "run_inference_with_telemetry.py"), "--dry-run"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=dict(os.environ))
        self.assertEqual(res.returncode, 0)
        self.assertIn("[MODE] DRY RUN VALIDATION", res.stdout)
        self.assertIn("Dry run completed successfully. (No inference was started).", res.stdout)

    def test_03_failed_preflight_blocks_execution(self):
        from scripts.run_inference_with_telemetry import execute_inference
        from nova.resource_steward import ResourceSteward

        steward = ResourceSteward(self.policy, reports_dir=self.amr_reports_dir)
        failing_ram = self._make_healthy_ram(avail_gib=8.5)
        failing_report = self.budget_mgr.generate_preflight_report(
            failing_ram, self._make_healthy_gpu(), self._make_healthy_disk(),
            top_processes=[], recommendations=[]
        )

        with patch.object(steward, "run_preflight", return_value=failing_report), \
             patch("subprocess.Popen") as mock_popen, \
             patch("sys.exit", side_effect=SystemExit(1)) as mock_exit:
            with self.assertRaises(SystemExit):
                execute_inference(steward, output_dir=self.inference_reports_dir)
            mock_popen.assert_not_called()
            mock_exit.assert_called_with(1)

    def test_04_conflicting_cli_modes_rejected(self):
        cmd = [
            sys.executable,
            os.path.join(PROJECT_ROOT, "scripts", "run_inference_with_telemetry.py"),
            "--execute",
            "--dry-run"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5, env=dict(os.environ))
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
        clock = FakeClock(105.0)
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
        clock = FakeClock(105.2)
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
        clock = FakeClock(100.5)
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
            output_lines=[
                "load time = 500.00 ms\n",
                "prompt eval time = 50.00 ms / 10 tokens\n",
                "eval time = 20.00 ms / 1 runs ( 20.00 ms per token, 50.00 tokens per second)\n",
                "print_timings: done\n"
            ]
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

    # =======================================================================
    # SECTION F: TIMESTAMP, SAMPLE & COMMIT CONSISTENCY VALIDATION
    # =======================================================================

    def test_20_supervisor_future_timestamp_aborts(self):
        clock = FakeClock(100.0)
        s_queue = queue.Queue()
        o_queue = queue.Queue()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        # Sample timestamp is 100.5 while clock is 100.0 (> clock + 0.1)
        s_queue.put(self._make_sample(1, 100.5))

        res = supervise_child_process(
            proc=mock_proc,
            sample_queue=s_queue,
            output_queue=o_queue,
            policy=self.policy,
            clock=clock,
            poll_sleep_sec=0.0
        )
        self.assertTrue(res.abort_triggered)
        self.assertIn("in the future", res.abort_reason)

    def test_21_supervisor_backward_timestamp_aborts(self):
        clock = FakeClock(105.0)
        s_queue = queue.Queue()
        o_queue = queue.Queue()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        # Sample 1 at 102.0, Sample 2 moved backward to 101.0
        s_queue.put(self._make_sample(1, 102.0))
        s_queue.put(self._make_sample(2, 101.0))

        res = supervise_child_process(
            proc=mock_proc,
            sample_queue=s_queue,
            output_queue=o_queue,
            policy=self.policy,
            clock=clock,
            poll_sleep_sec=0.0
        )
        self.assertTrue(res.abort_triggered)
        self.assertIn("timestamp moved backward", res.abort_reason)

    def test_22_supervisor_invalid_sample_id_types_abort(self):
        for bad_id in [True, False, 1.0, "1", None, [1]]:
            clock = FakeClock(100.0)
            s_queue = queue.Queue()
            o_queue = queue.Queue()
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None

            s = self._make_sample(1, 100.0)
            s["sample_id"] = bad_id
            s_queue.put(s)

            res = supervise_child_process(
                proc=mock_proc,
                sample_queue=s_queue,
                output_queue=o_queue,
                policy=self.policy,
                clock=clock,
                poll_sleep_sec=0.0
            )
            self.assertTrue(res.abort_triggered, f"Failed to abort on bad sample_id {bad_id!r}")
            self.assertIn("invalid sample_id", res.abort_reason)

    def test_23_supervisor_non_finite_metrics_abort(self):
        for bad_metric, bad_val in [("avail_phys_gib", -1.0), ("avail_phys_gib", float("nan")),
                                    ("commit_pct", 105.0), ("commit_pct", -5.0)]:
            clock = FakeClock(100.0)
            s_queue = queue.Queue()
            o_queue = queue.Queue()
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None

            s = self._make_sample(1, 100.0)
            s["system_ram"][bad_metric] = bad_val
            s_queue.put(s)

            res = supervise_child_process(
                proc=mock_proc,
                sample_queue=s_queue,
                output_queue=o_queue,
                policy=self.policy,
                clock=clock,
                poll_sleep_sec=0.0
            )
            self.assertTrue(res.abort_triggered, f"Failed to abort on {bad_metric}={bad_val}")
            self.assertIn("invalid, negative, or non-finite metrics", res.abort_reason)

    def test_24_commit_consistency_counter_mismatch_fails_gate(self):
        total = int(40 * 1024**3)
        committed = int(16 * 1024**3)
        # Contradictory available: sum is 100 MB short of total
        avail = total - committed - (100 * 1024**2)

        snap = SystemMemorySnapshot(
            total_phys_bytes=int(16 * 1024**3),
            avail_phys_bytes=int(12 * 1024**3),
            total_phys_gib=16.0,
            avail_phys_gib=12.0,
            commit_total_bytes=total,
            commit_avail_bytes=avail,
            committed_bytes=committed,
            commit_total_gib=40.0,
            commit_avail_gib=round(avail / (1024**3), 3),
            committed_gib=16.0,
            commit_pct=40.0,
            memory_load_pct=30,
            status=TelemetryStatus.SUCCESS
        )
        gpu = self._make_healthy_gpu()
        disk = self._make_healthy_disk()

        passed, gates = self.budget_mgr.evaluate_gates(snap, gpu, disk)
        self.assertFalse(passed)
        self.assertFalse(gates["system_commit"].passed)
        self.assertIn("Contradictory commit metrics: committed + avail", gates["system_commit"].failure_reason)

    def test_25_commit_consistency_pct_mismatch_fails_gate(self):
        total = int(40 * 1024**3)
        committed = int(16 * 1024**3) # 40.0%
        avail = total - committed
        # Contradictory percentage: reported 50.0% vs calculated 40.0% (delta 10% > 1%)
        snap = SystemMemorySnapshot(
            total_phys_bytes=int(16 * 1024**3),
            avail_phys_bytes=int(12 * 1024**3),
            total_phys_gib=16.0,
            avail_phys_gib=12.0,
            commit_total_bytes=total,
            commit_avail_bytes=avail,
            committed_bytes=committed,
            commit_total_gib=40.0,
            commit_avail_gib=24.0,
            committed_gib=16.0,
            commit_pct=50.0,
            memory_load_pct=30,
            status=TelemetryStatus.SUCCESS
        )
        gpu = self._make_healthy_gpu()
        disk = self._make_healthy_disk()

        passed, gates = self.budget_mgr.evaluate_gates(snap, gpu, disk)
        self.assertFalse(passed)
        self.assertFalse(gates["system_commit"].passed)
        self.assertIn("Contradictory commit percentage", gates["system_commit"].failure_reason)

    def test_26_inference_verifier_zero_and_missing_runs_rejected(self):
        rec_ok = MagicMock()
        rec_ok.recovery_successful = True
        rec_ok.reader_thread_stopped = True

        def make_sup(output_lines):
            return SupervisorResult(
                abort_triggered=False,
                abort_reason="",
                exit_code=0,
                peak_ram_gib=10.0,
                peak_vram_mib=4200.0,
                processed_samples_count=5,
                duration_seconds=1.2,
                output_lines=output_lines,
                reader_thread_stopped=True
            )

        # 1. Output with 0 runs (prompt eval only) -> MUST be UNVERIFIED
        sup_zero_runs = make_sup([
            "load time = 500 ms\n",
            "prompt eval time = 50.00 ms / 10 tokens\n",
            "eval time = 0.00 ms / 0 runs\n",
            "print_timings: done\n"
        ])
        outcome, reason = verify_inference_success(sup_zero_runs, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)
        self.assertIn("0 generated tokens", reason)

        # 2. Output with eval time without explicit run count -> MUST be UNVERIFIED
        sup_missing_count = make_sup([
            "load time = 500 ms\n",
            "prompt eval time = 50.00 ms / 10 tokens\n",
            "eval time = 25.00 ms\n",
            "print_timings: done\n"
        ])
        outcome, reason = verify_inference_success(sup_missing_count, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)
        self.assertIn("ambiguous eval timing without explicit run count", reason)

        # 3. Output with prompt eval only (no eval time at all) -> MUST be UNVERIFIED
        sup_prompt_only = make_sup([
            "load time = 500 ms\n",
            "prompt eval time = 50.00 ms / 10 tokens\n",
            "print_timings: done\n"
        ])
        outcome, reason = verify_inference_success(sup_prompt_only, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)
        self.assertIn("prompt evaluation only", reason)

        # 4. Output with tokens generated: 0 -> MUST be UNVERIFIED
        sup_tokens_zero = make_sup([
            "load time = 500 ms\n",
            "tokens generated: 0\n",
            "print_timings: done\n"
        ])
        outcome, reason = verify_inference_success(sup_tokens_zero, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)

        # 5. Output with load time or print_timings alone -> MUST be UNVERIFIED
        sup_timings_only = make_sup([
            "load time = 500 ms\n",
            "print_timings: done\n"
        ])
        outcome, reason = verify_inference_success(sup_timings_only, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)

        # 6. Malformed timing output -> MUST be UNVERIFIED
        sup_malformed = make_sup([
            "load time = 500 ms\n",
            "eval time = invalid / runs\n",
            "print_timings: done\n"
        ])
        outcome, reason = verify_inference_success(sup_malformed, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)

        # 7. Positive generated token count (/ 1 runs) -> MUST be SUCCESS
        sup_one_run = make_sup([
            "load time = 500 ms\n",
            "prompt eval time = 50.00 ms / 10 tokens\n",
            "eval time = 25.00 ms / 1 runs ( 25.00 ms per token, 40.00 tokens per second)\n",
            "print_timings: done\n"
        ])
        outcome, reason = verify_inference_success(sup_one_run, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.SUCCESS)

        # 8. Positive tokens generated count (tokens generated: 1) -> MUST be SUCCESS
        sup_tokens_one = make_sup([
            "load time = 500 ms\n",
            "tokens generated: 1\n",
            "print_timings: done\n"
        ])
        outcome, reason = verify_inference_success(sup_tokens_one, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.SUCCESS)

        # 9. Reader thread not stopped -> MUST be UNVERIFIED even with positive tokens
        sup_reader_unstopped = make_sup([
            "load time = 500 ms\n",
            "eval time = 25.00 ms / 1 runs\n",
            "print_timings: done\n"
        ])
        sup_reader_unstopped.reader_thread_stopped = False
        outcome, reason = verify_inference_success(sup_reader_unstopped, rec_ok)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)
        self.assertIn("Full resource recovery could not be verified", reason)

    # =======================================================================
    # SECTION G: ADAPTIVE MEMORY RECLAIMER (AMR)
    # =======================================================================

    def test_27_amr_pressure_detection_levels(self):
        detector = MemoryPressureDetector(self.policy)

        # 1. NORMAL (>= 12.0 GiB)
        snap_normal = self._make_healthy_ram(avail_gib=12.5)
        self.assertEqual(detector.evaluate_pressure(snap_normal), MemoryPressureLevel.NORMAL)

        # 2. OPTIMIZATION (10.0 GiB <= RAM < 12.0 GiB)
        snap_opt = self._make_healthy_ram(avail_gib=11.0)
        self.assertEqual(detector.evaluate_pressure(snap_opt), MemoryPressureLevel.OPTIMIZATION)

        # 3. CONSTRAINED (< 10.0 GiB)
        snap_const = self._make_healthy_ram(avail_gib=9.0)
        self.assertEqual(detector.evaluate_pressure(snap_const), MemoryPressureLevel.CONSTRAINED)

        # 4. CRITICAL (< 2.0 GiB or commit >= 90%)
        snap_crit_ram = self._make_healthy_ram(avail_gib=1.8)
        self.assertEqual(detector.evaluate_pressure(snap_crit_ram), MemoryPressureLevel.CRITICAL)

        snap_crit_commit = self._make_healthy_ram(avail_gib=12.0, commit_pct=92.0)
        self.assertEqual(detector.evaluate_pressure(snap_crit_commit), MemoryPressureLevel.CRITICAL)

        # 5. Sensor failure -> CRITICAL (Fail-Closed)
        snap_failed = self._make_healthy_ram(status=TelemetryStatus.FAILED)
        self.assertEqual(detector.evaluate_pressure(snap_failed), MemoryPressureLevel.CRITICAL)

        # 6. Reject invalid input types as CRITICAL
        for bad_val in [None, True, False, "12.0", float("nan"), float("inf"), -1.0]:
            snap_bad = self._make_healthy_ram(avail_gib=12.0)
            snap_bad.avail_phys_gib = bad_val
            self.assertEqual(detector.evaluate_pressure(snap_bad), MemoryPressureLevel.CRITICAL,
                             f"Failed to reject bad avail_phys_gib {bad_val!r}")

        for bad_bytes in [None, True, False, "bytes", -100]:
            snap_bad = self._make_healthy_ram(avail_gib=12.0)
            snap_bad.avail_phys_bytes = bad_bytes
            self.assertEqual(detector.evaluate_pressure(snap_bad), MemoryPressureLevel.CRITICAL,
                             f"Failed to reject bad avail_phys_bytes {bad_bytes!r}")

        for bad_commit in [None, True, False, "40.0", float("nan"), float("inf"), -5.0, 105.0]:
            snap_bad = self._make_healthy_ram(avail_gib=12.0)
            snap_bad.commit_pct = bad_commit
            self.assertEqual(detector.evaluate_pressure(snap_bad), MemoryPressureLevel.CRITICAL,
                             f"Failed to reject bad commit_pct {bad_commit!r}")

    def test_28_amr_cooldown_throttling_and_reset(self):
        clock = FakeClock(100.0)
        cd_mgr = ReclamationCooldownManager(cooldown_seconds=30.0, clock=clock)

        # Initially allowed
        self.assertTrue(cd_mgr.can_reclaim())
        self.assertEqual(cd_mgr.remaining_cooldown_seconds, 0.0)

        # Record reclamation at t=100.0
        cd_mgr.record_reclamation()
        self.assertFalse(cd_mgr.can_reclaim())
        self.assertEqual(cd_mgr.remaining_cooldown_seconds, 30.0)

        # Advance 15s (still cooling down)
        clock.advance(15.0)
        self.assertFalse(cd_mgr.can_reclaim())
        self.assertEqual(cd_mgr.remaining_cooldown_seconds, 15.0)

        # Advance another 16s (total 31s: cooldown expired)
        clock.advance(16.0)
        self.assertTrue(cd_mgr.can_reclaim())
        self.assertEqual(cd_mgr.remaining_cooldown_seconds, 0.0)

        # Test reset
        cd_mgr.record_reclamation()
        self.assertFalse(cd_mgr.can_reclaim())
        cd_mgr.reset()
        self.assertTrue(cd_mgr.can_reclaim())

    def test_29_amr_safe_reclaimer_no_exceptions(self):
        reclaimer = SafeMemoryReclaimer()
        attempted, completed, failed, dur = reclaimer.reclaim()
        self.assertIn("python_gc_collect", attempted)
        self.assertTrue(any("python_gc_collect" in c for c in completed))
        self.assertEqual(len(failed), 0)
        self.assertGreaterEqual(dur, 0.0)

    def test_30_amr_delta_measurement_and_status(self):
        clock = FakeClock(100.0)
        mock_monitor = MagicMock()
        curr_pid = os.getpid()
        with tempfile.TemporaryDirectory() as tmp_dir:
            controller = AdaptiveMemoryController(
                policy=self.policy,
                monitor=mock_monitor,
                reports_dir=tmp_dir,
                clock=clock
            )

            # Scenario A: Normal RAM -> NO_ACTION_REQUIRED
            mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=12.5)
            mock_monitor.get_process_memory.return_value = MagicMock(
                pid=curr_pid, status=TelemetryStatus.SUCCESS,
                working_set_bytes=20 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024
            )
            rep_a = controller.evaluate_and_reclaim()
            self.assertEqual(rep_a.overall_status, ReclamationStatus.NO_ACTION_REQUIRED)
            self.assertEqual(rep_a.observed_system_avail_delta_bytes, 0)
            self.assertEqual(rep_a.ram_delta_gib, 0.0)

            # Scenario B: Optimization RAM with demonstrable private-commit reduction (>= 1 MiB)
            # -> RECLAMATION_COMPLETED
            mock_monitor.get_system_memory.side_effect = [
                self._make_healthy_ram(avail_gib=11.00), # Pre
                self._make_healthy_ram(avail_gib=11.05)  # Post
            ]
            # Mock process memory showing 2 MB working set and 2 MB private commit reduction
            mock_proc_pre = MagicMock(pid=curr_pid, status=TelemetryStatus.SUCCESS,
                                      working_set_bytes=20 * 1024 * 1024, private_commit_bytes=12 * 1024 * 1024)
            mock_proc_post = MagicMock(pid=curr_pid, status=TelemetryStatus.SUCCESS,
                                       working_set_bytes=18 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024)
            mock_monitor.get_process_memory.side_effect = [mock_proc_pre, mock_proc_post]

            rep_b = controller.evaluate_and_reclaim(force=True)
            self.assertEqual(rep_b.overall_status, ReclamationStatus.RECLAMATION_COMPLETED)
            self.assertEqual(rep_b.nova_process_working_set_delta_bytes, 2 * 1024 * 1024)
            self.assertEqual(rep_b.nova_process_private_delta_bytes, 2 * 1024 * 1024)
            self.assertGreater(rep_b.observed_system_avail_delta_bytes, 0)

            # Scenario C: Process private commit shows no measurable reduction (< 1 MiB)
            # -> NO_MEASURABLE_IMPROVEMENT (truthful reporting: do not attribute external RAM changes to AMR)
            mock_monitor.get_system_memory.side_effect = [
                self._make_healthy_ram(avail_gib=9.000), # Pre
                self._make_healthy_ram(avail_gib=9.050)  # Post (host RAM grew, but not from NOVA)
            ]
            mock_proc_flat = MagicMock(pid=curr_pid, status=TelemetryStatus.SUCCESS,
                                       working_set_bytes=18 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024)
            mock_monitor.get_process_memory.side_effect = [mock_proc_flat, mock_proc_flat]

            rep_c = controller.evaluate_and_reclaim(force=True)
            self.assertEqual(rep_c.overall_status, ReclamationStatus.NO_MEASURABLE_IMPROVEMENT)
            self.assertEqual(rep_c.nova_process_working_set_delta_bytes, 0)
            self.assertEqual(rep_c.nova_process_private_delta_bytes, 0)

    def test_31_amr_reports_saved_to_json_across_all_outcomes(self):
        curr_pid = os.getpid()
        with tempfile.TemporaryDirectory() as tmp_dir:
            mock_monitor = MagicMock()
            controller = AdaptiveMemoryController(
                policy=self.policy,
                monitor=mock_monitor,
                reports_dir=tmp_dir
            )
            mock_proc_flat = MagicMock(pid=curr_pid, status=TelemetryStatus.SUCCESS,
                                       working_set_bytes=18 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024)

            # 1. Action outcome with verified private commit reduction
            mock_monitor.get_system_memory.side_effect = [
                self._make_healthy_ram(avail_gib=11.0),
                self._make_healthy_ram(avail_gib=11.1)
            ]
            mock_proc_pre = MagicMock(pid=curr_pid, status=TelemetryStatus.SUCCESS,
                                      working_set_bytes=20 * 1024 * 1024, private_commit_bytes=12 * 1024 * 1024)
            mock_proc_post = MagicMock(pid=curr_pid, status=TelemetryStatus.SUCCESS,
                                       working_set_bytes=18 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024)
            mock_monitor.get_process_memory.side_effect = [mock_proc_pre, mock_proc_post]
            rep_action = controller.evaluate_and_reclaim()
            latest_path = os.path.join(tmp_dir, "reclaim_latest.json")
            self.assertTrue(os.path.exists(latest_path))

            with open(latest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["overall_status"], ReclamationStatus.RECLAMATION_COMPLETED.value)
            self.assertIn("observed_system_avail_delta_bytes", data)
            self.assertIn("nova_process_working_set_delta_bytes", data)
            self.assertIn("nova_process_private_delta_bytes", data)
            self.assertEqual(data["nova_process_private_delta_bytes"], 2 * 1024 * 1024)

            # 2. Cooldown outcome (second call within cooldown)
            mock_monitor.get_system_memory.side_effect = None
            mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=11.0)
            mock_monitor.get_process_memory.side_effect = None
            mock_monitor.get_process_memory.return_value = mock_proc_flat
            rep_cd = controller.evaluate_and_reclaim()
            self.assertTrue(rep_cd.cooldown_applied)
            with open(latest_path, "r", encoding="utf-8") as f:
                data_cd = json.load(f)
            self.assertTrue(data_cd["cooldown_applied"])

            # 3. Normal / No-action outcome
            controller.cooldown_mgr.reset()
            mock_monitor.get_system_memory.side_effect = None
            mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=13.0)
            mock_monitor.get_process_memory.side_effect = None
            mock_monitor.get_process_memory.return_value = mock_proc_flat
            rep_norm = controller.evaluate_and_reclaim()
            self.assertEqual(rep_norm.overall_status, ReclamationStatus.NO_ACTION_REQUIRED)
            with open(latest_path, "r", encoding="utf-8") as f:
                data_norm = json.load(f)
            self.assertEqual(data_norm["overall_status"], ReclamationStatus.NO_ACTION_REQUIRED.value)

            # 4. Telemetry failure outcome
            controller.cooldown_mgr.reset()
            mock_monitor.get_system_memory.side_effect = None
            mock_monitor.get_system_memory.return_value = self._make_healthy_ram(status=TelemetryStatus.FAILED)
            rep_fail = controller.evaluate_and_reclaim()
            self.assertEqual(rep_fail.overall_status, ReclamationStatus.RECLAMATION_FAILED)
            with open(latest_path, "r", encoding="utf-8") as f:
                data_fail = json.load(f)
            self.assertEqual(data_fail["overall_status"], ReclamationStatus.RECLAMATION_FAILED.value)

    def test_32_static_inspection_forbidden_apis_absent(self):
        """
        Static inspection: verifies forbidden Windows memory cleaner APIs are NEVER called or imported
        across all Python source code in both nova/ and scripts/.
        """
        import ast
        scan_dirs = [
            os.path.join(PROJECT_ROOT, "nova"),
            os.path.join(PROJECT_ROOT, "scripts")
        ]
        forbidden = [
            "EmptyWorkingSet",
            "K32EmptyWorkingSet",
            "NtSetSystemInformation",
            "MemoryPurgeStandbyList",
            "MemoryFlushModifiedList",
            "SetSystemFileCacheSize"
        ]

        scanned_files_count = 0
        for scan_dir in scan_dirs:
            for root, _, files in os.walk(scan_dir):
                for fname in files:
                    if fname.endswith(".py"):
                        scanned_files_count += 1
                        fpath = os.path.join(root, fname)
                        with open(fpath, "r", encoding="utf-8") as f:
                            code = f.read()

                        tree = ast.parse(code, filename=fpath)
                        for node in ast.walk(tree):
                            # Check function calls
                            if isinstance(node, ast.Call):
                                if isinstance(node.func, ast.Name):
                                    for forb in forbidden:
                                        self.assertNotEqual(node.func.id, forb, f"Forbidden call {forb} in {fpath}")
                                elif isinstance(node.func, ast.Attribute):
                                    for forb in forbidden:
                                        self.assertNotEqual(node.func.attr, forb, f"Forbidden attribute call {forb} in {fpath}")
                            # Check imports
                            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                                for forb in forbidden:
                                    if isinstance(node, ast.ImportFrom) and node.module:
                                        self.assertNotIn(forb, node.module, f"Forbidden import {forb} in {fpath}")
                                    for alias in node.names:
                                        self.assertNotEqual(alias.name, forb, f"Forbidden import alias {forb} in {fpath}")

        self.assertGreaterEqual(scanned_files_count, 10, "Expected to scan at least 10 Python source files")

    def test_33_preflight_auto_reclaim_integration(self):
        """Verifies ResourceSteward run_preflight integrates AMR and still fails-closed if RAM < 10 GiB."""
        steward = ResourceSteward(self.policy, reports_dir=self.amr_reports_dir)
        # Mock monitor to return 9.5 GiB (Constrained)
        steward.monitor.get_system_memory = MagicMock(return_value=self._make_healthy_ram(avail_gib=9.5))
        steward.monitor.get_gpu_telemetry = MagicMock(return_value=self._make_healthy_gpu())
        steward.monitor.get_disk_space = MagicMock(return_value=self._make_healthy_disk())
        steward.monitor.list_top_processes = MagicMock(return_value=[])

        report = steward.run_preflight()
        # Even with auto-reclaim evaluated, if available RAM remains 9.5 GiB < 10.0 GiB, preflight MUST fail
        self.assertFalse(report.passed)
        self.assertFalse(report.gates["avail_ram"].passed)

    # =======================================================================
    # SECTION H: PRODUCTION-PATH REGRESSION: DELAYED TRAILING OUTPUT & EOF
    # =======================================================================

    def test_34_supervisor_retains_trailing_output_after_child_exit_and_detects_fatal_marker(self):
        """
        Production-path regression test:
        Deliberately delays final output line until process terminates.
        Proves:
          1. Reader thread continues until true EOF.
          2. Trailing output line is retained in output_lines.
          3. Fatal CUDA OOM marker in trailing output triggers abort.
          4. Recovery verifies reader thread shutdown.
        """
        from scripts.run_inference_with_telemetry import start_output_reader

        fixture_script = os.path.join(PROJECT_ROOT, "tests", "fixtures", "delayed_trailing_child.py")
        cmd = [sys.executable, "-u", fixture_script]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        out_queue = queue.Queue()
        reader_stop = threading.Event()
        reader_thread = start_output_reader(proc.stdout, out_queue, reader_stop)

        # Supervise child with mock telemetry queue
        s_queue = queue.Queue()
        # Put initial healthy sample with current monotonic time
        s_queue.put(self._make_sample(1, time.monotonic()))

        supervisor_res = supervise_child_process(
            proc=proc,
            sample_queue=s_queue,
            output_queue=out_queue,
            policy=self.policy,
            clock=time.monotonic,
            poll_sleep_sec=0.01,
            reader_thread=reader_thread,
            reader_timeout_sec=2.0
        )

        # Ensure reader finished and close pipes
        reader_stop.set()
        reader_thread.join(timeout=2.0)
        reader_stopped = not reader_thread.is_alive()
        pipes_closed = SafeReclamationManager.close_process_pipes(proc)

        # Assertions
        self.assertTrue(reader_stopped, "Reader thread must terminate cleanly at true EOF")
        self.assertTrue(pipes_closed, "Process pipes must close cleanly after reader completes")

        # Prove trailing line was retained
        full_output = "".join(supervisor_res.output_lines)
        self.assertIn("line 1: model loader initialized", full_output)
        self.assertIn("line 2: prompt eval time = 50.00 ms / 10 tokens", full_output)
        self.assertIn("CUDA error: out of memory in trailing buffer", full_output,
                      "Trailing output line after delay must be retained in output_lines")

        # Prove fatal CUDA OOM in trailing output triggered abort
        self.assertTrue(supervisor_res.abort_triggered,
                        "Fatal marker in trailing output must trigger abort")
        self.assertIn("CUDA OOM / Error detected", supervisor_res.abort_reason)

        # Prove that verify_inference_success reports ABORTED
        rec_mock = MagicMock()
        rec_mock.recovery_successful = True
        rec_mock.reader_thread_stopped = True
        outcome, reason = verify_inference_success(supervisor_res, rec_mock)
        self.assertEqual(outcome, InferenceOutcome.ABORTED)
        self.assertIn("CUDA OOM / Error detected", reason)

    # =======================================================================
    # SECTION I: AMR PROCESS-LOCAL MEMORY ACCOUNTING & INTEGRITY (REMEDIATION)
    # =======================================================================

    def test_35_amr_failed_pre_reclamation_process_snapshot(self):
        """Proves failed pre-reclamation process snapshot sets deltas to None and reports RECLAMATION_UNVERIFIED."""
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.side_effect = [
            self._make_healthy_ram(avail_gib=11.0),
            self._make_healthy_ram(avail_gib=11.1)
        ]
        failed_proc = MagicMock(
            pid=os.getpid(), status=TelemetryStatus.FAILED,
            working_set_bytes=0, private_commit_bytes=None
        )
        post_proc = MagicMock(
            pid=os.getpid(), status=TelemetryStatus.SUCCESS,
            working_set_bytes=18 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024
        )
        mock_monitor.get_process_memory.side_effect = [failed_proc, post_proc]

        controller = AdaptiveMemoryController(
            policy=self.policy,
            monitor=mock_monitor,
            reports_dir=self.amr_reports_dir
        )
        rep = controller.evaluate_and_reclaim(force=True)
        self.assertEqual(rep.overall_status, ReclamationStatus.RECLAMATION_UNVERIFIED)
        self.assertIsNone(rep.nova_process_working_set_delta_bytes)
        self.assertIsNone(rep.nova_process_private_delta_bytes)
        self.assertFalse(rep.process_telemetry_verified)

    def test_36_amr_failed_post_reclamation_process_snapshot(self):
        """Proves failed post-reclamation process snapshot sets deltas to None and reports RECLAMATION_UNVERIFIED."""
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.side_effect = [
            self._make_healthy_ram(avail_gib=11.0),
            self._make_healthy_ram(avail_gib=11.1)
        ]
        pre_proc = MagicMock(
            pid=os.getpid(), status=TelemetryStatus.SUCCESS,
            working_set_bytes=20 * 1024 * 1024, private_commit_bytes=12 * 1024 * 1024
        )
        failed_proc = MagicMock(
            pid=os.getpid(), status=TelemetryStatus.FAILED,
            working_set_bytes=0, private_commit_bytes=None
        )
        mock_monitor.get_process_memory.side_effect = [pre_proc, failed_proc]

        controller = AdaptiveMemoryController(
            policy=self.policy,
            monitor=mock_monitor,
            reports_dir=self.amr_reports_dir
        )
        rep = controller.evaluate_and_reclaim(force=True)
        self.assertEqual(rep.overall_status, ReclamationStatus.RECLAMATION_UNVERIFIED)
        self.assertIsNone(rep.nova_process_working_set_delta_bytes)
        self.assertIsNone(rep.nova_process_private_delta_bytes)
        self.assertFalse(rep.process_telemetry_verified)

    def test_37_amr_before_after_pid_mismatch(self):
        """Proves before/after PID mismatch invalidates measurement and reports RECLAMATION_UNVERIFIED."""
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.side_effect = [
            self._make_healthy_ram(avail_gib=11.0),
            self._make_healthy_ram(avail_gib=11.1)
        ]
        curr_pid = os.getpid()
        pre_proc = MagicMock(
            pid=curr_pid, status=TelemetryStatus.SUCCESS,
            working_set_bytes=20 * 1024 * 1024, private_commit_bytes=12 * 1024 * 1024
        )
        mismatched_post = MagicMock(
            pid=curr_pid + 9999, status=TelemetryStatus.SUCCESS,
            working_set_bytes=18 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024
        )
        mock_monitor.get_process_memory.side_effect = [pre_proc, mismatched_post]

        controller = AdaptiveMemoryController(
            policy=self.policy,
            monitor=mock_monitor,
            reports_dir=self.amr_reports_dir
        )
        rep = controller.evaluate_and_reclaim(force=True)
        self.assertEqual(rep.overall_status, ReclamationStatus.RECLAMATION_UNVERIFIED)
        self.assertIsNone(rep.nova_process_working_set_delta_bytes)
        self.assertIsNone(rep.nova_process_private_delta_bytes)
        self.assertFalse(rep.process_telemetry_verified)

    def test_38_amr_zero_default_working_set_from_failed_snapshot(self):
        """
        Proves that a failed snapshot reporting working_set_bytes=0 cannot be used
        to calculate a false-positive freed memory delta.
        """
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.side_effect = [
            self._make_healthy_ram(avail_gib=11.0),
            self._make_healthy_ram(avail_gib=11.0)
        ]
        pre_proc = MagicMock(
            pid=os.getpid(), status=TelemetryStatus.SUCCESS,
            working_set_bytes=50 * 1024 * 1024, private_commit_bytes=30 * 1024 * 1024
        )
        # Malformed snapshot with working_set_bytes=0
        zero_post = MagicMock(
            pid=os.getpid(), status=TelemetryStatus.SUCCESS,
            working_set_bytes=0, private_commit_bytes=0
        )
        mock_monitor.get_process_memory.side_effect = [pre_proc, zero_post]

        controller = AdaptiveMemoryController(
            policy=self.policy,
            monitor=mock_monitor,
            reports_dir=self.amr_reports_dir
        )
        rep = controller.evaluate_and_reclaim(force=True)
        self.assertEqual(rep.overall_status, ReclamationStatus.RECLAMATION_UNVERIFIED)
        self.assertIsNone(rep.nova_process_working_set_delta_bytes)
        self.assertIsNone(rep.nova_process_private_delta_bytes)
        self.assertNotEqual(rep.overall_status, ReclamationStatus.RECLAMATION_COMPLETED)

    def test_39_amr_working_set_decrease_without_private_commit_decrease(self):
        """
        Proves that working-set reduction alone NEVER classifies RECLAMATION_COMPLETED.
        When working set drops (e.g. 20 MB) but private commit is unchanged, status is NO_MEASURABLE_IMPROVEMENT.
        """
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.side_effect = [
            self._make_healthy_ram(avail_gib=11.0),
            self._make_healthy_ram(avail_gib=11.0)
        ]
        curr_pid = os.getpid()
        pre_proc = MagicMock(
            pid=curr_pid, status=TelemetryStatus.SUCCESS,
            working_set_bytes=40 * 1024 * 1024, private_commit_bytes=20 * 1024 * 1024
        )
        # Working set drops by 20 MB, but private commit remains identical
        post_proc = MagicMock(
            pid=curr_pid, status=TelemetryStatus.SUCCESS,
            working_set_bytes=20 * 1024 * 1024, private_commit_bytes=20 * 1024 * 1024
        )
        mock_monitor.get_process_memory.side_effect = [pre_proc, post_proc]

        controller = AdaptiveMemoryController(
            policy=self.policy,
            monitor=mock_monitor,
            reports_dir=self.amr_reports_dir
        )
        rep = controller.evaluate_and_reclaim(force=True)
        self.assertEqual(rep.overall_status, ReclamationStatus.NO_MEASURABLE_IMPROVEMENT)
        self.assertEqual(rep.nova_process_working_set_delta_bytes, 20 * 1024 * 1024)
        self.assertEqual(rep.nova_process_private_delta_bytes, 0)
        self.assertNotEqual(rep.overall_status, ReclamationStatus.RECLAMATION_COMPLETED)

    def test_40_amr_private_commit_decrease_with_both_snapshots_successful(self):
        """
        Proves that when both snapshots succeed and private commit decreases by >= 1 MiB,
        RECLAMATION_COMPLETED is correctly reported with verified deltas.
        """
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.side_effect = [
            self._make_healthy_ram(avail_gib=11.0),
            self._make_healthy_ram(avail_gib=11.0)
        ]
        curr_pid = os.getpid()
        pre_proc = MagicMock(
            pid=curr_pid, status=TelemetryStatus.SUCCESS,
            working_set_bytes=30 * 1024 * 1024, private_commit_bytes=20 * 1024 * 1024
        )
        # Private commit decreases by 2 MiB
        post_proc = MagicMock(
            pid=curr_pid, status=TelemetryStatus.SUCCESS,
            working_set_bytes=28 * 1024 * 1024, private_commit_bytes=18 * 1024 * 1024
        )
        mock_monitor.get_process_memory.side_effect = [pre_proc, post_proc]

        controller = AdaptiveMemoryController(
            policy=self.policy,
            monitor=mock_monitor,
            reports_dir=self.amr_reports_dir
        )
        rep = controller.evaluate_and_reclaim(force=True)
        self.assertEqual(rep.overall_status, ReclamationStatus.RECLAMATION_COMPLETED)
        self.assertEqual(rep.nova_process_working_set_delta_bytes, 2 * 1024 * 1024)
        self.assertEqual(rep.nova_process_private_delta_bytes, 2 * 1024 * 1024)
        self.assertTrue(rep.process_telemetry_verified)

    def test_41_pressure_detector_missing_and_invalid_commit_pct(self):
        """
        Proves MemoryPressureDetector.evaluate_pressure() strictly fails closed (CRITICAL)
        on missing, non-numeric, boolean, NaN, inf, negative, or > 100 commit percentages.
        """
        detector = MemoryPressureDetector(self.policy)
        snap = self._make_healthy_ram(avail_gib=13.0)

        # Missing commit_pct (None)
        snap.commit_pct = None
        self.assertEqual(detector.evaluate_pressure(snap), MemoryPressureLevel.CRITICAL)

        # Boolean commit_pct
        snap.commit_pct = True
        self.assertEqual(detector.evaluate_pressure(snap), MemoryPressureLevel.CRITICAL)
        snap.commit_pct = False
        self.assertEqual(detector.evaluate_pressure(snap), MemoryPressureLevel.CRITICAL)

        # String commit_pct
        snap.commit_pct = "75.0"
        self.assertEqual(detector.evaluate_pressure(snap), MemoryPressureLevel.CRITICAL)

        # NaN / inf
        snap.commit_pct = float("nan")
        self.assertEqual(detector.evaluate_pressure(snap), MemoryPressureLevel.CRITICAL)
        snap.commit_pct = float("inf")
        self.assertEqual(detector.evaluate_pressure(snap), MemoryPressureLevel.CRITICAL)
        snap.commit_pct = float("-inf")
        self.assertEqual(detector.evaluate_pressure(snap), MemoryPressureLevel.CRITICAL)

        # Out of bounds
        snap.commit_pct = -0.1
        self.assertEqual(detector.evaluate_pressure(snap), MemoryPressureLevel.CRITICAL)
        snap.commit_pct = 100.1
        self.assertEqual(detector.evaluate_pressure(snap), MemoryPressureLevel.CRITICAL)

    # =======================================================================
    # SECTION J: DETERMINISTIC EOF, READER EXCEPTIONS & TIMEOUTS
    # =======================================================================

    def test_42_deterministic_delayed_output_arriving_after_direct_child_exit(self):
        """
        Deterministic regression test:
        Direct child exits, pipe remains open, reader has not yet delivered final line,
        final line arrives afterward, supervisor waits for reader, drains trailing output,
        detects fatal CUDA OOM, and triggers abort.
        """
        from scripts.run_inference_with_telemetry import start_output_reader

        class ControlledDelayedStream:
            def __init__(self):
                self.child_exited_event = threading.Event()
                self._lines = ["line 1: prompt eval time = 50.0 ms / 5 runs\n"]
                self._trailing_line = "CUDA error: out of memory in trailing child stream\n"
                self._sent_trailing = False

            def readline(self) -> str:
                if self._lines:
                    return self._lines.pop(0)
                # Wait until parent has observed that the child process has exited
                self.child_exited_event.wait(timeout=2.0)
                if not self._sent_trailing:
                    self._sent_trailing = True
                    return self._trailing_line
                return ""

        stream = ControlledDelayedStream()
        out_queue = queue.Queue()
        reader_stop = threading.Event()
        reader_thread = start_output_reader(stream, out_queue, reader_stop)

        mock_proc = MagicMock()
        # Direct child is running for 1 poll, then exits (code 0)
        poll_results = [None, 0, 0, 0]
        def mock_poll():
            res = poll_results.pop(0) if poll_results else 0
            if res == 0:
                # Direct child has exited! Signal the delayed stream that child is dead
                stream.child_exited_event.set()
            return res
        mock_proc.poll.side_effect = mock_poll

        s_queue = queue.Queue()
        clock = FakeClock(100.0)
        s_queue.put(self._make_sample(1, 100.0))

        supervisor_res = supervise_child_process(
            proc=mock_proc,
            sample_queue=s_queue,
            output_queue=out_queue,
            policy=self.policy,
            clock=clock,
            poll_sleep_sec=0.01,
            reader_thread=reader_thread,
            reader_timeout_sec=2.0
        )

        # 1. Proves direct child exited
        self.assertEqual(supervisor_res.exit_code, 0)
        # 2. Proves reader reached verified EOF
        self.assertTrue(supervisor_res.reader_eof_reached)
        self.assertTrue(supervisor_res.reader_thread_stopped)
        # 3. Proves trailing line arrived and was retained
        full_output = "".join(supervisor_res.output_lines)
        self.assertIn("CUDA error: out of memory in trailing child stream", full_output)
        # 4. Proves trailing CUDA error triggered abort
        self.assertTrue(supervisor_res.abort_triggered)
        self.assertIn("CUDA OOM / Error detected", supervisor_res.abort_reason)

    def test_43_reader_exception_versus_successful_eof(self):
        """
        Proves that an output reader encountering an exception (e.g. broken pipe / I/O error)
        is marked as read_failed=True, eof_reached=False, and verify_inference_success
        reports UNVERIFIED (never SUCCESS).
        """
        from scripts.run_inference_with_telemetry import start_output_reader

        class BrokenStream:
            def readline(self):
                raise IOError("Simulated pipe read failure before EOF")

        out_queue = queue.Queue()
        reader_stop = threading.Event()
        reader_thread = start_output_reader(BrokenStream(), out_queue, reader_stop)
        reader_thread.join(timeout=2.0)

        # Reader stopped, but failed due to exception
        self.assertFalse(reader_thread.is_alive())
        self.assertTrue(reader_thread.read_failed)
        self.assertFalse(reader_thread.eof_reached)

        # Verify supervisor records read_failed and verify_inference_success rejects as UNVERIFIED
        sup_res = SupervisorResult(
            abort_triggered=False,
            abort_reason="",
            exit_code=0,
            peak_ram_gib=11.0,
            peak_vram_mib=2000.0,
            processed_samples_count=5,
            duration_seconds=1.0,
            output_lines=["eval time = 50.00 ms / 1 runs\n", "print_timings: ok\n"],
            reader_thread_stopped=True,
            reader_eof_reached=False,
            reader_read_failed=True,
            reader_exception="Simulated pipe read failure before EOF"
        )
        rec_mock = MagicMock(recovery_successful=True, reader_thread_stopped=True)
        outcome, reason = verify_inference_success(sup_res, rec_mock)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)
        self.assertIn("Output reader encountered an exception", reason)

    def test_44_reader_timeout_results_in_unverified(self):
        """
        Proves that when an output reader hangs and times out during supervisor join,
        reader_timed_out is set and verify_inference_success reports UNVERIFIED.
        """
        sup_res = SupervisorResult(
            abort_triggered=False,
            abort_reason="",
            exit_code=0,
            peak_ram_gib=11.0,
            peak_vram_mib=2000.0,
            processed_samples_count=5,
            duration_seconds=1.0,
            output_lines=["eval time = 50.00 ms / 1 runs\n", "print_timings: ok\n"],
            reader_thread_stopped=False,
            reader_eof_reached=False,
            reader_read_failed=True,
            reader_timed_out=True
        )
        rec_mock = MagicMock(recovery_successful=True, reader_thread_stopped=False)
        outcome, reason = verify_inference_success(sup_res, rec_mock)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)
        self.assertIn("timed out", reason)

    def test_45_amr_no_measurable_improvement_and_unverified_persistence(self):
        """
        Proves that both NO_MEASURABLE_IMPROVEMENT and RECLAMATION_UNVERIFIED reports
        are correctly persisted to disk with valid JSON schema.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            mock_monitor = MagicMock()
            controller = AdaptiveMemoryController(
                policy=self.policy,
                monitor=mock_monitor,
                reports_dir=tmp_dir
            )

            # 1. NO_MEASURABLE_IMPROVEMENT persistence
            mock_monitor.get_system_memory.side_effect = [
                self._make_healthy_ram(avail_gib=11.0),
                self._make_healthy_ram(avail_gib=11.0)
            ]
            mock_proc_flat = MagicMock(pid=os.getpid(), status=TelemetryStatus.SUCCESS,
                                       working_set_bytes=20 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024)
            mock_monitor.get_process_memory.side_effect = [mock_proc_flat, mock_proc_flat]

            rep_none = controller.evaluate_and_reclaim(force=True)
            self.assertEqual(rep_none.overall_status, ReclamationStatus.NO_MEASURABLE_IMPROVEMENT)
            latest_path = os.path.join(tmp_dir, "reclaim_latest.json")
            self.assertTrue(os.path.exists(latest_path))
            with open(latest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["overall_status"], ReclamationStatus.NO_MEASURABLE_IMPROVEMENT.value)
            self.assertEqual(data["nova_process_private_delta_bytes"], 0)

            # 2. RECLAMATION_UNVERIFIED persistence
            controller.cooldown_mgr.reset()
            mock_monitor.get_system_memory.side_effect = [
                self._make_healthy_ram(avail_gib=11.0),
                self._make_healthy_ram(avail_gib=11.0)
            ]
            failed_proc = MagicMock(pid=os.getpid(), status=TelemetryStatus.FAILED,
                                    working_set_bytes=0, private_commit_bytes=None)
            mock_monitor.get_process_memory.side_effect = [failed_proc, mock_proc_flat]

            rep_unv = controller.evaluate_and_reclaim(force=True)
            self.assertEqual(rep_unv.overall_status, ReclamationStatus.RECLAMATION_UNVERIFIED)
            with open(latest_path, "r", encoding="utf-8") as f:
                data_unv = json.load(f)
            self.assertEqual(data_unv["overall_status"], ReclamationStatus.RECLAMATION_UNVERIFIED.value)
            self.assertIsNone(data_unv["nova_process_private_delta_bytes"])
            self.assertIsNone(data_unv["nova_process_working_set_delta_bytes"])
            self.assertFalse(data_unv["process_telemetry_verified"])

    @patch("os.getpid", return_value=54321)
    def test_46_amr_default_operation_uses_nova_runtime_pid(self, mock_getpid):
        """Proves that AMR operation directly queries NOVA's own PID via os.getpid() without external injection."""
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=13.0)
        mock_proc = MagicMock(pid=54321, status=TelemetryStatus.SUCCESS,
                              working_set_bytes=20 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024)
        mock_monitor.get_process_memory.return_value = mock_proc

        controller = AdaptiveMemoryController(policy=self.policy, monitor=mock_monitor, reports_dir=self.amr_reports_dir)
        rep = controller.evaluate_and_reclaim()

        mock_getpid.assert_called()
        mock_monitor.get_process_memory.assert_called_with(54321)
        self.assertTrue(rep.process_telemetry_verified)

    def test_47_amr_caller_cannot_redirect_to_unrelated_pid(self):
        """Proves that neither constructor nor evaluate_and_reclaim accepts PID redirection arguments."""
        mock_monitor = MagicMock()
        # 1. Constructor rejects pid_provider or target_pid
        with self.assertRaises(TypeError):
            AdaptiveMemoryController(policy=self.policy, monitor=mock_monitor, pid_provider=lambda: 99999)  # type: ignore
        with self.assertRaises(TypeError):
            AdaptiveMemoryController(policy=self.policy, monitor=mock_monitor, target_pid=99999)  # type: ignore

        # 2. evaluate_and_reclaim rejects target_pid
        controller = AdaptiveMemoryController(policy=self.policy, monitor=mock_monitor, reports_dir=self.amr_reports_dir)
        with self.assertRaises(TypeError):
            controller.evaluate_and_reclaim(target_pid=99999)  # type: ignore

    @patch("os.getpid", return_value=40001)
    def test_48_amr_pre_and_post_snapshot_pid_mismatch_produces_unverified(self, mock_getpid):
        """Proves that if pre- or post-reclamation snapshot PID does not match runtime PID, status is RECLAMATION_UNVERIFIED."""
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.side_effect = [
            self._make_healthy_ram(avail_gib=11.0),
            self._make_healthy_ram(avail_gib=11.0),
            self._make_healthy_ram(avail_gib=11.0),
            self._make_healthy_ram(avail_gib=11.0)
        ]
        # Case A: pre-snapshot PID mismatch (foreign PID 88888 vs runtime 40001)
        foreign_pre = MagicMock(pid=88888, status=TelemetryStatus.SUCCESS,
                                working_set_bytes=20 * 1024 * 1024, private_commit_bytes=12 * 1024 * 1024)
        valid_post = MagicMock(pid=40001, status=TelemetryStatus.SUCCESS,
                               working_set_bytes=18 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024)
        mock_monitor.get_process_memory.side_effect = [foreign_pre, valid_post]

        controller = AdaptiveMemoryController(policy=self.policy, monitor=mock_monitor, reports_dir=self.amr_reports_dir)
        rep_a = controller.evaluate_and_reclaim(force=True)
        self.assertEqual(rep_a.overall_status, ReclamationStatus.RECLAMATION_UNVERIFIED)
        self.assertIsNone(rep_a.nova_process_private_delta_bytes)
        self.assertFalse(rep_a.process_telemetry_verified)

        # Case B: post-snapshot PID mismatch (runtime 40001 vs foreign post 99999)
        valid_pre = MagicMock(pid=40001, status=TelemetryStatus.SUCCESS,
                              working_set_bytes=20 * 1024 * 1024, private_commit_bytes=12 * 1024 * 1024)
        foreign_post = MagicMock(pid=99999, status=TelemetryStatus.SUCCESS,
                                 working_set_bytes=18 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024)
        mock_monitor.get_process_memory.side_effect = [valid_pre, foreign_post]
        rep_b = controller.evaluate_and_reclaim(force=True)
        self.assertEqual(rep_b.overall_status, ReclamationStatus.RECLAMATION_UNVERIFIED)
        self.assertIsNone(rep_b.nova_process_private_delta_bytes)
        self.assertFalse(rep_b.process_telemetry_verified)

    @patch("gc.collect", side_effect=RuntimeError("Simulated GC crash"))
    def test_49_amr_real_reclaimer_gc_exception_produces_unverified(self, mock_gc_collect):
        """
        Exercises the real SafeMemoryReclaimer with a real gc.collect exception.
        Proves that failed action is recorded, actions_completed is empty,
        and controller reports RECLAMATION_UNVERIFIED even with a 5 MiB private commit drop.
        """
        # 1. Direct unit verification on real SafeMemoryReclaimer
        real_reclaimer = SafeMemoryReclaimer()
        attempted, completed, failed, duration = real_reclaimer.reclaim()
        self.assertIn("python_gc_collect", attempted)
        self.assertEqual(completed, [])
        self.assertEqual(len(failed), 1)
        self.assertIn("python_gc_collect: Simulated GC crash", failed[0])

        # 2. End-to-end integration with AdaptiveMemoryController
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.side_effect = [
            self._make_healthy_ram(avail_gib=11.0),
            self._make_healthy_ram(avail_gib=11.0)
        ]
        curr_pid = os.getpid()
        pre_proc = MagicMock(pid=curr_pid, status=TelemetryStatus.SUCCESS,
                             working_set_bytes=30 * 1024 * 1024, private_commit_bytes=25 * 1024 * 1024)
        # 5 MiB private commit drop
        post_proc = MagicMock(pid=curr_pid, status=TelemetryStatus.SUCCESS,
                              working_set_bytes=25 * 1024 * 1024, private_commit_bytes=20 * 1024 * 1024)
        mock_monitor.get_process_memory.side_effect = [pre_proc, post_proc]

        controller = AdaptiveMemoryController(
            policy=self.policy,
            monitor=mock_monitor,
            reclaimer=real_reclaimer,
            reports_dir=self.amr_reports_dir
        )
        rep = controller.evaluate_and_reclaim(force=True)
        self.assertEqual(rep.overall_status, ReclamationStatus.RECLAMATION_UNVERIFIED)
        self.assertNotEqual(rep.overall_status, ReclamationStatus.RECLAMATION_COMPLETED)
        self.assertEqual(rep.actions_completed, [])
        self.assertIn("python_gc_collect: Simulated GC crash", rep.actions_failed)

    def test_50_amr_normal_and_cooldown_telemetry_verified_flag_accuracy(self):
        """
        Proves that in normal and cooldown paths, process_telemetry_verified requires
        both valid working set AND valid private commit counters.
        """
        mock_monitor = MagicMock()
        curr_pid = os.getpid()

        # 1. Normal path with missing private_commit_bytes -> process_telemetry_verified is False
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=13.0)
        proc_missing_priv = MagicMock(pid=curr_pid, status=TelemetryStatus.SUCCESS,
                                      working_set_bytes=20 * 1024 * 1024, private_commit_bytes=None)
        mock_monitor.get_process_memory.return_value = proc_missing_priv

        controller = AdaptiveMemoryController(policy=self.policy, monitor=mock_monitor, reports_dir=self.amr_reports_dir)
        rep_norm_missing = controller.evaluate_and_reclaim()
        self.assertTrue(rep_norm_missing.process_working_set_verified)
        self.assertFalse(rep_norm_missing.process_private_commit_verified)
        self.assertFalse(rep_norm_missing.process_telemetry_verified)

        # 2. Normal path with complete valid counters -> process_telemetry_verified is True
        proc_complete = MagicMock(pid=curr_pid, status=TelemetryStatus.SUCCESS,
                                  working_set_bytes=20 * 1024 * 1024, private_commit_bytes=10 * 1024 * 1024)
        mock_monitor.get_process_memory.return_value = proc_complete
        rep_norm_ok = controller.evaluate_and_reclaim()
        self.assertTrue(rep_norm_ok.process_working_set_verified)
        self.assertTrue(rep_norm_ok.process_private_commit_verified)
        self.assertTrue(rep_norm_ok.process_telemetry_verified)

        # 3. Cooldown path with missing private commit -> process_telemetry_verified is False
        controller.cooldown_mgr.record_reclamation()
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=11.0)
        mock_monitor.get_process_memory.return_value = proc_missing_priv
        rep_cd_missing = controller.evaluate_and_reclaim()
        self.assertTrue(rep_cd_missing.cooldown_applied)
        self.assertFalse(rep_cd_missing.process_private_commit_verified)
        self.assertFalse(rep_cd_missing.process_telemetry_verified)

    def test_51_memory_reclaim_report_defaults_fail_closed(self):
        """
        Proves that MemoryReclaimReport defaults all process verification flags
        to False (fail-closed) when constructed without explicit telemetry verification.
        """
        rep = MemoryReclaimReport(
            timestamp_utc="2026-11-04T17:00:00Z",
            trigger_reason="Test default flags",
            pressure_level=MemoryPressureLevel.NORMAL,
            system_avail_bytes_before=10 * 1024 * 1024 * 1024,
            system_avail_bytes_after=10 * 1024 * 1024 * 1024,
            observed_system_avail_delta_bytes=0,
            observed_system_avail_delta_mib=0.0,
            ram_available_before_gib=10.0,
            ram_available_after_gib=10.0,
            ram_delta_gib=0.0,
            commit_before_pct=40.0,
            commit_after_pct=40.0
        )
        self.assertFalse(rep.process_working_set_verified)
        self.assertFalse(rep.process_private_commit_verified)
        self.assertFalse(rep.process_telemetry_verified)

if __name__ == "__main__":
    unittest.main()
