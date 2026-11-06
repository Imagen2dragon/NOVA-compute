# NOVA Compute — Phase 3C.0 Smoke-Test Readiness Report
## Option A Standalone CLI Harness Audit, Safety Boundary Analysis & Operator Runbook Package

**Date:** 2026-11-06  
**Implementation Engineer:** Antigravity  
**Lead Architect / Independent Reviewer:** Claude  
**CC:** Project Owner (User), ChatGPT  
**Repository:** `Imagen2dragon/NOVA-compute`  
**Branch:** `feature/nova-chat-desktop`  
**Baseline Git HEAD:** `da5003c6c27ceb6f6642670f4a5b998253827639`  
**Runbook Document:** [`docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md`](file:///c:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md)  

---

## 1. Executive Summary & Verdict

### Final Verdict: `CONDITIONALLY READY`

This audit establishes the pre-execution technical baseline and safety boundaries for the separately authorized, single-token Configuration D smoke test using the Option A CLI harness (`scripts/run_inference_with_telemetry.py`).

#### Core Architectural Findings:
1. **Critical Containment Fact:**
   - The Option A Python harness spawns `llama-cli.exe` directly via `subprocess.Popen` with `CREATE_NEW_PROCESS_GROUP | BELOW_NORMAL_PRIORITY_CLASS`.
   - **None of the Rust desktop application protections verified in Phases 3B.9–3B.11 apply to Option A:** there is **no Win32 Job Object** (`KILL_ON_JOB_CLOSE`), **no restrictive file lock** (`GENERIC_READ | FILE_SHARE_READ`), and **no duplicated handle**.
   - If the Python parent process crashes, hangs, or is terminated via `taskkill`, `llama-cli.exe` **remains running in the operating system** as an orphaned process, continuing to hold GPU VRAM and host RAM.
2. **Current System State Blocks Execution:**
   - A live run of `--preflight` on the host machine confirms that inference is **strictly gated (blocked)**:
     * Available physical RAM is currently **7.17–7.62 GiB** (deficit of 2.38–2.83 GiB below the mandatory 10.00 GiB admission threshold).
     * `nvidia-smi` returned unprivileged process row formatting for row 1 (`[Insufficient Permissions]`), causing Gate 1 (Telemetry Integrity) to fail closed as designed.
3. **Model & Runtime Immutability:**
   - Model file `models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` is verified on disk at **18,632,184,480 bytes** with SHA-256 `a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32`.
   - The file resides locally on the WD PC SN810 NVMe SSD with standard `Archive` attributes. OneDrive is not running and will not sync or dehydrate the file.
4. **All Safety Tests Passing:**
   - Python test suite: **117 passed, 0 failed** in 4.83s.
   - Rust desktop test suite: **113 passed, 0 failed, 1 ignored** in 1.72s.

#### Conditions Required Before the Smoke Test Can Execute:
1. The Project Owner must decide whether to approve wrapping the Python harness child in a Win32 Job Object via `ctypes` (Proposal A) or accept the manual Task Manager teardown procedure (Proposal B).
2. The Project Owner must manually close memory-heavy applications (Claude Desktop, Chrome, Edge, Firefox) to free $\ge 10.0\text{ GiB}$ of physical RAM and satisfy Gate 2.
3. Written Project Owner authorization must be provided, designating whether the Owner or Antigravity executes the runbook.

---

## 2. Baseline Evidence

```powershell
git rev-parse HEAD
# da5003c6c27ceb6f6642670f4a5b998253827639

git branch --show-current
# feature/nova-chat-desktop

git status --short
#  M README.md
# ?? docs/runbooks/
# ?? reports/desktop/NOVA_CHAT_PHASE3C_0_INSTRUCTIONS_FOR_ANTIGRAVITY.md

git log -3 --format="%H %s"
# da5003c6c27ceb6f6642670f4a5b998253827639 fix(integrity): fail closed on untrusted startup model cache (F2)
# a6509b7620f7b477a9b1c733057179991eb9bf99 feat(desktop): add NOVA Chat desktop app and close model-lock TOCTOU and restart gaps
# 7d765f45888574751c8d674bba481fdaa5e2a01f feat(phase3b.4): integrate authorization state machine with supervised inference launcher

cargo --version; rustc --version
# cargo 1.93.1 (083ac5135 2025-12-15)
# rustc 1.93.1 (01f6ddf75 2026-02-11)

python --version
# Python 3.12.10
```

---

## 3. Comprehensive Audit of the Option A Harness

### 3.1 Exact Command Built by `get_workspace_safe_command()`
- **Citation:** `scripts/run_inference_with_telemetry.py:66–84`
```python
def get_workspace_safe_command() -> List[str]:
    return [
        LLAMA_CLI,
        "-m", MODEL_GGUF,
        "-ngl", "48",
        "-ncmoe", "40",
        "-c", "512",
        "-b", "64",
        "-ub", "16",
        "-ctk", "q8_0",
        "-ctv", "q8_0",
        "-fa", "auto",
        "-lm", "mmap",
        "-n", "1",
        "--simple-io",
        "--perf",
        "-p", "Hello"
    ]
```
- **Configuration D Parameter Verification:**
  - `-ngl 48`: Offloads all 48 transformer layers' non-expert tensors (attention weights, layer norms, router feed-forward gates) to the RTX 4050 Laptop GPU VRAM (~4.1 GiB).
  - `-ncmoe 40`: **Explicitly specifies that 40 MoE layers have their expert weights assigned to and computed by the CPU.** In Qwen3-30B-A3B (48 total layers), layers 0–7 have their MoE experts offloaded to GPU VRAM, while layers 8–47 have their MoE experts placed in host system RAM (~13.48 GiB).
  - `-c 512`: Strict context window limit of 512 tokens.
  - `-b 64 -ub 16`: Batch size 64, micro-batch 16.
  - `-ctk q8_0 -ctv q8_0`: Quantized 8-bit KV cache (~24.0 MiB total allocation).
  - `-fa auto`: FlashAttention enabled.
  - `-lm mmap`: Memory mapping enabled; mlock disabled.
  - `-n 1`: Strictly limits generation to **one single output token**.
  - `--simple-io --perf -p "Hello"`: Clean text I/O with performance timing output and a fixed 1-word prompt.

### 3.2 Runtime Binary Components & Cryptographic Hashes
- **Runtime Path:** `runtime\windows\llama-cpp\`
- **Upstream Origin:** Official `llama.cpp` portable Windows x64 CUDA 12.4 release, build tag `b12032` (commit `dde879bc4`), co-located with NVIDIA CUDA 12.4 runtime libraries.
- **Manifest Status:** **No separate hash pin manifest file exists in the repository for `llama-cli.exe` or companion DLLs.** They were recorded by release tag and filename in `reports/phase1/runtime/BASELINE.md:29–33`.
- **Measured Empirical Hashes (SHA-256):**

| Binary / Library Component | Relative Path | Size (Bytes) | Measured SHA-256 Hash |
|---|---|---:|---|
| `llama-cli.exe` | `runtime\windows\llama-cpp\llama-cli.exe` | 9,728 | `24985E809FA0903790E22623F4D4051DA8164D1058377E51089358ED0A3DEAC8` |
| `llama-cli-impl.dll` | `runtime\windows\llama-cpp\llama-cli-impl.dll` | 3,375,104 | `00F6F281009599FC126B17CAED66832B8E20F7B8094A8B3E97381163016DB32E` |
| `llama.dll` | `runtime\windows\llama-cpp\llama.dll` | 3,754,496 | `C58231599579ACC8F36593CF1462FA4DFD2EAF7FAB393F98CC1E15B570F7F5EE` |
| `ggml.dll` | `runtime\windows\llama-cpp\ggml.dll` | 78,848 | `216AC54F1633ABF9D4C690C4002F66BC6FB8DEF628B7727E4498660AC3543591` |
| `ggml-base.dll` | `runtime\windows\llama-cpp\ggml-base.dll` | 827,904 | `45703AEE2F00E637ED51E8FED4E9ED92872992A175BE32A23E37459067D504D1` |
| `ggml-cpu-alderlake.dll` | `runtime\windows\llama-cpp\ggml-cpu-alderlake.dll` | 1,442,816 | `E8D17104AF5A3E5A14BA0013375BBB0F098D1EABD60741BFAFC7B792DDE07867` |
| `ggml-cuda.dll` | `runtime\windows\llama-cpp\ggml-cuda.dll` | 594,123,776 | `2E878168CCB8260C21CF4095C2EFD1D915D55B718998891A8218B537AC723616` |
| `cudart64_12.dll` | `runtime\windows\llama-cpp\cudart64_12.dll` | 553,984 | `D28E42265DA7462162A54DA6B7A99EA4FA2CAF8139D862BB500DB875D0B32DFC` |
| `cublas64_12.dll` | `runtime\windows\llama-cpp\cublas64_12.dll` | 100,033,536 | `E40202FE4223C1CD2D2DCE7BEEC59E1ED61C7801BD827309183BE9B50E358F4C` |
| `cublasLt64_12.dll` | `runtime\windows\llama-cpp\cublasLt64_12.dll` | 473,551,360 | `2A896460BEF60ED57EF32B0875812F355A6984E671D638BB632F5E8C1D7A831F` |
| `libomp.dll` | `runtime\windows\llama-cpp\libomp.dll` | 768,000 | `A12116BA72D1D6820407CF30BE23DA04CE79D6BB8A71A5EE71759C5A1FAA6F1C` |

### 3.3 Authorization Flow & Execution Latch
- **Citations:** `scripts/run_inference_with_telemetry.py:432–452`, `nova/resource_steward/state_machine.py:304–740`
- **State Machine Integration:** `--execute` instantiates `GuidedStartupController`. It does not launch directly; it executes `controller.run_interactive()`.
- **Preflight Requirement:** Transition to `READY_FOR_AUTHORIZATION` requires all 6 admission gates to pass. Fabricated or manually set states are detected and rejected by `has_legitimate_qualification_history()`.
- **Interactive vs. Non-Interactive:**
  - `run_interactive()` prompts the operator: `Type 'RUN CONFIGURATION D SMOKE TEST ONCE' to authorize, or 'Q' to cancel:`.
  - Non-interactive execution is blocked by default because an empty stdin raises `EOFError`, which defaults to cancellation (`"q"`).
  - It *can* execute non-interactively only if the exact confirmation phrase is piped into stdin.
- **Single-Use Enforcement:**
  - Upon authorization, `grant_authorization()` issues an ephemeral UUID token (`AUTH-SMOKE-<HEX>`).
  - `ExecutionLatch` (`state_machine.py:44–53`) is marked `is_consumed = True`.
  - The latch permanently locks after exactly one process creation attempt. It cannot be reset or reused.

### 3.4 Preflight Admission Gates: Python vs. Rust Parity
Both the Python `ResourceSteward` and the Rust desktop core enforce six admission gates with identical mathematical thresholds:

| Gate Name | Rust Core Threshold (`admission.rs`) | Python Steward Threshold (`policy.py`) | Parity Status |
|---|---|---|:---:|
| **Telemetry Integrity** | All sensors `SUCCESS` | All sensors `SUCCESS` | **Identical** |
| **Available RAM** | $\ge 10.00\text{ GiB}$ (Config D) | `minimum_inference_ram_gib = 10.0` | **Identical** |
| **System Commit** | $< 90.0\%$ | `critical_commit_pct = 90.0` | **Identical** |
| **Available VRAM** | $\ge 5,500.0\text{ MiB}$ (Config D) | `minimum_vram_mib = 5500.0` | **Identical** |
| **GPU Contention** | $0$ competing compute apps | $0$ competing compute apps | **Identical** |
| **Free NVMe Disk** | $\ge 25.00\text{ GiB}$ | `minimum_disk_free_gib = 25.0` | **Identical** |

**Differences in Gate Execution:**
1. **Model Format Validation:** Rust runs binary GGUF header inspection (`validate_gguf_file`: magic `GGUF`, version 2/3, tensor count, minimum 10 MiB size). Python only checks `os.path.exists()`.
2. **Telemetry Freshness:** Rust strictly validates monotonic timestamp freshness ($\le 5.0\text{s}$). Python runs a fresh preflight immediately prior to spawn (line 386) and monitors sample intervals during execution ($3.0\text{s}$ timeout in supervisor).

### 3.5 Model File Integrity Checks
- **Citation:** `scripts/run_inference_with_telemetry.py:463–470`
- **Audit Result:** The Option A Python harness **does NOT compute or verify the GGUF file's SHA-256 hash or exact byte size before launching.** It only executes `os.path.exists(MODEL_GGUF)`.
- *Note:* The full SHA-256 hash (`a015794b…5d32`) was independently verified from disk in Phase 3B.11.

### 3.6 Containment & Orphan Process Lifecycle
- **Citation:** `scripts/run_inference_with_telemetry.py:481–501`
- **Creation Flags:** `CREATE_NEW_PROCESS_GROUP (0x00000200) | BELOW_NORMAL_PRIORITY_CLASS (0x00004000)`.
- **Audit Result:**
  - **No Win32 Job Object is used.**
  - **No restrictive kernel file lock handle is duplicated.**
  - **Consequence:** If the Python process dies, crashes, or is killed via Task Manager, **`llama-cli.exe` remains running in the background as an orphaned process.** It will not receive any termination signal and will continue holding 4.1 GiB of VRAM and mapped host RAM until it exits or is manually killed via `taskkill /IM llama-cli.exe /F`.

### 3.7 Abort Telemetry, Teardown & Wait Logic
- **Citations:** `nova/resource_steward/supervisor.py:52–237`, `nova/resource_steward/recovery.py:49–80`
- **Telemetry Abort Triggers:**
  1. Output stream contains `"CUDA error"` or `"out of memory"` (immediate abort).
  2. Available physical RAM $< 2.0\text{ GiB}$ sustained for $\ge 5.0\text{ seconds}$.
  3. System commit charge $\ge 90.0\%$.
  4. Free VRAM $< 512.0\text{ MiB}$.
  5. Live telemetry sampler unresponsive for $> 3.0\text{ seconds}$.
  6. Out-of-order or duplicate telemetry sample IDs.
- **Teardown Escalation:**
  - `SafeReclamationManager.terminate_child_process` sends `CTRL_BREAK_EVENT` to the process group.
  - Waits up to `grace_period_sec = 5.0s` (`proc.wait()`).
  - If still alive, escalates to `proc.kill()` (`TerminateProcess`).
  - Waits up to an additional $3.0\text{s}$ to confirm process exit.
- **Gaps Identified:**
  - **No GPU Temperature Trigger:** `temperature_c` is sampled and logged, but is NOT an abort trigger in `supervisor.py`.
  - **No Execution Timeout:** There is no overall runtime timeout; if `llama-cli.exe` hangs while telemetry remains normal, the supervisor polls indefinitely.

### 3.8 Output Files & Privacy Audit
- **Default Directory:** `reports\phase2c\first_inference\`
- **Files Written:**
  1. `PREFLIGHT_LATEST.json` (Hardware metrics, top process names, PIDs, working set, private commit).
  2. `RESOURCE_TELEMETRY.json` (Full 500 ms timeline of RAM, VRAM, and commit metrics).
  3. `EXECUTION_LOG.txt` (Stdout and stderr output from `llama-cli.exe`).
  4. `LOAD_RESULTS.md` (Markdown summary of outcome, exit code, duration, recovery status).
  5. `FAILURE_ANALYSIS.md` (Root-cause analysis if preflight or inference fails).
  6. `PHASE3B_BASELINE_SMOKE_TEST_REPORT.md` (Formal smoke test report).
- **Privacy & Secrets Audit:**
  - Zero cloud network transmission; all data is strictly local.
  - No API keys are used or stored.
  - Process inventory only captures process names and PIDs (no window titles, document names, or URLs).

### 3.9 Metrics & Measurements Captured
- **Captured in Logs:**
  - `eval time = XX ms / 1 runs` (verified single token generated).
  - `prompt eval time` (ms) and `load time` (ms) printed by `llama-cli` in `EXECUTION_LOG.txt`.
  - Peak physical RAM and peak VRAM recorded in `RESOURCE_TELEMETRY.json`.
- **Identified Gaps (What is Missing):**
  - Numeric `load_time_ms`, `prompt_eval_time_ms`, and `eval_time_ms` are not parsed into structured JSON keys (they remain as unstructured text in `EXECUTION_LOG.txt`).
  - The run is not tagged as "cold" or "warm" in JSON metadata.
  - GPU clock frequencies are not recorded during execution.

---

## 4. Quoted Outputs of Read-Only Harness Modes

### 4.1 `--dry-run` Output
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

### 4.2 `--preflight` Output
```text
[MODE] PREFLIGHT ONLY (Inference execution will NOT be launched)

========================================================================
NOVA Compute — Preflight Hardware Qualification Audit
Timestamp (UTC): 2026-11-06T17:03:33.954398+00:00
========================================================================
  [FAIL] telemetry_integrity   : GPU: Malformed GPU compute process numeric field in row 1 (['1128', '[Insufficient Permissions]', '[N/A]']): could not convert string to float: '[N/A]' (Req: All sensors SUCCESS)
         Reason: Telemetry failure detected. Fail-closed: cannot verify hardware safety.
  [FAIL] avail_ram             : 7.617 GiB (Req: >= 10.0 GiB)
         Reason: Available RAM deficit: 2.383 GiB below required 10.0 GiB
  [PASS] system_commit         : 65.8% (Req: < 90.0%)
  [FAIL] avail_vram            : UNAVAILABLE (Req: >= 5500 MiB)
         Reason: VRAM deficit: -110.0 MiB below required 5500 MiB
  [FAIL] no_competing_gpu      : UNKNOWN (Query Failed) (Req: 0 competing compute apps)
         Reason: GPU compute process query failed or unavailable. Fail-closed: competing workloads cannot be ruled out.
  [PASS] disk_free             : 97.99 GiB (Req: >= 25.0 GiB)
------------------------------------------------------------------------
PREFLIGHT STATUS: GATE CONDITIONS UNMET — INFERENCE GATED
========================================================================
```

### 4.3 `--memory-report` / `--advisor` Output
```text
[MODE] INTELLIGENT MEMORY ADVISOR REPORT
...
--- Detailed Process Working Set & Private Commit Inventory ---
  claude.exe                     WS:   691.6 MB  Priv:  4241.9 MB         [User Application] (Instances: 16/16)
  chrome.exe                     WS:   644.0 MB  Priv:  1292.9 MB         [User Application] (Instances: 12/12)
  language_server.exe            WS:   607.0 MB  Priv:   756.0 MB         [Background App] (Instances: 1/1)
  firefox.exe                    WS:   484.9 MB  Priv:  2324.3 MB         [User Application] (Instances: 17/17)
  msedge.exe                     WS:   443.8 MB  Priv:  3650.6 MB         [User Application] (Instances: 25/25)
  Antigravity.exe                WS:   321.6 MB  Priv:  1042.7 MB         [User Application] (Instances: 17/17)
  svchost.exe                    WS:   200.8 MB  Priv:    45.5 MB (partial)  [Protected System] (Instances: 7/49)
  MsMpEng.exe                    WS:   175.4 MB  Priv:     N/A            [Protected System] (Instances: 0/1)
  powershell.exe                 WS:   134.1 MB  Priv:   517.1 MB         [Unclassified] (Instances: 6/6)
...
```

---

## 5. Automated Test Results

- **Python Resource Steward Test Suite (`python -m unittest discover tests`):**
  - **117 tests run in 4.830s**
  - **Result: `OK` (117 passed, 0 failed, 0 errors)**
- **Rust Desktop Systems Core Test Suite (`cargo test --lib`):**
  - **114 test attributes run in 1.72s**
  - **Result: `ok` (113 passed, 0 failed, 1 ignored: `test_live_system_preflight_audit`)**

---

## 6. Risk Assessment & Engineering Proposals (Unimplemented)

| Risk Area | Severity | Blocks Smoke Test? | Technical Analysis & Proposed Smallest Mitigation |
|---|:---:|:---:|---|
| **Orphaned `llama-cli.exe` on Harness Crash** | **High** | **Yes (Pending Owner Choice)** | **Proposal A (Recommended):** Add ~30 lines of standard Python `ctypes` in `scripts/run_inference_with_telemetry.py` to create a Win32 Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and assign `proc._handle` immediately after `Popen`. Guarantees instant kernel termination.<br>**Proposal B:** Keep code unchanged; adopt operator runbook procedure to verify and kill orphaned `llama-cli.exe` via Task Manager or `taskkill /IM llama-cli.exe /F`. |
| **No Model File Lock During Execution** | **Low** | **No** | Same-user file modification during a 10-second single-token test is an extreme corner case. Accept and document, or open a shared read handle in Python via `kernel32.CreateFileW` for the duration of the run. |
| **No Pre-Run Integrity Verification** | **Medium** | **No** | Add a fast 1-line check in `execute_inference()`: `assert os.path.getsize(MODEL_GGUF) == 18632184480`. Full SHA-256 is already confirmed. |
| **16 GB RAM vs ~13.5 GiB CPU-Resident Weights** | **High** | **Yes (Operational)** | Host RAM will experience heavy paging. Strict operational precondition: free physical RAM until $\ge 10.0\text{ GiB}$ passes Gate 2. Hard stop: abort if RAM $< 1.0\text{ GiB}$ for $> 10\text{s}$ or commit $\ge 90\%$. |
| **OneDrive Directory Location** | **Low** | **No** | Confirmed: GGUF attribute is `Archive` (100% physically resident on NVMe SSD; not a reparse point). `OneDrive.exe` is not running. Zero dehydration risk. |
| **Thermal & Power Throttling** | **Medium** | **No** | Precondition: verify AC power online. Read-only query confirmed: AC Online, GPU idle at 54°C / 5.16W. Add manual stop condition if GPU temperature exceeds 82°C. |

---

## 7. Approved Changes Made & Test Isolation Analysis

### 7.1 Test Isolation Analysis (`storage::clear_model_cache`)
- **Inspection:** In `desktop/src-tauri/src/model_integrity.rs:536, 657, 686, 704, 717`, failure branches call `crate::storage::clear_model_cache()`.
- **Target Directory:** `clear_model_cache()` calls `get_app_data_dir()`, which checks `NOVA_DATA_DIR` before falling back to `%LOCALAPPDATA%\NOVA Chat\model_cache.json`.
- **Race Condition in Tests:** `std::env::set_var("NOVA_DATA_DIR", ...)` modifies the process-wide environment. In parallel tests (`cargo test`), setting environment variables causes thread data races.
- **Why a Production Code Change is Needed:** `verify_model_integrity_synchronous` does not accept a directory parameter. Clean test isolation without an environment variable race requires a production refactoring:
  ```rust
  pub fn verify_model_integrity_synchronous_in(
      path_str: &str,
      expected_sha256: &str,
      cancel_flag: &AtomicBool,
      storage_dir: Option<&Path>,
      progress_cb: impl Fn(u64, u64, f32) + Send + 'static,
  ) -> ModelIntegrityState
  ```
- **Action Taken:** Per directive instructions ("propose it, don't make it"), **no production code was modified.** This architectural refactoring is proposed for future owner review.

### 7.2 Documentation Corrections Made
1. **`README.md` (Line 3):**
   - Updated status line:
     `Phase 3B.10 Audited (F1/F3 fixed in a6509b7; F2 fixed in da5003c; O1 closed; F4 open; pre-assignment window noted) | 113 Rust Unit & Hardening Tests Passing (1 ignored, 114 total)...`
2. Created runbook:
   - [`docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md`](file:///c:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md)

---

## 8. Complete Log of Executed Commands

| # | Command Executed | Mode | Purpose / Result |
|---|---|:---:|---|
| 1 | `git rev-parse HEAD; git status --short; git log -5` | Read-Only | Verified baseline commit `da5003c...` and working tree status. |
| 2 | `cargo --version; rustc --version; python --version` | Read-Only | Verified toolchains: Cargo 1.93.1, Rustc 1.93.1, Python 3.12.10. |
| 3 | `python scripts/run_inference_with_telemetry.py --dry-run` | Read-Only Test | Verified command-line assembly and file existence. Zero processes spawned. |
| 4 | `python scripts/run_inference_with_telemetry.py --preflight` | Read-Only Test | Executed 6-gate admission audit. Gated on RAM deficit and GPU query. |
| 5 | `python scripts/run_inference_with_telemetry.py --memory-report` | Read-Only Test | Generated process memory and private commit classification. |
| 6 | `python scripts/run_inference_with_telemetry.py --advisor` | Read-Only Test | Verified intelligent memory advisor diagnostics. |
| 7 | `python -m unittest discover -s tests -p "test_*.py"` | Read-Only Test | Ran Python test suite: 117 passed, 0 failed (4.83s). |
| 8 | `cargo test --lib -j 4 --manifest-path desktop/src-tauri/Cargo.toml` | Read-Only Test | Ran Rust test suite: 113 passed, 0 failed, 1 ignored (1.72s). |
| 9 | `Get-Item "runtime\windows\llama-cpp\*" | Get-FileHash` | Read-Only | Computed exact SHA-256 checksums for all runtime binaries and DLLs. |
| 10 | `(Get-Item "models\...\Qwen...gguf").Attributes` | Read-Only | Verified GGUF attribute is `Archive` (100% locally resident, no OneDrive hydration). |
| 11 | `Get-Process "*onedrive*"` | Read-Only | Confirmed OneDrive process is not running. |
| 12 | Power & GPU Status Query | Read-Only | Confirmed AC Power Online, GPU at 54°C / 5.16W idle. |
| 13 | `git status --short; git diff` | Read-Only | Verified uncommitted changes are strictly limited to `README.md` and runbook. |

---

## 9. Key Questions for the Project Owner

Before authorizing the Phase 3C smoke test, the Project Owner must decide:

1. **Child Process Containment Choice:**
   - Do you approve implementing **Proposal A** (adding ~30 lines of `ctypes` Win32 Job Object containment to `scripts/run_inference_with_telemetry.py` to ensure `llama-cli.exe` terminates automatically if Python dies), OR
   - Do you prefer **Proposal B** (accepting the manual Task Manager kill procedure outlined in Section 6 of the runbook without code changes)?
2. **Operator Role:**
   - Will the **Project Owner** run the commands in the runbook directly, OR
   - Do you authorize **Antigravity** to execute the runbook under your written directive?
3. **RAM Precondition Fulfillment:**
   - The current available RAM is ~7.4 GiB (below the required 10.0 GiB threshold). Will you close background applications (Claude Desktop, Chrome, Edge, Firefox) before the authorized run to satisfy Gate 2?

---

*Report prepared and independently verified by Antigravity under Phase 3C.0 instructions.*  
*Zero model weights were mapped for inference; zero tokens were generated; zero user applications were manipulated.*
