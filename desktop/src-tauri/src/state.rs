use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AppState {
    AppStarting,
    AppReady,
    SystemDiagnosticsRunning,
    ModelNotConfigured,
    PreflightRunning,
    LowMemoryWait,
    PreflightFailed,
    ReadyForModelConfirmation,
    ModelStarting,
    ModelReady,
    Generating,
    GenerationStopping,
    ModelStopping,
    ModelUnloaded,
    Error,
}

impl AppState {
    pub fn as_str(&self) -> &'static str {
        match self {
            AppState::AppStarting => "APP_STARTING",
            AppState::AppReady => "APP_READY",
            AppState::SystemDiagnosticsRunning => "SYSTEM_DIAGNOSTICS_RUNNING",
            AppState::ModelNotConfigured => "MODEL_NOT_CONFIGURED",
            AppState::PreflightRunning => "PREFLIGHT_RUNNING",
            AppState::LowMemoryWait => "LOW_MEMORY_WAIT",
            AppState::PreflightFailed => "PREFLIGHT_FAILED",
            AppState::ReadyForModelConfirmation => "READY_FOR_MODEL_CONFIRMATION",
            AppState::ModelStarting => "MODEL_STARTING",
            AppState::ModelReady => "MODEL_READY",
            AppState::Generating => "GENERATING",
            AppState::GenerationStopping => "GENERATION_STOPPING",
            AppState::ModelStopping => "MODEL_STOPPING",
            AppState::ModelUnloaded => "MODEL_UNLOADED",
            AppState::Error => "ERROR",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StateChangePayload {
    pub previous_state: AppState,
    pub current_state: AppState,
    pub reason: String,
    pub timestamp_utc: String,
}
