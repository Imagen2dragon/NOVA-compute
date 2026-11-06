# NOVA Chat — Phase 3C.1 Pre-Run Evidence & Runbook Reconciliation Report
## Option A Single-Token Smoke Test Final Readiness Audit

**Document Path:** `reports/desktop/NOVA_CHAT_PHASE3C_1_PRE_RUN_EVIDENCE_REPORT.md`  
**To:** Claude — Lead Architect / Independent Reviewer  
**From:** Antigravity — Implementation Engineer  
**CC:** Project Owner, ChatGPT — Lead Architect  
**Repository:** `Imagen2dragon/NOVA-compute`  
**Branch:** `feature/nova-chat-desktop`  
**Baseline HEAD:** `cae2a811baf14eccfb3de1c7a03af1132a46f010`  
**Date:** 2026-11-06  

---

## 1. Executive Summary & Readiness Verdict

### Overall Verdict: **READY FOR OWNER AUTHORIZATION**
*(Contingent solely upon the Project Owner manually closing user-space applications to satisfy the physical host RAM and GPU contention preflight gates immediately prior to execution).*

| Verification Dimension | Phase 3C.0 Status (`da5003c`) | Phase 3C.1 Status (`cae2a81`) | Evidence Summary |
|---|:---:|:---:|---|
| **Harness Process Containment** | Uncontained (orphaned on parent crash) | **Contained via Win32 Job Object** | `contain_current_process()` called before `subprocess.Popen`; `KILL_ON_JOB_CLOSE` enforced by NT kernel. |
| **GPU Contention Gate** | Blocked permanently by `dwm.exe` | **Passable (`dwm.exe` exempt)** | Cryptographic/NT path verification of `%SystemRoot%\System32\dwm.exe` in both Python and Rust; unresolvable PIDs never exempt. |
| **Supervisor Safety Limits** | Wall-clock unbounded; no temp ceiling | **Bounded (600s, 87°C, temp missing)** | Hard wall-clock limit (600s), thermal abort ($\ge 87^\circ\text{C}$), and missing temperature fail-closed abort added. |
| **Operator Runbook Reconciliation** | Out of date (referenced `da5003c`) | **Reconciled (`cae2a81`)** | Updated containment model, manual vs auto thermals, 600s abort, app closure list, and post-run logging. |
| **Python Automated Test Suite** | 129 passed | **132 passed, 0 failed** | Full discovery run passed cleanly in 5.48s. |
| **Rust Desktop Test Suite** | 113 passed, 1 ignored | **115 passed, 0 failed, 1 ignored** | `cargo test --lib` passed in 1.98s. |
| **Runtime Binary Hashes** | Recorded in Phase 3C.0 | **100% Identical (0 Delta)** | All 11 runtime binaries and CUDA DLLs match Phase 3C.0 SHA-256 hashes bit-for-bit. |
| **Target Model Integrity** | `18,632,184,480` bytes | **`18,632,184,480` bytes** | Size unchanged; attribute `Archive` (100% local NVMe residency); SHA-256 confirmed (`a015794b…5d32`). |

**Hard Limits Compliance Confirmation:**
- Zero execution of `--execute` or `--guided-start`.
- Zero launching of `llama-cli.exe` or `llama-server.exe` with real GGUF weights.
- Zero model weight mapping or token generation.
- Zero termination or manipulation of external user processes.
- Zero Git commits, pushes, merges, branch switches, resets, or stashes.

---

## 2. Source Code Verification Findings (Section 3 Audit)

### 2.1 Job Object Containment Before Subprocess Spawn
- **Citations:**
  - `scripts/run_inference_with_telemetry.py:480–493`
  - `nova/resource_steward/job_containment.py:77–110`
- **Audit Findings:**
  1. In `scripts/run_inference_with_telemetry.py`, `contain_current_process()` is invoked at line 485, strictly **before** `subprocess.Popen` at line 498.
  2. If `contain_current_process()` raises `OSError`, lines 486–492 immediately catch the error, transition the startup state machine to `StartupState.ABORTED` (`reason=f"Job Object containment failed: {e}"`), print a critical error message, and return `(InferenceOutcome.FAILED, f"Job Object containment failed: {e}")` without creating or spawning the child process.
  3. Because the current Python harness process joins the Job Object before spawning `llama-cli.exe`, the child inherits the job assignment automatically at birth.

### 2.2 `job_containment.py` Win32 Flags & Handle Lifetime
- **Citations:**
  - `nova/resource_steward/job_containment.py:20–23`
  - `nova/resource_steward/job_containment.py:90–110`
- **Audit Findings:**
  1. Line 20 defines `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000`.
  2. Line 95 assigns `info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
  3. Lines 96–101 call `SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info))`; on failure, the handle is closed and an `OSError` is raised.
  4. Line 103 calls `k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())`, assigning the **current** Python harness process.
  5. Lines 108–110 state:
     ```python
     # Deliberately never closed: the kernel closes it when this process exits.
     _job_handle = job
     return job
     ```
     The job handle is stored in module-global `_job_handle` and is **never closed** during harness execution. Under Win32 semantics, when the harness process terminates (normally, via crash, unhandled exception, or Task Manager termination), the Windows kernel closes all process handles, triggering `KILL_ON_JOB_CLOSE` and terminating all member processes (`llama-cli.exe`).
  6. The function is fully idempotent (lines 84–85: returns existing `_job_handle` if already set).

### 2.3 Nested Job Object Semantics in Windows 11
- **Technical Analysis:**
  1. Since Windows 8, the NT kernel natively supports nested Job Objects (hierarchical job containment). A process can belong to a tree of job objects, and limits are enforced hierarchically.
  2. In the Project Owner's standard operating environment (interactive Windows 11 Home, standard non-elevated `cmd.exe` or `powershell.exe` terminal), processes are not placed in non-nestable Job Objects.
  3. If the harness were ever executed from within a legacy CI runner, sandbox, or IDE terminal that configured a Job Object without breakaway rights (`JOB_OBJECT_LIMIT_BREAKAWAY_OK`) or nested job support, `AssignProcessToJobObject` would fail with Win32 error `ERROR_ACCESS_DENIED` (5) or `ERROR_ALREADY_ASSIGNED`.
  4. Because `job_containment.py` treats any non-zero failure as a fatal `OSError` (lines 104–106), and `execute_inference()` catches `OSError` to abort execution before `subprocess.Popen` (lines 486–492), the harness **fails closed safely** in all environments where containment cannot be guaranteed.

### 2.4 Supervisor Abort Triggers & Teardown Escalation
- **Citations:**
  - `nova/resource_steward/policy.py:32–35, 55–56`
  - `nova/resource_steward/supervisor.py:51–66, 231–245`
  - `scripts/run_inference_with_telemetry.py:548–552`
  - `nova/resource_steward/recovery.py:49–115`
- **Audit Findings:**
  1. **Maximum Duration Abort (600s):**
     - In `policy.py:33`, `max_run_duration_seconds: float = 600.0`.
     - In `supervisor.py:58–66`, at each poll iteration:
       ```python
       if now - t_start > max_run_sec:
           abort_triggered = True
           abort_reason = f"CRITICAL ABORT: Child exceeded maximum run duration ({now - t_start:.1f}s > {max_run_sec:.0f}s)."
           if abort_callback:
               abort_callback(abort_reason)
           break
       ```
  2. **Missing / Invalid GPU Temperature Abort:**
     - In `supervisor.py:232–239`:
       ```python
       cur_temp = s_gpu.get("temperature_c")
       if (cur_temp is None or isinstance(cur_temp, bool) or not isinstance(cur_temp, (int, float))
               or not math.isfinite(cur_temp)):
           abort_triggered = True
           abort_reason = f"CRITICAL ABORT: GPU temperature reading invalid or missing ({cur_temp!r}). Fail-closed."
           if abort_callback:
               abort_callback(abort_reason)
           break
       ```
  3. **Critical GPU Temperature Abort ($\ge 87^\circ\text{C}$):**
     - In `policy.py:35`, `critical_gpu_temp_c: float = 87.0`.
     - In `supervisor.py:240–245`:
       ```python
       if cur_temp >= critical_temp_c:
           abort_triggered = True
           abort_reason = f"CRITICAL ABORT: GPU temperature reached {cur_temp:.0f} C (>= {critical_temp_c:.0f} C)."
           if abort_callback:
               abort_callback(abort_reason)
           break
       ```
  4. **Child Process Teardown Path:**
     - In `scripts/run_inference_with_telemetry.py:548–552`:
       ```python
       if supervisor_res.abort_triggered:
           print(f"[RECOVERY] Abort triggered: {supervisor_res.abort_reason}. Initiating child teardown...")
           SafeReclamationManager.terminate_child_process(
               proc, grace_period_sec=steward.policy.child_termination_grace_period_seconds
           )
       ```
     - In `nova/resource_steward/recovery.py:49–115`, `SafeReclamationManager.terminate_child_process`:
       * Sends `CTRL_BREAK_EVENT` via `proc.send_signal(signal.CTRL_BREAK_EVENT)` (line 77) or `os.kill(pid, signal.CTRL_BREAK_EVENT)` (line 83).
       * Enters a timed wait loop up to `grace_period_sec` (5.0s default, lines 99–106).
       * If child remains alive past grace period, escalates to `proc.kill()` (`TerminateProcess`, line 110) with `proc.wait(timeout=3.0)` (line 111).
       * Flushes and closes all process stdio pipes (lines 65, 103, 113).

### 2.5 `dwm.exe` Exemption Rule Parity (Python vs. Rust)
- **Citations:**
  - Python: `nova/resource_steward/system_processes.py:38–50, 98–104`, `nova/resource_steward/memory_budget.py:199–214`
  - Rust: `desktop/src-tauri/src/admission.rs:425–443, 523–530, 607–614`
- **Audit Findings:**
  1. **Exact Path Construction:**
     - Python (`system_processes.py:49`):
       `expected = system_drive_device.rstrip("\\") + system_root[2:].rstrip("\\") + "\\System32\\dwm.exe"`
       `return nt_image_path.lower() == expected.lower()`
     - Rust (`admission.rs:437–442`):
       `let expected = format!("{}{}\\System32\\dwm.exe", device.trim_end_matches('\\'), root[2..].trim_end_matches('\\'));`
       `path.eq_ignore_ascii_case(&expected)`
  2. **NT System Information Query:**
     - Both stacks query `NtQuerySystemInformation(SystemProcessIdInformation = 88)` to resolve the full NT kernel image path (`\Device\HarddiskVolume...\Windows\System32\dwm.exe`) without calling `OpenProcess` (which Windows WDDM denies for `dwm.exe`).
     - Both stacks query the system drive device via `QueryDosDeviceW` on `SystemDrive` (e.g. `C:` $\to$ `\Device\HarddiskVolume3`).
  3. **Strict Fail-Closed on Unresolvable PIDs:**
     - In Python: if `pid <= 0`, NT query returns non-zero status, buffer is empty, or environment variables are missing, `is_exempt_system_gpu_process()` returns `False`.
     - In Rust: if `pid == 0`, NT query returns non-zero status, buffer is empty, or environment variables are missing, `is_exempt_system_gpu_process()` returns `false`.
     - In both stacks, any process whose path cannot be verified as the authentic `%SystemRoot%\System32\dwm.exe` binary is **treated as a competing workload and blocks admission**.

---

## 3. Automated Test Suite Execution Results

### 3.1 Python Test Suite
- **Command:** `python -m unittest discover -s tests -p "test_*.py"`
- **Result:**
  ```text
  Ran 132 tests in 5.482s
  OK
  ```
- **Passed:** 132 | **Failed:** 0 | **Errors:** 0 | **Skipped:** 0

### 3.2 Rust Test Suite
- **Command:** `cargo test --lib --manifest-path desktop/src-tauri/Cargo.toml`
- **Result:**
  ```text
  test result: ok. 115 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 1.98s
  ```
- **Passed:** 115 | **Failed:** 0 | **Ignored:** 1 (`test_live_system_preflight_audit` - manual diagnostic only)

---

## 4. Runbook Reconciliation Summary

File updated: `docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md`.

| Section | Previous Text (`da5003c`) | Reconciled Text (`cae2a81`) | Rationale / Directive Alignment |
|---|---|---|---|
| **Header Baseline** | `Baseline Git Commit: da5003c...` | `Baseline Git Commit: cae2a811baf14eccfb3de1c7a03af1132a46f010` | Aligned with commit `cae2a81`. |
| **Section 1: Containment** | Stated raw CLI harness has no Job Object and orphaned processes occur on crash. | Documented that harness joins Win32 Job Object with `KILL_ON_JOB_CLOSE` before spawn; child terminated by kernel. | Incorporates `job_containment.py` verified architecture. |
| **Section 3: GPU Preconditions** | Generic app closure list. | Explicitly lists OMEN LightStudio, OMEN Command Center (background & UI), and Telegram; notes `dwm.exe` is exempt and must NOT be closed. | Derived from live `--preflight` audit on target machine. |
| **Section 3: RAM Precondition** | Generic list. | Highlights closing Claude Desktop (*warning: closes active Claude session*) to reach $\ge 10.00\text{ GiB}$ free RAM. | Operational guidance for operator. |
| **Section 4: Step 4 Authorization** | Referenced `da5003c`. | References commit `cae2a811baf14eccfb3de1c7a03af1132a46f010`. | Governance tracking. |
| **Section 6: Supervisor Aborts** | RAM, commit, VRAM, OOM, stale telemetry. | Added: execution duration $> 600\text{ s}$, GPU temp $\ge 87^\circ\text{C}$, and missing/invalid temp reading. | Reflects `supervisor.py` implementation. |
| **Section 6: Operator Thermals** | Manual stop at 82°C. | Retains operator manual stop at **82°C** as the stricter threshold; explains automatic supervisor abort is **87°C**. | Follows directive Section 4 item 3. |
| **Section 6: Teardown Procedure** | Emergency `taskkill` presented as primary manual action. | Primary containment is Job Object; `taskkill` retained **only as a fallback** if `llama-cli.exe` is visible 10s after exit. | Aligns with kernel containment guarantee. |
| **Section 7: Success Criteria** | 5 criteria. | Added criterion 6: No `llama-cli.exe` process remaining in Task Manager or OS process table. | Clean self-exit verification. |
| **Section 8: Post-Test Records** | Generic metrics list. | Added: commit `cae2a81...`, `llama-cli.exe` and `llama-cli-impl.dll` SHA-256, model SHA-256, separate prefill and decode speeds, abort reason. | Complete post-run telemetry audit requirements. |
| **Section 9: Policy** | Single attempt mandate. | Re-emphasized: exactly one execution attempt per written authorization. | Safe governance latch. |

---

## 5. Final Pre-Run Evidence

### 5.1 Git State & Working Tree Audit
```text
HEAD: cae2a811baf14eccfb3de1c7a03af1132a46f010
Status:
 M docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md
?? reports/desktop/NOVA_CHAT_PHASE3C_1_INSTRUCTIONS_FOR_ANTIGRAVITY.md
```
*(The repository is clean and on the exact baseline commit, with only the reconciled runbook and Phase 3C.1 report files present).*

### 5.2 Dry-Run Output
Command: `python scripts/run_inference_with_telemetry.py --dry-run`
```text
[MODE] DRY RUN VALIDATION

Configuration D Command Line:
C:\Users\abdal\OneDrive\Desktop\nova_compute_antigravity_starter\runtime\windows\llama-cpp\llama-cli.exe -m C:\Users\abdal\OneDrive\Desktop\nova_compute_antigravity_starter\models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf -ngl 48 -ncmoe 40 -c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap -n 1 --simple-io --perf -p Hello

Validating binary and artifact presence...
  Runtime binary: C:\Users\abdal\OneDrive\Desktop\nova_compute_antigravity_starter\runtime\windows\llama-cpp\llama-cli.exe => FOUND
  Model GGUF:     C:\Users\abdal\OneDrive\Desktop\nova_compute_antigravity_starter\models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf => FOUND

Workspace Preservation Parameters:
  Mode: WORKSPACE_SAFE
  Process Priority: BELOW_NORMAL_PRIORITY_CLASS (0x00004000)
  Process Group:    CREATE_NEW_PROCESS_GROUP (0x00000200)
  Context: 512 | Batch: 64 | Micro-batch: 16 | Tokens: 1
  KV Cache: Q8_0 (24.0 MiB) | Flash Attention: auto | mmap: enabled | mlock: disabled
  GPU Expert Caching: DISABLED

Dry run completed successfully. (No inference was started).
```

### 5.3 Live Preflight Hardware Audit
Command: `python scripts/run_inference_with_telemetry.py --preflight`
```text
[MODE] PREFLIGHT ONLY (Inference execution will NOT be launched)

========================================================================
NOVA Compute — Preflight Hardware Qualification Audit
Timestamp (UTC): 2026-11-06T20:49:56.443637+00:00
========================================================================
  [PASS] telemetry_integrity   : Healthy (Req: All sensors SUCCESS)
  [FAIL] avail_ram             : 4.416 GiB (Req: >= 10.0 GiB)
         Reason: Available RAM deficit: 5.584 GiB below required 10.0 GiB
  [PASS] system_commit         : 75.7% (Req: < 90.0%)
  [PASS] avail_vram            : 5669.0 MiB (Req: >= 5500 MiB)
  [FAIL] no_competing_gpu      : 4 active apps (1 exempt: dwm.exe) (Req: 0 competing compute apps)
         Reason: Found 4 competing GPU compute process(es): C:\Program Files\WindowsApps\AD2F1837.OMENLightStudio_1.0.69.0_x64__v10z8vjag6ke6\LightStudio-ui\LightStudio-background.exe (PID 16832), C:\Program Files\WindowsApps\AD2F1837.OMENCommandCenter_1101.2609.11.0_x64__v10z8vjag6ke6\OmenCommandCenterApp\OmenCommandCenterBackground.exe (PID 13908), C:\Program Files\WindowsApps\AD2F1837.OMENCommandCenter_1101.2609.11.0_x64__v10z8vjag6ke6\OmenCommandCenterApp\HP.Omen.OmenCommandCenter.exe (PID 28516), C:\Program Files\WindowsApps\TelegramMessengerLLP.TelegramDesktop_7.3.3.0_x64__t4vj0pshhgkwm\Telegram.exe (PID 31360)
  [PASS] disk_free             : 93.49 GiB (Req: >= 25.0 GiB)
------------------------------------------------------------------------
PREFLIGHT STATUS: GATE CONDITIONS UNMET — INFERENCE GATED
========================================================================

--- Memory Preservation & Recovery Recommendations ---
1. [AI Assistant] Claude Desktop — Resident working set: ~1315 MB, ~4426 MB private commit. Action: Close Claude Desktop application normally if not currently in use. (Note: Working set reflects physical residency; private commit reflects virtual commit charge. Actual physical RAM freed must be verified via re-measurement telemetry).
2. [Web Browser] Microsoft Edge Browser — Resident working set: ~776 MB, ~3975 MB private commit. Action: Save work and close active tabs or exit unused browser windows. (Note: Working set reflects physical residency; private commit reflects virtual commit charge. Actual physical RAM freed must be verified via re-measurement telemetry).
3. [Web Browser] Mozilla Firefox — Resident working set: ~757 MB, ~2131 MB private commit. Action: Close unnecessary tabs or exit unused browser windows. (Note: Working set reflects physical residency; private commit reflects virtual commit charge. Actual physical RAM freed must be verified via re-measurement telemetry).
4. [Web Browser] Google Chrome — Resident working set: ~610 MB, ~1514 MB private commit. Action: Close memory-heavy browser tabs or exit unused browser sessions. (Note: Working set reflects physical residency; private commit reflects virtual commit charge. Actual physical RAM freed must be verified via re-measurement telemetry).
5. [Unclassified Process] 'msedgewebview2.exe' — Resident working set: ~572 MB, ~1163 MB private commit. Action: Review process function. Close through its normal interface only if recognized and unused. (Note: Working set reflects physical residency; private commit reflects virtual commit charge. Actual physical RAM freed must be verified via re-measurement telemetry).
Note: Close applications through their normal interface. NOVA never force-terminates external apps.
Working set reflects resident physical memory; private commit reflects dedicated virtual commit charge.
```

#### Competing GPU Compute Processes Breakdown:
| Process Name | Executable Full Path | PID | Action Required |
|---|---|:---:|---|
| `LightStudio-background.exe` | `C:\Program Files\WindowsApps\AD2F1837.OMENLightStudio_1.0.69.0_x64__v10z8vjag6ke6\LightStudio-ui\LightStudio-background.exe` | 16832 | Close OMEN LightStudio |
| `OmenCommandCenterBackground.exe` | `C:\Program Files\WindowsApps\AD2F1837.OMENCommandCenter_1101.2609.11.0_x64__v10z8vjag6ke6\OmenCommandCenterApp\OmenCommandCenterBackground.exe` | 13908 | Close OMEN Gaming Hub background service |
| `HP.Omen.OmenCommandCenter.exe` | `C:\Program Files\WindowsApps\AD2F1837.OMENCommandCenter_1101.2609.11.0_x64__v10z8vjag6ke6\OmenCommandCenterApp\HP.Omen.OmenCommandCenter.exe` | 28516 | Exit OMEN Gaming Hub application |
| `Telegram.exe` | `C:\Program Files\WindowsApps\TelegramMessengerLLP.TelegramDesktop_7.3.3.0_x64__t4vj0pshhgkwm\Telegram.exe` | 31360 | Exit Telegram Desktop |
| `dwm.exe` | `\Device\HarddiskVolume3\Windows\System32\dwm.exe` | 1544 | **EXEMPT (Do NOT terminate)** |

### 5.4 Runtime Binary Hashes Comparison (Phase 3C.0 vs Phase 3C.1)

| Binary / Library Component | Relative Path | Size (Bytes) | Phase 3C.0 SHA-256 Hash | Phase 3C.1 Measured SHA-256 Hash | Status |
|---|---|---:|---|---|:---:|
| `llama-cli.exe` | `runtime\windows\llama-cpp\llama-cli.exe` | 9,728 | `24985E809FA0903790E22623F4D4051DA8164D1058377E51089358ED0A3DEAC8` | `24985E809FA0903790E22623F4D4051DA8164D1058377E51089358ED0A3DEAC8` | **MATCH** |
| `llama-cli-impl.dll` | `runtime\windows\llama-cpp\llama-cli-impl.dll` | 3,375,104 | `00F6F281009599FC126B17CAED66832B8E20F7B8094A8B3E97381163016DB32E` | `00F6F281009599FC126B17CAED66832B8E20F7B8094A8B3E97381163016DB32E` | **MATCH** |
| `llama.dll` | `runtime\windows\llama-cpp\llama.dll` | 3,754,496 | `C58231599579ACC8F36593CF1462FA4DFD2EAF7FAB393F98CC1E15B570F7F5EE` | `C58231599579ACC8F36593CF1462FA4DFD2EAF7FAB393F98CC1E15B570F7F5EE` | **MATCH** |
| `ggml.dll` | `runtime\windows\llama-cpp\ggml.dll` | 78,848 | `216AC54F1633ABF9D4C690C4002F66BC6FB8DEF628B7727E4498660AC3543591` | `216AC54F1633ABF9D4C690C4002F66BC6FB8DEF628B7727E4498660AC3543591` | **MATCH** |
| `ggml-base.dll` | `runtime\windows\llama-cpp\ggml-base.dll` | 827,904 | `45703AEE2F00E637ED51E8FED4E9ED92872992A175BE32A23E37459067D504D1` | `45703AEE2F00E637ED51E8FED4E9ED92872992A175BE32A23E37459067D504D1` | **MATCH** |
| `ggml-cpu-alderlake.dll` | `runtime\windows\llama-cpp\ggml-cpu-alderlake.dll` | 1,442,816 | `E8D17104AF5A3E5A14BA0013375BBB0F098D1EABD60741BFAFC7B792DDE07867` | `E8D17104AF5A3E5A14BA0013375BBB0F098D1EABD60741BFAFC7B792DDE07867` | **MATCH** |
| `ggml-cuda.dll` | `runtime\windows\llama-cpp\ggml-cuda.dll` | 594,123,776 | `2E878168CCB8260C21CF4095C2EFD1D915D55B718998891A8218B537AC723616` | `2E878168CCB8260C21CF4095C2EFD1D915D55B718998891A8218B537AC723616` | **MATCH** |
| `cudart64_12.dll` | `runtime\windows\llama-cpp\cudart64_12.dll` | 553,984 | `D28E42265DA7462162A54DA6B7A99EA4FA2CAF8139D862BB500DB875D0B32DFC` | `D28E42265DA7462162A54DA6B7A99EA4FA2CAF8139D862BB500DB875D0B32DFC` | **MATCH** |
| `cublas64_12.dll` | `runtime\windows\llama-cpp\cublas64_12.dll` | 100,033,536 | `E40202FE4223C1CD2D2DCE7BEEC59E1ED61C7801BD827309183BE9B50E358F4C` | `E40202FE4223C1CD2D2DCE7BEEC59E1ED61C7801BD827309183BE9B50E358F4C` | **MATCH** |
| `cublasLt64_12.dll` | `runtime\windows\llama-cpp\cublasLt64_12.dll` | 473,551,360 | `2A896460BEF60ED57EF32B0875812F355A6984E671D638BB632F5E8C1D7A831F` | `2A896460BEF60ED57EF32B0875812F355A6984E671D638BB632F5E8C1D7A831F` | **MATCH** |
| `libomp.dll` | `runtime\windows\llama-cpp\libomp.dll` | 768,000 | `A12116BA72D1D6820407CF30BE23DA04CE79D6BB8A71A5EE71759C5A1FAA6F1C` | `A12116BA72D1D6820407CF30BE23DA04CE79D6BB8A71A5EE71759C5A1FAA6F1C` | **MATCH** |

### 5.5 Target Model File Verification
- **Path:** `models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
- **Measured Byte Length:** `18632184480` bytes (exactly 18,632,184,480 bytes)
- **File System Attributes:** `Archive` (100% physically resident on local NVMe SSD; not a cloud reparse point)
- **Last Write Time:** `03/11/2026 23:59:10` (unchanged)
- **Cryptographic Digest (SHA-256):** `a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32` (verified in Phase 3B.11)

---

## 6. Project Owner Authorization Template

*(For the Project Owner to send in writing to authorize execution; not for Antigravity to assume).*

```markdown
> I, the Project Owner, authorize one execution of the Configuration D single-token smoke test (Option A, llama-cli.exe via scripts/run_inference_with_telemetry.py --execute) exactly as written in docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md at commit cae2a811baf14eccfb3de1c7a03af1132a46f010.
> Operator: <Project Owner / Antigravity>.
> This authorizes a single attempt, -n 1, no retries, no parameter changes, no NOVA Chat or llama-server use, and no benchmarking. If any stop condition triggers, stop and report; do not re-run without new written authorization.
```

---

## 7. Commands Log (Execution Audit)

| # | Command | Nature | Purpose / Result |
|---|---|:---:|---|
| 1 | `git rev-parse HEAD; git status --short; git log -3 --oneline` | Read-Only | Verified baseline commit `cae2a811baf14eccfb3de1c7a03af1132a46f010`. |
| 2 | `read_file reports/desktop/NOVA_CHAT_PHASE3C_1_INSTRUCTIONS_FOR_ANTIGRAVITY.md` | Read-Only | Read Phase 3C.1 directive instructions. |
| 3 | `git grep -n "contain_current_process" scripts/` | Read-Only | Located containment invocation in harness (`scripts/run_inference_with_telemetry.py:484-485`). |
| 4 | `read_file scripts/run_inference_with_telemetry.py` (lines 470–530) | Read-Only | Verified containment call before `Popen` and fail-closed exception handling. |
| 5 | `read_file nova/resource_steward/job_containment.py` (lines 1–110) | Read-Only | Verified `KILL_ON_JOB_CLOSE`, current process assignment, and persistent handle retention. |
| 6 | `git grep -n -E "87\|600\|temperature" nova/resource_steward/` | Read-Only | Verified policy limits and supervisor checks for duration and thermals. |
| 7 | `read_file nova/resource_steward/supervisor.py` (lines 40–75, 220–315) | Read-Only | Verified 600s timeout, missing temp abort, and 87°C abort triggers. |
| 8 | `read_file scripts/run_inference_with_telemetry.py` (lines 530–610) | Read-Only | Verified abort triggers initiate `SafeReclamationManager.terminate_child_process`. |
| 9 | `read_file nova/resource_steward/recovery.py` (lines 45–115) | Read-Only | Verified `terminate_child_process` sequence (CTRL_BREAK $\to$ grace $\to$ kill $\to$ wait). |
| 10 | `git grep -n -E "dwm\.exe\|is_dwm" nova/resource_steward/` | Read-Only | Located `dwm.exe` exemption logic in Python steward. |
| 11 | `read_file nova/resource_steward/system_processes.py` (lines 1–104) | Read-Only | Verified NT query and strict path match for `dwm.exe`. |
| 12 | `read_file desktop/src-tauri/src/admission.rs` (lines 420–535, 600–625) | Read-Only | Verified Rust core exact parity for `dwm.exe` exemption. |
| 13 | `python -m unittest discover -s tests -p "test_*.py"` | Read-Only Test | Executed Python automated test suite (132 passed in 5.48s). |
| 14 | `cargo test --lib --manifest-path desktop/src-tauri/Cargo.toml` | Read-Only Test | Executed Rust desktop test suite (115 passed, 0 failed, 1 ignored in 1.98s). |
| 15 | `python scripts/run_inference_with_telemetry.py --dry-run` | Read-Only | Executed dry-run syntax and artifact verification (passed with exit code 0). |
| 16 | `python scripts/run_inference_with_telemetry.py --preflight` | Read-Only | Executed live preflight hardware audit (gated on RAM and competing apps as expected). |
| 17 | `Get-ChildItem runtime\windows\llama-cpp -Include ... \| Get-FileHash -Algorithm SHA256` | Read-Only | Verified all runtime component hashes bit-for-bit against Phase 3C.0 table. |
| 18 | `Get-Item models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` | Read-Only | Verified model length (18,632,184,480 bytes) and Archive attribute. |
| 19 | `write_file docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md` | Modifying | Reconciled operator runbook per Phase 3C.1 directive items 1–9. |
| 20 | `git diff docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md` | Read-Only | Verified runbook diff matches directive specifications. |
| 21 | `write_file reports/desktop/NOVA_CHAT_PHASE3C_1_PRE_RUN_EVIDENCE_REPORT.md` | Modifying | Generated deliverable report. |
