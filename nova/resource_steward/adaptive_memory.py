"""
NOVA Compute — Adaptive Memory Reclaimer (AMR)
Inspired by open-source memory monitors (Mem Reduct, WinMemClean) under a strict SAFE_AUTO policy:
- Detects multi-tier system memory pressure (NORMAL, OPTIMIZATION, CONSTRAINED, CRITICAL).
- Throttles cleanup attempts via configurable cooldown intervals.
- Strictly reclaims ONLY NOVA-owned resources and Python runtime garbage (gc.collect).
- PROHIBITS system-wide cache purges (NtSetSystemInformation), working set flushing (EmptyWorkingSet),
  and foreign process termination.
- Validates pre- and post-reclamation telemetry and generates immutable audit reports.
"""

import os
import gc
import json
import time
from typing import Optional, List, Dict, Any, Tuple, Callable
from datetime import datetime, timezone

from .models import (
    TelemetryStatus,
    MemoryPressureLevel,
    ReclamationStatus,
    MemoryReclaimReport,
    SystemMemorySnapshot,
    current_iso_utc
)
from .policy import ResourcePolicy, DEFAULT_POLICY
from .monitor import ResourceMonitor

class MemoryPressureDetector:
    """Classifies system memory pressure into hierarchical operational tiers."""

    def __init__(self, policy: Optional[ResourcePolicy] = None):
        self.policy = policy or DEFAULT_POLICY

    def evaluate_pressure(self, snapshot: SystemMemorySnapshot) -> MemoryPressureLevel:
        """
        Determines memory pressure tier based on verified available physical RAM and commit status.
        Fail-closed: if telemetry failed or values invalid, treated as CRITICAL.
        """
        if snapshot.status != TelemetryStatus.SUCCESS:
            return MemoryPressureLevel.CRITICAL

        avail_ram = snapshot.avail_phys_gib
        commit_pct = snapshot.commit_pct

        # Critical threshold
        if avail_ram < self.policy.critical_ram_gib or (commit_pct is not None and commit_pct >= self.policy.critical_commit_pct):
            return MemoryPressureLevel.CRITICAL

        # Constrained threshold (< minimum required for inference, e.g. < 10.0 GiB)
        if avail_ram < self.policy.ram_pressure_constrained_gib:
            return MemoryPressureLevel.CONSTRAINED

        # Optimization threshold (10.0 GiB <= RAM < 12.0 GiB)
        if avail_ram < self.policy.ram_pressure_optimization_gib:
            return MemoryPressureLevel.OPTIMIZATION

        # Normal operational state (>= 12.0 GiB)
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

    def reclaim(
        self,
        extra_cleaners: Optional[List[Callable[[], None]]] = None
    ) -> Tuple[List[str], List[str], List[str], float]:
        """
        Executes SAFE_AUTO memory reclamation.
        Returns: (actions_attempted, actions_completed, actions_failed, duration_seconds)
        """
        t_start = self.clock()
        attempted: List[str] = []
        completed: List[str] = []
        failed: List[str] = []

        # Action 1: Python full generational garbage collection
        attempted.append("python_gc_collect")
        try:
            # Run generational collection across all 3 generations
            collected = gc.collect(2)
            completed.append(f"python_gc_collect (freed {collected} unreachable objects)")
        except Exception as e:
            failed.append(f"python_gc_collect: {e}")

        # Action 2: Execute any NOVA-internal registered buffer flushers
        if extra_cleaners:
            for idx, cleaner in enumerate(extra_cleaners):
                action_name = f"nova_internal_cleaner_{idx+1}"
                attempted.append(action_name)
                try:
                    cleaner()
                    completed.append(action_name)
                except Exception as e:
                    failed.append(f"{action_name}: {e}")

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
        self.reports_dir = reports_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "reports", "phase2c", "memory_reclamation"
        )
        self._registered_cleaners: List[Callable[[], None]] = []

    def register_cleaner(self, cleaner: Callable[[], None]) -> None:
        """Registers a NOVA-internal buffer/cache cleanup callable."""
        self._registered_cleaners.append(cleaner)

    def evaluate_and_reclaim(
        self,
        trigger_reason: str = "Preflight memory pressure check",
        force: bool = False
    ) -> MemoryReclaimReport:
        """
        Runs a complete evaluation and safe reclamation pass.
        1. Measure pre-reclamation telemetry.
        2. Evaluate pressure level.
        3. Check cooldown unless forced.
        4. Execute SAFE_AUTO reclamation if pressure indicates.
        5. Measure post-reclamation telemetry.
        6. Compute verified deltas and log report.
        """
        timestamp_utc = current_iso_utc()
        safety_notes: List[str] = [
            "Policy: SAFE_AUTO (Strictly NOVA-owned resources only).",
            "Zero system-wide standby/modified list purging.",
            "Zero foreign process working-set trimming.",
            "Zero unprivileged/privileged system modifications."
        ]

        # 1. Pre-reclamation telemetry
        pre_ram = self.monitor.get_system_memory()
        if pre_ram.status != TelemetryStatus.SUCCESS:
            return MemoryReclaimReport(
                timestamp_utc=timestamp_utc,
                trigger_reason=trigger_reason,
                pressure_level=MemoryPressureLevel.CRITICAL,
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
                safety_notes=safety_notes + ["Fail-closed: Pre-reclamation telemetry unavailable."]
            )

        pressure = self.detector.evaluate_pressure(pre_ram)

        # 2. Check if reclamation is necessary
        if pressure == MemoryPressureLevel.NORMAL and not force:
            return MemoryReclaimReport(
                timestamp_utc=timestamp_utc,
                trigger_reason=trigger_reason,
                pressure_level=pressure,
                ram_available_before_gib=pre_ram.avail_phys_gib,
                ram_available_after_gib=pre_ram.avail_phys_gib,
                ram_delta_gib=0.0,
                commit_before_pct=pre_ram.commit_pct or 0.0,
                commit_after_pct=pre_ram.commit_pct or 0.0,
                actions_attempted=[],
                actions_completed=[],
                actions_failed=[],
                reclamation_duration_seconds=0.0,
                cooldown_applied=False,
                overall_status=ReclamationStatus.NO_ACTION_REQUIRED,
                safety_notes=safety_notes + [f"System RAM healthy ({pre_ram.avail_phys_gib:.2f} GiB >= {self.policy.ram_pressure_optimization_gib:.1f} GiB). No action needed."]
            )

        # 3. Check cooldown
        if not self.cooldown_mgr.can_reclaim() and not force:
            remaining = self.cooldown_mgr.remaining_cooldown_seconds
            return MemoryReclaimReport(
                timestamp_utc=timestamp_utc,
                trigger_reason=trigger_reason,
                pressure_level=pressure,
                ram_available_before_gib=pre_ram.avail_phys_gib,
                ram_available_after_gib=pre_ram.avail_phys_gib,
                ram_delta_gib=0.0,
                commit_before_pct=pre_ram.commit_pct or 0.0,
                commit_after_pct=pre_ram.commit_pct or 0.0,
                actions_attempted=[],
                actions_completed=[],
                actions_failed=[],
                reclamation_duration_seconds=0.0,
                cooldown_applied=True,
                overall_status=ReclamationStatus.NO_ACTION_REQUIRED,
                safety_notes=safety_notes + [f"Reclamation throttled by cooldown ({remaining:.1f}s remaining)."]
            )

        # 4. Execute safe reclamation
        attempted, completed, failed, duration = self.reclaimer.reclaim(
            extra_cleaners=self._registered_cleaners
        )
        self.cooldown_mgr.record_reclamation()

        # 5. Post-reclamation telemetry
        post_ram = self.monitor.get_system_memory()
        if post_ram.status != TelemetryStatus.SUCCESS:
            return MemoryReclaimReport(
                timestamp_utc=timestamp_utc,
                trigger_reason=trigger_reason,
                pressure_level=pressure,
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
                safety_notes=safety_notes + ["Post-reclamation verification failed. Fail-closed: outcome unverified."]
            )

        ram_delta_gib = round(post_ram.avail_phys_gib - pre_ram.avail_phys_gib, 4)
        min_delta_gib = self.policy.min_reclamation_delta_mb / 1024.0

        if ram_delta_gib >= min_delta_gib:
            status = ReclamationStatus.RECLAMATION_COMPLETED
        else:
            # Memory reclaimed was below 10 MB or zero
            status = ReclamationStatus.NO_MEASURABLE_IMPROVEMENT

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
            ram_available_before_gib=pre_ram.avail_phys_gib,
            ram_available_after_gib=post_ram.avail_phys_gib,
            ram_delta_gib=ram_delta_gib,
            commit_before_pct=pre_ram.commit_pct or 0.0,
            commit_after_pct=post_ram.commit_pct or 0.0,
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
        """Saves report to latest and timestamped JSON files."""
        os.makedirs(self.reports_dir, exist_ok=True)
        latest_path = os.path.join(self.reports_dir, "reclaim_latest.json")
        ts_clean = report.timestamp_utc.replace(":", "-").replace("+", "_")
        ts_path = os.path.join(self.reports_dir, f"reclaim_{ts_clean}.json")

        data = report.to_dict()
        with open(latest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        with open(ts_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        return latest_path, ts_path
