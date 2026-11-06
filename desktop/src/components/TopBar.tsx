import React from 'react';
import { AppState, ServerStatus } from '../types';
import { Play, Square, Settings, RefreshCw, Activity, Sliders } from 'lucide-react';

interface TopBarProps {
  title: string;
  appState: AppState;
  serverStatus: ServerStatus | null;
  activeProfileName?: string;
  onOpenLoader: () => void;
  onUnloadModel: () => void;
  onOpenSettings: () => void;
  onOpenDashboard: () => void;
}

export const TopBar: React.FC<TopBarProps> = ({
  title,
  appState,
  serverStatus,
  activeProfileName,
  onOpenLoader,
  onUnloadModel,
  onOpenSettings,
  onOpenDashboard,
}) => {
  const isLoaded = appState === 'MODEL_READY' || appState === 'GENERATING';
  const isTransitioning =
    appState === 'MODEL_STARTING' ||
    appState === 'PREFLIGHT_RUNNING' ||
    appState === 'MODEL_STOPPING';

  const getStatusDisplay = () => {
    switch (appState) {
      case 'MODEL_READY':
        return {
          label: 'Qwen3-30B-A3B Ready',
          detail: `${activeProfileName || 'Config D'} • Port ${serverStatus?.port ?? 18080} • Uptime ${serverStatus?.uptime_seconds ?? 0}s`,
          color: 'green',
        };
      case 'GENERATING':
        return {
          label: 'Inference Active',
          detail: 'Streaming tokens safely through 127.0.0.1',
          color: 'purple',
        };
      case 'MODEL_STARTING':
        return {
          label: 'Initializing Runtime...',
          detail: 'Starting llama-server inside Job Object',
          color: 'blue',
        };
      case 'PREFLIGHT_RUNNING':
        return {
          label: 'Auditing Resources...',
          detail: 'Evaluating RAM, VRAM, Commit & Disk',
          color: 'blue',
        };
      case 'LOW_MEMORY_WAIT':
        return {
          label: 'Resource Recovery Needed',
          detail: 'Available RAM below profile requirement',
          color: 'amber',
        };
      case 'PREFLIGHT_FAILED':
        return {
          label: 'Admission Gate Blocked',
          detail: 'System criteria not met',
          color: 'red',
        };
      case 'MODEL_NOT_CONFIGURED':
        return {
          label: 'Model Not Configured',
          detail: 'Select GGUF model in settings',
          color: 'gray',
        };
      default:
        return {
          label: 'Model Unloaded',
          detail: 'Zero memory footprint idle standby',
          color: 'gray',
        };
    }
  };

  const status = getStatusDisplay();

  return (
    <header className="topbar">
      <div className="topbar-left">
        <h1 className="active-conv-title">{title || 'New Conversation'}</h1>
        <div className={`runtime-status-pill ${status.color}`}>
          <span className={`status-indicator-dot ${status.color}`} />
          <span className="runtime-label">{status.label}</span>
          <span className="runtime-sub">{status.detail}</span>
        </div>
      </div>

      <div className="topbar-right">
        {isTransitioning ? (
          <div className="topbar-action-busy">
            <RefreshCw size={14} className="spin-icon" />
            <span>Processing...</span>
          </div>
        ) : isLoaded ? (
          <button className="topbar-btn unload-btn" onClick={onUnloadModel} title="Stop inference server and release all memory">
            <Square size={14} />
            <span>Unload Model</span>
          </button>
        ) : (
          <button className="topbar-btn load-btn" onClick={onOpenLoader} title="Open Resource Recovery & Model Loader">
            <Play size={14} />
            <span>Load Model</span>
          </button>
        )}

        <button className="topbar-btn telemetry-btn" onClick={onOpenDashboard} title="View real-time hardware telemetry dashboard">
          <Activity size={15} />
          <span>Telemetry</span>
        </button>

        <button className="topbar-btn guard-btn" onClick={onOpenLoader} title="Configure runtime profile & memory recovery">
          <Sliders size={15} />
          <span>Profiles</span>
        </button>

        <button className="topbar-icon-btn" onClick={onOpenSettings} title="Settings">
          <Settings size={16} />
        </button>
      </div>
    </header>
  );
};
