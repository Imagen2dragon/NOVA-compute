export type AppState =
  | 'APP_STARTING'
  | 'APP_READY'
  | 'SYSTEM_DIAGNOSTICS_RUNNING'
  | 'MODEL_NOT_CONFIGURED'
  | 'PREFLIGHT_RUNNING'
  | 'LOW_MEMORY_WAIT'
  | 'PREFLIGHT_FAILED'
  | 'READY_FOR_MODEL_CONFIRMATION'
  | 'MODEL_STARTING'
  | 'MODEL_READY'
  | 'GENERATING'
  | 'GENERATION_STOPPING'
  | 'MODEL_STOPPING'
  | 'MODEL_UNLOADED'
  | 'ERROR';

export interface MemoryCleanReport {
  avail_before_gib: number;
  avail_after_gib: number;
  freed_gib: number;
  trimmed_processes: number;
  skipped_processes: number;
  /** "done", "declined", "skipped", or "failed: <reason>" */
  admin_step: string;
}

export type RuntimeProfileId =
  | 'gpu_focused'
  | 'balanced'
  | 'cpu_offload'
  | 'low_memory'
  | 'low_memory_streaming'
  | 'custom';

export type ProfileTierStatus =
  | 'APPROVED_BASELINE'
  | 'SUPPORTED_HYBRID'
  | 'EXPERIMENTAL_OFFLOAD'
  | 'USER_CUSTOM';

export interface RuntimeProfile {
  id: RuntimeProfileId;
  name: string;
  description: string;
  status?: ProfileTierStatus;
  gpu_layers: number;
  context_size: number;
  batch_size: number;
  ubatch_size: number;
  kv_cache_type_k: string;
  kv_cache_type_v: string;
  split_mode: string;
  required_ram_gib: number;
  required_vram_mib: number;
  is_approved_config_d: boolean;
}

export type ModelIntegrityState =
  | { status: 'PREVIOUSLY_VERIFIED'; details: { cached_sha256: string; last_verified_utc: string } }
  | { status: 'VERIFICATION_IN_PROGRESS'; details: { bytes_hashed: number; total_bytes: number; percent: number } }
  | { status: 'CRYPTOGRAPHICALLY_VERIFIED'; details: { sha256: string; verified_at_utc: string } }
  | { status: 'VERIFICATION_FAILED'; details: { reason: string; computed_sha256?: string | null; expected_sha256: string } };

export type AdmissionLevel =
  | 'READY'
  | 'RECOVERABLE'
  | 'LOW_RESOURCE_AVAILABLE'
  | 'BLOCKED'
  | 'UNKNOWN';

export interface ResourceBudgetCalculation {
  profile: RuntimeProfile;
  admission_level: AdmissionLevel;
  required_ram_gib: number;
  avail_ram_gib: number;
  ram_deficit_gib: number;
  required_vram_mib: number;
  avail_vram_mib: number;
  vram_deficit_mib: number;
  system_commit_pct: number;
  disk_free_gib: number;
  reclaimable_ram_gib: number;
  reclaimable_vram_mib: number;
  alternative_profiles: RuntimeProfile[];
  summary_reason: string;
}

export interface GateResult {
  gate_name: string;
  passed: boolean;
  required_str: string;
  measured_str: string;
  failure_reason?: string | null;
}

export interface ProcessCandidate {
  process_name: string;
  instances: number;
  pids: number[];
  working_set_mb: number;
  private_commit_mb?: number | null;
  vram_usage_mb?: number | null;
  category: string;
  guidance: string;
  step_by_step_instructions?: string[];
  is_reclaimable?: boolean;
  is_tray_app?: boolean;
}

export interface ResourceRecoverySummary {
  total_candidates: number;
  potential_ram_recovery_gib: number;
  potential_vram_recovery_mib: number;
  candidates: ProcessCandidate[];
  general_advice: string[];
}

export interface GgufHeaderInfo {
  magic: string;
  version: number;
  tensor_count: number;
  kv_count: number;
  file_size_bytes: number;
}

export interface ModelCacheEntry {
  path: string;
  file_size_bytes: number;
  modified_timestamp_secs: number;
  sha256: string;
  magic: string;
  version: number;
  tensor_count: number;
  kv_count: number;
  last_validated_utc: string;
}

export interface SystemTelemetrySnapshot {
  avail_ram_gib: number;
  total_ram_gib: number;
  commit_pct: number;
  free_vram_mib: number;
  total_vram_mib: number;
  competing_gpu_apps: number;
  free_disk_gib: number;
}

export interface PreflightReport {
  all_passed: boolean;
  timestamp_utc: string;
  monotonic_timestamp_secs: number;
  gates: GateResult[];
  avail_ram_gib: number;
  total_ram_gib: number;
  ram_deficit_gib: number;
  commit_pct: number;
  free_vram_mib: number;
  total_vram_mib: number;
  competing_gpu_apps: number;
  free_disk_gib: number;
  top_processes: ProcessCandidate[];
  model_valid: boolean;
  model_info?: GgufHeaderInfo | null;
  error_message?: string | null;
  budget?: ResourceBudgetCalculation | null;
  recovery_summary?: ResourceRecoverySummary | null;
  selected_profile?: RuntimeProfile | null;
  integrity_state?: ModelIntegrityState | null;
}

export interface LlamaTimings {
  prompt_n?: number;
  prompt_ms?: number;
  prompt_per_second?: number;
  predicted_n?: number;
  predicted_ms?: number;
  predicted_per_second?: number;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
  status?: string | null;
  timings?: LlamaTimings | null;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  messages: ChatMessage[];
}

export interface AppSettings {
  model_path: string;
  runtime_path: string;
  server_port: number;
  theme: string;
  context_size: number;
  runtime_profile: string;
  custom_gpu_layers?: number | null;
  custom_batch_size?: number | null;
  custom_ubatch_size?: number | null;
  telemetry_interval_secs: number;
  /** Automatically trim other apps' memory when NOVA needs RAM (ADR-004). */
  auto_memory_assist?: boolean;
}

export interface ServerStatus {
  is_running: boolean;
  is_ready: boolean;
  pid?: number | null;
  port: number;
  model_path: string;
  uptime_seconds: number;
  last_error?: string | null;
}

export interface StreamTokenPayload {
  conversation_id: string;
  message_id: string;
  token: string;
  done: boolean;
  status?: string;
  error?: string | null;
  timings?: LlamaTimings | null;
}
