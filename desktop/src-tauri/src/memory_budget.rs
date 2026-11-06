use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeProfileId {
    GpuFocused,
    Balanced,
    CpuOffload,
    LowMemory,
    Custom,
}

impl RuntimeProfileId {
    pub fn as_str(&self) -> &'static str {
        match self {
            RuntimeProfileId::GpuFocused => "gpu_focused",
            RuntimeProfileId::Balanced => "balanced",
            RuntimeProfileId::CpuOffload => "cpu_offload",
            RuntimeProfileId::LowMemory => "low_memory",
            RuntimeProfileId::Custom => "custom",
        }
    }

    pub fn from_str_id(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "gpu_focused" | "config_d" => RuntimeProfileId::GpuFocused,
            "balanced" => RuntimeProfileId::Balanced,
            "cpu_offload" => RuntimeProfileId::CpuOffload,
            "low_memory" => RuntimeProfileId::LowMemory,
            "custom" => RuntimeProfileId::Custom,
            _ => RuntimeProfileId::GpuFocused,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ProfileTierStatus {
    ApprovedBaseline,
    SupportedHybrid,
    ExperimentalOffload,
    UserCustom,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RuntimeProfile {
    pub id: RuntimeProfileId,
    pub name: String,
    pub description: String,
    pub status: ProfileTierStatus,
    pub gpu_layers: u32,
    pub context_size: u32,
    pub batch_size: u32,
    pub ubatch_size: u32,
    pub kv_cache_type_k: String,
    pub kv_cache_type_v: String,
    pub split_mode: String,
    pub required_ram_gib: f64,
    pub required_vram_mib: f64,
    pub is_approved_config_d: bool,
}

impl RuntimeProfile {
    pub fn get_standard_profiles() -> Vec<RuntimeProfile> {
        vec![
            RuntimeProfile {
                id: RuntimeProfileId::GpuFocused,
                name: "Configuration D (GPU-Focused)".to_string(),
                description: "Approved baseline: 48 layers offloaded (40 MoE experts), direct split mode, 512 context. Requires dedicated VRAM >= 5,500 MiB.".to_string(),
                status: ProfileTierStatus::ApprovedBaseline,
                gpu_layers: 48,
                context_size: 512,
                batch_size: 64,
                ubatch_size: 16,
                kv_cache_type_k: "q8_0".to_string(),
                kv_cache_type_v: "q8_0".to_string(),
                split_mode: "direct".to_string(),
                required_ram_gib: 10.0,
                required_vram_mib: 5500.0,
                is_approved_config_d: true,
            },
            RuntimeProfile {
                id: RuntimeProfileId::Balanced,
                name: "Balanced (Hybrid Offload)".to_string(),
                description: "Supported hybrid distribution: 24 layers on GPU, 24 on CPU, 512 context. Requires VRAM >= 4,000 MiB and RAM >= 8.0 GiB.".to_string(),
                status: ProfileTierStatus::SupportedHybrid,
                gpu_layers: 24,
                context_size: 512,
                batch_size: 64,
                ubatch_size: 16,
                kv_cache_type_k: "q8_0".to_string(),
                kv_cache_type_v: "q8_0".to_string(),
                split_mode: "layer".to_string(),
                required_ram_gib: 8.0,
                required_vram_mib: 4000.0,
                is_approved_config_d: false,
            },
            RuntimeProfile {
                id: RuntimeProfileId::CpuOffload,
                name: "CPU-Dominant Offload (Experimental)".to_string(),
                description: "Experimental fallback: 12 layers on GPU, 36 on CPU, 512 context. Reduced VRAM (2,500 MiB) but increases host RAM page pressure (requires 9.5 GiB).".to_string(),
                status: ProfileTierStatus::ExperimentalOffload,
                gpu_layers: 12,
                context_size: 512,
                batch_size: 64,
                ubatch_size: 16,
                kv_cache_type_k: "q8_0".to_string(),
                kv_cache_type_v: "q8_0".to_string(),
                split_mode: "layer".to_string(),
                required_ram_gib: 9.5,
                required_vram_mib: 2500.0,
                is_approved_config_d: false,
            },
            RuntimeProfile {
                id: RuntimeProfileId::LowMemory,
                name: "Low-VRAM Fallback (Experimental)".to_string(),
                description: "Experimental emergency profile: 4 layers on GPU, 44 on CPU, 512 context. Requires minimal VRAM (1,500 MiB), but imposes heavy host memory/paging overhead (requires 10.5 GiB).".to_string(),
                status: ProfileTierStatus::ExperimentalOffload,
                gpu_layers: 4,
                context_size: 512,
                batch_size: 64,
                ubatch_size: 16,
                kv_cache_type_k: "q8_0".to_string(),
                kv_cache_type_v: "q8_0".to_string(),
                split_mode: "layer".to_string(),
                required_ram_gib: 10.5,
                required_vram_mib: 1500.0,
                is_approved_config_d: false,
            },
        ]
    }

    pub fn find_by_id(id: RuntimeProfileId) -> RuntimeProfile {
        Self::get_standard_profiles()
            .into_iter()
            .find(|p| p.id == id)
            .unwrap_or_else(|| Self::get_standard_profiles()[0].clone())
    }

    pub fn create_custom_profile(
        gpu_layers: u32,
        context_size: u32,
        batch_size: Option<u32>,
        ubatch_size: Option<u32>,
    ) -> Result<RuntimeProfile, String> {
        if context_size == 0 || context_size > 32768 {
            return Err("Context size must be between 1 and 32,768 tokens".to_string());
        }
        if gpu_layers > 48 {
            return Err("GPU layers cannot exceed 48 (total model layers for Qwen3-30B-A3B)".to_string());
        }
        let b = batch_size.unwrap_or(64);
        let ub = ubatch_size.unwrap_or(16);
        if ub > b {
            return Err("Physical micro-batch size (-ub) cannot exceed logical batch size (-b)".to_string());
        }

        let kv_cache_mib = (context_size as f64 * gpu_layers as f64 * 2.0 * 2048.0) / (1024.0 * 1024.0);
        let required_vram_mib = if gpu_layers > 0 {
            500.0 + (gpu_layers as f64 * 100.0) + kv_cache_mib + 200.0
        } else {
            0.0
        };

        let cpu_layers = 48u32.saturating_sub(gpu_layers);
        let required_ram_gib = 6.0 + (cpu_layers as f64 * 0.1) + ((context_size as f64 / 2048.0) * 0.5);

        Ok(RuntimeProfile {
            id: RuntimeProfileId::Custom,
            name: format!("Custom ({} layers, {} ctx)", gpu_layers, context_size),
            description: format!("User custom configuration: {} GPU layers, {} context.", gpu_layers, context_size),
            status: ProfileTierStatus::UserCustom,
            gpu_layers,
            context_size,
            batch_size: b,
            ubatch_size: ub,
            kv_cache_type_k: "q8_0".to_string(),
            kv_cache_type_v: "q8_0".to_string(),
            split_mode: if gpu_layers == 48 { "direct".to_string() } else { "layer".to_string() },
            required_ram_gib: (required_ram_gib * 10.0).round() / 10.0,
            required_vram_mib: required_vram_mib.round(),
            is_approved_config_d: false,
        })
    }

    pub fn build_cli_args(&self, model_path: &str, port: u16, api_key: &str) -> Vec<String> {
        let manifest = crate::server_manager::ServerConfigManifest::from_profile(self, model_path, port, api_key);
        manifest.to_args_vector()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AdmissionLevel {
    Ready,
    Recoverable,
    LowResourceAvailable,
    Blocked,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResourceBudgetCalculation {
    pub profile: RuntimeProfile,
    pub admission_level: AdmissionLevel,
    pub required_ram_gib: f64,
    pub avail_ram_gib: f64,
    pub ram_deficit_gib: f64,
    pub required_vram_mib: f64,
    pub avail_vram_mib: f64,
    pub vram_deficit_mib: f64,
    pub system_commit_pct: f64,
    pub disk_free_gib: f64,
    pub reclaimable_ram_gib: f64,
    pub reclaimable_vram_mib: f64,
    pub alternative_profiles: Vec<RuntimeProfile>,
    pub summary_reason: String,
}

impl ResourceBudgetCalculation {
    pub fn evaluate(
        profile: &RuntimeProfile,
        avail_ram_gib: f64,
        total_ram_gib: f64,
        system_commit_pct: f64,
        avail_vram_mib: f64,
        disk_free_gib: f64,
        reclaimable_ram_gib: f64,
        reclaimable_vram_mib: f64,
        telemetry_healthy: bool,
    ) -> Self {
        if !telemetry_healthy {
            return Self {
                profile: profile.clone(),
                admission_level: AdmissionLevel::Unknown,
                required_ram_gib: profile.required_ram_gib,
                avail_ram_gib,
                ram_deficit_gib: (profile.required_ram_gib - avail_ram_gib).max(0.0),
                required_vram_mib: profile.required_vram_mib,
                avail_vram_mib,
                vram_deficit_mib: (profile.required_vram_mib - avail_vram_mib).max(0.0),
                system_commit_pct,
                disk_free_gib,
                reclaimable_ram_gib,
                reclaimable_vram_mib,
                alternative_profiles: Vec::new(),
                summary_reason: "Telemetry sensors failed or unverified".to_string(),
            };
        }

        let ram_deficit = (profile.required_ram_gib - avail_ram_gib).max(0.0);
        let vram_deficit = (profile.required_vram_mib - avail_vram_mib).max(0.0);
        let commit_ok = system_commit_pct < 90.0;
        let disk_ok = disk_free_gib >= 25.0;

        let has_deficit = ram_deficit > 0.0 || vram_deficit > 0.0;
        let basic_safety_ok = commit_ok && disk_ok;

        if !basic_safety_ok {
            let reason = if !disk_ok {
                format!("Disk space {} GiB below minimum 25.0 GiB", disk_free_gib)
            } else {
                format!("System commit {:.1}% exceeds 90.0% safety threshold", system_commit_pct)
            };
            return Self {
                profile: profile.clone(),
                admission_level: AdmissionLevel::Blocked,
                required_ram_gib: profile.required_ram_gib,
                avail_ram_gib,
                ram_deficit_gib: ram_deficit,
                required_vram_mib: profile.required_vram_mib,
                avail_vram_mib,
                vram_deficit_mib: vram_deficit,
                system_commit_pct,
                disk_free_gib,
                reclaimable_ram_gib,
                reclaimable_vram_mib,
                alternative_profiles: Vec::new(),
                summary_reason: reason,
            };
        }

        // Case 1: Immediately Ready
        if !has_deficit {
            return Self {
                profile: profile.clone(),
                admission_level: AdmissionLevel::Ready,
                required_ram_gib: profile.required_ram_gib,
                avail_ram_gib,
                ram_deficit_gib: 0.0,
                required_vram_mib: profile.required_vram_mib,
                avail_vram_mib,
                vram_deficit_mib: 0.0,
                system_commit_pct,
                disk_free_gib,
                reclaimable_ram_gib,
                reclaimable_vram_mib,
                alternative_profiles: Vec::new(),
                summary_reason: "System resources satisfy selected profile requirements".to_string(),
            };
        }

        // Case 2: Check if Recoverable by user manual closing of applications
        let potential_ram = avail_ram_gib + reclaimable_ram_gib;
        let potential_vram = avail_vram_mib + reclaimable_vram_mib;
        let can_recover_selected = potential_ram >= profile.required_ram_gib && potential_vram >= profile.required_vram_mib;

        // Check alternatives among standard profiles
        let alternatives: Vec<RuntimeProfile> = RuntimeProfile::get_standard_profiles()
            .into_iter()
            .filter(|alt| {
                alt.id != profile.id && (avail_ram_gib >= alt.required_ram_gib && avail_vram_mib >= alt.required_vram_mib)
            })
            .collect();

        let alt_count_str = alternatives.len().to_string();
        if can_recover_selected {
            Self {
                profile: profile.clone(),
                admission_level: AdmissionLevel::Recoverable,
                required_ram_gib: profile.required_ram_gib,
                avail_ram_gib,
                ram_deficit_gib: ram_deficit,
                required_vram_mib: profile.required_vram_mib,
                avail_vram_mib,
                vram_deficit_mib: vram_deficit,
                system_commit_pct,
                disk_free_gib,
                reclaimable_ram_gib,
                reclaimable_vram_mib,
                alternative_profiles: alternatives,
                summary_reason: format!(
                    "Resources insufficient for {} (RAM deficit: {:.2} GiB, VRAM deficit: {:.1} MiB), but recoverable by closing candidate applications",
                    profile.name, ram_deficit, vram_deficit
                ),
            }
        } else if !alternatives.is_empty() {
            Self {
                profile: profile.clone(),
                admission_level: AdmissionLevel::LowResourceAvailable,
                required_ram_gib: profile.required_ram_gib,
                avail_ram_gib,
                ram_deficit_gib: ram_deficit,
                required_vram_mib: profile.required_vram_mib,
                avail_vram_mib,
                vram_deficit_mib: vram_deficit,
                system_commit_pct,
                disk_free_gib,
                reclaimable_ram_gib,
                reclaimable_vram_mib,
                alternative_profiles: alternatives,
                summary_reason: format!(
                    "Current resources cannot run {}, but {} lower-resource configuration(s) can run safely immediately",
                    profile.name,
                    alt_count_str
                ),
            }
        } else {
            Self {
                profile: profile.clone(),
                admission_level: AdmissionLevel::Blocked,
                required_ram_gib: profile.required_ram_gib,
                avail_ram_gib,
                ram_deficit_gib: ram_deficit,
                required_vram_mib: profile.required_vram_mib,
                avail_vram_mib,
                vram_deficit_mib: vram_deficit,
                system_commit_pct,
                disk_free_gib,
                reclaimable_ram_gib,
                reclaimable_vram_mib,
                alternative_profiles: Vec::new(),
                summary_reason: format!(
                    "Total system capacity insufficient for any configuration (available RAM: {:.2} GiB, total RAM: {:.2} GiB)",
                    avail_ram_gib, total_ram_gib
                ),
            }
        }
    }
}

