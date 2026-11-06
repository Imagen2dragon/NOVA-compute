# NOVA Chat Desktop + Guided Model Loader Architecture Plan

**Document:** `docs/architecture/NOVA_CHAT_DESKTOP_ARCHITECTURE.md`  
**Phase:** Desktop Client & Guided Loader Integration  
**Date:** 2026-11-05  
**Author:** Antigravity (Implementation Engineer)  
**Reviewers:** User (Project Owner), ChatGPT (Lead Architect)  
**Target Platform:** Windows 11 x64 (RTX 4050 6GB VRAM, 16GB RAM)  
**Repository Branch:** `feature/nova-chat-desktop`

---

## 1. Executive Summary & Goals

NOVA Chat Desktop is a lightweight, responsive Windows desktop AI chat application powered by the local Qwen3-30B-A3B Q4_K_M GGUF model via the pinned `llama.cpp` Windows runtime.

Key Pillars:
1. **Lightweight & Fast Startup**: Starts without spinning up heavy runtimes or terminal windows. Initial idle working set targeted under 200 MiB (including WebView2).
2. **Mandatory Guided Model Loader**: Embedded 6-gate fail-closed admission auditing. When available physical RAM is under 10.0 GiB, the application displays the exact deficit in plain language, identifies candidate applications for manual closure, and safely awaits operator recovery before enabling model launch.
3. **Robust Process Lifecycle Management**: Model hosting is managed via `llama-server.exe` wrapped inside a Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), guaranteeing zero orphaned server instances even in the event of abnormal application exit.
4. **Preservation of Safety Governance**: The single-shot CLI smoke test remains completely intact and untouched. The desktop application establishes a separate, authenticated loopback server session. Zero cloud telemetry or external network transmission.

---

## 2. Technical Stack & Architectural Layers

```mermaid
flowchart TD
    subgraph UI ["Frontend (WebView2 / React 18 + TypeScript + Vite + Tailwind CSS)"]
        Sidebar["Sidebar: Conversations & Search"]
        ChatArea["Chat Area: Markdown, Code Blocks, Streaming"]
        Composer["Message Composer"]
        LoaderModal["Guided Loader: 6-Gate Audit & Guidance"]
        TopBar["Top Bar: Model Status & Resource Badge"]
    end

    subgraph IPC ["Tauri 2 IPC Bridge"]
        Commands["Tauri Commands (async invoke)"]
        Events["Tauri Event Channel (Streaming & Telemetry)"]
    end

    subgraph Backend ["Native Rust Core (src-tauri)"]
        SM["AppState State Machine"]
        Admission["Admission Engine (Win32 & NVML 6-Gate Check)"]
        JobObj["Windows Job Object Supervisor"]
        ProcessMgr["llama-server Process Lifecycle Manager"]
        Storage["Local Storage (%LOCALAPPDATA% JSON / SQLite)"]
        HttpClient["Reqwest Loopback HTTP Client (127.0.0.1)"]
    end

    subgraph Runtime ["Local Execution Tier"]
        LlamaServer["llama-server.exe (Pinned b12032, commit dde879bc4)"]
        ModelWeights["Qwen_Qwen3-30B-A3B-Q4_K_M.gguf (18.25 GiB)"]
    end

    UI <--> IPC
    IPC <--> Backend
    ProcessMgr -->|Spawn & Monitor| LlamaServer
    JobObj -->|OS Kernel Containment| LlamaServer
    LlamaServer -->|mmap / CUDA Offload| ModelWeights
    HttpClient -->|POST /v1/chat/completions (SSE)| LlamaServer
```

---

## 3. Subsystem Breakdown

### 3.1 Pinned Runtime & Server Option Matrix
- **Binary**: `runtime\windows\llama-cpp\llama-server.exe` (Version `0.8.0-dev`, build `12032`, commit `dde879bc4`).
- **Configuration Flags**:
  - `-ngl 48`: Offload dense and initial MoE layers to GPU.
  - `-ncmoe 40`: Retain 40 CPU MoE layers in virtual memory.
  - `--moe-cache-mib 0`: Expert cache disabled by default.
  - `-c 512`: Prompt context window.
  - `-b 64`: Batch size.
  - `-ub 16`: Micro-batch size.
  - `-ctk q8_0`, `-ctv q8_0`: Quantized KV cache.
  - `-fa auto`: Automatic FlashAttention.
  - `-lm mmap`: File-backed memory mapping.
  - `--host 127.0.0.1`: Loopback only (strictly blocks 0.0.0.0).
  - `--port <PORT>`: Dynamically assigned or user-configured loopback port.
  - `--api-key <RANDOM_SECRET>`: Local per-session secret authentication key generated at runtime.

### 3.2 Canonical 6-Gate Admission Audit (Rust Implementation)
The Rust admission engine executes the exact Phase 2D admission policies:
1. `telemetry_integrity`: Win32 and NVML sensors return success with valid finite numeric counters.
2. `avail_ram`: Available physical RAM $\ge 10.0\text{ GiB}$ ($10,737,418,240\text{ bytes}$).
3. `system_commit`: Virtual commit charge $< 90.0\%$ of total commit limit.
4. `avail_vram`: Free dedicated video RAM $\ge 5,500.0\text{ MiB}$.
5. `no_competing_gpu`: Competing GPU compute applications $== 0$.
6. `disk_free`: Free model drive space $\ge 25.0\text{ GiB}$.

When `avail_ram` fails:
- Calculates exact deficit: $10.00\text{ GiB} - \text{Current Avail GiB}$.
- Enumerates top memory-consuming processes using `EnumProcesses` + `GetProcessMemoryInfo`.
- Reports plain-language guidance with manual closure recommendations.

### 3.3 State Machine Specification
The backend owns and enforces the following authoritative states:
- `APP_STARTING`: Application is initializing native modules and local database.
- `APP_READY`: Desktop shell is running, model unloaded, awaiting user action.
- `MODEL_NOT_CONFIGURED`: Model GGUF path is missing or unverified on disk.
- `PREFLIGHT_RUNNING`: Active 6-gate hardware audit in progress.
- `LOW_MEMORY_WAIT`: Admission gated due to RAM deficit; awaiting manual recovery.
- `PREFLIGHT_FAILED`: Non-RAM admission gate failed (e.g. disk or VRAM).
- `READY_FOR_MODEL_CONFIRMATION`: All 6 gates passed; awaiting explicit confirmation.
- `MODEL_STARTING`: Process spawned in Job Object, awaiting `/health` readiness.
- `MODEL_READY`: `llama-server` healthy and responsive; composer unlocked.
- `GENERATING`: Streaming chat completion active.
- `GENERATION_STOPPING`: User triggered Stop; cancellation in flight.
- `MODEL_STOPPING`: Server teardown initiated.
- `MODEL_UNLOADED`: Server terminated cleanly; resources fully released.
- `ERROR`: Unrecoverable process or runtime error.

### 3.4 Windows Job Object & Clean Teardown
To prevent lingering background model servers:
1. Rust creates a Windows Job Object via `CreateJobObjectW`.
2. Sets `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` using `SetInformationJobObject`.
3. When `llama-server.exe` is spawned via `Command::new()`, its handle is assigned to the Job Object via `AssignProcessToJobObject`.
4. If NOVA Chat is closed normally, crashes, or is killed in Task Manager, the Windows kernel terminates `llama-server.exe` immediately.

### 3.5 Local Data & Privacy Boundaries
- **Storage Directory**: `%LOCALAPPDATA%\NOVA Chat\`
- **Files**:
  - `conversations.json`: Thread metadata, messages, timestamps.
  - `settings.json`: Selected GGUF path, port preference, theme.
  - `logs\app.log`: Rolling diagnostic log (bounded size $\le 5\text{ MB}$).
- **Privacy Assurance**: No outbound external telemetry or cloud API requests exist. All traffic is bound strictly to `127.0.0.1`.

---

## 4. Packaging & Installer Specifications
- **Tool**: Tauri 2 Bundler (NSIS target).
- **Mode**: Per-user installation (`RequestExecutionLevel user`), no administrator prompt.
- **Payload**: Bundled desktop binary (`nova-chat.exe`), WebView2 bootstrap detection, desktop & Start Menu shortcuts.
- **Model weights**: Excluded from installer to maintain small package size (~10–20 MB).
