# NOVA Chat Desktop — Phase 3A: Final Preflight and Smoke-Test Readiness Report

**Date:** 2026-11-05  
**Repository:** `Imagen2dragon/NOVA-compute`  
**Branch:** `feature/nova-chat-desktop`  
**Baseline HEAD:** `7d765f45888574751c8d674bba481fdaa5e2a01f`  
**Project Owner:** User  
**Lead Architect & Reviewer:** ChatGPT  
**Implementation Engineer:** Antigravity  

---

## 1. Executive Summary & Audit Verdict

Phase 3A was conducted under strict read-only execution boundaries. No real model weights were mapped into memory, no inference or token generation was executed, no third-party processes were terminated, and zero configuration or system modifications were made.

### Final Readiness Verdict: **BLOCKED — WITH EXACT GATE DEFICITS**
The fresh six-gate hardware admission audit executed via NOVA Chat's production telemetry sensor suite returned:
- **Gate 1 (`telemetry_integrity`):** **PASS** (Win32 & NVML sensors healthy, 0 errors, fresh timestamp)
- **Gate 2 (`avail_ram`):** **FAIL — DEFICIT: 2.790 GiB** (Measured: `7.210 GiB`, Required: `>= 10.000 GiB`)
- **Gate 3 (`system_commit`):** **PASS** (Measured: `41.1%`, Required: `< 90.0%`)
- **Gate 4 (`avail_vram`):** **PASS** (Measured: `5920.0 MiB`, Required: `>= 5500.0 MiB`)
- **Gate 5 (`no_competing_gpu`):** **PASS** (Measured: `0 active compute apps`, Required: `0`)
- **Gate 6 (`disk_free`):** **PASS** (Measured: `114.56 GiB`, Required: `>= 25.00 GiB`)
- **Model File Validation:** **PASS** (Valid GGUF v3 header, 614 tensors, 33 metadata KV pairs, 17.07 GiB file size)

**Per Strict Governance:** Because Gate 2 failed due to resident physical memory consumption by background user applications (`msedge.exe`, `claude.exe`, `antigravity.exe`), the runtime supervisor and Guided Model Loader correctly block model startup. Live inference is strictly gated until available physical RAM is recovered to at least 10.00 GiB and explicit authorization is granted by the Project Owner.

---

## 2. Read-Only Runtime Dependency Verification

### 2.1 Pinned Runtime Closure (10 DLLs + Executable)
All 10 required runtime libraries are verified present in the application-local directory `runtime/windows/llama-cpp/`. SHA-256 hashes and byte counts match the pinned upstream baseline:

| File Name | Byte Size | SHA-256 Digest | Status |
|---|---|---|---|
| `llama-server.exe` | 9,728 | `fe2957c5fc9defb76279a513ea58153b5990c14af8e2c9d2150c2d194bf72668` | Verified Present |
| `llama-server-impl.dll` | 10,091,520 | `98a151f1915cbeda0dfb7ef5ab860e3d672f9b173ec5b575f4b2e7ccf8be2985` | Verified Present |
| `mtmd.dll` | 2,156,032 | `73adcc2d33e6f8fca6e92d5c43be989d9c325393b4eb9d463e179fdc72deaa23` | Verified Present |
| `llama.dll` | 3,754,496 | `c58231599579acc8f36593cf1462fa4dfd2eaf7fab393f98cc1e15b570f7f5ee` | Verified Present |
| `llama-common.dll` | 8,346,112 | `a259f6d6257055f4f18b80b3b8139fe671e3c04110d7b2f2e1f3d155a672172a` | Verified Present |
| `ggml.dll` | 78,848 | `216ac54f1633abf9d4c690c4002f66bc6fb8def628b7727e4498660ac3543591` | Verified Present |
| `ggml-base.dll` | 827,904 | `45703aee2f00e637ed51e8fed4e9ed92872992a175be32a23e37459067d504d1` | Verified Present |
| `ggml-cuda.dll` | 594,123,776 | `2e878168ccb8260c21cf4095c2efd1d915d55b718998891a8218b537ac723616` | Verified Present |
| `cublas64_12.dll` | 100,033,536 | `e40202fe4223c1cd2d2dce7beec59e1ed61c7801bd827309183be9b50e358f4c` | Verified Present |
| `cublasLt64_12.dll` | 473,551,360 | `2a896460bef60ed57ef32b0875812f355a6984e671d638bb632f5e8c1d7a831f` | Verified Present |
| `cudart64_12.dll` | 553,984 | `d28e42265da7462162a54da6b7a99ea4fa2caf8139d862bb500db875d0b32dfc` | Verified Present |

### 2.2 Application-Local DLL Loading Path
Under the standard Windows PE loader resolution hierarchy:
1. The directory containing the executable (`runtime/windows/llama-cpp/`) takes precedence over system directories and `PATH`.
2. All 10 dependencies reside in the exact same directory as `llama-server.exe`.
3. `ServerManager::find_llama_server_binary()` invokes `verify_runtime_dependencies()`, guaranteeing that before any child process is launched, every one of the 10 libraries exists in the binary's parent directory.

### 2.3 CUDA & Driver Environment
Querying `nvidia-smi` confirms:
- **GPU Model:** NVIDIA GeForce RTX 4050 Laptop GPU (Device 0)
- **Total Dedicated VRAM:** 6,141 MiB (6.0 GiB)
- **Driver Model:** WDDM
- **Driver Version:** `592.82`
- **Supported CUDA Runtime:** `13.1` (Backward-compatible with CUDA 12.x / `cublas64_12.dll`)
- **Active Processes:** `No running processes found` (0 competing compute processes)

### 2.4 Executable Invocation Verification & Strict Caveat
A safe, read-only invocation of `llama-server.exe --version` was executed:
```
version: 0.8.0-dev (build 12032, commit dde879bc4)
built with Clang 20.1.8 for Windows x86_64
```
> [!IMPORTANT]
> **Dynamic Loader Verification Boundary:**  
> A successful `--version` invocation confirms that the Windows loader resolved static dependencies required for basic process initialization (`llama-server-impl.dll`, `MSVCP140.dll`, `VCRUNTIME140.dll`, UCRT). It does **NOT** verify that CUDA kernels or dynamic cublas contexts will initialize without error upon tensor loading. Full CUDA backend validation occurs only when the model server initializes GPU layers.

---

## 3. Visual C++ Runtime Availability

Inspection of the Windows Registry confirmed that the Microsoft Visual C++ 2015–2022 Redistributable (x64) is installed on the host system:

- **Registry Key:** `HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64`
- **Installed Version:** `v14.51.36247.00` (Major: 14, Minor: 51, Build: 36247)
- **Status:** Installed (`Installed = 1`)
- **System CRT DLLs:** `MSVCP140.dll`, `VCRUNTIME140.dll`, and `VCRUNTIME140_1.dll` are accessible in `%SystemRoot%\System32\` and satisfy all import requirements for `llama-server-impl.dll` and `mtmd.dll`.

---

## 4. Configuration D Manifest Audit

### 4.1 Canonical Argument Vector & Order
`ServerConfigManifest::configuration_d()` builds the approved Phase 2D/2E Configuration D baseline. `to_args_vector()` produces the exact ordered sequence:

```text
-m <canonical_model_path>
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
--port 18080
--api-key nova-<uuid>
```

### 4.2 Verifications Completed:
1. **Model Path Canonicalization:** `load_model` in `lib.rs` executes `Path::new(&target_path).canonicalize()` prior to assembling arguments.
2. **Strict Parameter Protection:** `ServerManager::spawn_process` constructs `ServerConfigManifest::configuration_d()` directly; no custom or stale settings from `settings.json` can override or corrupt the approved Configuration D flags.
3. **Secret Redaction:** The ephemeral UUID secret (`nova-<uuid>`) is generated via OS cryptographic PRNG (`uuid::Uuid::new_v4()`). Custom `std::fmt::Debug` implementations for `ServerConfigManifest` and `ServerManager` redact the secret as `"[REDACTED]"`. Unit test `test_server_config_manifest_debug_redacts_api_key` asserts that the secret string never appears in debug logs.

---

## 5. Fresh Six-Gate Preflight Audit Evidence

The admission preflight was executed live on the target machine using the production `run_preflight_audit` engine.

**Audit Captured:** `2026-11-05T19:56:41.699411+00:00`  
**Model Target:** `C:\Users\abdal\OneDrive\Desktop\nova_compute_antigravity_starter\models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`

| Gate Name | Status | Requirement | Live Measured Value | Failure Reason / Details |
|---|---|---|---|---|
| **Gate 1: `telemetry_integrity`** | **PASS** | All sensors SUCCESS | Healthy | All Win32 & NVML sensors operational |
| **Gate 2: `avail_ram`** | **FAIL** | `>= 10.00 GiB` | **7.210 GiB** | **Deficit: 2.790 GiB below 10.00 GiB baseline** |
| **Gate 3: `system_commit`** | **PASS** | `< 90.0%` | 41.1% | Commit charge well below paging limits |
| **Gate 4: `avail_vram`** | **PASS** | `>= 5500.0 MiB` | 5,920.0 MiB | Dedicated VRAM free (Total: 6,141.0 MiB) |
| **Gate 5: `no_competing_gpu`** | **PASS** | 0 active apps | 0 active apps | No competing compute/graphics workloads |
| **Gate 6: `disk_free`** | **PASS** | `>= 25.00 GiB` | 114.56 GiB | Target SSD volume has ample free space |
| **Model Header Integrity** | **PASS** | Valid GGUF v2/v3 | Valid GGUF v3 | 614 tensors, 33 metadata keys, 17.07 GiB |

### 5.1 Actionable Memory Recovery Guidance
To assist the user in resolving the 2.790 GiB physical RAM deficit, `query_candidate_processes()` identified the top non-system background memory consumers:

1. **`msedge.exe`** (28 instances): Working Set: `1,891.0 MB`, Private Commit: `3,233.5 MB`  
   *Guidance:* Save work and close active tabs or exit unused browser windows.
2. **`claude.exe`** (7 instances): Working Set: `930.6 MB`, Private Commit: `1,051.3 MB`  
   *Guidance:* Review process function. Close through normal application interface if unused.
3. **`antigravity.exe`** (14 instances): Working Set: `720.8 MB`, Private Commit: `1,196.9 MB`  
   *Guidance:* Development IDE parent process. Keep active for workflow.
4. **`language_server.exe`** (1 instance): Working Set: `597.1 MB`, Private Commit: `706.7 MB`  
5. **`firefox.exe`** (11 instances): Working Set: `239.1 MB`, Private Commit: `761.8 MB`  

*Impact Analysis:* Exiting `msedge.exe` (~1.89 GiB) and `claude.exe` (~0.93 GiB) through normal user action recovers **~2.82 GiB** of physical RAM, immediately clearing the 2.790 GiB deficit and bringing available RAM to > 10.0 GiB.

---

## 6. Process Lifecycle & Teardown Source Audit

### 6.1 `JobObject::terminate_jobs()`
From `desktop/src-tauri/src/job_object.rs`:
```rust
    /// Terminates all processes assigned to this Job Object kernel-side
    pub fn terminate_jobs(&self) -> Result<(), String> {
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;

        unsafe {
            if !self.handle.is_null() {
                let res = TerminateJobObject(self.handle, 1);
                if res == 0 {
                    return Err(format!(
                        "TerminateJobObject failed (code: {})",
                        windows_sys::Win32::Foundation::GetLastError()
                    ));
                }
            }
            Ok(())
        }
    }
```

### 6.2 `ServerManager::stop_server()`
From `desktop/src-tauri/src/server_manager.rs`:
```rust
    pub fn stop_server(&mut self) {
        if let Some(ref job) = self.job_object {
            let _ = job.terminate_jobs();
        }
        if let Some(mut child) = self.child.take() {
            log_diagnostic(&format!(
                "Terminating llama-server child (PID: {})",
                child.pid
            ));
            child.terminate();
        }
        self.job_object = None;
        self.start_time = None;
        self.ready = false;
        log_diagnostic(
            "llama-server stopped cleanly via Job Object; memory and GPU resources released.",
        );
    }
```

### 6.3 Process Wait and Handle Cleanup
From `desktop/src-tauri/src/job_object.rs`:
```rust
    pub fn terminate(&mut self) {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{TerminateProcess, WaitForSingleObject};
        unsafe {
            if !self.h_process.is_null() {
                let _ = TerminateProcess(self.h_process, 0);
                let _ = WaitForSingleObject(self.h_process, 2000);
                CloseHandle(self.h_process);
                self.h_process = std::ptr::null_mut();
            }
            if !self.h_thread.is_null() {
                CloseHandle(self.h_thread);
                self.h_thread = std::ptr::null_mut();
            }
        }
    }
```
*Confirmed Mechanism:* Process tree termination via `TerminateJobObject` is followed by explicit `WaitForSingleObject(self.h_process, 2000)` ensuring the Windows kernel completely terminates the process before closing process and thread handles.

### 6.4 `GenerationGuard::drop()`
From `desktop/src-tauri/src/chat.rs`:
```rust
impl Drop for GenerationGuard {
    fn drop(&mut self) {
        {
            let mut guard = self.state.lock().unwrap();
            if let Some(ref active) = guard.active {
                if active.id == self.gen_id {
                    guard.active = None;
                }
            }
        }
        if let Some(tx) = self.completion_tx.take() {
            let _ = tx.send(true);
        }
    }
}
```

### 6.5 Generation Task Ownership & Cancellation
From `desktop/src-tauri/src/chat.rs`:
```rust
    pub fn claim_generation_slot(
        &self,
    ) -> Result<(GenerationGuard, watch::Receiver<bool>), String> {
        let mut guard = self.state.lock().unwrap();
        if guard.is_unloading {
            return Err("Cannot send message: model is currently unloading.".to_string());
        }
        if guard.active.is_some() {
            return Err("Another generation is already in progress.".to_string());
        }
        let gen_id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (cancel_tx, cancel_rx) = watch::channel(false);
        let (completion_tx, completion_rx) = watch::channel(false);

        guard.active = Some(ActiveGeneration {
            id: gen_id,
            cancel_tx,
            completion_rx,
        });

        let gen_guard = GenerationGuard {
            state: self.state.clone(),
            gen_id,
            completion_tx: Some(completion_tx),
        };

        Ok((gen_guard, cancel_rx))
    }
```

### 6.6 Unload Completion Sequence
From `desktop/src-tauri/src/lib.rs`:
```rust
#[tauri::command]
async fn unload_model(
    context: State<'_, AppContext>,
    app_handle: AppHandle,
) -> Result<ServerStatus, String> {
    let _lifecycle_guard = context.lifecycle_lock.lock().await;

    // Set unloading flag so any new send_chat_message is rejected
    context.is_unloading.store(true, Ordering::SeqCst);

    // Invalidate any active authorization
    *context.model_load_authorization.lock().unwrap() = None;

    // Await actual active generation termination and socket release (no arbitrary sleep!)
    context.chat_service.stop_generation();
    context.chat_service.await_generation_complete().await;

    {
        let mut st = context.current_state.lock().unwrap();
        *st = AppState::ModelStopping;
    }
    let _ = app_handle.emit("nova://state-change", "MODEL_STOPPING");

    let status = {
        let mut sm = context.server_manager.lock().unwrap();
        sm.stop_server();
        sm.get_status()
    };

    {
        let mut st = context.current_state.lock().unwrap();
        *st = AppState::ModelUnloaded;
    }
    let _ = app_handle.emit("nova://state-change", "MODEL_UNLOADED");

    context.chat_service.end_unload();
    context.is_unloading.store(false, Ordering::SeqCst);
    Ok(status)
}
```

---

## 7. Generation Task Panic Regression Test

As requested in the Phase 2E review, an explicit panic unwind regression test was added to `desktop/src-tauri/src/tests.rs`:

```rust
    #[tokio::test]
    async fn test_chat_service_panic_unwind_clears_generation_slot_via_raii() {
        let chat_service = Arc::new(ChatService::new());
        assert!(!chat_service.is_generating());

        let cs = chat_service.clone();
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();

        // Spawn a task that claims the slot and then panics
        let handle = tokio::spawn(async move {
            let (_guard, _cancel_rx) = cs.claim_generation_slot().unwrap();
            started_tx.send(()).unwrap();
            panic!("Simulated worker panic inside stream processing");
        });

        // Wait until slot is claimed
        started_rx.await.unwrap();

        // Await the task join handle, which captures the panic
        let join_result = handle.await;
        assert!(join_result.is_err(), "Task must terminate with panic error");
        assert!(
            join_result.unwrap_err().is_panic(),
            "Join error must be marked as panic"
        );

        // RAII Drop MUST have executed during unwinding when _guard was dropped
        assert!(
            !chat_service.is_generating(),
            "RAII Drop must have cleared active generation slot during panic unwinding"
        );

        // A subsequent generation must succeed immediately without deadlock or rejected slot
        let second_claim = chat_service.claim_generation_slot();
        assert!(
            second_claim.is_ok(),
            "Subsequent generation slot claim must succeed immediately after panic"
        );
    }
```

**Execution Result:** `test tests::tests::test_chat_service_panic_unwind_clears_generation_slot_via_raii ... ok` (Verified passing).

---

## 8. Production Storage Integrity Audit

A read-only audit of `%LOCALAPPDATA%\\NOVA Chat\\` confirms that production data remained 100% untouched throughout all preflight and test operations:

| File Name | Byte Size | Last Modified (Local) | SHA-256 Digest | Audit Status |
|---|---|---|---|---|
| `conversations.json` | 2 | 2026-11-05 20:06:02 | `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` | Unaltered |
| `settings.json` | 334 | 2026-11-05 20:51:09 | `de609535aa10b219aa173bb2150138e4602c71788d78d762c3b9a82099b35e24` | Unaltered |
| `logs/app.log` | 35,561 | 2026-11-05 22:33:19 | `fadba8f50c08e8e501516444232f7b095242a49c1a7f0671a2c102013368309d` | Diagnostics only |

---

## 9. Comprehensive Verification Suite Results

| Test Suite | Total Tests | Passed | Failed | Duration | Status |
|---|---|---|---|---|---|
| Rust Desktop Backend (`cargo test`) | 52 | 52 | 0 | 1.57s | **100% PASS** |
| Rust Code Formatting (`cargo fmt --check`) | N/A | Clean | 0 | 0.40s | **100% PASS** |
| Python Core Engine (`unittest`) | 117 | 117 | 0 | 4.58s | **100% PASS** |
| TypeScript Frontend Build (`npm run build`) | Vite bundle | Done | 0 | 1.64s | **100% PASS** |
| **Combined Total** | **169** | **169** | **0** | **~8.2s** | **ALL PASS** |

---

## 10. Strict Authorization Boundary Notice

**INSPECTION ONLY COMPLETED — NO MODEL WAS LOADED.**

In accordance with Section 8 of the prompt and strict NOVA Compute development governance:
1. `llama-server.exe` was never spawned with model parameters.
2. `llama-cli.exe` was never spawned with model parameters.
3. No weights were mapped to physical RAM or VRAM.
4. When the user closes background applications and physical RAM passes Gate 2, **the engine will still NOT load the model until the Project Owner provides explicit, separate execution authorization.**
