use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Instant;
use uuid::Uuid;

use crate::job_object::{ContainedChild, JobObject};
use crate::storage::log_diagnostic;

/// Canonical Configuration D Server Launch Manifest
#[derive(Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ServerConfigManifest {
    pub model_path: String,
    pub n_gpu_layers: u32,
    pub n_cpu_moe: u32,
    pub moe_cache_mib: u32,
    pub ctx_size: u32,
    pub batch_size: u32,
    pub ubatch_size: u32,
    pub cache_type_k: String,
    pub cache_type_v: String,
    pub flash_attn: String,
    pub load_mode: String,
    pub host: String,
    pub port: u16,
    pub api_key: String,
}

impl std::fmt::Debug for ServerConfigManifest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ServerConfigManifest")
            .field("model_path", &self.model_path)
            .field("n_gpu_layers", &self.n_gpu_layers)
            .field("n_cpu_moe", &self.n_cpu_moe)
            .field("moe_cache_mib", &self.moe_cache_mib)
            .field("ctx_size", &self.ctx_size)
            .field("batch_size", &self.batch_size)
            .field("ubatch_size", &self.ubatch_size)
            .field("cache_type_k", &self.cache_type_k)
            .field("cache_type_v", &self.cache_type_v)
            .field("flash_attn", &self.flash_attn)
            .field("load_mode", &self.load_mode)
            .field("host", &self.host)
            .field("port", &self.port)
            .field("api_key", &"[REDACTED]")
            .finish()
    }
}

impl ServerConfigManifest {
    /// Constructs the approved Configuration D baseline
    pub fn configuration_d(model_path: &str, port: u16, api_key: &str) -> Self {
        Self {
            model_path: model_path.to_string(),
            n_gpu_layers: 48,
            n_cpu_moe: 40,
            moe_cache_mib: 0,
            ctx_size: 512,
            batch_size: 64,
            ubatch_size: 16,
            cache_type_k: "q8_0".to_string(),
            cache_type_v: "q8_0".to_string(),
            flash_attn: "auto".to_string(),
            load_mode: "mmap".to_string(),
            host: "127.0.0.1".to_string(),
            port,
            api_key: api_key.to_string(),
        }
    }

    /// Constructs manifest from a selected RuntimeProfile
    pub fn from_profile(
        profile: &crate::memory_budget::RuntimeProfile,
        model_path: &str,
        port: u16,
        api_key: &str,
    ) -> Self {
        if profile.is_approved_config_d {
            return Self::configuration_d(model_path, port, api_key);
        }

        Self {
            model_path: model_path.to_string(),
            n_gpu_layers: profile.gpu_layers,
            n_cpu_moe: if profile.gpu_layers < 48 { 0 } else { 40 },
            moe_cache_mib: 0,
            ctx_size: profile.context_size,
            batch_size: profile.batch_size,
            ubatch_size: profile.ubatch_size,
            cache_type_k: profile.kv_cache_type_k.clone(),
            cache_type_v: profile.kv_cache_type_v.clone(),
            flash_attn: "auto".to_string(),
            load_mode: "mmap".to_string(),
            host: "127.0.0.1".to_string(),
            port,
            api_key: api_key.to_string(),
        }
    }

    /// Assembles the canonical, ordered argument vector
    pub fn to_args_vector(&self) -> Vec<String> {
        vec![
            "-m".to_string(),
            self.model_path.clone(),
            "-ngl".to_string(),
            self.n_gpu_layers.to_string(),
            "-ncmoe".to_string(),
            self.n_cpu_moe.to_string(),
            "--moe-cache-mib".to_string(),
            self.moe_cache_mib.to_string(),
            "-c".to_string(),
            self.ctx_size.to_string(),
            "-b".to_string(),
            self.batch_size.to_string(),
            "-ub".to_string(),
            self.ubatch_size.to_string(),
            "-ctk".to_string(),
            self.cache_type_k.clone(),
            "-ctv".to_string(),
            self.cache_type_v.clone(),
            "-fa".to_string(),
            self.flash_attn.clone(),
            "-lm".to_string(),
            self.load_mode.clone(),
            "--host".to_string(),
            self.host.clone(),
            "--port".to_string(),
            self.port.to_string(),
            "--api-key".to_string(),
            self.api_key.clone(),
        ]
    }

    /// Quotes an argument according to standard Windows command-line quoting rules
    pub fn quote_windows_arg(arg: &str) -> String {
        if arg.is_empty() {
            return "\"\"".to_string();
        }
        if !arg.contains(' ')
            && !arg.contains('\t')
            && !arg.contains('\n')
            && !arg.contains('\r')
            && !arg.contains('\"')
        {
            return arg.to_string();
        }
        let mut quoted = String::from("\"");
        let mut backslashes = 0;
        for c in arg.chars() {
            if c == '\\' {
                backslashes += 1;
            } else if c == '\"' {
                for _ in 0..backslashes * 2 + 1 {
                    quoted.push('\\');
                }
                backslashes = 0;
                quoted.push('\"');
            } else {
                for _ in 0..backslashes {
                    quoted.push('\\');
                }
                backslashes = 0;
                quoted.push(c);
            }
        }
        for _ in 0..backslashes * 2 {
            quoted.push('\\');
        }
        quoted.push('\"');
        quoted
    }

    /// Assembles the complete Windows command line string for CreateProcessW
    pub fn build_command_line(&self, exe_path: &Path) -> String {
        let mut cmd = Self::quote_windows_arg(&exe_path.to_string_lossy());
        for arg in self.to_args_vector() {
            cmd.push(' ');
            cmd.push_str(&Self::quote_windows_arg(&arg));
        }
        cmd
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum ReadinessStatus {
    Ready,
    Loading,
    Unauthorized,
    Failed(String),
}

pub async fn check_endpoint_readiness(
    client: &reqwest::Client,
    port: u16,
    api_key: &str,
) -> ReadinessStatus {
    let url = format!("http://127.0.0.1:{}/health", port);
    let res = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", api_key))
        .send()
        .await;

    match res {
        Ok(resp) => {
            let status = resp.status();
            if status == reqwest::StatusCode::UNAUTHORIZED {
                return ReadinessStatus::Unauthorized;
            }
            if status == reqwest::StatusCode::SERVICE_UNAVAILABLE {
                // 503 while model is still loading
                return ReadinessStatus::Loading;
            }
            if status.is_success() {
                match resp.json::<serde_json::Value>().await {
                    Ok(val) => {
                        if let Some(s) = val.get("status").and_then(|v| v.as_str()) {
                            if s == "ok" {
                                ReadinessStatus::Ready
                            } else if s == "loading model" {
                                ReadinessStatus::Loading
                            } else {
                                ReadinessStatus::Failed(format!(
                                    "Unexpected server status in body: {}",
                                    s
                                ))
                            }
                        } else {
                            ReadinessStatus::Failed(
                                "Malformed response: missing 'status' field".to_string(),
                            )
                        }
                    }
                    Err(e) => ReadinessStatus::Failed(format!("Malformed JSON response: {}", e)),
                }
            } else {
                ReadinessStatus::Failed(format!("Server returned HTTP {}", status))
            }
        }
        Err(e) => {
            let err_str = e.to_string().to_lowercase();
            if e.is_connect()
                || e.is_timeout()
                || err_str.contains("connection refused")
                || err_str.contains("10061")
                || err_str.contains("error sending request")
            {
                // Connection refused or not yet accepting TCP connections
                ReadinessStatus::Loading
            } else {
                ReadinessStatus::Failed(format!("HTTP transport error: {}", e))
            }
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerStatus {
    pub is_running: bool,
    pub is_ready: bool,
    pub pid: Option<u32>,
    pub port: u16,
    pub model_path: String,
    pub uptime_seconds: u64,
    pub last_error: Option<String>,
}

pub struct ServerManager {
    pub(crate) child: Option<ContainedChild>,
    pub(crate) job_object: Option<JobObject>,
    pub(crate) model_lock: Option<crate::model_integrity::ModelFileLockGuard>,
    pub(crate) port: u16,
    pub(crate) api_key: String,
    pub(crate) model_path: String,
    pub(crate) start_time: Option<Instant>,
    pub(crate) ready: bool,
    pub(crate) last_error: Option<String>,
}

impl ServerManager {
    pub fn new() -> Self {
        Self {
            child: None,
            job_object: None,
            model_lock: None,
            port: 18080,
            api_key: Uuid::new_v4().to_string(),
            model_path: String::new(),
            start_time: None,
            ready: false,
            last_error: None,
        }
    }

    pub fn get_status(&self) -> ServerStatus {
        let is_running = self.child.as_ref().map(|c| c.is_alive()).unwrap_or(false);
        let pid = self.child.as_ref().map(|c| c.pid);
        let uptime = self.start_time.map(|t| t.elapsed().as_secs()).unwrap_or(0);

        ServerStatus {
            is_running,
            is_ready: is_running && self.ready,
            pid,
            port: self.port,
            model_path: self.model_path.clone(),
            uptime_seconds: uptime,
            last_error: self.last_error.clone(),
        }
    }

    pub fn get_api_key(&self) -> String {
        self.api_key.clone()
    }

    pub fn find_llama_server_binary(custom_runtime_dir: Option<&str>) -> Result<PathBuf, String> {
        // 1. If user supplied custom runtime directory in settings
        if let Some(custom) = custom_runtime_dir {
            if !custom.trim().is_empty() {
                let custom_path = Path::new(custom);
                let candidate = if custom_path.is_file() {
                    custom_path.to_path_buf()
                } else {
                    custom_path.join("llama-server.exe")
                };
                if candidate.exists() {
                    Self::verify_runtime_dependencies(&candidate)?;
                    return Ok(candidate);
                } else {
                    return Err(format!(
                        "Specified custom runtime binary not found at: {:?}",
                        candidate
                    ));
                }
            }
        }

        // 2. Check NOVA_RUNTIME_DIR environment variable
        if let Ok(env_dir) = std::env::var("NOVA_RUNTIME_DIR") {
            let candidate = PathBuf::from(env_dir).join("llama-server.exe");
            if candidate.exists() {
                Self::verify_runtime_dependencies(&candidate)?;
                return Ok(candidate);
            }
        }

        // 3. Search relative development workspace candidates
        let candidates = [
            "runtime/windows/llama-cpp/llama-server.exe",
            "../runtime/windows/llama-cpp/llama-server.exe",
            "../../runtime/windows/llama-cpp/llama-server.exe",
            "../../../runtime/windows/llama-cpp/llama-server.exe",
        ];

        for c in &candidates {
            if let Ok(p) = std::fs::canonicalize(c) {
                if p.exists() {
                    Self::verify_runtime_dependencies(&p)?;
                    return Ok(p);
                }
            }
        }

        // 4. Check application directory (e.g. %LOCALAPPDATA%\Programs\NOVA Chat\)
        if let Ok(mut exe_dir) = std::env::current_exe() {
            exe_dir.pop();
            let relative = exe_dir.join("llama-server.exe");
            if relative.exists() {
                Self::verify_runtime_dependencies(&relative)?;
                return Ok(relative);
            }
            let sub = exe_dir
                .join("runtime")
                .join("windows")
                .join("llama-cpp")
                .join("llama-server.exe");
            if sub.exists() {
                Self::verify_runtime_dependencies(&sub)?;
                return Ok(sub);
            }
            let res = exe_dir
                .join("resources")
                .join("runtime")
                .join("windows")
                .join("llama-cpp")
                .join("llama-server.exe");
            if res.exists() {
                Self::verify_runtime_dependencies(&res)?;
                return Ok(res);
            }
        }

        Err(
            "llama-server.exe runtime binary could not be found. \
            Please place the pinned runtime in 'runtime/windows/llama-cpp/' or specify the runtime directory in Settings."
                .to_string(),
        )
    }

    pub fn verify_runtime_dependencies(server_exe: &Path) -> Result<(), String> {
        let parent = server_exe.parent().ok_or("Invalid runtime path")?;
        let required_dlls = [
            "llama-server-impl.dll",
            "llama.dll",
            "llama-common.dll",
            "mtmd.dll",
            "ggml.dll",
            "ggml-base.dll",
            "ggml-cuda.dll",
            "cublas64_12.dll",
            "cublasLt64_12.dll",
            "cudart64_12.dll",
        ];

        let mut missing = Vec::new();
        for dll in &required_dlls {
            if !parent.join(dll).exists() {
                missing.push(*dll);
            }
        }

        if !missing.is_empty() {
            return Err(format!(
                "Incomplete runtime installation in {:?}. Missing required libraries: {}",
                parent,
                missing.join(", ")
            ));
        }

        Ok(())
    }

    pub fn spawn_process(
        &mut self,
        model_path: &str,
        port: u16,
        custom_runtime_dir: Option<&str>,
    ) -> Result<(u16, String), String> {
        self.spawn_process_with_profile(model_path, port, custom_runtime_dir, None, None)
    }

    pub fn spawn_process_with_profile(
        &mut self,
        model_path: &str,
        port: u16,
        custom_runtime_dir: Option<&str>,
        profile: Option<&crate::memory_budget::RuntimeProfile>,
        existing_lock: Option<crate::model_integrity::ModelFileLockGuard>,
    ) -> Result<(u16, String), String> {
        // If already running, stop first. If termination of the previous server cannot be
        // confirmed, refuse to start another one (finding F3): the old child may still hold
        // memory and the port, and stop_server has deliberately retained its model lock.
        if !self.stop_server() {
            let err = "Refusing to start llama-server: termination of the previous server is unconfirmed; its model lock is retained. Unload the model again before starting.".to_string();
            log_diagnostic(&err);
            self.last_error = Some(err.clone());
            return Err(err);
        }

        let binary = Self::find_llama_server_binary(custom_runtime_dir)?;
        if !Path::new(model_path).exists() {
            let err = format!("Model GGUF file not found at: {}", model_path);
            self.last_error = Some(err.clone());
            return Err(err);
        }

        // Step 1 & 2: Retain pre-acquired lock from verification or acquire fresh restrictive sharing lock.
        // Opens handle with GENERIC_READ and FILE_SHARE_READ.
        // This grants llama-server read-only sharing access, but completely blocks
        // any other process from opening with write permissions or attempting rename/deletion.
        let model_lock = match existing_lock {
            Some(guard) => {
                if guard.path == model_path {
                    log_diagnostic(&format!(
                        "Retaining active restrictive model lock guard for {:?} from verification.",
                        model_path
                    ));
                    guard
                } else {
                    // Path mismatch: the supplied guard was verified for a different file.
                    // Fail closed rather than locking an unverified file (finding F1).
                    let err = format!(
                        "Model lock path mismatch: lock guards {:?} but launch targets {:?}; refusing to start unverified model.",
                        guard.path, model_path
                    );
                    log_diagnostic(&err);
                    self.last_error = Some(err.clone());
                    return Err(err);
                }
            }
            None => match crate::model_integrity::ModelFileLockGuard::acquire(model_path) {
                Ok(guard) => guard,
                Err(e) => {
                    let err = format!("TOCTOU protection failed to lock model file: {}", e);
                    log_diagnostic(&err);
                    self.last_error = Some(err.clone());
                    return Err(err);
                }
            },
        };

        self.port = port;
        self.api_key = format!("nova-{}", Uuid::new_v4());
        self.model_path = model_path.to_string();
        self.ready = false;

        log_diagnostic(&format!(
            "Preparing llama-server spawn from {:?} for model {} on 127.0.0.1:{}",
            binary, model_path, port
        ));

        // Step 7: Create Windows Job Object with KILL_ON_JOB_CLOSE
        let job = JobObject::new()?;

        // Build command line using requested profile or fallback to Configuration D
        let manifest = match profile {
            Some(p) => ServerConfigManifest::from_profile(p, model_path, port, &self.api_key),
            None => ServerConfigManifest::configuration_d(model_path, port, &self.api_key),
        };
        let cmd_line = manifest.build_command_line(&binary);

        // Spawn suspended and assign to Job Object with duplicated model lock handle before resuming execution
        #[cfg(windows)]
        let lock_raw = Some(model_lock.raw_handle());
        #[cfg(not(windows))]
        let lock_raw = None;

        let child = match job.spawn_child_suspended_with_handle(&cmd_line, lock_raw) {
            Ok(c) => c,
            Err(e) => {
                let err = format!("Failed to spawn contained child: {}", e);
                log_diagnostic(&err);
                self.last_error = Some(err.clone());
                return Err(err);
            }
        };

        let pid = child.pid;
        log_diagnostic(&format!(
            "llama-server child process safely spawned inside Job Object with duplicated model lock (PID: {}) [Step 7]",
            pid
        ));

        self.child = Some(child);
        self.job_object = Some(job);
        self.model_lock = Some(model_lock);
        self.start_time = Some(Instant::now());
        self.last_error = None;

        Ok((self.port, self.api_key.clone()))
    }

    pub fn check_child_alive(&mut self) -> Result<bool, String> {
        if let Some(ref c) = self.child {
            if c.is_alive() {
                Ok(true)
            } else {
                let err = "llama-server child process terminated prematurely.".to_string();
                self.ready = false;
                self.last_error = Some(err.clone());
                Err(err)
            }
        } else {
            Ok(false)
        }
    }

    pub fn mark_ready(&mut self, is_ready: bool, error: Option<String>) {
        self.ready = is_ready;
        self.last_error = error;
    }

    /// True while a llama-server child is tracked (running, or termination unconfirmed).
    pub fn has_child(&self) -> bool {
        self.child.is_some()
    }

    pub fn is_model_locked(&self) -> bool {
        self.model_lock.is_some()
    }

    pub fn stop_server(&mut self) -> bool {
        self.stop_server_with_timeout(5000)
    }

    pub fn stop_server_with_timeout(&mut self, timeout_ms: u32) -> bool {
        // Step 9: Terminate jobs kernel-side
        if let Some(ref job) = self.job_object {
            let _ = job.terminate_jobs();
        }

        let mut child_exited = true;
        if let Some(mut child) = self.child.take() {
            log_diagnostic(&format!(
                "Terminating llama-server child (PID: {}) and awaiting complete handle closure",
                child.pid
            ));
            let exited = child.terminate_with_timeout(timeout_ms);
            if exited {
                log_diagnostic(&format!(
                    "llama-server child (PID: {}) confirmed terminated; kernel handles closed.",
                    child.pid
                ));
            } else {
                child_exited = false;
                let err_msg = format!(
                    "WARNING: llama-server child (PID: {}) did not signal exit within wait timeout ({} ms); retaining model file lock for safety",
                    child.pid, timeout_ms
                );
                log_diagnostic(&err_msg);
                self.last_error = Some(err_msg);
                // Retain child handle because termination hasn't completed
                self.child = Some(child);
            }
        }

        // Verify entire Job Object process population has reached zero (descendants included)
        let mut job_empty = true;
        if let Some(ref job) = self.job_object {
            job_empty = job.wait_for_job_empty(timeout_ms);
            if !job_empty {
                let err_msg = format!(
                    "WARNING: Job Object still contains active processes after {} ms wait; retaining model file lock for safety",
                    timeout_ms
                );
                log_diagnostic(&err_msg);
                self.last_error = Some(err_msg);
            }
        }

        if child_exited && job_empty {
            self.job_object = None;
            // Step 10: Release lock guard ONLY after direct child and all descendant processes have stopped
            self.model_lock = None;
            self.start_time = None;
            self.ready = false;
            log_diagnostic(
                "llama-server and all descendant processes stopped cleanly via Job Object; model lock released; memory and GPU resources released.",
            );
            true
        } else {
            // Retain model lock! DO NOT DROP self.model_lock!
            self.ready = false;
            log_diagnostic(
                "Process or descendant termination unconfirmed; model file lock RETAINED to prevent file alteration.",
            );
            false
        }
    }
}

impl std::fmt::Debug for ServerManager {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ServerManager")
            .field("port", &self.port)
            .field("model_path", &self.model_path)
            .field("api_key", &"[REDACTED]")
            .field("ready", &self.ready)
            .field("last_error", &self.last_error)
            .finish()
    }
}

pub type SharedServerManager = Arc<Mutex<ServerManager>>;
