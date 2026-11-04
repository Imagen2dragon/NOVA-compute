"""
NOVA Compute — Memory Budget Manager
Evaluates hardware state snapshots against configurable resource policies.
Enforces strict fail-closed gating for memory, GPU, storage, commit, and telemetry health.
"""

from typing import Dict, Tuple, List, Optional
from .models import (
    TelemetryStatus,
    SystemMemorySnapshot,
    GPUSnapshot,
    DiskSnapshot,
    GateResult,
    PreflightReport,
    ProcessResourceSnapshot,
    MemoryRecommendation
)
from .policy import ResourcePolicy, DEFAULT_POLICY

class MemoryBudgetManager:
    """Evaluates resource availability and enforces preflight safety gates."""

    def __init__(self, policy: Optional[ResourcePolicy] = None):
        self.policy = policy or DEFAULT_POLICY

    def evaluate_gates(
        self,
        system_ram: SystemMemorySnapshot,
        gpu: GPUSnapshot,
        disk: DiskSnapshot
    ) -> Tuple[bool, Dict[str, GateResult]]:
        """
        Evaluates preflight gates with strict fail-closed semantics.
        If any telemetry metric is unverified or failed, the gate fails.
        """
        gates: Dict[str, GateResult] = {}

        # 1. Telemetry Health Gate (Fail-Closed)
        telemetry_healthy = (
            system_ram.status == TelemetryStatus.SUCCESS and
            gpu.status == TelemetryStatus.SUCCESS and
            disk.status == TelemetryStatus.SUCCESS
        )
        telemetry_failures = []
        if system_ram.status != TelemetryStatus.SUCCESS:
            telemetry_failures.append(f"RAM: {system_ram.error_message or 'Unavailable'}")
        if gpu.status != TelemetryStatus.SUCCESS:
            telemetry_failures.append(f"GPU: {gpu.error_message or 'Unavailable'}")
        if disk.status != TelemetryStatus.SUCCESS:
            telemetry_failures.append(f"Disk: {disk.error_message or 'Unavailable'}")

        gates["telemetry_integrity"] = GateResult(
            gate_name="telemetry_integrity",
            passed=telemetry_healthy,
            required_str="All sensors SUCCESS",
            measured_str="Healthy" if telemetry_healthy else "; ".join(telemetry_failures),
            failure_reason="Telemetry failure detected. Fail-closed: cannot verify hardware safety." if not telemetry_healthy else None
        )

        # 2. Available RAM Gate
        ram_passed = (
            system_ram.status == TelemetryStatus.SUCCESS and
            system_ram.avail_phys_gib >= self.policy.minimum_inference_ram_gib
        )
        ram_deficit = round(self.policy.minimum_inference_ram_gib - system_ram.avail_phys_gib, 3)
        gates["avail_ram"] = GateResult(
            gate_name="avail_ram",
            passed=ram_passed,
            required_str=f">= {self.policy.minimum_inference_ram_gib:.1f} GiB",
            measured_str=f"{system_ram.avail_phys_gib:.3f} GiB" if system_ram.status == TelemetryStatus.SUCCESS else "UNAVAILABLE",
            failure_reason=f"Available RAM deficit: {ram_deficit:.3f} GiB below required {self.policy.minimum_inference_ram_gib:.1f} GiB" if not ram_passed else None
        )

        # 3. System Commit Gate (Fail-Closed)
        commit_valid = (
            system_ram.status == TelemetryStatus.SUCCESS and
            system_ram.commit_pct is not None
        )
        commit_passed = (
            commit_valid and
            system_ram.commit_pct < self.policy.critical_commit_pct
        )
        if not commit_valid:
            commit_measured = "UNAVAILABLE"
            commit_failure = "System commit telemetry missing or invalid. Fail-closed: cannot verify commit pressure."
        elif not commit_passed:
            commit_measured = f"{system_ram.commit_pct:.1f}%"
            commit_failure = f"System commit {system_ram.commit_pct:.1f}% exceeds critical threshold (< {self.policy.critical_commit_pct:.1f}%)."
        else:
            commit_measured = f"{system_ram.commit_pct:.1f}%"
            commit_failure = None

        gates["system_commit"] = GateResult(
            gate_name="system_commit",
            passed=commit_passed,
            required_str=f"< {self.policy.critical_commit_pct:.1f}%",
            measured_str=commit_measured,
            failure_reason=commit_failure
        )

        # 4. Available VRAM Gate
        vram_passed = (
            gpu.status == TelemetryStatus.SUCCESS and
            gpu.vram_free_mib >= self.policy.minimum_vram_mib
        )
        vram_deficit = round(self.policy.minimum_vram_mib - gpu.vram_free_mib, 1)
        gates["avail_vram"] = GateResult(
            gate_name="avail_vram",
            passed=vram_passed,
            required_str=f">= {self.policy.minimum_vram_mib:.0f} MiB",
            measured_str=f"{gpu.vram_free_mib:.1f} MiB" if gpu.status == TelemetryStatus.SUCCESS else "UNAVAILABLE",
            failure_reason=f"VRAM deficit: {vram_deficit:.1f} MiB below required {self.policy.minimum_vram_mib:.0f} MiB" if not vram_passed else None
        )

        # 5. Competing GPU Compute Workload Gate (Fail-Closed)
        if gpu.status != TelemetryStatus.SUCCESS or gpu.compute_processes is None:
            gpu_comp_passed = False
            gpu_comp_measured = "UNKNOWN (Query Failed)"
            gpu_comp_failure = "GPU compute process query failed or unavailable. Fail-closed: competing workloads cannot be ruled out."
        else:
            comp_count = len(gpu.compute_processes)
            gpu_comp_passed = (comp_count == 0)
            gpu_comp_measured = f"{comp_count} active apps"
            gpu_comp_failure = f"Found {comp_count} competing GPU compute process(es)" if not gpu_comp_passed else None

        gates["no_competing_gpu"] = GateResult(
            gate_name="no_competing_gpu",
            passed=gpu_comp_passed,
            required_str="0 competing compute apps",
            measured_str=gpu_comp_measured,
            failure_reason=gpu_comp_failure
        )

        # 6. Free Storage Gate
        disk_passed = (
            disk.status == TelemetryStatus.SUCCESS and
            disk.free_gb >= self.policy.minimum_disk_free_gib
        )
        disk_deficit = round(self.policy.minimum_disk_free_gib - disk.free_gb, 2)
        gates["disk_free"] = GateResult(
            gate_name="disk_free",
            passed=disk_passed,
            required_str=f">= {self.policy.minimum_disk_free_gib:.1f} GB",
            measured_str=f"{disk.free_gb:.2f} GB" if disk.status == TelemetryStatus.SUCCESS else "UNAVAILABLE",
            failure_reason=f"Disk space deficit: {disk_deficit:.2f} GB below required {self.policy.minimum_disk_free_gib:.1f} GB" if not disk_passed else None
        )

        all_passed = all(g.passed for g in gates.values())
        return all_passed, gates

    def generate_preflight_report(
        self,
        system_ram: SystemMemorySnapshot,
        gpu: GPUSnapshot,
        disk: DiskSnapshot,
        top_processes: List[ProcessResourceSnapshot],
        recommendations: List[MemoryRecommendation]
    ) -> PreflightReport:
        all_passed, gates = self.evaluate_gates(system_ram, gpu, disk)
        return PreflightReport(
            passed=all_passed,
            gates=gates,
            system_ram=system_ram,
            gpu=gpu,
            disk=disk,
            top_processes=top_processes,
            recommendations=recommendations
        )
