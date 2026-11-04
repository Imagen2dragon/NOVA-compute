"""
NOVA Compute — Resource Steward Package
Provides resource-aware system diagnostics, memory budget policies,
fail-closed preflight gating, and safe reclamation managers.
"""

from .models import (
    TelemetryStatus,
    ExecutionMode,
    SystemMemorySnapshot,
    ProcessResourceSnapshot,
    GPUSnapshot,
    GPUProcessInfo,
    DiskSnapshot,
    GateResult,
    MemoryRecommendation,
    PreflightReport,
    RecoveryReport
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

class ResourceSteward:
    """Unified coordinator for resource diagnostics, preflight auditing, and safe reclamation."""

    def __init__(self, policy: ResourcePolicy = None):
        self.policy = policy or DEFAULT_POLICY
        self.budget_mgr = MemoryBudgetManager(self.policy)
        self.monitor = ResourceMonitor()
        self.advisor = MemoryAdvisor()
        self.reclamation = SafeReclamationManager()

    def run_preflight(self) -> PreflightReport:
        """Collects fresh hardware metrics and runs fail-closed preflight evaluation."""
        ram = self.monitor.get_system_memory()
        gpu = self.monitor.get_gpu_telemetry()
        disk = self.monitor.get_disk_space("C:\\")
        top_procs = self.monitor.list_top_processes(limit=15)
        
        deficit = max(0.0, self.policy.minimum_inference_ram_gib - ram.avail_phys_gib)
        recs = self.advisor.analyze_processes(top_procs, ram_deficit_gib=deficit)
        
        return self.budget_mgr.generate_preflight_report(
            system_ram=ram,
            gpu=gpu,
            disk=disk,
            top_processes=top_procs,
            recommendations=recs
        )

__all__ = [
    "ResourceSteward",
    "ResourcePolicy",
    "DEFAULT_POLICY",
    "ExecutionMode",
    "TelemetryStatus",
    "SystemMemorySnapshot",
    "ProcessResourceSnapshot",
    "GPUSnapshot",
    "GPUProcessInfo",
    "DiskSnapshot",
    "GateResult",
    "MemoryRecommendation",
    "PreflightReport",
    "RecoveryReport",
    "ResourceMonitor",
    "LiveTelemetrySampler",
    "WindowsPDHCollector",
    "MemoryBudgetManager",
    "MemoryAdvisor",
    "SafeReclamationManager",
]
