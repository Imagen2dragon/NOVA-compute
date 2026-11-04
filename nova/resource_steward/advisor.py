"""
NOVA Compute — Intelligent Memory Advisor
Analyzes process memory distribution and generates safe, actionable recommendations
for normal application closure without force-killing or interfering with user work.
"""

from typing import List, Dict, Optional
from .models import ProcessResourceSnapshot, MemoryRecommendation

USER_APP_PROFILES = {
    "msedge.exe": {
        "label": "Microsoft Edge Browser",
        "category": "Browser",
        "action": "Save work and close active tabs or exit unused browser windows."
    },
    "chrome.exe": {
        "label": "Google Chrome",
        "category": "Browser",
        "action": "Close memory-heavy browser tabs or exit unused browser sessions."
    },
    "firefox.exe": {
        "label": "Mozilla Firefox",
        "category": "Browser",
        "action": "Close unnecessary tabs or exit unused browser windows."
    },
    "claude.exe": {
        "label": "Claude Desktop",
        "category": "AI Assistant",
        "action": "Close Claude Desktop application normally if not currently in use."
    },
    "code.exe": {
        "label": "Visual Studio Code",
        "category": "Development IDE",
        "action": "Save open files and close unused editor windows or heavy extensions."
    },
    "devenv.exe": {
        "label": "Microsoft Visual Studio",
        "category": "Development IDE",
        "action": "Save open solutions and close unused Visual Studio instances."
    },
    "pycharm64.exe": {
        "label": "PyCharm",
        "category": "Development IDE",
        "action": "Close unused projects and exit IDE normally."
    },
    "ollama.exe": {
        "label": "Ollama Local LLM Service",
        "category": "Local Inference",
        "action": "Stop unused background models via 'ollama stop <model>' or exit Ollama from the system tray."
    },
    "llama-server.exe": {
        "label": "llama-server Background Process",
        "category": "Local Inference",
        "action": "Stop the background llama-server via its normal CLI/service interface."
    },
    "steam.exe": {
        "label": "Steam Client",
        "category": "Gaming",
        "action": "Exit Steam client from system tray to free background client memory."
    },
    "spotify.exe": {
        "label": "Spotify",
        "category": "Media",
        "action": "Close Spotify desktop player if not actively streaming."
    },
    "slack.exe": {
        "label": "Slack",
        "category": "Communication",
        "action": "Close unused Slack workspaces or minimize background activity."
    },
    "discord.exe": {
        "label": "Discord",
        "category": "Communication",
        "action": "Close Discord desktop client if not needed during inference."
    },
    "telegram.exe": {
        "label": "Telegram Desktop",
        "category": "Communication",
        "action": "Minimize or close Telegram desktop application."
    }
}

class MemoryAdvisor:
    """Provides evidence-based recommendations to help the user clear memory safely."""

    @staticmethod
    def analyze_processes(processes: List[ProcessResourceSnapshot], ram_deficit_gib: float = 0.0) -> List[MemoryRecommendation]:
        recommendations: List[MemoryRecommendation] = []

        # Group by executable name
        grouped: Dict[str, List[ProcessResourceSnapshot]] = {}
        for p in processes:
            grouped.setdefault(p.name.lower(), []).append(p)

        # Sort by aggregate working set descending
        sorted_groups = sorted(
            grouped.items(),
            key=lambda item: sum(proc.working_set_bytes for proc in item[1]),
            reverse=True
        )

        for name, proc_list in sorted_groups:
            total_ws_bytes = sum(proc.working_set_bytes for proc in proc_list)
            total_ws_mb = round(total_ws_bytes / (1024**2), 1)

            # Aggregate instances count and measurement coverage
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

            # Only suggest for processes holding significant RAM (> 150 MB)
            if total_ws_mb < 150.0:
                continue

            profile = USER_APP_PROFILES.get(name)
            is_user_facing = profile is not None

            # Unverified heuristic: not a promise of exact reclaimable RAM
            heuristic_reclaim_mb: Optional[float] = None
            if total_priv_mb is not None:
                heuristic_reclaim_mb = round(total_priv_mb + (total_ws_mb - total_priv_mb) * 0.5, 1)

            coverage_str = f" ({instances_measured}/{instances_total} measured)" if is_partial else ""
            priv_str = f", ~{total_priv_mb:.0f} MB private commit{coverage_str}" if total_priv_mb is not None else ""

            if is_user_facing:
                rec_text = (
                    f"[{profile['category']}] {profile['label']} observed resident working set: ~{total_ws_mb:.0f} MB "
                    f"across {instances_total} process instance(s){priv_str}. "
                    f"Action: {profile['action']} "
                    f"(Note: Observed working set is an indicator; actual RAM released depends on OS standby caching)."
                )
            else:
                rec_text = (
                    f"Background process '{name}' observed resident working set: ~{total_ws_mb:.0f} MB across {instances_total} instance(s){priv_str}. "
                    f"If this is an active tool, consider closing it through its normal interface."
                )

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
                recommendation_text=rec_text
            ))

        return recommendations
