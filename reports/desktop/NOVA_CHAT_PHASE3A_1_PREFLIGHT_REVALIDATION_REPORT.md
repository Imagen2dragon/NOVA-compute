# NOVA Chat Desktop — Phase 3A.1: Memory Recovery and Fresh Preflight Revalidation Report

**Date:** 2026-11-05  
**Repository:** `Imagen2dragon/NOVA-compute`  
**Branch:** `feature/nova-chat-desktop`  
**Baseline HEAD:** `7d765f45888574751c8d674bba481fdaa5e2a01f`  
**Project Owner:** User  
**Lead Architect & Reviewer:** ChatGPT  
**Implementation Engineer:** Antigravity  

---

## 1. Executive Summary & Final Verdict

Phase 3A.1 revalidation was executed in accordance with strict machine safety governance:
- **Zero Live Model Invocations:** Neither `llama-server.exe` nor `llama-cli.exe` was invoked with model paths.
- **Zero Application Terminations:** In accordance with Section 1, no third-party processes (`msedge.exe`, `claude.exe`, etc.) were terminated or suspended. Memory recovery is strictly the Project Owner's prerogative.
- **Deterministic Test Suite Isolation:** `test_live_system_preflight_audit` was converted to `#[ignore]` so that regular `cargo test` executions rely purely on deterministic mock fixtures.

### Final Readiness Verdict: **BLOCKED — EXACT REMAINING DEFICITS**

A fresh read-only six-gate preflight was captured using the production `run_preflight_audit` telemetry implementation.

- **Gate 1 (`telemetry_integrity`):** **PASS** (Win32 & NVML sensors healthy, 0 errors)
- **Gate 2 (`avail_ram`):** **FAIL — DEFICIT: 3.204 GiB** (Measured: `6.800 GiB`, Required: `>= 10.000 GiB`)
- **Gate 3 (`system_commit`):** **PASS** (Measured: `41.0%`, Required: `< 90.0%`)
- **Gate 4 (`avail_vram`):** **PASS** (Measured: `5,920.0 MiB`, Required: `>= 5,500.0 MiB`)
- **Gate 5 (`no_competing_gpu`):** **PASS** (Measured: `0 active compute apps`, Required: `0`)
- **Gate 6 (`disk_free`):** **PASS** (Measured: `114.54 GiB`, Required: `>= 25.00 GiB`)
- **Model File Validation:** **PASS** (Valid GGUF v3 header, 614 tensors, 33 metadata keys, 17.07 GiB)

**Status:** The system remains **BLOCKED** from launching inference due to the Gate 2 available-RAM deficit. Live inference remains strictly gated until the Project Owner closes unwanted applications to achieve >= 10.000 GiB available physical RAM and grants explicit authorization.

---

## 2. Capture Timestamps & Timezone Discrepancy Resolution

### 2.1 Revalidation Timestamps
- **UTC Timestamp:** `2026-11-05T20:13:09.943488200+00:00`
- **Africa/Addis_Ababa (UTC+3 / EAT) Timestamp:** `2026-11-05T23:13:09.943512700+03:00`
- **Windows System Local Time:** `Thu Nov 5 23:13:09 2026` (`E. Africa Standard Time`, Base UTC Offset: `+03:00`)

### 2.2 Resolution of Previous Timestamp Confusion
In earlier Phase 2 and Phase 3 reports, timestamps generated in the local time zone (`22:xx` / `23:xx`) appeared to readers located in Western time zones (UTC/GMT, US EST/PST) as being "in the future" when compared against their local clocks or unadjusted UTC timestamps.
- The host system's hardware real-time clock and OS timezone are configured for **East Africa Time (EAT / UTC+3)**, matching Addis Ababa / Nairobi.
- At UTC `20:13:09Z`, local time in Addis Ababa is exactly `23:13:09`.
- All telemetry structures in `admission.rs` now explicitly record timestamps with unambiguous RFC 3339 timezone offsets (`Z` or `+03:00`), eliminating any ambiguity.

---

## 3. Fresh Six-Gate Preflight Measurements

| Gate Name | Status | Threshold Requirement | Live Measured Value | Gate Margin / Deficit |
|---|---|---|---|---|
| **Gate 1: `telemetry_integrity`** | **PASS** | All sensors SUCCESS | Healthy | All Win32 & NVML sensors reporting 0 errors |
| **Gate 2: `avail_ram`** | **FAIL** | `>= 10.000 GiB` | **6.800 GiB** | **Deficit: 3.204 GiB below 10.000 GiB baseline** |
| **Gate 3: `system_commit`** | **PASS** | `< 90.0%` | 41.0% | Commit margin: 49.0% below threshold |
| **Gate 4: `avail_vram`** | **PASS** | `>= 5,500.0 MiB` | 5,920.0 MiB | Free VRAM margin: +420.0 MiB above threshold |
| **Gate 5: `no_competing_gpu`** | **PASS** | 0 active compute apps | 0 active apps | Dedicated GPU is 100% idle |
| **Gate 6: `disk_free`** | **PASS** | `>= 25.00 GiB` | 114.54 GiB | SSD margin: +89.54 GiB free space |
| **Model Header Integrity** | **PASS** | Valid GGUF v2/v3 | Valid GGUF v3 | `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` verified |

---

## 4. RAM Change Analysis Compared to Phase 3A

In Phase 3A (captured at `19:56:41 UTC` / `22:56:41 EAT`):
- **Phase 3A Available RAM:** `7.210 GiB` (Deficit: `2.790 GiB`)

In Phase 3A.1 (captured at `20:13:09 UTC` / `23:13:09 EAT`):
- **Phase 3A.1 Available RAM:** `6.800 GiB` (Deficit: `3.204 GiB`)
- **Net RAM Delta:** **-0.410 GiB (-419.8 MiB)**

### Root Cause of the Decrease:
Per strict safety rules, the implementation engineer did not terminate any applications. In the ~16 minutes between audits, background browser tabs in `msedge.exe` allocated additional working set memory:

| Process Name | Instances | Phase 3A Working Set | Phase 3A.1 Working Set | Delta |
|---|---|---|---|---|
| `msedge.exe` | 28 | 1,891.0 MB | 2,210.7 MB | **+319.7 MB** |
| `claude.exe` | 6 (-1) | 930.6 MB | 873.1 MB | -57.5 MB |
| `antigravity.exe` | 14 | 720.8 MB | 735.0 MB | +14.2 MB |
| `language_server.exe` | 1 | 597.1 MB | 559.0 MB | -38.1 MB |
| `firefox.exe` | 11 | 239.1 MB | 282.2 MB | +43.1 MB |
| `explorer.exe` | 1 | 153.2 MB | 176.7 MB | +23.5 MB |

**Takeaway:** Physical memory will only be recovered when the Project Owner manually saves work and closes unused browser windows (`msedge.exe`) and desktop apps (`claude.exe`). Closing Edge (~2.2 GiB) and Claude (~0.87 GiB) will release ~3.08 GiB, bringing available RAM to ~9.88–10.0 GiB.

---

## 5. CUDA and Runtime Inventory

### 5.1 Host GPU & Driver
- **GPU Hardware:** NVIDIA GeForce RTX 4050 Laptop GPU (AD107M, 6,141 MiB dedicated VRAM, 75W TGP).
- **Driver Model:** WDDM 3.1.
- **NVIDIA Display Driver Version:** `592.82` (Installed on host Windows OS).
- **Maximum Driver-Supported CUDA Version:** `CUDA 13.1` (as reported by `nvidia-smi 592.82`).

### 5.2 Bundled Application Runtime Libraries
The application bundles the CUDA 12.x runtime components in `runtime/windows/llama-cpp/`:
- `cublas64_12.dll` (100,033,536 bytes, SHA-256: `e40202fe...`) -> cuBLAS library for CUDA 12.
- `cublasLt64_12.dll` (473,551,360 bytes, SHA-256: `2a896460...`) -> cuBLASLt library for CUDA 12.
- `cudart64_12.dll` (553,984 bytes, SHA-256: `d28e4226...`) -> CUDA Runtime for CUDA 12.
- `ggml-cuda.dll` (594,123,776 bytes, SHA-256: `2e878168...`) -> GGML CUDA backend compiled against CUDA 12.

### 5.3 Critical Verification Boundary: Driver vs. Backend Initialization
- **Driver Compatibility:** The host driver (CUDA 13.1) is backward-compatible with the bundled CUDA 12.x runtime libraries.
- **`llama-server.exe --version` Scope:** Running `llama-server.exe --version` loads basic CRT and CPU dependencies (`llama-server-impl.dll`, `MSVCP140.dll`, UCRT). It does **NOT** initialize a CUDA context, allocate VRAM, or load CUDA kernels.
- **Unverified Status:** Actual CUDA backend initialization (`cuInit`, device context creation, CUBLAS handle creation, CUDA graph compilation, and MoE tensor offloading) remains **unverified** until the model server is authorized to load weights on the physical GPU.

---

## 6. Deterministic Test Suite Hardening

### 6.1 Test Suite Isolation
In accordance with Section 5 of the prompt:
- `test_live_system_preflight_audit` in [`desktop/src-tauri/src/tests.rs`](file:///c:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/desktop/src-tauri/src/tests.rs#L1826-L1865) has been decorated with `#[ignore = "Live system hardware diagnostic; manual invocation only via -- --ignored"]`.
- The standard `cargo test` command now runs exclusively **deterministic, mock-based unit tests** that never query live machine sensors and cannot fail due to host memory or VRAM state.

### 6.2 Standard Deterministic Test Run (`cargo test`)
Command: `cargo test --manifest-path desktop/src-tauri/Cargo.toml`
```
running 52 tests
test tests::tests::test_confirmation_preflight_alone_cannot_authorize_load ... ok
test tests::tests::test_confirmation_token_binding_and_expiration ... ok
test tests::tests::test_confirmation_invalidated_on_model_path_change_or_unload ... ok
test tests::tests::test_chat_service_stop_generation_signal ... ok
test tests::tests::test_chat_service_task_abort_clears_generation_slot_via_raii ... ok
test tests::tests::test_chat_service_panic_unwind_clears_generation_slot_via_raii ... ok
test tests::tests::test_gguf_validation_nonexistent_and_invalid_path ... ok
test tests::tests::test_confirmation_concurrent_token_consumption_race ... ok
test tests::tests::test_chat_service_unexpected_eof_without_done ... ok
test tests::tests::test_chat_service_mock_sse_streaming_with_done ... ok
test tests::tests::test_chat_service_malformed_sse_json ... ok
test tests::tests::test_lifecycle_completion_before_wait ... ok
test tests::tests::test_lifecycle_failed_server_does_not_restore_model_ready ... ok
test tests::tests::test_lifecycle_generation_admission_unload_interleaving_race ... ok
test tests::tests::test_lifecycle_generation_rejected_during_unload ... ok
test tests::tests::test_gguf_validation_corrupt_and_wrong_magic ... ok
test tests::tests::test_live_system_preflight_audit ... ignored, Live system hardware diagnostic; manual invocation only via -- --ignored
test tests::tests::test_gguf_validation_valid_mock_header ... ok
test tests::tests::test_job_object_manual_child_termination ... ok
test tests::tests::test_nvml_return_code_mapping_and_two_call_behavior ... ok
test tests::tests::test_server_config_manifest_debug_redacts_api_key ... ok
test tests::tests::test_server_config_manifest_exact_vector_and_order ... ok
test tests::tests::test_server_manager_debug_redacts_api_key ... ok
test tests::tests::test_mtmd_dll_verified_in_required_dependencies ... ok
test tests::tests::test_gate_parity_all_six_pass ... ok
test tests::tests::test_gate_1_telemetry_integrity_failure_fails_closed ... ok
test tests::tests::test_storage_corrupted_primary_with_invalid_backup_returns_error ... ok
test tests::tests::test_storage_conversations_corrupted_returns_error ... ok
test tests::tests::test_lifecycle_repeated_simultaneous_send_and_unload ... ok
test tests::tests::test_storage_interrupted_save_cleans_up_temp_file ... ok
test tests::tests::test_storage_write_errors_returned ... ok
test tests::tests::test_storage_production_data_untouched_regression ... ok
test tests::tests::test_multiple_simultaneous_gate_failures ... ok
test tests::tests::test_storage_isolated_unique_temp_dir ... ok
test tests::tests::test_gate_5_no_competing_gpu_individual_failure ... ok
test tests::tests::test_gate_4_avail_vram_individual_failure_and_boundaries ... ok
test tests::tests::test_gate_6_disk_free_individual_failure_and_boundaries ... ok
test tests::tests::test_gate_2_avail_ram_individual_failure_and_deficit ... ok
test tests::tests::test_telemetry_monotonic_freshness_clock_boundaries ... ok
test tests::tests::test_telemetry_sensor_failure_fails_all_admission ... ok
test tests::tests::test_storage_atomic_write_and_recovery_from_backup ... ok
test tests::tests::test_gate_3_system_commit_individual_failure_and_boundaries ... ok
test tests::tests::test_chat_service_mock_sse_unicode_chunk_splitting ... ok
test tests::tests::test_chat_service_old_generation_cannot_clear_new_generation_state ... ok
test tests::tests::test_chat_service_cancellation_while_stalled ... ok
test tests::tests::test_lifecycle_unload_awaits_generation_task_termination ... ok
test tests::tests::test_lifecycle_simultaneous_unload_requests ... ok
test tests::tests::test_job_object_terminate_jobs_kills_process_tree ... ok
test tests::tests::test_chat_service_cancellation_race_repeated_immediate_second_generation ... ok
test tests::tests::test_server_readiness_probe_all_seven_scenarios ... ok
test tests::tests::test_job_object_spawn_suspended_and_automatic_cleanup_on_close ... ok
test tests::tests::test_job_object_parent_termination_cleans_up_child ... ok

test result: ok. 51 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 1.53s
```

### 6.3 Manual Live Preflight Diagnostic Command
Command: `cargo test test_live_system_preflight_audit --manifest-path desktop/src-tauri/Cargo.toml -- --ignored --nocapture`
Result: Ran and reported live metrics (1 passed, 51 filtered out in 1.54s).

---

## 7. Production Storage Integrity Audit

Cryptographic audit of `%LOCALAPPDATA%\\NOVA Chat\\` confirms that production files remained unaltered:

| File Name | Byte Size | Last Modified (Local EAT) | SHA-256 Digest | Status |
|---|---|---|---|---|
| `conversations.json` | 2 | 2026-11-05 20:06:02 | `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` | Unaltered |
| `settings.json` | 334 | 2026-11-05 20:51:09 | `de609535aa10b219aa173bb2150138e4602c71788d78d762c3b9a82099b35e24` | Unaltered |
| `logs/app.log` | 42,868 | 2026-11-05 23:32:18 | `f17d13cc1d5cb82d076f36c9e9a56a109cb7266806ab40f10cda83aa563681f6` | Append-only diagnostics |

---

## 8. Summary of Verification Commands & Test Results

| Suite / Tool | Command Line | Outcome |
|---|---|---|
| Rust Unit Tests | `cargo test --manifest-path desktop/src-tauri/Cargo.toml` | 51 passed, 1 ignored (100% deterministic pass) |
| Rust Code Formatting | `cargo fmt --check --manifest-path desktop/src-tauri/Cargo.toml` | 0 errors (clean) |
| Python Core Tests | `python -m unittest discover -s tests` | 117 passed, 0 failed (100% pass) |
| Desktop UI Bundle | `npm --prefix desktop run build` | 2,076 modules transformed, 0 errors |
| Live Diagnostic | `cargo test test_live_system_preflight_audit -- --ignored --nocapture` | Live telemetry verified |

---

## 9. Strict Authorization Boundary Notice

**READ-ONLY AUDIT ONLY — NO MODEL WAS LOADED.**

In accordance with strict project rules:
1. No inference process was spawned.
2. No weights were mapped.
3. The Project Owner has not yet closed memory-consuming applications, so Gate 2 properly remains in the **BLOCKED** state.
4. When the Project Owner decides to close background applications and available physical RAM satisfies Gate 2, **a fresh preflight audit will be recorded, but live execution will NOT occur until the Project Owner grants explicit, separate execution authorization.**
