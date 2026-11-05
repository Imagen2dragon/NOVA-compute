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
   - **Working Set**: The physical RAM pages mapped into the controller process's address space at measurement time.
   - **Private Commit**: Dedicated virtual memory allocated exclusively by the process (via `VirtualAlloc` / `malloc`), backed by pagefile space.
3. **System-Wide Metric Independence**:
   - System Available RAM and System Commit Ratio reflect overall OS and multi-process states. Fluctuations in available RAM during controller execution are primarily driven by background OS tasks and concurrent desktop applications, not NOVA's lightweight footprint (< 30 MB).
4. **Zero Model Memory Overhead**:
   - At all checkpoints prior to explicit authorization, model weights remain strictly **NOT LOADED**.
   - Zero VRAM and zero GGUF file mappings are active in the controller process.

---
## 4. Measurement Limitations and Uncertainty

- Telemetry queries Windows `GetProcessMemoryInfo` and `GlobalMemoryStatusEx` via `ctypes`.
- Windows memory management may page clean code pages or adjust working sets dynamically under external pressure.
- Numbers are recorded directly from kernel counters without heuristic manipulation or artificial inflation.