import React from 'react';
import { PreflightReport, AppState, ServerStatus, RuntimeProfile } from '../types';
import {
  Activity,
  Cpu,
  Layers,
  HardDrive,
  Server,
  RefreshCw,
  X,
  Sliders,
  CheckCircle2,
  AlertTriangle,
} from 'lucide-react';

interface ResourceDashboardProps {
  isOpen: boolean;
  onClose: () => void;
  report: PreflightReport | null;
  serverStatus: ServerStatus | null;
  activeProfile: RuntimeProfile | null;
  appState: AppState;
  onRefresh: () => void;
  isLoading: boolean;
}

export const ResourceDashboard: React.FC<ResourceDashboardProps> = ({
  isOpen,
  onClose,
  report,
  serverStatus,
  activeProfile,
  appState,
  onRefresh,
  isLoading,
}) => {
  if (!isOpen) return null;

  const totalRam = report?.total_ram_gib || 16.0;
  const availRam = report?.avail_ram_gib || 0.0;
  const usedRam = (totalRam - availRam).toFixed(2);
  const ramPct = Math.min(100, Math.max(0, ((totalRam - availRam) / totalRam) * 100));

  const totalVram = report?.total_vram_mib || 6141.0;
  const freeVram = report?.free_vram_mib || 0.0;
  const usedVram = (totalVram - freeVram).toFixed(0);
  const vramPct = Math.min(100, Math.max(0, ((totalVram - freeVram) / totalVram) * 100));

  const commitPct = report?.commit_pct || 0.0;
  const diskFree = report?.free_disk_gib || 0.0;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card dashboard-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title-group">
            <div className="header-icon-badge">
              <Activity size={22} className="accent-icon" />
            </div>
            <div>
              <h2 className="modal-title">NOVA Resource Telemetry Dashboard</h2>
              <p className="modal-subtitle">Real-time hardware telemetry and runtime containment status</p>
            </div>
          </div>
          <div className="header-actions-group">
            <button
              className="btn-refresh-sm"
              onClick={onRefresh}
              disabled={isLoading}
              title="Refresh telemetry"
            >
              <RefreshCw size={14} className={isLoading ? 'spin-icon' : ''} />
              <span>Refresh</span>
            </button>
            <button className="modal-close-btn" onClick={onClose}>
              <X size={18} />
            </button>
          </div>
        </div>

        <div className="modal-body-scrollable">
          {/* Hardware Gauges Grid */}
          <div className="dashboard-grid">
            {/* RAM Meter */}
            <div className="dash-meter-card">
              <div className="meter-header">
                <div className="meter-label-group">
                  <Cpu size={16} />
                  <span>Host Physical RAM</span>
                </div>
                <span className="meter-value-highlight">{availRam.toFixed(2)} GiB Free</span>
              </div>
              <div className="meter-progress-track">
                <div
                  className={`meter-progress-fill ${ramPct > 85 ? 'danger' : ramPct > 70 ? 'warn' : 'good'}`}
                  style={{ width: `${ramPct}%` }}
                />
              </div>
              <div className="meter-footer-stats">
                <span>Used: {usedRam} GiB ({ramPct.toFixed(1)}%)</span>
                <span>Total: {totalRam.toFixed(1)} GiB</span>
              </div>
            </div>

            {/* VRAM Meter */}
            <div className="dash-meter-card">
              <div className="meter-header">
                <div className="meter-label-group">
                  <Layers size={16} />
                  <span>NVIDIA Dedicated VRAM</span>
                </div>
                <span className="meter-value-highlight">{freeVram.toFixed(0)} MiB Free</span>
              </div>
              <div className="meter-progress-track">
                <div
                  className={`meter-progress-fill ${vramPct > 80 ? 'danger' : vramPct > 60 ? 'warn' : 'good'}`}
                  style={{ width: `${vramPct}%` }}
                />
              </div>
              <div className="meter-footer-stats">
                <span>Used: {usedVram} MiB ({vramPct.toFixed(1)}%)</span>
                <span>Total: {totalVram.toFixed(0)} MiB</span>
              </div>
            </div>

            {/* Commit Charge Meter */}
            <div className="dash-meter-card">
              <div className="meter-header">
                <div className="meter-label-group">
                  <Activity size={16} />
                  <span>System Commit Charge</span>
                </div>
                <span className="meter-value-highlight">{commitPct.toFixed(1)}%</span>
              </div>
              <div className="meter-progress-track">
                <div
                  className={`meter-progress-fill ${commitPct >= 90 ? 'danger' : commitPct > 75 ? 'warn' : 'good'}`}
                  style={{ width: `${commitPct}%` }}
                />
              </div>
              <div className="meter-footer-stats">
                <span>Safety Threshold: &lt; 90.0%</span>
                <span>Status: {commitPct < 90 ? 'Safe' : 'Constrained'}</span>
              </div>
            </div>

            {/* Storage Meter */}
            <div className="dash-meter-card">
              <div className="meter-header">
                <div className="meter-label-group">
                  <HardDrive size={16} />
                  <span>Model Drive Storage</span>
                </div>
                <span className="meter-value-highlight">{diskFree.toFixed(1)} GiB Free</span>
              </div>
              <div className="meter-progress-track">
                <div className="meter-progress-fill good" style={{ width: '45%' }} />
              </div>
              <div className="meter-footer-stats">
                <span>Required: &ge; 25.00 GiB</span>
                <span>Status: Safe</span>
              </div>
            </div>
          </div>

          {/* Model & Runtime Status */}
          <div className="dashboard-status-row">
            <div className="dash-info-panel">
              <div className="panel-title-row">
                <Server size={15} />
                <h4>Model File & Tensor Architecture</h4>
              </div>
              <div className="panel-specs-list">
                <div className="spec-line">
                  <span>Target File:</span>
                  <strong className="truncate-val">Qwen_Qwen3-30B-A3B-Q4_K_M.gguf</strong>
                </div>
                <div className="spec-line">
                  <span>Architecture:</span>
                  <strong>MoE (579 tensors, 41 metadata entries)</strong>
                </div>
                <div className="spec-line">
                  <span>File Size:</span>
                  <strong>18,632,184,480 bytes (17.35 GiB)</strong>
                </div>
                <div className="spec-line">
                  <span>Integrity Hash:</span>
                  <strong className="code-hash">a015794bfb1d69cb...</strong>
                </div>
                <div className="spec-line">
                  <span>Cryptographic Gate:</span>
                  <strong className={report?.integrity_state?.status === 'CRYPTOGRAPHICALLY_VERIFIED' ? 'text-success' : 'text-warn'}>
                    {report?.integrity_state?.status || 'UNKNOWN (not verified)'}
                  </strong>
                </div>
              </div>
            </div>

            <div className="dash-info-panel">
              <div className="panel-title-row">
                <Sliders size={15} />
                <h4>Active Runtime Containment</h4>
              </div>
              <div className="panel-specs-list">
                <div className="spec-line">
                  <span>Selected Profile:</span>
                  <strong>{activeProfile?.name || 'Configuration D'}</strong>
                </div>
                <div className="spec-line">
                  <span>GPU Layers / Context:</span>
                  <strong>{activeProfile?.gpu_layers || 48} layers / {activeProfile?.context_size || 512} tokens</strong>
                </div>
                <div className="spec-line">
                  <span>Process Isolation:</span>
                  <strong>Win32 Job Object (KILL_ON_JOB_CLOSE)</strong>
                </div>
                <div className="spec-line">
                  <span>Application State:</span>
                  <strong>{appState}</strong>
                </div>
                <div className="spec-line">
                  <span>Server Status:</span>
                  <strong className={serverStatus?.is_ready ? 'text-success' : 'text-muted'}>
                    {serverStatus?.is_ready
                      ? `Active on 127.0.0.1:${serverStatus.port} (PID: ${serverStatus.pid})`
                      : 'Unloaded (Zero Background Resource Footprint)'}
                  </strong>
                </div>
              </div>
            </div>
          </div>

          {/* Admission Gates Summary */}
          {report && (
            <div className="dash-gates-summary">
              <h4>Safety Admission Gates Status</h4>
              <div className="mini-gates-grid">
                {report.gates.map((g, idx) => (
                  <div key={idx} className={`mini-gate-pill ${g.passed ? 'pass' : 'fail'}`}>
                    {g.passed ? <CheckCircle2 size={13} /> : <AlertTriangle size={13} />}
                    <span className="mini-gate-name">{g.gate_name}</span>
                    <span className="mini-gate-val">{g.measured_str}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="modal-footer">
          <button className="btn-secondary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
};
