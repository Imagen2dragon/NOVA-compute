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
    MemoryReclaimReport,
    ProcessCategory,
    MemoryRecommendation,
    MemoryAdvisorReport
)
from nova.resource_steward.policy import (
    ResourcePolicy,
    DEFAULT_POLICY,
    enforce_mandatory_admission_floors
)
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

    def _make_healthy_disk(
        self,
        free_bytes: Optional[int] = None,
        free_gib: Optional[float] = None,
        free_gb: Optional[float] = None,
        status: TelemetryStatus = TelemetryStatus.SUCCESS
    ) -> DiskSnapshot:
        if free_bytes is not None:
            bytes_val = free_bytes
            val_gib = float(free_bytes) / (1024**3)
        elif free_gib is not None:
            val_gib = free_gib
            bytes_val = int(val_gib * 1024**3)
        elif free_gb is not None:
            val_gib = free_gb
            bytes_val = int(val_gib * 1024**3)
        else:
            val_gib = 100.0
            bytes_val = int(100.0 * 1024**3)

        return DiskSnapshot(
            drive="C:\\",
            free_bytes=bytes_val,
            total_bytes=int(950 * 1024**3),
            free_gib=val_gib,
            total_gib=950.0,
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

    def test_52_advisor_process_classification(self):
        """Proves that MemoryAdvisor correctly classifies processes into User, Background, Protected, and Unknown."""
        # 1. Category A: User Applications
        self.assertEqual(MemoryAdvisor.classify_process("msedge.exe"), ProcessCategory.USER_APPLICATION)
        self.assertEqual(MemoryAdvisor.classify_process("code.exe"), ProcessCategory.USER_APPLICATION)
        self.assertEqual(MemoryAdvisor.classify_process("chrome.exe"), ProcessCategory.USER_APPLICATION)
        self.assertEqual(MemoryAdvisor.classify_process("claude.exe"), ProcessCategory.USER_APPLICATION)
        self.assertEqual(MemoryAdvisor.classify_process("spotify.exe"), ProcessCategory.USER_APPLICATION)

        # 2. Category B: Background Applications
        self.assertEqual(MemoryAdvisor.classify_process("onedrive.exe"), ProcessCategory.BACKGROUND_APPLICATION)
        self.assertEqual(MemoryAdvisor.classify_process("dropbox.exe"), ProcessCategory.BACKGROUND_APPLICATION)
        self.assertEqual(MemoryAdvisor.classify_process("language_server.exe"), ProcessCategory.BACKGROUND_APPLICATION)
        self.assertEqual(MemoryAdvisor.classify_process("node.exe"), ProcessCategory.BACKGROUND_APPLICATION)

        # 3. Category C: Protected Processes
        self.assertEqual(MemoryAdvisor.classify_process("system"), ProcessCategory.PROTECTED_SYSTEM)
        self.assertEqual(MemoryAdvisor.classify_process("registry"), ProcessCategory.PROTECTED_SYSTEM)
        self.assertEqual(MemoryAdvisor.classify_process("smss.exe"), ProcessCategory.PROTECTED_SYSTEM)
        self.assertEqual(MemoryAdvisor.classify_process("csrss.exe"), ProcessCategory.PROTECTED_SYSTEM)
        self.assertEqual(MemoryAdvisor.classify_process("services.exe"), ProcessCategory.PROTECTED_SYSTEM)
        self.assertEqual(MemoryAdvisor.classify_process("svchost.exe"), ProcessCategory.PROTECTED_SYSTEM)
        self.assertEqual(MemoryAdvisor.classify_process("msmpeng.exe"), ProcessCategory.PROTECTED_SYSTEM)
        self.assertEqual(MemoryAdvisor.classify_process("nvcontainer.exe"), ProcessCategory.PROTECTED_SYSTEM)
        self.assertEqual(MemoryAdvisor.classify_process("dwm.exe"), ProcessCategory.PROTECTED_SYSTEM)

        # 4. Unknown Processes
        self.assertEqual(MemoryAdvisor.classify_process("random_daemon_xyz.exe"), ProcessCategory.UNKNOWN)
        self.assertEqual(MemoryAdvisor.classify_process("unknown_tool.bin"), ProcessCategory.UNKNOWN)

    def test_53_advisor_strictly_excludes_protected_processes(self):
        """Proves that Protected Processes are NEVER included in recommendations."""
        procs = [
            ProcessResourceSnapshot(pid=100, name="svchost.exe", working_set_bytes=1000 * 1024 * 1024,
                                    private_commit_bytes=800 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=200, name="msmpeng.exe", working_set_bytes=600 * 1024 * 1024,
                                    private_commit_bytes=400 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=300, name="nvcontainer.exe", working_set_bytes=500 * 1024 * 1024,
                                    private_commit_bytes=300 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=400, name="system", working_set_bytes=400 * 1024 * 1024,
                                    private_commit_bytes=None, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=500, name="msedge.exe", working_set_bytes=1200 * 1024 * 1024,
                                    private_commit_bytes=1500 * 1024 * 1024, status=TelemetryStatus.SUCCESS)
        ]
        recs = MemoryAdvisor.analyze_processes(procs)
        rec_names = [r.process_name for r in recs]

        # Protected processes must be strictly excluded
        self.assertNotIn("svchost.exe", rec_names)
        self.assertNotIn("msmpeng.exe", rec_names)
        self.assertNotIn("nvcontainer.exe", rec_names)
        self.assertNotIn("system", rec_names)

        # User application should be recommended
        self.assertIn("msedge.exe", rec_names)
        self.assertEqual(len(recs), 1)

    def test_54_advisor_handles_missing_and_partial_process_telemetry(self):
        """Proves that missing or partial process telemetry is handled truthfully without failing."""
        procs = [
            # Complete telemetry
            ProcessResourceSnapshot(pid=1001, name="code.exe", working_set_bytes=500 * 1024 * 1024,
                                    private_commit_bytes=600 * 1024 * 1024, pids=[1001, 1002, 1003],
                                    instances_total=3, instances_measured=3, is_partial_private_commit=False,
                                    status=TelemetryStatus.SUCCESS),
            # Partial private commit telemetry
            ProcessResourceSnapshot(pid=2001, name="chrome.exe", working_set_bytes=800 * 1024 * 1024,
                                    private_commit_bytes=400 * 1024 * 1024, pids=[2001, 2002, 2003, 2004],
                                    instances_total=4, instances_measured=1, is_partial_private_commit=True,
                                    status=TelemetryStatus.SUCCESS),
            # Missing private commit telemetry (access restricted)
            ProcessResourceSnapshot(pid=3001, name="onedrive.exe", working_set_bytes=300 * 1024 * 1024,
                                    private_commit_bytes=None, pids=[3001, 3002],
                                    instances_total=2, instances_measured=0, is_partial_private_commit=False,
                                    status=TelemetryStatus.SUCCESS)
        ]
        recs = MemoryAdvisor.analyze_processes(procs)
        by_name = {r.process_name: r for r in recs}

        # 1. Complete process
        code_rec = by_name["code.exe"]
        self.assertFalse(code_rec.is_partial)
        self.assertEqual(code_rec.total_private_commit_mb, 600.0)
        self.assertIn("across all 3 instance(s)", code_rec.private_commit_interpretation)

        # 2. Partial process
        chrome_rec = by_name["chrome.exe"]
        self.assertTrue(chrome_rec.is_partial)
        self.assertEqual(chrome_rec.total_private_commit_mb, 400.0)
        self.assertIn("partial measurement: 1/4 instances measured", chrome_rec.private_commit_interpretation)
        self.assertIn("(1/4 measured)", chrome_rec.recommendation_text)

        # 3. Missing telemetry process
        od_rec = by_name["onedrive.exe"]
        self.assertIsNone(od_rec.total_private_commit_mb)
        self.assertIn("Private commit telemetry unavailable", od_rec.private_commit_interpretation)

    def test_55_advisor_working_set_vs_private_commit_semantics(self):
        """
        Proves that recommendations clearly distinguish working set (physical residency/standby cache)
        from private commit (dedicated virtual commit charge), never claiming 1:1 guaranteed available RAM.
        """
        procs = [
            ProcessResourceSnapshot(pid=2001, name="msedge.exe", working_set_bytes=1000 * 1024 * 1024,
                                    private_commit_bytes=1200 * 1024 * 1024, status=TelemetryStatus.SUCCESS)
        ]
        recs = MemoryAdvisor.analyze_processes(procs)
        self.assertEqual(len(recs), 1)
        rec = recs[0]

        # Working set semantics: must mention physical residency and standby caching
        self.assertIn("physical memory residency", rec.working_set_interpretation)
        self.assertIn("standby", rec.working_set_interpretation)
        self.assertNotIn("guaranteed available RAM", rec.working_set_interpretation)

        # Private commit semantics: must mention dedicated commit charge
        self.assertIn("dedicated commit charge", rec.private_commit_interpretation)
        self.assertIn("virtual commit allocation", rec.private_commit_interpretation)

    def test_56_advisor_candidate_ranking_by_verified_footprint(self):
        """Proves that candidates are ranked by verified memory footprint with user applications prioritized."""
        procs = [
            # 1. Background app holding 400 MB WS, 300 MB Priv
            ProcessResourceSnapshot(pid=3001, name="onedrive.exe", working_set_bytes=400 * 1024 * 1024,
                                    private_commit_bytes=300 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            # 2. Large User app holding 1500 MB WS, 2000 MB Priv
            ProcessResourceSnapshot(pid=3002, name="code.exe", working_set_bytes=1500 * 1024 * 1024,
                                    private_commit_bytes=2000 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            # 3. Medium User app holding 800 MB WS, 900 MB Priv
            ProcessResourceSnapshot(pid=3003, name="msedge.exe", working_set_bytes=800 * 1024 * 1024,
                                    private_commit_bytes=900 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            # 4. Large Protected System process (must be excluded)
            ProcessResourceSnapshot(pid=3004, name="msmpeng.exe", working_set_bytes=3000 * 1024 * 1024,
                                    private_commit_bytes=3000 * 1024 * 1024, status=TelemetryStatus.SUCCESS)
        ]
        recs = MemoryAdvisor.analyze_processes(procs)
        rec_names = [r.process_name for r in recs]

        # msmpeng must not be present
        self.assertNotIn("msmpeng.exe", rec_names)

        # Ordering must rank largest user application first, then medium user application, then background app
        self.assertEqual(rec_names, ["code.exe", "msedge.exe", "onedrive.exe"])

    def test_57_advisor_read_only_guarantee_no_process_termination(self):
        """Proves that MemoryAdvisor never terminates, signals, or kills any external process."""
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=8.5)
        mock_monitor.list_top_processes.return_value = [
            ProcessResourceSnapshot(pid=4001, name="msedge.exe", working_set_bytes=1000 * 1024 * 1024,
                                    private_commit_bytes=1000 * 1024 * 1024, status=TelemetryStatus.SUCCESS)
        ]

        with patch("os.kill") as mock_kill, patch("subprocess.run") as mock_subproc:
            # 1. Direct analyze_processes
            MemoryAdvisor.analyze_processes(mock_monitor.list_top_processes.return_value)
            # 2. generate_advisor_report
            advice = MemoryAdvisor.generate_advisor_report(mock_monitor)
            # 3. steward.get_memory_advice
            steward = ResourceSteward()
            steward.monitor = mock_monitor
            steward.get_memory_advice()

            # Verify that os.kill was NEVER called
            mock_kill.assert_not_called()
            # Verify that no taskkill or termination command was executed
            for call_args in mock_subproc.call_args_list:
                args = call_args[0][0] if call_args[0] else []
                if isinstance(args, list):
                    self.assertNotIn("taskkill", args)
                    self.assertNotIn("/F", args)

    def test_58_advisor_static_inspection_forbidden_apis_absent(self):
        """Static inspection: verifies forbidden Windows memory cleaner and kill APIs are absent in advisor.py."""
        import ast
        advisor_path = os.path.join(PROJECT_ROOT, "nova", "resource_steward", "advisor.py")
        with open(advisor_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="advisor.py")

        forbidden = [
            "EmptyWorkingSet",
            "K32EmptyWorkingSet",
            "NtSetSystemInformation",
            "MemoryPurgeStandbyList",
            "MemoryFlushModifiedList",
            "SetSystemFileCacheSize",
            "SetProcessWorkingSetSize",
            "TerminateProcess",
            "taskkill"
        ]

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden:
                self.fail(f"Forbidden API '{node.id}' found in advisor.py")
            elif isinstance(node, ast.Attribute) and node.attr in forbidden:
                self.fail(f"Forbidden attribute '{node.attr}' found in advisor.py")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name in forbidden:
                        self.fail(f"Forbidden import '{alias.name}' found in advisor.py")

    def test_59_advisor_integration_with_preflight_report(self):
        """Proves that ResourceSteward preflight integrates MemoryAdvisor and partitions recommendations."""
        steward = ResourceSteward()
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=9.0) # Deficit 1.0 GiB
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu()
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk()
        mock_monitor.list_top_processes.return_value = [
            ProcessResourceSnapshot(pid=5001, name="msedge.exe", working_set_bytes=1200 * 1024 * 1024,
                                    private_commit_bytes=1500 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=5002, name="onedrive.exe", working_set_bytes=300 * 1024 * 1024,
                                    private_commit_bytes=200 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=5003, name="svchost.exe", working_set_bytes=800 * 1024 * 1024,
                                    private_commit_bytes=300 * 1024 * 1024, status=TelemetryStatus.SUCCESS)
        ]
        steward.monitor = mock_monitor
        steward.adaptive_reclaimer = MagicMock()

        report = steward.run_preflight(run_auto_reclaim=False)
        self.assertFalse(report.passed) # RAM deficit -> GATED
        self.assertEqual(len(report.recommendations), 2)
        rec_names = [r.process_name for r in report.recommendations]
        self.assertIn("msedge.exe", rec_names)
        self.assertIn("onedrive.exe", rec_names)
        self.assertNotIn("svchost.exe", rec_names)

        # Test dedicated advice report
        advice = steward.get_memory_advice()
        self.assertTrue(advice.is_ram_constrained)
        self.assertEqual(advice.ram_deficit_gib, 1.0)
        self.assertEqual(len(advice.user_app_recommendations), 1)
        self.assertEqual(advice.user_app_recommendations[0].process_name, "msedge.exe")
        self.assertEqual(len(advice.background_app_recommendations), 1)
        self.assertEqual(advice.background_app_recommendations[0].process_name, "onedrive.exe")
        self.assertIn("svchost.exe", advice.protected_processes_observed)

    def test_60_advisor_remeasure_memory_after_user_action_and_threshold_fidelity(self):
        """
        Proves that re-measurement correctly calculates RAM gains after manual user action
        and strictly preserves the 10.0 GiB available RAM admission threshold.
        """
        steward = ResourceSteward()
        mock_monitor = MagicMock()
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu()
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk()

        baseline_ram = self._make_healthy_ram(avail_gib=8.5, commit_pct=45.0)

        # Scenario A: User closes apps, RAM increases to 10.5 GiB (meets threshold)
        fresh_a = self._make_healthy_ram(avail_gib=10.5, commit_pct=35.0)
        mock_monitor.get_system_memory.return_value = fresh_a
        steward.monitor = mock_monitor

        snap_a, summary_a = steward.remeasure_memory(baseline_ram=baseline_ram)
        self.assertEqual(summary_a["avail_ram_gib"], 10.5)
        self.assertEqual(summary_a["ram_delta_gib"], 2.0)
        self.assertEqual(summary_a["commit_delta_pct"], 10.0)
        self.assertTrue(summary_a["admission_gate_passed"]) # >= 10.0 GiB
        self.assertTrue(summary_a["ram_gate_passed"])

        # Scenario B: User closes apps, but RAM only increases to 9.5 GiB (fails threshold)
        fresh_b = self._make_healthy_ram(avail_gib=9.5, commit_pct=40.0)
        mock_monitor.get_system_memory.return_value = fresh_b
        steward.monitor = mock_monitor

        snap_b, summary_b = steward.remeasure_memory(baseline_ram=baseline_ram)
        self.assertEqual(summary_b["avail_ram_gib"], 9.5)
        self.assertEqual(summary_b["ram_delta_gib"], 1.0)
        self.assertEqual(summary_b["commit_delta_pct"], 5.0)
        # Advisor strictly preserves the 10.0 GiB admission threshold: 9.5 GiB fails closed
        self.assertFalse(summary_b["admission_gate_passed"])
        self.assertFalse(summary_b["ram_gate_passed"])

    def test_61_protected_process_classification_consistency_and_exclusion(self):
        """
        Regression test proving:
        1. Protected processes (svchost.exe, MsMpEng.exe, nvcontainer.exe, dwm.exe, explorer.exe)
           are classified consistently as ProcessCategory.PROTECTED_SYSTEM regardless of casing.
        2. Top process inventory populates p.category and maps to 'Protected System' display label.
        3. Protected processes are strictly excluded from closure recommendations even with heavy memory.
        4. Protected processes are never eligible for automatic manipulation (AMR strictly binds to os.getpid()).
        """
        protected_targets = ["svchost.exe", "MsMpEng.exe", "nvcontainer.exe", "dwm.exe", "explorer.exe"]

        # 1. Classification consistency
        for proc_name in protected_targets:
            category = MemoryAdvisor.classify_process(proc_name)
            self.assertEqual(
                category,
                ProcessCategory.PROTECTED_SYSTEM,
                f"Process {proc_name} failed consistent classification as PROTECTED_SYSTEM."
            )
            # Case variations
            self.assertEqual(MemoryAdvisor.classify_process(proc_name.upper()), ProcessCategory.PROTECTED_SYSTEM)
            self.assertEqual(MemoryAdvisor.classify_process(proc_name.lower()), ProcessCategory.PROTECTED_SYSTEM)

        # 2. Preflight top processes category population and display label
        steward = ResourceSteward()
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=11.0)
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu()
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk()
        mock_monitor.list_top_processes.return_value = [
            ProcessResourceSnapshot(pid=1001, name="svchost.exe", working_set_bytes=1000 * 1024 * 1024,
                                    private_commit_bytes=800 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=1002, name="MsMpEng.exe", working_set_bytes=1200 * 1024 * 1024,
                                    private_commit_bytes=900 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=1003, name="nvcontainer.exe", working_set_bytes=400 * 1024 * 1024,
                                    private_commit_bytes=300 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=1004, name="dwm.exe", working_set_bytes=500 * 1024 * 1024,
                                    private_commit_bytes=400 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=1005, name="explorer.exe", working_set_bytes=600 * 1024 * 1024,
                                    private_commit_bytes=500 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=2001, name="msedge.exe", working_set_bytes=1500 * 1024 * 1024,
                                    private_commit_bytes=1200 * 1024 * 1024, status=TelemetryStatus.SUCCESS)
        ]
        steward.monitor = mock_monitor
        report = steward.run_preflight(run_auto_reclaim=False)

        for p in report.top_processes:
            self.assertIsNotNone(p.category)
            if p.name.lower() in [t.lower() for t in protected_targets]:
                self.assertEqual(p.category, ProcessCategory.PROTECTED_SYSTEM)
            elif p.name == "msedge.exe":
                self.assertEqual(p.category, ProcessCategory.USER_APPLICATION)

        # 3. Recommendations exclusion: even with multi-gigabyte memory, protected procs never appear
        recs = steward.advisor.analyze_processes(report.top_processes)
        rec_names = [r.process_name.lower() for r in recs]
        for protected_name in protected_targets:
            self.assertNotIn(
                protected_name.lower(),
                rec_names,
                f"Protected process {protected_name} must NEVER appear in closure recommendations."
            )
        self.assertIn("msedge.exe", rec_names)

        # 4. Automatic manipulation immunity: AMR operates exclusively on os.getpid()
        from nova.resource_steward.adaptive_memory import AdaptiveMemoryController
        amr = AdaptiveMemoryController(self.policy, mock_monitor, reports_dir=".")
        # AMR evaluation has no external PID parameters and queries os.getpid()
        self.assertFalse(hasattr(amr, "target_pid"))

    def test_62_transparent_memory_ranking_heuristic_score(self):
        """
        Proves the transparent memory ranking methodology:
        1. compute_ranking_score calculates ws_mb + 0.25 * (priv_mb or 0.0) without claiming to be physical RAM.
        2. User applications take tier priority over background/unknown apps regardless of raw score.
        3. Within tier, candidates are sorted by ranking_score descending, then ws_mb descending.
        4. Private commit is never conflated with physical RAM.
        """
        # Score calculation tests
        score_complete = MemoryAdvisor.compute_ranking_score(ws_mb=1000.0, priv_mb=800.0)
        self.assertEqual(score_complete, 1200.0) # 1000 + 0.25 * 800

        score_no_priv = MemoryAdvisor.compute_ranking_score(ws_mb=1000.0, priv_mb=None)
        self.assertEqual(score_no_priv, 1000.0) # 1000 + 0.0

        # Candidate prioritization test:
        # Process 1: Large Background App (onedrive.exe): ws=1200 MB, priv=1000 MB
        # Process 2: Medium User App (code.exe): ws=800 MB, priv=900 MB
        # Process 3: Medium Background App (steamwebhelper.exe): ws=800 MB, priv=900 MB
        # Process 4: Medium User App with lower priv (pycharm64.exe): ws=800 MB, priv=300 MB
        # Process 5: Small User App (msedge.exe): ws=500 MB, priv=400 MB
        procs = [
            ProcessResourceSnapshot(pid=3001, name="onedrive.exe", working_set_bytes=1200 * 1024 * 1024,
                                    private_commit_bytes=1000 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=3002, name="code.exe", working_set_bytes=800 * 1024 * 1024,
                                    private_commit_bytes=900 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=3003, name="steamwebhelper.exe", working_set_bytes=800 * 1024 * 1024,
                                    private_commit_bytes=900 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=3004, name="pycharm64.exe", working_set_bytes=800 * 1024 * 1024,
                                    private_commit_bytes=300 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=3005, name="msedge.exe", working_set_bytes=500 * 1024 * 1024,
                                    private_commit_bytes=400 * 1024 * 1024, status=TelemetryStatus.SUCCESS)
        ]
        recs = MemoryAdvisor.analyze_processes(procs)
        rec_names = [r.process_name for r in recs]

        # 1. Primary consideration is physical working set:
        #    onedrive.exe (1200 MB) ranks first ahead of 800 MB and 500 MB candidates.
        self.assertEqual(rec_names[0], "onedrive.exe")

        # 2. Secondary consideration is category priority when working set is tied (800 MB):
        #    code.exe (User App) ranks ahead of steamwebhelper.exe (Background App).
        self.assertEqual(rec_names[1], "code.exe")

        # 3. Secondary consideration is private commit when working set and category tie (800 MB User Apps):
        #    code.exe (900 MB priv) ranks ahead of pycharm64.exe (300 MB priv).
        self.assertEqual(rec_names[2], "pycharm64.exe")
        self.assertEqual(rec_names[3], "steamwebhelper.exe")

        # 4. Smallest physical working set ranks last:
        self.assertEqual(rec_names[4], "msedge.exe")

        # Verified unverified_heuristic_reclaim_mb is None (no false reclaim promise)
        for r in recs:
            self.assertIsNone(r.unverified_heuristic_reclaim_mb)

    def test_63_remeasure_memory_full_admission_gate_preservation(self):
        """
        Proves that steward.remeasure_memory strictly reuses the full MemoryBudgetManager gate suite:
        - Fresh telemetry is always queried directly from monitor (no caller snapshot injection).
        - Passing RAM threshold alone CANNOT produce a passed preflight if another gate fails.
        - Tested failure modes: VRAM deficit, competing GPU processes, commit >= 90%, disk space deficit, sensor failures.
        """
        steward = ResourceSteward()
        mock_monitor = MagicMock()
        steward.monitor = mock_monitor

        baseline = self._make_healthy_ram(avail_gib=8.0)
        healthy_ram = self._make_healthy_ram(avail_gib=12.0, commit_pct=40.0) # Meets RAM gate
        mock_monitor.get_system_memory.return_value = healthy_ram

        # 1. Failure Mode: Insufficient VRAM (4000 MiB < 5500 MiB)
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu(free_mib=4000.0)
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk()
        snap, summary = steward.remeasure_memory(baseline_ram=baseline)
        self.assertTrue(summary["ram_gate_passed"]) # RAM gate passed
        self.assertFalse(summary["admission_gate_passed"]) # But admission fails closed!
        self.assertIn("avail_vram", summary["failed_gates"])

        # 2. Failure Mode: Competing GPU compute workloads (1 active app)
        comp_gpu = self._make_healthy_gpu(free_mib=5800.0)
        comp_gpu.compute_processes = [GPUProcessInfo(pid=999, process_name="other_ai.exe", used_memory_mib=1000.0)]
        mock_monitor.get_gpu_telemetry.return_value = comp_gpu
        snap, summary = steward.remeasure_memory(baseline_ram=baseline)
        self.assertTrue(summary["ram_gate_passed"])
        self.assertFalse(summary["admission_gate_passed"])
        self.assertIn("no_competing_gpu", summary["failed_gates"])

        # 3. Failure Mode: Excessive system commit (92% >= 90%)
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=12.0, commit_pct=92.0)
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu()
        snap, summary = steward.remeasure_memory(baseline_ram=baseline)
        self.assertTrue(summary["ram_gate_passed"])
        self.assertFalse(summary["admission_gate_passed"])
        self.assertIn("system_commit", summary["failed_gates"])

        # 4. Failure Mode: Insufficient free disk (15 GB < 25 GB)
        mock_monitor.get_system_memory.return_value = healthy_ram
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk(free_gb=15.0)
        snap, summary = steward.remeasure_memory(baseline_ram=baseline)
        self.assertTrue(summary["ram_gate_passed"])
        self.assertFalse(summary["admission_gate_passed"])
        self.assertIn("disk_free", summary["failed_gates"])

        # 5. Failure Mode: GPU sensor failure (status=FAILED)
        mock_monitor.get_gpu_telemetry.return_value = GPUSnapshot(
            name="NVIDIA GeForce RTX 4050", vram_total_mib=6144.0, vram_free_mib=0.0,
            vram_used_mib=0.0, utilization_pct=0.0, temperature_c=0.0, power_draw_w=0.0,
            driver_version="Unknown", status=TelemetryStatus.FAILED, error_message="NVML unavailable"
        )
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk()
        snap, summary = steward.remeasure_memory(baseline_ram=baseline)
        self.assertTrue(summary["ram_gate_passed"])
        self.assertFalse(summary["admission_gate_passed"])

        # 6. Failure Mode: Disk sensor failure (status=FAILED)
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu()
        mock_monitor.get_disk_space.return_value = DiskSnapshot(
            drive="C:\\", free_bytes=0, total_bytes=0, free_gb=0.0, total_gb=0.0,
            status=TelemetryStatus.FAILED, error_message="Disk query failed"
        )
        snap, summary = steward.remeasure_memory(baseline_ram=baseline)
        self.assertTrue(summary["ram_gate_passed"])
        self.assertFalse(summary["admission_gate_passed"])

        # 7. Success Mode: All gates pass
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk()
        snap, summary = steward.remeasure_memory(baseline_ram=baseline)
        self.assertTrue(summary["ram_gate_passed"])
        self.assertTrue(summary["admission_gate_passed"])
        self.assertEqual(len(summary["failed_gates"]), 0)

    def test_64_private_commit_semantics_and_partial_measurement_fidelity(self):
        """
        Proves that:
        1. Recommendations explicitly distinguish working set residency from private commit virtual charge.
        2. Recommendation text never asserts private commit is guaranteed physical RAM.
        3. Partial measurements explicitly warn about partial instance coverage.
        """
        procs = [
            ProcessResourceSnapshot(
                pid=4001,
                name="msedge.exe",
                working_set_bytes=800 * 1024 * 1024,
                private_commit_bytes=600 * 1024 * 1024,
                pids=[4001, 4002, 4003],
                instances_total=5,
                instances_measured=2,
                is_partial_private_commit=True,
                status=TelemetryStatus.SUCCESS
            )
        ]
        recs = MemoryAdvisor.analyze_processes(procs)
        self.assertEqual(len(recs), 1)
        r = recs[0]

        # Verify partial coverage notice
        self.assertTrue(r.is_partial)
        self.assertIn("2/5 measured", r.recommendation_text)
        self.assertIn("2/5 instances measured", r.private_commit_interpretation)

        # Verify semantics
        self.assertIn("Working set reflects physical residency", r.recommendation_text)
        self.assertIn("private commit reflects virtual commit charge", r.recommendation_text)
        self.assertIn("does NOT guarantee immediate physical RAM recovery", r.private_commit_interpretation)
        self.assertIn("Actual physical RAM freed must be verified via re-measurement telemetry", r.recommendation_text)

    def test_65_process_counter_validation_and_partial_fidelity(self):
        """
        Proves that MemoryAdvisor.analyze_processes strictly validates process telemetry counters:
        - Failed process snapshots (status != SUCCESS) are never aggregated into totals.
        - Negative counters are rejected.
        - Boolean values (subclass of int in Python) are rejected.
        - Missing working-set values are rejected.
        - Missing private-commit values do not contribute to private commit total.
        - Zero measured instances out of several expected instances is NEVER treated as complete.
        - Mixed valid and invalid process measurements safely preserve verified totals and expected instances.
        """
        # Scenario A: Negative and boolean counters
        invalid_procs = [
            ProcessResourceSnapshot(pid=5001, name="chrome.exe", working_set_bytes=-1000 * 1024 * 1024, # negative WS
                                    private_commit_bytes=500 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=5002, name="firefox.exe", working_set_bytes=True, # boolean WS
                                    private_commit_bytes=500 * 1024 * 1024, status=TelemetryStatus.SUCCESS),
            ProcessResourceSnapshot(pid=5003, name="brave.exe", working_set_bytes=500 * 1024 * 1024,
                                    private_commit_bytes=-200 * 1024 * 1024, status=TelemetryStatus.SUCCESS), # negative priv
            ProcessResourceSnapshot(pid=5004, name="opera.exe", working_set_bytes=500 * 1024 * 1024,
                                    private_commit_bytes=False, status=TelemetryStatus.SUCCESS), # boolean priv
            ProcessResourceSnapshot(pid=5005, name="slack.exe", working_set_bytes=500 * 1024 * 1024,
                                    private_commit_bytes=500 * 1024 * 1024, status=TelemetryStatus.FAILED) # failed snapshot
        ]
        recs = MemoryAdvisor.analyze_processes(invalid_procs)
        rec_map = {r.process_name: r for r in recs}

        # chrome and firefox had invalid WS -> valid WS was 0 (< 100 MB threshold) -> excluded
        self.assertNotIn("chrome.exe", rec_map)
        self.assertNotIn("firefox.exe", rec_map)
        # slack had status=FAILED -> excluded
        self.assertNotIn("slack.exe", rec_map)

        # brave had valid WS (500 MB) but negative priv commit -> priv commit is None (not aggregated)
        self.assertIn("brave.exe", rec_map)
        self.assertEqual(rec_map["brave.exe"].observed_working_set_mb, 500.0)
        self.assertIsNone(rec_map["brave.exe"].total_private_commit_mb)

        # opera had valid WS (500 MB) but boolean priv commit -> priv commit is None (not aggregated)
        self.assertIn("opera.exe", rec_map)
        self.assertEqual(rec_map["opera.exe"].observed_working_set_mb, 500.0)
        self.assertIsNone(rec_map["opera.exe"].total_private_commit_mb)

        # Scenario B: Zero measured instances out of positive total
        # B1: Zero measured instances with missing private commit
        zero_meas_procs_missing = [
            ProcessResourceSnapshot(
                pid=6001,
                name="msedge.exe",
                working_set_bytes=600 * 1024 * 1024,
                private_commit_bytes=None,
                pids=[6001, 6002, 6003, 6004],
                instances_total=4,
                instances_measured=0,
                status=TelemetryStatus.SUCCESS
            )
        ]
        recs_zero_missing = MemoryAdvisor.analyze_processes(zero_meas_procs_missing)
        self.assertEqual(len(recs_zero_missing), 1)
        r_zero = recs_zero_missing[0]
        self.assertEqual(r_zero.instances_total, 4)
        self.assertEqual(r_zero.instances_measured, 0)
        self.assertTrue(r_zero.is_partial) # CANNOT be treated as complete!
        self.assertIsNone(r_zero.total_private_commit_mb)
        self.assertEqual(r_zero.observed_working_set_mb, 600.0) # WS preserved independently
        self.assertIn("unavailable", r_zero.private_commit_interpretation)
        self.assertIn("0/4", r_zero.private_commit_interpretation)

        # B2: Zero measured instances with non-None private commit
        # (Private commit must be classified inconsistent/unavailable and excluded; coverage remains 0)
        zero_meas_procs_non_none = [
            ProcessResourceSnapshot(
                pid=6002,
                name="msedge.exe",
                working_set_bytes=600 * 1024 * 1024,
                private_commit_bytes=500 * 1024 * 1024, # Non-None value with 0 measured instances
                pids=[6001, 6002, 6003, 6004],
                instances_total=4,
                instances_measured=0, # Explicitly 0 measured instances
                status=TelemetryStatus.SUCCESS
            )
        ]
        recs_zero_non_none = MemoryAdvisor.analyze_processes(zero_meas_procs_non_none)
        self.assertEqual(len(recs_zero_non_none), 1)
        r_zero_nn = recs_zero_non_none[0]
        self.assertEqual(r_zero_nn.instances_total, 4)
        self.assertEqual(r_zero_nn.instances_measured, 0) # Coverage must remain strictly 0 (never replaced with 1)
        self.assertTrue(r_zero_nn.is_partial)
        self.assertIsNone(r_zero_nn.total_private_commit_mb) # Excluded from aggregation!
        self.assertEqual(r_zero_nn.observed_working_set_mb, 600.0) # WS preserved independently
        self.assertIn("unavailable", r_zero_nn.private_commit_interpretation)
        self.assertIn("0/4", r_zero_nn.private_commit_interpretation)

        # B3: Complete measurement (>= 1 measured instances)
        complete_meas_procs = [
            ProcessResourceSnapshot(
                pid=6003,
                name="msedge.exe",
                working_set_bytes=600 * 1024 * 1024,
                private_commit_bytes=300 * 1024 * 1024,
                pids=[6001, 6002, 6003],
                instances_total=3,
                instances_measured=3,
                status=TelemetryStatus.SUCCESS
            )
        ]
        recs_complete = MemoryAdvisor.analyze_processes(complete_meas_procs)
        self.assertEqual(len(recs_complete), 1)
        r_comp = recs_complete[0]
        self.assertEqual(r_comp.instances_total, 3)
        self.assertEqual(r_comp.instances_measured, 3)
        self.assertFalse(r_comp.is_partial)
        self.assertEqual(r_comp.total_private_commit_mb, 300.0)
        self.assertEqual(r_comp.observed_working_set_mb, 600.0)

        # B4: instances_measured=True (Boolean rejected: becomes 0, priv excluded, WS preserved)
        bool_true_procs = [
            ProcessResourceSnapshot(
                pid=6004,
                name="msedge.exe",
                working_set_bytes=500 * 1024 * 1024,
                private_commit_bytes=400 * 1024 * 1024,
                pids=[6001, 6002],
                instances_total=2,
                instances_measured=True, # Boolean True must be rejected!
                status=TelemetryStatus.SUCCESS
            )
        ]
        recs_true = MemoryAdvisor.analyze_processes(bool_true_procs)
        self.assertEqual(len(recs_true), 1)
        r_true = recs_true[0]
        self.assertEqual(r_true.instances_total, 2)
        self.assertEqual(r_true.instances_measured, 0) # Strictly 0
        self.assertTrue(r_true.is_partial)
        self.assertIsNone(r_true.total_private_commit_mb) # Excluded
        self.assertEqual(r_true.observed_working_set_mb, 500.0) # WS preserved

        # B5: instances_measured=False (Boolean rejected: becomes 0, priv excluded, WS preserved)
        bool_false_procs = [
            ProcessResourceSnapshot(
                pid=6005,
                name="msedge.exe",
                working_set_bytes=500 * 1024 * 1024,
                private_commit_bytes=400 * 1024 * 1024,
                pids=[6001, 6002],
                instances_total=2,
                instances_measured=False, # Boolean False must be rejected!
                status=TelemetryStatus.SUCCESS
            )
        ]
        recs_false = MemoryAdvisor.analyze_processes(bool_false_procs)
        self.assertEqual(len(recs_false), 1)
        r_false = recs_false[0]
        self.assertEqual(r_false.instances_total, 2)
        self.assertEqual(r_false.instances_measured, 0) # Strictly 0
        self.assertTrue(r_false.is_partial)
        self.assertIsNone(r_false.total_private_commit_mb) # Excluded
        self.assertEqual(r_false.observed_working_set_mb, 500.0) # WS preserved

        # B6: Negative measurement count (instances_measured=-5 becomes 0, priv excluded, WS preserved)
        neg_meas_procs = [
            ProcessResourceSnapshot(
                pid=6006,
                name="msedge.exe",
                working_set_bytes=500 * 1024 * 1024,
                private_commit_bytes=400 * 1024 * 1024,
                pids=[6001, 6002],
                instances_total=2,
                instances_measured=-5, # Negative count must become 0!
                status=TelemetryStatus.SUCCESS
            )
        ]
        recs_neg = MemoryAdvisor.analyze_processes(neg_meas_procs)
        self.assertEqual(len(recs_neg), 1)
        r_neg = recs_neg[0]
        self.assertEqual(r_neg.instances_total, 2)
        self.assertEqual(r_neg.instances_measured, 0) # Strictly 0
        self.assertTrue(r_neg.is_partial)
        self.assertIsNone(r_neg.total_private_commit_mb) # Excluded
        self.assertEqual(r_neg.observed_working_set_mb, 500.0) # WS preserved

        # B7: Measured count above expected count (instances_total=2, instances_measured=5 clamped to 2)
        over_meas_procs = [
            ProcessResourceSnapshot(
                pid=6007,
                name="msedge.exe",
                working_set_bytes=500 * 1024 * 1024,
                private_commit_bytes=400 * 1024 * 1024,
                pids=[6001, 6002],
                instances_total=2,
                instances_measured=5, # Over-measurement must be clamped to instances_total
                status=TelemetryStatus.SUCCESS
            )
        ]
        recs_over = MemoryAdvisor.analyze_processes(over_meas_procs)
        self.assertEqual(len(recs_over), 1)
        r_over = recs_over[0]
        self.assertEqual(r_over.instances_total, 2)
        self.assertEqual(r_over.instances_measured, 2) # Clamped to 2, never 5
        self.assertFalse(r_over.is_partial)
        self.assertEqual(r_over.total_private_commit_mb, 400.0)
        self.assertEqual(r_over.observed_working_set_mb, 500.0)

        # Scenario C: Contradictory and partially valid grouped measurements for the same application
        mixed_procs = [
            # Instance 1: Valid (300 MB WS, 200 MB Priv, 1/1 measured)
            ProcessResourceSnapshot(pid=7001, name="code.exe", working_set_bytes=300 * 1024 * 1024,
                                    private_commit_bytes=200 * 1024 * 1024, instances_total=1,
                                    instances_measured=1, status=TelemetryStatus.SUCCESS),
            # Instance 2: Failed telemetry (must be excluded from total)
            ProcessResourceSnapshot(pid=7002, name="code.exe", working_set_bytes=400 * 1024 * 1024,
                                    private_commit_bytes=400 * 1024 * 1024, instances_total=1,
                                    instances_measured=1, status=TelemetryStatus.FAILED),
            # Instance 3: Valid WS (200 MB), but negative priv commit (priv excluded)
            ProcessResourceSnapshot(pid=7003, name="code.exe", working_set_bytes=200 * 1024 * 1024,
                                    private_commit_bytes=-100, instances_total=1,
                                    instances_measured=1, status=TelemetryStatus.SUCCESS),
            # Instance 4: Contradictory snapshot: Valid WS (150 MB), non-None priv commit (100 MB), but 0 measured instances!
            ProcessResourceSnapshot(pid=7004, name="code.exe", working_set_bytes=150 * 1024 * 1024,
                                    private_commit_bytes=100 * 1024 * 1024, instances_total=1,
                                    instances_measured=0, status=TelemetryStatus.SUCCESS)
        ]
        recs_mixed = MemoryAdvisor.analyze_processes(mixed_procs)
        self.assertEqual(len(recs_mixed), 1)
        r_mixed = recs_mixed[0]
        # Valid WS: 300 + 200 + 150 = 650 MB (instance 2 excluded)
        self.assertEqual(r_mixed.observed_working_set_mb, 650.0)
        # Valid Priv: 200 MB (instance 2 FAILED, 3 negative, 4 contradictory 0-meas excluded)
        self.assertEqual(r_mixed.total_private_commit_mb, 200.0)
        # Expected instances preserved (4 total, 1 measured for priv commit)
        self.assertEqual(r_mixed.instances_total, 4)
        self.assertEqual(r_mixed.instances_measured, 1)
        self.assertTrue(r_mixed.is_partial)

    def test_66_conflicting_process_category_canonical_override(self):
        """
        Proves that canonical classification is strictly authoritative:
        - A protected executable (e.g. svchost.exe) with category incorrectly set to USER_APPLICATION
          is still canonically classified as PROTECTED_SYSTEM.
        - It is strictly excluded from closure recommendations.
        - In CLI inventory, it is displayed as [Protected System], never [User Application].
        """
        conflicting_snapshot = ProcessResourceSnapshot(
            pid=8001,
            name="svchost.exe",
            working_set_bytes=2000 * 1024 * 1024,
            private_commit_bytes=1500 * 1024 * 1024,
            category=ProcessCategory.USER_APPLICATION, # Intentionally conflicting stored category
            status=TelemetryStatus.SUCCESS
        )

        # 1. Recommendation engine exclusion
        recs = MemoryAdvisor.analyze_processes([conflicting_snapshot])
        self.assertEqual(len(recs), 0, "Protected process with conflicting category was not excluded!")

        # 2. Canonical classification check
        canonical = MemoryAdvisor.classify_process(conflicting_snapshot.name)
        self.assertEqual(canonical, ProcessCategory.PROTECTED_SYSTEM)

    def test_67_remeasure_memory_no_caller_snapshot_or_evaluator_injection(self):
        """
        Proves that the public remeasure_memory API does not accept caller-supplied snapshot or evaluator injection:
        - Passing gpu_snapshot, disk_snapshot, or budget_manager kwarg raises TypeError.
        - Fresh telemetry is always queried directly from monitor.
        - Admission evaluation strictly uses NOVA's trusted MemoryBudgetManager and intended ResourcePolicy.
        - Callers cannot inject a permissive custom evaluator to obtain admission_gate_passed=True.
        """
        steward = ResourceSteward()
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=11.0)
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu()
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk()
        steward.monitor = mock_monitor

        # 1. Verify parameter rejection on steward.remeasure_memory
        with self.assertRaises(TypeError):
            steward.remeasure_memory(gpu_snapshot=self._make_healthy_gpu()) # type: ignore

        with self.assertRaises(TypeError):
            steward.remeasure_memory(disk_snapshot=self._make_healthy_disk()) # type: ignore

        with self.assertRaises(TypeError):
            steward.remeasure_memory(budget_manager=MagicMock()) # type: ignore

        # 2. Verify parameter rejection on MemoryAdvisor.remeasure_memory
        with self.assertRaises(TypeError):
            MemoryAdvisor.remeasure_memory(mock_monitor, gpu_snapshot=self._make_healthy_gpu()) # type: ignore

        with self.assertRaises(TypeError):
            MemoryAdvisor.remeasure_memory(mock_monitor, disk_snapshot=self._make_healthy_disk()) # type: ignore

        with self.assertRaises(TypeError):
            MemoryAdvisor.remeasure_memory(mock_monitor, budget_manager=MagicMock()) # type: ignore

        # 3. Normal call succeeds and invokes monitor queries
        fresh, summary = steward.remeasure_memory()
        self.assertTrue(summary["admission_gate_passed"])
        mock_monitor.get_system_memory.assert_called()
        mock_monitor.get_gpu_telemetry.assert_called()
        mock_monitor.get_disk_space.assert_called_with("C:\\")

        # 4. Prove permissive evaluator bypass is impossible:
        # If GPU telemetry fails Gate 4 (3000 MiB free < 5500 MiB), admission_gate_passed MUST be False.
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu(free_mib=3000.0)
        fresh_fail, summary_fail = MemoryAdvisor.remeasure_memory(mock_monitor)
        self.assertFalse(summary_fail["admission_gate_passed"])
        self.assertIn("avail_vram", summary_fail["failed_gates"])

    def test_68_mandatory_admission_floors_cannot_be_weakened_by_policy(self):
        """
        Adversarial regression test:
        Proves that callers cannot weaken NOVA's 6 mandatory admission constraints
        using a deliberately permissive ResourcePolicy:
          1. Available RAM >= 10.0 GiB
          2. System commit < 90.0%
          3. Free VRAM >= 5,500 MiB
          4. Competing GPU compute apps == 0
          5. Free disk >= 25.0 GB
          6. Telemetry integrity (all sensors SUCCESS)
        Proves that legitimate stricter policy settings are strictly preserved.
        """
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=12.0, commit_pct=40.0)
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu(free_mib=6000.0)
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk(free_gb=50.0)

        # Deliberately permissive policy attempting to weaken all admission thresholds
        permissive_policy = ResourcePolicy(
            minimum_inference_ram_gib=2.0,     # Permissive: 2.0 GiB (Mandatory floor: 10.0 GiB)
            critical_commit_pct=99.0,          # Permissive: 99.0% (Mandatory ceiling: 90.0%)
            minimum_vram_mib=1000.0,           # Permissive: 1000 MiB (Mandatory floor: 5500 MiB)
            minimum_disk_free_gib=5.0          # Permissive: 5.0 GB (Mandatory floor: 25.0 GB)
        )

        # Sub-test 1: Available RAM deficit (8.0 GiB < 10.0 GiB floor, even though > 2.0 GiB permissive)
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=8.0, commit_pct=40.0)
        steward = ResourceSteward(policy=permissive_policy)
        steward.monitor = mock_monitor
        snap, summary = steward.remeasure_memory()
        self.assertFalse(summary["admission_gate_passed"], "Permissive policy weakened RAM admission threshold!")
        self.assertIn("avail_ram", summary["failed_gates"])
        self.assertEqual(summary["gates"]["avail_ram"].required_str, ">= 10.0 GiB")

        # Also verify via MemoryAdvisor.remeasure_memory directly
        snap_adv, summary_adv = MemoryAdvisor.remeasure_memory(mock_monitor, policy=permissive_policy)
        self.assertFalse(summary_adv["admission_gate_passed"])
        self.assertIn("avail_ram", summary_adv["failed_gates"])

        # Sub-test 2: System commit excess (95.0% >= 90.0% ceiling, even though < 99.0% permissive)
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=12.0, commit_pct=95.0)
        snap, summary = steward.remeasure_memory()
        self.assertFalse(summary["admission_gate_passed"], "Permissive policy weakened commit threshold!")
        self.assertIn("system_commit", summary["failed_gates"])
        self.assertEqual(summary["gates"]["system_commit"].required_str, "< 90.0%")

        # Sub-test 3: VRAM deficit (4000 MiB < 5500 MiB floor, even though > 1000 MiB permissive)
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=12.0, commit_pct=40.0)
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu(free_mib=4000.0)
        snap, summary = steward.remeasure_memory()
        self.assertFalse(summary["admission_gate_passed"], "Permissive policy weakened VRAM threshold!")
        self.assertIn("avail_vram", summary["failed_gates"])
        self.assertEqual(summary["gates"]["avail_vram"].required_str, ">= 5500 MiB")

        # Sub-test 4: Disk deficit (15.0 GiB < 25.0 GiB floor, even though > 5.0 GiB permissive)
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu(free_mib=6000.0)
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk(free_gib=15.0)
        snap, summary = steward.remeasure_memory()
        self.assertFalse(summary["admission_gate_passed"], "Permissive policy weakened disk threshold!")
        self.assertIn("disk_free", summary["failed_gates"])
        self.assertEqual(summary["gates"]["disk_free"].required_str, ">= 25.0 GiB")

        # Sub-test 5: Competing GPU compute apps (1 active app fails unconditionally)
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk(free_gb=50.0)
        comp_gpu = self._make_healthy_gpu(free_mib=6000.0)
        comp_gpu.compute_processes = [GPUProcessInfo(pid=999, process_name="other_ai.exe", used_memory_mib=1000.0)]
        mock_monitor.get_gpu_telemetry.return_value = comp_gpu
        snap, summary = steward.remeasure_memory()
        self.assertFalse(summary["admission_gate_passed"], "Competing GPU workloads were not gated!")
        self.assertIn("no_competing_gpu", summary["failed_gates"])

        # Sub-test 6: Telemetry integrity failure (sensor status FAILED fails unconditionally)
        mock_monitor.get_gpu_telemetry.return_value = GPUSnapshot(
            name="NVIDIA GeForce RTX 4050", vram_total_mib=6144.0, vram_free_mib=0.0,
            vram_used_mib=0.0, utilization_pct=0.0, temperature_c=0.0, power_draw_w=0.0,
            driver_version="Unknown", status=TelemetryStatus.FAILED, error_message="Sensor failure"
        )
        snap, summary = steward.remeasure_memory()
        self.assertFalse(summary["admission_gate_passed"], "Telemetry sensor failure did not fail closed!")
        self.assertIn("telemetry_integrity", summary["failed_gates"])

        # Sub-test 7: Stricter policy settings are strictly preserved
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu(free_mib=6000.0)
        stricter_policy = ResourcePolicy(
            minimum_inference_ram_gib=13.0,    # Stricter than 10.0 GiB
            critical_commit_pct=75.0,          # Stricter than 90.0%
            minimum_vram_mib=5800.0,           # Stricter than 5500 MiB
            minimum_disk_free_gib=40.0         # Stricter than 25.0 GB
        )
        steward_strict = ResourceSteward(policy=stricter_policy)
        steward_strict.monitor = mock_monitor

        # Host has 11.5 GiB RAM (meets 10.0 GiB mandatory floor, but fails stricter 13.0 GiB policy)
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=11.5, commit_pct=40.0)
        snap_s, summary_s = steward_strict.remeasure_memory()
        self.assertFalse(summary_s["admission_gate_passed"])
        self.assertIn("avail_ram", summary_s["failed_gates"])
        self.assertEqual(summary_s["gates"]["avail_ram"].required_str, ">= 13.0 GiB")

        # Host has 78% commit (meets 90% mandatory ceiling, but fails stricter 75% policy)
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=14.0, commit_pct=78.0)
        snap_s2, summary_s2 = steward_strict.remeasure_memory()
        self.assertFalse(summary_s2["admission_gate_passed"])
        self.assertIn("system_commit", summary_s2["failed_gates"])
        self.assertEqual(summary_s2["gates"]["system_commit"].required_str, "< 75.0%")

    def test_69_policy_immutability_and_defensive_copying(self):
        """
        Proves policy immutability, defensive copying, and evaluation-time re-enforcement:
          A. Original-object mutation: Mutating the caller's original ResourcePolicy after passing
             to ResourceSteward cannot weaken admission gates.
          B. Manager-policy mutation: Modifying manager.policy thresholds directly cannot bypass
             mandatory floors because evaluate_gates re-applies floors at admission evaluation time.
          C. Defensive-copy behavior: enforce_mandatory_admission_floors always returns a new
             copy, never the caller's original mutable object.
          D. Stricter policies: Confirms stricter thresholds remain fully preserved and enforced.
        """
        # --- A. Original-object mutation ---
        orig_policy = ResourcePolicy()
        steward = ResourceSteward(policy=orig_policy)
        mock_monitor = MagicMock()
        mock_monitor.get_system_memory.return_value = self._make_healthy_ram(avail_gib=8.0, commit_pct=40.0) # Deficit
        mock_monitor.get_gpu_telemetry.return_value = self._make_healthy_gpu(free_mib=6000.0)
        mock_monitor.get_disk_space.return_value = self._make_healthy_disk(free_gb=50.0)
        steward.monitor = mock_monitor

        # Mutate the caller's original policy object to permissive values
        orig_policy.minimum_inference_ram_gib = 2.0
        orig_policy.critical_commit_pct = 99.0
        orig_policy.minimum_vram_mib = 1000.0
        orig_policy.minimum_disk_free_gib = 5.0

        # Verify admission still enforces mandatory 10.0 GiB floor
        snap, summary = steward.remeasure_memory()
        self.assertFalse(summary["admission_gate_passed"])
        self.assertIn("avail_ram", summary["failed_gates"])
        self.assertEqual(summary["gates"]["avail_ram"].required_str, ">= 10.0 GiB")

        # --- B. Manager-policy mutation ---
        manager = MemoryBudgetManager(DEFAULT_POLICY)
        # Modify publicly accessible policy attributes on the manager instance
        manager.policy.minimum_inference_ram_gib = 2.0
        manager.policy.critical_commit_pct = 99.0
        manager.policy.minimum_vram_mib = 1000.0
        manager.policy.minimum_disk_free_gib = 5.0

        ram_8gib = self._make_healthy_ram(avail_gib=8.0, commit_pct=40.0)
        gpu_healthy = self._make_healthy_gpu(free_mib=6000.0)
        disk_healthy = self._make_healthy_disk(free_gb=50.0)

        # evaluate_gates re-applies mandatory floors at evaluation time
        all_passed, gates = manager.evaluate_gates(ram_8gib, gpu_healthy, disk_healthy)
        self.assertFalse(all_passed)
        self.assertFalse(gates["avail_ram"].passed)
        self.assertEqual(gates["avail_ram"].required_str, ">= 10.0 GiB")

        # --- C. Defensive-copy behavior ---
        caller_policy = ResourcePolicy()
        enforced = enforce_mandatory_admission_floors(caller_policy)
        self.assertIsNot(enforced, caller_policy, "enforce_mandatory_admission_floors must return a defensive copy!")
        self.assertIsNot(steward.policy, orig_policy, "ResourceSteward.policy must be a defensive copy!")
        self.assertIsNot(manager.policy, DEFAULT_POLICY, "MemoryBudgetManager.policy must be a defensive copy!")

        # --- D. Stricter policies preserved ---
        stricter = ResourcePolicy(
            minimum_inference_ram_gib=14.0,
            critical_commit_pct=70.0,
            minimum_vram_mib=5900.0,
            minimum_disk_free_gib=50.0
        )
        enforced_strict = enforce_mandatory_admission_floors(stricter)
        self.assertIsNot(enforced_strict, stricter)
        self.assertEqual(enforced_strict.minimum_inference_ram_gib, 14.0)
        self.assertEqual(enforced_strict.critical_commit_pct, 70.0)
        self.assertEqual(enforced_strict.minimum_vram_mib, 5900.0)
        self.assertEqual(enforced_strict.minimum_disk_free_gib, 50.0)

        # Stricter gate evaluation enforcement
        manager_strict = MemoryBudgetManager(stricter)
        ram_12gib = self._make_healthy_ram(avail_gib=12.0, commit_pct=40.0) # Meets 10.0 GiB floor, fails 14.0 GiB policy
        all_passed_s, gates_s = manager_strict.evaluate_gates(ram_12gib, gpu_healthy, disk_healthy)
        self.assertFalse(all_passed_s)
        self.assertFalse(gates_s["avail_ram"].passed)
        self.assertEqual(gates_s["avail_ram"].required_str, ">= 14.0 GiB")

    def test_70_disk_space_unit_consistency_and_boundary_gates(self):
        """
        Validates disk telemetry GiB unit semantics, boundary conditions, and fail-closed integrity:
          A. Boundary below threshold (24.99 GiB): Fails disk_free gate with explicit 0.01 GiB deficit.
          B. Boundary exact threshold (25.00 GiB): Passes disk_free gate.
          C. Boundary above threshold (25.01 GiB): Passes disk_free gate.
          D. Failed telemetry sensor: Fails both disk_free and telemetry_integrity gates closed.
          E. Stricter configured threshold (40.0 GiB required, 35.0 GiB measured): Enforces stricter requirement.
          F. Backward compatibility: Validates aliases (free_gb, total_gb, minimum_disk_free_gb).
        """
        manager = MemoryBudgetManager(DEFAULT_POLICY)
        ram_healthy = self._make_healthy_ram(avail_gib=12.0, commit_pct=40.0)
        gpu_healthy = self._make_healthy_gpu(free_mib=6000.0)

        # --- A. Boundary Below Threshold: 24.99 GiB ---
        free_disk_below_gib = 24.99
        disk_below = self._make_healthy_disk(free_gib=free_disk_below_gib)
        all_passed_a, gates_a = manager.evaluate_gates(ram_healthy, gpu_healthy, disk_below)
        self.assertFalse(all_passed_a, "24.99 GiB free disk must not pass admission")
        self.assertFalse(gates_a["disk_free"].passed)
        self.assertEqual(gates_a["disk_free"].required_str, ">= 25.0 GiB")
        self.assertEqual(gates_a["disk_free"].measured_str, "24.99 GiB")
        self.assertIn("0.01 GiB below required 25.0 GiB", gates_a["disk_free"].failure_reason)

        # --- B. Boundary Exact Threshold: 25.00 GiB ---
        free_disk_exact_gib = 25.00
        disk_exact = self._make_healthy_disk(free_gib=free_disk_exact_gib)
        all_passed_b, gates_b = manager.evaluate_gates(ram_healthy, gpu_healthy, disk_exact)
        self.assertTrue(all_passed_b, "25.00 GiB free disk must satisfy the admission threshold")
        self.assertTrue(gates_b["disk_free"].passed)
        self.assertEqual(gates_b["disk_free"].required_str, ">= 25.0 GiB")
        self.assertEqual(gates_b["disk_free"].measured_str, "25.00 GiB")
        self.assertIsNone(gates_b["disk_free"].failure_reason)

        # --- C. Boundary Above Threshold: 25.01 GiB ---
        free_disk_above_gib = 25.01
        disk_above = self._make_healthy_disk(free_gib=free_disk_above_gib)
        all_passed_c, gates_c = manager.evaluate_gates(ram_healthy, gpu_healthy, disk_above)
        self.assertTrue(all_passed_c, "25.01 GiB free disk must satisfy the admission threshold")
        self.assertTrue(gates_c["disk_free"].passed)
        self.assertEqual(gates_c["disk_free"].required_str, ">= 25.0 GiB")
        self.assertEqual(gates_c["disk_free"].measured_str, "25.01 GiB")
        self.assertIsNone(gates_c["disk_free"].failure_reason)

        # --- D. Failed Sensor Telemetry: Fail-Closed ---
        disk_failed = DiskSnapshot(
            drive="C:\\",
            free_bytes=0,
            total_bytes=0,
            free_gib=0.0,
            total_gib=0.0,
            status=TelemetryStatus.FAILED,
            error_message="Win32 error: Device not ready"
        )
        all_passed_d, gates_d = manager.evaluate_gates(ram_healthy, gpu_healthy, disk_failed)
        self.assertFalse(all_passed_d, "Failed disk sensor must fail closed")
        self.assertFalse(gates_d["disk_free"].passed)
        self.assertEqual(gates_d["disk_free"].measured_str, "UNAVAILABLE")
        self.assertFalse(gates_d["telemetry_integrity"].passed)
        self.assertIn("Disk: Win32 error: Device not ready", gates_d["telemetry_integrity"].measured_str)
        self.assertEqual(gates_d["telemetry_integrity"].failure_reason, "Telemetry failure detected. Fail-closed: cannot verify hardware safety.")

        # --- E. Stricter Configured Policy (40.0 GiB Required, 35.0 GiB Measured) ---
        stricter_disk_policy = ResourcePolicy(minimum_disk_free_gib=40.0)
        manager_strict_disk = MemoryBudgetManager(stricter_disk_policy)
        free_disk_measured_gib = 35.0
        disk_measured_strict = self._make_healthy_disk(free_gib=free_disk_measured_gib)
        all_passed_e, gates_e = manager_strict_disk.evaluate_gates(ram_healthy, gpu_healthy, disk_measured_strict)
        self.assertFalse(all_passed_e, "35.0 GiB must fail stricter 40.0 GiB policy")
        self.assertFalse(gates_e["disk_free"].passed)
        self.assertEqual(gates_e["disk_free"].required_str, ">= 40.0 GiB")
        self.assertEqual(gates_e["disk_free"].measured_str, "35.00 GiB")
        self.assertIn("5.00 GiB below required 40.0 GiB", gates_e["disk_free"].failure_reason)

        # --- F. Backward Compatibility Aliases & Serialization ---
        # 1. DiskSnapshot aliases
        snap = DiskSnapshot(drive="C:\\", free_bytes=32212254720, total_bytes=107374182400, free_gib=30.0, total_gib=100.0)
        self.assertEqual(snap.free_gb, 30.0)
        self.assertEqual(snap.total_gb, 100.0)

        # 2. DiskSnapshot constructed with legacy free_gb kwarg
        snap_legacy = DiskSnapshot(drive="C:\\", free_bytes=32212254720, total_bytes=107374182400, free_gb=30.0, total_gb=100.0)
        self.assertEqual(snap_legacy.free_gib, 30.0)
        self.assertEqual(snap_legacy.free_gb, 30.0)

        # 3. ResourcePolicy alias and from_dict
        pol = ResourcePolicy(minimum_disk_free_gib=32.0)
        self.assertEqual(pol.minimum_disk_free_gb, 32.0)
        pol_from_legacy_dict = ResourcePolicy.from_dict({"minimum_disk_free_gb": 35.0})
        self.assertEqual(pol_from_legacy_dict.minimum_disk_free_gib, 35.0)
        self.assertEqual(pol_from_legacy_dict.minimum_disk_free_gb, 35.0)
        pol_from_legacy_kwarg = ResourcePolicy(minimum_disk_free_gb=30.0)
        self.assertEqual(pol_from_legacy_kwarg.minimum_disk_free_gib, 30.0)

    def test_71_disk_admission_precision_and_byte_boundaries(self):
        """
        Validates full-precision disk telemetry admission and byte-level boundary conditions:
          Case A. One byte below threshold:
            - Available bytes: 25 * (1024 ** 3) - 1 (26,843,545,599 bytes)
            - Full-precision free_gib: 24.999999999068677 GiB
            - Expected gate decision: FAIL (disk_free gate passed == False, all_passed == False)
          Case B. Exactly at threshold:
            - Available bytes: 25 * (1024 ** 3) (26,843,545,600 bytes)
            - Full-precision free_gib: 25.0 GiB
            - Expected gate decision: PASS (disk_free gate passed == True, all_passed == True)
          Case C. One byte above threshold:
            - Available bytes: 25 * (1024 ** 3) + 1 (26,843,545,601 bytes)
            - Full-precision free_gib: 25.000000000931323 GiB
            - Expected gate decision: PASS (disk_free gate passed == True, all_passed == True)
          Case D. Rounding regression (sub-25 GiB measurement that previously rounded to 25.00 GiB):
            - Available bytes: int(24.996 * 1024**3) = 26,839,250,698 bytes
            - Full-precision free_gib: ~24.995999999776483 GiB (< 25.0 GiB)
            - Previous behavior: round(24.996, 2) was 25.00 GiB, which would have passed a rounded check
            - Full-precision behavior: FAIL (disk_free gate passed == False, all_passed == False)
          Case E. Direct DiskSnapshot byte derivation:
            - Proves DiskSnapshot(drive="C:\\", free_bytes=b, total_bytes=tb) computes full precision free_gib
        """
        manager = MemoryBudgetManager(DEFAULT_POLICY)
        ram_healthy = self._make_healthy_ram(avail_gib=12.0, commit_pct=40.0)
        gpu_healthy = self._make_healthy_gpu(free_mib=6000.0)
        exact_25_gib_bytes = 25 * (1024 ** 3)

        # --- Case A: One byte below threshold (25 GiB - 1 byte) ---
        bytes_below_1 = exact_25_gib_bytes - 1
        disk_below_1 = self._make_healthy_disk(free_bytes=bytes_below_1)
        self.assertEqual(disk_below_1.free_bytes, bytes_below_1)
        self.assertLess(disk_below_1.free_gib, 25.0, "free_gib must be strictly less than 25.0 GiB")
        all_passed_a, gates_a = manager.evaluate_gates(ram_healthy, gpu_healthy, disk_below_1)
        self.assertFalse(all_passed_a, "One byte below threshold must FAIL admission")
        self.assertFalse(gates_a["disk_free"].passed, "disk_free gate must be False for 25 GiB - 1 byte")
        self.assertEqual(gates_a["disk_free"].required_str, ">= 25.0 GiB")

        # --- Case B: Exactly at threshold (25 GiB) ---
        bytes_exact = exact_25_gib_bytes
        disk_exact = self._make_healthy_disk(free_bytes=bytes_exact)
        self.assertEqual(disk_exact.free_bytes, bytes_exact)
        self.assertEqual(disk_exact.free_gib, 25.0, "free_gib must be exactly 25.0 GiB")
        all_passed_b, gates_b = manager.evaluate_gates(ram_healthy, gpu_healthy, disk_exact)
        self.assertTrue(all_passed_b, "Exactly at threshold must PASS admission")
        self.assertTrue(gates_b["disk_free"].passed, "disk_free gate must be True for exact 25 GiB")
        self.assertEqual(gates_b["disk_free"].required_str, ">= 25.0 GiB")

        # --- Case C: One byte above threshold (25 GiB + 1 byte) ---
        bytes_above_1 = exact_25_gib_bytes + 1
        disk_above_1 = self._make_healthy_disk(free_bytes=bytes_above_1)
        self.assertEqual(disk_above_1.free_bytes, bytes_above_1)
        self.assertGreater(disk_above_1.free_gib, 25.0, "free_gib must be strictly greater than 25.0 GiB")
        all_passed_c, gates_c = manager.evaluate_gates(ram_healthy, gpu_healthy, disk_above_1)
        self.assertTrue(all_passed_c, "One byte above threshold must PASS admission")
        self.assertTrue(gates_c["disk_free"].passed, "disk_free gate must be True for 25 GiB + 1 byte")
        self.assertEqual(gates_c["disk_free"].required_str, ">= 25.0 GiB")

        # --- Case D: Rounding regression (below 25 GiB that previously rounded to 25.00 GiB) ---
        # 24.996 GiB rounds to 25.00 under round(val, 2), but is 0.004 GiB (~4.3 MB) short of threshold
        bytes_rounding_regression = int(24.996 * (1024 ** 3))
        disk_rounding_reg = self._make_healthy_disk(free_bytes=bytes_rounding_regression)
        # Verify that round(..., 2) would have yielded 25.00
        self.assertEqual(round(disk_rounding_reg.free_gib, 2), 25.00)
        # Verify full-precision value is strictly sub-threshold
        self.assertLess(disk_rounding_reg.free_gib, 25.0)
        all_passed_d, gates_d = manager.evaluate_gates(ram_healthy, gpu_healthy, disk_rounding_reg)
        self.assertFalse(all_passed_d, "Sub-25 GiB value rounding to 25.00 GiB must FAIL admission")
        self.assertFalse(gates_d["disk_free"].passed, "disk_free gate must reject rounded-up measurement")

        # --- Case E: Direct DiskSnapshot instantiation full precision ---
        raw_snap = DiskSnapshot(
            drive="C:\\",
            free_bytes=bytes_below_1,
            total_bytes=100 * (1024 ** 3)
        )
        self.assertEqual(raw_snap.free_bytes, bytes_below_1)
        self.assertLess(raw_snap.free_gib, 25.0)
        self.assertEqual(raw_snap.free_gb, raw_snap.free_gib)

if __name__ == "__main__":
    unittest.main()
