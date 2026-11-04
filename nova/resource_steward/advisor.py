"""
NOVA Compute — Intelligent Memory Advisor
Analyzes process memory distribution and generates safe, actionable recommendations
for normal application closure without force-killing or interfering with user work.
"""

from typing import List, Dict, Set
from .models import ProcessResourceSnapshot, MemoryRecommendation

# Well-known user-facing desktop process groups
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
    """Provides polite, evidence-based recommendations to help the user clear memory safely."""

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
            total_priv_bytes = sum(proc.private_commit_bytes for proc in proc_list)
            total_ws_mb = round(total_ws_bytes / (1024**2), 1)
            total_priv_mb = round(total_priv_bytes / (1024**2), 1)
            pids = [proc.pid for proc in proc_list if proc.pid > 0]

            # Only suggest for processes holding significant RAM (> 150 MB)
            if total_ws_mb < 150.0:
                continue

            profile = USER_APP_PROFILES.get(name)
            is_user_facing = profile is not None

            # Realistic reclaim estimate:
            # Private commit is 100% released on process exit.
            # Working set file-backed pages may linger in standby cache, but ~75% becomes immediately reusable RAM.
            estimated_reclaim_mb = round(total_priv_mb * 0.90 + (total_ws_mb - total_priv_mb) * 0.50, 1)

            if is_user_facing:
                rec_text = (
                    f"[{profile['category']}] {profile['label']} is currently consuming ~{total_ws_mb:.0f} MB resident RAM "
                    f"({total_priv_mb:.0f} MB private commit across {len(proc_list)} process instance(s)). "
                    f"Action: {profile['action']}"
                )
            else:
                # Background system or tool process
                rec_text = (
                    f"Background process '{name}' is consuming ~{total_ws_mb:.0f} MB resident RAM. "
                    f"If this is an active tool, consider closing it through its normal interface."
                )

            recommendations.append(MemoryRecommendation(
                process_name=name,
                pids=pids,
                total_working_set_mb=total_ws_mb,
                total_private_commit_mb=total_priv_mb,
                is_user_facing=is_user_facing,
                estimated_reclaimable_ram_mb=estimated_reclaim_mb,
                recommendation_text=rec_text
            ))

        return recommendations
