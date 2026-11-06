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
    MemoryRecommendation,
    MAX_ACCEPTED_DISK_BYTES,
    MAX_ACCEPTED_DISK_GIB,
    is_safe_disk_byte_counter,
    is_safe_finite_float,
    is_safe_positive_policy_threshold
)
from .policy import ResourcePolicy, DEFAULT_POLICY, enforce_mandatory_admission_floors
from . import system_processes

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
        expected_free_gib = None
        if is_safe_disk_byte_counter(disk.free_bytes, allow_zero=True):
            try:
                expected_free_gib = float(disk.free_bytes) / (1024 ** 3)
            except (OverflowError, TypeError, ValueError):
                expected_free_gib = None

        expected_total_gib = None
        if is_safe_disk_byte_counter(disk.total_bytes, allow_zero=False):
            try:
                expected_total_gib = float(disk.total_bytes) / (1024 ** 3)
            except (OverflowError, TypeError, ValueError):
                expected_total_gib = None

        free_gib_consistent = (
            expected_free_gib is not None and
            is_safe_finite_float(disk.free_gib) and
            abs(disk.free_gib - expected_free_gib) <= 1e-9
        )
        total_gib_consistent = (
            expected_total_gib is not None and
            is_safe_finite_float(disk.total_gib) and
            abs(disk.total_gib - expected_total_gib) <= 1e-9
        )
        disk_telemetry_healthy = (
            disk.status == TelemetryStatus.SUCCESS and
            is_safe_disk_byte_counter(disk.free_bytes, allow_zero=True) and
            is_safe_disk_byte_counter(disk.total_bytes, allow_zero=False) and
            (disk.free_bytes <= disk.total_bytes if isinstance(disk.free_bytes, int) and isinstance(disk.total_bytes, int) else False) and
            free_gib_consistent and
            total_gib_consistent
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
            # Project Owner decision (2026-11-06): only the verified system dwm.exe is exempt.
            # Every other listed process, including unresolvable ones, counts as competing.
            exempt = [p for p in gpu.compute_processes
                      if system_processes.is_exempt_system_gpu_process(p.pid)]
            competing = [p for p in gpu.compute_processes if p not in exempt]
            comp_count = len(competing)
            gpu_comp_passed = (comp_count == 0)
            gpu_comp_measured = f"{comp_count} active apps"
            if exempt:
                gpu_comp_measured += f" ({len(exempt)} exempt: dwm.exe)"
            if gpu_comp_passed:
                gpu_comp_failure = None
            else:
                names = ", ".join(f"{p.process_name} (PID {p.pid})" for p in competing)
                gpu_comp_failure = f"Found {comp_count} competing GPU compute process(es): {names}"

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

        # Validate policy threshold before calculating required_bytes
        threshold_gib = effective_policy.minimum_disk_free_gib
        threshold_valid = is_safe_positive_policy_threshold(threshold_gib)

        if not threshold_valid:
            disk_valid = False
            if (
                isinstance(threshold_gib, (int, float)) and
                not isinstance(threshold_gib, bool) and
                math.isfinite(threshold_gib) and
                threshold_gib > 0
            ):
                disk_invalid_reason = (
                    f"Invalid policy disk threshold: {threshold_gib!r} "
                    f"exceeds maximum supported range (<= {MAX_ACCEPTED_DISK_GIB:.1f} GiB)."
                )
            else:
                disk_invalid_reason = f"Invalid non-finite or non-positive policy disk threshold: {threshold_gib!r}."
        elif disk.status != TelemetryStatus.SUCCESS:
            disk_valid = False
            disk_invalid_reason = f"Disk telemetry status is not SUCCESS ({disk.error_message or 'Unavailable'})."
        elif not is_safe_disk_byte_counter(disk.free_bytes, allow_zero=True):
            disk_valid = False
            disk_invalid_reason = f"Invalid free_bytes counter: {disk.free_bytes!r} (must be non-negative integer <= 2^64-1, non-bool)."
        elif not is_safe_disk_byte_counter(disk.total_bytes, allow_zero=False):
            disk_valid = False
            disk_invalid_reason = f"Invalid total_bytes counter: {disk.total_bytes!r} (must be positive integer <= 2^64-1, non-bool)."
        elif disk.free_bytes > disk.total_bytes:
            disk_valid = False
            disk_invalid_reason = f"Contradictory disk counters: free_bytes ({disk.free_bytes}) > total_bytes ({disk.total_bytes})."
        elif not is_safe_finite_float(disk.free_gib):
            disk_valid = False
            disk_invalid_reason = f"Invalid or non-finite free_gib telemetry: {disk.free_gib!r}."
        elif not is_safe_finite_float(disk.total_gib):
            disk_valid = False
            disk_invalid_reason = f"Invalid or non-finite total_gib telemetry: {disk.total_gib!r}."
        else:
            # Consistency check 1: free_gib must agree with free_bytes within narrow conversion precision
            expected_free_gib = float(disk.free_bytes) / (1024 ** 3)
            if abs(disk.free_gib - expected_free_gib) > 1e-9:
                disk_valid = False
                disk_invalid_reason = (
                    f"Contradictory free disk representations: free_bytes ({disk.free_bytes}) yields "
                    f"{expected_free_gib:.6f} GiB, but free_gib is {disk.free_gib:.6f}."
                )
            else:
                # Consistency check 2: total_gib must agree with total_bytes within narrow conversion precision
                expected_total_gib = float(disk.total_bytes) / (1024 ** 3)
                if abs(disk.total_gib - expected_total_gib) > 1e-9:
                    disk_valid = False
                    disk_invalid_reason = (
                        f"Contradictory total disk representations: total_bytes ({disk.total_bytes}) yields "
                        f"{expected_total_gib:.6f} GiB, but total_gib is {disk.total_gib:.6f}."
                    )

        if not disk_valid:
            disk_passed = False
            disk_measured = "UNAVAILABLE" if disk.status != TelemetryStatus.SUCCESS else "INVALID / UNAVAILABLE"
            disk_failure = f"Fail-closed: disk validation failed ({disk_invalid_reason})"
        else:
            required_bytes = math.ceil(threshold_gib * (1024 ** 3))
            if required_bytes > MAX_ACCEPTED_DISK_BYTES:
                disk_passed = False
                disk_measured = "INVALID / UNAVAILABLE"
                disk_failure = (
                    f"Fail-closed: calculated required disk bytes ({required_bytes}) "
                    f"exceeds maximum supported counter ({MAX_ACCEPTED_DISK_BYTES})"
                )
            else:
                # Authoritative admission decision derived strictly from integer free_bytes
                disk_passed = (disk.free_bytes >= required_bytes)

                byte_deficit = max(0, required_bytes - disk.free_bytes)
                gib_deficit = byte_deficit / (1024 ** 3)
                deficit_rounded = round(gib_deficit, 2)
                deficit_str = f"{deficit_rounded:.2f} GiB" if deficit_rounded > 0 else "< 0.01 GiB"

                disk_measured = f"{disk.free_gib:.2f} GiB"
                disk_failure = (
                    f"Disk space deficit: {deficit_str} below required {threshold_gib:.1f} GiB"
                    if not disk_passed else None
                )

        gates["disk_free"] = GateResult(
            gate_name="disk_free",
            passed=disk_passed,
            required_str=f">= {threshold_gib:.1f} GiB" if threshold_valid else "INVALID POLICY",
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
