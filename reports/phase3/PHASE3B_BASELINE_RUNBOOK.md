# NOVA Compute — Phase 3B: Baseline Qualification Runbook
## Controlled Configuration D Baseline Smoke-Test Specification

**Document:** `reports/phase3/PHASE3B_BASELINE_RUNBOOK.md`  
**Date:** 2026-11-05  
**Status:** Preparation and Read-Only Qualification Only — **Inference Execution NOT Authorized**  
**Repository:** `Imagen2dragon/NOVA-compute`  
**Branch:** `feature/phase2d-memory-advisor`  
**Project Owner:** User  
**Lead Architect:** ChatGPT  
**Implementation Engineer:** Antigravity  

---

## 1. Governance & Execution Boundary

> [!IMPORTANT]
> **Strict Operational Boundary:**  
> This runbook defines the exact technical procedure, safety preconditions, telemetry acquisition parameters, and verification requirements for the upcoming Phase 3B Configuration D baseline smoke test.  
> **This document does NOT authorize inference.** Model execution (`--execute`), weight loading, and benchmark runs remain strictly blocked until the Project Owner provides separate, explicit authorization following Lead Architect review.

### Mandatory Pre-Run Checklist:
- [x] Static memory hierarchy architecture documented and approved (`reports/phase3/MEMORY_HIERARCHY_ARCHITECTURE.md`).
- [x] Per-layer expert quantization ledger reconciled (`reports/phase3/MEMORY_PLACEMENT_LEDGER.md`).
- [x] Single-variable experiment matrix defined (`reports/phase3/EXPERIMENT_MATRIX.md`).
- [x] Runtime option support and device capabilities empirically inventoried.
- [ ] Fresh live preflight passes all six Phase 2D safety gates immediately prior to launch.
- [ ] Separate explicit execution authorization received from Project Owner.

---

## 2. Runtime Capability & Environment Inventory

All parameters below were obtained via verified, read-only system queries and binary inspection without loading model weights:

### 2.1 Bundled Executable & Compiler Metadata
- **Runtime Path:** `runtime\windows\llama-cpp\llama-cli.exe`
- **Binary Version:** `0.8.0-dev` (build `12032`, commit `dde879bc4`)
- **Compiler:** Clang 20.1.8 for Windows `x86_64`
- **Runtime Build Target:** `llama.cpp` Windows x64 with CUDA 12.4 backend (`ggml-cuda.dll`, 594 MB)

### 2.2 CUDA Driver & Hardware Subsystem
- **GPU Name:** NVIDIA GeForce RTX 4050 Laptop GPU (AD107)
- **NVIDIA Driver Version:** `592.82` (Windows Display Driver Model: WDDM 3.2)
- **Driver-Supported CUDA Version:** CUDA 13.1 (maximum driver capability)
- **Bundled CUDA Runtime:** CUDA 12.4 (`cudart64_12.dll`, FileVersion: `6.14.11.12040`)
- **GPU Compute Capability:** `8.9` (Ada Lovelace architecture)
- **Dedicated Video Memory (Total):** `6,141 MiB` GDDR6 (96-bit bus, ~192 GB/s peak theoretical bandwidth)
- **Observed Idle Free VRAM:** `5,920 MiB` (Windows DWM consumes ~200–350 MiB)
- **PCIe Interface Capability:** PCIe Gen4 x8 capable (~15.75 GB/s peak unidirectional); observed at Gen1 x8 during low-power idle and dynamically scaling to Gen4 x8 under load.

### 2.3 CUDA Device Properties (Read-Only Attribute Verification)
Queried directly via CUDA Driver API (`nvcuda.dll` / `cuDeviceGetAttribute` on Device 0):
- `CU_DEVICE_ATTRIBUTE_MANAGED_MEMORY` (attr 83): **`1`** (Device supports managed memory allocation via `cudaMallocManaged`).
- `CU_DEVICE_ATTRIBUTE_CONCURRENT_MANAGED_ACCESS` (attr 89): **`0`** (Device does **NOT** support concurrent managed access; fine-grained page-fault migration is unavailable in this Windows WDDM configuration).
- `CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS` (attr 90): **`1`**.
- `CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS_USES_HOST_PAGE_TABLES` (attr 91): **`0`**.

### 2.4 Bundled CLI Option Support Matrix

| Option Flag | Semantic Function | `llama-cli.exe` | `llama-server.exe` | `llama-bench.exe` | Verified Operational Constraints |
|---|---|:---:|:---:|:---:|---|
| `-ngl N` | Max layers offloaded to GPU | **Supported** | **Supported** | **Supported** | Offloads dense layers and MoE layers up to $N$. |
| `-ncmoe N` | CPU MoE layer placement count | **Supported** | **Supported** | **Supported** | Retains MoE weights of first $N$ layers in CPU virtual memory (`mmap`). **Does NOT pin physical RAM.** |
| `--moe-cache-mib N` | Dynamic GPU MoE expert cache size | **Supported** | **Supported** | **NOT Supported** | GPU VRAM cache for CPU-resident experts. Internal replacement heuristic (do not assume strict LRU). |
| `-lm MODE` | Model file loading mode | **Supported** | **Supported** | **Supported** | Modes: `auto`, `none`, `mmap`, `mlock`, `mmap+mlock`. |
| `-lzm MODE` | Lazy loading mode | **Supported** | **Supported** | **Supported** | Modes: `auto`, `off`, `on`. In `b12032`, `auto` only applies to individual tensors $>4\text{ GiB}$. |
| `-c N` | Context window size | **Supported** | **Supported** | **Supported** | Strictly sizes the KV cache CUDA buffer. |
| `-b N` / `-ub N` | Batch & micro-batch sizes | **Supported** | **Supported** | **Supported** | `-ub` controls dynamic activation scratchpad size. |
| `-ctk` / `-ctv` | KV cache quantization type | **Supported** | **Supported** | **Supported** | Quantized KV cache format (`q8_0`, `f16`, `q4_0`). |
| `-fa MODE` | FlashAttention mode | **Supported** | **Supported** | **Supported** | Modes: `auto`, `off`, `on`. |
| `--mlock` | Physical memory locking | **Supported** | **Supported** | **Supported** | Fails or induces OS instability if model exceeds available RAM. |

---

## 3. Configuration D Baseline Smoke-Test Specification

The Configuration D baseline smoke test is designed to verify allocation stability, measure initial baseline overheads, and validate clean process teardown with minimal hardware strain.

### 3.1 Exact Command-Line String

```powershell
C:\Users\abdal\OneDrive\Desktop\nova_compute_antigravity_starter\runtime\windows\llama-cpp\llama-cli.exe `
  -m C:\Users\abdal\OneDrive\Desktop\nova_compute_antigravity_starter\models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf `
  -ngl 48 `
  -ncmoe 40 `
  -c 512 `
  -b 64 `
  -ub 16 `
  -ctk q8_0 `
  -ctv q8_0 `
  -fa auto `
  -lm mmap `
  -n 1 `
  --simple-io `
  --perf `
  -p "Hello"
```

### 3.2 Workspace Preservation & Subprocess Parameters
When executed via NOVA's `ResourceSteward` (`scripts/run_inference_with_telemetry.py`), the subprocess must strictly inherit the following Win32 process creation attributes:
- **Priority Class:** `BELOW_NORMAL_PRIORITY_CLASS` (`0x00004000`) — Guarantees desktop responsiveness and prevents thread starvation of core Windows services.
- **Process Group:** `CREATE_NEW_PROCESS_GROUP` (`0x00000200`) — Required for clean console break signal delivery (`CTRL_BREAK_EVENT`) without killing the parent runner.
- **Token Prediction Bound:** Strictly **`-n 1`** (exactly one output token generated).
- **GPU Expert Cache:** Strictly **`--moe-cache-mib 0`** (disabled; zero VRAM allocated to dynamic expert caching).
- **Physical Memory Locking:** Strictly **no `--mlock`** (file-backed Win32 `mmap` only).

---

## 4. Preflight Admission Gate Qualification Rules

Prior to launching the smoke test, NOVA's `MemoryBudgetManager` executes the six non-overridable, fail-closed preflight admission gates. If any single gate fails, execution is blocked immediately.

```
+----------------------------------------------------------------------------------------------------+
|                                NOVA PREFLIGHT ADMISSION GATE SUITE                                  |
+----------------------------------------------------------------------------------------------------+
| Gate 1: Telemetry Integrity   | ALL sensors SUCCESS; no NaN/inf; GiB-byte agreement within 1e-9 GiB |
| Gate 2: Available Physical RAM| Available RAM >= 10.0 GiB (10,737,418,240 bytes)                   |
| Gate 3: System Commit Charge  | Total Virtual Commit < 90.0% of system commit limit                |
| Gate 4: Available VRAM        | Free Dedicated VRAM >= 5,500.0 MiB (NVIDIA AD107)                  |
| Gate 5: Competing GPU Apps    | Competing GPU compute applications == 0                            |
| Gate 6: Free Storage Capacity | Free disk >= 25.0 GiB (26,843,545,600 authoritative bytes)         |
+----------------------------------------------------------------------------------------------------+
```

### Operational Guidance for Gate 2 (Available RAM):
- Target machine physical RAM is 16.0 GB (15.65 GB visible to OS).
- Background desktop applications (e.g. web browsers, background IDE processes, language servers) consume ~6–7 GB during normal desktop usage, causing available RAM to hover between 8.3 GiB and 9.9 GiB.
- **Action Required Before Run:** The user must review the read-only Memory Advisor recommendations (`--preflight` or `--memory-report`) and close non-essential applications through their normal UI until available RAM satisfies $\ge 10.0\text{ GiB}$.
- **NOVA Never Forces Process Closures:** No external application is ever terminated programmatically by NOVA.

---

## 5. Continuous Runtime Telemetry Requirements

During execution, NOVA's `ResourceSupervisor` monitors the child process at **500ms monotonic intervals** (`time.monotonic()`) across all memory tiers:

### Telemetry Channels & Metrics:

| Category | Telemetry Field | Target Metric & Collection Source | Sampling Frequency |
|---|---|---|---|
| **System Memory** | `avail_phys_gib` | Win32 `GlobalMemoryStatusEx()` (`ullAvailPhys`) | 500 ms |
| **System Memory** | `commit_pct` | Virtual commit charge relative to commit limit | 500 ms |
| **Process Memory** | `working_set_mb` | Win32 `GetProcessMemoryInfo()` (`WorkingSetSize`) | 500 ms |
| **Process Memory** | `private_commit_mb`| Win32 `GetProcessMemoryInfo()` (`PrivateUsage`) | 500 ms |
| **Process Memory** | `page_fault_count` | Cumulative soft + hard page faults | 500 ms |
| **Storage Activity**| `pages_input_sec` | NT PerfCounters (`Memory\Pages Input/sec`) | 500 ms |
| **GPU Subsystem** | `vram_used_mib` | NVML `nvmlDeviceGetMemoryInfo()` (`used`) | 500 ms |
| **GPU Subsystem** | `vram_free_mib` | NVML `nvmlDeviceGetMemoryInfo()` (`free`) | 500 ms |
| **GPU Subsystem** | `gpu_util_pct` | NVML `nvmlDeviceGetUtilizationRates()` (`gpu`) | 500 ms |
| **GPU Subsystem** | `gpu_temp_c` | NVML `nvmlDeviceGetTemperature()` | 500 ms |
| **Timing & Throughput**| `load_time_ms` | Parsed from `llama.cpp` `--perf` output | End of run |
| **Timing & Throughput**| `prompt_eval_ms` | Parsed from `llama.cpp` `--perf` output | End of run |
| **Timing & Throughput**| `eval_time_ms` | Parsed from `llama.cpp` `--perf` output | End of run |

---

## 6. Mandatory Stop and Critical Abort Conditions

If system resource limits are approached during execution, the supervisor triggers an immediate automated recovery sequence:

### 6.1 Abort Triggers
1. **Critical VRAM Depletion:** Free dedicated VRAM drops below **`512 MiB`** at any sample.
2. **Sustained Low RAM:** Available physical RAM drops below **`2.0 GiB`** for **`5.0 consecutive seconds`**.
3. **Critical System Commit:** System virtual commit charge reaches or exceeds **`90.0%`**.
4. **CUDA Out-of-Memory:** Child process emits a CUDA OOM error (`cudaErrorMemoryAllocation` or `out of memory`).
5. **Process Stagnation / Hang:** Zero token output or process telemetry activity for **`> 30.0 seconds`**.

### 6.2 Two-Stage Recovery Sequence
1. **Stage 1 (Console Break):** Supervisor sends Win32 `CTRL_BREAK_EVENT` to the process group. Child is granted a **5.0-second grace window** to flush telemetry and cleanly release CUDA device allocations.
2. **Stage 2 (Forced Termination):** If child fails to exit within 5.0 seconds, supervisor issues `process.kill()`, verifies process termination, logs post-mortem memory recovery deltas, and saves an `ABORTED` audit report.

---

## 7. Expected Outputs & Artifact Deliverables

Upon completion of an authorized run, the following structured artifacts must be produced:

```
reports/phase2c/first_inference/
├── PREFLIGHT_LATEST.json    # Complete 6-gate preflight sensor evaluation
├── RESOURCE_TELEMETRY.json  # 500ms time-series telemetry array
├── EXECUTION_LOG.txt        # Full stdout/stderr stream from llama-cli
├── LOAD_RESULTS.md          # Human-readable execution summary with --perf timings
└── FAILURE_ANALYSIS.md      # Detailed diagnostic report (if gated or aborted)
```

### Expected `--perf` Console Output:
```
llama_perf_context_print:        load time =     XXXX.XX ms
llama_perf_context_print: prompt eval time =      XXX.XX ms /     X tokens
llama_perf_context_print:        eval time =      XXX.XX ms /     1 runs
llama_perf_context_print:       total time =     XXXX.XX ms /     X tokens
```

---

## 8. Material Unknowns to Measure During Smoke Test

The primary scientific objective of this 1-token smoke test is to convert the following **`[UNKNOWN]`** parameters into empirical **`[Measured]`** values:

1. **CUDA Driver Context Initialization Overhead:**  
   *Current Estimate:* ~150–250 MiB.  
   *Empirical Measurement:* Pre-run free VRAM minus VRAM immediately following CUDA backend initialization before model graph allocation.
2. **Actual Dense-Weight Device Allocation Overhead:**  
   *Current Estimate:* $1,022.5\text{ MiB}$ [Derived — GGUF tensor inventory].  
   *Empirical Measurement:* Exact delta in dedicated VRAM allocated for the 48 offloaded dense layers under GGML CUDA page alignment.
3. **Compute Graph Scratchpad Allocation:**  
   *Current Estimate:* ~200–450 MiB.  
   *Empirical Measurement:* Peak VRAM during the prompt evaluation and 1-token decode step minus static weight allocation.
4. **Net Unallocated Safety Margin Above 512 MiB Floor:**  
   *Current Estimate:* ~720–1,070 MiB.  
   *Empirical Measurement:* Minimum free VRAM observed throughout the entire execution lifecycle.
5. **Initial Cold Page-Fault Count & Startup Latency:**  
   *Current Estimate:* Unknown.  
   *Empirical Measurement:* Total `PageFaultCount` incurred to fault in the dense layers and the first token's active CPU MoE expert slices.

---

## 9. Next Steps

1. Commit this runbook locally on branch `feature/phase2d-memory-advisor`.
2. Present this document to the **Project Owner (User)** and **Lead Architect (ChatGPT)** for review and evaluation.
3. **Await explicit execution authorization.** Zero inference commands will be executed until authorized.
