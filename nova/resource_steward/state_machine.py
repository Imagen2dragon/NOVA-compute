"""
NOVA Compute — Startup State Machine & Guided Low-Memory Controller
Phase 3B.1 & Phase 3B.2: Implements formal lifecycle states, strict transition validation,
safe lightweight low-memory bootstrapping, read-only memory recovery guidance,
controller process memory footprint measurement (Checkpoints A-D),
and decoupling of qualification from model inference execution.
"""

from typing import List, Dict, Optional, Set, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import math
import time

from .models import (
    StartupState,
    TelemetryStatus,
    PreflightReport,
    ControllerMemorySample,
    ControllerMemoryProfile,
    current_iso_utc
)
from .policy import ResourcePolicy, enforce_mandatory_admission_floors
from .monitor import ResourceMonitor
from .memory_budget import MemoryBudgetManager
from .advisor import MemoryAdvisor
from .models import MemoryAdvisorReport, ProcessResourceSnapshot


class InvalidStateTransitionError(ValueError):
    """Raised when an illegal lifecycle state transition is attempted."""
    pass


class UnauthorizedInferenceError(InvalidStateTransitionError):
    """Raised when model initialization or inference is attempted without explicit authorization."""
    pass


@dataclass
class StateTransitionRecord:
    """Audit log entry capturing state machine transitions."""
    from_state: StartupState
    to_state: StartupState
    timestamp_utc: str = field(default_factory=current_iso_utc)
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class StartupStateMachine:
    """
    Formal lifecycle state machine for NOVA Compute startup and model loading.
    
    States:
      - BOOTSTRAPPING
      - SAFE_STARTUP
      - LOW_MEMORY_WAIT
      - RESOURCE_RECOVERY
      - PREFLIGHT_RUNNING
      - PREFLIGHT_FAILED
      - READY_FOR_AUTHORIZATION
      - MODEL_INITIALIZATION
      - MODEL_RUNTIME
      - SMOKE_TEST_COMPLETE
      - ABORTED

    Strict Transition Rules:
      - BOOTSTRAPPING -> SAFE_STARTUP
      - SAFE_STARTUP -> LOW_MEMORY_WAIT | PREFLIGHT_RUNNING
      - LOW_MEMORY_WAIT -> RESOURCE_RECOVERY
      - RESOURCE_RECOVERY -> PREFLIGHT_RUNNING
      - PREFLIGHT_RUNNING -> PREFLIGHT_FAILED | READY_FOR_AUTHORIZATION (only when all 6 gates pass)
      - PREFLIGHT_FAILED -> LOW_MEMORY_WAIT
      - READY_FOR_AUTHORIZATION -> MODEL_INITIALIZATION (only on explicit authorization) | RESOURCE_RECOVERY | LOW_MEMORY_WAIT
      - MODEL_INITIALIZATION -> MODEL_RUNTIME
      - MODEL_RUNTIME -> SMOKE_TEST_COMPLETE
      - Any non-terminal state -> ABORTED (on critical safety breach or user abort)
    """

    ALLOWED_TRANSITIONS: Dict[StartupState, Set[StartupState]] = {
        StartupState.BOOTSTRAPPING: {
            StartupState.SAFE_STARTUP,
            StartupState.ABORTED
        },
        StartupState.SAFE_STARTUP: {
            StartupState.LOW_MEMORY_WAIT,
            StartupState.PREFLIGHT_RUNNING,
            StartupState.ABORTED
        },
        StartupState.LOW_MEMORY_WAIT: {
            StartupState.RESOURCE_RECOVERY,
            StartupState.ABORTED
        },
        StartupState.RESOURCE_RECOVERY: {
            StartupState.PREFLIGHT_RUNNING,
            StartupState.ABORTED
        },
        StartupState.PREFLIGHT_RUNNING: {
            StartupState.PREFLIGHT_FAILED,
            StartupState.READY_FOR_AUTHORIZATION,
            StartupState.ABORTED
        },
        StartupState.PREFLIGHT_FAILED: {
            StartupState.LOW_MEMORY_WAIT,
            StartupState.ABORTED
        },
        StartupState.READY_FOR_AUTHORIZATION: {
            StartupState.MODEL_INITIALIZATION,
            StartupState.RESOURCE_RECOVERY,
            StartupState.LOW_MEMORY_WAIT,
            StartupState.ABORTED
        },
        StartupState.MODEL_INITIALIZATION: {
            StartupState.MODEL_RUNTIME,
            StartupState.ABORTED
        },
        StartupState.MODEL_RUNTIME: {
            StartupState.SMOKE_TEST_COMPLETE,
            StartupState.ABORTED
        },
        StartupState.SMOKE_TEST_COMPLETE: {
            StartupState.ABORTED
        },
        StartupState.ABORTED: set()
    }

    def __init__(self, initial_state: StartupState = StartupState.BOOTSTRAPPING):
        self._current_state = initial_state
        self._history: List[StateTransitionRecord] = []
        self._last_preflight_passed: bool = False
        self._pending_authorization_token: Optional[str] = None

    @property
    def current_state(self) -> StartupState:
        return self._current_state

    @property
    def history(self) -> List[StateTransitionRecord]:
        return list(self._history)

    @property
    def model_loaded(self) -> bool:
        """Model weights are strictly NOT loaded until authorized MODEL_INITIALIZATION."""
        return self._current_state in (
            StartupState.MODEL_INITIALIZATION,
            StartupState.MODEL_RUNTIME,
            StartupState.SMOKE_TEST_COMPLETE
        )

    @property
    def inference_blocked(self) -> bool:
        """Inference is strictly blocked in all states except MODEL_RUNTIME."""
        return self._current_state != StartupState.MODEL_RUNTIME

    def grant_authorization(self, authorization_token: str) -> None:
        """
        Grants explicit, single-use, per-run Project Owner authorization for model initialization.
        Requires state to be READY_FOR_AUTHORIZATION with all 6 gates passed.
        """
        if self._current_state != StartupState.READY_FOR_AUTHORIZATION:
            raise InvalidStateTransitionError(
                f"Cannot authorize model initialization from state {self._current_state.value}. "
                "Must be in READY_FOR_AUTHORIZATION."
            )
        if not self._last_preflight_passed:
            raise InvalidStateTransitionError(
                "Cannot authorize model initialization when preflight gates have failed."
            )
        if not authorization_token or not isinstance(authorization_token, str) or not authorization_token.strip():
            raise UnauthorizedInferenceError("Authorization token must be a non-empty string.")
        
        self._pending_authorization_token = authorization_token.strip()

    def transition_to(
        self,
        target_state: StartupState,
        reason: str = "",
        authorized: bool = False,
        preflight_passed: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> StartupState:
        """
        Executes a validated transition to the target state.
        Raises InvalidStateTransitionError or UnauthorizedInferenceError on violation.
        """
        if not isinstance(target_state, StartupState):
            raise InvalidStateTransitionError(f"Target state must be a StartupState enum, got {type(target_state)}: {target_state}")

        allowed = self.ALLOWED_TRANSITIONS.get(self._current_state, set())
        if target_state not in allowed:
            raise InvalidStateTransitionError(
                f"Illegal state transition from {self._current_state.value} to {target_state.value}. "
                f"Allowed target states: {[s.value for s in allowed]}"
            )

        # Gate check for READY_FOR_AUTHORIZATION
        if target_state == StartupState.READY_FOR_AUTHORIZATION:
            effective_preflight = preflight_passed if preflight_passed is not None else self._last_preflight_passed
            if not effective_preflight:
                raise InvalidStateTransitionError(
                    "Cannot enter READY_FOR_AUTHORIZATION when preflight gates have failed."
                )

        # Authorization check for MODEL_INITIALIZATION
        if target_state == StartupState.MODEL_INITIALIZATION:
            effective_auth = authorized or (self._pending_authorization_token is not None)
            if not effective_auth:
                raise UnauthorizedInferenceError(
                    "Model initialization strictly blocked: requires explicit Project Owner authorization."
                )
            if not self._last_preflight_passed and not (preflight_passed is True):
                raise UnauthorizedInferenceError(
                    "Model initialization strictly blocked: preflight admission gates are unmet."
                )
            # Consume authorization token (single-use guarantee)
            self._pending_authorization_token = None

        if preflight_passed is not None:
            self._last_preflight_passed = preflight_passed

        record = StateTransitionRecord(
            from_state=self._current_state,
            to_state=target_state,
            reason=reason,
            metadata=metadata or {}
        )
        self._history.append(record)
        self._current_state = target_state
        return self._current_state


class GuidedStartupController:
    """
    Lightweight startup controller for NOVA Compute.
    Allows startup under low RAM, measures resources, explains deficits,
    presents Memory Advisor recommendations, tracks controller process memory footprint (Checkpoints A-D),
    and rechecks admission gates on demand.
    Never spawns model inference or llama-cli.
    """

    def __init__(
        self,
        steward: Optional[Any] = None,
        input_fn: Optional[Callable[[str], str]] = None,
        output_fn: Optional[Callable[[str], None]] = None,
        state_machine: Optional[StartupStateMachine] = None,
        early_sample: Optional[ControllerMemorySample] = None
    ):
        from . import ResourceSteward
        self.steward = steward or ResourceSteward()
        self.input_fn = input_fn or input
        self.output_fn = output_fn or print
        self.state_machine = state_machine or StartupStateMachine()
        self.last_preflight_report: Optional[PreflightReport] = None

        # Controller Memory Profile (Phase 3B.2)
        self.memory_profile = ControllerMemoryProfile()
        
        # Checkpoint A: Earliest controller initialization
        cp_a = early_sample or self._sample_memory(
            "CHECKPOINT_A",
            "Earliest controller initialization in guided-start entrypoint"
        )
        self.memory_profile.checkpoints["CHECKPOINT_A"] = cp_a

        # Checkpoint B: After core controller initialization
        cp_b = self._sample_memory(
            "CHECKPOINT_B",
            "After core controller initialization"
        )
        self.memory_profile.checkpoints["CHECKPOINT_B"] = cp_b
        self.memory_profile.compute_deltas()

    def _sample_memory(self, checkpoint_id: str, description: str = "") -> ControllerMemorySample:
        """Safely samples controller memory via steward monitor or ResourceMonitor classmethod."""
        monitor = getattr(self.steward, "monitor", None)
        if monitor and hasattr(monitor, "sample_controller_memory"):
            return monitor.sample_controller_memory(checkpoint_id, description)
        return ResourceMonitor.sample_controller_memory(checkpoint_id, description)

    @property
    def current_state(self) -> StartupState:
        return self.state_machine.current_state

    @property
    def model_loaded(self) -> bool:
        return self.state_machine.model_loaded

    @property
    def inference_blocked(self) -> bool:
        return self.state_machine.inference_blocked

    def print_banner(self) -> None:
        self.output_fn("========================================================================")
        self.output_fn("NOVA COMPUTE — GUIDED STARTUP")
        self.output_fn("Startup Mode: LOW-MEMORY SAFE MODE")
        self.output_fn("Model: Qwen3-30B-A3B Q4_K_M")
        self.output_fn("Model State: NOT LOADED")
        self.output_fn("Inference: BLOCKED")
        self.output_fn("========================================================================")

    def evaluate_admission(self) -> bool:
        """
        Runs read-only preflight evaluation without auto-reclamation.
        Updates last_preflight_report and informs the state machine.
        """
        report = self.steward.run_preflight(run_auto_reclaim=False)
        self.last_preflight_report = report
        return report.passed

    def sample_idle_footprint(self, num_samples: int = 3) -> None:
        """Captures Checkpoint C idle samples while awaiting user action."""
        idle_samples = []
        for i in range(1, num_samples + 1):
            s = self._sample_memory(
                f"CHECKPOINT_C_IDLE_{i}",
                f"Idle waiting state sample {i}"
            )
            idle_samples.append(s)
        self.memory_profile.idle_samples = idle_samples
        if idle_samples:
            self.memory_profile.checkpoints["CHECKPOINT_C"] = idle_samples[0]
        self.memory_profile.compute_deltas()

    def start(self) -> StartupState:
        """
        Initializes lightweight safe mode and evaluates initial admission.
        Transitions: BOOTSTRAPPING -> SAFE_STARTUP -> PREFLIGHT_RUNNING -> (READY | PREFLIGHT_FAILED -> LOW_MEMORY_WAIT).
        """
        self.print_banner()
        self.state_machine.transition_to(
            StartupState.SAFE_STARTUP,
            reason="Lightweight controller initialized in safe mode"
        )

        self.state_machine.transition_to(
            StartupState.PREFLIGHT_RUNNING,
            reason="Evaluating initial hardware admission gates"
        )

        passed = self.evaluate_admission()

        if passed:
            self.state_machine.transition_to(
                StartupState.READY_FOR_AUTHORIZATION,
                reason="All six admission gates passed",
                preflight_passed=True
            )
            self._display_qualified_status()
            self.sample_idle_footprint()
            return self.state_machine.current_state
        else:
            self.state_machine.transition_to(
                StartupState.PREFLIGHT_FAILED,
                reason="Admission gates unmet",
                preflight_passed=False
            )
            self._display_failure_guidance()
            self.state_machine.transition_to(
                StartupState.LOW_MEMORY_WAIT,
                reason="Awaiting user memory recovery or recheck"
            )
            self.sample_idle_footprint()
            return self.state_machine.current_state

    def recheck(self) -> StartupState:
        """
        Re-measures all 6 hardware gates after user action.
        Transitions: LOW_MEMORY_WAIT -> RESOURCE_RECOVERY -> PREFLIGHT_RUNNING -> (READY | PREFLIGHT_FAILED -> LOW_MEMORY_WAIT).
        Captures Checkpoint D post-recheck footprint.
        """
        if self.state_machine.current_state != StartupState.LOW_MEMORY_WAIT:
            # If in READY_FOR_AUTHORIZATION or other state, allow rechecking by transitioning safely
            if self.state_machine.current_state == StartupState.READY_FOR_AUTHORIZATION:
                self.state_machine.transition_to(
                    StartupState.RESOURCE_RECOVERY,
                    reason="User requested recheck while in ready state"
                )
            else:
                raise InvalidStateTransitionError(
                    f"Cannot recheck memory from state {self.state_machine.current_state.value}. Must be in LOW_MEMORY_WAIT."
                )
        else:
            self.state_machine.transition_to(
                StartupState.RESOURCE_RECOVERY,
                reason="User initiated memory recheck"
            )

        self.output_fn("\n[Recheck] Collecting fresh telemetry for all 6 admission gates...")
        self.state_machine.transition_to(
            StartupState.PREFLIGHT_RUNNING,
            reason="Evaluating refreshed admission telemetry"
        )

        passed = self.evaluate_admission()

        # Checkpoint D: After user-requested recheck telemetry collection
        cp_d = self._sample_memory(
            "CHECKPOINT_D",
            "After user-requested recheck telemetry collection"
        )
        self.memory_profile.checkpoints["CHECKPOINT_D"] = cp_d
        self.memory_profile.compute_deltas()

        if passed:
            self.state_machine.transition_to(
                StartupState.READY_FOR_AUTHORIZATION,
                reason="All six admission gates passed on recheck",
                preflight_passed=True
            )
            self._display_qualified_status()
            return self.state_machine.current_state
        else:
            self.state_machine.transition_to(
                StartupState.PREFLIGHT_FAILED,
                reason="Admission gates unmet on recheck",
                preflight_passed=False
            )
            self._display_failure_guidance()
            self.state_machine.transition_to(
                StartupState.LOW_MEMORY_WAIT,
                reason="Awaiting user memory recovery or recheck"
            )
            return self.state_machine.current_state

    def _display_qualified_status(self) -> None:
        report = self.last_preflight_report
        ram_str = f"{report.system_ram.avail_phys_gib:.2f} GiB" if report else "UNKNOWN"
        vram_str = f"{report.gpu.vram_free_mib:.1f} MiB" if report and report.gpu else "UNKNOWN"
        commit_str = f"{report.system_ram.commit_pct:.1f}%" if report else "UNKNOWN"
        disk_str = f"{report.disk.free_gib:.2f} GiB" if report and report.disk else "UNKNOWN"

        self.output_fn("\n========================================================================")
        self.output_fn("NOVA COMPUTE — SYSTEM QUALIFIED")
        self.output_fn("HARDWARE QUALIFICATION AUDIT: PASSED")
        self.output_fn("========================================================================")
        self.output_fn("Status: ALL ADMISSION GATES PASSED")
        self.output_fn("All 6 admission gates PASSED. System is qualified for Configuration D smoke test.")
        self.output_fn("")
        self.output_fn(f"Available RAM: {ram_str}")
        self.output_fn(f"Free VRAM: {vram_str}")
        self.output_fn(f"System Commit: {commit_str}")
        self.output_fn(f"Free Disk: {disk_str}")
        self.output_fn("Competing GPU Processes: ZERO")
        self.output_fn("")
        self.output_fn("Model: NOT LOADED")
        self.output_fn("Smoke Test: READY FOR AUTHORIZATION")
        self.output_fn("========================================================================")
        self.output_fn("")
        self.output_fn("Exact Configuration D Command:")
        self.output_fn("llama-cli.exe -m Qwen_Qwen3-30B-A3B-Q4_K_M.gguf -ngl 48 -ncmoe 40 -c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap -n 1 --simple-io --perf -p \"Hello\"")
        self.output_fn("")
        self.output_fn("Runtime Safety Summary:")
        self.output_fn("- Target Binary: runtime\\windows\\llama-cpp\\llama-cli.exe")
        self.output_fn("- Target Model:  models\\qwen3-30b\\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf (~18.25 GiB)")
        self.output_fn("- Parameters:    -ngl 48 -ncmoe 40 -c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap -n 1")
        self.output_fn("- Constraints:   1 token limit | Zero expert cache | Clean text I/O")
        self.output_fn("- Supervisor:    Abort on VRAM < 512 MiB, RAM < 2 GiB for >= 5s, Commit >= 90.0%")
        self.output_fn("- Silence Rule:  30s console silence is WARNING ONLY (never terminates)")
        self.output_fn("")
        self.output_fn("CRITICAL SAFETY GOVERNANCE:")
        self.output_fn("Inference state: BLOCKED UNTIL AUTHORIZED")
        self.output_fn("A successful preflight NEVER automatically launches model inference.")
        self.output_fn("Waiting for separate Project Owner authorization before initiating model loading.")
        self.output_fn("========================================================================")

    def _display_failure_guidance(self) -> None:
        report = self.last_preflight_report
        if not report:
            return

        self.output_fn("\n========================================================================")
        self.output_fn("NOVA COMPUTE — PREFLIGHT EVALUATION: GATES UNMET")
        self.output_fn("========================================================================")

        # Audit gate summary
        for gate_name, gate in report.gates.items():
            status_tag = "[PASS]" if gate.passed else "[FAIL]"
            self.output_fn(f"  {status_tag} {gate_name:<24}: {gate.measured_str} (Req: {gate.required_str})")
            if not gate.passed and gate.failure_reason:
                self.output_fn(f"         Reason: {gate.failure_reason}")
        self.output_fn("------------------------------------------------------------------------")

        # Specific RAM Gate Handling — strictly distinguish RAM from Disk
        ram_gate = report.gates.get("avail_ram")
        if ram_gate and not ram_gate.passed:
            ram = report.system_ram
            req_ram = self.steward.policy.minimum_inference_ram_gib
            cur_ram = ram.avail_phys_gib
            deficit = max(0.0, round(req_ram - cur_ram, 2))

            self.output_fn("\n[!] Available physical RAM is below the required threshold. Close unused applications to make more memory available.")
            self.output_fn(f"Available RAM: {cur_ram:.2f} GiB")
            self.output_fn(f"Current available RAM: {cur_ram:.2f} GiB")
            self.output_fn(f"Required RAM: {req_ram:.2f} GiB")
            self.output_fn(f"RAM Deficit: {deficit:.2f} GiB")
            self.output_fn(f"Additional available RAM required: {deficit:.2f} GiB")
            self.output_fn("Next Action: Free memory and request another measurement.")
            self.output_fn("")
            self.output_fn("NOVA's lightweight controller is running in safe mode.")
            self.output_fn("The Qwen3-30B-A3B model (18.25 GiB) has NOT been loaded into memory.")
            self.output_fn("Inference execution is strictly blocked.")
            self.output_fn("\nSuggested actions:")
            self.output_fn("1. Save work and close unused browser tabs/windows.")
            self.output_fn("2. Close unused IDE projects.")
            self.output_fn("3. Exit unused development tools through their normal interfaces.")
            self.output_fn("4. Review other recognized user applications.")

            if report.recommendations:
                self.output_fn("\nTop candidate applications for manual closure:")
                for rec in report.recommendations[:5]:
                    pids_str = f"PID {rec.pids[0]}" if len(rec.pids) == 1 else f"{len(rec.pids)} instances"
                    priv_str = f", Private Commit: {rec.total_private_commit_mb:.1f} MB" if rec.total_private_commit_mb is not None else ", Private Commit: UNKNOWN"
                    self.output_fn(f"  - {rec.process_name} ({pids_str}): Working Set: {rec.observed_working_set_mb:.1f} MB{priv_str}")
                    reason_text = rec.why_recommended or "High resident memory consumer"
                    action_text = rec.action_guidance or rec.recommendation_text or "Save work and close through normal interface if unused."
                    self.output_fn(f"    Why: {reason_text}")
                    self.output_fn(f"    Action: {action_text}")

            self.output_fn("\nImportant telemetry interpretation:")
            self.output_fn("- Working set represents resident physical memory at measurement time.")
            self.output_fn("- Private commit represents virtual committed memory.")
            self.output_fn("- Do not present private commit as guaranteed recoverable physical RAM.")
            self.output_fn("- Closing an application may not immediately release its entire measured working set.")
            self.output_fn("- Re-measure after closing applications.")

        # Specific Disk Gate Handling — separate storage warning
        disk_gate = report.gates.get("disk_free")
        if disk_gate and not disk_gate.passed:
            self.output_fn(f"\n[!] Storage admission gate failed: Free disk space is below the required threshold.")
            self.output_fn(f"    Required: {disk_gate.required_str}, Measured: {disk_gate.measured_str}")
            self.output_fn("    Please free storage space on the model drive. Never delete files automatically.")

        # Specific Commit Gate Handling
        commit_gate = report.gates.get("system_commit")
        if commit_gate and not commit_gate.passed:
            self.output_fn(f"\n[!] System commit gate failed: System commit limit threshold exceeded.")
            self.output_fn(f"    Required: {commit_gate.required_str}, Measured: {commit_gate.measured_str}")

        # Specific VRAM Gate Handling
        vram_gate = report.gates.get("avail_vram")
        if vram_gate and not vram_gate.passed:
            self.output_fn(f"\n[!] VRAM admission gate failed: Available VRAM is below required threshold.")
            self.output_fn(f"    Required: {vram_gate.required_str}, Measured: {vram_gate.measured_str}")

        # Competing GPU Compute Gate Handling
        gpu_procs_gate = report.gates.get("no_competing_gpu")
        if gpu_procs_gate and not gpu_procs_gate.passed:
            self.output_fn(f"\n[!] Competing GPU compute processes detected:")
            self.output_fn(f"    Required: {gpu_procs_gate.required_str}, Measured: {gpu_procs_gate.measured_str}")

        # Telemetry Integrity Gate Handling
        telemetry_gate = report.gates.get("telemetry_integrity")
        if telemetry_gate and not telemetry_gate.passed:
            self.output_fn(f"\n[!] Telemetry integrity gate failed: sensor error detected.")
            self.output_fn(f"    Measured: {telemetry_gate.measured_str}")

    def run_interactive(self, max_iterations: Optional[int] = None) -> int:
        """
        Interactive guided startup loop.
        Prompt user for manual recovery and explicit recheck.
        """
        state = self.start()
        iterations = 0

        while self.state_machine.current_state == StartupState.LOW_MEMORY_WAIT:
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1

            try:
                user_choice = self.input_fn(
                    "\n[Recheck Memory] Press Enter or 'R' to recheck all admission gates, or 'Q' to exit: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                self.output_fn("\nInterrupted by user.")
                user_choice = "q"

            if user_choice in ("q", "quit", "exit"):
                self.state_machine.transition_to(
                    StartupState.ABORTED,
                    reason="User requested exit from guided startup"
                )
                self.output_fn("Exiting guided startup. Safe shutdown complete.")
                return 0
            elif user_choice in ("", "r", "recheck"):
                self.recheck()
            else:
                self.output_fn(f"Unrecognized option: '{user_choice}'. Enter 'R' to recheck memory or 'Q' to quit.")

        if self.state_machine.current_state == StartupState.READY_FOR_AUTHORIZATION:
            self.output_fn("\nNOVA Compute is now in READY state. Inference will not run without explicit authorization.")
            return 0
        elif self.state_machine.current_state == StartupState.ABORTED:
            return 0

        return 0

    def format_memory_profile_markdown(self) -> str:
        """Renders the controller memory footprint report in markdown."""
        return self.memory_profile.format_profile_markdown()

    def save_memory_profile_report(self, output_path: str) -> None:
        """Saves the memory profile markdown report to disk."""
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        content = self.format_memory_profile_markdown()
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
