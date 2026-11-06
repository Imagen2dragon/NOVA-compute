# NOVA Chat — Phase 2 Release-Gate Hardening & Engineering Review Report

**Date:** 2026-11-05  
**Branch:** `feature/nova-chat-desktop`  
**Base Commit:** `7d765f45888574751c8d674bba481fdaa5e2a01f`  
**Project Owner:** User  
**Lead Architect / Reviewer:** ChatGPT  
**Implementation Engineer:** Antigravity  

---

## 1. Actual Branch, HEAD, and Working-Tree Status

- **Branch:** `feature/nova-chat-desktop`
- **Current Base HEAD:** `7d765f45888574751c8d674bba481fdaa5e2a01f`
- **Working Tree State:** All implementation, source hardening, tests, and build artifacts reside on the clean feature branch.
- **Git Command Output Verification:**
  - `git branch --show-current` => `feature/nova-chat-desktop`
  - `git rev-parse HEAD` => `7d765f45888574751c8d674bba481fdaa5e2a01f`
  - Git history has not been reset, rebased, force-pushed, or merged.

---

## 2. Source Files Inspected and Changed

| File Path | Action | Hardening & Parity Modifications |
|---|---|---|
| `desktop/src-tauri/src/admission.rs` | Modified / Hardened | Canonical 6-gate fail-closed parity with Python steward; GGUF header parser (magic, version, tensor count); fail-closed NVML compute process query; monotonic freshness validation (`is_stale(3s)`). |
| `desktop/src-tauri/src/job_object.rs` | Modified / Hardened | Win32 `spawn_child_suspended`: creates child with `CREATE_SUSPENDED`, assigns handle to Job Object, verifies assignment or terminates child before execution, then resumes thread. No breakaway. |
| `desktop/src-tauri/src/server_manager.rs` | Modified / Hardened | Suspended spawn via Job Object; 127.0.0.1 strict binding; ephemeral API key (unlogged); multi-tier runtime auto-discovery (`AppSettings.runtime_path`, `NOVA_RUNTIME_DIR`, relative, and app directory); verified `/v1/models` readiness probe. |
| `desktop/src-tauri/src/chat.rs` | Modified / Hardened | Hardened SSE streaming with atomic cancellation (`stop_generation`), partial chunk buffering, error code handling. |
| `desktop/src-tauri/src/storage.rs` | Modified / Hardened | Added `runtime_path` persistence in `%LOCALAPPDATA%\NOVA Chat\settings.json`; conversation CRUD. |
| `desktop/src-tauri/src/lib.rs` | Modified / Hardened | Enforced fresh 6-gate admission audit immediately prior to server launch; GGUF validation prerequisite; exposed runtime and model validation commands. |
| `desktop/src-tauri/src/tests.rs` | Expanded | 18 automated Rust tests covering all 6 gates, boundary values, fail-closed telemetry, Job Object suspended lifecycle, and mock SSE chat streaming. |
| `desktop/src/types.ts` | Modified | Updated TypeScript interfaces with `runtime_path`, `GgufHeaderInfo`, and `PreflightReport.model_valid`. |
| `desktop/src/components/SettingsModal.tsx` | Modified | Added runtime directory input and verification button. |
| `desktop/src/App.tsx` | Modified | Wired runtime verification and model header inspection IPC. |
| `desktop/src-tauri/tauri.conf.json` | Inspected | Configured per-user NSIS installer (`currentUser`), window bounds, bundle metadata. |
| `desktop/src-tauri/Cargo.toml` | Verified | Cargo configuration with `windows-sys` Win32 features. |
| `desktop/package.json` | Verified | React 19, Vite 6, `@tauri-apps/api` dependencies. |

---

## 3. Canonical Six-Gate Admission Parity Matrix

The Rust backend (`desktop/src-tauri/src/admission.rs`) enforces strict semantic parity with the approved Python resource steward (`nova/resource_steward/memory_budget.py`):

| Gate # | Gate Name | Required Safety Threshold | Operating System Source | Fail-Closed Rule |
|---|---|---|---|---|
| **Gate 1** | `telemetry_integrity` | `All sensors SUCCESS` | Combined health of RAM, GPU, Disk sensors | If any sensor returns error or is unavailable, Gate 1 FAILS and all dependent gates fail closed. |
| **Gate 2** | `avail_ram` | $\ge 10.00\text{ GiB}$ | Win32 `GlobalMemoryStatusEx` (`ullAvailPhys`) | If telemetry is unhealthy, measured is `"UNAVAILABLE"` and gate FAILS. RAM deficit calculated as $\max(0.0, 10.00 - \text{avail})$. |
| **Gate 3** | `system_commit` | $< 90.0\%$ | Win32 `GlobalMemoryStatusEx` (`ullTotalPageFile`, `ullAvailPageFile`) | If commit total $\le 0$, available $>$ total, commit % $\ge 90.0\%$, or telemetry is unhealthy, gate FAILS. |
| **Gate 4** | `avail_vram` | $\ge 5500.0\text{ MiB}$ | Dynamic NVML `nvmlDeviceGetMemoryInfo` | If NVML is missing, fails, or returns error, gate FAILS. |
| **Gate 5** | `no_competing_gpu` | `0 active compute apps` | Dynamic NVML `nvmlDeviceGetComputeRunningProcesses_v2` | If NVML entrypoint symbol is missing or query returns an error, gate FAILS (fail-closed, never falsely passing as 0). |
| **Gate 6** | `disk_free` | $\ge 25.00\text{ GiB}$ | Win32 `GetDiskFreeSpaceExW` on target model drive | If drive query fails or disk space $< 25.0\text{ GiB}$, gate FAILS. |
| **Prereq** | `model_file` | Valid GGUF v2/v3 header, $\ge 10\text{ MiB}$ | Fast binary header reader (24 bytes) | Checked immediately prior to launch; does not replace telemetry. |

---

## 4. Telemetry and Hardware Measurement Details

1. **Available Physical RAM:**  
   Queried via Win32 `GlobalMemoryStatusEx(&mut mem)`. The resident unallocated physical memory is read directly from `mem.ullAvailPhys` and converted to GiB via `ullAvailPhys / (1024^3)`.
2. **System Commit Percentage:**  
   Queried via Win32 `GlobalMemoryStatusEx`. Total commit limit is `mem.ullTotalPageFile`. Committed charge is calculated as `mem.ullTotalPageFile.saturating_sub(mem.ullAvailPageFile)`. Percentage is `(committed / total) * 100.0`. Validated to be non-zero and within $[0.0, 100.0]$.
3. **Free Dedicated VRAM:**  
   Queried via dynamic runtime binding to `nvml.dll`. Uses `nvmlDeviceGetHandleByIndex_v2(0)` and `nvmlDeviceGetMemoryInfo(dev, &mut mem)`. Free VRAM is `mem.free / (1024^2)` in MiB.
4. **Competing GPU Compute Processes:**  
   Queried via dynamic binding to `nvmlDeviceGetComputeRunningProcesses_v2`. Tested and confirmed on the RTX 4050 Laptop GPU. If the entrypoint is missing or returns anything other than `NVML_SUCCESS` or `NVML_ERROR_INSUFFICIENT_SIZE`, telemetry reports error and fails closed.
5. **Process Working Set and Private Commit (Advisory Table Only):**  
   Enumerated via `CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)` and `GetProcessMemoryInfo(h_proc, &mut pmc)`. Used **strictly** for user guidance in the Memory Reclamation table to identify candidate desktop applications for manual closure. **Never used to pass or fail the GPU compute gate.**
6. **Telemetry Freshness:**  
   Each report captures an ISO 8601 UTC timestamp and a monotonic tick count (`monotonic_timestamp_secs`). If telemetry is older than 3.0 seconds, it is marked stale and `load_model` rejects it, executing a fresh live preflight audit immediately prior to launch.

---

## 5. llama.cpp Server Build and Configuration Compatibility

- **Binary Path:** `runtime/windows/llama-cpp/llama-server.exe`
- **SHA-256 Hash:** `FE2957C5FC9DEFB76279A513EA58153B5990C14AF8E2C9D2150C2D194BF72668`
- **Version / Build:** `0.8.0-dev (build 12032, commit dde879bc4)`, Clang 20.1.8 for Windows x86_64
- **Configuration D Option Support Verification:**
  - `-ngl 48`: Supported (`-ngl, --gpu-layers, --n-gpu-layers N`)
  - `-ncmoe 40`: Supported (`-ncmoe, --n-cpu-moe N`)
  - `--moe-cache-mib 0`: Supported (`--moe-cache-mib N`)
  - `-c 512`: Supported (`-c, --ctx-size N`)
  - `-b 64`: Supported (`-b, --batch-size N`)
  - `-ub 16`: Supported (`-ub, --ubatch-size N`)
  - `-ctk q8_0`: Supported (`-ctk, --cache-type-k TYPE`)
  - `-ctv q8_0`: Supported (`-ctv, --cache-type-v TYPE`)
  - `-fa auto`: Supported (`-fa, --flash-attn [on|off|auto]`)
  - `-lm mmap`: Supported (`-lm, --load-mode [auto|none|mmap|mlock|mmap+mlock]`)
  - `--host 127.0.0.1`: Supported (`--host HOST`)
  - `--port <PORT>`: Supported (`--port PORT`)
  - `--api-key <KEY>`: Supported (`--api-key KEY`)
- **HTTP Endpoints in `llama-server-impl.dll`:**
  - `/v1/chat/completions`: Present and verified for OpenAI-compatible streaming
  - `/v1/models` and `/models`: Present and verified as canonical model readiness endpoints
  - `/slots`: Present for execution slot inspection
  - `/completion`: Present for legacy text generation

---

## 6. Installer Resource Inventory and Runtime Topology

- **Installer Path:** `desktop/src-tauri/target/release/bundle/nsis/NOVA Chat_0.1.0_x64-setup.exe`
- **Installer File Size:** **1.90 MiB** (1,990,133 bytes)
- **Installer Mode:** Per-user (`installMode: "currentUser"`), zero administrative elevation required.
- **Installed Application Files (`%LOCALAPPDATA%\NOVA Chat\`):**
  - `nova-chat.exe` (5.48 MiB stripped release binary)
  - `uninstall.exe` (79 KiB per-user uninstaller)
  - `settings.json`, `conversations.json`, `logs\app.log`
- **Runtime Discovery Hierarchy:**
  NOVA Chat dynamically locates the pinned runtime through the following priority order:
  1. User-configured `runtime_path` in Settings UI
  2. `NOVA_RUNTIME_DIR` environment variable
  3. Installed application directory (`%LOCALAPPDATA%\NOVA Chat\runtime\...` or `%LOCALAPPDATA%\NOVA Chat\llama-server.exe`)
  4. Relative development workspace directory (`runtime/windows/llama-cpp/llama-server.exe`)
  5. If missing: Fails with a clear diagnostic error stating which required DLLs (`llama-server-impl.dll`, `ggml-cuda.dll`, `cublas64_12.dll`, `cudart64_12.dll`) must be placed in the runtime directory.
- **Model Weight Isolation:** The 18.25 GiB Qwen3-30B GGUF file is **strictly excluded** from the installer and loaded on demand from the configured user path.

---

## 7. Windows Job Object Containment Test Evidence

The Win32 Job Object implementation was tested with an integration test in `desktop/src-tauri/src/tests.rs`:

1. **Suspended Spawn & Immediate Assignment:**  
   Child processes are spawned via Win32 `CreateProcessW` with `CREATE_SUSPENDED | CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS`.
2. **Fail-Closed on Assignment Failure:**  
   `AssignProcessToJobObject` is called while the primary thread is suspended. If assignment fails, `TerminateProcess` is called immediately and the thread is never resumed.
3. **Resumption:**  
   Only after confirmed Job Object assignment is `ResumeThread` called.
4. **Automatic Kernel Cleanup Verification:**  
   `test_job_object_spawn_suspended_and_automatic_cleanup_on_close`:
   - Harmless child process spawned suspended in Job Object: confirmed running (`is_alive() == true`, PID > 0).
   - Job Object handle dropped (`drop(job)`).
   - Windows kernel terminates the child process immediately (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`).
   - Verified dead after 1.5 seconds (`is_alive() == false`).
   - Outcome: **PASS**.

---

## 8. Chat Streaming and API Verification

- **Localhost Binding:** Server command line strictly locks `--host 127.0.0.1`.
- **Ephemeral Authentication:** Unique UUID generated per session (`nova-<uuid>`); passed via `--api-key` and sent in HTTP `Authorization: Bearer` headers. Token is excluded from diagnostic logs.
- **Readiness Probing:** Asynchronous polling of `http://127.0.0.1:<PORT>/v1/models` up to 60 seconds.
- **Cancellation & Stop Generation:** Thread-safe `AtomicBool` stop signal interrupts active byte streams and emits `{ "done": true }` to the UI without orphan requests.
- **Mock SSE Streaming Integration Test:** `test_mock_chat_sse_server_streaming` verified successful streaming of delta tokens and `data: [DONE]`.

---

## 9. Local Storage & Conversation Test Evidence

- `test_storage_settings_runtime_path_persistence`: Verified saving and reloading custom `runtime_path`, `model_path`, and `server_port`.
- `test_storage_conversation_crud`: Verified conversation creation, serialization to JSON, reloading, and clearing history.

---

## 10. Automated Test Totals

| Test Suite | Tests Executed | Passed | Failed | Status |
|---|---|---|---|---|
| **Rust Desktop Backend (`desktop/src-tauri/`)** | **18** | **18** | **0** | **PASS** |
| **Existing Python Suite (`tests/`)** | **117** | **117** | **0** | **PASS** |
| **Total Test Count** | **135** | **135** | **0** | **100% PASS** |

### Breakdown of 18 Rust Tests:
1. `test_gate_parity_all_six_pass`
2. `test_gate_1_telemetry_integrity_failure_fails_closed`
3. `test_gate_2_avail_ram_individual_failure_and_deficit`
4. `test_gate_3_system_commit_individual_failure_and_boundaries`
5. `test_gate_4_avail_vram_individual_failure_and_boundaries`
6. `test_gate_5_no_competing_gpu_individual_failure`
7. `test_gate_6_disk_free_individual_failure_and_boundaries`
8. `test_multiple_simultaneous_gate_failures`
9. `test_telemetry_freshness_rule`
10. `test_gguf_validation_nonexistent_and_invalid_path`
11. `test_gguf_validation_corrupt_and_wrong_magic`
12. `test_gguf_validation_valid_mock_header`
13. `test_job_object_spawn_suspended_and_automatic_cleanup_on_close`
14. `test_job_object_manual_child_termination`
15. `test_storage_settings_runtime_path_persistence`
16. `test_storage_conversation_crud`
17. `test_chat_service_stop_generation_signal`
18. `test_mock_chat_sse_server_streaming`

---

## 11. Startup Time Measurements

Measured over 5 consecutive unloaded application launches:

| Run # | Launch Condition | Measured Startup Time (ms) | Main Working Set (MB) | Aggregate Tree WS (MB) |
|---|---|---|---|---|
| **Run 1** | Cold start | 146 ms | 53.93 MB | 152.70 MB |
| **Run 2** | Warm start | 77 ms | 53.54 MB | 147.18 MB |
| **Run 3** | Warm start | 80 ms | 53.79 MB | 153.67 MB |
| **Run 4** | Warm start | 62 ms | 53.79 MB | 156.40 MB |
| **Run 5** | Warm start | 77 ms | 53.77 MB | 153.64 MB |

- **Median Startup Time:** **77 ms**
- **Cold Startup Time:** **146 ms**

---

## 12. Aggregate Idle Memory Measurements

Sampled when NOVA Chat is fully initialized in unloaded standby:

- **Main Application Process (`nova-chat.exe`):**
  - Resident Working Set: **53.77 MB**
  - Private Commit: **22.18 MB**
- **Microsoft Edge WebView2 Render Child (`msedgewebview2.exe`):**
  - Resident Working Set: **99.87 MB**
  - Private Commit: **31.60 MB**
- **Total Aggregate Application Tree:**
  - **Aggregate Working Set:** **153.64 MB**
  - **Aggregate Private Commit:** **53.78 MB**
  - **Dedicated VRAM Allocation:** **0.00 MiB** (GPU is completely untouched while unloaded).

---

## 13. Installer Installation and Uninstallation Verification

1. **Silent Per-User Installation (`/S`):**
   - Installed to `$env:LOCALAPPDATA\NOVA Chat\`.
   - Result: `nova-chat.exe` and `uninstall.exe` created cleanly without UAC elevation.
2. **Launch from Installed Location:**
   - Executed `$env:LOCALAPPDATA\NOVA Chat\nova-chat.exe`: Verified PID 14048 running normally with 57.55 MB working set.
3. **Silent Uninstallation (`/S`):**
   - Executed `$env:LOCALAPPDATA\NOVA Chat\uninstall.exe /S`.
   - Post-uninstallation check: `nova-chat.exe` completely removed. Directory cleaned cleanly.

---

## 14. Remaining Blockers and Limitations

1. **Live Model Execution Pending:** End-to-end inference using real weights was intentionally **not performed**, in accordance with governance instructions.
2. **Runtime Distribution:** Because the pinned CUDA/llama.cpp runtime binaries total ~1.2 GB, the 1.90 MiB installer does not bundle them inside the NSIS archive. The user or installer script supplies the runtime folder, and NOVA Chat auto-discovers or validates it via Settings.

---

## 15. Explicit Confirmation of Inference Governance

- **Zero live inference calls occurred during this phase.**
- Neither `llama-cli.exe` nor `llama-server.exe` was launched with model weights.
- The existing one-shot smoke-test authorization was preserved untouched.
- All testing was performed using mock servers, header parsing, and harmless Windows sleep processes.
