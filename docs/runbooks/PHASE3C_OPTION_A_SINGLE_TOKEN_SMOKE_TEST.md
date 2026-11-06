# NOVA Compute — Phase 3C: Option A Single-Token Smoke Test Operator Runbook

**Document Path:** `docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md`  
**Target Architecture:** Configuration D (`-ngl 48 -ncmoe 40 -c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap -n 1`)  
**Target Model:** `Qwen/Qwen3-30B-A3B-Q4_K_M` (~18.63 GB / 17.35 GiB, SHA-256 `a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32`)  
**Target Machine:** HP Victus Laptop (RTX 4050 6GB Laptop GPU, 16GB Host DDR4-3200, 1TB WD SN810 NVMe)  
**Execution Scope:** Standalone Option A CLI Harness (`scripts/run_inference_with_telemetry.py`), NOT NOVA Chat Desktop UI.  
**Baseline Git Commit:** `cae2a811baf14eccfb3de1c7a03af1132a46f010`  

---

## 1. Purpose & Scope

This runbook defines the exact operational procedure for executing a single-token smoke test under Configuration D using the standalone Python telemetry harness and `llama-cli.exe`.

- **Strict Scope Limit:** Exactly ONE generated token (`-n 1`).
- **Target Prompt:** `"Hello"` (5 tokens prompt evaluation, 1 token generation).
- **Not a Benchmark:** This run is solely a binary runtime feasibility check to prove whether Qwen3-30B-A3B can initialize CUDA, allocate VRAM, map expert weights into host virtual memory, and emit a single token without kernel panics, OOM faults, or GPU crashes.
- **Containment Architecture:** This test does NOT run through the NOVA Chat Desktop application (`nova-chat.exe`). However, the Option A Python harness is contained inside a Win32 Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (`nova/resource_steward/job_containment.py`). Because the harness joins this job before spawning `llama-cli.exe`, Windows kernel containment guarantees that if the Python process dies, crashes, or is terminated, `llama-cli.exe` is killed automatically by the kernel. (File lock duplication and restrictive share modes remain specific to the Rust desktop server).

---

## 2. Authorization & Operator Roles

- **Who Runs This Test:**
  - Either the **Project Owner** directly, OR
  - **Antigravity** strictly upon receiving explicit, written Project Owner authorization referencing this specific runbook and Git commit.
- **Single-Use Mandate:**
  - Authorization applies to exactly **one** execution attempt per written authorization.
  - Automatic retries, follow-up benchmark iterations, or multi-token generations are strictly prohibited without a new, separate written directive.
- **Safety Governance:**
  - The Project Owner and operator own process management. If the preflight gates fail, do not force execution.

---

## 3. Preconditions Checklist

Before executing any commands, verify that **every** item in this checklist is satisfied:

- [ ] **1. Power State:** Laptop must be physically connected to AC power (`PowerLineStatus: Online`). Battery execution is prohibited due to OEM power throttling of the RTX 4050 GPU.
- [ ] **2. Manual Application Closure (External Apps):** The Project Owner must manually close non-essential memory-heavy applications and competing GPU applications through their normal UI interfaces (NOVA never force-terminates external software):
  - **Close Competing GPU Applications:**
    - OMEN LightStudio (`LightStudio-background.exe`)
    - OMEN Command Center (background and UI processes: `OmenCommandCenterBackground.exe`, `HP.Omen.OmenCommandCenter.exe`)
    - Telegram Desktop (`Telegram.exe`)
    - Web browsers with GPU hardware acceleration (Chrome, Edge, Firefox)
    - *Exemption Note:* `dwm.exe` (Desktop Window Manager) is verified as `%SystemRoot%\System32\dwm.exe` and is **exempt** under the Project Owner rule. Do **NOT** attempt to close or terminate `dwm.exe`.
  - **Free Physical Host Memory:**
    - Close Claude Desktop (*Important Note: closing Claude Desktop will terminate any open Claude session*).
    - Close unnecessary browser windows, tabs, and development tools until Available Physical RAM is $\ge 10.00\text{ GiB}$.
- [ ] **3. Fresh Preflight Verification:** Run `python scripts/run_inference_with_telemetry.py --preflight` within **2 minutes** prior to launch. All six gates must pass:
  - Gate 1: Sensor Telemetry Integrity = `Healthy`
  - Gate 2: Available Physical RAM $\ge 10.00\text{ GiB}$
  - Gate 3: System Commit Charge $< 90.0\%$
  - Gate 4: Dedicated Free VRAM $\ge 5,500.0\text{ MiB}$
  - Gate 5: Competing GPU Compute Processes $= 0$ (with verified `dwm.exe` exempt)
  - Gate 6: Free NVMe Disk Space $\ge 25.00\text{ GiB}$
- [ ] **4. Model File Integrity Check:** Verify the file length matches exactly:
  - Command: `(Get-Item "models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf").Length`
  - Expected: `18632184480` bytes.
  - File attributes must be `Archive` (physically resident on NVMe SSD, not a cloud placeholder).
- [ ] **5. No Extraneous Processes:** Confirm no other instance of `llama-cli.exe`, `llama-server.exe`, or `nova-chat.exe` is currently running:
  - Command: `Get-Process llama-cli, llama-server, nova-chat -ErrorAction SilentlyContinue`
  - Must return zero processes.

---

## 4. Exact Execution Procedure (Copy-Pasteable)

Open a standard (non-elevated) PowerShell terminal in the repository root directory:
```powershell
cd C:\Users\abdal\OneDrive\Desktop\nova_compute_antigravity_starter
```

### Step 1: Pre-Execution Environment & Sanity Check
```powershell
# 1. Verify AC power
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SystemInformation]::PowerStatus.PowerLineStatus

# 2. Verify model file size
(Get-Item "models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf").Length

# 3. Check GPU idle state and temperature
nvidia-smi --query-gpu=temperature.gpu,power.draw,memory.free,memory.used --format=csv
```

### Step 2: Validate Harness Syntax (Dry Run)
```powershell
python scripts/run_inference_with_telemetry.py --dry-run
```
*Expected Output:* `Dry run completed successfully. (No inference was started).`

### Step 3: Run Live Preflight Audit
```powershell
python scripts/run_inference_with_telemetry.py --preflight
```
*Criteria:* Output must conclude with `PREFLIGHT STATUS: ALL GATES PASSED`. If any gate fails (e.g. RAM deficit or competing GPU processes), do NOT proceed to Step 4; close competing applications, free additional memory, and re-run Step 3.

### Step 4: Execute Authorized Single-Token Smoke Test
*Only run this step after the Project Owner has provided written authorization referencing commit `cae2a811baf14eccfb3de1c7a03af1132a46f010`.*

```powershell
python scripts/run_inference_with_telemetry.py --execute
```

When prompted by the interactive Guided Startup controller:
```text
[Confirm Execution] Type 'RUN CONFIGURATION D SMOKE TEST ONCE' to authorize, or 'Q' to cancel:
```
Type:
```text
RUN CONFIGURATION D SMOKE TEST ONCE
```
and press Enter.

---

## 5. Live Monitoring During Execution

While the smoke test executes (~10–25 seconds total duration):

1. **Terminal Output:** Watch for stdout markers from `llama-cli.exe`:
   - `llama_model_loader: loaded meta data with 41 key-value pairs and 579 tensors...`
   - `llm_load_tensors: offloaded 48/48 layers to GPU`
   - `llm_load_tensors: CPU_MOE offloaded 40/48 layers to CPU`
   - `print_timings: prompt eval time = ... ms / 5 tokens`
   - `print_timings: eval time = ... ms / 1 runs`
2. **Task Manager (Performance Tab):**
   - **Memory:** Observe physical memory usage climb as ~4.1 GiB VRAM loads and Windows fault-pages ~1.5–2.5 GiB of active CPU weights.
   - **GPU 0 (RTX 4050):** Dedicated GPU memory usage should rise to ~5.1–5.4 GiB out of 6.0 GiB.
3. **Telemetry Sampler:** The background sampler will record RAM, VRAM, and commit at 500 ms intervals into `reports/phase2c/first_inference/RESOURCE_TELEMETRY.json`.

---

## 6. Stop Conditions & Emergency Manual Abort

### Automatic Abort Triggers (Built into Supervisor)
The supervisor will immediately initiate teardown of `llama-cli.exe` via `SafeReclamationManager.terminate_child_process` (sending `CTRL_BREAK_EVENT` with graceful wait, followed by escalation to `proc.kill()` / `TerminateProcess`) if any of the following triggers occur:
- **Maximum Execution Duration:** Child process execution time exceeds **600 seconds** (10 minutes hard wall-clock limit).
- **GPU Thermal Ceiling:** Dedicated GPU temperature reaches $\ge 87^\circ\text{C}$ (`critical_gpu_temp_c = 87.0`).
- **Missing Sensor Reading:** Dedicated GPU temperature reading is missing, invalid, or non-finite (`cur_temp is None or not math.isfinite(cur_temp)`).
- **Low Available RAM:** Available physical RAM drops below **2.0 GiB** continuously for $\ge 5.0\text{ seconds}$.
- **High Commit Charge:** System commit charge reaches $\ge 90.0\%$.
- **VRAM Exhaustion:** Free dedicated VRAM drops below **512 MiB**.
- **CUDA OOM:** `CUDA error: out of memory` (or CUDA error) is detected in output streams.
- **Telemetry Stall:** Live telemetry sampler fails to deliver a sample for $> 3.0\text{ seconds}$.

### Operator Manual Stop Criteria
The operator must immediately abort the test manually if:
- Dedicated GPU temperature reported by `nvidia-smi` exceeds **82°C** (stricter manual operator ceiling; supervisor automatic abort is $87^\circ\text{C}$).
- The Windows operating system becomes unresponsive or mouse input freezes for $> 5\text{ seconds}$ (indicating catastrophic disk paging thrash).
- The process fails to emit output for $> 60\text{ seconds}$.

### Teardown & Fallback Cleanup Procedure
- **Primary Kernel Containment:** The harness process and child `llama-cli.exe` run inside a Win32 Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. If the Python harness exits, crashes, or is killed, Windows kernel cleanup automatically terminates `llama-cli.exe`.
- **Manual Teardown (Normal Abort):** Press `Ctrl + C` or `Ctrl + Break` in the PowerShell terminal running the harness.
- **Fallback Verification (Task Manager):** Keep the manual kill procedure **only as a fallback** if `llama-cli.exe` is still visible 10 seconds after the harness has exited:
  1. Open a PowerShell terminal and run:
     ```powershell
     taskkill /IM llama-cli.exe /F
     ```
  2. Verify process termination:
     ```powershell
     Get-Process llama-cli -ErrorAction SilentlyContinue
     ```
     Ensure zero instances remain.

---

## 7. Success Criteria

A successful smoke test is achieved ONLY if ALL of the following conditions are confirmed:

1. **Exit Code Zero:** `llama-cli.exe` exits with return code `0`.
2. **Verified Generated Token:** Terminal output explicitly confirms evaluation of at least one generated token:
   `eval time = XX.XX ms / 1 runs` (or `1 tokens generated`). Prompt evaluation alone does NOT constitute success.
3. **Zero Supervisor Aborts:** No safety threshold was breached; `supervisor_res.abort_triggered == False`.
4. **Complete Resource Recovery:** Within **60 seconds** after process exit:
   - Dedicated GPU VRAM used returns to within $\pm 5\%$ of pre-run baseline ($\le 350\text{ MiB}$).
   - System available RAM returns to within $\pm 5\%$ of pre-run baseline.
   - Reader and sampler background threads confirm verified EOF and clean shutdown.
5. **Model File Immutability:** The GGUF file size on disk is exactly `18,632,184,480` bytes before and after execution.
6. **No Residual Processes:** No `llama-cli.exe` process remains running in Task Manager or the OS process table (`Get-Process llama-cli` returns nothing).

---

## 8. What to Record Post-Test

Record the following empirical measurements in the post-test report:

1. **Governance & Integrity Tracking:**
   - Exact Git commit hash: `cae2a811baf14eccfb3de1c7a03af1132a46f010`.
   - Runtime binary SHA-256 hashes:
     - `llama-cli.exe`: `24985E809FA0903790E22623F4D4051DA8164D1058377E51089358ED0A3DEAC8`
     - `llama-cli-impl.dll`: `00F6F281009599FC126B17CAED66832B8E20F7B8094A8B3E97381163016DB32E`
   - Model file size (`18,632,184,480` bytes) and SHA-256 (`a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32`).
   - Single-token execution latch consumption confirmed (`is_consumed = True`).
2. **Timings (from `EXECUTION_LOG.txt`):**
   - Total runtime duration (seconds).
   - Cold vs. Warm run classification.
   - `load time` (ms).
   - Prefill and decode reported separately:
     - Prompt eval (prefill) time (ms) and throughput (tokens/second).
     - Eval (decode) time (ms) for the single generated token (tokens/second).
3. **Resource Peaks (from `RESOURCE_TELEMETRY.json`):**
   - Peak physical RAM consumed (GiB).
   - Peak dedicated VRAM allocated (MiB).
   - Peak system commit percentage (%).
4. **Thermals & Abort State:**
   - Peak GPU temperature during load and decode (°C).
   - Average GPU power draw (W).
   - Abort reason if any abort was triggered (or confirm zero aborts).

---

## 9. Post-Execution Policy

- **Do Not Repeat:** Exactly one execution attempt per written authorization. Regardless of whether the smoke test succeeds or fails, do NOT execute a second run.
- **Reporting:** Immediately submit the output files (`LOAD_RESULTS.md`, `RESOURCE_TELEMETRY.json`, `EXECUTION_LOG.txt`) to the Lead Architect and Project Owner for review.
- **Next Steps:** Any further token generation or multi-token benchmarking requires a formal review of these results and a new written Phase directive.
