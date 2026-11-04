"""
NOVA Compute — Resource Steward Package
Provides resource-aware system diagnostics, memory budget policies,
fail-closed preflight gating, supervisor engines, and safe reclamation managers.
"""

from .models import (
    TelemetryStatus,
    ExecutionMode,
    InferenceOutcome,
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
