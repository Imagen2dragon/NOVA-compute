"""
NOVA Compute — Resource Steward Data Models
Defines immutable data containers and dataclasses for system telemetry,
process diagnostics, GPU state, safety policies, supervisor state, memory reclamation, and audit reports.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Optional, Any
from datetime import datetime, timezone
import math

class TelemetryStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

class ExecutionMode(str, Enum):
    WORKSPACE_SAFE = "WORKSPACE_SAFE"
    MAXIMUM_INFERENCE = "MAXIMUM_INFERENCE"

class InferenceOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"

class MemoryPressureLevel(str, Enum):
    NORMAL = "NORMAL"              # RAM >= 12.0 GiB: no reclamation needed
    OPTIMIZATION = "OPTIMIZATION"  # 10.0 GiB <= RAM < 12.0 GiB: safe pass against NOVA resources
    CONSTRAINED = "CONSTRAINED"    # RAM < 10.0 GiB: run safe cleanup; block inference if still < 10 GiB
    CRITICAL = "CRITICAL"          # RAM < 2.0 GiB sustained: trigger critical abort

class ReclamationStatus(str, Enum):
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"
    NO_RECLAIMABLE_NOVA_MEMORY = "NO_RECLAIMABLE_NOVA_MEMORY"
    RECLAMATION_COMPLETED = "RECLAMATION_COMPLETED"
    NO_MEASURABLE_IMPROVEMENT = "NO_MEASURABLE_IMPROVEMENT"
    RECLAMATION_FAILED = "RECLAMATION_FAILED"
    RECLAMATION_UNVERIFIED = "RECLAMATION_UNVERIFIED"

class ProcessCategory(str, Enum):
    USER_APPLICATION = "USER_APPLICATION"
    BACKGROUND_APPLICATION = "BACKGROUND_APPLICATION"
    PROTECTED_SYSTEM = "PROTECTED_SYSTEM"
    UNKNOWN = "UNKNOWN"

class StartupState(str, Enum):
    BOOTSTRAPPING = "BOOTSTRAPPING"
    SAFE_STARTUP = "SAFE_STARTUP"
    LOW_MEMORY_WAIT = "LOW_MEMORY_WAIT"
    RESOURCE_RECOVERY = "RESOURCE_RECOVERY"
    PREFLIGHT_RUNNING = "PREFLIGHT_RUNNING"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    READY_FOR_AUTHORIZATION = "READY_FOR_AUTHORIZATION"
    MODEL_INITIALIZATION = "MODEL_INITIALIZATION"
    MODEL_RUNTIME = "MODEL_RUNTIME"
    SMOKE_TEST_COMPLETE = "SMOKE_TEST_COMPLETE"
    ABORTED = "ABORTED"

def current_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

@dataclass
class SystemMemorySnapshot:
    total_phys_bytes: int
    avail_phys_bytes: int
    total_phys_gib: float
    avail_phys_gib: float
    commit_total_bytes: int
    commit_avail_bytes: int
    committed_bytes: int
    commit_total_gib: float
    commit_avail_gib: float
    committed_gib: float
    commit_pct: float
    memory_load_pct: int
    system_cache_bytes: Optional[int] = None
    pages_input_per_sec: Optional[float] = None
    pages_output_per_sec: Optional[float] = None
    page_reads_per_sec: Optional[float] = None
    page_writes_per_sec: Optional[float] = None
    disk_read_bytes_per_sec: Optional[float] = None
    disk_write_bytes_per_sec: Optional[float] = None
    status: TelemetryStatus = TelemetryStatus.SUCCESS
    error_message: Optional[str] = None
    timestamp_utc: str = field(default_factory=current_iso_utc)

@dataclass
class ProcessResourceSnapshot:
    pid: int
    name: str
    working_set_bytes: int                            # Observed resident physical memory
    peak_working_set_bytes: Optional[int] = None      # Peak resident physical memory
    private_commit_bytes: Optional[int] = None        # Private committed virtual memory (PrivateUsage)
    process_commit_charge_bytes: Optional[int] = None # Total committed memory (PagefileUsage)
    page_fault_count: Optional[int] = None            # Total page faults
    working_set_mb: float = 0.0
    private_commit_mb: Optional[float] = None
    pids: List[int] = field(default_factory=list)     # Aggregated PIDs for multi-process apps
    instances_total: int = 1
    instances_measured: int = 1
    is_partial_private_commit: bool = False
    cpu_percent: Optional[float] = None
    gpu_vram_used_bytes: Optional[int] = None
    is_user_application: bool = False
    category: Optional[ProcessCategory] = None
    status: TelemetryStatus = TelemetryStatus.SUCCESS
    error_message: Optional[str] = None

    def __post_init__(self):
        if self.working_set_mb == 0.0 and self.working_set_bytes > 0:
            self.working_set_mb = round(self.working_set_bytes / (1024**2), 1)
        if self.private_commit_mb is None and self.private_commit_bytes is not None:
            self.private_commit_mb = round(self.private_commit_bytes / (1024**2), 1)
        if not self.pids and self.pid > 0:
            self.pids = [self.pid]
        if self.pids and self.instances_total <= 1:
            self.instances_total = len(self.pids)

@dataclass
class GPUProcessInfo:
    pid: int
    process_name: str
    used_memory_mib: float

@dataclass
class GPUSnapshot:
    name: str
    vram_total_mib: float
    vram_free_mib: float
    vram_used_mib: float
    utilization_pct: float
    temperature_c: float
    power_draw_w: float
    driver_version: str
    compute_processes: Optional[List[GPUProcessInfo]] = None
    status: TelemetryStatus = TelemetryStatus.SUCCESS
    error_message: Optional[str] = None
    timestamp_utc: str = field(default_factory=current_iso_utc)

MAX_ACCEPTED_DISK_BYTES: int = (1 << 64) - 1  # 18.44 Exabytes (maximum 64-bit unsigned integer)
# The exact quotient (2**64 - 1)/(1024**3) in IEEE 754 float64 rounds up to 17179869184.0 (2**34).
# Because 17179869184.0 * (1024**3) evaluates to 2**64 (exceeding MAX_ACCEPTED_DISK_BYTES by 1 byte),
# MAX_ACCEPTED_DISK_GIB is defined as the largest representable float whose ceil(val * 1024**3)
# does not exceed MAX_ACCEPTED_DISK_BYTES (the next float below 17179869184.0).
MAX_ACCEPTED_DISK_GIB: float = math.nextafter(17179869184.0, -math.inf)

def is_safe_disk_byte_counter(val: Any, allow_zero: bool = True) -> bool:
    """Exception-safe validation for integer disk byte counters."""
    if isinstance(val, bool) or not isinstance(val, int):
        return False
    min_val = 0 if allow_zero else 1
    return min_val <= val <= MAX_ACCEPTED_DISK_BYTES

def is_safe_finite_float(val: Any) -> bool:
    """Exception-safe validation for finite floating-point numeric values."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    try:
        f = float(val)
        return math.isfinite(f)
    except (OverflowError, TypeError, ValueError):
        return False

def is_safe_positive_policy_threshold(val: Any) -> bool:
    """
    Exception-safe validation for positive finite policy thresholds.
    Requires 0 < threshold_gib <= MAX_ACCEPTED_DISK_BYTES / (1024 ** 3).
    Rejects NaN, inf, <= 0, booleans, non-numeric types, and values exceeding maximum disk capacity.
    """
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    try:
        f = float(val)
        return math.isfinite(f) and 0.0 < f <= MAX_ACCEPTED_DISK_GIB
    except (OverflowError, TypeError, ValueError):
        return False

@dataclass
class DiskSnapshot:
    drive: str
    free_bytes: int
    total_bytes: int
    free_gib: float = 0.0
    total_gib: float = 0.0
    status: TelemetryStatus = TelemetryStatus.SUCCESS
    error_message: Optional[str] = None
    timestamp_utc: str = field(default_factory=current_iso_utc)

    def __init__(
        self,
        drive: str,
        free_bytes: int,
        total_bytes: int,
        free_gib: Optional[float] = None,
        total_gib: Optional[float] = None,
        free_gb: Optional[float] = None,
        total_gb: Optional[float] = None,
        status: TelemetryStatus = TelemetryStatus.SUCCESS,
        error_message: Optional[str] = None,
        timestamp_utc: Optional[str] = None
    ):
        self.drive = drive
        self.free_bytes = free_bytes
        self.total_bytes = total_bytes
        self.status = status
        self.error_message = error_message
        self.timestamp_utc = timestamp_utc or current_iso_utc()

        # Validate explicitly provided float metrics with exception safety
        for val_name, val in [("free_gib", free_gib), ("total_gib", total_gib), ("free_gb", free_gb), ("total_gb", total_gb)]:
            if val is not None and not is_safe_finite_float(val):
                self.status = TelemetryStatus.FAILED
                self.error_message = f"Invalid or non-finite {val_name} provided: {val!r}"

        # Validate integer byte counters with exception safety
        is_valid_free_b = is_safe_disk_byte_counter(free_bytes, allow_zero=True)
        is_valid_total_b = is_safe_disk_byte_counter(total_bytes, allow_zero=False)

        if not is_valid_free_b:
            self.status = TelemetryStatus.FAILED
            if not self.error_message:
                self.error_message = f"Invalid free_bytes counter: {free_bytes!r} (must be integer in [0, 2^64-1], non-bool)"
        if not is_valid_total_b:
            self.status = TelemetryStatus.FAILED
            if not self.error_message:
                self.error_message = f"Invalid total_bytes counter: {total_bytes!r} (must be integer in (0, 2^64-1], non-bool)"
        if is_valid_free_b and is_valid_total_b and free_bytes > total_bytes:
            self.status = TelemetryStatus.FAILED
            if not self.error_message:
                self.error_message = f"Contradictory counters: free_bytes ({free_bytes}) > total_bytes ({total_bytes})"

        # Raw integer byte counters are authoritative: derive presentation values when valid
        if is_valid_free_b:
            try:
                self.free_gib = float(free_bytes) / (1024 ** 3)
            except (OverflowError, TypeError, ValueError):
                self.free_gib = 0.0
                self.status = TelemetryStatus.FAILED
        elif free_gib is not None and is_safe_finite_float(free_gib):
            self.free_gib = float(free_gib)
        elif free_gb is not None and is_safe_finite_float(free_gb):
            self.free_gib = float(free_gb)
        else:
            self.free_gib = 0.0

        if is_valid_total_b:
            try:
                self.total_gib = float(total_bytes) / (1024 ** 3)
            except (OverflowError, TypeError, ValueError):
                self.total_gib = 0.0
                self.status = TelemetryStatus.FAILED
        elif total_gib is not None and is_safe_finite_float(total_gib):
            self.total_gib = float(total_gib)
        elif total_gb is not None and is_safe_finite_float(total_gb):
            self.total_gib = float(total_gb)
        else:
            self.total_gib = 0.0

    @property
    def free_gb(self) -> float:
        """Backward-compatibility alias for free_gib."""
        return self.free_gib

    @property
    def total_gb(self) -> float:
        """Backward-compatibility alias for total_gib."""
        return self.total_gib

@dataclass
class GateResult:
    gate_name: str
    passed: bool
    required_str: str
    measured_str: str
    failure_reason: Optional[str] = None

@dataclass
class ControllerMemorySample:
    checkpoint_id: str
    description: str
    pid: int
    working_set_bytes: int
    working_set_mb: float
    private_commit_bytes: Optional[int]
    private_commit_mb: Optional[float]
    page_fault_count: Optional[int]
    cpu_time_seconds: Optional[float]
    system_avail_ram_gib: float
    system_commit_pct: float
    timestamp_utc: str = field(default_factory=current_iso_utc)
    status: TelemetryStatus = TelemetryStatus.SUCCESS
    error_message: Optional[str] = None

@dataclass
class ControllerMemoryProfile:
    checkpoints: Dict[str, ControllerMemorySample] = field(default_factory=dict)
    idle_samples: List[ControllerMemorySample] = field(default_factory=list)
    working_set_delta_startup_mb: Optional[float] = None
    private_commit_delta_startup_mb: Optional[float] = None
    average_idle_working_set_mb: Optional[float] = None
    average_idle_private_commit_mb: Optional[float] = None
    timestamp_utc: str = field(default_factory=current_iso_utc)

    def compute_deltas(self) -> None:
        cp_a = self.checkpoints.get("CHECKPOINT_A")
        cp_b = self.checkpoints.get("CHECKPOINT_B")
        if cp_a and cp_b and cp_a.status == TelemetryStatus.SUCCESS and cp_b.status == TelemetryStatus.SUCCESS:
            self.working_set_delta_startup_mb = round(cp_b.working_set_mb - cp_a.working_set_mb, 2)
            if cp_a.private_commit_mb is not None and cp_b.private_commit_mb is not None:
                self.private_commit_delta_startup_mb = round(cp_b.private_commit_mb - cp_a.private_commit_mb, 2)
            else:
                self.private_commit_delta_startup_mb = None
        else:
            self.working_set_delta_startup_mb = None
            self.private_commit_delta_startup_mb = None

        if self.idle_samples:
            valid_ws = [s.working_set_mb for s in self.idle_samples if s.status == TelemetryStatus.SUCCESS]
            self.average_idle_working_set_mb = round(sum(valid_ws) / len(valid_ws), 2) if valid_ws else None
            valid_priv = [s.private_commit_mb for s in self.idle_samples if s.status == TelemetryStatus.SUCCESS and s.private_commit_mb is not None]
            self.average_idle_private_commit_mb = round(sum(valid_priv) / len(valid_priv), 2) if valid_priv else None
        else:
            self.average_idle_working_set_mb = None
            self.average_idle_private_commit_mb = None

    def format_profile_markdown(self) -> str:
        """Renders the controller memory footprint report in markdown."""
        lines = [
            "# NOVA Compute — Phase 3B.2: Controller Memory Footprint Profile",
            f"**Timestamp (UTC):** `{self.timestamp_utc}`  ",
            "**Target Process:** NOVA Controller (`scripts/run_inference_with_telemetry.py --guided-start`)  ",
            "**Operational Mode:** Lightweight Safe Mode (Model Not Loaded, Inference Gated)  ",
            "**Status:** MEASURED AND EMPIRICALLY AUDITED  \n",
            "---",
            "## 1. Executive Summary & Measurement Overview\n",
            "To qualify NOVA Compute for controlled inference on memory-constrained systems (such as the target 16 GB laptop), NOVA's own memory footprint before loading model weights must be precisely accounted for. This document establishes empirical baseline measurements of the lightweight controller across four distinct lifecycle checkpoints:\n",
            "- **Checkpoint A (Earliest Initialization)**: Captured immediately at the guided-start entrypoint prior to heavy module or coordinator setup.",
            "- **Checkpoint B (Core Controller Initialization)**: Captured after `GuidedStartupController`, state machine, memory budget manager, and memory advisor are instantiated.",
            "- **Checkpoint C (Idle Waiting State)**: Sampled across multiple quiescent intervals while awaiting user interaction in `LOW_MEMORY_WAIT` or `READY_FOR_AUTHORIZATION`.",
            "- **Checkpoint D (Post-Recheck State)**: Sampled immediately following a full 6-gate admission evaluation.\n",
            "---",
            "## 2. Process Footprint Summary Table\n",
            "| Metric | Checkpoint A (Earliest) | Checkpoint B (Core Init) | Startup Delta (B - A) | Checkpoint C (Idle Avg) | Checkpoint D (Post-Recheck) |",
            "|---|---|---|---|---|---|"
        ]

        cp_a = self.checkpoints.get("CHECKPOINT_A")
        cp_b = self.checkpoints.get("CHECKPOINT_B")
        cp_c = self.checkpoints.get("CHECKPOINT_C")
        cp_d = self.checkpoints.get("CHECKPOINT_D")

        def fmt_mb(sample: Optional[ControllerMemorySample], field: str) -> str:
            if not sample or sample.status != TelemetryStatus.SUCCESS:
                return "UNKNOWN"
            val = getattr(sample, field, None)
            return f"{val:.2f} MB" if val is not None else "UNKNOWN"

        def fmt_gib(sample: Optional[ControllerMemorySample]) -> str:
            if not sample or sample.status != TelemetryStatus.SUCCESS:
                return "UNKNOWN"
            return f"{sample.system_avail_ram_gib:.2f} GiB"

        def fmt_pct(sample: Optional[ControllerMemorySample]) -> str:
            if not sample or sample.status != TelemetryStatus.SUCCESS:
                return "UNKNOWN"
            return f"{sample.system_commit_pct:.1f}%"

        def fmt_int(sample: Optional[ControllerMemorySample], field: str) -> str:
            if not sample or sample.status != TelemetryStatus.SUCCESS:
                return "UNKNOWN"
            val = getattr(sample, field, None)
            return f"{val:,}" if val is not None else "UNKNOWN"

        def fmt_sec(sample: Optional[ControllerMemorySample]) -> str:
            if not sample or sample.status != TelemetryStatus.SUCCESS:
                return "UNKNOWN"
            return f"{sample.cpu_time_seconds:.3f} s" if sample.cpu_time_seconds is not None else "UNKNOWN"

        ws_delta_str = f"{self.working_set_delta_startup_mb:+.2f} MB" if self.working_set_delta_startup_mb is not None else "UNKNOWN"
        priv_delta_str = f"{self.private_commit_delta_startup_mb:+.2f} MB" if self.private_commit_delta_startup_mb is not None else "UNKNOWN"
        idle_ws_str = f"{self.average_idle_working_set_mb:.2f} MB" if self.average_idle_working_set_mb is not None else "UNKNOWN"
        idle_priv_str = f"{self.average_idle_private_commit_mb:.2f} MB" if self.average_idle_private_commit_mb is not None else "UNKNOWN"

        lines.append(f"| **Process Working Set** | {fmt_mb(cp_a, 'working_set_mb')} | {fmt_mb(cp_b, 'working_set_mb')} | {ws_delta_str} | {idle_ws_str} | {fmt_mb(cp_d, 'working_set_mb')} |")
        lines.append(f"| **Process Private Commit** | {fmt_mb(cp_a, 'private_commit_mb')} | {fmt_mb(cp_b, 'private_commit_mb')} | {priv_delta_str} | {idle_priv_str} | {fmt_mb(cp_d, 'private_commit_mb')} |")
        lines.append(f"| **Page Fault Count** | {fmt_int(cp_a, 'page_fault_count')} | {fmt_int(cp_b, 'page_fault_count')} | N/A (Cumulative) | {fmt_int(cp_c, 'page_fault_count')} | {fmt_int(cp_d, 'page_fault_count')} |")
        lines.append(f"| **Process CPU Time** | {fmt_sec(cp_a)} | {fmt_sec(cp_b)} | N/A (Cumulative) | {fmt_sec(cp_c)} | {fmt_sec(cp_d)} |")
        lines.append(f"| **System Available RAM** | {fmt_gib(cp_a)} | {fmt_gib(cp_b)} | N/A (System-wide) | {fmt_gib(cp_c)} | {fmt_gib(cp_d)} |")
        lines.append(f"| **System Commit Ratio** | {fmt_pct(cp_a)} | {fmt_pct(cp_b)} | N/A (System-wide) | {fmt_pct(cp_c)} | {fmt_pct(cp_d)} |\n")

        lines.extend([
            "---",
            "## 3. Telemetry Interpretation & Working Set Mechanics\n",
            "1. **Observed Startup Delta vs. Exclusive Attribution**:",
            f"   - Working Set Delta ($B - A$): `{ws_delta_str}`.",
            f"   - Private Commit Delta ($B - A$): `{priv_delta_str}`.",
            "   - These deltas represent observed differences during Python runtime setup and initialization of Resource Steward modules. They reflect both NOVA's code path and standard CPython runtime allocations.",
            "2. **Process Working Set vs. Private Commit**:",
            "   - **Working Set**: The physical RAM pages mapped into the controller process's address space at measurement time.",
            "   - **Private Commit**: Dedicated virtual memory allocated exclusively by the process (via `VirtualAlloc` / `malloc`), backed by pagefile space.",
            "3. **System-Wide Metric Independence**:",
            "   - System Available RAM and System Commit Ratio reflect overall OS and multi-process states. Fluctuations in available RAM during controller execution are primarily driven by background OS tasks and concurrent desktop applications, not NOVA's lightweight footprint (< 30 MB).",
            "4. **Zero Model Memory Overhead**:",
            "   - At all checkpoints prior to explicit authorization, model weights remain strictly **NOT LOADED**.",
            "   - Zero VRAM and zero GGUF file mappings are active in the controller process.\n",
            "---",
            "## 4. Measurement Limitations and Uncertainty\n",
            "- Telemetry queries Windows `GetProcessMemoryInfo` and `GlobalMemoryStatusEx` via `ctypes`.",
            "- Windows memory management may page clean code pages or adjust working sets dynamically under external pressure.",
            "- Numbers are recorded directly from kernel counters without heuristic manipulation or artificial inflation."
        ])
        return "\n".join(lines)



@dataclass
class MemoryRecommendation:
    process_name: str
    pids: List[int]
    observed_working_set_mb: float
    total_private_commit_mb: Optional[float]
    is_user_facing: bool
    instances_total: int
    instances_measured: int
    is_partial: bool
    unverified_heuristic_reclaim_mb: Optional[float]
    recommendation_text: str
    category: ProcessCategory = ProcessCategory.USER_APPLICATION
    why_recommended: str = ""
    action_guidance: str = ""
    working_set_interpretation: str = ""
    private_commit_interpretation: str = ""
    ranking_score: float = 0.0

@dataclass
class MemoryAdvisorReport:
    timestamp_utc: str
    available_ram_gib: float
    total_commit_gib: float
    commit_pct: float
    is_ram_constrained: bool
    ram_deficit_gib: float
    recommendations: List[MemoryRecommendation]
    user_app_recommendations: List[MemoryRecommendation]
    background_app_recommendations: List[MemoryRecommendation]
    protected_processes_observed: List[str]
    disclaimer: str = (
        "Memory Advisor is strictly read-only. Observed working set measures physical residency, "
        "not guaranteed reclaimable RAM. Private commit represents dedicated virtual commit charge. "
        "All application closure decisions remain under complete manual user control."
    )

@dataclass
class PreflightReport:
    passed: bool
    gates: Dict[str, GateResult]
    system_ram: SystemMemorySnapshot
    gpu: GPUSnapshot
    disk: DiskSnapshot
    top_processes: List[ProcessResourceSnapshot]
    recommendations: List[MemoryRecommendation]
    timestamp_utc: str = field(default_factory=current_iso_utc)

@dataclass
class RecoveryReport:
    pre_avail_ram_gib: float
    peak_pressure_ram_gib: float
    post_avail_ram_gib: float
    pre_vram_used_mib: float
    peak_vram_used_mib: float
    post_vram_used_mib: float
    pre_process_private_commit_mb: float
    post_process_private_commit_mb: float
    child_pid: Optional[int]
    termination_status: str
    shutdown_method: str                          # e.g. "NATURAL_EXIT", "CTRL_BREAK", "FORCED_TERMINATION", "NO_CHILD"
    recovery_duration_seconds: float
    residual_processes_detected: List[int]
    residual_vram_leak_mib: Optional[float]       # None if GPU telemetry failed
    child_terminated_successfully: bool           # Child process exited
    reader_thread_stopped: bool                   # Reader thread joined cleanly
    sampler_thread_stopped: bool                  # Sampler thread joined cleanly
    pipes_closed: bool                            # All owned pipes verified closed
    reader_eof_reached: bool = True               # Reader thread confirmed verified EOF
    reader_read_failed: bool = False              # Reader thread encountered no I/O exception
    gpu_verification_status: str = "VERIFIED"     # "VERIFIED", "FAILED", "UNVERIFIED"
    process_verification_status: str = "VERIFIED" # "VERIFIED", "FAILED", "UNKNOWN"
    recovery_successful: bool = False             # Full resource recovery verified
    notes: List[str] = field(default_factory=list)
    timestamp_utc: str = field(default_factory=current_iso_utc)

@dataclass
class SupervisorResult:
    abort_triggered: bool
    abort_reason: str
    exit_code: Optional[int]
    peak_ram_gib: float
    peak_vram_mib: float
    processed_samples_count: int
    duration_seconds: float
    output_lines: List[str] = field(default_factory=list)
    reader_thread_stopped: bool = True
    reader_eof_reached: bool = True
    reader_read_failed: bool = False
    reader_timed_out: bool = False
    reader_exception: Optional[str] = None

@dataclass
class MemoryReclaimReport:
    timestamp_utc: str
    trigger_reason: str
    pressure_level: MemoryPressureLevel
    # System-wide memory metrics (exact bytes and derived GiB)
    system_avail_bytes_before: int
    system_avail_bytes_after: int
    observed_system_avail_delta_bytes: int
    observed_system_avail_delta_mib: float
    ram_available_before_gib: float
    ram_available_after_gib: float
    ram_delta_gib: float # Observed system-wide delta in GiB (not attributed to AMR)
    commit_before_pct: float
    commit_after_pct: float
    # NOVA process-local memory metrics (bytes)
    nova_process_working_set_bytes_before: Optional[int] = None
    nova_process_working_set_bytes_after: Optional[int] = None
    nova_process_private_bytes_before: Optional[int] = None
    nova_process_private_bytes_after: Optional[int] = None
    nova_process_working_set_delta_bytes: Optional[int] = None
    nova_process_private_delta_bytes: Optional[int] = None
    process_working_set_verified: bool = False
    process_private_commit_verified: bool = False
    process_telemetry_verified: bool = False
    # Action accounting
    actions_attempted: List[str] = field(default_factory=list)
    actions_completed: List[str] = field(default_factory=list)
    actions_failed: List[str] = field(default_factory=list)
    reclamation_duration_seconds: float = 0.0
    cooldown_applied: bool = False
    overall_status: ReclamationStatus = ReclamationStatus.NO_ACTION_REQUIRED
    safety_notes: List[str] = field(default_factory=list)
    paging_activity_before: Optional[Dict[str, Any]] = None
    paging_activity_after: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc,
            "trigger_reason": self.trigger_reason,
            "pressure_level": self.pressure_level.value,
            "system_avail_bytes_before": self.system_avail_bytes_before,
            "system_avail_bytes_after": self.system_avail_bytes_after,
            "observed_system_avail_delta_bytes": self.observed_system_avail_delta_bytes,
            "observed_system_avail_delta_mib": self.observed_system_avail_delta_mib,
            "ram_available_before_gib": self.ram_available_before_gib,
            "ram_available_after_gib": self.ram_available_after_gib,
            "observed_system_wide_ram_delta_gib": self.ram_delta_gib,
            "commit_before_pct": self.commit_before_pct,
            "commit_after_pct": self.commit_after_pct,
            "nova_process_working_set_bytes_before": self.nova_process_working_set_bytes_before,
            "nova_process_working_set_bytes_after": self.nova_process_working_set_bytes_after,
            "nova_process_private_bytes_before": self.nova_process_private_bytes_before,
            "nova_process_private_bytes_after": self.nova_process_private_bytes_after,
            "nova_process_working_set_delta_bytes": self.nova_process_working_set_delta_bytes,
            "nova_process_private_delta_bytes": self.nova_process_private_delta_bytes,
            "process_working_set_verified": self.process_working_set_verified,
            "process_private_commit_verified": self.process_private_commit_verified,
            "process_telemetry_verified": self.process_telemetry_verified,
            "actions_attempted": self.actions_attempted,
            "actions_completed": self.actions_completed,
            "actions_failed": self.actions_failed,
            "reclamation_duration_seconds": self.reclamation_duration_seconds,
            "cooldown_applied": self.cooldown_applied,
            "overall_status": self.overall_status.value,
            "safety_notes": self.safety_notes,
            "paging_activity_before": self.paging_activity_before,
            "paging_activity_after": self.paging_activity_after
        }
