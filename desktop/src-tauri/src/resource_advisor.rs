use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ProcessCandidate {
    pub process_name: String,
    pub instances: usize,
    pub pids: Vec<u32>,
    pub working_set_mb: f64,
    pub private_commit_mb: Option<f64>,
    pub vram_usage_mb: Option<f64>,
    pub category: String,
    pub guidance: String,
    pub step_by_step_instructions: Vec<String>,
    pub is_reclaimable: bool,
    pub is_tray_app: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResourceRecoverySummary {
    pub total_candidates: usize,
    pub potential_ram_recovery_gib: f64,
    pub potential_vram_recovery_mib: f64,
    pub candidates: Vec<ProcessCandidate>,
    pub general_advice: Vec<String>,
}

impl ProcessCandidate {
    pub fn build(
        process_name: String,
        instances: usize,
        pids: Vec<u32>,
        working_set_mb: f64,
        private_commit_mb: Option<f64>,
        vram_usage_mb: Option<f64>,
    ) -> Self {
        let name_lower = process_name.to_lowercase();

        let (category, guidance, step_by_step, is_tray) = if name_lower.contains("ollama") {
            (
                "Competing LLM Runtime".to_string(),
                "Ollama runs as a background daemon holding physical RAM and VRAM. Close via tray icon.".to_string(),
                vec![
                    "Locate the Ollama llama icon in the Windows notification area (system tray, bottom right near clock).".to_string(),
                    "Right-click the Ollama icon.".to_string(),
                    "Click 'Quit Ollama' to completely release memory and GPU allocation.".to_string(),
                ],
                true,
            )
        } else if name_lower.contains("llama-server") {
            (
                "Competing Inference Backend".to_string(),
                "Active llama.cpp inference backend process occupying GPU memory.".to_string(),
                vec![
                    "This backend is typically launched by Ollama or another local AI tool.".to_string(),
                    "Quit the parent application (such as Ollama in the system tray).".to_string(),
                ],
                false,
            )
        } else if name_lower.contains("claude") {
            (
                "AI Desktop Assistant".to_string(),
                "Electron-based desktop application holding substantial working set RAM.".to_string(),
                vec![
                    "Save any ongoing conversations in Claude.".to_string(),
                    "Close the Claude window or right-click its system tray icon and click 'Exit'.".to_string(),
                ],
                true,
            )
        } else if name_lower.contains("msedge")
            || name_lower.contains("chrome")
            || name_lower.contains("firefox")
            || name_lower.contains("brave")
        {
            (
                "Web Browser".to_string(),
                "Multi-process web browser holding substantial RAM across open tabs.".to_string(),
                vec![
                    "Bookmark or save open tabs you wish to keep.".to_string(),
                    "Close heavy background tabs (video streaming, cloud apps) or exit unused browser windows.".to_string(),
                ],
                false,
            )
        } else if name_lower.contains("code") || name_lower.contains("cursor") {
            (
                "Development IDE".to_string(),
                "Code editor holding language server memory.".to_string(),
                vec![
                    "Save all open files and commits.".to_string(),
                    "Close idle editor workspaces if not actively developing.".to_string(),
                ],
                false,
            )
        } else if name_lower.contains("slack")
            || name_lower.contains("discord")
            || name_lower.contains("teams")
        {
            (
                "Communication App".to_string(),
                "Chat application running in background.".to_string(),
                vec![
                    "Right-click the icon in the system tray and choose Quit/Exit.".to_string(),
                ],
                true,
            )
        } else {
            (
                "User Application".to_string(),
                "Review application purpose. Close through its normal window interface if unused.".to_string(),
                vec![
                    "Review whether you need this application while running local inference.".to_string(),
                    "Save work and close the window normally.".to_string(),
                ],
                false,
            )
        };

        Self {
            process_name,
            instances,
            pids,
            working_set_mb,
            private_commit_mb,
            vram_usage_mb,
            category,
            guidance,
            step_by_step_instructions: step_by_step,
            is_reclaimable: true,
            is_tray_app: is_tray,
        }
    }
}

pub fn summarize_recovery(candidates: &[ProcessCandidate]) -> ResourceRecoverySummary {
    let mut total_ram_mb = 0.0;
    let mut total_vram_mb = 0.0;

    for c in candidates {
        if c.is_reclaimable {
            // Use private commit (unshared memory) to avoid double-counting shared DLL/page memory across instances
            total_ram_mb += c.private_commit_mb.unwrap_or(c.working_set_mb);
            if let Some(vram) = c.vram_usage_mb {
                total_vram_mb += vram;
            }
        }
    }

    let general_advice = vec![
        "NOVA Chat will never automatically terminate your processes.".to_string(),
        "Closing applications through their normal interface or system tray is the safest way to return RAM to Windows.".to_string(),
        "After closing candidates, click 'Check Again' to capture fresh hardware measurements.".to_string(),
    ];

    ResourceRecoverySummary {
        total_candidates: candidates.len(),
        potential_ram_recovery_gib: total_ram_mb / 1024.0,
        potential_vram_recovery_mib: total_vram_mb,
        candidates: candidates.to_vec(),
        general_advice,
    }
}
