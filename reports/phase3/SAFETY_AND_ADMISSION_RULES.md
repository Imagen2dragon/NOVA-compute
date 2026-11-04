# NOVA Compute — Phase 3A: Safety and Admission Rules
## Non-Negotiable Operational Constraints & Runtime Supervision Governance

**Date:** 2026-11-05  
**Document:** `reports/phase3/SAFETY_AND_ADMISSION_RULES.md`  
**Status:** Design and Static Analysis Only (Phase 3A)  
**Target Hardware:** HP Victus Laptop (RTX 4050 6GB GDDR6, 16GB DDR4-3200 RAM, WD SN810 NVMe)  
**Target Model:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` ($18.63\text{ GB}$)  
**Installed Runtime:** `llama.cpp` Windows x64 CUDA 12.4 (`b12032`)  
**Project Owner:** User  
**Lead Architect:** ChatGPT  
**Implementation Engineer:** Antigravity  

---

## 1. Carried-Forward Preflight Admission Gates (Phase 2D Baseline)

Prior to launching any inference executable or benchmark process, NOVA's `MemoryBudgetManager` executes six non-overridable, fail-closed preflight admission gates. If any single gate fails, inference execution is unconditionally blocked.

```
+-------------------------------------------------------------------------+
|                  NOVA PREFLIGHT ADMISSION GATE SUITE                    |
+-------------------------------------------------------------------------+
  Gate 1: Telemetry Integrity   --> ALL sensors SUCCESS, no NaN/inf, math consistent
  Gate 2: Available Physical RAM --> Available RAM >= 10.0 GiB (10,737,418,240 bytes)
  Gate 3: System Commit Charge  --> Total Commit < 90.0% of system commit limit
  Gate 4: Available Dedicated VRAM-> Free VRAM >= 5,500.0 MiB (NVIDIA AD107)
  Gate 5: Competing GPU Apps    --> Competing GPU compute applications == 0
  Gate 6: Free Storage Capacity --> Free disk >= 25.0 GiB (26,843,545,600 bytes)
+-------------------------------------------------------------------------+
  ALL 6 GATES PASS  ==> ADMISSION GRANTED (Execution authorized)
  ANY 1 GATE FAILS  ==> ADMISSION GATED (Execution blocked; fail-closed)
+-------------------------------------------------------------------------+
```

### Detailed Gate Specifications:
1. **Gate 1: Telemetry Integrity Gate (Fail-Closed):**
   - Telemetry status for RAM, GPU, and Disk must be strictly `TelemetryStatus.SUCCESS`.
   - Byte counters must be non-boolean, non-negative integers bounded by 64-bit unsigned maximum (`0 <= bytes <= MAX_ACCEPTED_DISK_BYTES`).
   - Converted GiB metrics must agree mathematically with raw byte counters within $\le 10^{-9}\text{ GiB}$ tolerance.
   - Any contradictory representation, missing telemetry, or malformed numeric input immediately fails the gate closed.
2. **Gate 2: Available Physical RAM Gate ($\ge 10.0\text{ GiB}$):**
   - Requires at least $10.0\text{ GiB}$ ($10,737,418,240\text{ bytes}$) of available physical memory.
   - Guarantees sufficient physical RAM headroom for the logical active MoE expert working set (~862.6 MiB per forward pass), process heap (~400 MB), and OS operation without forced swapping. Note that available RAM is dynamic and fluctuates with desktop applications; preflight runs have observed 8.66–9.91 GiB available.
3. **Gate 3: System Commit Gate ($< 90.0\%$):**
   - Verifies that system-wide virtual commit charge is strictly below 90.0% of the commit limit (`SizeStoredInPagingFiles + TotalPhysicalMemory`).
   - Prevents memory-allocation failures in background desktop components and driver staging buffers.
4. **Gate 4: Available Dedicated VRAM Gate ($\ge 5,500.0\text{ MiB}$):**
   - Requires at least 5,500 MiB of unallocated GDDR6 memory on the RTX 4050 GPU.
   - Ensures sufficient capacity for offloaded dense layers ($1,022.5\text{ MiB}$), GPU MoE layers ($2,938.5\text{ MiB} \approx 2.87\text{ GiB}$ in Config D), KV cache ($25.5\text{ MiB}$ at ctx 512), and compute scratch buffers.
5. **Gate 5: Competing GPU Compute Gate ($== 0$ Apps):**
   - Verifies via NVML that zero external compute processes are running on the discrete GPU.
   - Prevents VRAM allocation race conditions and CUDA context contention.
6. **Gate 6: Free Storage Capacity Gate ($\ge 25.0\text{ GiB}$):**
   - Authoritative integer byte counters must satisfy `free_bytes >= ceil(threshold_gib * (1024 ** 3))`.
   - Validates that policy thresholds are strictly bounded within $(0, \text{MAX\_ACCEPTED\_DISK\_GIB}]$ prior to calculation, failing closed on non-numeric or out-of-range inputs.

---

## 2. Why Admission Success Does Not Prove Model-Load Feasibility

A critical principle of resource-aware systems engineering: **Preflight admission success is a necessary precondition, not a guarantee of execution feasibility.**

### Reasons Preflight Success Alone Does Not Ensure Feasibility:
1. **Preflight is an Idle-State Snapshot:** Preflight evaluates the machine's memory state before `llama.cpp` has spawned or allocated any memory. It proves that the operating environment is clean and meets baseline safety floors, but cannot predict runtime behavior.
2. **Dynamic Runtime Allocation Spikes:**
   - **CUDA Context Overhead:** Initializing the CUDA driver context consumes ~150–250 MiB of VRAM that is not reflected in static model weight math.
   - **Compute Graph Scratchpads:** GGML allocates dynamic scratch buffers for FlashAttention and intermediate tensor activations during the first forward pass. If physical batch size (`-ub`) is too large, activation buffers can spike by 300–600 MiB.
   - **KV Cache Materialization:** Allocating the KV cache commits contiguous memory blocks that can fail if VRAM is fragmented, even if total free VRAM appears sufficient.
3. **Working Set Growth Under Token Generation:**
   - While preflight confirms $\ge 10.0\text{ GiB}$ RAM is available, sequential token generation that activates diverse experts over a long context will cause the process's working set to grow as new file pages are faulted in from NVMe.
   - If the cumulative working set exceeds available RAM, the OS will begin discarding standby pages and page-faulting from disk, degrading throughput even though preflight passed.

**Engineering Conclusion:** Preflight gates prevent launching into an already-constrained environment, but **continuous runtime supervision** is mandatory throughout the entire execution lifecycle.

---

## 3. Runtime Supervision and Telemetry Protocol

During any future authorized inference run, the child process must be continuously monitored by NOVA's `ResourceSupervisor`:

1. **High-Frequency Monotonic Sampling:**
   - Telemetry must be collected every **500 milliseconds** using a monotonic clock (`time.monotonic()`).
   - Samples must be validated for strict temporal monotonicity ($t_{k} > t_{k-1}$) and gap detection ($\Delta t \le 2.0\text{s}$). If telemetry sampling stalls for $>2.0\text{ seconds}$, the supervisor flags telemetry corruption.
2. **Multi-Tier Metric Tracking:**
   - System available RAM, committed memory, and memory load percentage.
   - Process resident working set (`WorkingSetSize`) and private commit (`PrivateUsage`).
   - Dedicated GPU VRAM allocated, free VRAM, GPU temperature, and power draw.
   - Page fault rate and disk page read activity (`pages_input_per_sec`).

---

## 4. Safe Stop and Critical Abort Requirements

If system stability is threatened during execution, the supervisor must immediately trigger an automated recovery sequence to protect host stability:

### 4.1 Abort Triggers
Execution must be terminated immediately if any of the following conditions occur:
1. **Sustained Low Physical RAM:** Available physical RAM drops below **2.0 GiB** for **5.0 consecutive seconds**.
2. **Critical VRAM Depletion:** Free dedicated VRAM drops below **512 MiB**.
3. **Critical System Commit:** System-wide commit charge reaches or exceeds **90.0%**.
4. **CUDA Out-of-Memory:** The child process emits a CUDA OOM error (`cudaErrorMemoryAllocation` or `out of memory`) detected in stdout/stderr queues.
5. **Process Stagnation / Hang:** Zero token output or process activity for greater than 30.0 seconds.

### 4.2 Two-Stage Graceful Escalation Protocol
When an abort is triggered, the supervisor must strictly follow the verified two-stage recovery protocol:
1. **Stage 1 (Graceful Console Break):**
   - The supervisor sends a Win32 `CTRL_BREAK_EVENT` to the process group of the child process.
   - The child process is granted a **5.0-second grace period** to execute internal cleanup handlers, flush telemetry buffers, and release CUDA allocations.
2. **Stage 2 (Forced Termination):**
   - If the child process does not exit within the 5.0-second grace window, the supervisor escalates to forced termination (`process.kill()`).
   - The supervisor polls process exit, records post-termination RAM and VRAM recovery deltas, and logs an explicit `ABORTED` audit report.

---

## 5. Prohibited Operations & Governance Constraints

### 5.1 Prohibited Memory-Cleaning APIs
NOVA strictly adheres to zero-invasive system memory governance. Automated AST verification (`test_32` and `test_58`) enforces that none of the following Win32 memory-cleaner APIs exist anywhere in the codebase:
- `EmptyWorkingSet` / `K32EmptyWorkingSet`
- `NtSetSystemInformation`
- `MemoryPurgeStandbyList`
- `MemoryFlushModifiedList`
- `SetSystemFileCacheSize`
- `SetProcessWorkingSetSize`

**Technical Rationale:**  
Third-party memory cleaners (such as aggressive standby purgers) force the operating system to discard cached file pages and flush modified data to disk. On a laptop running local LLMs, purging the file cache discards the memory-mapped model weights, forcing the storage subsystem to re-read gigabytes of data from NVMe on the very next token. This destroys inference throughput and induces severe I/O stutter.

### 5.2 Prohibited External-Process Manipulation
- NOVA **never** terminates, suspends, or modifies the priority of external user applications (e.g. browsers, IDEs, background workers).
- The Memory Advisor operates in a strictly **read-only, advisory capacity**. It identifies high-memory processes and provides clear human recommendations. All application closures are performed exclusively by the user through normal application interfaces.

### 5.3 Why Pagefile and Registry Modifications are Outside Scope
1. **System Pagefile (`pagefile.sys`):**
   - Disabling or reducing the Windows pagefile is strictly prohibited.
   - The system pagefile provides an essential safety net for modified anonymous memory. Disabling the pagefile does not increase physical RAM; rather, it causes instant system crashes and kernel bugchecks (BSOD) the moment committed virtual memory exceeds physical capacity.
2. **Windows Registry Edits:**
   - Modifying Windows memory management registry keys (e.g. `LargeSystemCache`, `DisablePagingExecutive`, or driver timeouts) is dangerous, non-portable, and unsupported.
   - NOVA must operate cleanly within the standard user-space boundaries of Microsoft Windows 11 Home without requiring administrator elevation or OS reconfigurations.
