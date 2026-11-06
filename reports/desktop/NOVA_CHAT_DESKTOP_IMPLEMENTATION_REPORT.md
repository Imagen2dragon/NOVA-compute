# NOVA Compute — NOVA Chat Desktop & Guided Model Loader Implementation Report

**Document Version:** 1.0.0  
**Date:** 2026-11-05  
**Branch:** `feature/nova-chat-desktop`  
**Base Commit:** `7d765f45888574751c8d674bba481fdaa5e2a01f`  
**Framework:** Tauri 2 (Rust native backend + React 19 / TypeScript 6 / Vite 6 frontend)  
**Target Platform:** Windows x64 (Windows 11 Home 24H2)  
**Packaging:** Per-User NSIS Installer (`installMode: "currentUser"`, no administrative privileges required)  
**Project Owner:** User  
**Lead Architect & Reviewer:** ChatGPT  
**Implementation Engineer:** Antigravity  

---

## 1. Executive Summary

In accordance with architectural guidance and Phase 3 safety requirements, we have designed, implemented, tested, and packaged **NOVA Chat Desktop** and its integrated **Guided Model Loader**.

NOVA Chat Desktop is a native Windows desktop application that delivers a responsive ChatGPT/Gemini-style conversational interface while enforcing hardware-aware safety boundaries:
1. **Lightweight Standby:** When started, NOVA Chat initializes in a low-footprint unloaded standby state (~58 MB working set, ~22 MB private commit, 0 MB VRAM).
2. **6-Gate Admission Gate Enforcement:** Model inference weights are NEVER loaded into memory until all 6 mandatory admission gates pass.
3. **RAM Deficit & Reclamation Guidance:** If physical RAM is insufficient (< 10.0 GiB available), NOVA Chat explains the exact deficit in plain language and enumerates top candidate applications with specific closure recommendations based on live OS process accounting (`WorkingSet` + `PrivateUsage`).
4. **Kernel-Level Lifecycle Containment:** The inference server process (`llama-server.exe`) is spawned inside a dedicated Win32 Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Even in the event of an abnormal application crash, the Windows kernel guarantees immediate teardown of all child processes, preventing orphaned VRAM/RAM allocations.
5. **Zero Live Inference Constraint Observed:** Live Qwen3-30B-A3B weights were strictly prohibited from memory allocation during development and testing. All tests operated via mocked telemetry, unit tests, and structural validation.

---

## 2. Technical Stack and Architecture

```mermaid
flowchart TD
    subgraph UI ["NOVA Chat Desktop UI (React 19 + TypeScript + Vite 6)"]
        Sidebar["Sidebar (Brand, History, New Chat)"]
        TopBar["TopBar (Model Status, Memory Pill, Unload)"]
        ChatArea["Chat Area (Message Stream, Code Copy)"]
        Composer["Composer (Multiline Input, Stop Gen)"]
        GuidedModal["Guided Model Loader Modal (6 Gates, Deficit, Process Table)"]
        SettingsModal["Settings Modal (Model Path, Port 18080, Clear History)"]
    end

    subgraph Native ["Tauri 2 Native Core (Rust)"]
        IPC["Tauri IPC Command Router"]
        Admission["Admission Engine (6-Gate Hardware Audit)"]
        ProcEnum["Win32 Process Memory Enumerator (ToolHelp32 + PSAPI)"]
        JobObj["Win32 Job Object (KILL_ON_JOB_CLOSE)"]
        ServerMgr["Server Manager (llama-server Lifecycle)"]
        ChatService["Chat Streaming Service (SSE Client -> nova://stream-token)"]
        Storage["Storage Engine (%LOCALAPPDATA%\\NOVA Chat\\)"]
    end

    subgraph Subprocess ["Contained Child Runtime"]
        LlamaServer["llama-server.exe (Pinned v0.8.0-dev build 12032)"]
    end

    UI <-->|Tauri IPC / Events| Native
    Native -->|AssignProcessToJobObject| JobObj
    JobObj -->|Enforces Termination| LlamaServer
    ServerMgr -->|Spawn / Health Probe| LlamaServer
    ChatService <-->|HTTP /v1/chat/completions (SSE)| LlamaServer
```

### 2.1 Backend Architecture (`desktop/src-tauri/`)
- [`src/state.rs`](file:///C:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/desktop/src-tauri/src/state.rs): Authoritative `AppState` enum (`APP_STARTING`, `APP_READY`, `MODEL_NOT_CONFIGURED`, `PREFLIGHT_RUNNING`, `LOW_MEMORY_WAIT`, `PREFLIGHT_FAILED`, `READY_FOR_MODEL_CONFIRMATION`, `MODEL_STARTING`, `MODEL_READY`, `GENERATING`, `GENERATION_STOPPING`, `MODEL_STOPPING`, `MODEL_UNLOADED`, `ERROR`).
- [`src/admission.rs`](file:///C:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/desktop/src-tauri/src/admission.rs): 6-gate preflight evaluator querying Win32 `GlobalMemoryStatusEx`, `GetDiskFreeSpaceExW`, NVML dynamic library bindings, and `ToolHelp32` + `GetProcessMemoryInfo` for resident working set and private commit accounting.
- [`src/job_object.rs`](file:///C:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/desktop/src-tauri/src/job_object.rs): Win32 Job Object implementation with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and RAII handle lifecycle.
- [`src/server_manager.rs`](file:///C:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/desktop/src-tauri/src/server_manager.rs): Process lifecycle orchestrator for `llama-server.exe` locking Configuration D parameters, assigning to the Job Object, generating ephemeral loopback API credentials, and polling `/health` readiness asynchronously.
- [`src/chat.rs`](file:///C:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/desktop/src-tauri/src/chat.rs): SSE streaming completion client with thread-safe stop signal (`AtomicBool`) emitting `nova://stream-token` events directly to the frontend.
- [`src/storage.rs`](file:///C:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/desktop/src-tauri/src/storage.rs): Local JSON persistence in `%LOCALAPPDATA%\NOVA Chat\` for conversations, messages, settings, and structured diagnostic logs.

### 2.2 Locked Configuration D Runtime Parameters
When launched through the Guided Model Loader, `llama-server.exe` is strictly configured with:
```text
-m <MODEL_PATH>
-ngl 48
-ncmoe 40
--moe-cache-mib 0
-c 512
-b 64
-ub 16
-ctk q8_0
-ctv q8_0
-fa auto
-lm mmap
--host 127.0.0.1
--port <PORT>
--api-key <EPHEMERAL_KEY>
```

---

## 3. The 6-Gate Admission Audit & Guided Model Loader

| Gate # | Name | Metric | Required Safety Threshold | Operating System Query Source |
|---|---|---|---|---|
| 1 | Telemetry Integrity | Sensor status | All sensors SUCCESS | Win32 API / NVML |
| 2 | Available Physical RAM | `ullAvailPhys` | $\ge 10.00\text{ GiB}$ | `GlobalMemoryStatusEx` |
| 3 | System Commit Pressure | `commit_pct` | $< 90.0\%$ | `GlobalMemoryStatusEx` (`ullTotalPageFile` - `ullAvailPageFile`) |
| 4 | Free Dedicated VRAM | `free_vram` | $\ge 5500.0\text{ MiB}$ | Dynamic NVML (`nvmlDeviceGetMemoryInfo`) |
| 5 | Competing GPU Compute | Active compute PIDs | $== 0\text{ apps}$ | Dynamic NVML (`nvmlDeviceGetComputeRunningProcesses_v3`) |
| 6 | Free Drive Storage | Disk free space | $\ge 25.00\text{ GiB}$ | `GetDiskFreeSpaceExW` on model file path drive |

### Memory Reclamation & Candidate Process Guidance
When Gate 2 fails ($\text{Available RAM} < 10.0\text{ GiB}$), NOVA Chat:
1. Calculates the exact deficit: $\text{Deficit} = 10.00\text{ GiB} - \text{Available RAM}$.
2. Displays a high-contrast amber advisory banner explaining the deficit in plain language.
3. Automatically queries the top candidate processes, classifying them by type (Web Browser, Development IDE, Competing LLM Runtime, Communication App, Media Launcher) and reporting:
   - Process executable name
   - Instance count
   - Resident Working Set (MB)
   - Private Commit (MB)
   - Specific user-facing guidance recommendation
4. Provides a "Check Again" button to immediately re-sample telemetry.
5. Keeps the "Confirm & Load Model" button disabled until all 6 gates pass.

---

## 4. Verification and Test Results

### 4.1 Rust Native Unit Tests (`desktop/src-tauri/`)
Executed via `cargo test --manifest-path desktop\src-tauri\Cargo.toml`:
```text
running 9 tests
test tests::tests::test_job_object_creation ... ok
test tests::tests::test_app_state_string_representations ... ok
test tests::tests::test_conversation_serialization ... ok
test tests::tests::test_admission_all_gates_pass ... ok
test tests::tests::test_admission_competing_gpu ... ok
test tests::tests::test_admission_ram_deficit ... ok
test tests::tests::test_admission_commit_boundary ... ok
test tests::tests::test_admission_vram_boundary ... ok
test tests::tests::test_admission_disk_boundary ... ok

test result: ok. 9 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.03s
```

### 4.2 Python Regression Test Suite
Executed via `python -m unittest discover -s tests`:
```text
Ran 117 tests in 6.421s

OK
```
All existing Phase 2D, Phase 3A, Phase 3B.1–3B.4 supervisor safety tests, mocked inference gates, and memory advisor benchmarks continue to pass 100%.

### 4.3 Packaging & Resource Footprint Measurements

| Metric | Measured Value | Standard / Requirement | Compliance |
|---|---|---|---|
| **NSIS Installer File Size** | **1.89 MiB** (1,986,686 bytes) | $< 10\text{ MB}$ (no model weights bundled) | **PASS** |
| **Installed Binary Size** | **5.47 MiB** (5,735,936 bytes) | Stripped LTO release binary | **PASS** |
| **Unloaded Idle Working Set** | **58.18 MB** (PID 23980) | Lightweight standby $(< 100\text{ MB})$ | **PASS** |
| **Unloaded Private Commit** | **22.31 MB** | Minimal commit overhead | **PASS** |
| **Unloaded VRAM Allocation** | **0.00 MiB** | Zero GPU memory impact when unloaded | **PASS** |
| **Installer Elevation** | Per-user (`currentUser`) | No Administrator elevation prompt | **PASS** |
| **Evergreen Runtime** | Microsoft Edge WebView2 (System) | Shared Evergreen, zero duplicate Chromium | **PASS** |

---

## 5. Artifacts and Generated Deliverables

1. **Architecture Specification:**  
   [`docs/architecture/NOVA_CHAT_DESKTOP_ARCHITECTURE.md`](file:///C:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/docs/architecture/NOVA_CHAT_DESKTOP_ARCHITECTURE.md)
2. **Desktop Application Workspace:**  
   [`desktop/`](file:///C:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/desktop/)
3. **Rust Native Backend Source:**  
   [`desktop/src-tauri/src/`](file:///C:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/desktop/src-tauri/src/)
4. **React Frontend Source:**  
   [`desktop/src/`](file:///C:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/desktop/src/)
5. **Windows NSIS Installer Bundle:**  
   `desktop/src-tauri/target/release/bundle/nsis/NOVA Chat_0.1.0_x64-setup.exe` (1.89 MiB)
6. **Implementation Report:**  
   [`reports/desktop/NOVA_CHAT_DESKTOP_IMPLEMENTATION_REPORT.md`](file:///C:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/reports/desktop/NOVA_CHAT_DESKTOP_IMPLEMENTATION_REPORT.md)

---

## 6. Adherence to Project Governance

- **Zero Live Inference Constraint:** Verified. Neither `llama-cli.exe` nor `llama-server.exe` was invoked with live model weights during implementation or automated testing.
- **Model Distribution Isolation:** Confirmed. The GGUF model file is strictly excluded from the application installer bundle and read on demand from the configured user path.
- **Job Object Process Containment:** Enforced. Win32 Job Object assigns all child server handles with kernel-enforced `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
- **Git State:** Dedicated branch `feature/nova-chat-desktop` maintained cleanly off starting commit `7d765f4`. All prior commit history preserved.
