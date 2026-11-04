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
    def analyze_processes(
        cls,
        processes: List[ProcessResourceSnapshot],
        ram_deficit_gib: float = 0.0
    ) -> List[MemoryRecommendation]:
        """
        Analyzes a list of running process snapshots and returns ranked recommendations.
        Strict Safety Rules:
        - PROTECTED_SYSTEM processes are NEVER recommended for closure.
        - Candidates are ranked by verified memory footprint.
        - Distinguishes physical working set from private commit charge.
        - Clearly flags partial or missing private commit telemetry.
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

            # Strict exclusion: Protected system processes are NEVER recommended for closure
            if category == ProcessCategory.PROTECTED_SYSTEM:
                continue

            total_ws_bytes = sum(proc.working_set_bytes for proc in proc_list)
            total_ws_mb = round(total_ws_bytes / (1024**2), 1)

            instances_total = sum(proc.instances_total for proc in proc_list)
            instances_measured = sum(proc.instances_measured for proc in proc_list)
            is_partial = any(proc.is_partial_private_commit for proc in proc_list) or (0 < instances_measured < instances_total)

            known_priv_bytes = [proc.private_commit_bytes for proc in proc_list if proc.private_commit_bytes is not None]
            total_priv_mb: Optional[float] = round(sum(known_priv_bytes) / (1024**2), 1) if known_priv_bytes else None

            # Collect all unique PIDs
            all_pids: List[int] = []
            for proc in proc_list:
                if proc.pids:
                    all_pids.extend(proc.pids)
                elif proc.pid > 0:
                    all_pids.append(proc.pid)
            all_pids = sorted(list(set(all_pids)))

            # Threshold: evaluate processes holding >= 100 MB working set or >= 100 MB private commit
            if total_ws_mb < 100.0 and (total_priv_mb or 0.0) < 100.0:
                continue

            candidate_data.append({
                "name": name,
                "proc_list": proc_list,
                "category": category,
                "all_pids": all_pids,
                "ws_bytes": total_ws_bytes,
                "ws_mb": total_ws_mb,
                "priv_mb": total_priv_mb,
                "instances_total": instances_total,
                "instances_measured": instances_measured,
                "is_partial": is_partial
            })

        # Rank candidates using verified telemetry:
        # Sort by total memory footprint ((total_priv_mb or 0) + ws_mb) descending,
        # giving user applications priority at comparable sizes.
        def _sort_key(c: Dict[str, Any]) -> Tuple[int, float]:
            cat_prio = 2 if c["category"] == ProcessCategory.USER_APPLICATION else 1
            combined_mb = (c["priv_mb"] or 0.0) + c["ws_mb"]
            return (cat_prio, combined_mb)

        candidate_data.sort(key=_sort_key, reverse=True)

        for c in candidate_data:
            name = c["name"]
            category = c["category"]
            total_ws_mb = c["ws_mb"]
            total_priv_mb = c["priv_mb"]
            instances_total = c["instances_total"]
            instances_measured = c["instances_measured"]
            is_partial = c["is_partial"]
            all_pids = c["all_pids"]

            # Heuristic calculation (informational only; not a guarantee)
            heuristic_reclaim_mb: Optional[float] = None
            if total_priv_mb is not None:
                heuristic_reclaim_mb = round(total_priv_mb + max(0.0, total_ws_mb - total_priv_mb) * 0.5, 1)

            # Working-set interpretation
            ws_note = (
                f"Observed resident working set: ~{total_ws_mb:.0f} MB. "
                "Working set measures physical memory residency; actual RAM released depends on OS standby caching."
            )

            # Private commit interpretation
            if total_priv_mb is not None:
                if is_partial:
                    priv_note = (
                        f"Observed private commit: ~{total_priv_mb:.0f} MB (partial measurement: "
                        f"{instances_measured}/{instances_total} instances measured). "
                        "Private commit represents dedicated commit charge; closing frees virtual commit allocation."
                    )
                else:
                    priv_note = (
                        f"Observed private commit: ~{total_priv_mb:.0f} MB across all {instances_total} instance(s). "
                        "Private commit represents dedicated commit charge; closing frees virtual commit allocation."
                    )
            else:
                priv_note = "Private commit telemetry unavailable (access restricted or zero measured instances)."

            if category == ProcessCategory.USER_APPLICATION:
                profile = USER_APP_PROFILES.get(name, {})
                label = profile.get("label", name)
                sub_cat = profile.get("sub_category", "User Application")
                action = profile.get("action", "Close application through its normal window interface.")
                why = f"High-memory user-facing application ({sub_cat}). Safe to close if not currently required."
                coverage_str = f" ({instances_measured}/{instances_total} measured)" if is_partial else ""
                priv_str = f", ~{total_priv_mb:.0f} MB private commit{coverage_str}" if total_priv_mb is not None else ""
                rec_text = (
                    f"[{sub_cat}] {label} observed resident working set: ~{total_ws_mb:.0f} MB "
                    f"across {instances_total} process instance(s){priv_str}. "
                    f"Action: {action} "
                    f"(Note: Observed working set is an indicator; actual RAM released depends on OS standby caching)."
                )
                is_user_facing = True

            elif category == ProcessCategory.BACKGROUND_APPLICATION:
                profile = KNOWN_BACKGROUND_PROFILES.get(name, {})
                label = profile.get("label", name)
                sub_cat = profile.get("sub_category", "Background Worker")
                action = profile.get("action", "Close or pause application via system tray or console.")
                why = f"Optional background application ({sub_cat}). Review if active during inference."
                coverage_str = f" ({instances_measured}/{instances_total} measured)" if is_partial else ""
                priv_str = f", ~{total_priv_mb:.0f} MB private commit{coverage_str}" if total_priv_mb is not None else ""
                rec_text = (
                    f"[Background: {sub_cat}] {label} observed resident working set: ~{total_ws_mb:.0f} MB "
                    f"across {instances_total} instance(s){priv_str}. "
                    f"Action: {action} "
                    f"If this is an active tool, consider closing it through its normal interface."
                )
                is_user_facing = False

            else:  # ProcessCategory.UNKNOWN
                action = "Review process function. Close through its normal interface only if recognized and unused."
                why = "Unclassified high-memory process. Treated conservatively; do not close if purpose is unknown."
                coverage_str = f" ({instances_measured}/{instances_total} measured)" if is_partial else ""
                priv_str = f", ~{total_priv_mb:.0f} MB private commit{coverage_str}" if total_priv_mb is not None else ""
                rec_text = (
                    f"[Unclassified Process] '{name}' observed resident working set: ~{total_ws_mb:.0f} MB "
                    f"across {instances_total} instance(s){priv_str}. "
                    f"Action: {action}"
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
                unverified_heuristic_reclaim_mb=heuristic_reclaim_mb,
                recommendation_text=rec_text,
                category=category,
                why_recommended=why,
                action_guidance=action,
                working_set_interpretation=ws_note,
                private_commit_interpretation=priv_note
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

    @staticmethod
    def remeasure_memory(
        monitor: ResourceMonitor,
        baseline_ram: Optional[SystemMemorySnapshot] = None
    ) -> Tuple[SystemMemorySnapshot, Dict[str, Any]]:
        """
        Collects fresh read-only system memory telemetry and compares it against a baseline snapshot.
        Reports exact observed changes in available physical RAM and system commit charge.
        Evaluates whether the 10.0 GiB available RAM admission gate is met.
        """
        fresh = monitor.get_system_memory()
        summary: Dict[str, Any] = {
            "status": fresh.status.value,
            "avail_ram_gib": fresh.avail_phys_gib,
            "commit_total_gib": fresh.commit_total_gib,
            "commit_pct": fresh.commit_pct,
            "admission_gate_passed": fresh.avail_phys_gib >= 10.0,
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

        return fresh, summary
