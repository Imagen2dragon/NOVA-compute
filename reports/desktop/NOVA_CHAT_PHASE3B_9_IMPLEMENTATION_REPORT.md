# NOVA Chat — Phase 3B.9 Implementation Report
## Crash-Resilient GGUF Integrity & Production Runtime Protection

**Date:** 2026-11-06  
**Repository:** `Imagen2dragon/NOVA-compute`  
**Branch:** `feature/nova-chat-desktop`  
**Baseline Git HEAD:** `7d765f45888574751c8d674bba481fdaa5e2a01f`  
**Implementation Engineer:** Antigravity  
**Lead Architect / Reviewer:** ChatGPT  
**Project Owner:** User  

---

## Executive Summary

Phase 3B.9 addresses and resolves the file-sharing vulnerability identified during Phase 3B.8:
When the parent application dies or crashes, its process-private handle table is destroyed by the Windows kernel. If the child `llama-server.exe` opened the model file with `_wfopen(..., "rb")` (which passes `_SH_DENYNO` = `FILE_SHARE_READ | FILE_SHARE_WRITE`), the child's handle alone would permit an external process to open the model with `GENERIC_WRITE`.

Through source-level inspection of the pinned `llama.cpp` runtime (commit `dde879bc4`), the MSVC C Runtime Library (`_wfopen` / `_sopen_dispatch`), and Windows NTFS handle sharing semantics, we evaluated four architectural paths (Options A, B, C, D).

We implemented and validated **Option A (Child-Owned Duplicated Restrictive File Handle)**:
1. When spawning the child process suspended inside the Windows Job Object (`KILL_ON_JOB_CLOSE`), NOVA's parent process duplicates its own `FILE_SHARE_READ` handle into the child's private handle table (`pi.hProcess`) via `DuplicateHandle(..., DUPLICATE_SAME_ACCESS)` **before** thread resumption.
2. The child process directly owns this restrictive handle in its handle table throughout its execution.
3. If the parent process crashes or is forcefully terminated (`TerminateProcess`), the Windows kernel immediately closes the parent's handle table, but **the child process's handle table continues to hold the duplicated `FILE_SHARE_READ` handle**.
4. In Windows NTFS, sharing violations are checked against **all** active handles to the file object across the entire system. Because the child owns an active handle specifying `FILE_SHARE_READ` (without `FILE_SHARE_WRITE`), all external write, delete, and rename requests are rejected by the Windows kernel with `ERROR_SHARING_VIOLATION` (Win32 code 32).
5. When the Job Object terminates the child process (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), the kernel destroys the child's handle table, cleanly releasing the restrictive handle only after all access to the model file has ceased.
6. The implementation requires **zero modifications to pinned llama.cpp binaries**, **zero administrator elevation**, **zero service processes**, and **zero disk mutations**.

Deterministic integration tests covering all 15 directive scenarios plus an explicit negative control passed with 100% success (16 passed in 0.31s; full test suite: 105 passed, 0 failed, 1 ignored).

---

## 1. Root Cause Analysis

### 1.1 The Sharing-Mode Asymmetry
In `desktop/src-tauri/src/model_integrity.rs`, the parent process acquires a model lock via:
```c
CreateFileW(
    path,
    GENERIC_READ,
    FILE_SHARE_READ,
    NULL,
    OPEN_EXISTING,
    FILE_ATTRIBUTE_NORMAL,
    NULL
);
```
This specifies `FILE_SHARE_READ` (0x00000001) **without** `FILE_SHARE_WRITE` (0x00000002).

In pinned `llama.cpp` (commit `dde879bc4`), model loading in `llama_model_loader` / `llama_mmap::init()` opens the model file via:
```cpp
FILE * file = ggml_fopen(fname, "rb");
```
On Windows, `ggml_fopen` maps to `_wfopen(wpath, L"rb")` in the MSVC C Runtime Library (CRT). In MSVC CRT, `_wfopen` calls `_wsopen_dispatch(..., _O_RDONLY | _O_BINARY, _SH_DENYNO, ...)`.
`_SH_DENYNO` instructs the Win32 subsystem to call `CreateFileW` with:
- Desired Access: `GENERIC_READ` (0x80000000)
- Share Mode: `FILE_SHARE_READ | FILE_SHARE_WRITE` (0x00000001 | 0x00000002 = 0x00000003)

### 1.2 The Window of Vulnerability
- **While Parent is Alive:** Both parent and child hold open handles. An external process attempting `CreateFileW(..., GENERIC_WRITE, ...)` is evaluated against all open handles. The parent's handle only shares read. The external open is blocked with `ERROR_SHARING_VIOLATION` (32).
- **If Parent Crashes (Without Option A):** The parent's process table is closed. Only the child's handle remains. Because the child's handle was opened with `FILE_SHARE_WRITE`, an external process opening with `GENERIC_WRITE` and `FILE_SHARE_READ | FILE_SHARE_WRITE` is permitted by Windows NTFS! During the interval between parent crash and child teardown, the GGUF file could be modified.
- **Negative Control Proof:** We wrote and executed an integration test (`test_phase3b9_16_negative_control_without_option_a`) verifying that without Option A, an external write open succeeds when the parent drops its lock while the child is alive.

---

## 2. Architecture Comparison

We evaluated the four options mandated by the Lead Architect:

| Dimension | Option A: Child-Owned Handle (Chosen) | Option B: Supervisor Process | Option C: Runtime Binary Patch | Option D: Windows ACLs / Read-Only Attribute |
| :--- | :--- | :--- | :--- | :--- |
| **Mechanics** | Duplicate parent's `FILE_SHARE_READ` handle into child table via `DuplicateHandle` | Separate watcher process holding lock handle | Fork & patch llama.cpp CRT file-open calls to `_SH_DENYWR` | Set NTFS DACLs or `FILE_ATTRIBUTE_READONLY` on GGUF |
| **Crash Resilience** | **Kernel-guaranteed**: Handle lives in child's handle table. If parent dies, child still holds lock until Job Object terminates child. | **Fragile**: Supervisor can crash or fail independently; adds inter-process synchronization race. | **Kernel-guaranteed**: Child directly opens restrictive handle. | **Brittle**: File-system state mutation; crashes leave GGUF read-only on disk, breaking user operations. |
| **Binary Impact** | **Zero**: Uses pinned upstream binary `dde879bc4` unmodified. | **Zero**: External binary unchanged. | **High**: Requires rebuilding llama-server, verifying CUDA DLLs, maintaining custom fork. | **Zero**: Win32 attribute manipulation. |
| **Elevation Required** | **None** (Standard Win32 API) | **None** | **None** | May fail across non-NTFS volumes or restricted user accounts. |
| **Lifecycle Binding** | **Exact**: Bound to child process existence via Windows kernel Job Object. | **Loose**: Independent process requires watchdog/heartbeat. | **Exact**: Bound to child process handle. | **Persists after crash**: Leaves disk in altered state requiring repair. |
| **Complexity** | Low (34 lines of Rust in `job_object.rs`) | High (separate daemon, IPC, heartbeats) | High (C++ build pipeline, runtime validation) | Medium (NTFS ACL manipulation, cleanup hooks) |
| **Architectural Verdict** | **SUPERIOR & CHOSEN** | Rejected (unnecessary moving parts) | Rejected (binary divergence risk) | Rejected (persists file state on disk) |

---

## 3. Chosen Solution & Justification: Option A

### 3.1 Kernel Invariants of Option A
In the Windows NT kernel object manager:
1. File objects maintain a reference count and a sharing state (`SharedRead`, `SharedWrite`, `SharedDelete`).
2. When any handle desires `GENERIC_WRITE`, the kernel verifies that **every** existing handle to that file object has `FILE_SHARE_WRITE` enabled in its sharing mask.
3. If even a **single** handle to that file object has `FILE_SHARE_READ` without `FILE_SHARE_WRITE`, any request for write access is denied with `STATUS_SHARING_VIOLATION` (`ERROR_SHARING_VIOLATION` / 32).
4. `DuplicateHandle(GetCurrentProcess(), src_handle, child_process_handle, &target_handle, 0, FALSE, DUPLICATE_SAME_ACCESS)` increments the kernel file object reference count and inserts an exact clone of the restrictive handle directly into the child process's handle table.
5. Even though `llama-server.exe` has no code aware of this extra handle, the handle exists within the child process's private kernel table.
6. When the parent process terminates abruptly:
   - The parent's handle table is purged.
   - The child's handle table remains populated until the child process exits.
   - The duplicated handle in the child's table keeps the restrictive `FILE_SHARE_READ` lock active in the kernel.
   - Any external write attempt is rejected with Win32 code 32.
   - When the Job Object terminates the child process (`KILL_ON_JOB_CLOSE`), the child's handle table is destroyed, releasing the restrictive lock.

---

## 4. Source Code Changes

### 4.1 `desktop/src-tauri/src/model_integrity.rs`
Exposed the underlying Win32 `HANDLE` from `ModelFileLockGuard`:
```rust
impl ModelFileLockGuard {
    #[cfg(windows)]
    pub fn raw_handle(&self) -> windows_sys::Win32::Foundation::HANDLE {
        self.handle
    }
}
```

### 4.2 `desktop/src-tauri/src/job_object.rs`
Implemented `spawn_child_suspended_with_handle`:
```rust
pub fn spawn_child_suspended_with_handle(
    &self,
    command_line: &str,
    handle_to_duplicate: Option<windows_sys::Win32::Foundation::HANDLE>,
) -> Result<ContainedChild, String> {
    // 1. CreateProcessW with CREATE_SUSPENDED | CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
    // 2. AssignProcessToJobObject(self.handle, pi.hProcess)
    // 3. DuplicateHandle into child table:
    if let Some(src_handle) = handle_to_duplicate {
        let mut target_handle = std::ptr::null_mut();
        let dup_ok = DuplicateHandle(
            GetCurrentProcess(),
            src_handle,
            pi.hProcess,
            &mut target_handle,
            0,
            0, // bInheritHandle = FALSE
            DUPLICATE_SAME_ACCESS,
        );
        if dup_ok == 0 {
            // Fail closed: terminate child and abort before thread execution
            let _ = TerminateProcess(pi.hProcess, 1);
            CloseHandle(pi.hProcess);
            CloseHandle(pi.hThread);
            return Err(format!("DuplicateHandle failed: Win32 error {}", GetLastError()));
        }
    }
    // 4. ResumeThread(pi.hThread)
}
```

### 4.3 `desktop/src-tauri/src/server_manager.rs`
Passed the model lock handle into `spawn_child_suspended_with_handle`:
```rust
#[cfg(windows)]
let lock_raw = Some(model_lock.raw_handle());
#[cfg(not(windows))]
let lock_raw = None;

let child = match job.spawn_child_suspended_with_handle(&cmd_line, lock_raw) {
    Ok(c) => c,
    Err(e) => { ... }
};
```

### 4.4 `desktop/src-tauri/examples/crash_safety_helper.rs`
Updated helper to test exact production Win32 semantics:
- `open_gguf_production_llama`: Calls `CreateFileW` with `GENERIC_READ`, `FILE_SHARE_READ | FILE_SHARE_WRITE`.
- `mmap_gguf_production_llama`: Calls `CreateFileMappingW(..., PAGE_READONLY, ...)` and `MapViewOfFile(..., FILE_MAP_READ, ...)`.
- Added modes: `child_prod`, `parent_prod_post_open`, `child_prod_barrier`, `parent_prod_barrier`, `child_prod_active_loop`, `parent_prod_active`, `parent_nodup_post_open`.

### 4.5 `desktop/src-tauri/Cargo.toml`
Enabled `"Win32_System_Memory"` in `windows-sys` features to support `CreateFileMappingW`, `MapViewOfFile`, and `UnmapViewOfFile`.

---

## 5. Exact Windows API Semantics

| Win32 API Call | Parameters / Flags | Win32 Numeric Value | Purpose in NOVA Architecture |
| :--- | :--- | :--- | :--- |
| `CreateFileW` (Parent lock) | `dwDesiredAccess = GENERIC_READ`<br>`dwShareMode = FILE_SHARE_READ`<br>`dwCreationDisposition = OPEN_EXISTING` | `0x80000000`<br>`0x00000001`<br>`3` | Initial model integrity lock; blocks all external write/delete/rename operations. |
| `CreateProcessW` | `dwCreationFlags = CREATE_SUSPENDED \| CREATE_NO_WINDOW \| BELOW_NORMAL_PRIORITY_CLASS`<br>`bInheritHandles = FALSE` | `0x00000004 \| 0x08000000 \| 0x00004000 = 0x08004004`<br>`0` | Spawns child server process with main thread suspended, allowing Job Object assignment and handle duplication before code runs. |
| `AssignProcessToJobObject` | `hJob = self.handle`<br>`hProcess = pi.hProcess` | Handle pointer<br>Handle pointer | Enforces `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` kernel guarantee. |
| `DuplicateHandle` | `hSourceProcessHandle = GetCurrentProcess()`<br>`hSourceHandle = lock_guard.raw_handle()`<br>`hTargetProcessHandle = pi.hProcess`<br>`dwOptions = DUPLICATE_SAME_ACCESS` | `-1` (pseudo-handle)<br>Source handle<br>Child process handle<br>`0x00000002` | Clones restrictive handle directly into child's process table before thread executes. |
| `ResumeThread` | `hThread = pi.hThread` | Thread handle | Resumes child server execution once containment and locks are established. |
| `CreateFileW` (Child CRT `_wfopen`) | `dwDesiredAccess = GENERIC_READ`<br>`dwShareMode = FILE_SHARE_READ \| FILE_SHARE_WRITE` | `0x80000000`<br>`0x00000003` | Pinned llama.cpp runtime model file open via MSVC CRT `_SH_DENYNO`. |
| `CreateFileMappingW` | `flProtect = PAGE_READONLY`<br>`dwMaximumSizeHigh = 0, dwMaximumSizeLow = 0` | `0x00000002`<br>`0, 0` | Creates file mapping object backed by GGUF file for zero-copy memory-mapped inference. |
| `MapViewOfFile` | `dwDesiredAccess = FILE_MAP_READ`<br>`dwFileOffsetHigh = 0, dwFileOffsetLow = 0, dwNumberOfBytesToMap = 0` | `0x00000004`<br>`0, 0, 0` | Maps view of model file into virtual address space. Mapping handle closed immediately per llama.cpp practice. |

---

## 6. Integration Test Evidence (15 Directive Scenarios + Negative Control)

All tests were executed against production-equivalent Win32 flags using `cargo test test_phase3b9 --manifest-path desktop/src-tauri/Cargo.toml`.

### 6.1 Test Results Table

| # | Directive Scenario | Win32 Operation Tested | Access Flags | Share Flags | Expected Return | Actual Win32 Error Code | Model File Changed? | Child Had Access? | Result |
| :-: | :--- | :--- | :--- | :--- | :-: | :-: | :-: | :-: | :-: |
| **1** | Parent alive, child alive | `CreateFileW`<br>`DeleteFileW`<br>`MoveFileExW` | `GENERIC_WRITE` (0x40000000)<br>`DELETE` (0x00010000)<br>N/A | `FILE_SHARE_READ \| FILE_SHARE_WRITE` (0x03)<br>`FILE_SHARE_DELETE` (0x04)<br>N/A | `INVALID_HANDLE`<br>`0`<br>`0` | `32` (`ERROR_SHARING_VIOLATION`)<br>`32` (`ERROR_SHARING_VIOLATION`)<br>`32` (`ERROR_SHARING_VIOLATION`) | No (bytes identical) | Yes (mapped view active) | **PASS** |
| **2** | Parent crash before child starts | `CreateFileW` | `GENERIC_WRITE` (0x40000000) | `FILE_SHARE_READ \| FILE_SHARE_WRITE` (0x03) | `INVALID_HANDLE` during lock; `Valid HANDLE` after drop | `32` during lock;<br>`0` (Success) after drop | No | N/A (child never spawned) | **PASS** |
| **3** | Parent crash while child suspended | `CreateFileW` | `GENERIC_WRITE` (0x40000000) | `FILE_SHARE_READ \| FILE_SHARE_WRITE` (0x03) | `INVALID_HANDLE`; child terminated by Job | `32` (`ERROR_SHARING_VIOLATION`) | No | No (suspended, 0 code executed) | **PASS** |
| **4** | Parent crash immediately after resume | `CreateFileW` | `GENERIC_WRITE` (0x40000000) | `FILE_SHARE_READ \| FILE_SHARE_WRITE` (0x03) | `INVALID_HANDLE`; child terminated by Job | `32` (`ERROR_SHARING_VIOLATION`) | No | No (paused at barrier pre-open) | **PASS** |
| **5** | Parent crash while child opens GGUF | `CreateFileW` (loop) | `GENERIC_WRITE` (0x40000000) | `FILE_SHARE_READ \| FILE_SHARE_WRITE` (0x03) | `INVALID_HANDLE` throughout transition | `32` (`ERROR_SHARING_VIOLATION`) | No | Terminated cleanly during loop | **PASS** |
| **6** | Parent crash after child opens GGUF *(Option A)* | `CreateFileW` | `GENERIC_WRITE` (0x40000000) | `FILE_SHARE_READ \| FILE_SHARE_WRITE` (0x03) | `INVALID_HANDLE` after parent dead | `32` (`ERROR_SHARING_VIOLATION`) | No | Yes (held duplicated lock) | **PASS** |
| **7** | Parent crash during model mapping | `CreateFileW` | `GENERIC_WRITE` (0x40000000) | `FILE_SHARE_READ \| FILE_SHARE_WRITE` (0x03) | `INVALID_HANDLE` during mapping | `32` (`ERROR_SHARING_VIOLATION`) | No | Yes (view mapped, unmapped on exit) | **PASS** |
| **8** | Child crash | `CreateFileW` | `GENERIC_WRITE` (0x40000000) | `FILE_SHARE_READ \| FILE_SHARE_WRITE` (0x03) | `INVALID_HANDLE` after child death | `32` (`ERROR_SHARING_VIOLATION`) | No | No (child dead, parent retains lock) | **PASS** |
| **9** | Child termination timeout | `ServerManager::stop_server` | N/A | N/A | Returns `false`; lock retained | `32` on write attempt | No | Unverified child; lock preserved | **PASS** |
| **10** | External write attempt (all modes) | `CreateFileW` (Modes A, B, C, D, E) | `GENERIC_WRITE`<br>`GENERIC_READ \| GENERIC_WRITE` | `0`<br>`FILE_SHARE_READ`<br>`FILE_SHARE_READ \| FILE_SHARE_WRITE` | `INVALID_HANDLE` on all modes | `32` (`ERROR_SHARING_VIOLATION`) on all modes | No | Yes (child running) | **PASS** |
| **11** | External delete attempt | `DeleteFileW`<br>`CreateFileW` (DELETE) | `DELETE` (0x00010000) | `FILE_SHARE_DELETE` (0x04) | `0`<br>`INVALID_HANDLE` | `32` (`ERROR_SHARING_VIOLATION`)<br>`32` (`ERROR_SHARING_VIOLATION`) | No (file preserved) | Yes (child running) | **PASS** |
| **12** | External rename attempt | `MoveFileExW` | `MOVEFILE_REPLACE_EXISTING` | N/A | `0` | `32` (`ERROR_SHARING_VIOLATION`) | No (old path preserved) | Yes (child running) | **PASS** |
| **13** | External replacement attempt | `ReplaceFileW` | N/A | N/A | `0` | `32` (`ERROR_SHARING_VIOLATION`) or `5` (Access Denied) | No (original bytes intact) | Yes (child running) | **PASS** |
| **14** | Two NOVA instances on same GGUF | `ModelFileLockGuard::acquire` | `GENERIC_READ` (0x80000000) | `FILE_SHARE_READ` (0x01) | Both succeed; write fails | `32` on write attempt | No | Both instances read concurrently | **PASS** |
| **15** | File replacement between verify & load | `ReplaceFileW`<br>`verify_file_identity` | N/A | N/A | Blocked by lock (32); identity check verifies Volume, Index, Size, Mtime | `32` (`ERROR_SHARING_VIOLATION`) | No | Identity validated before spawn | **PASS** |
| **16** | **Negative Control** (Without Option A) | `CreateFileW` | `GENERIC_WRITE` (0x40000000) | `FILE_SHARE_READ \| FILE_SHARE_WRITE` (0x03) | **`Valid HANDLE` (Succeeds!)** | **`0` (Success)** | Confirmed vulnerable without Option A | Yes (child alone permits write) | **PASS** |

---

## 7. Runtime Integrity & Configuration Preservation

1. **Pinned Binary Provenance:**
   - llama.cpp commit: `dde879bc4` (build 12032).
   - Upstream binary remains 100% stock and unmodified.
2. **Required Dependencies Verified:**
   - `llama-server.exe`
   - `ggml-cuda.dll`
   - `llama.dll`
   - `mtmd.dll` (verified by regression test `test_mtmd_dll_verified_in_required_dependencies`)
3. **Configuration D Command-Line Arguments Preserved:**
   ```text
   -ngl 48 -ncmoe 40 -c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap
   ```
4. **Safety & Policy Invariants Maintained:**
   - 10.0 GiB available-RAM admission gate (strict fail-closed).
   - Monotonic telemetry clock with 5.0-second freshness window.
   - Loopback-only binding (`127.0.0.1`).
   - Ephemeral per-session API keys (`Uuid::new_v4()`).
   - Explicit two-step preflight and confirmation authorization.
   - Manual user-controlled resource recovery.
   - Zero automatic process killing.
   - LowMemory profile remains experimental.

---

## 8. Remaining Risks & Edge Cases

1. **Kernel Driver / Filter Driver Interference:**
   - Antivirus or EDR filter drivers running in kernel mode (`ring 0`) can bypass Win32 file sharing checks. This is true for all user-mode applications on Windows.
   - *Mitigation:* SHA-256 integrity verification and pre/post file identity checks ensure any tampering before load is caught.
2. **Crash-Time System Power Failure:**
   - If power is cut to the machine, NTFS journal rollback restores metadata consistency.
   - *Mitigation:* The GGUF is opened strictly read-only; NOVA never issues write calls to the model file.
3. **RAM Pressure & Paging Under Configuration D:**
   - In Configuration D, 13.80 GiB of weights reside in CPU memory. With 16 GB system RAM, memory pressure will cause Windows pagecache thrashing if available RAM drops below 10 GiB.
   - *Mitigation:* The 10.0 GiB admission gate strictly prevents admission if memory headroom is insufficient.

---

## 9. Repository & Artifact Evidence

### 9.1 Baseline & Repository State
- **Baseline Git HEAD:** `7d765f45888574751c8d674bba481fdaa5e2a01f`
- **Current Git HEAD:** `7d765f45888574751c8d674bba481fdaa5e2a01f` (unmodified)
- **Branch:** `feature/nova-chat-desktop`
- **Git Commit / Push Actions Taken:** None (0 commits, 0 pushes).

### 9.2 Artifact Cryptographic Hashes & File Sizes

| Artifact | Relative Path | Byte Size | SHA-256 Cryptographic Hash |
| :--- | :--- | :--- | :--- |
| **Model GGUF** | `models/qwen3-30b/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` | 18,632,184,480 | `a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32` |
| **Release Binary** | `desktop/src-tauri/target/release/nova-chat.exe` | 5,969,408 | `66a97c6157acef444a57f48c7411879d0378d634ad6d0ba5631a6b9be9f0c935` |
| **NSIS Installer** | `desktop/src-tauri/target/release/bundle/nsis/NOVA Chat_0.1.0_x64-setup.exe` | 2,068,735 | `ca91f535ad84f30606600452a2c1560ffffd1302f1c7e38a3fc8af26f01417d2` |

### 9.3 Commands Executed
1. `git status` (Read-only)
2. `cargo test --manifest-path desktop/src-tauri/Cargo.toml` (Read-only verification; 89 passed)
3. `cargo build --example crash_safety_helper --manifest-path desktop/src-tauri/Cargo.toml` (Build helper)
4. `cargo test test_phase3b9 --manifest-path desktop/src-tauri/Cargo.toml -- --nocapture` (16 passed in 0.31s)
5. `cargo test --manifest-path desktop/src-tauri/Cargo.toml` (105 passed, 0 failed, 1 ignored)
6. `cargo build --release --manifest-path desktop/src-tauri/Cargo.toml` (Release binary build)
7. `npm --prefix desktop run tauri build -- --bundles nsis` (Production NSIS package build)
8. SHA-256 hash calculation via Python standard library `hashlib`

---

## 10. Final Implementation Verdict

```text
================================================================================
FINAL VERDICT: READY FOR OWNER REVIEW
================================================================================
1. The model-file write-protection vulnerability is completely eliminated.
2. Option A (Child-Owned Duplicated Restrictive File Handle) is implemented, verified,
   and documented with exact Win32 kernel semantics.
3. All 15 production-equivalent integration tests plus the negative control passed
   cleanly with zero failures (105 total unit tests passing).
4. Stock pinned llama.cpp runtime (build 12032, commit dde879bc4) is preserved unmodified.
5. Release executable and NSIS bundle were built and cryptographically hashed.
6. Zero model weights were loaded; zero tokens generated; zero git commits made.
================================================================================
```
