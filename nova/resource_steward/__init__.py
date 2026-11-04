"""
NOVA Compute — Resource Steward Package
Provides resource-aware system diagnostics, memory budget policies,
fail-closed preflight gating, supervisor engines, and safe reclamation managers.
"""

from typing import Optional

from .models import (
    TelemetryStatus,
    ExecutionMode,
    InferenceOutcome,
    MemoryPressureLevel,
    ReclamationStatus,
    MemoryReclaimReport,
    SystemMemorySnapshot,
    ProcessResourceSnapshot,
    GPUSnapshot,
    GPUProcessInfo,
    DiskSnapshot,
    GateResult,
    MemoryRecommendation,
    PreflightReport,
    RecoveryReport,
    SupervisorResult
)

from .policy import (
    ResourcePolicy,
    DEFAULT_POLICY
)

from .monitor import (
    ResourceMonitor,
    LiveTelemetrySampler,
    WindowsPDHCollector
)

from .memory_budget import (
    MemoryBudgetManager
)

from .advisor import (
    MemoryAdvisor
)

from .recovery import (
    SafeReclamationManager
)

from .supervisor import (
    supervise_child_process
)

from .adaptive_memory import (
    MemoryPressureDetector,
    ReclamationCooldownManager,
    SafeMemoryReclaimer,
    AdaptiveMemoryController
)

class ResourceSteward:
    """Unified coordinator for resource diagnostics, preflight auditing, safe reclamation, and adaptive memory control."""

    def __init__(self, policy: ResourcePolicy = None, reports_dir: Optional[str] = None):
        self.policy = policy or DEFAULT_POLICY
        self.reports_dir = reports_dir
        self.budget_mgr = MemoryBudgetManager(self.policy)
        self.monitor = ResourceMonitor()
        self.advisor = MemoryAdvisor()
        self.reclamation = SafeReclamationManager()
        self.adaptive_reclaimer = AdaptiveMemoryController(
            policy=self.policy,
            monitor=self.monitor,
            reports_dir=self.reports_dir
        )

    def run_preflight(self, run_auto_reclaim: bool = True) -> PreflightReport:
        """Collects fresh hardware metrics and runs fail-closed preflight evaluation."""
        if run_auto_reclaim and self.policy.auto_reclaim_enabled:
            # Run SAFE_AUTO admission check before final preflight gates
            self.adaptive_reclaimer.evaluate_and_reclaim(trigger_reason="Preflight admission check")

        ram = self.monitor.get_system_memory()
        gpu = self.monitor.get_gpu_telemetry()
        disk = self.monitor.get_disk_space("C:\\")
        top_procs = self.monitor.list_top_processes(limit=15)
        
        all_passed, gates = self.budget_mgr.evaluate_gates(ram, gpu, disk)
        
        deficit = round(self.policy.minimum_inference_ram_gib - ram.avail_phys_gib, 3) if not gates["avail_ram"].passed else 0.0
        recommendations = self.advisor.analyze_processes(top_procs, ram_deficit_gib=max(0.0, deficit))
        
        return PreflightReport(
            passed=all_passed,
            gates=gates,
            system_ram=ram,
            gpu=gpu,
            disk=disk,
            top_processes=top_procs,
            recommendations=recommendations
        )
