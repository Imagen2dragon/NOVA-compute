import React from 'react';
import { PreflightReport, AppState } from '../types';
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
} from 'lucide-react';

interface GuidedLoaderModalProps {
  isOpen: boolean;
  onClose: () => void;
  report: PreflightReport | null;
  appState: AppState;
  modelPath: string;
  onRecheck: () => void;
  onLoadModel: () => void;
  isLoading: boolean;
  errorMessage?: string | null;
}

export const GuidedLoaderModal: React.FC<GuidedLoaderModalProps> = ({
  isOpen,
  onClose,
  report,
  appState,
  modelPath,
  onRecheck,
  onLoadModel,
  isLoading,
  errorMessage,
}) => {
  if (!isOpen) return null;

  const allPassed = report ? report.all_passed : false;
  const ramDeficit = report ? report.ram_deficit_gib : 0;
  const isTransitioning = appState === 'MODEL_STARTING' || appState === 'PREFLIGHT_RUNNING';

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card guided-loader-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title-group">
            <div className="header-icon-badge">
              <ShieldCheck size={24} className="accent-icon" />
            </div>
            <div>
              <h2 className="modal-title">NOVA Guided Model Loader</h2>
              <p className="modal-subtitle">
                Hardware Qualification & Safety Admission Control • Qwen3-30B-A3B
              </p>
            </div>
          </div>
          <button className="modal-close-btn" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        <div className="modal-body-scrollable">
          {/* Top Status Alert */}
          {allPassed ? (
            <div className="admission-alert success">
              <CheckCircle2 size={22} className="alert-icon-check" />
              <div>
                <h4 className="alert-title">Hardware Qualified for Inference</h4>
                <p className="alert-text">
                  All 6 safety admission gates have passed. System resources are sufficient to safely
                  initialize Qwen3-30B-A3B under Configuration D without memory thrashing.
                </p>
              </div>
            </div>
          ) : (
            <div className="admission-alert warning">
              <ShieldAlert size={22} className="alert-icon-warn" />
              <div>
                <h4 className="alert-title">
                  {ramDeficit > 0
                    ? `Physical RAM Deficit: ${ramDeficit.toFixed(2)} GiB Needed`
                    : 'Hardware Admission Gate Blocked'}
                </h4>
                <p className="alert-text">
                  {ramDeficit > 0
                    ? `NOVA requires at least 10.00 GiB of available physical RAM to avoid operating system paging. Currently available: ${report?.avail_ram_gib.toFixed(2)} GiB.`
                    : 'One or more mandatory safety admission gates did not meet the required threshold.'}
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

          {/* Model File Path Display */}
          <div className="model-path-card">
            <div className="model-path-label">
              <Server size={14} />
              <span>Target Model Weight File:</span>
            </div>
            <div className="model-path-value">
              <span className="path-text" title={modelPath}>{modelPath || 'Not configured'}</span>
            </div>
          </div>

          {/* 6 Admission Gates Grid */}
          <div className="gates-section">
            <h3 className="section-heading">6-Gate Hardware Safety Audit</h3>
            <div className="gates-grid">
              {report?.gates.map((g, idx) => (
                <div key={idx} className={`gate-card ${g.passed ? 'passed' : 'failed'}`}>
                  <div className="gate-header">
                    <span className="gate-name">{g.gate_name}</span>
                    {g.passed ? (
                      <span className="gate-badge pass">PASS</span>
                    ) : (
                      <span className="gate-badge fail">FAIL</span>
                    )}
                  </div>
                  <div className="gate-details">
                    <div className="gate-stat">
                      <span className="stat-label">Measured:</span>
                      <span className="stat-value measured">{g.measured_str}</span>
                    </div>
                    <div className="gate-stat">
                      <span className="stat-label">Required:</span>
                      <span className="stat-value required">{g.required_str}</span>
                    </div>
                  </div>
                  {g.failure_reason && (
                    <div className="gate-failure-reason">
                      <AlertTriangle size={12} />
                      <span>{g.failure_reason}</span>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* Memory Reclamation Guidance & Candidate Applications */}
          {ramDeficit > 0 && report?.top_processes && report.top_processes.length > 0 && (
            <div className="recovery-guidance-section">
              <div className="guidance-header">
                <Info size={16} className="info-icon" />
                <h3 className="section-heading">Recommended Applications to Close Manually</h3>
              </div>
              <p className="guidance-explainer">
                Closing idle applications will return physical RAM to Windows. NOVA will never
                terminate other applications automatically. Review the candidate applications below
                and close them through their standard interface:
              </p>

              <div className="candidate-table-wrapper">
                <table className="candidate-table">
                  <thead>
                    <tr>
                      <th>Application</th>
                      <th>Category</th>
                      <th>Instances</th>
                      <th>Working Set</th>
                      <th>Private Commit</th>
                      <th>Guidance</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.top_processes.map((proc, i) => (
                      <tr key={i}>
                        <td className="proc-name">{proc.process_name}</td>
                        <td><span className="category-chip">{proc.category}</span></td>
                        <td>{proc.instances}</td>
                        <td className="num-cell">{proc.working_set_mb.toFixed(1)} MB</td>
                        <td className="num-cell">
                          {proc.private_commit_mb ? `${proc.private_commit_mb.toFixed(1)} MB` : 'N/A'}
                        </td>
                        <td className="guidance-cell">{proc.guidance}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Locked Configuration D Specifications */}
          <div className="runtime-profile-section">
            <h3 className="section-heading">Locked Runtime Configuration (Configuration D)</h3>
            <div className="profile-spec-chips">
              <div className="spec-chip">GPU Layers: <strong>48 offloaded</strong></div>
              <div className="spec-chip">MoE Experts: <strong>40 offloaded</strong></div>
              <div className="spec-chip">Expert Cache: <strong>0 MiB (Direct)</strong></div>
              <div className="spec-chip">Context Size: <strong>512 tokens</strong></div>
              <div className="spec-chip">Batch / Micro: <strong>64 / 16</strong></div>
              <div className="spec-chip">KV Cache: <strong>Q8_0 quantized</strong></div>
              <div className="spec-chip">Kernel Containment: <strong>Win32 Job Object</strong></div>
            </div>
          </div>
        </div>

        <div className="modal-footer">
          <button className="btn-secondary" onClick={onClose} disabled={isTransitioning}>
            Cancel / Standby
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
              onClick={onLoadModel}
              disabled={!allPassed || isTransitioning || isLoading}
            >
              {isTransitioning ? (
                <>
                  <RefreshCw size={16} className="spin-icon" />
                  <span>Loading Model...</span>
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
