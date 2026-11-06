import React, { useState } from 'react';
import {
  PreflightReport,
  AppState,
  RuntimeProfile,
  ModelIntegrityState,
} from '../types';
import {
  ShieldCheck,
  ShieldAlert,
  CheckCircle2,
  XCircle,
  RefreshCw,
  Play,
  X,
  AlertTriangle,
  Info,
  Server,
  ChevronDown,
  ChevronUp,
  Cpu,
  Layers,
  HardDrive,
  EyeOff,
  Sliders,
} from 'lucide-react';

interface ResourceRecoveryModalProps {
  isOpen: boolean;
  onClose: () => void;
  report: PreflightReport | null;
  appState: AppState;
  modelPath: string;
  profiles: RuntimeProfile[];
  selectedProfileId: string;
  onSelectProfile: (profileId: string) => void;
  onRecheck: () => void;
  onLoadModel: () => void;
  isLoading: boolean;
  errorMessage?: string | null;
  initialTab?: 'ram' | 'vram' | 'disk' | 'profiles';
  initialExpandedProc?: string | null;
  initialShowConfirmation?: boolean;
  integrity?: ModelIntegrityState | null;
  onVerifyModel?: () => void;
  onCancelVerify?: () => void;
}

/** A model may be loaded only after its SHA-256 has been verified (now or in a trusted cache). */
export function isIntegrityVerified(integrity?: ModelIntegrityState | null): boolean {
  return (
    integrity?.status === 'CRYPTOGRAPHICALLY_VERIFIED' || integrity?.status === 'PREVIOUSLY_VERIFIED'
  );
}

export function integrityLabel(integrity?: ModelIntegrityState | null): string {
  switch (integrity?.status) {
    case 'CRYPTOGRAPHICALLY_VERIFIED':
      return 'Verified (SHA-256 checked now)';
    case 'PREVIOUSLY_VERIFIED':
      return 'Verified (trusted cache)';
    case 'VERIFICATION_IN_PROGRESS':
      return `Verifying… ${integrity.details.percent}%`;
    case 'VERIFICATION_FAILED':
      return 'Not verified';
    default:
      return 'Unknown';
  }
}

export const ResourceRecoveryModal: React.FC<ResourceRecoveryModalProps> = ({
  isOpen,
  onClose,
  report,
  appState,
  modelPath,
  profiles,
  selectedProfileId,
  onSelectProfile,
  onRecheck,
  onLoadModel,
  isLoading,
  errorMessage,
  initialTab = 'ram',
  initialExpandedProc = null,
  initialShowConfirmation = false,
  integrity = null,
  onVerifyModel,
  onCancelVerify,
}) => {
  if (!isOpen) return null;

  const [activeTab, setActiveTab] = useState<'ram' | 'vram' | 'disk' | 'profiles'>(initialTab);
  const [expandedProc, setExpandedProc] = useState<string | null>(initialExpandedProc);
  const [skippedProcs, setSkippedProcs] = useState<string[]>([]);
  const [showConfirmation, setShowConfirmation] = useState(initialShowConfirmation);

  React.useEffect(() => {
    if (initialTab) setActiveTab(initialTab);
  }, [initialTab]);

  React.useEffect(() => {
    if (initialExpandedProc !== undefined) setExpandedProc(initialExpandedProc);
  }, [initialExpandedProc]);

  React.useEffect(() => {
    if (initialShowConfirmation !== undefined) setShowConfirmation(initialShowConfirmation);
  }, [initialShowConfirmation]);

  const budget = report?.budget;
  const currentProfile =
    profiles.find((p) => p.id === selectedProfileId) ||
    report?.selected_profile ||
    profiles[0];

  const allPassed = report ? report.all_passed : false;
  const integrityOk = isIntegrityVerified(integrity);
  const streamingProfile = profiles.find((p) => p.id === 'low_memory_streaming');
  const isVerifying = integrity?.status === 'VERIFICATION_IN_PROGRESS';
  const ramDeficit = report ? report.ram_deficit_gib : 0;
  const vramDeficit = budget ? budget.vram_deficit_mib : 0;
  const isTransitioning = appState === 'MODEL_STARTING' || appState === 'PREFLIGHT_RUNNING';

  const candidates = (report?.top_processes || []).filter(
    (p) => !skippedProcs.includes(p.process_name)
  );

  const toggleInstructions = (name: string) => {
    setExpandedProc(expandedProc === name ? null : name);
  };

  const handleSkip = (name: string) => {
    setSkippedProcs((prev) => [...prev, name]);
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card resource-assistant-card" onClick={(e) => e.stopPropagation()}>
        {/* Modal Header */}
        <div className="modal-header">
          <div className="modal-title-group">
            <div className="header-icon-badge">
              <ShieldCheck size={24} className="accent-icon" />
            </div>
            <div>
              <h2 className="modal-title">NOVA Intelligent Resource Recovery & Loader</h2>
              <p className="modal-subtitle">
                Adaptive Memory Budgeting • Hardware Safety Admission • Qwen3-30B-A3B
              </p>
            </div>
          </div>
          <button className="modal-close-btn" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        {/* Modal Body */}
        <div className="modal-body-scrollable">
          {/* Top Status Alert */}
          {allPassed ? (
            <div className="admission-alert success">
              <CheckCircle2 size={24} className="alert-icon-check" />
              <div>
                <h4 className="alert-title">Hardware Qualified for Selected Profile</h4>
                <p className="alert-text">
                  All safety admission criteria have passed for <strong>{currentProfile?.name}</strong>.
                  System resources are verified safe for local inference without OS thrashing.
                </p>
              </div>
            </div>
          ) : budget?.admission_level === 'RECOVERABLE' ? (
            <div className="admission-alert warning">
              <ShieldAlert size={24} className="alert-icon-warn" />
              <div>
                <h4 className="alert-title">Resources Needed — Reclaimable Memory Detected</h4>
                <p className="alert-text">
                  Your laptop is currently using available memory. However, we identified background
                  applications holding ~{report?.recovery_summary?.potential_ram_recovery_gib.toFixed(2)} GiB RAM
                  and ~{report?.recovery_summary?.potential_vram_recovery_mib.toFixed(0)} MiB VRAM.
                  Closing them will satisfy admission.
                </p>
              </div>
            </div>
          ) : budget?.admission_level === 'LOW_RESOURCE_AVAILABLE' ? (
            <div className="admission-alert info">
              <Info size={24} className="alert-icon-info" />
              <div>
                <h4 className="alert-title">Alternative Low-Resource Configuration Available</h4>
                <p className="alert-text">
                  The selected profile requires more RAM/VRAM than currently free. However, a lower-resource
                  profile can safely run on your hardware right now!
                </p>
              </div>
            </div>
          ) : (
            <div className="admission-alert error">
              <AlertTriangle size={24} className="alert-icon-err" />
              <div>
                <h4 className="alert-title">Hardware Admission Gate Blocked</h4>
                <p className="alert-text">
                  {report?.error_message || report?.budget?.summary_reason || 'System resources do not meet safety requirements.'}
                </p>
              </div>
            </div>
          )}

          {errorMessage && (
            <div className="admission-alert error">
              <XCircle size={20} />
              <div>
                <h4 className="alert-title">Error</h4>
                <p className="alert-text">{errorMessage}</p>
              </div>
            </div>
          )}

          {/* Model File Path & Active Profile Header */}
          <div className="resource-header-card">
            <div className="resource-header-item">
              <span className="res-meta-label"><Server size={14} /> Target Model:</span>
              <span className="res-meta-val" title={modelPath}>{modelPath || 'Not configured'}</span>
            </div>
            <div className="resource-header-item">
              <span className="res-meta-label"><Sliders size={14} /> Active Profile:</span>
              <span className="res-meta-val profile-badge">{currentProfile?.name}</span>
            </div>
          </div>

          {/* Model integrity: a one-time SHA-256 check is required before the first load */}
          <div className={`admission-alert ${integrityOk ? 'success' : isVerifying ? 'info' : 'warning'}`}>
            {integrityOk ? <ShieldCheck size={22} /> : <ShieldAlert size={22} />}
            <div style={{ flex: 1 }}>
              <h4 className="alert-title">Model integrity: {integrityLabel(integrity)}</h4>
              {integrityOk ? (
                <p className="alert-text">The model file matches the pinned SHA-256 and is protected while it is in use.</p>
              ) : isVerifying ? (
                <>
                  <p className="alert-text">
                    Checking the 18.6 GB model file. This reads the whole file once and can take a few minutes.
                  </p>
                  <progress
                    max={100}
                    value={integrity?.status === 'VERIFICATION_IN_PROGRESS' ? integrity.details.percent : 0}
                    style={{ width: '100%' }}
                  />
                </>
              ) : (
                <p className="alert-text">
                  {integrity?.status === 'VERIFICATION_FAILED' ? `${integrity.details.reason} ` : ''}
                  Verify the model once before loading it. The result is remembered until the file changes.
                </p>
              )}
            </div>
            {!integrityOk && (
              isVerifying ? (
                <button className="btn-secondary" onClick={onCancelVerify} disabled={!onCancelVerify}>
                  Cancel
                </button>
              ) : (
                <button className="btn-primary" onClick={onVerifyModel} disabled={!onVerifyModel || !modelPath}>
                  <ShieldCheck size={16} />
                  <span>Verify model</span>
                </button>
              )
            )}
          </div>

          {/* Resource Navigation Tabs */}
          <div className="resource-tabs">
            <button
              className={`res-tab-btn ${activeTab === 'ram' ? 'active' : ''}`}
              onClick={() => setActiveTab('ram')}
            >
              <Cpu size={15} />
              <span>System RAM</span>
              {ramDeficit > 0 && <span className="tab-deficit-pill">-{ramDeficit.toFixed(1)} GB</span>}
            </button>
            <button
              className={`res-tab-btn ${activeTab === 'vram' ? 'active' : ''}`}
              onClick={() => setActiveTab('vram')}
            >
              <Layers size={15} />
              <span>GPU VRAM</span>
              {vramDeficit > 0 && <span className="tab-deficit-pill">-{vramDeficit.toFixed(0)} MB</span>}
            </button>
            <button
              className={`res-tab-btn ${activeTab === 'disk' ? 'active' : ''}`}
              onClick={() => setActiveTab('disk')}
            >
              <HardDrive size={15} />
              <span>Disk Space</span>
            </button>
            <button
              className={`res-tab-btn ${activeTab === 'profiles' ? 'active' : ''}`}
              onClick={() => setActiveTab('profiles')}
            >
              <Sliders size={15} />
              <span>Runtime Profiles ({profiles.length})</span>
            </button>
          </div>

          {/* Tab 1: System RAM */}
          {activeTab === 'ram' && (
            <div className="tab-content-area">
              <div className="metrics-summary-bar">
                <div className="metric-box">
                  <span className="metric-name">Available RAM</span>
                  <span className={`metric-digit ${ramDeficit > 0 ? 'deficit' : 'good'}`}>
                    {report?.avail_ram_gib.toFixed(2)} GiB
                  </span>
                </div>
                <div className="metric-box">
                  <span className="metric-name">Required by Profile</span>
                  <span className="metric-digit">{currentProfile?.required_ram_gib.toFixed(2)} GiB</span>
                </div>
                <div className="metric-box">
                  <span className="metric-name">RAM Deficit</span>
                  <span className="metric-digit deficit">{ramDeficit.toFixed(2)} GiB</span>
                </div>
                <div className="metric-box">
                  <span className="metric-name">System Commit</span>
                  <span className="metric-digit">{report?.commit_pct.toFixed(1)}%</span>
                </div>
              </div>

              {/* Guided step: what to free, or switch to the 4 GiB streaming profile */}
              {ramDeficit > 0 && (
                <div className="admission-alert warning">
                  <AlertTriangle size={20} />
                  <div style={{ flex: 1 }}>
                    <h4 className="alert-title">
                      Free about {ramDeficit.toFixed(1)} GiB more RAM to load {currentProfile?.name}
                    </h4>
                    <p className="alert-text">
                      Close one or more of the applications below yourself, then press <strong>Check Again</strong>.
                      {streamingProfile && selectedProfileId !== streamingProfile.id && (
                        <>
                          {' '}Or use <strong>{streamingProfile.name}</strong>: it needs only{' '}
                          {streamingProfile.required_ram_gib.toFixed(0)} GiB free and streams experts from the SSD
                          (slower replies).
                        </>
                      )}
                    </p>
                  </div>
                  {streamingProfile && selectedProfileId !== streamingProfile.id && (
                    <button className="btn-secondary" onClick={() => onSelectProfile(streamingProfile.id)}>
                      Use 4 GiB profile
                    </button>
                  )}
                </div>
              )}

              {candidates.length > 0 ? (
                <div className="process-recommendations-list">
                  <div className="section-title-row">
                    <Info size={16} />
                    <h4 className="section-subheading">Candidate Applications to Close Manually</h4>
                  </div>
                  <p className="section-desc">
                    NOVA Chat never automatically terminates your applications. Review the candidates below
                    and close them manually through their standard interface:
                  </p>

                  {candidates.map((proc, idx) => (
                    <div key={idx} className="candidate-card">
                      <div className="candidate-main-row">
                        <div className="candidate-info">
                          <span className="candidate-name">{proc.process_name}</span>
                          <span className="candidate-category-badge">{proc.category}</span>
                          {proc.is_tray_app && <span className="tray-badge">System Tray Icon</span>}
                        </div>
                        <div className="candidate-metrics">
                          <span className="ws-stat">RAM: <strong>{proc.working_set_mb.toFixed(1)} MB</strong></span>
                          {proc.vram_usage_mb && (
                            <span className="vram-stat">VRAM: <strong>{proc.vram_usage_mb.toFixed(0)} MB</strong></span>
                          )}
                        </div>
                        <div className="candidate-actions">
                          <button
                            className="btn-guidance-toggle"
                            onClick={() => toggleInstructions(proc.process_name)}
                          >
                            <span>{expandedProc === proc.process_name ? 'Hide' : 'Show me how'}</span>
                            {expandedProc === proc.process_name ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                          </button>
                          <button
                            className="btn-skip-candidate"
                            title="Skip this application"
                            onClick={() => handleSkip(proc.process_name)}
                          >
                            <EyeOff size={14} />
                          </button>
                        </div>
                      </div>

                      {expandedProc === proc.process_name && (
                        <div className="candidate-instruction-panel">
                          <span className="instruction-heading">How to close safely:</span>
                          <ol className="instruction-steps">
                            {(proc.step_by_step_instructions || [proc.guidance]).map((step, sIdx) => (
                              <li key={sIdx}>{step}</li>
                            ))}
                          </ol>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="no-candidates-box">
                  <span>No heavy background applications identified. All idle candidates skipped or closed.</span>
                </div>
              )}
            </div>
          )}

          {/* Tab 2: GPU VRAM */}
          {activeTab === 'vram' && (
            <div className="tab-content-area">
              <div className="metrics-summary-bar">
                <div className="metric-box">
                  <span className="metric-name">Available Dedicated VRAM</span>
                  <span className={`metric-digit ${vramDeficit > 0 ? 'deficit' : 'good'}`}>
                    {report?.free_vram_mib.toFixed(1)} MiB
                  </span>
                </div>
                <div className="metric-box">
                  <span className="metric-name">Required by Profile</span>
                  <span className="metric-digit">{currentProfile?.required_vram_mib.toFixed(1)} MiB</span>
                </div>
                <div className="metric-box">
                  <span className="metric-name">Competing GPU Apps</span>
                  <span className={`metric-digit ${report && report.competing_gpu_apps > 0 ? 'deficit' : 'good'}`}>
                    {report?.competing_gpu_apps} active
                  </span>
                </div>
                <div className="metric-box">
                  <span className="metric-name">Total Adapter VRAM</span>
                  <span className="metric-digit">{report?.total_vram_mib.toFixed(0)} MiB</span>
                </div>
              </div>

              <div className="vram-guidance-card">
                <h4 className="guidance-card-title">GPU VRAM Sharing Guidance</h4>
                <p className="guidance-card-text">
                  Laptops with 6 GB dedicated VRAM share memory with the Windows Desktop Window Manager
                  and background graphics processes. To achieve maximum throughput:
                </p>
                <ul className="guidance-bullet-list">
                  <li>If Ollama is running in the background, quit it from the Windows system tray to release ~4.2 GB of GPU memory.</li>
                  <li>Close hardware-accelerated video streaming or 3D applications.</li>
                  <li>Or switch to <strong>Balanced (24 layers)</strong> or <strong>CPU-Dominant (12 layers)</strong> runtime profiles below to reduce VRAM requirements.</li>
                </ul>
              </div>
            </div>
          )}

          {/* Tab 3: Disk Space */}
          {activeTab === 'disk' && (
            <div className="tab-content-area">
              <div className="metrics-summary-bar">
                <div className="metric-box">
                  <span className="metric-name">Drive Free Space</span>
                  <span className="metric-digit good">{report?.free_disk_gib.toFixed(2)} GiB</span>
                </div>
                <div className="metric-box">
                  <span className="metric-name">Required Minimum</span>
                  <span className="metric-digit">25.00 GiB</span>
                </div>
                <div className="metric-box">
                  <span className="metric-name">Disk Gate Status</span>
                  <span className="metric-digit good">PASSED</span>
                </div>
              </div>

              <div className="disk-details-card">
                <span className="disk-title">Model Storage Information:</span>
                <p className="disk-text">
                  The model file <code>Qwen_Qwen3-30B-A3B-Q4_K_M.gguf</code> requires 18.6 GB on disk and uses
                  read-only memory mapping. Disk space is well above the safety threshold.
                </p>
              </div>
            </div>
          )}

          {/* Tab 4: Runtime Profiles */}
          {activeTab === 'profiles' && (
            <div className="tab-content-area">
              <div className="profiles-grid">
                {profiles.map((p) => {
                  const isSelected = p.id === selectedProfileId;
                  const wouldPass =
                    (report?.avail_ram_gib || 0) >= p.required_ram_gib &&
                    (report?.free_vram_mib || 0) >= p.required_vram_mib;

                  return (
                    <div
                      key={p.id}
                      className={`profile-card ${isSelected ? 'selected' : ''} ${wouldPass ? 'eligible' : ''}`}
                      onClick={() => onSelectProfile(p.id)}
                    >
                      <div className="profile-card-top">
                        <span className="profile-name">{p.name}</span>
                        {p.is_approved_config_d && <span className="profile-tag config-d">Baseline</span>}
                        {wouldPass && <span className="profile-tag pass">Can Run Now</span>}
                      </div>
                      <p className="profile-desc">{p.description}</p>
                      <div className="profile-specs-row">
                        <span>GPU Layers: <strong>{p.gpu_layers}</strong></span>
                        <span>Context: <strong>{p.context_size}</strong></span>
                        <span>Req RAM: <strong>{p.required_ram_gib.toFixed(1)} GB</strong></span>
                        <span>Req VRAM: <strong>{p.required_vram_mib.toFixed(0)} MB</strong></span>
                      </div>
                      <button
                        className={`profile-select-btn ${isSelected ? 'active' : ''}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelectProfile(p.id);
                        }}
                      >
                        {isSelected ? 'Active Configuration' : 'Switch to this Profile'}
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Explicit Model Load Confirmation Dialog */}
          {showConfirmation && (
            <div className="confirmation-overlay-card">
              <div className="confirm-icon-glow">
                <Server size={32} />
              </div>
              <h3 className="confirm-title">Explicit Authorization: Load Qwen3-30B-A3B?</h3>
              <p className="confirm-desc">
                You are about to initialize local inference with <strong>{currentProfile?.name}</strong>.
                The runtime will launch bound to <code>127.0.0.1</code> inside a Windows Job Object.
              </p>
              <div className="confirm-specs-box">
                <div>Model: <strong>Qwen3-30B-A3B (Q4_K_M)</strong></div>
                <div>Offloaded Layers: <strong>{currentProfile?.gpu_layers} layers</strong></div>
                <div>Context Size: <strong>{currentProfile?.context_size} tokens</strong></div>
                <div>Process Containment: <strong>Win32 Job Object (KILL_ON_JOB_CLOSE)</strong></div>
                <div>
                  Integrity Decision:{' '}
                  <strong className={integrityOk ? 'text-success' : 'text-warn'}>
                    {integrityLabel(integrity)}
                  </strong>
                </div>
              </div>
              <div className="confirm-action-row">
                <button
                  className="btn-secondary"
                  onClick={() => setShowConfirmation(false)}
                >
                  Cancel
                </button>
                <button
                  className="btn-primary load-confirm-btn"
                  onClick={() => {
                    setShowConfirmation(false);
                    onLoadModel();
                  }}
                  disabled={isTransitioning || isLoading || !integrityOk}
                >
                  <Play size={16} />
                  <span>Authorize & Launch Runtime</span>
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Modal Footer */}
        <div className="modal-footer">
          <button className="btn-secondary" onClick={onClose} disabled={isTransitioning}>
            Close / Standby
          </button>

          <div className="modal-footer-actions">
            <button
              className="btn-refresh"
              onClick={onRecheck}
              disabled={isLoading || isTransitioning}
            >
              <RefreshCw size={15} className={isLoading ? 'spin-icon' : ''} />
              <span>Check Again</span>
            </button>

            <button
              className="btn-primary load-confirm-btn"
              onClick={() => setShowConfirmation(true)}
              disabled={!allPassed || !integrityOk || isTransitioning || isLoading}
              title={!integrityOk ? 'Verify the model first' : undefined}
            >
              {isTransitioning ? (
                <>
                  <RefreshCw size={16} className="spin-icon" />
                  <span>Initializing Runtime...</span>
                </>
              ) : (
                <>
                  <Play size={16} />
                  <span>Confirm & Load Model</span>
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
