"""
NOVA Compute — Memory Budget Manager
Evaluates hardware state snapshots against configurable resource policies.
Enforces strict fail-closed gating for memory, GPU, storage, commit, and telemetry health.
"""

import math
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
from .policy import ResourcePolicy, DEFAULT_POLICY, enforce_mandatory_admission_floors

class MemoryBudgetManager:
    """Evaluates resource availability and enforces preflight safety gates."""

    def __init__(self, policy: Optional[ResourcePolicy] = None):
        self.policy = enforce_mandatory_admission_floors(policy)

    def evaluate_gates(
        self,
        system_ram: SystemMemorySnapshot,
        gpu: GPUSnapshot,
        disk: DiskSnapshot
    ) -> Tuple[bool, Dict[str, GateResult]]:
        """
        Evaluates preflight gates with strict fail-closed semantics.
        If any telemetry metric is unverified, malformed, or failed, the gate fails.
        Re-applies mandatory project admission floors immediately prior to evaluating thresholds
        to prevent object mutation from weakening admission gates.
        """
        effective_policy = enforce_mandatory_admission_floors(self.policy)
        gates: Dict[str, GateResult] = {}

        # 1. Telemetry Health Gate (Fail-Closed)
        disk_telemetry_healthy = (
            disk.status == TelemetryStatus.SUCCESS and
            isinstance(disk.free_bytes, int) and not isinstance(disk.free_bytes, bool) and disk.free_bytes >= 0 and
            isinstance(disk.total_bytes, int) and not isinstance(disk.total_bytes, bool) and disk.total_bytes > 0 and
            disk.free_bytes <= disk.total_bytes and
            disk.free_gib is not None and isinstance(disk.free_gib, (int, float)) and not isinstance(disk.free_gib, bool) and math.isfinite(disk.free_gib) and
            disk.total_gib is not None and isinstance(disk.total_gib, (int, float)) and not isinstance(disk.total_gib, bool) and math.isfinite(disk.total_gib)
        )
        telemetry_healthy = (
            system_ram.status == TelemetryStatus.SUCCESS and
            gpu.status == TelemetryStatus.SUCCESS and
            disk_telemetry_healthy
        )
        telemetry_failures = []
        if system_ram.status != TelemetryStatus.SUCCESS:
            telemetry_failures.append(f"RAM: {system_ram.error_message or 'Unavailable'}")
        if gpu.status != TelemetryStatus.SUCCESS:
            telemetry_failures.append(f"GPU: {gpu.error_message or 'Unavailable'}")
        if not disk_telemetry_healthy:
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
            system_ram.avail_phys_gib >= effective_policy.minimum_inference_ram_gib
        )
        ram_deficit = round(effective_policy.minimum_inference_ram_gib - system_ram.avail_phys_gib, 3)
        gates["avail_ram"] = GateResult(
            gate_name="avail_ram",
            passed=ram_passed,
            required_str=f">= {effective_policy.minimum_inference_ram_gib:.1f} GiB",
            measured_str=f"{system_ram.avail_phys_gib:.3f} GiB" if system_ram.status == TelemetryStatus.SUCCESS else "UNAVAILABLE",
            failure_reason=f"Available RAM deficit: {ram_deficit:.3f} GiB below required {effective_policy.minimum_inference_ram_gib:.1f} GiB" if not ram_passed else None
        )

        # 3. Hardened System Commit Gate (Fail-Closed)
        commit_valid = False
        commit_invalid_reason = None

        if system_ram.status != TelemetryStatus.SUCCESS:
            commit_invalid_reason = "System memory telemetry status is not SUCCESS."
        elif system_ram.commit_total_bytes <= 0:
            commit_invalid_reason = f"Invalid commit limit: {system_ram.commit_total_bytes} bytes (must be > 0)."
        elif system_ram.commit_avail_bytes < 0:
            commit_invalid_reason = f"Invalid negative available commit: {system_ram.commit_avail_bytes} bytes."
        elif system_ram.committed_bytes < 0:
            commit_invalid_reason = f"Invalid negative committed bytes: {system_ram.committed_bytes} bytes."
        elif system_ram.committed_bytes > system_ram.commit_total_bytes:
            commit_invalid_reason = (
                f"Contradictory commit metrics: committed bytes ({system_ram.committed_bytes}) "
                f"exceed commit limit ({system_ram.commit_total_bytes})."
            )
        elif system_ram.commit_pct is None or math.isnan(system_ram.commit_pct) or math.isinf(system_ram.commit_pct):
            commit_invalid_reason = f"Invalid commit percentage: {system_ram.commit_pct}."
        elif not (0.0 <= system_ram.commit_pct <= 100.0):
            commit_invalid_reason = f"Commit percentage out of range [0, 100]: {system_ram.commit_pct}%."
        else:
            # Consistency check 1: committed_bytes + commit_avail_bytes == commit_total_bytes within 1 MiB / 0.1% tolerance
            byte_sum = system_ram.committed_bytes + system_ram.commit_avail_bytes
            sum_diff = abs(byte_sum - system_ram.commit_total_bytes)
            allowed_tolerance = max(1024 * 1024, int(0.001 * system_ram.commit_total_bytes))
            if sum_diff > allowed_tolerance:
                commit_invalid_reason = (
                    f"Contradictory commit metrics: committed + avail ({byte_sum}) != "
                    f"total ({system_ram.commit_total_bytes}), delta={sum_diff} bytes > allowed tolerance ({allowed_tolerance} bytes)."
                )
            else:
                # Consistency check 2: commit_pct agrees with calculated byte percentage within 1.0%
                calc_pct = (system_ram.committed_bytes / system_ram.commit_total_bytes) * 100.0
                pct_diff = abs(system_ram.commit_pct - calc_pct)
                if pct_diff > 1.0:
                    commit_invalid_reason = (
                        f"Contradictory commit percentage: reported {system_ram.commit_pct:.2f}% "
                        f"disagrees with calculated {calc_pct:.2f}% (delta={pct_diff:.2f}% > allowed 1.0%)."
                    )
                else:
                    commit_valid = True

        if not commit_valid:
            commit_passed = False
            commit_measured = "INVALID / UNAVAILABLE"
            commit_failure = f"Fail-closed: commit validation failed ({commit_invalid_reason})."
        else:
            commit_passed = (system_ram.commit_pct < effective_policy.critical_commit_pct)
            commit_measured = f"{system_ram.commit_pct:.1f}%"
            commit_failure = (
                f"System commit {system_ram.commit_pct:.1f}% exceeds critical threshold (< {effective_policy.critical_commit_pct:.1f}%)."
                if not commit_passed else None
            )

        gates["system_commit"] = GateResult(
            gate_name="system_commit",
            passed=commit_passed,
            required_str=f"< {effective_policy.critical_commit_pct:.1f}%",
            measured_str=commit_measured,
            failure_reason=commit_failure
        )

        # 4. Available VRAM Gate
        vram_passed = (
            gpu.status == TelemetryStatus.SUCCESS and
            gpu.vram_free_mib >= effective_policy.minimum_vram_mib
        )
        vram_deficit = round(effective_policy.minimum_vram_mib - gpu.vram_free_mib, 1)
        gates["avail_vram"] = GateResult(
            gate_name="avail_vram",
            passed=vram_passed,
            required_str=f">= {effective_policy.minimum_vram_mib:.0f} MiB",
            measured_str=f"{gpu.vram_free_mib:.1f} MiB" if gpu.status == TelemetryStatus.SUCCESS else "UNAVAILABLE",
            failure_reason=f"VRAM deficit: {vram_deficit:.1f} MiB below required {effective_policy.minimum_vram_mib:.0f} MiB" if not vram_passed else None
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

        # 6. Free Storage Gate (Raw Integer Byte Counters Authoritative)
        disk_valid = True
        disk_invalid_reason = None

        if disk.status != TelemetryStatus.SUCCESS:
            disk_valid = False
            disk_invalid_reason = f"Disk telemetry status is not SUCCESS ({disk.error_message or 'Unavailable'})."
        elif not isinstance(disk.free_bytes, int) or isinstance(disk.free_bytes, bool) or disk.free_bytes < 0:
            disk_valid = False
            disk_invalid_reason = f"Invalid free_bytes counter: {disk.free_bytes!r} (must be non-negative integer, non-bool)."
        elif not isinstance(disk.total_bytes, int) or isinstance(disk.total_bytes, bool) or disk.total_bytes <= 0:
            disk_valid = False
            disk_invalid_reason = f"Invalid total_bytes counter: {disk.total_bytes!r} (must be positive integer, non-bool)."
        elif disk.free_bytes > disk.total_bytes:
            disk_valid = False
            disk_invalid_reason = f"Contradictory disk counters: free_bytes ({disk.free_bytes}) > total_bytes ({disk.total_bytes})."
        elif disk.free_gib is None or not isinstance(disk.free_gib, (int, float)) or isinstance(disk.free_gib, bool) or not math.isfinite(disk.free_gib):
            disk_valid = False
            disk_invalid_reason = f"Invalid or non-finite free_gib telemetry: {disk.free_gib!r}."
        elif disk.total_gib is None or not isinstance(disk.total_gib, (int, float)) or isinstance(disk.total_gib, bool) or not math.isfinite(disk.total_gib):
            disk_valid = False
            disk_invalid_reason = f"Invalid or non-finite total_gib telemetry: {disk.total_gib!r}."
        else:
            # Reject conflicting representations: free_bytes and free_gib must be mathematically consistent
            expected_free_gib = float(disk.free_bytes) / (1024 ** 3)
            if abs(disk.free_gib - expected_free_gib) > 0.001:
                disk_valid = False
                disk_invalid_reason = (
                    f"Contradictory disk representations: free_bytes ({disk.free_bytes}) yields "
                    f"{expected_free_gib:.4f} GiB, but free_gib is {disk.free_gib:.4f}."
                )

        if not disk_valid:
            disk_passed = False
            disk_measured = "UNAVAILABLE" if disk.status != TelemetryStatus.SUCCESS else "INVALID / UNAVAILABLE"
            disk_failure = f"Fail-closed: disk validation failed ({disk_invalid_reason})"
        else:
            required_bytes = math.ceil(effective_policy.minimum_disk_free_gib * (1024 ** 3))
            # Authoritative admission decision derived strictly from integer free_bytes
            disk_passed = (disk.free_bytes >= required_bytes)

            byte_deficit = max(0, required_bytes - disk.free_bytes)
            gib_deficit = byte_deficit / (1024 ** 3)
            deficit_rounded = round(gib_deficit, 2)
            deficit_str = f"{deficit_rounded:.2f} GiB" if deficit_rounded > 0 else "< 0.01 GiB"

            disk_measured = f"{disk.free_gib:.2f} GiB"
            disk_failure = (
                f"Disk space deficit: {deficit_str} below required {effective_policy.minimum_disk_free_gib:.1f} GiB"
                if not disk_passed else None
            )

        gates["disk_free"] = GateResult(
            gate_name="disk_free",
            passed=disk_passed,
            required_str=f">= {effective_policy.minimum_disk_free_gib:.1f} GiB",
            measured_str=disk_measured,
            failure_reason=disk_failure
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
