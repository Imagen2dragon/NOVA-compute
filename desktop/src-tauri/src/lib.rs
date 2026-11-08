pub mod admission;
pub mod chat;
pub mod job_object;
pub mod memory_budget;
pub mod memory_cleaner;
pub mod memory_watchdog;
pub mod model_integrity;
pub mod resource_advisor;
pub mod server_manager;
pub mod state;
pub mod storage;

#[cfg(test)]
mod tests;

use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, State};

use admission::{
    query_candidate_processes, run_preflight_audit, validate_gguf_file, GgufHeaderInfo,
    PreflightReport, ProcessCandidate,
};
use chat::{ChatRequestMessage, ChatService};
use server_manager::{ServerManager, ServerStatus};
use state::AppState;
use storage::{
    load_conversations, load_settings, log_diagnostic, save_conversations, save_settings,
    AppSettings, Conversation,
};

#[derive(Clone, Debug)]
pub struct ModelLoadAuthorization {
    pub token: String,
    pub model_path: String,
    pub issued_at: Instant,
    pub expires_at: Instant,
}

pub struct AppContext {
    pub current_state: Mutex<AppState>,
    pub server_manager: Mutex<ServerManager>,
    pub chat_service: Arc<ChatService>,
    pub active_model_path: Mutex<String>,
    pub lifecycle_lock: tokio::sync::Mutex<()>,
    pub is_unloading: Arc<AtomicBool>,
    pub model_load_authorization: Mutex<Option<ModelLoadAuthorization>>,
    pub integrity_state: Arc<RwLock<model_integrity::ModelIntegrityState>>,
    pub integrity_cancel: Arc<AtomicBool>,
    pub active_model_lock: Arc<Mutex<Option<crate::model_integrity::ModelFileLockGuard>>>,
    /// Incremented on every model start and unload; a watchdog task exits when it changes.
    pub watchdog_epoch: Arc<AtomicU64>,
    /// Reason the memory watchdog last stopped the model, if any.
    pub watchdog_reason: Arc<Mutex<Option<String>>>,
    /// Last automatic memory assist (pre-load), for its 30 s cooldown.
    pub last_auto_assist: Mutex<Option<Instant>>,
    /// Minimum free RAM and maximum system commit observed by watchdog during model load.
    pub watchdog_load_metrics: Arc<Mutex<(Option<f64>, Option<f64>)>>,
}

/// Starts the live memory watchdog for the server started in `epoch`. It samples free RAM
/// and commit every `interval_ms` and stops the server (never any other process) when
/// `MemoryWatchdog` says so. It exits when the epoch changes or no server is tracked.
fn start_memory_watchdog(app_handle: AppHandle, epoch: u64) {
    tauri::async_runtime::spawn(async move {
        let policy = memory_watchdog::WatchdogPolicy::default();
        let mut watchdog = memory_watchdog::MemoryWatchdog::new();
        let auto_assist = load_settings().map(|s| s.auto_memory_assist).unwrap_or(true);
        let t0 = Instant::now();
        loop {
            tokio::time::sleep(Duration::from_millis(policy.interval_ms)).await;
            let ctx = app_handle.state::<AppContext>();
            if ctx.watchdog_epoch.load(Ordering::SeqCst) != epoch {
                break;
            }
            if !ctx.server_manager.lock().unwrap().has_child() {
                break;
            }
            let sample = admission::query_system_memory().map(|(avail, _total, commit)| (avail, commit));
            let t = t0.elapsed().as_secs_f64();

            // Automatic memory assist (ADR-004): give the model room by trimming other apps
            // (never NOVA, llama-server or the desktop), at most once per cooldown.
            if let Ok((avail, _)) = sample {
                if auto_assist && watchdog.should_assist(&policy, avail, t) {
                    let trimmed = tauri::async_runtime::spawn_blocking(|| {
                        memory_cleaner::trim_user_working_sets(&[]).0
                    })
                    .await
                    .unwrap_or(0);
                    let after = admission::query_system_memory().map(|(a, _, _)| a).unwrap_or(avail);
                    log_diagnostic(&format!(
                        "[AUTO_ASSIST] Runtime: free RAM {:.2} -> {:.2} GiB; trimmed {} apps",
                        avail, after, trimmed
                    ));
                    let _ = app_handle.emit(
                        "nova://auto-assist",
                        serde_json::json!({ "before_gib": avail, "after_gib": after, "trimmed": trimmed }),
                    );
                }
            }

            let maybe_reason = watchdog.observe(&policy, sample, t);
            *ctx.watchdog_load_metrics.lock().unwrap() = (watchdog.min_avail_gib_seen(), watchdog.max_commit_pct_seen());
            let Some(reason) = maybe_reason else {
                continue;
            };

            log_diagnostic(&format!("[WATCHDOG] {}", reason));
            *ctx.watchdog_reason.lock().unwrap() = Some(reason.clone());
            ctx.chat_service.stop_generation();

            // Stopping waits for process exit; keep it off the async worker threads.
            let handle = app_handle.clone();
            let reason_for_stop = reason.clone();
            let stopped_cleanly = tauri::async_runtime::spawn_blocking(move || {
                let ctx = handle.state::<AppContext>();
                let mut sm = ctx.server_manager.lock().unwrap();
                let ok = sm.stop_server();
                sm.mark_ready(false, Some(reason_for_stop));
                ok
            })
            .await
            .unwrap_or(false);

            *ctx.current_state.lock().unwrap() = AppState::Error;
            let _ = app_handle.emit("nova://state-change", "ERROR");
            let _ = app_handle.emit(
                "nova://watchdog-abort",
                serde_json::json!({ "reason": reason, "stopped_cleanly": stopped_cleanly }),
            );
            break;
        }
    });
}

#[tauri::command]
fn get_app_version() -> String {
    "0.1.0".to_string()
}

#[tauri::command]
fn get_initial_view() -> Option<String> {
    std::env::var("NOVA_CHAT_VIEW").ok()
}

#[tauri::command]
fn get_app_state(context: State<'_, AppContext>) -> AppState {
    *context.current_state.lock().unwrap()
}

#[tauri::command]
fn get_server_status(context: State<'_, AppContext>) -> ServerStatus {
    context.server_manager.lock().unwrap().get_status()
}

#[tauri::command]
async fn check_admission(
    model_path: Option<String>,
    context: State<'_, AppContext>,
) -> Result<PreflightReport, String> {
    let settings = load_settings().unwrap_or_default();
    let path = model_path
        .filter(|p| !p.trim().is_empty())
        .unwrap_or(settings.model_path);

    // Off the UI thread: the audit is slow enough to freeze the window.
    let audit = tauri::async_runtime::spawn_blocking(move || run_preflight_audit(&path))
        .await
        .map_err(|e| format!("Hardware check failed: {}", e))?;

    let mut current_state = context.current_state.lock().unwrap();
    if *current_state != AppState::ModelReady && *current_state != AppState::Generating {
        if !audit.all_passed {
            // Invalidate any existing authorization if preflight fails
            *context.model_load_authorization.lock().unwrap() = None;
            if audit.ram_deficit_gib > 0.0 || audit.avail_ram_gib < 10.0 {
                *current_state = AppState::LowMemoryWait;
            } else {
                *current_state = AppState::PreflightFailed;
            }
        } else {
            *current_state = AppState::ReadyForModelConfirmation;
        }
    }

    Ok(audit)
}

/// Dedicated backend command to explicitly confirm model loading.
/// A successful preflight alone CANNOT load the model; explicit confirmation is mandatory.
#[tauri::command]
fn confirm_model_load(
    model_path: String,
    context: State<'_, AppContext>,
) -> Result<String, String> {
    if model_path.trim().is_empty() {
        return Err("Cannot confirm model load: empty model path.".to_string());
    }

    let current_state = *context.current_state.lock().unwrap();
    if current_state != AppState::ReadyForModelConfirmation {
        return Err(format!(
            "Model confirmation rejected: application is in {:?} state, must be READY_FOR_MODEL_CONFIRMATION.",
            current_state
        ));
    }

    let canonical_target = Path::new(&model_path)
        .canonicalize()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| model_path.clone());

    let token = format!("nova-auth-{}", uuid::Uuid::new_v4());
    let now = Instant::now();
    let expires = now + Duration::from_secs(60);

    let auth = ModelLoadAuthorization {
        token: token.clone(),
        model_path: canonical_target,
        issued_at: now,
        expires_at: expires,
    };

    *context.model_load_authorization.lock().unwrap() = Some(auth);
    log_diagnostic(&format!(
        "Issued single-use model load authorization token (expires in 60s) for model: {}",
        model_path
    ));

    Ok(token)
}

#[tauri::command]
fn cancel_model_confirmation(context: State<'_, AppContext>) -> Result<(), String> {
    *context.model_load_authorization.lock().unwrap() = None;
    let mut st = context.current_state.lock().unwrap();
    if *st == AppState::ReadyForModelConfirmation {
        *st = AppState::ModelUnloaded;
    }
    log_diagnostic("Model load confirmation was cancelled; authorization invalidated.");
    Ok(())
}

/// "Clean memory" button (ADR-003): trims other apps' working sets, then asks Windows for
/// admin rights (UAC) to purge the standby and modified lists. Never closes any app.
/// Refused while a model is loading or loaded, because it would evict the model's pages.
#[tauri::command]
async fn clean_memory(
    context: State<'_, AppContext>,
) -> Result<memory_cleaner::MemoryCleanReport, String> {
    {
        let st = *context.current_state.lock().unwrap();
        let model_active = matches!(
            st,
            AppState::ModelStarting
                | AppState::ModelReady
                | AppState::Generating
                | AppState::GenerationStopping
                | AppState::ModelStopping
        );
        if model_active || context.server_manager.lock().unwrap().has_child() {
            return Err(
                "Unload the model before cleaning memory; cleaning now would push the model out of RAM and slow it down."
                    .to_string(),
            );
        }
    }

    let report = tauri::async_runtime::spawn_blocking(|| {
        let before = admission::query_system_memory().map(|(a, _, _)| a).unwrap_or(0.0);
        let (trimmed, skipped) = memory_cleaner::trim_user_working_sets(&[]);
        let admin_step = memory_cleaner::run_elevated_purge();
        std::thread::sleep(Duration::from_millis(700));
        let after = admission::query_system_memory().map(|(a, _, _)| a).unwrap_or(before);
        memory_cleaner::build_report(before, after, trimmed, skipped, admin_step)
    })
    .await
    .map_err(|e| format!("Memory cleaning failed: {}", e))?;

    log_diagnostic(&format!(
        "[MEMORY_CLEAN] free RAM {:.2} -> {:.2} GiB (+{:.2}); trimmed {} processes, skipped {}; admin step: {}",
        report.avail_before_gib,
        report.avail_after_gib,
        report.freed_gib,
        report.trimmed_processes,
        report.skipped_processes,
        report.admin_step
    ));
    Ok(report)
}

#[tauri::command]
fn get_top_processes() -> Vec<ProcessCandidate> {
    query_candidate_processes()
}

#[tauri::command]
async fn load_model(
    model_path: Option<String>,
    confirmation_token: Option<String>,
    profile_id: Option<String>,
    context: State<'_, AppContext>,
    app_handle: AppHandle,
) -> Result<ServerStatus, String> {
    // 1. Serialize lifecycle operations: only one load/unload at a time
    let _lifecycle_guard = match context.lifecycle_lock.try_lock() {
        Ok(guard) => guard,
        Err(_) => return Err("Model lifecycle operation already in progress.".to_string()),
    };

    let settings = load_settings().unwrap_or_default();
    let target_path = model_path
        .filter(|p| !p.trim().is_empty())
        .unwrap_or(settings.model_path);

    if target_path.is_empty() {
        let mut st = context.current_state.lock().unwrap();
        *st = AppState::ModelNotConfigured;
        return Err(
            "No model GGUF path configured. Please select the Qwen3-30B-A3B model file."
                .to_string(),
        );
    }

    let pid = profile_id
        .as_deref()
        .map(memory_budget::RuntimeProfileId::from_str_id)
        .unwrap_or_else(|| {
            memory_budget::RuntimeProfileId::from_str_id(&settings.runtime_profile)
        });
    let active_profile = memory_budget::RuntimeProfile::find_by_id(pid);

    // 2. Validate and consume explicit backend confirmation (strictly single-use, non-reusable)
    let auth = context
        .model_load_authorization
        .lock()
        .unwrap()
        .take()
        .ok_or_else(|| {
            "Model load rejected: explicit user confirmation required. Please run admission preflight and confirm model load.".to_string()
        })?;

    if Instant::now() > auth.expires_at {
        return Err("Model load rejected: confirmation authorization has expired. Please re-confirm model load.".to_string());
    }

    if let Some(ref supplied_token) = confirmation_token {
        if supplied_token != &auth.token {
            return Err(
                "Model load rejected: confirmation token does not match authorized token."
                    .to_string(),
            );
        }
    }

    let canonical_target = Path::new(&target_path)
        .canonicalize()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| target_path.clone());

    if canonical_target != auth.model_path && target_path != auth.model_path {
        return Err(
            "Model load rejected: authorization was issued for a different model path.".to_string(),
        );
    }

    // 3. Mandatory fresh admission audit immediately prior to launch for the selected profile
    {
        let mut st = context.current_state.lock().unwrap();
        *st = AppState::PreflightRunning;
    }
    let _ = app_handle.emit("nova://state-change", "PREFLIGHT_RUNNING");

    let audit = admission::run_preflight_audit_with_profile(&target_path, Some(&active_profile));
    if !audit.all_passed {
        let mut st = context.current_state.lock().unwrap();
        if audit.ram_deficit_gib > 0.0 || audit.avail_ram_gib < active_profile.required_ram_gib {
            *st = AppState::LowMemoryWait;
            let _ = app_handle.emit("nova://state-change", "LOW_MEMORY_WAIT");
        } else {
            *st = AppState::PreflightFailed;
            let _ = app_handle.emit("nova://state-change", "PREFLIGHT_FAILED");
        }
        return Err(format!(
            "Fresh preflight admission audit failed for {}: {}",
            active_profile.name,
            audit.failure_summary()
        ));
    }

    // 4. Additional prerequisite: Validate GGUF file format and header
    validate_gguf_file(&target_path)?;

    // 4b. Mandatory Cryptographic Model Integrity Gate & TOCTOU Identity Verification:
    let expected_identity = {
        let integrity = context.integrity_state.read().unwrap();
        match &*integrity {
            crate::model_integrity::ModelIntegrityState::CryptographicallyVerified { file_identity, .. } => {
                *file_identity
            }
            crate::model_integrity::ModelIntegrityState::PreviouslyVerified { file_identity, .. } => {
                if let Some(cached) = crate::storage::load_model_cache() {
                    if !cached.is_cryptographically_verified {
                        return Err(
                            "Model integrity gate blocked: Model cache was not cryptographically verified. Full cryptographic verification (SHA-256) is required before live model load.".to_string(),
                        );
                    }
                }
                *file_identity
            }
            crate::model_integrity::ModelIntegrityState::VerificationInProgress { percent, .. } => {
                return Err(format!(
                    "Model cryptographic verification is currently in progress ({}%). Please wait for verification to complete before loading.",
                    percent
                ));
            }
            crate::model_integrity::ModelIntegrityState::VerificationFailed { reason, .. } => {
                return Err(format!(
                    "Model cryptographic integrity verification failed: {}. Live model load is blocked.",
                    reason
                ));
            }
        }
    };

    // 4c. Acquire the restrictive model lock FIRST, then verify the Windows file identity
    // through that locked handle (finding F1). Reuse the in-session verification lock when
    // it guards the same path. The verified guard is the one handed to the spawn, so the
    // file object that was checked is exactly the one that stays locked.
    let pre_acquired_lock = {
        let mut lock_slot = context.active_model_lock.lock().unwrap();
        lock_slot.take()
    };
    let verified_lock = match crate::model_integrity::acquire_verified_lock(
        &target_path,
        pre_acquired_lock,
        expected_identity.as_ref(),
    ) {
        Ok(guard) => guard,
        Err(e) => {
            let err = format!("Model integrity lock verification failed: {}", e);
            log_diagnostic(&err);
            let mut st = context.current_state.lock().unwrap();
            *st = AppState::Error;
            let _ = app_handle.emit("nova://state-change", "ERROR");
            return Err(err);
        }
    };

    // 5. Transition to MODEL_STARTING
    {
        let mut st = context.current_state.lock().unwrap();
        *st = AppState::ModelStarting;
    }
    let _ = app_handle.emit("nova://state-change", "MODEL_STARTING");

    // 6. Spawn process inside Win32 Job Object suspended, then resumed with profile
    let port = settings.server_port;
    let custom_runtime = if settings.runtime_path.trim().is_empty() {
        None
    } else {
        Some(settings.runtime_path.as_str())
    };

    let (server_port, api_key) = {
        let mut sm = context.server_manager.lock().unwrap();
        match sm.spawn_process_with_profile(
            &target_path,
            port,
            custom_runtime,
            Some(&active_profile),
            Some(verified_lock),
        ) {
            Ok(creds) => creds,
            Err(e) => {
                let mut st = context.current_state.lock().unwrap();
                *st = AppState::Error;
                let _ = app_handle.emit("nova://state-change", "ERROR");
                return Err(e);
            }
        }
    };

    // 6b. Live memory watchdog covers loading, idle and generation for this server.
    *context.watchdog_reason.lock().unwrap() = None;
    *context.watchdog_load_metrics.lock().unwrap() = (None, None);
    let epoch = context.watchdog_epoch.fetch_add(1, Ordering::SeqCst) + 1;
    start_memory_watchdog(app_handle.clone(), epoch);

    // 7. Poll readiness endpoint (/health) asynchronously with verified status semantics.
    // Streaming experts from the SSD makes the first load much slower, so the low-memory
    // profile waits longer; the watchdog, not this timeout, is what keeps the machine safe.
    let readiness_timeout_secs: u64 =
        if active_profile.id == memory_budget::RuntimeProfileId::LowMemoryStreaming {
            300
        } else {
            60
        };
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap_or_default();
    let probe_start = Instant::now();
    let mut is_ready = false;

    while probe_start.elapsed() < Duration::from_secs(readiness_timeout_secs) {
        {
            let mut sm = context.server_manager.lock().unwrap();
            if let Err(e) = sm.check_child_alive() {
                sm.stop_server();
                let mut st = context.current_state.lock().unwrap();
                *st = AppState::Error;
                let _ = app_handle.emit("nova://state-change", "ERROR");
                let watchdog = context.watchdog_reason.lock().unwrap().clone();
                return Err(match watchdog {
                    Some(reason) => format!("Memory watchdog stopped the model: {}", reason),
                    None => format!("llama-server failed during startup: {}", e),
                });
            }
        }
        if let Some(reason) = context.watchdog_reason.lock().unwrap().clone() {
            return Err(format!("Memory watchdog stopped the model: {}", reason));
        }

        match server_manager::check_endpoint_readiness(&client, server_port, &api_key).await {
            server_manager::ReadinessStatus::Ready => {
                is_ready = true;
                break;
            }
            server_manager::ReadinessStatus::Loading => {
                // Server process is alive and still loading weights (HTTP 503 or connecting)
            }
            server_manager::ReadinessStatus::Unauthorized => {
                let mut sm = context.server_manager.lock().unwrap();
                sm.stop_server();
                let mut st = context.current_state.lock().unwrap();
                *st = AppState::Error;
                let _ = app_handle.emit("nova://state-change", "ERROR");
                return Err("llama-server rejected API key: 401 Unauthorized.".to_string());
            }
            server_manager::ReadinessStatus::Failed(err) => {
                log_diagnostic(&format!(
                    "Readiness probe returned non-ready status: {}",
                    err
                ));
            }
        }

        tokio::time::sleep(Duration::from_millis(500)).await;
    }

    let mut sm = context.server_manager.lock().unwrap();
    if is_ready {
        sm.mark_ready(true, None);
        let mut st = context.current_state.lock().unwrap();
        *st = AppState::ModelReady;
        let mut path_lock = context.active_model_path.lock().unwrap();
        *path_lock = target_path;
        let _ = app_handle.emit("nova://state-change", "MODEL_READY");
        log_diagnostic(&format!(
            "Model successfully initialized and ready on 127.0.0.1:{}",
            server_port
        ));
        let load_duration_secs = probe_start.elapsed().as_secs_f64();
        let (min_ram, max_commit) = *context.watchdog_load_metrics.lock().unwrap();
        let ram_str = min_ram.map(|r| format!("{:.2} GiB", r)).unwrap_or_else(|| "N/A".to_string());
        let commit_str = max_commit.map(|c| format!("{:.1}%", c)).unwrap_or_else(|| "N/A".to_string());
        log_diagnostic(&format!(
            "[LOAD_MEASUREMENT] Profile: '{}', Time from spawn to /health ready: {:.2}s, Lowest free RAM: {}, Highest commit: {}",
            active_profile.name,
            load_duration_secs,
            ram_str,
            commit_str
        ));
        Ok(sm.get_status())
    } else {
        let err = format!(
            "llama-server API readiness probe timed out after {} seconds.",
            readiness_timeout_secs
        );
        sm.stop_server();
        sm.mark_ready(false, Some(err.clone()));
        let mut st = context.current_state.lock().unwrap();
        *st = AppState::Error;
        let _ = app_handle.emit("nova://state-change", "ERROR");
        Err(err)
    }
}

#[tauri::command]
async fn unload_model(
    context: State<'_, AppContext>,
    app_handle: AppHandle,
) -> Result<ServerStatus, String> {
    let _lifecycle_guard = context.lifecycle_lock.lock().await;

    // Set unloading flag so any new send_chat_message is rejected
    context.is_unloading.store(true, Ordering::SeqCst);

    // Retire the memory watchdog for the server being unloaded.
    context.watchdog_epoch.fetch_add(1, Ordering::SeqCst);

    // Invalidate any active authorization
    *context.model_load_authorization.lock().unwrap() = None;

    // Await actual active generation termination and socket release (no arbitrary sleep!)
    context.chat_service.stop_generation();
    context.chat_service.await_generation_complete().await;

    {
        let mut st = context.current_state.lock().unwrap();
        *st = AppState::ModelStopping;
    }
    let _ = app_handle.emit("nova://state-change", "MODEL_STOPPING");

    let (status, stopped_cleanly) = {
        let mut sm = context.server_manager.lock().unwrap();
        let ok = sm.stop_server();
        (sm.get_status(), ok)
    };

    if stopped_cleanly {
        let mut st = context.current_state.lock().unwrap();
        *st = AppState::ModelUnloaded;
        let _ = app_handle.emit("nova://state-change", "MODEL_UNLOADED");
    } else {
        let mut st = context.current_state.lock().unwrap();
        *st = AppState::Error;
        let _ = app_handle.emit("nova://state-change", "ERROR");
    }

    context.chat_service.end_unload();
    context.is_unloading.store(false, Ordering::SeqCst);
    Ok(status)
}

#[tauri::command]
fn get_conversations() -> Result<Vec<Conversation>, String> {
    load_conversations()
}

#[tauri::command]
fn save_conversation(conversation: Conversation) -> Result<(), String> {
    let mut convs = load_conversations()?;
    if let Some(pos) = convs.iter().position(|c| c.id == conversation.id) {
        convs[pos] = conversation;
    } else {
        convs.insert(0, conversation);
    }
    save_conversations(&convs)
}

#[tauri::command]
fn delete_conversation(conversation_id: String) -> Result<(), String> {
    let mut convs = load_conversations()?;
    convs.retain(|c| c.id != conversation_id);
    save_conversations(&convs)
}

#[tauri::command]
fn clear_history() -> Result<(), String> {
    save_conversations(&[])
}

#[tauri::command]
fn get_settings() -> Result<AppSettings, String> {
    load_settings()
}

#[tauri::command]
fn save_settings_cmd(settings: AppSettings) -> Result<(), String> {
    save_settings(&settings)
}

#[tauri::command]
async fn send_chat_message(
    conversation_id: String,
    assistant_message_id: String,
    messages: Vec<ChatRequestMessage>,
    context: State<'_, AppContext>,
    app_handle: AppHandle,
) -> Result<String, String> {
    if context.is_unloading.load(Ordering::SeqCst) {
        return Err("Cannot send message: model is currently unloading.".to_string());
    }

    let (port, api_key) = {
        let sm = context.server_manager.lock().unwrap();
        let status = sm.get_status();
        if !status.is_ready {
            return Err(
                "Model is not loaded or ready for inference. Please load model first.".to_string(),
            );
        }
        (status.port, sm.get_api_key())
    };

    {
        let mut st = context.current_state.lock().unwrap();
        *st = AppState::Generating;
    }
    let _ = app_handle.emit("nova://state-change", "GENERATING");

    let chat_service = context.chat_service.clone();
    let result = chat_service
        .stream_completion(
            &app_handle,
            port,
            &api_key,
            conversation_id,
            assistant_message_id,
            messages,
        )
        .await;

    // Verify actual server health before restoring state: do not blindly restore MODEL_READY!
    {
        let mut st = context.current_state.lock().unwrap();
        let sm = context.server_manager.lock().unwrap();
        let status = sm.get_status();
        if status.is_ready {
            *st = AppState::ModelReady;
            let _ = app_handle.emit("nova://state-change", "MODEL_READY");
        } else if status.is_running {
            *st = AppState::Error;
            let _ = app_handle.emit("nova://state-change", "ERROR");
        } else {
            *st = AppState::ModelUnloaded;
            let _ = app_handle.emit("nova://state-change", "MODEL_UNLOADED");
        }
    }

    result
}

#[tauri::command]
async fn stop_generation(context: State<'_, AppContext>) -> Result<(), String> {
    context.chat_service.stop_generation();
    Ok(())
}

#[tauri::command]
fn validate_model_path(path: String) -> Result<GgufHeaderInfo, String> {
    validate_gguf_file(&path)
}

#[tauri::command]
fn validate_runtime_path(path: String) -> Result<String, String> {
    let candidate = server_manager::ServerManager::find_llama_server_binary(Some(&path))?;
    Ok(candidate.display().to_string())
}

#[tauri::command]
fn get_runtime_profiles() -> Vec<memory_budget::RuntimeProfile> {
    memory_budget::RuntimeProfile::get_standard_profiles()
}

#[tauri::command]
fn get_resource_budget(
    model_path: Option<String>,
    profile_id: Option<String>,
) -> memory_budget::ResourceBudgetCalculation {
    let settings = load_settings().unwrap_or_default();
    let path = model_path
        .filter(|p| !p.trim().is_empty())
        .unwrap_or(settings.model_path);
    let pid = profile_id
        .as_deref()
        .map(memory_budget::RuntimeProfileId::from_str_id)
        .unwrap_or_else(|| {
            memory_budget::RuntimeProfileId::from_str_id(&settings.runtime_profile)
        });
    let profile = memory_budget::RuntimeProfile::find_by_id(pid);
    let report = admission::run_preflight_audit_with_profile(&path, Some(&profile));
    report.budget.unwrap_or_else(|| {
        memory_budget::ResourceBudgetCalculation::evaluate(
            &profile,
            report.avail_ram_gib,
            report.total_ram_gib,
            report.commit_pct,
            report.free_vram_mib,
            report.free_disk_gib,
            0.0,
            0.0,
            false,
        )
    })
}

/// Model-related states during which other apps must not be trimmed by the pre-load assist.
fn model_is_active(context: &AppContext) -> bool {
    let st = *context.current_state.lock().unwrap();
    matches!(
        st,
        AppState::ModelStarting
            | AppState::ModelReady
            | AppState::Generating
            | AppState::GenerationStopping
            | AppState::ModelStopping
    ) || context.server_manager.lock().unwrap().has_child()
}

/// Automatic memory assist before a load (ADR-004): if RAM is short for `profile` and the
/// owner has auto assist enabled, trim other apps' memory once (cooldown 30 s), no admin.
/// Returns how much free RAM was gained (GiB), if it ran.
fn auto_assist_before_load(
    context: &AppContext,
    settings: &storage::AppSettings,
    avail_ram_gib: f64,
    required_ram_gib: f64,
) -> Option<f64> {
    if !settings.auto_memory_assist || avail_ram_gib >= required_ram_gib || model_is_active(context) {
        return None;
    }
    {
        let mut last = context.last_auto_assist.lock().unwrap();
        if let Some(t) = *last {
            if t.elapsed() < Duration::from_secs(30) {
                return None;
            }
        }
        *last = Some(Instant::now());
    }
    let (trimmed, _skipped) = memory_cleaner::trim_user_working_sets(&[]);
    std::thread::sleep(Duration::from_millis(500));
    let after = admission::query_system_memory().map(|(a, _, _)| a).unwrap_or(avail_ram_gib);
    let gained = (after - avail_ram_gib).max(0.0);
    log_diagnostic(&format!(
        "[AUTO_ASSIST] Pre-load: free RAM {:.2} -> {:.2} GiB (+{:.2}) for {:.1} GiB requirement; trimmed {} apps",
        avail_ram_gib, after, gained, required_ram_gib, trimmed
    ));
    Some(gained)
}

/// Runs off the UI thread (async + spawn_blocking): the audit scans processes, initialises
/// NVML and reads the model header, which used to freeze the window on every check.
#[tauri::command]
async fn check_admission_v2(
    model_path: Option<String>,
    profile_id: Option<String>,
    app_handle: AppHandle,
    context: State<'_, AppContext>,
) -> Result<PreflightReport, String> {
    let settings = load_settings().unwrap_or_default();
    let path = model_path
        .filter(|p| !p.trim().is_empty())
        .unwrap_or(settings.model_path.clone());
    let pid = profile_id
        .as_deref()
        .map(memory_budget::RuntimeProfileId::from_str_id)
        .unwrap_or_else(|| {
            memory_budget::RuntimeProfileId::from_str_id(&settings.runtime_profile)
        });
    let profile = memory_budget::RuntimeProfile::find_by_id(pid);

    let handle = app_handle.clone();
    let (path_c, profile_c, settings_c) = (path.clone(), profile.clone(), settings.clone());
    let audit = tauri::async_runtime::spawn_blocking(move || {
        let first = admission::run_preflight_audit_with_profile(&path_c, Some(&profile_c));
        let ctx = handle.state::<AppContext>();
        match auto_assist_before_load(&ctx, &settings_c, first.avail_ram_gib, profile_c.required_ram_gib) {
            Some(_) => admission::run_preflight_audit_with_profile(&path_c, Some(&profile_c)),
            None => first,
        }
    })
    .await
    .map_err(|e| format!("Hardware check failed: {}", e))?;

    let mut current_state = context.current_state.lock().unwrap();
    if *current_state != AppState::ModelReady && *current_state != AppState::Generating {
        if !audit.all_passed {
            *context.model_load_authorization.lock().unwrap() = None;
            if audit.ram_deficit_gib > 0.0 || audit.avail_ram_gib < profile.required_ram_gib {
                *current_state = AppState::LowMemoryWait;
            } else {
                *current_state = AppState::PreflightFailed;
            }
        } else {
            *current_state = AppState::ReadyForModelConfirmation;
        }
    }

    Ok(audit)
}

#[tauri::command]
fn get_recovery_summary() -> resource_advisor::ResourceRecoverySummary {
    let procs = admission::query_candidate_processes();
    resource_advisor::summarize_recovery(&procs)
}

#[tauri::command]
fn get_model_cache_info() -> Option<storage::ModelCacheEntry> {
    storage::load_model_cache()
}

#[tauri::command]
fn revalidate_model_cache(model_path: String) -> Result<GgufHeaderInfo, String> {
    admission::get_or_validate_model_info(&model_path, true)
}

#[derive(serde::Serialize, serde::Deserialize)]
pub struct SystemTelemetrySnapshot {
    pub avail_ram_gib: f64,
    pub total_ram_gib: f64,
    pub commit_pct: f64,
    pub free_vram_mib: f64,
    pub total_vram_mib: f64,
    pub competing_gpu_apps: u32,
    pub free_disk_gib: f64,
}

#[tauri::command]
fn get_system_telemetry(model_path: Option<String>) -> Result<SystemTelemetrySnapshot, String> {
    let (avail_ram, total_ram, commit) = admission::query_system_memory_raw()?;
    let (free_vram, total_vram, gpu_apps) = admission::query_nvml_gpu().unwrap_or((0.0, 0.0, 0));
    let settings = load_settings().unwrap_or_default();
    let p = model_path.unwrap_or(settings.model_path);
    let free_disk = admission::query_disk_free(&p).unwrap_or(0.0);

    Ok(SystemTelemetrySnapshot {
        avail_ram_gib: (avail_ram as f64 / (1024.0 * 1024.0 * 1024.0) * 100.0).round() / 100.0,
        total_ram_gib: (total_ram as f64 / (1024.0 * 1024.0 * 1024.0) * 100.0).round() / 100.0,
        commit_pct: (commit * 10.0).round() / 10.0,
        free_vram_mib: (free_vram * 10.0).round() / 10.0,
        total_vram_mib: (total_vram * 10.0).round() / 10.0,
        competing_gpu_apps: gpu_apps,
        free_disk_gib: (free_disk * 100.0).round() / 100.0,
    })
}

#[tauri::command]
fn get_model_integrity_status(
    context: State<'_, AppContext>,
) -> crate::model_integrity::ModelIntegrityState {
    context.integrity_state.read().unwrap().clone()
}

#[tauri::command]
async fn start_model_integrity_verification(
    model_path: Option<String>,
    app_handle: AppHandle,
    context: State<'_, AppContext>,
) -> Result<crate::model_integrity::ModelIntegrityState, String> {
    let settings = load_settings().unwrap_or_default();
    let target_path = model_path
        .filter(|p| !p.trim().is_empty())
        .unwrap_or(settings.model_path);

    if target_path.trim().is_empty() {
        return Err("No model path configured for integrity verification".to_string());
    }

    {
        let current = context.integrity_state.read().unwrap();
        if let crate::model_integrity::ModelIntegrityState::VerificationInProgress { .. } = &*current {
            return Ok(current.clone());
        }
    }

    context.integrity_cancel.store(false, Ordering::Relaxed);
    let cancel_flag = Arc::clone(&context.integrity_cancel);
    let state_arc = Arc::clone(&context.integrity_state);

    {
        let mut st = state_arc.write().unwrap();
        *st = crate::model_integrity::ModelIntegrityState::VerificationInProgress {
            bytes_hashed: 0,
            total_bytes: 0,
            percent: 0,
        };
    }

    let target_clone = target_path.clone();
    let app_handle_clone = app_handle.clone();
    let state_arc_worker = Arc::clone(&state_arc);
    let active_model_lock_worker = Arc::clone(&context.active_model_lock);

    tokio::task::spawn_blocking(move || {
        let (result_state, maybe_guard) = crate::model_integrity::verify_model_integrity_with_lock(
            &target_clone,
            crate::model_integrity::EXPECTED_QWEN3_30B_SHA256,
            &cancel_flag,
            |bytes_hashed, total_bytes, percent| {
                let _ = app_handle_clone.emit(
                    "nova://integrity-progress",
                    serde_json::json!({
                        "bytes_hashed": bytes_hashed,
                        "total_bytes": total_bytes,
                        "percent": percent
                    }),
                );
            },
        );

        if let Some(guard) = maybe_guard {
            *active_model_lock_worker.lock().unwrap() = Some(guard);
        } else {
            *active_model_lock_worker.lock().unwrap() = None;
        }

        {
            let mut st = state_arc_worker.write().unwrap();
            *st = result_state.clone();
        }

        let _ = app_handle_clone.emit("nova://integrity-status", &result_state);
    });

    Ok(context.integrity_state.read().unwrap().clone())
}

#[tauri::command]
fn cancel_model_integrity_verification(context: State<'_, AppContext>) -> Result<(), String> {
    context.integrity_cancel.store(true, Ordering::Relaxed);
    *context.active_model_lock.lock().unwrap() = None;
    let mut st = context.integrity_state.write().unwrap();
    *st = crate::model_integrity::ModelIntegrityState::VerificationFailed {
        reason: "Cryptographic verification was canceled by user request".to_string(),
        computed_sha256: None,
        expected_sha256: crate::model_integrity::EXPECTED_QWEN3_30B_SHA256.to_string(),
    };
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let chat_service = Arc::new(ChatService::new());
    let server_manager = Mutex::new(ServerManager::new());
    let initial_settings = load_settings().unwrap_or_default();

    let initial_state = if initial_settings.model_path.is_empty() {
        AppState::ModelNotConfigured
    } else {
        AppState::ModelUnloaded
    };

    let initial_integrity = crate::model_integrity::evaluate_startup_integrity(
        &initial_settings.model_path,
        crate::storage::load_model_cache().as_ref(),
    );

    let context = AppContext {
        current_state: Mutex::new(initial_state),
        server_manager,
        chat_service,
        active_model_path: Mutex::new(initial_settings.model_path),
        lifecycle_lock: tokio::sync::Mutex::new(()),
        is_unloading: Arc::new(AtomicBool::new(false)),
        model_load_authorization: Mutex::new(None),
        integrity_state: Arc::new(RwLock::new(initial_integrity)),
        integrity_cancel: Arc::new(AtomicBool::new(false)),
        active_model_lock: Arc::new(Mutex::new(None)),
        watchdog_epoch: Arc::new(AtomicU64::new(0)),
        watchdog_reason: Arc::new(Mutex::new(None)),
        watchdog_load_metrics: Arc::new(Mutex::new((None, None))),
        last_auto_assist: Mutex::new(None),
    };

    log_diagnostic("NOVA Chat Desktop initializing...");

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(context)
        .invoke_handler(tauri::generate_handler![
            get_app_version,
            get_initial_view,
            get_app_state,
            get_server_status,
            check_admission,
            check_admission_v2,
            get_resource_budget,
            get_runtime_profiles,
            get_recovery_summary,
            get_model_cache_info,
            revalidate_model_cache,
            get_system_telemetry,
            get_model_integrity_status,
            start_model_integrity_verification,
            cancel_model_integrity_verification,
            confirm_model_load,
            cancel_model_confirmation,
            get_top_processes,
            clean_memory,
            load_model,
            unload_model,
            get_conversations,
            save_conversation,
            delete_conversation,
            clear_history,
            get_settings,
            save_settings_cmd,
            send_chat_message,
            stop_generation,
            validate_model_path,
            validate_runtime_path,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
