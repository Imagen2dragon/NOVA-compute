"""
NOVA Compute — Intelligent Memory Advisor
Analyzes process memory distribution and generates safe, actionable recommendations
for normal application closure without force-killing or interfering with user work.
Adheres strictly to read-only diagnostics and safe application classification:
  - Category A: User Applications
  - Category B: Background Applications
  - Category C: Protected Processes (STRICTLY EXCLUDED from closure recommendations)
"""

from typing import List, Dict, Optional, Tuple, Set, Any
from .models import (
    ProcessResourceSnapshot,
    MemoryRecommendation,
    ProcessCategory,
    SystemMemorySnapshot,
    GPUSnapshot,
    DiskSnapshot,
    MemoryAdvisorReport,
    TelemetryStatus,
    current_iso_utc
)
from .policy import ResourcePolicy, DEFAULT_POLICY
from .monitor import ResourceMonitor

# Category C: Protected Processes (Windows services, Defender, drivers, NVIDIA services)
# STRICT SAFETY GOVERNANCE: These processes must NEVER be recommended for closure,
# terminated, suspended, or manipulated.
PROTECTED_PROCESSES: Set[str] = {
    # Windows Core & Session Architecture
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "services.exe", "lsass.exe", "winlogon.exe", "svchost.exe", "fontdrvhost.exe",
    "dwm.exe", "explorer.exe", "sihost.exe", "taskhostw.exe", "spoolsv.exe", "ctfmon.exe",
    "conhost.exe", "runtimebroker.exe", "shellexperiencehost.exe", "searchindexer.exe",
    "searchhost.exe", "startmenuexperiencehost.exe", "textinputhost.exe",
    # Security & Microsoft Defender
    "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe", "smartscreen.exe",
    "securityhealthsystray.exe", "sgrmbroker.exe",
    # Audio, Drivers & NVIDIA Services
    "nvcontainer.exe", "nvdisplay.container.exe", "nvsphelper64.exe", "wlanext.exe",
    "audiodg.exe"
}

# Category A: User Applications (Interactive user-facing software)
USER_APP_PROFILES: Dict[str, Dict[str, str]] = {
    "msedge.exe": {
        "label": "Microsoft Edge Browser",
        "sub_category": "Web Browser",
        "action": "Save work and close active tabs or exit unused browser windows."
    },
    "chrome.exe": {
        "label": "Google Chrome",
        "sub_category": "Web Browser",
        "action": "Close memory-heavy browser tabs or exit unused browser sessions."
    },
    "firefox.exe": {
        "label": "Mozilla Firefox",
        "sub_category": "Web Browser",
        "action": "Close unnecessary tabs or exit unused browser windows."
    },
    "brave.exe": {
        "label": "Brave Browser",
        "sub_category": "Web Browser",
        "action": "Close open tabs or exit browser window normally."
    },
    "opera.exe": {
        "label": "Opera Browser",
        "sub_category": "Web Browser",
        "action": "Close open tabs or exit browser window normally."
    },
    "claude.exe": {
        "label": "Claude Desktop",
        "sub_category": "AI Assistant",
        "action": "Close Claude Desktop application normally if not currently in use."
    },
    "chatgpt.exe": {
        "label": "ChatGPT Desktop",
        "sub_category": "AI Assistant",
        "action": "Close ChatGPT Desktop application normally if not currently in use."
    },
    "code.exe": {
        "label": "Visual Studio Code",
        "sub_category": "Development IDE",
        "action": "Save open files and close unused editor windows or heavy extensions."
    },
    "devenv.exe": {
        "label": "Microsoft Visual Studio",
        "sub_category": "Development IDE",
        "action": "Save open solutions and close unused Visual Studio instances."
    },
    "pycharm64.exe": {
        "label": "PyCharm",
        "sub_category": "Development IDE",
        "action": "Close unused projects and exit IDE normally."
    },
    "idea64.exe": {
        "label": "IntelliJ IDEA",
        "sub_category": "Development IDE",
        "action": "Close unused projects and exit IDE normally."
    },
    "cursor.exe": {
        "label": "Cursor IDE",
        "sub_category": "Development IDE",
        "action": "Save open files and close editor windows normally."
    },
    "antigravity.exe": {
        "label": "Antigravity IDE / Agent",
        "sub_category": "Development IDE",
        "action": "If this is an active workspace IDE, consider closing background windows through normal interface."
    },
    "ollama.exe": {
        "label": "Ollama Local LLM Service",
        "sub_category": "Local Inference",
        "action": "Stop unused background models via 'ollama stop <model>' or exit Ollama from the system tray."
    },
    "llama-server.exe": {
        "label": "llama-server Background Process",
        "sub_category": "Local Inference",
        "action": "Stop the background llama-server via its normal CLI/service interface."
    },
    "steam.exe": {
        "label": "Steam Client",
        "sub_category": "Gaming",
        "action": "Exit Steam client from system tray to free background client memory."
    },
    "epicgameslauncher.exe": {
        "label": "Epic Games Launcher",
        "sub_category": "Gaming",
        "action": "Exit Epic Games Launcher from system tray."
    },
    "spotify.exe": {
        "label": "Spotify",
        "sub_category": "Media",
        "action": "Close Spotify desktop player if not actively streaming."
    },
    "slack.exe": {
        "label": "Slack",
        "sub_category": "Communication",
        "action": "Close unused Slack workspaces or minimize background activity."
    },
    "discord.exe": {
        "label": "Discord",
        "sub_category": "Communication",
        "action": "Close Discord desktop client if not needed during inference."
    },
    "telegram.exe": {
        "label": "Telegram Desktop",
        "sub_category": "Communication",
        "action": "Minimize or close Telegram desktop application."
    },
    "teams.exe": {
        "label": "Microsoft Teams",
        "sub_category": "Communication",
        "action": "Exit or quit Microsoft Teams from the system tray."
    },
    "zoom.exe": {
        "label": "Zoom",
        "sub_category": "Communication",
        "action": "End active calls and close Zoom application normally."
    }
}

# Category B: Background Applications (sync tools, launchers, optional background workers)
KNOWN_BACKGROUND_PROFILES: Dict[str, Dict[str, str]] = {
    "onedrive.exe": {
        "label": "Microsoft OneDrive",
        "sub_category": "Cloud Storage Sync",
        "action": "Pause syncing or exit OneDrive from system tray during inference."
    },
    "dropbox.exe": {
        "label": "Dropbox",
        "sub_category": "Cloud Storage Sync",
        "action": "Pause syncing or exit Dropbox from system tray."
    },
    "googledrivesync.exe": {
        "label": "Google Drive",
        "sub_category": "Cloud Storage Sync",
        "action": "Pause syncing or exit Google Drive from system tray."
    },
    "steamwebhelper.exe": {
        "label": "Steam Web Helper",
        "sub_category": "Launcher Helper",
        "action": "Exiting Steam will close all related Steam Web Helper processes."
    },
    "epicwebhelper.exe": {
        "label": "Epic Games Web Helper",
        "sub_category": "Launcher Helper",
        "action": "Exiting Epic Games Launcher will terminate helper processes."
    },
    "node.exe": {
        "label": "Node.js Background Process",
        "sub_category": "Development Worker",
        "action": "Terminate unused local Node.js development servers or scripts via their console/terminal."
    },
    "language_server.exe": {
        "label": "Language Server Background Worker",
        "sub_category": "Development Worker",
        "action": "Close idle IDE editor tabs or projects to allow language server to unload index caches."
    }
}

class MemoryAdvisor:
    """
    Intelligent, read-only memory diagnostic advisor.
    Identifies high-memory applications, categorizes processes into strict safety tiers,
    distinguishes complete from partial telemetry, and generates evidence-based recommendations
    for normal manual application closure by the user.
    """

    @classmethod
    def classify_process(cls, process_name: str) -> ProcessCategory:
        """
        Classifies an executable into one of three strict operational tiers:
        - PROTECTED_SYSTEM: Windows services, Defender, drivers, NVIDIA services.
        - USER_APPLICATION: Interactive user-facing apps (browsers, IDEs, desktop tools).
        - BACKGROUND_APPLICATION: Known optional background sync/launcher/helper tools.
        - UNKNOWN: Unclassified process, treated conservatively.
        """
        norm = process_name.lower().strip()
        if norm in PROTECTED_PROCESSES:
            return ProcessCategory.PROTECTED_SYSTEM
        if norm in USER_APP_PROFILES:
            return ProcessCategory.USER_APPLICATION
        if norm in KNOWN_BACKGROUND_PROFILES:
            return ProcessCategory.BACKGROUND_APPLICATION
        return ProcessCategory.UNKNOWN

    @classmethod
    def compute_ranking_score(cls, ws_mb: float, priv_mb: Optional[float] = None) -> float:
        """
        Computes a transparent advisory prioritization index for candidate processes.

        CRITICAL ARCHITECTURAL DISTINCTION:
        1. Sorting Formula: Candidate ordering is strictly governed by the 3-tier tuple:
             _sort_key = (ws_mb, cat_prio, priv_mb or 0.0)
           - Primary key: Observed physical working set (resident RAM footprint in MB)
           - Secondary key: Category priority (User Applications prioritized over Background/Unknown)
           - Tertiary key: Dedicated private commit charge (virtual commit relief tie-breaker)
        2. Informational Score: RankingScore is an informational composite indicator only:
             RankingScore = ws_mb + 0.25 * (priv_mb if priv_mb is not None else 0.0)
           It does NOT represent recoverable physical RAM, does not guarantee memory recovery,
           and is NOT used as the candidate sorting formula.
        """
        commit_bonus = (priv_mb * 0.25) if priv_mb is not None else 0.0
        return round(ws_mb + commit_bonus, 2)

    @classmethod
    def analyze_processes(
        cls,
        processes: List[ProcessResourceSnapshot],
        ram_deficit_gib: float = 0.0
    ) -> List[MemoryRecommendation]:
        """
        Analyzes a list of running process snapshots and returns ranked recommendations.
        Strict Safety Rules:
        - PROTECTED_SYSTEM processes are NEVER recommended for closure.
        - Primary ranking consideration is observed physical working set.
        - Secondary ranking factors are category priority and private commit charge.
        - Strict validation: failed, boolean, or negative telemetry counters are never aggregated.
        - Clearly distinguishes physical working set from private commit charge.
        - Preserves partial-measurement warnings and unavailable measurement states.
        - Zero measured instances out of a positive total is never treated as complete.
        - Never claims working set reduction is guaranteed available physical RAM.
        - Never terminates or manipulates external processes.
        """
        recommendations: List[MemoryRecommendation] = []

        # 1. Group by executable name
        grouped: Dict[str, List[ProcessResourceSnapshot]] = {}
        for p in processes:
            grouped.setdefault(p.name.lower().strip(), []).append(p)

        candidate_data = []

        for name, proc_list in grouped.items():
            category = cls.classify_process(name)

            # Strict exclusion: Protected system processes are NEVER recommended for closure.
            # Canonical classification is strictly authoritative regardless of any snapshot field.
            if category == ProcessCategory.PROTECTED_SYSTEM:
                continue

            valid_ws_bytes_list: List[int] = []
            valid_priv_bytes_list: List[int] = []
            exp_instances_total = 0
            meas_instances_total = 0
            has_partial_flag = False
            all_pids: List[int] = []

            for proc in proc_list:
                # 1. Expected instances
                if isinstance(proc.instances_total, int) and not isinstance(proc.instances_total, bool) and proc.instances_total > 0:
                    proc_exp = proc.instances_total
                elif proc.pids:
                    proc_exp = len(proc.pids)
                else:
                    proc_exp = 1
                exp_instances_total += proc_exp

                if proc.is_partial_private_commit:
                    has_partial_flag = True

                # 2. PIDs collection (valid positive integers only)
                if proc.pids:
                    for pid in proc.pids:
                        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                            all_pids.append(pid)
                elif isinstance(proc.pid, int) and not isinstance(proc.pid, bool) and proc.pid > 0:
                    all_pids.append(proc.pid)

                # 3. Working-set validity (status SUCCESS, non-boolean integer, non-negative)
                ws_val = proc.working_set_bytes
                is_ws_valid = (
                    proc.status == TelemetryStatus.SUCCESS and
                    isinstance(ws_val, int) and
                    not isinstance(ws_val, bool) and
                    ws_val >= 0
                )
                if is_ws_valid:
                    valid_ws_bytes_list.append(ws_val)

                # 4. Measured instances extraction
                # When instances_measured is not an int or is negative, clamp to 0.
                if isinstance(proc.instances_measured, int) and not isinstance(proc.instances_measured, bool):
                    proc_meas = max(0, proc.instances_measured)
                else:
                    proc_meas = 0

                # 5. Private-commit validity:
                # Requires status SUCCESS, non-boolean non-negative integer, AND measured instances > 0.
                # If a snapshot contains a valid-looking private-commit value but explicitly reports 0
                # measured instances, private commit is classified as inconsistent/unavailable and
                # excluded from verified aggregation (meas coverage remains strictly 0; never replace with 1).
                priv_val = proc.private_commit_bytes
                is_priv_valid = (
                    proc.status == TelemetryStatus.SUCCESS and
                    priv_val is not None and
                    isinstance(priv_val, int) and
                    not isinstance(priv_val, bool) and
                    priv_val >= 0 and
                    proc_meas > 0
                )
                if is_priv_valid:
                    valid_priv_bytes_list.append(priv_val)
                    meas_instances_total += min(proc_meas, proc_exp)

            instances_total = max(1, exp_instances_total)
            instances_measured = min(meas_instances_total, instances_total)
            all_pids = sorted(list(set(all_pids)))

            # Correct partial coverage logic:
            # If measured instances are fewer than total expected instances, measurement is incomplete/partial.
            # Specifically, zero measured instances out of a positive total cannot be treated as complete.
            is_partial = (instances_measured < instances_total) or has_partial_flag

            if not valid_ws_bytes_list:
                # No verified physical working set measurements; skip candidate
                continue

            total_ws_bytes = sum(valid_ws_bytes_list)
            total_ws_mb = round(total_ws_bytes / (1024**2), 1)
            total_priv_mb: Optional[float] = round(sum(valid_priv_bytes_list) / (1024**2), 1) if valid_priv_bytes_list else None

            # Threshold: evaluate processes holding >= 100 MB working set or >= 100 MB private commit
            if total_ws_mb < 100.0 and (total_priv_mb or 0.0) < 100.0:
                continue

            ranking_score = cls.compute_ranking_score(total_ws_mb, total_priv_mb)

            candidate_data.append({
                "name": name,
                "proc_list": proc_list,
                "category": category,
                "all_pids": all_pids,
                "ws_bytes": total_ws_bytes,
                "ws_mb": total_ws_mb,
                "priv_mb": total_priv_mb,
                "ranking_score": ranking_score,
                "instances_total": instances_total,
                "instances_measured": instances_measured,
                "is_partial": is_partial
            })

        # Rank candidates so observed physical working set remains the primary consideration:
        # Priority 1 (Primary): Observed physical working set (resident RAM footprint in MB)
        # Priority 2 (Secondary): Category priority (User Applications prioritized over Background/Unknown)
        # Priority 3 (Tertiary / Tie-breaker): Dedicated private commit charge
        # Note: RankingScore is an informational indicator only; candidate ordering strictly uses this 3-tier key.
        def _sort_key(c: Dict[str, Any]) -> Tuple[float, int, float]:
            cat_prio = 2 if c["category"] == ProcessCategory.USER_APPLICATION else 1
            return (c["ws_mb"], cat_prio, c["priv_mb"] or 0.0)

        candidate_data.sort(key=_sort_key, reverse=True)

        for c in candidate_data:
            name = c["name"]
            category = c["category"]
            total_ws_mb = c["ws_mb"]
            total_priv_mb = c["priv_mb"]
            ranking_score = c["ranking_score"]
            instances_total = c["instances_total"]
            instances_measured = c["instances_measured"]
            is_partial = c["is_partial"]
            all_pids = c["all_pids"]

            # Working-set interpretation (physical memory residency)
            ws_note = (
                f"Observed resident working set: ~{total_ws_mb:.0f} MB across {instances_total} instance(s). "
                "Working set measures physical memory residency mapped into RAM; "
                "actual physical RAM freed depends on OS page replacement and standby list management."
            )

            # Private commit interpretation (dedicated virtual commit charge)
            if total_priv_mb is not None:
                coverage_note = f" (partial measurement: {instances_measured}/{instances_total} instances measured)" if is_partial else f" across all {instances_total} instance(s)"
                priv_note = (
                    f"Observed private commit: ~{total_priv_mb:.0f} MB{coverage_note}. "
                    "Private commit represents dedicated commit charge (virtual address space committed against the system commit limit); "
                    "closing the application frees virtual commit allocation, but does NOT guarantee immediate physical RAM recovery."
                )
            else:
                priv_note = (
                    f"Private commit telemetry unavailable (0/{instances_total} instances measured; "
                    "access restricted, invalid telemetry counters, or zero measured instances)."
                )

            coverage_str = f" ({instances_measured}/{instances_total} measured)" if is_partial else ""
            priv_str = f", ~{total_priv_mb:.0f} MB private commit{coverage_str}" if total_priv_mb is not None else ""

            if category == ProcessCategory.USER_APPLICATION:
                profile = USER_APP_PROFILES.get(name, {})
                label = profile.get("label", name)
                sub_cat = profile.get("sub_category", "User Application")
                action = profile.get("action", "Close application through its normal window interface.")
                why = f"High-memory user-facing application ({sub_cat}). Safe to close if not currently required."
                rec_text = (
                    f"[{sub_cat}] {label} — Resident working set: ~{total_ws_mb:.0f} MB{priv_str}. "
                    f"Action: {action} "
                    f"(Note: Working set reflects physical residency; private commit reflects virtual commit charge. "
                    f"Actual physical RAM freed must be verified via re-measurement telemetry)."
                )
                is_user_facing = True

            elif category == ProcessCategory.BACKGROUND_APPLICATION:
                profile = KNOWN_BACKGROUND_PROFILES.get(name, {})
                label = profile.get("label", name)
                sub_cat = profile.get("sub_category", "Background Worker")
                action = profile.get("action", "Close or pause application via system tray or console.")
                why = f"Optional background application ({sub_cat}). Review if active during inference."
                rec_text = (
                    f"[Background: {sub_cat}] {label} — Resident working set: ~{total_ws_mb:.0f} MB{priv_str}. "
                    f"Action: {action} "
                    f"(Note: Working set reflects physical residency; private commit reflects virtual commit charge. "
                    f"Actual physical RAM freed must be verified via re-measurement telemetry)."
                )
                is_user_facing = False

            else:  # ProcessCategory.UNKNOWN
                action = "Review process function. Close through its normal interface only if recognized and unused."
                why = "Unclassified high-memory process. Treated conservatively; do not close if purpose is unknown."
                rec_text = (
                    f"[Unclassified Process] '{name}' — Resident working set: ~{total_ws_mb:.0f} MB{priv_str}. "
                    f"Action: {action} "
                    f"(Note: Working set reflects physical residency; private commit reflects virtual commit charge. "
                    f"Actual physical RAM freed must be verified via re-measurement telemetry)."
                )
                is_user_facing = False

            recommendations.append(MemoryRecommendation(
                process_name=name,
                pids=all_pids,
                observed_working_set_mb=total_ws_mb,
                total_private_commit_mb=total_priv_mb,
                is_user_facing=is_user_facing,
                instances_total=instances_total,
                instances_measured=instances_measured,
                is_partial=is_partial,
                unverified_heuristic_reclaim_mb=None,
                recommendation_text=rec_text,
                category=category,
                why_recommended=why,
                action_guidance=action,
                working_set_interpretation=ws_note,
                private_commit_interpretation=priv_note,
                ranking_score=ranking_score
            ))

        return recommendations

    @classmethod
    def generate_advisor_report(
        cls,
        monitor: ResourceMonitor,
        policy: Optional[ResourcePolicy] = None
    ) -> MemoryAdvisorReport:
        """
        Executes a comprehensive, read-only memory advisory evaluation.
        Collects available RAM, commit ratios, process memory footprints,
        and partitions recommendations by category.
        """
        pol = policy or DEFAULT_POLICY
        ram = monitor.get_system_memory()
        top_procs = monitor.list_top_processes(limit=30)

        is_constrained = ram.avail_phys_gib < pol.minimum_inference_ram_gib
        deficit = max(0.0, round(pol.minimum_inference_ram_gib - ram.avail_phys_gib, 3)) if is_constrained else 0.0

        all_recs = cls.analyze_processes(top_procs, ram_deficit_gib=deficit)

        user_recs = [r for r in all_recs if r.category == ProcessCategory.USER_APPLICATION]
        bg_recs = [r for r in all_recs if r.category in (ProcessCategory.BACKGROUND_APPLICATION, ProcessCategory.UNKNOWN)]

        # Track protected processes observed in top_procs
        protected_observed = sorted(list(set(
            p.name for p in top_procs if cls.classify_process(p.name) == ProcessCategory.PROTECTED_SYSTEM
        )))

        return MemoryAdvisorReport(
            timestamp_utc=current_iso_utc(),
            available_ram_gib=ram.avail_phys_gib,
            total_commit_gib=ram.commit_total_gib,
            commit_pct=ram.commit_pct or 0.0,
            is_ram_constrained=is_constrained,
            ram_deficit_gib=deficit,
            recommendations=all_recs,
            user_app_recommendations=user_recs,
            background_app_recommendations=bg_recs,
            protected_processes_observed=protected_observed
        )

    @classmethod
    def remeasure_memory(
        cls,
        monitor: ResourceMonitor,
        baseline_ram: Optional[SystemMemorySnapshot] = None,
        policy: Optional[ResourcePolicy] = None
    ) -> Tuple[SystemMemorySnapshot, Dict[str, Any]]:
        """
        Collects fresh read-only system memory telemetry and compares it against a baseline snapshot.
        Always obtains fresh system RAM, fresh GPU telemetry, and fresh disk telemetry directly
        from the monitor. Never accepts external caller-supplied GPU, disk snapshots, or custom
        budget evaluators.
        Evaluates full system admission gates (RAM, system commit, VRAM, competing GPU apps, disk)
        using NOVA's trusted MemoryBudgetManager and intended ResourcePolicy.
        Fails closed: reaching the 10.0 GiB RAM threshold alone CANNOT pass admission if any other gate fails.
        """
        fresh = monitor.get_system_memory()
        gpu = monitor.get_gpu_telemetry()
        disk = monitor.get_disk_space("C:\\")

        from .memory_budget import MemoryBudgetManager
        budget_mgr = MemoryBudgetManager(policy or DEFAULT_POLICY)

        all_passed, gates = budget_mgr.evaluate_gates(fresh, gpu, disk)
        ram_gate = gates.get("avail_ram")
        ram_passed = ram_gate.passed if ram_gate is not None else False
        failed_gate_names = [name for name, g in gates.items() if not g.passed]

        summary: Dict[str, Any] = {
            "status": fresh.status.value,
            "avail_ram_gib": fresh.avail_phys_gib,
            "commit_total_gib": fresh.commit_total_gib,
            "commit_pct": fresh.commit_pct,
            "admission_gate_passed": all_passed,
            "ram_gate_passed": ram_passed,
            "all_gates_passed": all_passed,
            "failed_gates": failed_gate_names,
            "gates": gates,
            "gpu": gpu,
            "disk": disk,
            "ram_delta_gib": None,
            "commit_delta_pct": None,
            "notes": []
        }

        if baseline_ram is not None and baseline_ram.status == TelemetryStatus.SUCCESS and fresh.status == TelemetryStatus.SUCCESS:
            ram_delta = round(fresh.avail_phys_gib - baseline_ram.avail_phys_gib, 3)
            commit_delta = round(baseline_ram.commit_pct - fresh.commit_pct, 1)
            summary["ram_delta_gib"] = ram_delta
            summary["commit_delta_pct"] = commit_delta

            if ram_delta > 0:
                summary["notes"].append(f"Available physical RAM increased by {ram_delta:.3f} GiB.")
            elif ram_delta < 0:
                summary["notes"].append(f"Available physical RAM decreased by {abs(ram_delta):.3f} GiB due to uncoordinated host activity.")
            else:
                summary["notes"].append("Available physical RAM remained unchanged.")

            if commit_delta > 0:
                summary["notes"].append(f"System commit reduced by {commit_delta:.1f}%.")
            elif commit_delta < 0:
                summary["notes"].append(f"System commit increased by {abs(commit_delta):.1f}%.")

        if not all_passed:
            summary["notes"].append(
                f"Admission gated: {len(failed_gate_names)} gate(s) failed ({', '.join(failed_gate_names)}). "
                "Meeting the RAM threshold alone is insufficient for inference admission."
            )

        return fresh, summary
