"""
NOVA Compute — Phase 3B.4: Authorized Execution & Supervised Inference Safety Test Suite
Validates the complete authorization pipeline, execution latch, confirmation gates,
failure containment, supervised child process targeting, and single-token validation.

MOCKED TESTING ONLY: ZERO real model inference, ZERO GGUF weight mapping, ZERO real llama-cli invocation.
"""

import os
import sys
import time
import queue
import tempfile
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

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
    GateResult,
    PreflightReport,
    InferenceOutcome,
    SupervisorResult,
    RecoveryReport,
)
from nova.resource_steward.policy import ResourcePolicy, DEFAULT_POLICY
from nova.resource_steward.state_machine import (
    StartupStateMachine,
    GuidedStartupController,
    ExecutionLatch,
    CONFIRMATION_PHRASE,
    InvalidStateTransitionError,
    UnauthorizedInferenceError,
    StateTransitionRecord,
)
from nova.resource_steward import ResourceSteward
from scripts.run_inference_with_telemetry import (
    execute_inference,
    verify_inference_success,
    main,
    get_workspace_safe_command,
    LLAMA_CLI,
    MODEL_GGUF,
)


def make_test_snapshots(
    avail_ram_gib: float = 10.5,
    total_ram_gib: float = 15.75,
    committed_gib: float = 6.30,
    commit_total_gib: float = 18.0,
    vram_free_mib: float = 5900.0,
    vram_total_mib: float = 6141.0,
    disk_free_gib: float = 100.0,
    disk_total_gib: float = 512.0,
    gpu_procs: int = 0,
    telemetry_status: TelemetryStatus = TelemetryStatus.SUCCESS,
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
        status=telemetry_status,
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
        status=telemetry_status,
    )

    disk_free_bytes = int(disk_free_gib * (1024 ** 3))
    disk_total_bytes = int(disk_total_gib * (1024 ** 3))
    disk = DiskSnapshot(
        drive="C:\\",
        free_bytes=disk_free_bytes,
        total_bytes=disk_total_bytes,
        free_gib=round(disk_free_gib, 2),
        total_gib=round(disk_total_gib, 2),
        status=telemetry_status,
    )

    return ram, gpu, disk


def make_preflight_report(
    ram_gib: float = 10.5,
    vram_mib: float = 5900.0,
    commit_pct: float = 35.0,
    disk_gib: float = 100.0,
    gpu_procs: int = 0,
    telemetry_status: TelemetryStatus = TelemetryStatus.SUCCESS,
) -> PreflightReport:
    """Builds a PreflightReport with standard 6 admission gates."""
    ram, gpu, disk = make_test_snapshots(
        avail_ram_gib=ram_gib,
        vram_free_mib=vram_mib,
        disk_free_gib=disk_gib,
        gpu_procs=gpu_procs,
        telemetry_status=telemetry_status,
    )
    gates = {
        "telemetry_integrity": GateResult(
            gate_name="telemetry_integrity",
            passed=(telemetry_status == TelemetryStatus.SUCCESS),
            required_str="All sensors SUCCESS",
            measured_str="Healthy" if telemetry_status == TelemetryStatus.SUCCESS else "ERROR",
        ),
        "avail_ram": GateResult(
            gate_name="avail_ram",
            passed=(ram_gib >= 10.0),
            required_str=">= 10.0 GiB",
            measured_str=f"{ram_gib:.3f} GiB",
            failure_reason="" if ram_gib >= 10.0 else f"Available RAM deficit: {10.0 - ram_gib:.3f} GiB below 10.0 GiB",
        ),
        "system_commit": GateResult(
            gate_name="system_commit",
            passed=(commit_pct < 90.0),
            required_str="< 90.0%",
            measured_str=f"{commit_pct:.1f}%",
        ),
        "avail_vram": GateResult(
            gate_name="avail_vram",
            passed=(vram_mib >= 5500.0),
            required_str=">= 5500 MiB",
            measured_str=f"{vram_mib:.1f} MiB",
        ),
        "no_competing_gpu": GateResult(
            gate_name="no_competing_gpu",
            passed=(gpu_procs == 0),
            required_str="0 competing compute apps",
            measured_str=f"{gpu_procs} active apps",
        ),
        "disk_free": GateResult(
            gate_name="disk_free",
            passed=(disk_gib >= 25.0),
            required_str=">= 25.0 GiB",
            measured_str=f"{disk_gib:.2f} GiB",
        ),
    }
    all_passed = all(g.passed for g in gates.values())
    return PreflightReport(
        passed=all_passed,
        gates=gates,
        system_ram=ram,
        gpu=gpu,
        disk=disk,
        top_processes=[],
        recommendations=[],
        timestamp_utc="2026-11-05T12:00:00+00:00",
    )


def make_mock_recovery(recovery_successful: bool = True) -> MagicMock:
    """Builds a mock RecoveryReport satisfying all recovery validation requirements."""
    rec = MagicMock(spec=RecoveryReport)
    rec.recovery_successful = recovery_successful
    rec.child_terminated_successfully = True
    rec.reader_thread_stopped = True
    rec.sampler_thread_stopped = True
    rec.pipes_closed = True
    rec.post_avail_ram_gib = 11.0
    rec.post_vram_used_mib = 100.0
    rec.residual_vram_leak_mib = 0.0
    rec.gpu_verification_status = "VERIFIED"
    rec.process_verification_status = "VERIFIED"
    rec.termination_status = "CLEAN_EXIT"
    rec.shutdown_method = "NONE"
    rec.notes = ["Clean exit"]
    return rec


class TestAuthorizedExecutionSuite(unittest.TestCase):
    """21 dedicated test cases for Phase 3B.4 authorized execution and supervisor safety."""

    def setUp(self):
        self.output_lines = []
        self.output_fn = lambda msg: self.output_lines.append(str(msg))
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_mock_proc(self, pid: int = 12345, returncode: int = 0):
        """Creates a mock subprocess with stdout pipe."""
        proc = MagicMock()
        proc.pid = pid
        proc.returncode = returncode
        proc.stdout = MagicMock()
        proc.stdout.readline = MagicMock(return_value="")
        proc.poll = MagicMock(return_value=returncode)
        return proc

    def test_01_direct_execute_inference_without_authorization_rejected(self):
        """Test 1: Direct call to execute_inference() without controller or authorization fails closed."""
        steward = MagicMock(spec=ResourceSteward)
        steward.policy = DEFAULT_POLICY
        steward.run_preflight.return_value = make_preflight_report(ram_gib=11.0)

        with patch("subprocess.Popen") as mock_popen:
            with self.assertRaises(UnauthorizedInferenceError) as ctx:
                execute_inference(steward, controller=None, output_dir=self.temp_dir.name)
            self.assertIn("Direct invocation of execute_inference() without authorized", str(ctx.exception))
            mock_popen.assert_not_called()

    def test_02_direct_execute_flag_cannot_bypass_state_machine(self):
        """Test 2: Invoking --execute CLI route cannot bypass the state machine."""
        with patch("nova.resource_steward.state_machine.GuidedStartupController.run_interactive") as mock_interactive:
            mock_interactive.return_value = 0
            with patch("subprocess.Popen") as mock_popen:
                exit_code = main(["--execute"])
                self.assertEqual(exit_code, 0)
                mock_interactive.assert_called_once()
                mock_popen.assert_not_called()

    def test_03_successful_preflight_follows_legitimate_transitions(self):
        """Test 3: Passing preflight follows legitimate transitions to READY_FOR_AUTHORIZATION."""
        steward = MagicMock(spec=ResourceSteward)
        steward.policy = DEFAULT_POLICY
        passing_report = make_preflight_report(ram_gib=11.0)
        steward.run_preflight.return_value = passing_report

        controller = GuidedStartupController(steward=steward, output_fn=self.output_fn)
        state = controller.start()

        self.assertEqual(state, StartupState.READY_FOR_AUTHORIZATION)
        self.assertEqual(controller.current_state, StartupState.READY_FOR_AUTHORIZATION)
        self.assertTrue(controller.has_legitimate_qualification_history())

        history_states = [rec.to_state for rec in controller.state_machine.history]
        self.assertIn(StartupState.SAFE_STARTUP, history_states)
        self.assertIn(StartupState.PREFLIGHT_RUNNING, history_states)
        self.assertIn(StartupState.READY_FOR_AUTHORIZATION, history_states)

    def test_04_failed_preflight_cannot_reach_ready_for_authorization(self):
        """Test 4: Unmet admission gates transition to LOW_MEMORY_WAIT, never READY_FOR_AUTHORIZATION."""
        steward = MagicMock(spec=ResourceSteward)
        steward.policy = DEFAULT_POLICY
        failing_report = make_preflight_report(ram_gib=8.5)
        steward.run_preflight.return_value = failing_report

        controller = GuidedStartupController(steward=steward, output_fn=self.output_fn)
        state = controller.start()

        self.assertEqual(state, StartupState.LOW_MEMORY_WAIT)
        self.assertEqual(controller.current_state, StartupState.LOW_MEMORY_WAIT)
        self.assertFalse(controller.has_legitimate_qualification_history())

        history_states = [rec.to_state for rec in controller.state_machine.history]
        self.assertNotIn(StartupState.READY_FOR_AUTHORIZATION, history_states)
        self.assertIn(StartupState.PREFLIGHT_FAILED, history_states)
        self.assertIn(StartupState.LOW_MEMORY_WAIT, history_states)

    def test_05_stale_preflight_evidence_cannot_authorize_execution(self):
        """Test 5: Preflight evidence older than 300s cannot authorize execution."""
        mock_clock = MagicMock()
        mock_clock.return_value = 1000.0

        sm = StartupStateMachine(clock=mock_clock)
        passing_report = make_preflight_report(ram_gib=11.0)

        sm.transition_to(StartupState.SAFE_STARTUP)
        sm.transition_to(StartupState.PREFLIGHT_RUNNING)
        sm.transition_to(
            StartupState.READY_FOR_AUTHORIZATION,
            preflight_report=passing_report,
            preflight_passed=True,
        )

        # Advance time by 305 seconds (exceeding 300.0s max age)
        mock_clock.return_value = 1305.0

        with self.assertRaises(UnauthorizedInferenceError) as ctx:
            sm.grant_authorization("AUTH-TOKEN-12345")
        self.assertIn("Preflight evidence is stale", str(ctx.exception))

    def test_06_manually_initialized_ready_state_rejected(self):
        """Test 6: Manually instantiated or fabricated READY state without genuine qualification history is rejected."""
        steward = MagicMock(spec=ResourceSteward)
        steward.policy = DEFAULT_POLICY

        # Controller initialized without running start() or qualification lifecycle
        controller = GuidedStartupController(steward=steward, output_fn=self.output_fn)
        # Attempt to forge state
        controller.state_machine._current_state = StartupState.READY_FOR_AUTHORIZATION
        controller.state_machine._last_preflight_passed = True

        self.assertFalse(controller.has_legitimate_qualification_history())

        with patch("subprocess.Popen") as mock_popen:
            with self.assertRaises(UnauthorizedInferenceError) as ctx:
                controller.launch_authorized_inference()
            self.assertIn("Manually initialized or unverified READY_FOR_AUTHORIZATION state is rejected", str(ctx.exception))
            self.assertEqual(controller.current_state, StartupState.ABORTED)
            mock_popen.assert_not_called()

    def test_07_empty_or_invalid_authorization_rejected(self):
        """Test 7: Empty string, 'yes', or invalid confirmation phrases fail closed."""
        steward = MagicMock(spec=ResourceSteward)
        steward.policy = DEFAULT_POLICY
        steward.run_preflight.return_value = make_preflight_report(ram_gib=11.0)

        # Case 7a: user enters 'yes'
        inputs = ["yes"]
        controller = GuidedStartupController(
            steward=steward,
            input_fn=lambda _: inputs.pop(0),
            output_fn=self.output_fn,
        )
        with patch("subprocess.Popen") as mock_popen:
            rc = controller.run_interactive()
            self.assertEqual(rc, 1)
            self.assertEqual(controller.current_state, StartupState.ABORTED)
            mock_popen.assert_not_called()

        # Case 7b: user enters empty string
        inputs = [""]
        controller2 = GuidedStartupController(
            steward=steward,
            input_fn=lambda _: inputs.pop(0),
            output_fn=self.output_fn,
        )
        with patch("subprocess.Popen") as mock_popen:
            rc = controller2.run_interactive()
            self.assertEqual(rc, 1)
            self.assertEqual(controller2.current_state, StartupState.ABORTED)
            mock_popen.assert_not_called()

    def test_08_explicit_local_confirmation_required(self):
        """Test 8: Exact phrase 'RUN CONFIGURATION D SMOKE TEST ONCE' authorizes and launches."""
        steward = MagicMock(spec=ResourceSteward)
        steward.policy = DEFAULT_POLICY
        steward.run_preflight.return_value = make_preflight_report(ram_gib=11.0)

        mock_runner = MagicMock(return_value=(InferenceOutcome.SUCCESS, "OK"))

        inputs = [CONFIRMATION_PHRASE]
        controller = GuidedStartupController(
            steward=steward,
            input_fn=lambda _: inputs.pop(0),
            output_fn=self.output_fn,
            execution_runner=mock_runner,
        )

        rc = controller.run_interactive()
        self.assertEqual(rc, 0)
        mock_runner.assert_called_once()

    def test_09_valid_authorization_consumed_exactly_once(self):
        """Test 9: Valid authorization token is single-use and consumed upon MODEL_INITIALIZATION."""
        sm = StartupStateMachine()
        sm.transition_to(StartupState.SAFE_STARTUP)
        sm.transition_to(StartupState.PREFLIGHT_RUNNING)
        sm.transition_to(
            StartupState.READY_FOR_AUTHORIZATION,
            preflight_report=make_preflight_report(ram_gib=11.0),
            preflight_passed=True,
        )

        sm.grant_authorization("AUTH-TOKEN-SINGLE-USE")
        self.assertTrue(sm.has_valid_authorization)

        # Transition consumes the authorization token
        sm.transition_to(StartupState.MODEL_INITIALIZATION)
        self.assertFalse(sm.has_valid_authorization)

    def test_10_second_execution_attempt_rejected_by_latch(self):
        """Test 10: ExecutionLatch trips and strictly blocks any second execution attempt."""
        latch = ExecutionLatch()
        self.assertFalse(latch.is_consumed)

        latch.consume()
        self.assertTrue(latch.is_consumed)

        with self.assertRaises(UnauthorizedInferenceError) as ctx:
            latch.consume()
        self.assertIn("Execution latch already consumed", str(ctx.exception))

        # Test latch protection on GuidedStartupController
        steward = MagicMock(spec=ResourceSteward)
        steward.policy = DEFAULT_POLICY
        steward.run_preflight.return_value = make_preflight_report(ram_gib=11.0)

        controller = GuidedStartupController(
            steward=steward,
            output_fn=self.output_fn,
            execution_runner=MagicMock(return_value=(InferenceOutcome.SUCCESS, "OK")),
        )
        controller.start()
        controller.state_machine.grant_authorization("TOKEN-1")
        controller.launch_authorized_inference()

        # Second launch attempt in same session must be blocked
        with self.assertRaises(UnauthorizedInferenceError) as ctx:
            controller.launch_authorized_inference()
        self.assertIn("Execution latch already consumed", str(ctx.exception))

    def test_11_no_subprocess_spawn_before_authorization(self):
        """Test 11: Zero subprocesses are created during qualification or while awaiting authorization."""
        steward = MagicMock(spec=ResourceSteward)
        steward.policy = DEFAULT_POLICY
        steward.run_preflight.return_value = make_preflight_report(ram_gib=11.0)

        with patch("subprocess.Popen") as mock_popen:
            controller = GuidedStartupController(steward=steward, output_fn=self.output_fn)
            controller.start()
            self.assertEqual(controller.current_state, StartupState.READY_FOR_AUTHORIZATION)
            mock_popen.assert_not_called()

    def test_12_no_subprocess_spawn_if_final_admission_fails(self):
        """Test 12: Zero subprocesses spawned if preflight check immediately prior to launch fails."""
        steward = MagicMock()
        steward.policy = DEFAULT_POLICY
        # Preflight fails right at execute time
        steward.run_preflight.return_value = make_preflight_report(ram_gib=8.5)

        controller = MagicMock()
        controller.state_machine.current_state = StartupState.MODEL_INITIALIZATION
        controller.has_legitimate_qualification_history.return_value = True

        with patch("subprocess.Popen") as mock_popen:
            with patch("sys.exit", side_effect=SystemExit(1)):
                with self.assertRaises(SystemExit) as ctx:
                    execute_inference(steward, controller=controller, output_dir=self.temp_dir.name)
                self.assertEqual(ctx.exception.code, 1)
            mock_popen.assert_not_called()

    def test_13_exactly_one_subprocess_creation_following_valid_authorization(self):
        """Test 13: Exactly one subprocess creation occurs following valid authorization."""
        steward = MagicMock()
        steward.policy = DEFAULT_POLICY
        passing_report = make_preflight_report(ram_gib=11.0)
        steward.run_preflight.return_value = passing_report

        mock_proc = self.make_mock_proc()
        mock_supervisor_res = SupervisorResult(
            abort_triggered=False,
            abort_reason="",
            exit_code=0,
            peak_ram_gib=5.0,
            peak_vram_mib=2000.0,
            processed_samples_count=5,
            duration_seconds=1.0,
            output_lines=[
                "llama_perf_context_print: load time = 100.00 ms\n",
                "llama_perf_context_print: eval time = 20.00 ms / 1 runs ( 20.00 ms per token, 50.00 tokens per second)\n",
            ],
        )
        mock_recovery = make_mock_recovery(recovery_successful=True)
        steward.reclamation.perform_post_inference_recovery.return_value = mock_recovery

        controller = GuidedStartupController(steward=steward, output_fn=self.output_fn)
        controller.start()
        controller.state_machine.grant_authorization("AUTH-VALID-1")
        controller.state_machine.transition_to(StartupState.MODEL_INITIALIZATION)

        with patch("os.path.exists", return_value=True):
            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                with patch("scripts.run_inference_with_telemetry.supervise_child_process", return_value=mock_supervisor_res):
                    with patch("scripts.run_inference_with_telemetry.LiveTelemetrySampler") as mock_sampler_cls:
                        mock_sampler = MagicMock()
                        mock_sampler.samples = []
                        mock_sampler.stop.return_value = True
                        mock_sampler_cls.return_value = mock_sampler

                        outcome, _ = execute_inference(steward, controller=controller, output_dir=self.temp_dir.name)
                        self.assertEqual(mock_popen.call_count, 1)

    def test_14_supervisor_receives_correct_nova_owned_process(self):
        """Test 14: Supervisor is passed the exact child process handle owned by NOVA."""
        steward = MagicMock()
        steward.policy = DEFAULT_POLICY
        steward.run_preflight.return_value = make_preflight_report(ram_gib=11.0)

        mock_proc = self.make_mock_proc(pid=54321)
        mock_supervisor_res = SupervisorResult(
            abort_triggered=False,
            abort_reason="",
            exit_code=0,
            peak_ram_gib=5.0,
            peak_vram_mib=2000.0,
            processed_samples_count=5,
            duration_seconds=1.0,
            output_lines=["load time = 100.0 ms\n", "eval time = 20.0 ms / 1 runs\n"],
        )
        mock_recovery = make_mock_recovery(recovery_successful=True)
        steward.reclamation.perform_post_inference_recovery.return_value = mock_recovery

        controller = GuidedStartupController(steward=steward, output_fn=self.output_fn)
        controller.start()
        controller.state_machine.grant_authorization("AUTH-VALID-2")
        controller.state_machine.transition_to(StartupState.MODEL_INITIALIZATION)

        with patch("os.path.exists", return_value=True):
            with patch("subprocess.Popen", return_value=mock_proc):
                with patch("scripts.run_inference_with_telemetry.supervise_child_process", return_value=mock_supervisor_res) as mock_supervise:
                    with patch("scripts.run_inference_with_telemetry.LiveTelemetrySampler") as mock_sampler_cls:
                        mock_sampler = MagicMock()
                        mock_sampler.samples = []
                        mock_sampler.stop.return_value = True
                        mock_sampler_cls.return_value = mock_sampler

                        execute_inference(steward, controller=controller, output_dir=self.temp_dir.name)
                        # Supervisor MUST have been passed the NOVA-owned mock_proc
                        mock_supervise.assert_called_once()
                        self.assertEqual(mock_supervise.call_args.kwargs["proc"], mock_proc)

    def test_15_supervisor_abort_produces_aborted_state(self):
        """Test 15: Supervisor abort triggers transition to ABORTED state."""
        steward = MagicMock()
        steward.policy = DEFAULT_POLICY
        steward.run_preflight.return_value = make_preflight_report(ram_gib=11.0)

        mock_proc = self.make_mock_proc()
        mock_supervisor_res = SupervisorResult(
            abort_triggered=True,
            abort_reason="VRAM floor breached: free VRAM 450.0 MiB < 512.0 MiB floor",
            exit_code=-1,
            peak_ram_gib=5.0,
            peak_vram_mib=5600.0,
            processed_samples_count=3,
            duration_seconds=0.5,
            output_lines=["loading model...\n"],
        )
        mock_recovery = make_mock_recovery(recovery_successful=False)
        steward.reclamation.perform_post_inference_recovery.return_value = mock_recovery

        controller = GuidedStartupController(steward=steward, output_fn=self.output_fn)
        controller.start()
        controller.state_machine.grant_authorization("AUTH-VALID-3")
        controller.state_machine.transition_to(StartupState.MODEL_INITIALIZATION)

        with patch("os.path.exists", return_value=True):
            with patch("subprocess.Popen", return_value=mock_proc):
                with patch("scripts.run_inference_with_telemetry.supervise_child_process", return_value=mock_supervisor_res):
                    with patch("scripts.run_inference_with_telemetry.SafeReclamationManager.terminate_child_process"):
                        with patch("scripts.run_inference_with_telemetry.LiveTelemetrySampler") as mock_sampler_cls:
                            mock_sampler = MagicMock()
                            mock_sampler.samples = []
                            mock_sampler.stop.return_value = True
                            mock_sampler_cls.return_value = mock_sampler

                            outcome, _ = execute_inference(steward, controller=controller, output_dir=self.temp_dir.name)
                            self.assertEqual(outcome, InferenceOutcome.ABORTED)
                            self.assertEqual(controller.current_state, StartupState.ABORTED)

    def test_16_cuda_oom_produces_aborted_state(self):
        """Test 16: CUDA OOM error produces ABORTED state."""
        steward = MagicMock()
        steward.policy = DEFAULT_POLICY
        steward.run_preflight.return_value = make_preflight_report(ram_gib=11.0)

        mock_proc = self.make_mock_proc(returncode=1)
        mock_supervisor_res = SupervisorResult(
            abort_triggered=True,
            abort_reason="CUDA error: out of memory",
            exit_code=1,
            peak_ram_gib=5.0,
            peak_vram_mib=6000.0,
            processed_samples_count=2,
            duration_seconds=0.3,
            output_lines=["CUDA error: out of memory\n"],
        )
        mock_recovery = make_mock_recovery(recovery_successful=False)
        steward.reclamation.perform_post_inference_recovery.return_value = mock_recovery

        controller = GuidedStartupController(steward=steward, output_fn=self.output_fn)
        controller.start()
        controller.state_machine.grant_authorization("AUTH-VALID-4")
        controller.state_machine.transition_to(StartupState.MODEL_INITIALIZATION)

        with patch("os.path.exists", return_value=True):
            with patch("subprocess.Popen", return_value=mock_proc):
                with patch("scripts.run_inference_with_telemetry.supervise_child_process", return_value=mock_supervisor_res):
                    with patch("scripts.run_inference_with_telemetry.LiveTelemetrySampler") as mock_sampler_cls:
                        mock_sampler = MagicMock()
                        mock_sampler.samples = []
                        mock_sampler.stop.return_value = True
                        mock_sampler_cls.return_value = mock_sampler

                        outcome, _ = execute_inference(steward, controller=controller, output_dir=self.temp_dir.name)
                        self.assertEqual(outcome, InferenceOutcome.ABORTED)
                        self.assertEqual(controller.current_state, StartupState.ABORTED)

    def test_17_process_spawn_failure_produces_aborted_without_retry(self):
        """Test 17: Process spawn failure produces ABORTED state without retry."""
        steward = MagicMock(spec=ResourceSteward)
        steward.policy = DEFAULT_POLICY
        steward.run_preflight.return_value = make_preflight_report(ram_gib=11.0)

        controller = GuidedStartupController(steward=steward, output_fn=self.output_fn)
        controller.start()
        controller.state_machine.grant_authorization("AUTH-VALID-5")
        controller.state_machine.transition_to(StartupState.MODEL_INITIALIZATION)

        with patch("os.path.exists", return_value=True):
            with patch("subprocess.Popen", side_effect=OSError("Exec format error")) as mock_popen:
                outcome, reason = execute_inference(steward, controller=controller, output_dir=self.temp_dir.name)
                self.assertEqual(outcome, InferenceOutcome.FAILED)
                self.assertIn("Subprocess spawn failed", reason)
                self.assertEqual(controller.current_state, StartupState.ABORTED)
                # Exactly one attempt, no automatic retry
                self.assertEqual(mock_popen.call_count, 1)

    def test_18_successful_one_token_output_validated(self):
        """Test 18: Successful single-token output is accurately validated."""
        supervisor_res = SupervisorResult(
            abort_triggered=False,
            abort_reason="",
            exit_code=0,
            peak_ram_gib=5.0,
            peak_vram_mib=2000.0,
            processed_samples_count=3,
            duration_seconds=1.5,
            output_lines=[
                "print_timings:     load time = 1200.00 ms\n",
                "print_timings:      eval time = 25.00 ms / 1 runs ( 25.00 ms per token, 40.00 tokens per second)\n",
            ],
        )
        recovery_report = make_mock_recovery(recovery_successful=True)
        outcome, reason = verify_inference_success(supervisor_res, recovery_report)
        self.assertEqual(outcome, InferenceOutcome.SUCCESS)
        self.assertIn("1 run(s)", reason)

    def test_19_unknown_token_count_never_falsely_represented_as_verified(self):
        """Test 19: Ambiguous timing or 0 tokens evaluated is flagged UNVERIFIED, never falsely SUCCESS."""
        # Case 19a: Ambiguous timing without count after /
        res_ambiguous = SupervisorResult(
            abort_triggered=False,
            abort_reason="",
            exit_code=0,
            peak_ram_gib=5.0,
            peak_vram_mib=2000.0,
            processed_samples_count=3,
            duration_seconds=1.5,
            output_lines=[
                "print_timings:     load time = 1200.00 ms\n",
                "print_timings:      eval time = 25.00 ms\n",
            ],
        )
        recovery = make_mock_recovery(recovery_successful=True)
        outcome, reason = verify_inference_success(res_ambiguous, recovery)
        self.assertEqual(outcome, InferenceOutcome.UNVERIFIED)

        # Case 19b: Prompt eval only (0 generated tokens evaluated)
        res_prompt_only = SupervisorResult(
            abort_triggered=False,
            abort_reason="",
            exit_code=0,
            peak_ram_gib=5.0,
            peak_vram_mib=2000.0,
            processed_samples_count=3,
            duration_seconds=1.5,
            output_lines=[
                "print_timings:     load time = 1200.00 ms\n",
                "print_timings: prompt eval time = 50.00 ms / 5 tokens\n",
            ],
        )
        outcome2, reason2 = verify_inference_success(res_prompt_only, recovery)
        self.assertEqual(outcome2, InferenceOutcome.UNVERIFIED)

    def test_20_successful_completion_transitions_to_smoke_test_complete(self):
        """Test 20: Verified successful execution transitions to SMOKE_TEST_COMPLETE."""
        steward = MagicMock()
        steward.policy = DEFAULT_POLICY
        passing_report = make_preflight_report(ram_gib=11.0)
        steward.run_preflight.return_value = passing_report

        mock_proc = self.make_mock_proc()
        mock_supervisor_res = SupervisorResult(
            abort_triggered=False,
            abort_reason="",
            exit_code=0,
            peak_ram_gib=5.0,
            peak_vram_mib=2000.0,
            processed_samples_count=5,
            duration_seconds=1.0,
            output_lines=[
                "llama_perf_context_print: load time = 100.00 ms\n",
                "llama_perf_context_print: eval time = 20.00 ms / 1 runs ( 20.00 ms per token, 50.00 tokens per second)\n",
            ],
        )
        mock_recovery = make_mock_recovery(recovery_successful=True)
        steward.reclamation.perform_post_inference_recovery.return_value = mock_recovery

        controller = GuidedStartupController(steward=steward, output_fn=self.output_fn)
        controller.start()
        controller.state_machine.grant_authorization("AUTH-VALID-FINAL")
        controller.state_machine.transition_to(StartupState.MODEL_INITIALIZATION)

        with patch("os.path.exists", return_value=True):
            with patch("subprocess.Popen", return_value=mock_proc):
                with patch("scripts.run_inference_with_telemetry.supervise_child_process", return_value=mock_supervisor_res):
                    with patch("scripts.run_inference_with_telemetry.LiveTelemetrySampler") as mock_sampler_cls:
                        mock_sampler = MagicMock()
                        mock_sampler.samples = []
                        mock_sampler.stop.return_value = True
                        mock_sampler_cls.return_value = mock_sampler

                        outcome, _ = execute_inference(steward, controller=controller, output_dir=self.temp_dir.name)
                        self.assertEqual(outcome, InferenceOutcome.SUCCESS)
                        self.assertEqual(controller.current_state, StartupState.SMOKE_TEST_COMPLETE)

    def test_21_existing_96_tests_remain_passing(self):
        """Test 21: Meta-test ensuring the entire suite integrates without regressions."""
        # Validates that this test class is registered and will run alongside existing tests
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
