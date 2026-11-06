use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ChatMessage {
    pub id: String,
    pub role: String, // "user" | "assistant" | "system"
    pub content: String,
    pub timestamp: String,
    pub status: Option<String>, // "complete", "streaming", "cancelled", "error"
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Conversation {
    pub id: String,
    pub title: String,
    pub created_at: String,
    pub updated_at: String,
    pub messages: Vec<ChatMessage>,
}

fn default_runtime_profile() -> String {
    "gpu_focused".to_string()
}

fn default_telemetry_interval() -> u32 {
    3
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AppSettings {
    pub model_path: String,
    pub runtime_path: String,
    pub server_port: u16,
    pub theme: String,
    pub context_size: u32,
    #[serde(default = "default_runtime_profile")]
    pub runtime_profile: String,
    #[serde(default)]
    pub custom_gpu_layers: Option<u32>,
    #[serde(default)]
    pub custom_batch_size: Option<u32>,
    #[serde(default)]
    pub custom_ubatch_size: Option<u32>,
    #[serde(default = "default_telemetry_interval")]
    pub telemetry_interval_secs: u32,
}

impl Default for AppSettings {
    fn default() -> Self {
        Self {
            model_path: String::new(),
            runtime_path: String::new(),
            server_port: 18080,
            theme: "dark".to_string(),
            context_size: 512,
            runtime_profile: "gpu_focused".to_string(),
            custom_gpu_layers: None,
            custom_batch_size: None,
            custom_ubatch_size: None,
            telemetry_interval_secs: 3,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ModelCacheEntry {
    pub path: String,
    pub file_size_bytes: u64,
    pub modified_timestamp_secs: u64,
    pub sha256: String,
    pub magic: String,
    pub version: u32,
    pub tensor_count: u64,
    pub kv_count: u64,
    pub last_validated_utc: String,
    #[serde(default)]
    pub is_cryptographically_verified: bool,
    #[serde(default)]
    pub volume_serial_number: Option<u32>,
    #[serde(default)]
    pub file_index: Option<u64>,
    #[serde(default)]
    pub last_write_time_raw: Option<u64>,
}

/// Resolves the storage directory for NOVA Chat.
/// Checks `NOVA_DATA_DIR` environment variable first for testing and isolated instances.
/// Otherwise falls back to `%LOCALAPPDATA%\NOVA Chat\` on Windows.
pub fn get_app_data_dir() -> PathBuf {
    if let Ok(custom_dir) = std::env::var("NOVA_DATA_DIR") {
        let p = PathBuf::from(custom_dir);
        let _ = fs::create_dir_all(&p);
        return p;
    }

    // Test builds never touch the user's real %LOCALAPPDATA%\NOVA Chat\ (which holds the
    // model integrity cache): they use a dedicated temp directory instead.
    #[cfg(test)]
    {
        let p = std::env::temp_dir().join("nova_chat_test_appdata");
        let _ = fs::create_dir_all(&p);
        p
    }

    #[cfg(not(test))]
    {
        #[cfg(windows)]
        {
            if let Ok(local_appdata) = std::env::var("LOCALAPPDATA") {
                let path = PathBuf::from(local_appdata).join("NOVA Chat");
                let _ = fs::create_dir_all(&path);
                return path;
            }
        }
        let fallback = PathBuf::from(".").join(".nova_chat_data");
        let _ = fs::create_dir_all(&fallback);
        fallback
    }
}

/// Performs an atomic write by writing to a unique temporary file on the same directory,
/// calling sync_all, creating a .bak copy if the target exists, and renaming the temporary file.
/// If any error occurs, the temporary file is guaranteed to be cleaned up.
pub fn atomic_write(target_path: &Path, content: &[u8]) -> Result<(), String> {
    let parent = target_path
        .parent()
        .ok_or_else(|| format!("Target path {:?} has no parent directory", target_path))?;
    fs::create_dir_all(parent)
        .map_err(|e| format!("Failed to create directory {:?}: {}", parent, e))?;

    let file_name = target_path
        .file_name()
        .ok_or_else(|| format!("Target path {:?} has no file name", target_path))?
        .to_string_lossy();

    let tmp_path = parent.join(format!(".{}.tmp.{}", file_name, uuid::Uuid::new_v4()));

    // Closure to perform write and sync, ensuring cleanup on error
    let write_res = (|| -> Result<(), String> {
        let mut f = fs::File::create(&tmp_path)
            .map_err(|e| format!("Failed to create temporary file {:?}: {}", tmp_path, e))?;
        f.write_all(content)
            .map_err(|e| format!("Failed to write temporary file {:?}: {}", tmp_path, e))?;
        f.sync_all()
            .map_err(|e| format!("Failed to sync temporary file {:?}: {}", tmp_path, e))?;
        Ok(())
    })();

    if let Err(e) = write_res {
        let _ = fs::remove_file(&tmp_path);
        return Err(e);
    }

    // Preserve prior valid data via .bak before replacing target
    if target_path.exists() {
        let bak_path = parent.join(format!("{}.bak", file_name));
        if let Ok(meta) = fs::metadata(target_path) {
            if meta.len() > 0 {
                fs::copy(target_path, &bak_path).map_err(|e| {
                    let _ = fs::remove_file(&tmp_path);
                    format!("Failed to create backup at {:?}: {}", bak_path, e)
                })?;
            }
        }
    }

    fs::rename(&tmp_path, target_path).map_err(|e| {
        let _ = fs::remove_file(&tmp_path);
        format!("Failed to atomically replace {:?}: {}", target_path, e)
    })?;

    Ok(())
}

pub fn load_settings_from(dir: &Path) -> Result<AppSettings, String> {
    let settings_file = dir.join("settings.json");
    if settings_file.exists() {
        match fs::read_to_string(&settings_file) {
            Ok(contents) => match serde_json::from_str::<AppSettings>(&contents) {
                Ok(settings) => return Ok(settings),
                Err(e) => {
                    log_diagnostic(&format!("Corrupted settings.json in {:?}: {}", dir, e));
                    let bak_file = dir.join("settings.json.bak");
                    if bak_file.exists() {
                        if let Ok(bak_contents) = fs::read_to_string(&bak_file) {
                            if let Ok(recovered) =
                                serde_json::from_str::<AppSettings>(&bak_contents)
                            {
                                log_diagnostic("Successfully recovered settings from settings.json.bak; restoring primary file.");
                                // Atomically restore primary from valid backup
                                let _ = atomic_write(&settings_file, bak_contents.as_bytes());
                                return Ok(recovered);
                            }
                        }
                    }
                    return Err(format!(
                        "Settings file {:?} is corrupt and no valid backup exists: {}",
                        settings_file, e
                    ));
                }
            },
            Err(e) => {
                return Err(format!(
                    "Failed to read settings from {:?}: {}",
                    settings_file, e
                ))
            }
        }
    }

    // Auto-detect standard model and runtime paths if no configuration exists
    let mut default_settings = AppSettings::default();
    let candidate_model_paths = [
        "models/qwen3-30b/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf",
        "../models/qwen3-30b/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf",
        "../../models/qwen3-30b/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf",
    ];

    for c in &candidate_model_paths {
        if let Ok(canonical) = fs::canonicalize(c) {
            if canonical.exists() {
                default_settings.model_path = canonical.to_string_lossy().to_string();
                break;
            }
        }
    }

    let candidate_runtime_dirs = [
        "runtime/windows/llama-cpp",
        "../runtime/windows/llama-cpp",
        "../../runtime/windows/llama-cpp",
    ];

    for c in &candidate_runtime_dirs {
        if let Ok(canonical) = fs::canonicalize(c) {
            if canonical.exists() {
                default_settings.runtime_path = canonical.to_string_lossy().to_string();
                break;
            }
        }
    }

    save_settings_to(dir, &default_settings)?;
    Ok(default_settings)
}

pub fn save_settings_to(dir: &Path, settings: &AppSettings) -> Result<(), String> {
    let settings_file = dir.join("settings.json");
    let json = serde_json::to_string_pretty(settings)
        .map_err(|e| format!("Failed to serialize settings: {}", e))?;
    atomic_write(&settings_file, json.as_bytes())
}

pub fn load_conversations_from(dir: &Path) -> Result<Vec<Conversation>, String> {
    let conv_file = dir.join("conversations.json");
    if conv_file.exists() {
        match fs::read_to_string(&conv_file) {
            Ok(contents) => match serde_json::from_str::<Vec<Conversation>>(&contents) {
                Ok(convs) => return Ok(convs),
                Err(e) => {
                    log_diagnostic(&format!("Corrupted conversations.json in {:?}: {}", dir, e));
                    let bak_file = dir.join("conversations.json.bak");
                    if bak_file.exists() {
                        if let Ok(bak_contents) = fs::read_to_string(&bak_file) {
                            if let Ok(recovered) =
                                serde_json::from_str::<Vec<Conversation>>(&bak_contents)
                            {
                                log_diagnostic("Successfully recovered conversations from conversations.json.bak; restoring primary file.");
                                let _ = atomic_write(&conv_file, bak_contents.as_bytes());
                                return Ok(recovered);
                            }
                        }
                    }
                    return Err(format!(
                        "Conversations file {:?} is corrupt and no valid backup exists: {}",
                        conv_file, e
                    ));
                }
            },
            Err(e) => {
                return Err(format!(
                    "Failed to read conversations from {:?}: {}",
                    conv_file, e
                ))
            }
        }
    }
    Ok(Vec::new())
}

pub fn save_conversations_to(dir: &Path, conversations: &[Conversation]) -> Result<(), String> {
    let conv_file = dir.join("conversations.json");
    let json = serde_json::to_string_pretty(conversations)
        .map_err(|e| format!("Failed to serialize conversations: {}", e))?;
    atomic_write(&conv_file, json.as_bytes())
}

pub fn load_settings() -> Result<AppSettings, String> {
    let dir = get_app_data_dir();
    load_settings_from(&dir)
}

pub fn save_settings(settings: &AppSettings) -> Result<(), String> {
    let dir = get_app_data_dir();
    save_settings_to(&dir, settings)
}

pub fn load_conversations() -> Result<Vec<Conversation>, String> {
    let dir = get_app_data_dir();
    load_conversations_from(&dir)
}

pub fn save_conversations(conversations: &[Conversation]) -> Result<(), String> {
    let dir = get_app_data_dir();
    save_conversations_to(&dir, conversations)
}

pub fn load_model_cache_from(dir: &Path) -> Option<ModelCacheEntry> {
    let cache_file = dir.join("model_cache.json");
    if cache_file.exists() {
        if let Ok(contents) = fs::read_to_string(&cache_file) {
            if let Ok(entry) = serde_json::from_str::<ModelCacheEntry>(&contents) {
                return Some(entry);
            }
        }
    }
    None
}

pub fn save_model_cache_to(dir: &Path, entry: &ModelCacheEntry) -> Result<(), String> {
    let cache_file = dir.join("model_cache.json");
    let json = serde_json::to_string_pretty(entry)
        .map_err(|e| format!("Failed to serialize model cache: {}", e))?;
    atomic_write(&cache_file, json.as_bytes())
}

pub fn load_model_cache() -> Option<ModelCacheEntry> {
    let dir = get_app_data_dir();
    load_model_cache_from(&dir)
}

pub fn save_model_cache(entry: &ModelCacheEntry) -> Result<(), String> {
    let dir = get_app_data_dir();
    save_model_cache_to(&dir, entry)
}

pub fn clear_model_cache_from(dir: &Path) -> Result<(), String> {
    let cache_file = dir.join("model_cache.json");
    if cache_file.exists() {
        let _ = fs::remove_file(cache_file);
    }
    Ok(())
}

pub fn clear_model_cache() -> Result<(), String> {
    let dir = get_app_data_dir();
    clear_model_cache_from(&dir)
}

pub fn log_diagnostic(message: &str) {
    let log_dir = get_app_data_dir().join("logs");
    let _ = fs::create_dir_all(&log_dir);
    let log_file = log_dir.join("app.log");

    if let Ok(mut f) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_file)
    {
        let timestamp = Utc::now().to_rfc3339();
        let _ = writeln!(f, "[{}] {}", timestamp, message);
    }
}
