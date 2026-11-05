# NOVA Compute — Phase 3B.2: Controller Memory Footprint Profile
**Timestamp (UTC):** `2026-11-05T09:04:03.898562+00:00`  
**Target Process:** NOVA Controller (`scripts/run_inference_with_telemetry.py --guided-start`)  
**Operational Mode:** Lightweight Safe Mode (Model Not Loaded, Inference Gated)  
**Status:** MEASURED AND EMPIRICALLY AUDITED  

---
## 1. Executive Summary & Measurement Overview

To qualify NOVA Compute for controlled inference on memory-constrained systems (such as the target 16 GB laptop), NOVA's own memory footprint before loading model weights must be precisely accounted for. This document establishes empirical baseline measurements of the lightweight controller across four distinct lifecycle checkpoints:

- **Checkpoint A (Earliest Initialization)**: Captured immediately at the guided-start entrypoint prior to heavy module or coordinator setup.
- **Checkpoint B (Core Controller Initialization)**: Captured after `GuidedStartupController`, state machine, memory budget manager, and memory advisor are instantiated.
- **Checkpoint C (Idle Waiting State)**: Sampled across multiple quiescent intervals while awaiting user interaction in `LOW_MEMORY_WAIT` or `READY_FOR_AUTHORIZATION`.
- **Checkpoint D (Post-Recheck State)**: Sampled immediately following a full 6-gate admission evaluation.

---
## 2. Process Footprint Summary Table

| Metric | Checkpoint A (Earliest) | Checkpoint B (Core Init) | Startup Delta (B - A) | Checkpoint C (Idle Avg) | Checkpoint D (Post-Recheck) |
|---|---|---|---|---|---|
| **Process Working Set** | 20.10 MB | 25.80 MB | +5.70 MB | 26.30 MB | UNKNOWN |
| **Process Private Commit** | 13.50 MB | 17.90 MB | +4.40 MB | 17.90 MB | UNKNOWN |
| **Page Fault Count** | 5,296 | 27,647 | N/A (Cumulative) | 27,791 | UNKNOWN |
| **Process CPU Time** | 0.297 s | 0.297 s | N/A (Cumulative) | 0.297 s | UNKNOWN |
| **System Available RAM** | 9.07 GiB | 9.05 GiB | N/A (System-wide) | 9.06 GiB | UNKNOWN |
| **System Commit Ratio** | 35.8% | 35.8% | N/A (System-wide) | 35.7% | UNKNOWN |

---
## 3. Telemetry Interpretation & Working Set Mechanics

1. **Observed Startup Delta vs. Exclusive Attribution**:
   - Working Set Delta ($B - A$): `+5.70 MB`.
   - Private Commit Delta ($B - A$): `+4.40 MB`.
   - These deltas represent observed differences during Python runtime setup and initialization of Resource Steward modules. They reflect both NOVA's code path and standard CPython runtime allocations.
2. **Process Working Set vs. Private Commit**:
   - **Working Set**: The physical RAM pages mapped into the controller process's address space at measurement time (~26.30 MB initialized idle).
   - **Private Commit**: Dedicated virtual memory allocated exclusively by the process (via `VirtualAlloc` / `malloc`), backed by pagefile space (~17.90 MB).
3. **Controller Process Footprint vs. System-Wide Available RAM**:
   - Controller process resident usage (~26.30 MB working set, ~17.90 MB private commit) is strictly decoupled from system-wide available physical RAM (~9.06 GiB) and commit ratio (~35.7%).
   - A lightweight controller footprint does not imply that all remaining physical RAM is freely available for inference workloads; system available RAM is dynamically governed by Windows caching, kernel pools, background system services, and concurrent desktop applications.
4. **Idle Sampling Methodology & Observation Boundary**:
   - Checkpoint C (Idle Waiting State) was recorded across 3 consecutive quiescent samples spaced ~50–100 ms apart (~0.2 s total observation period).
   - This short idle sampling window establishes an empirical baseline footprint for the quiescent controller, but does NOT prove the absence of slow or long-term memory leaks over prolonged execution.
5. **Checkpoint D (Post-Recheck State) Status**:
   - Checkpoint D is marked as `UNKNOWN` because no interactive recheck cycle was executed during this automated capture run.
6. **Zero Model Memory Overhead**:
   - At all checkpoints prior to explicit authorization, model weights remain strictly **NOT LOADED**.
   - Zero VRAM and zero GGUF file mappings are active in the controller process.

---
## 4. Measurement Limitations and Uncertainty

- Telemetry queries Windows `GetProcessMemoryInfo` and `GlobalMemoryStatusEx` via `ctypes`.
- Windows memory management may page clean code pages or adjust working sets dynamically under external pressure.
- Numbers are recorded directly from kernel counters without heuristic manipulation or artificial inflation.
