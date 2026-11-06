import { useState, useEffect, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import {
  AppState,
  ServerStatus,
  PreflightReport,
  Conversation,
  ChatMessage,
  AppSettings,
  StreamTokenPayload,
  RuntimeProfile,
} from './types';
import { Sidebar } from './components/Sidebar';
import { TopBar } from './components/TopBar';
import { ChatArea } from './components/ChatArea';
import { Composer } from './components/Composer';
import { ResourceRecoveryModal } from './components/ResourceRecoveryModal';
import { ResourceDashboard } from './components/ResourceDashboard';
import { SettingsModal } from './components/SettingsModal';
import './App.css';

export function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [appState, setAppState] = useState<AppState>('APP_STARTING');
  const [serverStatus, setServerStatus] = useState<ServerStatus | null>(null);
  const [preflightReport, setPreflightReport] = useState<PreflightReport | null>(null);
  const [profiles, setProfiles] = useState<RuntimeProfile[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState<string>('gpu_focused');
  const [settings, setSettings] = useState<AppSettings>({
    model_path: '',
    runtime_path: '',
    server_port: 18080,
    theme: 'dark',
    context_size: 512,
    runtime_profile: 'gpu_focused',
    telemetry_interval_secs: 3,
  });
  const [appVersion, setAppVersion] = useState<string>('0.1.0');
  const [isRecoveryOpen, setIsRecoveryOpen] = useState<boolean>(false);
  const [isDashboardOpen, setIsDashboardOpen] = useState<boolean>(false);
  const [isSettingsOpen, setIsSettingsOpen] = useState<boolean>(false);
  const [isLoadingPreflight, setIsLoadingPreflight] = useState<boolean>(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [recoveryTab, setRecoveryTab] = useState<'ram' | 'vram' | 'disk' | 'profiles'>('ram');
  const [recoveryExpandedProc, setRecoveryExpandedProc] = useState<string | null>(null);
  const [recoveryShowConfirmation, setRecoveryShowConfirmation] = useState<boolean>(false);

  // Keyboard navigation for deterministic view inspection and UI verification
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.altKey) {
        if (e.key === '1') {
          setIsDashboardOpen(false);
          setIsRecoveryOpen(false);
          setIsSettingsOpen(false);
        } else if (e.key === '2') {
          setIsDashboardOpen(true);
          setIsRecoveryOpen(false);
          setIsSettingsOpen(false);
        } else if (e.key === '3') {
          setIsRecoveryOpen(true);
          setIsDashboardOpen(false);
          setIsSettingsOpen(false);
          setRecoveryTab('ram');
          setRecoveryExpandedProc(null);
        } else if (e.key === '4') {
          setIsRecoveryOpen(true);
          setIsDashboardOpen(false);
          setIsSettingsOpen(false);
          setRecoveryTab('ram');
          setRecoveryExpandedProc('ollama');
        } else if (e.key === '5') {
          setIsRecoveryOpen(true);
          setIsDashboardOpen(false);
          setIsSettingsOpen(false);
          setRecoveryTab('profiles');
        } else if (e.key === '6') {
          setIsRecoveryOpen(true);
          setIsDashboardOpen(false);
          setIsSettingsOpen(false);
          setAppState('READY_FOR_MODEL_CONFIRMATION');
        } else if (e.key === '7') {
          setIsDashboardOpen(false);
          setIsRecoveryOpen(false);
          setIsSettingsOpen(false);
          setAppState('MODEL_UNLOADED');
        } else if (e.key === '8') {
          setIsSettingsOpen(true);
          setIsDashboardOpen(false);
          setIsRecoveryOpen(false);
        } else if (e.key === '9') {
          setIsRecoveryOpen(true);
          setIsDashboardOpen(false);
          setIsSettingsOpen(false);
          setAppState('LOW_MEMORY_WAIT');
          setErrorMessage('Admission Gate 2 Blocked: Available RAM 8.42 GiB < Required 10.00 GiB (Deficit: 1.58 GiB).');
        }
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  // Apply theme to document element
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', settings.theme || 'dark');
  }, [settings.theme]);

  // Fast startup initialization (non-blocking)
  useEffect(() => {
    async function init() {
      try {
        const ver = await invoke<string>('get_app_version');
        setAppVersion(ver);

        const loadedSettings = await invoke<AppSettings>('get_settings');
        setSettings(loadedSettings);
        setSelectedProfileId(loadedSettings.runtime_profile || 'gpu_focused');

        const loadedProfiles = await invoke<RuntimeProfile[]>('get_runtime_profiles');
        setProfiles(loadedProfiles);

        const loadedConvs = await invoke<Conversation[]>('get_conversations');
        setConversations(loadedConvs);
        if (loadedConvs.length > 0) {
          setActiveConversationId(loadedConvs[0].id);
        }

        const state = await invoke<AppState>('get_app_state');
        setAppState(state);

        const status = await invoke<ServerStatus>('get_server_status');
        setServerStatus(status);

        // Fast background admission telemetry (does NOT block initial window)
        const report = await invoke<PreflightReport>('check_admission_v2', {
          modelPath: loadedSettings.model_path,
          profileId: loadedSettings.runtime_profile || 'gpu_focused',
        });
        setPreflightReport(report);

        if (!report.all_passed && report.ram_deficit_gib > 0) {
          setAppState('LOW_MEMORY_WAIT');
        }

        const initialView = await invoke<string | null>('get_initial_view');
        if (initialView) {
          if (initialView === 'dashboard') {
            setIsDashboardOpen(true);
          } else if (initialView === 'recovery') {
            setIsRecoveryOpen(true);
            setRecoveryTab('ram');
          } else if (initialView === 'recovery_instructions') {
            setIsRecoveryOpen(true);
            setRecoveryTab('ram');
            setRecoveryExpandedProc('ollama');
          } else if (initialView === 'alternative_profiles') {
            setIsRecoveryOpen(true);
            setRecoveryTab('profiles');
          } else if (initialView === 'confirmation') {
            setIsRecoveryOpen(true);
            setAppState('READY_FOR_MODEL_CONFIRMATION');
          } else if (initialView === 'chat') {
            setIsDashboardOpen(false);
            setIsRecoveryOpen(false);
            setIsSettingsOpen(false);
            setAppState('MODEL_UNLOADED');
          } else if (initialView === 'settings') {
            setIsSettingsOpen(true);
          } else if (initialView === 'error') {
            setIsRecoveryOpen(true);
            setAppState('LOW_MEMORY_WAIT');
            setErrorMessage('Admission Gate 2 Blocked: Available RAM 8.42 GiB < Required 10.00 GiB (Deficit: 1.58 GiB).');
          }
        }
      } catch (err) {
        console.warn('Running outside Tauri or preview mode, initializing view data:', err);
      }

      // Check URL view parameter (?view=...) for deterministic visual capture
      const urlParams = new URLSearchParams(window.location.search);
      const urlView = urlParams.get('view');
      if (urlView) {
        const standardProfiles: RuntimeProfile[] = [
          {
            id: 'gpu_focused',
            name: 'Configuration D (GPU-Focused)',
            description: 'Approved baseline: 48 layers offloaded (40 MoE experts), direct split mode, 512 context.',
            status: 'APPROVED_BASELINE',
            gpu_layers: 48,
            context_size: 512,
            batch_size: 64,
            ubatch_size: 16,
            kv_cache_type_k: 'q8_0',
            kv_cache_type_v: 'q8_0',
            split_mode: 'direct',
            required_ram_gib: 10.0,
            required_vram_mib: 5500.0,
            is_approved_config_d: true,
          },
          {
            id: 'balanced',
            name: 'Balanced (Hybrid Offload)',
            description: 'Supported hybrid distribution: 24 layers on GPU, 24 on CPU, 512 context. Requires VRAM >= 4,000 MiB and RAM >= 8.0 GiB.',
            status: 'SUPPORTED_HYBRID',
            gpu_layers: 24,
            context_size: 512,
            batch_size: 64,
            ubatch_size: 16,
            kv_cache_type_k: 'q8_0',
            kv_cache_type_v: 'q8_0',
            split_mode: 'layer',
            required_ram_gib: 8.0,
            required_vram_mib: 4000.0,
            is_approved_config_d: false,
          },
          {
            id: 'cpu_offload',
            name: 'CPU-Dominant Offload (Experimental)',
            description: 'Experimental fallback: 12 layers on GPU, 36 on CPU, 512 context. Requires RAM >= 9.5 GiB.',
            status: 'EXPERIMENTAL_OFFLOAD',
            gpu_layers: 12,
            context_size: 512,
            batch_size: 64,
            ubatch_size: 16,
            kv_cache_type_k: 'q8_0',
            kv_cache_type_v: 'q8_0',
            split_mode: 'layer',
            required_ram_gib: 9.5,
            required_vram_mib: 2500.0,
            is_approved_config_d: false,
          },
          {
            id: 'low_memory',
            name: 'Low-VRAM Fallback (Experimental)',
            description: 'Experimental emergency profile: 4 layers on GPU, 44 on CPU, 512 context. Requires RAM >= 10.5 GiB.',
            status: 'EXPERIMENTAL_OFFLOAD',
            gpu_layers: 4,
            context_size: 512,
            batch_size: 64,
            ubatch_size: 16,
            kv_cache_type_k: 'q8_0',
            kv_cache_type_v: 'q8_0',
            split_mode: 'layer',
            required_ram_gib: 10.5,
            required_vram_mib: 1500.0,
            is_approved_config_d: false,
          },
        ];
        setProfiles(standardProfiles);

        const mockReport: PreflightReport = {
          all_passed: urlView === 'confirmation',
          timestamp_utc: new Date().toISOString(),
          monotonic_timestamp_secs: 1420.5,
          gates: [
            { gate_name: 'Telemetry Freshness', passed: true, required_str: '< 3.0s', measured_str: '0.2s', failure_reason: null },
            { gate_name: 'Available Physical Host RAM', passed: urlView === 'confirmation', required_str: '>= 10.00 GiB', measured_str: urlView === 'confirmation' ? '11.20 GiB' : '8.42 GiB', failure_reason: urlView === 'confirmation' ? null : 'Available RAM below profile requirement' },
            { gate_name: 'System Commit Limit Margin', passed: true, required_str: '< 85.0%', measured_str: '74.2%', failure_reason: null },
            { gate_name: 'Dedicated GPU VRAM Margin', passed: true, required_str: '>= 5,500 MiB', measured_str: '5,540 MiB', failure_reason: null },
            { gate_name: 'Exclusive GPU Compute Access', passed: true, required_str: '0 competing apps', measured_str: '0 apps', failure_reason: null },
            { gate_name: 'Free NVMe Storage Headroom', passed: true, required_str: '>= 20.0 GiB', measured_str: '112.5 GiB', failure_reason: null },
          ],
          avail_ram_gib: urlView === 'confirmation' ? 11.20 : 8.42,
          total_ram_gib: 16.0,
          ram_deficit_gib: urlView === 'confirmation' ? 0.0 : 1.58,
          commit_pct: 74.2,
          free_vram_mib: 5540.0,
          total_vram_mib: 6141.0,
          competing_gpu_apps: 0,
          free_disk_gib: 112.5,
          model_valid: true,
          top_processes: [
            {
              process_name: 'ollama.exe',
              instances: 1,
              pids: [14220],
              working_set_mb: 1680.0,
              vram_usage_mb: 0.0,
              is_tray_app: true,
              category: 'AI Inference Service',
              guidance: 'Close Ollama from system tray to recover 1.68 GiB host RAM',
              step_by_step_instructions: [
                'Locate Ollama icon in the Windows taskbar system notification area (tray).',
                'Right-click the Ollama llama icon.',
                'Select "Quit Ollama".',
                'Click "Recheck Admission" in NOVA Chat to verify memory release.',
              ],
            },
            {
              process_name: 'msedge.exe',
              instances: 8,
              pids: [21040, 21044],
              working_set_mb: 850.0,
              vram_usage_mb: 320.0,
              is_tray_app: false,
              category: 'Web Browser',
              guidance: 'Close background browser tabs or window',
              step_by_step_instructions: [
                'Close heavy tabs running YouTube or WebGL apps.',
                'Or close Microsoft Edge window completely.',
              ],
            },
            {
              process_name: 'Code.exe',
              instances: 6,
              pids: [18450],
              working_set_mb: 620.0,
              vram_usage_mb: 140.0,
              is_tray_app: false,
              category: 'Code Editor',
              guidance: 'Save active workspaces and close inactive VSCode windows',
              step_by_step_instructions: [
                'Save files in VSCode.',
                'Close secondary editor windows.',
              ],
            },
          ],
          integrity_state: {
            status: 'CRYPTOGRAPHICALLY_VERIFIED',
            details: {
              sha256: 'a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32',
              verified_at_utc: '2026-11-06T15:14:19Z',
            },
          },
          budget: {
            profile: standardProfiles[0],
            admission_level: urlView === 'confirmation' ? 'READY' : 'RECOVERABLE',
            required_ram_gib: 10.0,
            avail_ram_gib: urlView === 'confirmation' ? 11.20 : 8.42,
            ram_deficit_gib: urlView === 'confirmation' ? 0.0 : 1.58,
            required_vram_mib: 5500.0,
            avail_vram_mib: 5540.0,
            vram_deficit_mib: 0.0,
            system_commit_pct: 74.2,
            disk_free_gib: 112.5,
            reclaimable_ram_gib: 3.15,
            reclaimable_vram_mib: 460.0,
            alternative_profiles: [standardProfiles[1], standardProfiles[3]],
            summary_reason: 'Configuration D requires 10.0 GiB available RAM. Close Ollama (1.68 GiB) to unlock load.',
          },
        };
        setPreflightReport(mockReport);

        if (urlView === 'welcome') {
          setIsDashboardOpen(false);
          setIsRecoveryOpen(false);
          setIsSettingsOpen(false);
          setAppState('MODEL_UNLOADED');
        } else if (urlView === 'dashboard') {
          setIsDashboardOpen(true);
          setIsRecoveryOpen(false);
          setIsSettingsOpen(false);
        } else if (urlView === 'recovery') {
          setIsRecoveryOpen(true);
          setIsDashboardOpen(false);
          setIsSettingsOpen(false);
          setRecoveryTab('ram');
          setRecoveryExpandedProc(null);
          setRecoveryShowConfirmation(false);
        } else if (urlView === 'recovery_instructions') {
          setIsRecoveryOpen(true);
          setIsDashboardOpen(false);
          setIsSettingsOpen(false);
          setRecoveryTab('ram');
          setRecoveryExpandedProc('ollama.exe');
          setRecoveryShowConfirmation(false);
        } else if (urlView === 'alternative_profiles') {
          setIsRecoveryOpen(true);
          setIsDashboardOpen(false);
          setIsSettingsOpen(false);
          setRecoveryTab('profiles');
          setRecoveryShowConfirmation(false);
        } else if (urlView === 'confirmation') {
          setIsRecoveryOpen(true);
          setIsDashboardOpen(false);
          setIsSettingsOpen(false);
          setRecoveryShowConfirmation(true);
          setAppState('READY_FOR_MODEL_CONFIRMATION');
        } else if (urlView === 'chat') {
          setIsDashboardOpen(false);
          setIsRecoveryOpen(false);
          setIsSettingsOpen(false);
          setAppState('MODEL_UNLOADED');
          setConversations([
            {
              id: 'conv-sample-1',
              title: 'NOVA System Baseline Inquiry',
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
              messages: [
                {
                  id: 'msg-1',
                  role: 'user',
                  content: 'Explain how Configuration D offloads the Qwen3-30B-A3B MoE experts on RTX 4050.',
                  timestamp: '15:20:00',
                  status: 'complete',
                },
                {
                  id: 'msg-2',
                  role: 'assistant',
                  content: 'NOVA Chat uses Configuration D: all 48 transformer attention layers are offloaded to dedicated GPU VRAM (~4,560 MiB) while all 40 MoE experts are pinned to CPU host RAM (~12.8 GiB) via mmap with zero MoE caching overhead.',
                  timestamp: '15:20:02',
                  status: 'complete',
                },
              ],
            },
          ]);
        } else if (urlView === 'settings') {
          setIsSettingsOpen(true);
          setIsDashboardOpen(false);
          setIsRecoveryOpen(false);
        } else if (urlView === 'error') {
          setIsRecoveryOpen(true);
          setIsDashboardOpen(false);
          setIsSettingsOpen(false);
          setAppState('LOW_MEMORY_WAIT');
          setErrorMessage('Admission Gate 2 Blocked: Available RAM 8.42 GiB < Required 10.00 GiB (Deficit: 1.58 GiB).');
        }
      }
    }
    init();
  }, []);

  // Listen for real-time backend state changes & token streams
  useEffect(() => {
    const unlistenState = listen<string>('nova://state-change', (event) => {
      setAppState(event.payload as AppState);
    });

    const unlistenStream = listen<StreamTokenPayload>('nova://stream-token', (event) => {
      const { conversation_id, message_id, token, done, error } = event.payload;

      setConversations((prev) =>
        prev.map((c) => {
          if (c.id !== conversation_id) return c;
          return {
            ...c,
            messages: c.messages.map((m) => {
              if (m.id !== message_id) return m;
              return {
                ...m,
                content: m.content + (token || ''),
                status: done ? (error ? 'error' : 'complete') : 'streaming',
              };
            }),
          };
        })
      );

      if (done) {
        // Persist completed conversation safely
        setConversations((currentConvs) => {
          const conv = currentConvs.find((c) => c.id === conversation_id);
          if (conv) {
            invoke('save_conversation', { conversation: conv }).catch(console.error);
          }
          return currentConvs;
        });
      }
    });

    return () => {
      unlistenState.then((f) => f());
      unlistenStream.then((f) => f());
    };
  }, []);

  const runPreflight = useCallback(
    async (profileIdOverride?: string) => {
      setIsLoadingPreflight(true);
      setErrorMessage(null);
      const pid = profileIdOverride || selectedProfileId;
      try {
        const report = await invoke<PreflightReport>('check_admission_v2', {
          modelPath: settings.model_path,
          profileId: pid,
        });
        setPreflightReport(report);
        if (!report.all_passed) {
          if (report.ram_deficit_gib > 0) {
            setAppState('LOW_MEMORY_WAIT');
          } else {
            setAppState('PREFLIGHT_FAILED');
          }
        } else {
          setAppState('READY_FOR_MODEL_CONFIRMATION');
        }
      } catch (e: any) {
        setErrorMessage(String(e));
      } finally {
        setIsLoadingPreflight(false);
      }
    },
    [settings.model_path, selectedProfileId]
  );

  const handleSelectProfile = async (profileId: string) => {
    setSelectedProfileId(profileId);
    const updated = { ...settings, runtime_profile: profileId };
    setSettings(updated);
    await invoke('save_settings_cmd', { settings: updated });
    await runPreflight(profileId);
  };

  const handleLoadModel = async () => {
    setIsLoadingPreflight(true);
    setErrorMessage(null);
    try {
      // 1. Explicit backend authorization confirmation
      const token = await invoke<string>('confirm_model_load', {
        modelPath: settings.model_path,
      });

      // 2. Load model with explicit confirmation token and selected profile
      const status = await invoke<ServerStatus>('load_model', {
        modelPath: settings.model_path,
        confirmationToken: token,
        profileId: selectedProfileId,
      });
      setServerStatus(status);
      setAppState('MODEL_READY');
      setIsRecoveryOpen(false);
    } catch (e: any) {
      setErrorMessage(String(e));
      await runPreflight();
    } finally {
      setIsLoadingPreflight(false);
    }
  };

  const handleUnloadModel = async () => {
    try {
      const status = await invoke<ServerStatus>('unload_model');
      setServerStatus(status);
      setAppState('MODEL_UNLOADED');
    } catch (e: any) {
      console.error('Failed to unload model:', e);
    }
  };

  const handleNewConversation = () => {
    const newConv: Conversation = {
      id: crypto.randomUUID(),
      title: 'New Conversation',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      messages: [],
    };
    const updated = [newConv, ...conversations];
    setConversations(updated);
    setActiveConversationId(newConv.id);
    invoke('save_conversation', { conversation: newConv }).catch(console.error);
  };

  const handleDeleteConversation = async (id: string) => {
    const updated = conversations.filter((c) => c.id !== id);
    setConversations(updated);
    if (activeConversationId === id) {
      setActiveConversationId(updated.length > 0 ? updated[0].id : null);
    }
    await invoke('delete_conversation', { conversationId: id });
  };

  const handleRenameConversation = async (id: string, newTitle: string) => {
    const updated = conversations.map((c) =>
      c.id === id ? { ...c, title: newTitle, updated_at: new Date().toISOString() } : c
    );
    setConversations(updated);
    const target = updated.find((c) => c.id === id);
    if (target) {
      await invoke('save_conversation', { conversation: target });
    }
  };

  const handleClearHistory = async () => {
    setConversations([]);
    setActiveConversationId(null);
    await invoke('clear_history');
  };

  const handleSaveSettings = async (newSettings: AppSettings) => {
    setSettings(newSettings);
    setSelectedProfileId(newSettings.runtime_profile || 'gpu_focused');
    await invoke('save_settings_cmd', { settings: newSettings });
    await runPreflight(newSettings.runtime_profile);
  };

  const handleValidatePath = async (path: string): Promise<any> => {
    return await invoke('validate_model_path', { path });
  };

  const handleValidateRuntime = async (path: string): Promise<string> => {
    return await invoke<string>('validate_runtime_path', { path });
  };

  const handleSendMessage = async (text: string) => {
    let currentConvId = activeConversationId;
    let currentConv = conversations.find((c) => c.id === currentConvId);

    if (!currentConv || !currentConvId) {
      const newConv: Conversation = {
        id: crypto.randomUUID(),
        title: text.slice(0, 32),
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        messages: [],
      };
      currentConvId = newConv.id;
      currentConv = newConv;
      setConversations([newConv, ...conversations]);
      setActiveConversationId(newConv.id);
    } else if (currentConv.messages.length === 0) {
      currentConv.title = text.slice(0, 32);
    }

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: text,
      timestamp: new Date().toISOString(),
      status: 'complete',
    };

    const assistantMsgId = crypto.randomUUID();
    const assistantMsg: ChatMessage = {
      id: assistantMsgId,
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      status: 'streaming',
    };

    const updatedMessages = [...currentConv.messages, userMsg, assistantMsg];
    const updatedConv = {
      ...currentConv,
      messages: updatedMessages,
      updated_at: new Date().toISOString(),
    };

    setConversations((prev) =>
      prev.map((c) => (c.id === currentConvId ? updatedConv : c))
    );

    const historyPayload = updatedMessages
      .filter((m) => m.id !== assistantMsgId && m.status !== 'error')
      .map((m) => ({
        role: m.role,
        content: m.content,
      }));

    try {
      await invoke('send_chat_message', {
        conversationId: currentConvId,
        assistantMessageId: assistantMsgId,
        messages: historyPayload,
      });
    } catch (e: any) {
      console.error('Chat stream error:', e);
      setConversations((prev) =>
        prev.map((c) => {
          if (c.id !== currentConvId) return c;
          return {
            ...c,
            messages: c.messages.map((m) => {
              if (m.id !== assistantMsgId) return m;
              return {
                ...m,
                content: m.content || `Inference error: ${e}`,
                status: 'error',
              };
            }),
          };
        })
      );
    }
  };

  const handleRegenerate = async () => {
    const activeConv = conversations.find((c) => c.id === activeConversationId);
    if (!activeConv || activeConv.messages.length === 0) return;

    // Find the last user message
    const lastUser = [...activeConv.messages].reverse().find((m) => m.role === 'user');
    if (lastUser) {
      await handleSendMessage(lastUser.content);
    }
  };

  const handleStopGeneration = async () => {
    try {
      await invoke('stop_generation');
    } catch (e) {
      console.error('Failed to stop generation:', e);
    }
  };

  const activeConversation = conversations.find((c) => c.id === activeConversationId);
  const activeProfile =
    profiles.find((p) => p.id === selectedProfileId) || profiles[0] || null;

  return (
    <div className="nova-app-layout">
      <Sidebar
        conversations={conversations}
        activeConversationId={activeConversationId}
        appState={appState}
        onSelectConversation={setActiveConversationId}
        onNewConversation={handleNewConversation}
        onDeleteConversation={handleDeleteConversation}
        onRenameConversation={handleRenameConversation}
        onOpenSettings={() => setIsSettingsOpen(true)}
        onOpenLoader={() => {
          runPreflight();
          setIsRecoveryOpen(true);
        }}
        onOpenDashboard={() => setIsDashboardOpen(true)}
        appVersion={appVersion}
      />

      <main className="main-content">
        <TopBar
          title={activeConversation?.title || 'New Conversation'}
          appState={appState}
          serverStatus={serverStatus}
          activeProfileName={activeProfile?.name ? String(activeProfile.name) : undefined}
          onOpenLoader={() => {
            runPreflight();
            setIsRecoveryOpen(true);
          }}
          onUnloadModel={handleUnloadModel}
          onOpenSettings={() => setIsSettingsOpen(true)}
          onOpenDashboard={() => setIsDashboardOpen(true)}
        />

        <ChatArea
          messages={activeConversation?.messages || []}
          appState={appState}
          onOpenLoader={() => {
            runPreflight();
            setIsRecoveryOpen(true);
          }}
          onQuickPrompt={(prompt) => handleSendMessage(prompt)}
          onRegenerate={handleRegenerate}
        />

        <Composer
          appState={appState}
          onSendMessage={handleSendMessage}
          onStopGeneration={handleStopGeneration}
          onOpenLoader={() => {
            runPreflight();
            setIsRecoveryOpen(true);
          }}
        />
      </main>

      {/* Resource Recovery & Adaptive Loader Modal */}
      <ResourceRecoveryModal
        isOpen={isRecoveryOpen}
        onClose={() => {
          invoke('cancel_model_confirmation').catch(console.error);
          setIsRecoveryOpen(false);
        }}
        report={preflightReport}
        appState={appState}
        modelPath={settings.model_path}
        profiles={profiles}
        selectedProfileId={selectedProfileId}
        onSelectProfile={handleSelectProfile}
        onRecheck={() => runPreflight()}
        onLoadModel={handleLoadModel}
        isLoading={isLoadingPreflight}
        errorMessage={errorMessage}
        initialTab={recoveryTab}
        initialExpandedProc={recoveryExpandedProc}
        initialShowConfirmation={recoveryShowConfirmation}
      />

      {/* Resource Telemetry Dashboard */}
      <ResourceDashboard
        isOpen={isDashboardOpen}
        onClose={() => setIsDashboardOpen(false)}
        report={preflightReport}
        serverStatus={serverStatus}
        activeProfile={activeProfile}
        appState={appState}
        onRefresh={() => runPreflight()}
        isLoading={isLoadingPreflight}
      />

      {/* Settings Modal */}
      <SettingsModal
        isOpen={isSettingsOpen}
        onClose={() => setIsSettingsOpen(false)}
        settings={settings}
        profiles={profiles}
        onSaveSettings={handleSaveSettings}
        onClearHistory={handleClearHistory}
        onValidatePath={handleValidatePath}
        onValidateRuntime={handleValidateRuntime}
      />
    </div>
  );
}

export default App;
