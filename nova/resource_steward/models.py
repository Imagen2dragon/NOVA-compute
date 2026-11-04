"""
NOVA Compute — Resource Steward Data Models
Defines immutable data containers and dataclasses for system telemetry,
process diagnostics, GPU state, safety policies, and audit reports.
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
    working_set_bytes: int          # Resident physical memory
    peak_working_set_bytes: int     # Peak resident physical memory
    private_commit_bytes: int       # Private committed virtual memory (PrivateUsage)
    process_commit_charge_bytes: int # Total committed memory by process (PagefileUsage)
    page_fault_count: int           # Total page faults
    working_set_mb: float = 0.0
    private_commit_mb: float = 0.0
    cpu_percent: Optional[float] = None
    gpu_vram_used_bytes: Optional[int] = None
    is_user_application: bool = False
    status: TelemetryStatus = TelemetryStatus.SUCCESS
    error_message: Optional[str] = None

    def __post_init__(self):
        if self.working_set_mb == 0.0 and self.working_set_bytes > 0:
            self.working_set_mb = round(self.working_set_bytes / (1024**2), 1)
        if self.private_commit_mb == 0.0 and self.private_commit_bytes > 0:
            self.private_commit_mb = round(self.private_commit_bytes / (1024**2), 1)

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
    total_working_set_mb: float
    total_private_commit_mb: float
    is_user_facing: bool
    estimated_reclaimable_ram_mb: float
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
    recovery_duration_seconds: float
    residual_processes_detected: List[int]
    residual_vram_leak_mib: float
    recovery_successful: bool
    notes: List[str] = field(default_factory=list)
    timestamp_utc: str = field(default_factory=current_iso_utc)
