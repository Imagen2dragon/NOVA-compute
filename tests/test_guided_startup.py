"""
NOVA Compute — Guided Low-Memory Startup & Progressive Loading Test Suite
Phase 3B.1: Validates lightweight low-memory bootstrapping, state machine transitions,
exact RAM deficit calculation, strict distinction between RAM and disk messages,
multi-gate qualification, authorization boundaries, and prevention of unauthorized inference.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from nova.resource_steward.models import (
    StartupState,
    TelemetryStatus,
    SystemMemorySnapshot,
    GPUSnapshot,
    GPUProcessInfo,
    DiskSnapshot,
    ProcessResourceSnapshot,
    ProcessCategory,
    GateResult,
    PreflightReport,
    ControllerMemorySample,
    ControllerMemoryProfile
)
from nova.resource_steward.policy import ResourcePolicy, DEFAULT_POLICY
from nova.resource_steward.state_machine import (
    StartupStateMachine,
    GuidedStartupController,
    InvalidStateTransitionError,
    UnauthorizedInferenceError,
    StateTransitionRecord
)
from nova.resource_steward import ResourceSteward


def make_test_snapshots(
    avail_ram_gib: float = 8.23,
    total_ram_gib: float = 15.75,
    committed_gib: float = 6.30,
    commit_total_gib: float = 18.0,
    vram_free_mib: float = 5900.0,
    vram_total_mib: float = 6141.0,
    disk_free_gib: float = 100.0,
    disk_total_gib: float = 512.0,
    gpu_procs: int = 0,
    telemetry_status: TelemetryStatus = TelemetryStatus.SUCCESS
):
    """Creates a consistent set of telemetry snapshots for testing."""
    avail_bytes = int(avail_ram_gib * (1024 ** 3))
    total_bytes = int(total_ram_gib * (1024 ** 3))
    commit_total_bytes = int(commit_total_gib * (1024 ** 3))
    committed_bytes = int(committed_gib * (1024 ** 3))
    commit_avail_bytes = commit_total_bytes - committed_bytes
    commit_avail_gib = round((commit_avail_bytes) / (1024 ** 3), 3)
    commit_pct = round((committed_bytes / commit_total_bytes) * 100.0, 1)
    memory_load_pct = int(((total_bytes - avail_bytes) / total_bytes) * 100)

    ram = SystemMemorySnapshot(
        total_phys_bytes=total_bytes,
        avail_phys_bytes=avail_bytes,
        total_phys_gib=round(total_ram_gib, 3),
        avail_phys_gib=round(avail_ram_gib, 3),
        commit_total_bytes=commit_total_bytes,
        commit_avail_bytes=commit_avail_bytes,
        committed_bytes=committed_bytes,
        commit_total_gib=round(commit_total_gib, 3),
        commit_avail_gib=commit_avail_gib,
        committed_gib=round(committed_gib, 3),
        commit_pct=commit_pct,
        memory_load_pct=memory_load_pct,
        status=telemetry_status
    )

    gpu = GPUSnapshot(
        name="NVIDIA GeForce RTX 4050 Laptop GPU",
        vram_total_mib=vram_total_mib,
        vram_free_mib=vram_free_mib,
        vram_used_mib=vram_total_mib - vram_free_mib,
        utilization_pct=0.0,
        temperature_c=50.0,
        power_draw_w=35.0,
        driver_version="555.99",
        compute_processes=[] if gpu_procs == 0 else [
            GPUProcessInfo(pid=9999, process_name="competing_app.exe", used_memory_mib=500.0)
        ],
        status=telemetry_status
    )

    disk_free_bytes = int(disk_free_gib * (1024 ** 3))
    disk_total_bytes = int(disk_total_gib * (1024 ** 3))
    disk = DiskSnapshot(
        drive="C:\\",
        free_bytes=disk_free_bytes,
        total_bytes=disk_total_bytes,
        free_gib=round(disk_free_gib, 2),
        total_gib=round(disk_total_gib, 2),
        status=telemetry_status
    )

    procs = [
        ProcessResourceSnapshot(
            pid=1001,
            name="msedge.exe",
            working_set_bytes=800 * 1024 * 1024,
            working_set_mb=800.0,
            private_commit_bytes=900 * 1024 * 1024,
            private_commit_mb=900.0,
            category=ProcessCategory.USER_APPLICATION,
            pids=[1001, 1002, 1003],
            instances_total=3,
            instances_measured=3
        ),
        ProcessResourceSnapshot(
            pid=2001,
            name="code.exe",
            working_set_bytes=600 * 1024 * 1024,
            working_set_mb=600.0,
            private_commit_bytes=700 * 1024 * 1024,
            private_commit_mb=700.0,
            category=ProcessCategory.USER_APPLICATION,
            pids=[2001],
            instances_total=1,
            instances_measured=1
        )
    ]

    return ram, gpu, disk, procs


class MockResourceSteward:
    """Mock ResourceSteward for deterministic telemetry control."""

    def __init__(self, ram, gpu, disk, procs, policy: ResourcePolicy = None):
        self.policy = policy or DEFAULT_POLICY
        self.ram = ram
        self.gpu = gpu
        self.disk = disk
        self.procs = procs
        self.preflight_call_count = 0

    def run_preflight(self, run_auto_reclaim: bool = False) -> PreflightReport:
        self.preflight_call_count += 1
        from nova.resource_steward.memory_budget import MemoryBudgetManager
        from nova.resource_steward.advisor import MemoryAdvisor

        budget_mgr = MemoryBudgetManager(self.policy)
        advisor = MemoryAdvisor()

        all_passed, gates = budget_mgr.evaluate_gates(self.ram, self.gpu, self.disk)
        deficit = round(self.policy.minimum_inference_ram_gib - self.ram.avail_phys_gib, 3) if not gates["avail_ram"].passed else 0.0
        recommendations = advisor.analyze_processes(self.procs, ram_deficit_gib=max(0.0, deficit))

        return PreflightReport(
            passed=all_passed,
            gates=gates,
            system_ram=self.ram,
            gpu=self.gpu,
            disk=self.disk,
            top_processes=self.procs,
            recommendations=recommendations
        )


class TestGuidedStartup(unittest.TestCase):
    """Test suite for Guided Low-Memory Startup and State Machine."""

    def test_01_guided_startup_under_low_ram_does_not_start_llama_cli(self):
        """Test 1: Guided startup works when RAM < 10 GiB without starting llama-cli."""
        ram, gpu, disk, procs = make_test_snapshots(avail_ram_gib=8.23)
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        output_lines = []
        controller = GuidedStartupController(
            steward=mock_steward,
            input_fn=lambda _: "q",
            output_fn=output_lines.append
        )

        with patch("subprocess.Popen") as mock_popen, patch("subprocess.run") as mock_run:
            ret = controller.run_interactive()

            # Controller exited safely
            self.assertEqual(ret, 0)
            # Llama-cli was never invoked
            mock_popen.assert_not_called()
            mock_run.assert_not_called()

        # Final state was ABORTED due to clean user exit 'q'
        self.assertEqual(controller.current_state, StartupState.ABORTED)
        # Check that banner was printed
        banner_present = any("LOW-MEMORY SAFE MODE" in line for line in output_lines)
        self.assertTrue(banner_present)

    def test_02_model_remains_not_loaded_in_low_memory_mode(self):
        """Test 2: Model state remains NOT_LOADED in low-memory mode."""
        ram, gpu, disk, procs = make_test_snapshots(avail_ram_gib=7.50)
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        controller = GuidedStartupController(
            steward=mock_steward,
            input_fn=lambda _: "q",
            output_fn=lambda _: None
        )

        state = controller.start()
        self.assertEqual(state, StartupState.LOW_MEMORY_WAIT)
        self.assertFalse(controller.model_loaded)
        self.assertTrue(controller.inference_blocked)

    def test_03_exact_ram_deficit_calculation_and_language_precision(self):
        """Test 3: Exact RAM deficit calculation and strict RAM vs disk messaging."""
        ram, gpu, disk, procs = make_test_snapshots(avail_ram_gib=8.23)
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        output_lines = []
        controller = GuidedStartupController(
            steward=mock_steward,
            input_fn=lambda _: "q",
            output_fn=output_lines.append
        )

        controller.start()
        output_text = "\n".join(output_lines)

        # Exact RAM values and deficit
        self.assertIn("Current available RAM: 8.23 GiB", output_text)
        self.assertIn("Required RAM: 10.00 GiB", output_text)
        self.assertIn("Additional available RAM required: 1.77 GiB", output_text)

        # Precise RAM wording
        self.assertIn("Available physical RAM is below the required threshold. Close unused applications to make more memory available.", output_text)

        # Disk was 100 GiB (passed), so storage failure warning must NOT be present
        self.assertNotIn("Storage admission gate failed", output_text)
        self.assertNotIn("free storage space", output_text)

        # Explains working set vs private commit semantics
        self.assertIn("Working set represents resident physical memory at measurement time.", output_text)
        self.assertIn("Private commit represents virtual committed memory.", output_text)
        self.assertIn("Do not present private commit as guaranteed recoverable physical RAM.", output_text)

    def test_04_manual_recovery_and_recheck_transitions(self):
        """Test 4: Manual recovery and recheck transitions."""
        ram, gpu, disk, procs = make_test_snapshots(avail_ram_gib=8.0)
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        controller = GuidedStartupController(
            steward=mock_steward,
            output_fn=lambda _: None
        )

        controller.start()
        self.assertEqual(controller.current_state, StartupState.LOW_MEMORY_WAIT)

        # Trigger recheck
        controller.recheck()
        self.assertEqual(controller.current_state, StartupState.LOW_MEMORY_WAIT)

        history = [r.to_state for r in controller.state_machine.history]
        expected_sequence = [
            StartupState.SAFE_STARTUP,
            StartupState.PREFLIGHT_RUNNING,
            StartupState.PREFLIGHT_FAILED,
            StartupState.LOW_MEMORY_WAIT,
            StartupState.RESOURCE_RECOVERY,
            StartupState.PREFLIGHT_RUNNING,
            StartupState.PREFLIGHT_FAILED,
            StartupState.LOW_MEMORY_WAIT
        ]
        self.assertEqual(history, expected_sequence)

    def test_05_ram_increases_but_remains_below_threshold(self):
        """Test 5: RAM increases but remains below threshold (e.g. 9.5 GiB) -> continues blocking."""
        ram, gpu, disk, procs = make_test_snapshots(avail_ram_gib=8.0)
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        output_lines = []
        controller = GuidedStartupController(
            steward=mock_steward,
            output_fn=output_lines.append
        )

        controller.start()
        self.assertEqual(controller.current_state, StartupState.LOW_MEMORY_WAIT)

        # User freed some memory: now 9.5 GiB (deficit 0.50 GiB)
        new_ram, _, _, _ = make_test_snapshots(avail_ram_gib=9.50)
        mock_steward.ram = new_ram

        controller.recheck()
        self.assertEqual(controller.current_state, StartupState.LOW_MEMORY_WAIT)
        self.assertFalse(controller.model_loaded)
        self.assertTrue(controller.inference_blocked)

        output_text = "\n".join(output_lines)
        self.assertIn("Current available RAM: 9.50 GiB", output_text)
        self.assertIn("Additional available RAM required: 0.50 GiB", output_text)

    def test_06a_ram_sufficient_but_vram_fails_continues_blocking(self):
        """Test 6a: RAM is 10.4 GiB but VRAM < 5500 MiB -> continues blocking."""
        ram, gpu, disk, procs = make_test_snapshots(
            avail_ram_gib=10.40,
            vram_free_mib=5000.0  # Fails gate (< 5500 MiB)
        )
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        output_lines = []
        controller = GuidedStartupController(
            steward=mock_steward,
            output_fn=output_lines.append
        )

        controller.start()
        self.assertEqual(controller.current_state, StartupState.LOW_MEMORY_WAIT)
        self.assertFalse(controller.model_loaded)

        output_text = "\n".join(output_lines)
        self.assertIn("[PASS] avail_ram", output_text)
        self.assertIn("[FAIL] avail_vram", output_text)
        self.assertIn("VRAM admission gate failed: Available VRAM is below required threshold.", output_text)

    def test_06b_ram_sufficient_but_commit_fails_continues_blocking(self):
        """Test 6b: RAM is 10.4 GiB but commit >= 90% -> continues blocking."""
        ram, gpu, disk, procs = make_test_snapshots(
            avail_ram_gib=10.40,
            committed_gib=16.5,
            commit_total_gib=18.0  # 16.5 / 18.0 = 91.7% commit (Fails gate)
        )
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        output_lines = []
        controller = GuidedStartupController(
            steward=mock_steward,
            output_fn=output_lines.append
        )

        controller.start()
        self.assertEqual(controller.current_state, StartupState.LOW_MEMORY_WAIT)
        self.assertFalse(controller.model_loaded)

        output_text = "\n".join(output_lines)
        self.assertIn("[PASS] avail_ram", output_text)
        self.assertIn("[FAIL] system_commit", output_text)
        self.assertIn("System commit gate failed: System commit limit threshold exceeded.", output_text)

    def test_06c_ram_sufficient_but_disk_fails_distinguishes_disk_from_ram(self):
        """Test 6c: RAM is 10.4 GiB but free disk < 25 GiB -> storage warning distinct from RAM."""
        ram, gpu, disk, procs = make_test_snapshots(
            avail_ram_gib=10.40,
            disk_free_gib=15.0  # Fails gate (< 25.0 GiB)
        )
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        output_lines = []
        controller = GuidedStartupController(
            steward=mock_steward,
            output_fn=output_lines.append
        )

        controller.start()
        self.assertEqual(controller.current_state, StartupState.LOW_MEMORY_WAIT)
        self.assertFalse(controller.model_loaded)

        output_text = "\n".join(output_lines)
        self.assertIn("[PASS] avail_ram", output_text)
        self.assertIn("[FAIL] disk_free", output_text)
        self.assertIn("Storage admission gate failed: Free disk space is below the required threshold.", output_text)
        self.assertIn("Please free storage space on the model drive. Never delete files automatically.", output_text)

    def test_07_all_gates_pass_enters_ready_for_authorization(self):
        """Test 7: All 6 gates pass -> enters READY_FOR_AUTHORIZATION."""
        ram, gpu, disk, procs = make_test_snapshots(
            avail_ram_gib=10.50,
            committed_gib=6.0,
            commit_total_gib=18.0,
            vram_free_mib=5900.0,
            disk_free_gib=50.0,
            gpu_procs=0,
            telemetry_status=TelemetryStatus.SUCCESS
        )
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        output_lines = []
        controller = GuidedStartupController(
            steward=mock_steward,
            output_fn=output_lines.append
        )

        controller.start()
        self.assertEqual(controller.current_state, StartupState.READY_FOR_AUTHORIZATION)
        self.assertFalse(controller.model_loaded)
        self.assertTrue(controller.inference_blocked)

        output_text = "\n".join(output_lines)
        self.assertIn("HARDWARE QUALIFICATION AUDIT: PASSED", output_text)
        self.assertIn("All 6 admission gates PASSED. System is qualified for Configuration D smoke test.", output_text)
        self.assertIn("Inference state: BLOCKED UNTIL AUTHORIZED", output_text)
        self.assertIn("A successful preflight NEVER automatically launches model inference.", output_text)

    def test_08_successful_preflight_does_not_spawn_llama_cli(self):
        """Test 8: Successful preflight does not spawn llama-cli or load model."""
        ram, gpu, disk, procs = make_test_snapshots(avail_ram_gib=11.0)
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        controller = GuidedStartupController(
            steward=mock_steward,
            output_fn=lambda _: None
        )

        with patch("subprocess.Popen") as mock_popen, patch("subprocess.run") as mock_run:
            controller.start()
            self.assertEqual(controller.current_state, StartupState.READY_FOR_AUTHORIZATION)
            mock_popen.assert_not_called()
            mock_run.assert_not_called()

        self.assertFalse(controller.model_loaded)

    def test_09_user_exits_guided_mode_safe_shutdown(self):
        """Test 9: User exits guided mode ('q') -> safe shutdown."""
        ram, gpu, disk, procs = make_test_snapshots(avail_ram_gib=8.0)
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        output_lines = []
        controller = GuidedStartupController(
            steward=mock_steward,
            input_fn=lambda _: "q",
            output_fn=output_lines.append
        )

        ret = controller.run_interactive()
        self.assertEqual(ret, 0)
        self.assertEqual(controller.current_state, StartupState.ABORTED)
        self.assertIn("Exiting guided startup. Safe shutdown complete.", "\n".join(output_lines))

    def test_10_unauthorized_inference_transition_rejected(self):
        """Test 10: Unauthorized inference transition is strictly rejected."""
        sm = StartupStateMachine(StartupState.READY_FOR_AUTHORIZATION)
        sm._last_preflight_passed = True

        # Attempt transition to MODEL_INITIALIZATION without authorization
        with self.assertRaises(UnauthorizedInferenceError):
            sm.transition_to(StartupState.MODEL_INITIALIZATION, authorized=False)

        # Attempt transition from BOOTSTRAPPING directly to MODEL_INITIALIZATION
        sm_boot = StartupStateMachine(StartupState.BOOTSTRAPPING)
        with self.assertRaises(InvalidStateTransitionError):
            sm_boot.transition_to(StartupState.MODEL_INITIALIZATION, authorized=True)

        # Attempt transition to READY_FOR_AUTHORIZATION when preflight failed
        sm_pref = StartupStateMachine(StartupState.PREFLIGHT_RUNNING)
        with self.assertRaises(InvalidStateTransitionError):
            sm_pref.transition_to(StartupState.READY_FOR_AUTHORIZATION, preflight_passed=False)

        # Transition with authorization succeeds
        sm.transition_to(StartupState.MODEL_INITIALIZATION, authorized=True)
        self.assertEqual(sm.current_state, StartupState.MODEL_INITIALIZATION)
        self.assertTrue(sm.model_loaded)

    def test_11_cli_guided_start_mutual_exclusivity(self):
        """Test 11: CLI argument mutually exclusive with other modes."""
        from scripts.run_inference_with_telemetry import main
        import sys

        # Test conflicting arguments
        test_args = ["run_inference_with_telemetry.py", "--guided-start", "--execute"]
        with patch.object(sys, "argv", test_args):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 2)

    def test_12_controller_memory_profile_checkpoints_and_deltas(self):
        """Test 12: Checkpoints A, B, C, D sampling and delta calculations."""
        ram, gpu, disk, procs = make_test_snapshots(avail_ram_gib=8.0)
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        cp_a = ControllerMemorySample(
            checkpoint_id="CHECKPOINT_A",
            description="Earliest",
            pid=1234,
            working_set_bytes=10 * 1024 * 1024,
            working_set_mb=10.0,
            private_commit_bytes=8 * 1024 * 1024,
            private_commit_mb=8.0,
            page_fault_count=1000,
            cpu_time_seconds=0.05,
            system_avail_ram_gib=8.0,
            system_commit_pct=40.0,
            status=TelemetryStatus.SUCCESS
        )

        controller = GuidedStartupController(
            steward=mock_steward,
            early_sample=cp_a,
            input_fn=lambda _: "q",
            output_fn=lambda _: None
        )

        # Checkpoints A and B exist
        prof = controller.memory_profile
        self.assertIn("CHECKPOINT_A", prof.checkpoints)
        self.assertIn("CHECKPOINT_B", prof.checkpoints)
        self.assertEqual(prof.checkpoints["CHECKPOINT_A"].working_set_mb, 10.0)

        # Startup deltas are computed
        self.assertIsNotNone(prof.working_set_delta_startup_mb)
        self.assertIsNotNone(prof.private_commit_delta_startup_mb)

        # Start controller (transitions to LOW_MEMORY_WAIT, captures Checkpoint C)
        controller.start()
        self.assertIn("CHECKPOINT_C", prof.checkpoints)
        self.assertEqual(len(prof.idle_samples), 3)
        self.assertIsNotNone(prof.average_idle_working_set_mb)

        # Recheck captures Checkpoint D
        controller.recheck()
        self.assertIn("CHECKPOINT_D", prof.checkpoints)

    def test_13_controller_memory_profile_unknown_handling(self):
        """Test 13: Measurements are labeled UNKNOWN when unavailable."""
        prof = ControllerMemoryProfile()
        prof.checkpoints["CHECKPOINT_A"] = ControllerMemorySample(
            checkpoint_id="CHECKPOINT_A",
            description="Failed sample",
            pid=9999,
            working_set_bytes=0,
            working_set_mb=0.0,
            private_commit_bytes=None,
            private_commit_mb=None,
            page_fault_count=None,
            cpu_time_seconds=None,
            system_avail_ram_gib=0.0,
            system_commit_pct=0.0,
            status=TelemetryStatus.FAILED,
            error_message="Sensor offline"
        )
        prof.compute_deltas()

        self.assertIsNone(prof.working_set_delta_startup_mb)
        self.assertIsNone(prof.private_commit_delta_startup_mb)

        md = prof.format_profile_markdown()
        self.assertIn("UNKNOWN", md)

    def test_14_single_use_authorization_boundary_and_no_binary_launch(self):
        """Test 14: Single-use authorization boundary and zero binary invocation."""
        sm = StartupStateMachine(StartupState.READY_FOR_AUTHORIZATION)
        sm._last_preflight_passed = True

        # Empty or whitespace token is rejected
        with self.assertRaises(UnauthorizedInferenceError):
            sm.grant_authorization("")

        with self.assertRaises(UnauthorizedInferenceError):
            sm.grant_authorization("   ")

        # Grant valid single-use authorization
        sm.grant_authorization("AUTH-RUN-001")
        self.assertEqual(sm._pending_authorization_token, "AUTH-RUN-001")

        # First transition consumes the authorization
        with patch("subprocess.Popen") as mock_popen, patch("subprocess.run") as mock_run:
            sm.transition_to(StartupState.MODEL_INITIALIZATION)
            self.assertEqual(sm.current_state, StartupState.MODEL_INITIALIZATION)
            self.assertIsNone(sm._pending_authorization_token)
            # Binary is strictly never launched
            mock_popen.assert_not_called()
            mock_run.assert_not_called()

        # Second transition attempt without re-authorization is rejected
        sm._current_state = StartupState.READY_FOR_AUTHORIZATION
        with self.assertRaises(UnauthorizedInferenceError):
            sm.transition_to(StartupState.MODEL_INITIALIZATION)

    def test_15_ready_display_configuration_d_and_safety_summary(self):
        """Test 15: READY state displays Configuration D command and safety summary."""
        ram, gpu, disk, procs = make_test_snapshots(avail_ram_gib=10.5)
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        output_lines = []
        controller = GuidedStartupController(
            steward=mock_steward,
            output_fn=output_lines.append
        )

        controller.start()
        self.assertEqual(controller.current_state, StartupState.READY_FOR_AUTHORIZATION)

        output_text = "\n".join(output_lines)
        self.assertIn("SYSTEM QUALIFIED", output_text)
        self.assertIn("Status: ALL ADMISSION GATES PASSED", output_text)
        self.assertIn("llama-cli.exe -m Qwen_Qwen3-30B-A3B-Q4_K_M.gguf -ngl 48 -ncmoe 40 -c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap -n 1 --simple-io --perf -p \"Hello\"", output_text)
        self.assertIn("Runtime Safety Summary:", output_text)
        self.assertIn("CRITICAL SAFETY GOVERNANCE:", output_text)
        self.assertIn("Inference state: BLOCKED UNTIL AUTHORIZED", output_text)

    def test_16_process_working_set_and_private_commit_reported_separately(self):
        """Test 16: Working set and private commit are reported separately in advice."""
        ram, gpu, disk, procs = make_test_snapshots(avail_ram_gib=8.0)
        mock_steward = MockResourceSteward(ram, gpu, disk, procs)

        output_lines = []
        controller = GuidedStartupController(
            steward=mock_steward,
            output_fn=output_lines.append
        )

        controller.start()
        output_text = "\n".join(output_lines)

        self.assertIn("Working Set:", output_text)
        self.assertIn("Private Commit:", output_text)
        self.assertIn("Why:", output_text)
        self.assertIn("Action:", output_text)



if __name__ == "__main__":
    unittest.main()
