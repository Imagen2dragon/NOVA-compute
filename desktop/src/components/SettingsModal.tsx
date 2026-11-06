import React, { useState } from 'react';
import { AppSettings, RuntimeProfile } from '../types';
import { Settings, X, Save, Trash2, CheckCircle2, AlertCircle, Moon, Sun } from 'lucide-react';

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  settings: AppSettings;
  profiles: RuntimeProfile[];
  onSaveSettings: (settings: AppSettings) => void;
  onClearHistory: () => void;
  onValidatePath: (path: string) => Promise<any>;
  onValidateRuntime: (path: string) => Promise<string>;
}

export const SettingsModal: React.FC<SettingsModalProps> = ({
  isOpen,
  onClose,
  settings,
  profiles,
  onSaveSettings,
  onClearHistory,
  onValidatePath,
  onValidateRuntime,
}) => {
  if (!isOpen) return null;

  const [modelPath, setModelPath] = useState(settings.model_path);
  const [runtimePath, setRuntimePath] = useState(settings.runtime_path);
  const [port, setPort] = useState(settings.server_port);
  const [selectedProfile, setSelectedProfile] = useState(settings.runtime_profile || 'gpu_focused');
  const [contextSize, setContextSize] = useState(settings.context_size || 512);
  const [theme, setTheme] = useState(settings.theme || 'dark');
  const [telemetryInterval, setTelemetryInterval] = useState(settings.telemetry_interval_secs || 3);

  React.useEffect(() => {
    setModelPath(settings.model_path);
    setRuntimePath(settings.runtime_path);
    setPort(settings.server_port);
    setSelectedProfile(settings.runtime_profile || 'gpu_focused');
    setContextSize(settings.context_size || 512);
    setTheme(settings.theme || 'dark');
    setTelemetryInterval(settings.telemetry_interval_secs || 3);
  }, [settings]);

  const [pathValid, setPathValid] = useState<boolean | null>(null);
  const [pathDetails, setPathDetails] = useState<string | null>(null);
  const [validating, setValidating] = useState(false);
  const [runtimeValid, setRuntimeValid] = useState<boolean | null>(null);
  const [runtimeMsg, setRuntimeMsg] = useState<string | null>(null);
  const [validatingRuntime, setValidatingRuntime] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);

  const handleVerifyPath = async () => {
    setValidating(true);
    try {
      const res = await onValidatePath(modelPath);
      setPathValid(true);
      setPathDetails(
        `GGUF v${res.version} • ${res.tensor_count} tensors • ${(
          res.file_size_bytes /
          (1024 * 1024 * 1024)
        ).toFixed(2)} GB`
      );
    } catch (e: any) {
      setPathValid(false);
      setPathDetails(String(e));
    } finally {
      setValidating(false);
    }
  };

  const handleVerifyRuntime = async () => {
    setValidatingRuntime(true);
    try {
      const detected = await onValidateRuntime(runtimePath);
      setRuntimeValid(true);
      setRuntimeMsg(`Runtime located: ${detected}`);
    } catch (e: any) {
      setRuntimeValid(false);
      setRuntimeMsg(String(e));
    } finally {
      setValidatingRuntime(false);
    }
  };

  const handleSave = () => {
    onSaveSettings({
      ...settings,
      model_path: modelPath.trim(),
      runtime_path: runtimePath.trim(),
      server_port: Number(port) || 18080,
      runtime_profile: selectedProfile,
      context_size: Number(contextSize) || 512,
      theme,
      telemetry_interval_secs: Number(telemetryInterval) || 3,
    });
    onClose();
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card settings-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title-group">
            <div className="header-icon-badge">
              <Settings size={22} className="accent-icon" />
            </div>
            <div>
              <h2 className="modal-title">NOVA Workstation Settings</h2>
              <p className="modal-subtitle">Runtime profiles, model paths, theme, and local persistence</p>
            </div>
          </div>
          <button className="modal-close-btn" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        <div className="modal-body-scrollable">
          {/* Model Weights Path */}
          <div className="settings-field">
            <label className="field-label">GGUF Model Weights Path</label>
            <div className="input-with-action">
              <input
                type="text"
                className="text-input"
                placeholder="C:\path\to\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf"
                value={modelPath}
                onChange={(e) => {
                  setModelPath(e.target.value);
                  setPathValid(null);
                  setPathDetails(null);
                }}
              />
              <button
                className="btn-input-action"
                onClick={handleVerifyPath}
                disabled={validating || !modelPath}
              >
                {validating ? 'Checking...' : 'Verify Model'}
              </button>
            </div>
            {pathValid === true && (
              <span className="field-hint success">
                <CheckCircle2 size={13} /> Model header verified: {pathDetails}
              </span>
            )}
            {pathValid === false && (
              <span className="field-hint error">
                <AlertCircle size={13} /> {pathDetails || 'Model file not found or invalid format.'}
              </span>
            )}
          </div>

          {/* Runtime Profile Selector */}
          <div className="settings-field">
            <label className="field-label">Default Runtime Profile</label>
            <select
              className="select-input"
              value={selectedProfile}
              onChange={(e) => {
                setSelectedProfile(e.target.value);
                const found = profiles.find((p) => p.id === e.target.value);
                if (found) {
                  setContextSize(found.context_size);
                }
              }}
            >
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} — {p.gpu_layers} layers on GPU (Req: {p.required_ram_gib.toFixed(1)} GB RAM, {p.required_vram_mib.toFixed(0)} MB VRAM)
                </option>
              ))}
            </select>
            <span className="field-hint">
              Controls offloaded layers and KV cache footprint to fit within host memory budgets.
            </span>
          </div>

          {/* Context Length */}
          <div className="settings-field">
            <label className="field-label">Active Context Size (Tokens)</label>
            <select
              className="select-input"
              value={contextSize}
              onChange={(e) => setContextSize(Number(e.target.value))}
            >
              <option value={512}>512 tokens (Locked Configuration D baseline)</option>
              <option value={1024}>1024 tokens (Low memory)</option>
              <option value={2048}>2048 tokens (Balanced standard)</option>
              <option value={4096}>4096 tokens (Extended context)</option>
            </select>
            <span className="field-hint">
              Allocates Q8_0 KV cache memory per token. Lower sizes reduce VRAM allocation.
            </span>
          </div>

          {/* Pinned Runtime Path */}
          <div className="settings-field">
            <label className="field-label">Pinned llama.cpp Runtime Directory</label>
            <div className="input-with-action">
              <input
                type="text"
                className="text-input"
                placeholder="runtime\windows\llama-cpp"
                value={runtimePath}
                onChange={(e) => {
                  setRuntimePath(e.target.value);
                  setRuntimeValid(null);
                  setRuntimeMsg(null);
                }}
              />
              <button
                className="btn-input-action"
                onClick={handleVerifyRuntime}
                disabled={validatingRuntime}
              >
                {validatingRuntime ? 'Locating...' : 'Verify Runtime'}
              </button>
            </div>
            {runtimeValid === true && (
              <span className="field-hint success">
                <CheckCircle2 size={13} /> {runtimeMsg}
              </span>
            )}
            {runtimeValid === false && (
              <span className="field-hint error">
                <AlertCircle size={13} /> {runtimeMsg}
              </span>
            )}
            <span className="field-hint">
              Contains llama-server.exe, cublas64_12.dll, and CUDA 12 dependencies.
            </span>
          </div>

          {/* Theme & Refresh Interval Row */}
          <div className="settings-row-pair">
            <div className="settings-field half-width">
              <label className="field-label">Interface Theme</label>
              <div className="theme-toggle-row">
                <button
                  type="button"
                  className={`theme-chip-btn ${theme === 'dark' ? 'active' : ''}`}
                  onClick={() => setTheme('dark')}
                >
                  <Moon size={14} />
                  <span>Dark Theme</span>
                </button>
                <button
                  type="button"
                  className={`theme-chip-btn ${theme === 'light' ? 'active' : ''}`}
                  onClick={() => setTheme('light')}
                >
                  <Sun size={14} />
                  <span>Light Theme</span>
                </button>
              </div>
            </div>

            <div className="settings-field half-width">
              <label className="field-label">Internal Loopback Port</label>
              <input
                type="number"
                className="text-input"
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
              />
              <span className="field-hint">Binds strictly to 127.0.0.1.</span>
            </div>
          </div>

          {/* Telemetry Polling Interval */}
          <div className="settings-field">
            <label className="field-label">Telemetry Refresh Interval</label>
            <select
              className="select-input"
              value={telemetryInterval}
              onChange={(e) => setTelemetryInterval(Number(e.target.value))}
            >
              <option value={2}>Every 2 seconds (High responsiveness)</option>
              <option value={3}>Every 3 seconds (Default balanced)</option>
              <option value={5}>Every 5 seconds (Low CPU)</option>
              <option value={10}>Every 10 seconds (Minimal polling)</option>
            </select>
            <span className="field-hint">
              Controls background diagnostic sampling rate for hardware gauges.
            </span>
          </div>

          {/* Data Management Section */}
          <div className="danger-zone-section">
            <h4 className="danger-zone-title">Data Storage & History</h4>
            <p className="danger-zone-desc">
              All settings and chat history reside locally in <code>%LOCALAPPDATA%\NOVA Chat\</code>.
              NOVA Chat transmits no cloud telemetry.
            </p>
            {confirmClear ? (
              <div className="confirm-row">
                <span className="confirm-text">Permanently delete all chat history?</span>
                <button
                  className="btn-danger-confirm"
                  onClick={() => {
                    onClearHistory();
                    setConfirmClear(false);
                  }}
                >
                  Yes, Clear All
                </button>
                <button className="btn-secondary" onClick={() => setConfirmClear(false)}>
                  Cancel
                </button>
              </div>
            ) : (
              <button className="btn-danger" onClick={() => setConfirmClear(true)}>
                <Trash2 size={15} />
                <span>Clear All Chat History</span>
              </button>
            )}
          </div>
        </div>

        <div className="modal-footer">
          <button className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-primary" onClick={handleSave}>
            <Save size={16} />
            <span>Save Settings</span>
          </button>
        </div>
      </div>
    </div>
  );
};
