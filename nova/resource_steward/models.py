"""
NOVA Compute — Resource Steward Data Models
Defines immutable data containers and dataclasses for system telemetry,
process diagnostics, GPU state, safety policies, supervisor state, memory reclamation, and audit reports.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Optional, Any
from datetime import datetime, timezone

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
    status: TelemetryStatus = TelemetryStatus.SUCCESS
    error_message: Optional[str] = None

    def __post_init__(self):
        if self.working_set_mb == 0.0 and self.working_set_bytes > 0:
            self.working_set_mb = round(self.working_set_bytes / (1024**2), 1)
        if self.private_commit_mb is None and self.private_commit_bytes is not None:
            self.private_commit_mb = round(self.private_commit_bytes / (1024**2), 1)
        if not self.pids and self.pid > 0:
            self.pids = [self.pid]
        if self.pids:
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

@dataclass
class DiskSnapshot:
    drive: str
    free_bytes: int
    total_bytes: int
    free_gb: float
    total_gb: float
    status: TelemetryStatus = TelemetryStatus.SUCCESS
    error_message: Optional[str] = None
    timestamp_utc: str = field(default_factory=current_iso_utc)

@dataclass
class GateResult:
    gate_name: str
    passed: bool
    required_str: str
    measured_str: str
    failure_reason: Optional[str] = None

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
