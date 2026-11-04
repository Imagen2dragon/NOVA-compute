"""
NOVA Compute — Adaptive Memory Reclaimer (AMR)
Inspired by open-source memory monitors (Mem Reduct, WinMemClean) under a strict SAFE_AUTO policy:
- Detects multi-tier system memory pressure (NORMAL, OPTIMIZATION, CONSTRAINED, CRITICAL).
- Throttles cleanup attempts via configurable cooldown intervals.
- Strictly reclaims ONLY NOVA-owned resources and Python runtime garbage (gc.collect).
- PROHIBITS system-wide cache purges (NtSetSystemInformation), working set flushing (EmptyWorkingSet),
  and foreign process termination.
- Validates pre- and post-reclamation telemetry and generates immutable audit reports.
- Distinguishes observed system-wide deltas from process-local memory demonstrably released by NOVA.
"""

import os
import gc
import json
import time
import math
from typing import Optional, List, Dict, Any, Tuple, Callable

from .models import (
    TelemetryStatus,
    MemoryPressureLevel,
    ReclamationStatus,
    MemoryReclaimReport,
    SystemMemorySnapshot,
    ProcessResourceSnapshot,
    current_iso_utc
)
from .policy import ResourcePolicy, DEFAULT_POLICY
from .monitor import ResourceMonitor

def _safe_int(val: Any) -> Optional[int]:
    if val is not None and isinstance(val, int) and not isinstance(val, bool):
        return val
    return None

def _is_valid_byte_counter(val: Any) -> bool:
    """Returns True if val is a valid non-boolean integer >= 0."""
    return val is not None and isinstance(val, int) and not isinstance(val, bool) and val >= 0

def _validate_process_snapshot(snapshot: Optional[ProcessResourceSnapshot], expected_pid: int) -> bool:
    """
    Validates process snapshot integrity before calculating memory deltas:
    1. Snapshot exists and is not None.
    2. Status is TelemetryStatus.SUCCESS.
    3. Snapshot PID matches expected NOVA runtime PID.
    4. working_set_bytes is a valid non-boolean integer >= 0, and not 0 (0 indicates failed fallback).
    """
    if snapshot is None:
        return False
    if snapshot.status != TelemetryStatus.SUCCESS:
        return False
    if snapshot.pid != expected_pid:
        return False
    if not _is_valid_byte_counter(snapshot.working_set_bytes) or snapshot.working_set_bytes == 0:
        return False
    return True

class MemoryPressureDetector:
    """Classifies system memory pressure into hierarchical operational tiers."""

    def __init__(self, policy: Optional[ResourcePolicy] = None):
        self.policy = policy or DEFAULT_POLICY

    def evaluate_pressure(self, snapshot: SystemMemorySnapshot) -> MemoryPressureLevel:
        """
        Determines memory pressure tier based on verified available physical RAM and commit status.
        Fail-closed: if telemetry failed, missing, boolean, NaN, inf, or negative, treated as CRITICAL.
        """
        if snapshot.status != TelemetryStatus.SUCCESS:
            return MemoryPressureLevel.CRITICAL

        avail_ram = snapshot.avail_phys_gib
        avail_bytes = snapshot.avail_phys_bytes
        commit_pct = snapshot.commit_pct

        # Validate numeric types and finite bounds for RAM
        if (avail_ram is None or isinstance(avail_ram, bool) or not isinstance(avail_ram, (int, float)) or
            math.isnan(avail_ram) or math.isinf(avail_ram) or avail_ram < 0):
            return MemoryPressureLevel.CRITICAL

        if (avail_bytes is None or isinstance(avail_bytes, bool) or not isinstance(avail_bytes, int) or
            avail_bytes < 0):
            return MemoryPressureLevel.CRITICAL

        # Commit percentage validation: MUST be present, non-bool numeric, finite, and in [0.0, 100.0]
        if (commit_pct is None or isinstance(commit_pct, bool) or not isinstance(commit_pct, (int, float)) or
            math.isnan(commit_pct) or math.isinf(commit_pct) or not (0.0 <= commit_pct <= 100.0)):
            return MemoryPressureLevel.CRITICAL

        # 1. Critical threshold: RAM < critical_ram_gib (e.g. 2.0 GiB) or commit >= critical_commit_pct (90%)
        if avail_ram < self.policy.critical_ram_gib or commit_pct >= self.policy.critical_commit_pct:
            return MemoryPressureLevel.CRITICAL

        # 2. Constrained threshold: RAM < minimum_inference_ram_gib (e.g. 10.0 GiB)
        if avail_ram < self.policy.minimum_inference_ram_gib:
            return MemoryPressureLevel.CONSTRAINED

        # 3. Optimization threshold: minimum_inference_ram_gib <= RAM < preferred_inference_ram_gib (10.0 - 12.0 GiB)
        if avail_ram < self.policy.preferred_inference_ram_gib:
            return MemoryPressureLevel.OPTIMIZATION

        # 4. Normal operational state: RAM >= preferred_inference_ram_gib (>= 12.0 GiB)
        return MemoryPressureLevel.NORMAL

class ReclamationCooldownManager:
    """Enforces cooldown windows between memory reclamation passes to prevent thrashing."""

    def __init__(
        self,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic
    ):
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._last_reclamation_monotonic: Optional[float] = None

    def can_reclaim(self, current_time: Optional[float] = None) -> bool:
        """Checks if enough monotonic time has elapsed since the last reclamation."""
        if self._last_reclamation_monotonic is None:
            return True
        now = current_time if current_time is not None else self.clock()
        return (now - self._last_reclamation_monotonic) >= self.cooldown_seconds

    def record_reclamation(self, current_time: Optional[float] = None) -> None:
        """Records the monotonic timestamp of a completed reclamation attempt."""
        self._last_reclamation_monotonic = current_time if current_time is not None else self.clock()

    def reset(self) -> None:
        """Clears the cooldown state."""
        self._last_reclamation_monotonic = None

    @property
    def remaining_cooldown_seconds(self) -> float:
        """Returns remaining seconds before another reclamation is permitted."""
        if self._last_reclamation_monotonic is None:
            return 0.0
        now = self.clock()
        elapsed = now - self._last_reclamation_monotonic
        return max(0.0, self.cooldown_seconds - elapsed)

class SafeMemoryReclaimer:
    """
    Executes safe memory reclamation strictly scoped to NOVA-owned resources.
    
    FORBIDDEN APIS & BEHAVIORS (STRICT SAFETY GOVERNANCE):
    - EmptyWorkingSet (K32EmptyWorkingSet) on foreign or system processes.
    - NtSetSystemInformation (MemoryPurgeStandbyList, MemoryFlushModifiedList).
    - SetSystemFileCacheSize.
    - Terminating or suspending third-party or system processes.
    - Modifying Windows pagefile or registry.
    """

    FORBIDDEN_IDENTIFIERS = [
        "EmptyWorkingSet",
        "K32EmptyWorkingSet",
        "NtSetSystemInformation",
        "MemoryPurgeStandbyList",
        "MemoryFlushModifiedList",
        "SetSystemFileCacheSize"
    ]

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock

    def reclaim(self) -> Tuple[List[str], List[str], List[str], float]:
        """
        Executes SAFE_AUTO memory reclamation.
        Currently performs process-local Python runtime garbage collection (gc.collect(2)).
        Returns: (actions_attempted, actions_completed, actions_failed, duration_seconds)
        """
        t_start = self.clock()
        attempted: List[str] = []
        completed: List[str] = []
        failed: List[str] = []

        # Action: Python full generational garbage collection (process-local only)
        attempted.append("python_gc_collect")
        try:
            collected = gc.collect(2)
            completed.append(f"python_gc_collect (freed {collected} unreachable runtime objects)")
        except Exception as e:
            failed.append(f"python_gc_collect: {e}")

        dur = round(self.clock() - t_start, 4)
        return attempted, completed, failed, dur

class AdaptiveMemoryController:
    """
    Coordinates pressure detection, cooldown management, safe reclamation,
    and before/after telemetry verification.
    """

    def __init__(
        self,
        policy: Optional[ResourcePolicy] = None,
        monitor: Optional[ResourceMonitor] = None,
        cooldown_mgr: Optional[ReclamationCooldownManager] = None,
        reclaimer: Optional[SafeMemoryReclaimer] = None,
        reports_dir: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic
    ):
        self.policy = policy or DEFAULT_POLICY
        self.monitor = monitor or ResourceMonitor()
        self.clock = clock
        self.detector = MemoryPressureDetector(self.policy)
        self.cooldown_mgr = cooldown_mgr or ReclamationCooldownManager(
            cooldown_seconds=self.policy.reclamation_cooldown_seconds,
            clock=self.clock
        )
        self.reclaimer = reclaimer or SafeMemoryReclaimer(clock=self.clock)
        default_reports_dir = os.environ.get(
            "NOVA_AMR_REPORT_DIR",
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "reports", "phase2c", "memory_reclamation"
            )
        )
        self.reports_dir = reports_dir or default_reports_dir

    def evaluate_and_reclaim(
        self,
        trigger_reason: str = "Preflight memory pressure check",
        force: bool = False
    ) -> MemoryReclaimReport:
        """
        Runs a complete evaluation and safe reclamation pass.
        1. Measure pre-reclamation system and process-local telemetry.
        2. Evaluate pressure level.
        3. Check cooldown unless forced.
        4. Execute SAFE_AUTO reclamation if pressure indicates.
        5. Measure post-reclamation system and process-local telemetry.
        6. Compute verified deltas and log report safely.
        """
        pid_to_check = os.getpid()
        timestamp_utc = current_iso_utc()
        safety_notes: List[str] = [
            "Policy: SAFE_AUTO (Strictly NOVA-owned resources only).",
            "Zero system-wide standby/modified list purging.",
            "Zero foreign process working-set trimming.",
            "Zero unprivileged/privileged system modifications.",
            "Observed system-wide RAM delta reflects uncoordinated host activity; only process-local deltas are attributable to NOVA.",
            "Do not infer freed bytes from Python object count."
        ]

        # 1. Pre-reclamation telemetry
        pre_ram = self.monitor.get_system_memory()
        pre_proc = None
        try:
            pre_proc = self.monitor.get_process_memory(pid_to_check)
        except Exception:
            pass

        if pre_ram.status != TelemetryStatus.SUCCESS:
            rep = MemoryReclaimReport(
                timestamp_utc=timestamp_utc,
                trigger_reason=trigger_reason,
                pressure_level=MemoryPressureLevel.CRITICAL,
                system_avail_bytes_before=0,
                system_avail_bytes_after=0,
                observed_system_avail_delta_bytes=0,
                observed_system_avail_delta_mib=0.0,
                ram_available_before_gib=0.0,
                ram_available_after_gib=0.0,
                ram_delta_gib=0.0,
                commit_before_pct=0.0,
                commit_after_pct=0.0,
                actions_attempted=[],
                actions_completed=[],
                actions_failed=["Pre-reclamation RAM telemetry collection failed"],
                reclamation_duration_seconds=0.0,
                cooldown_applied=False,
                overall_status=ReclamationStatus.RECLAMATION_FAILED,
                safety_notes=safety_notes + ["Fail-closed: Pre-reclamation telemetry unavailable."],
                process_working_set_verified=False,
                process_private_commit_verified=False,
                process_telemetry_verified=False
            )
            self.save_report(rep)
            return rep

        pressure = self.detector.evaluate_pressure(pre_ram)

        # 2. Check if reclamation is necessary (Normal RAM)
        if pressure == MemoryPressureLevel.NORMAL and not force:
            pre_ws_valid = _validate_process_snapshot(pre_proc, pid_to_check)
            pre_priv_valid = pre_ws_valid and _is_valid_byte_counter(pre_proc.private_commit_bytes)
            pre_telemetry_valid = pre_ws_valid and pre_priv_valid
            ws_bytes = pre_proc.working_set_bytes if pre_ws_valid else None
            priv_bytes = pre_proc.private_commit_bytes if pre_priv_valid else None
            rep = MemoryReclaimReport(
                timestamp_utc=timestamp_utc,
                trigger_reason=trigger_reason,
                pressure_level=pressure,
                system_avail_bytes_before=pre_ram.avail_phys_bytes,
                system_avail_bytes_after=pre_ram.avail_phys_bytes,
                observed_system_avail_delta_bytes=0,
                observed_system_avail_delta_mib=0.0,
                ram_available_before_gib=pre_ram.avail_phys_gib,
                ram_available_after_gib=pre_ram.avail_phys_gib,
                ram_delta_gib=0.0,
                commit_before_pct=pre_ram.commit_pct or 0.0,
                commit_after_pct=pre_ram.commit_pct or 0.0,
                nova_process_working_set_bytes_before=ws_bytes,
                nova_process_working_set_bytes_after=ws_bytes,
                nova_process_private_bytes_before=priv_bytes,
                nova_process_private_bytes_after=priv_bytes,
                nova_process_working_set_delta_bytes=0 if pre_ws_valid else None,
                nova_process_private_delta_bytes=0 if pre_priv_valid else None,
                process_working_set_verified=pre_ws_valid,
                process_private_commit_verified=pre_priv_valid,
                process_telemetry_verified=pre_telemetry_valid,
                actions_attempted=[],
                actions_completed=[],
                actions_failed=[],
                reclamation_duration_seconds=0.0,
                cooldown_applied=False,
                overall_status=ReclamationStatus.NO_ACTION_REQUIRED,
                safety_notes=safety_notes + [f"System RAM healthy ({pre_ram.avail_phys_gib:.2f} GiB >= {self.policy.preferred_inference_ram_gib:.1f} GiB). No action needed."]
            )
            self.save_report(rep)
            return rep

        # 3. Check cooldown
        if not self.cooldown_mgr.can_reclaim() and not force:
            remaining = self.cooldown_mgr.remaining_cooldown_seconds
            pre_ws_valid = _validate_process_snapshot(pre_proc, pid_to_check)
            pre_priv_valid = pre_ws_valid and _is_valid_byte_counter(pre_proc.private_commit_bytes)
            pre_telemetry_valid = pre_ws_valid and pre_priv_valid
            ws_bytes = pre_proc.working_set_bytes if pre_ws_valid else None
            priv_bytes = pre_proc.private_commit_bytes if pre_priv_valid else None
            rep = MemoryReclaimReport(
                timestamp_utc=timestamp_utc,
                trigger_reason=trigger_reason,
                pressure_level=pressure,
                system_avail_bytes_before=pre_ram.avail_phys_bytes,
                system_avail_bytes_after=pre_ram.avail_phys_bytes,
                observed_system_avail_delta_bytes=0,
                observed_system_avail_delta_mib=0.0,
                ram_available_before_gib=pre_ram.avail_phys_gib,
                ram_available_after_gib=pre_ram.avail_phys_gib,
                ram_delta_gib=0.0,
                commit_before_pct=pre_ram.commit_pct or 0.0,
                commit_after_pct=pre_ram.commit_pct or 0.0,
                nova_process_working_set_bytes_before=ws_bytes,
                nova_process_working_set_bytes_after=ws_bytes,
                nova_process_private_bytes_before=priv_bytes,
                nova_process_private_bytes_after=priv_bytes,
                nova_process_working_set_delta_bytes=0 if pre_ws_valid else None,
                nova_process_private_delta_bytes=0 if pre_priv_valid else None,
                process_working_set_verified=pre_ws_valid,
                process_private_commit_verified=pre_priv_valid,
                process_telemetry_verified=pre_telemetry_valid,
                actions_attempted=[],
                actions_completed=[],
                actions_failed=[],
                reclamation_duration_seconds=0.0,
                cooldown_applied=True,
                overall_status=ReclamationStatus.NO_ACTION_REQUIRED,
                safety_notes=safety_notes + [f"Reclamation throttled by cooldown ({remaining:.1f}s remaining)."]
            )
            self.save_report(rep)
            return rep

        # 4. Execute safe reclamation
        attempted, completed, failed, duration = self.reclaimer.reclaim()
        self.cooldown_mgr.record_reclamation()

        # 5. Post-reclamation telemetry
        post_proc = None
        try:
            post_proc = self.monitor.get_process_memory(pid_to_check)
        except Exception:
            pass
        post_ram = self.monitor.get_system_memory()

        if post_ram.status != TelemetryStatus.SUCCESS:
            rep = MemoryReclaimReport(
                timestamp_utc=timestamp_utc,
                trigger_reason=trigger_reason,
                pressure_level=pressure,
                system_avail_bytes_before=pre_ram.avail_phys_bytes,
                system_avail_bytes_after=0,
                observed_system_avail_delta_bytes=0,
                observed_system_avail_delta_mib=0.0,
                ram_available_before_gib=pre_ram.avail_phys_gib,
                ram_available_after_gib=0.0,
                ram_delta_gib=0.0,
                commit_before_pct=pre_ram.commit_pct or 0.0,
                commit_after_pct=0.0,
                actions_attempted=attempted,
                actions_completed=completed,
                actions_failed=failed + ["Post-reclamation RAM telemetry collection failed"],
                reclamation_duration_seconds=duration,
                cooldown_applied=False,
                overall_status=ReclamationStatus.RECLAMATION_UNVERIFIED,
                safety_notes=safety_notes + ["Post-reclamation verification failed. Fail-closed: outcome unverified."],
                process_working_set_verified=False,
                process_private_commit_verified=False,
                process_telemetry_verified=False
            )
            self.save_report(rep)
            return rep

        # System-wide memory delta calculation (exact bytes)
        sys_delta_bytes = post_ram.avail_phys_bytes - pre_ram.avail_phys_bytes
        sys_delta_mib = round(sys_delta_bytes / (1024 * 1024), 2)
        ram_delta_gib = round(sys_delta_bytes / (1024**3), 4)

        # Process-local memory verification
        process_ws_verified = False
        process_priv_verified = False
        process_telemetry_verified = False

        pre_ws_valid = _validate_process_snapshot(pre_proc, pid_to_check)
        post_ws_valid = _validate_process_snapshot(post_proc, pid_to_check)

        if not pre_ws_valid or not post_ws_valid:
            # If either process measurement failed or was malformed, never calculate false deltas from zero fields!
            nova_ws_before = pre_proc.working_set_bytes if pre_ws_valid else None
            nova_ws_after = post_proc.working_set_bytes if post_ws_valid else None
            nova_priv_before = pre_proc.private_commit_bytes if (pre_ws_valid and _is_valid_byte_counter(pre_proc.private_commit_bytes)) else None
            nova_priv_after = post_proc.private_commit_bytes if (post_ws_valid and _is_valid_byte_counter(post_proc.private_commit_bytes)) else None
            nova_ws_delta_bytes = None
            nova_priv_delta_bytes = None
            status = ReclamationStatus.RECLAMATION_UNVERIFIED
            process_ws_verified = False
            process_priv_verified = False
            process_telemetry_verified = False
            safety_notes.append(
                "Process-local memory telemetry failed or malformed (status != SUCCESS, PID mismatch, or invalid byte counters). "
                "Measured deltas set to None. Outcome unverified despite gc.collect(2) execution."
            )
        else:
            nova_ws_before = pre_proc.working_set_bytes
            nova_ws_after = post_proc.working_set_bytes
            nova_ws_delta_bytes = nova_ws_before - nova_ws_after # Kept for diagnostics
            process_ws_verified = True

            pre_priv_valid = _is_valid_byte_counter(pre_proc.private_commit_bytes)
            post_priv_valid = _is_valid_byte_counter(post_proc.private_commit_bytes)

            if not pre_priv_valid or not post_priv_valid:
                nova_priv_before = pre_proc.private_commit_bytes if pre_priv_valid else None
                nova_priv_after = post_proc.private_commit_bytes if post_priv_valid else None
                nova_priv_delta_bytes = None
                status = ReclamationStatus.RECLAMATION_UNVERIFIED
                process_priv_verified = False
                process_telemetry_verified = False
                safety_notes.append("Process private commit telemetry was unavailable or invalid. Outcome unverified.")
            else:
                nova_priv_before = pre_proc.private_commit_bytes
                nova_priv_after = post_proc.private_commit_bytes
                nova_priv_delta_bytes = nova_priv_before - nova_priv_after
                process_priv_verified = True
                process_telemetry_verified = True

                # Action integrity check: cleanup action must complete successfully without failure
                action_succeeded = len(failed) == 0 and len(completed) > 0
                min_proc_delta_bytes = int(self.policy.min_reclamation_delta_mib * 1024 * 1024)

                if not action_succeeded:
                    status = ReclamationStatus.RECLAMATION_UNVERIFIED
                    safety_notes.append(
                        "Reclamation action failed or raised an exception. Observed process memory changes "
                        "cannot establish successful reclamation."
                    )
                elif nova_priv_delta_bytes >= min_proc_delta_bytes:
                    status = ReclamationStatus.RECLAMATION_COMPLETED
                    safety_notes.append(
                        f"Demonstrable process-private-commit decrease observed ({nova_priv_delta_bytes / (1024**2):.2f} MiB >= "
                        f"{self.policy.min_reclamation_delta_mib:.1f} MiB). Note: Concurrent process activity may also affect counters."
                    )
                else:
                    status = ReclamationStatus.NO_MEASURABLE_IMPROVEMENT
                    if nova_ws_delta_bytes is not None and nova_ws_delta_bytes >= min_proc_delta_bytes:
                        safety_notes.append(
                            "Working set physical residency decreased, but private commit showed no measurable improvement (>= 1 MiB). "
                            "Working set alone does not prove memory release; status truthfully reported as NO_MEASURABLE_IMPROVEMENT."
                        )
                    else:
                        safety_notes.append("No measurable decrease in process private commit observed (>= 1 MiB).")

        paging_before = {
            "pages_input_per_sec": pre_ram.pages_input_per_sec,
            "pages_output_per_sec": pre_ram.pages_output_per_sec
        } if pre_ram.pages_input_per_sec is not None else None

        paging_after = {
            "pages_input_per_sec": post_ram.pages_input_per_sec,
            "pages_output_per_sec": post_ram.pages_output_per_sec
        } if post_ram.pages_input_per_sec is not None else None

        report = MemoryReclaimReport(
            timestamp_utc=timestamp_utc,
            trigger_reason=trigger_reason,
            pressure_level=pressure,
            system_avail_bytes_before=pre_ram.avail_phys_bytes,
            system_avail_bytes_after=post_ram.avail_phys_bytes,
            observed_system_avail_delta_bytes=sys_delta_bytes,
            observed_system_avail_delta_mib=sys_delta_mib,
            ram_available_before_gib=pre_ram.avail_phys_gib,
            ram_available_after_gib=post_ram.avail_phys_gib,
            ram_delta_gib=ram_delta_gib,
            commit_before_pct=pre_ram.commit_pct or 0.0,
            commit_after_pct=post_ram.commit_pct or 0.0,
            nova_process_working_set_bytes_before=nova_ws_before,
            nova_process_working_set_bytes_after=nova_ws_after,
            nova_process_private_bytes_before=nova_priv_before,
            nova_process_private_bytes_after=nova_priv_after,
            nova_process_working_set_delta_bytes=nova_ws_delta_bytes,
            nova_process_private_delta_bytes=nova_priv_delta_bytes,
            process_working_set_verified=process_ws_verified,
            process_private_commit_verified=process_priv_verified,
            process_telemetry_verified=process_telemetry_verified,
            actions_attempted=attempted,
            actions_completed=completed,
            actions_failed=failed,
            reclamation_duration_seconds=duration,
            cooldown_applied=False,
            overall_status=status,
            safety_notes=safety_notes,
            paging_activity_before=paging_before,
            paging_activity_after=paging_after
        )

        self.save_report(report)
        return report

    def save_report(self, report: MemoryReclaimReport) -> Tuple[str, str]:
        """Saves report atomically to latest and timestamped JSON files."""
        os.makedirs(self.reports_dir, exist_ok=True)
        latest_path = os.path.join(self.reports_dir, "reclaim_latest.json")
        ts_clean = report.timestamp_utc.replace(":", "-").replace("+", "_")
        ts_path = os.path.join(self.reports_dir, f"reclaim_{ts_clean}.json")

        data = report.to_dict()

        for dest in [latest_path, ts_path]:
            tmp_dest = dest + ".tmp"
            with open(tmp_dest, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_dest, dest)

        return latest_path, ts_path
