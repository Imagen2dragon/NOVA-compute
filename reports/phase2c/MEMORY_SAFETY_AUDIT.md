# NOVA Compute — Phase 2C Memory Safety & Telemetry Audit

**Document ID:** `NOVA-AUD-P2C-MEM-01`  
**Date:** 2026-11-04  
**Author:** Antigravity (Implementation Engineer)  
**Governance:** Implementation submitted; **Awaiting Lead Architect (ChatGPT) & Project Owner Review**  
**Status:** AUDITED & REMEDIATED (FINAL STABILIZATION)  

---

## 1. Executive Summary

This audit addresses memory accounting ambiguities, Windows virtual memory mechanics, and execution harness vulnerabilities identified during the Lead Architect review.

Key stabilization actions completed:
1. **Hardened Preflight Commit Gate:** Evaluates commit limit, available commit, committed bytes, and commit percentage against contradictory, negative, NaN, infinite, and out-of-range values. Zero commit limits fail closed instead of showing 0%.
2. **Measurement Coverage Tracking:** Process memory accounting now tracks `instances_measured` vs `instances_total`. Inaccessible PIDs are explicitly reported as partial (`is_partial_private_commit = True`).
3. **Ordered Sample Queue & Monotonic Timer Reset:** The supervisor consumes every sample from an ordered queue, validates sequential sample IDs, detects gaps, and resets the low-RAM timer whenever a fresh sample reports available RAM $\ge 2.0\text{ GiB}$.
4. **Decoupled Production Supervisor:** The `supervise_child_process` engine is decoupled from harness execution and covered by 12 integrated automated tests using deterministic fake clocks and queues.
5. **Truthful Outcome & Recovery Verification:** `verify_inference_success` requires exit code 0, no safety abort, verified token generation markers, valid telemetry, and clean recovery verification.

---

## 2. Win32 Memory Accounting Audit

In Windows operating system internals, process memory is tracked via `PROCESS_MEMORY_COUNTERS_EX`. The table below defines the precise semantic meaning of each field:

| Win32 Struct Member | Correct Technical Meaning | Common Misconception | NOVA Implementation Usage |
| :--- | :--- | :--- | :--- |
| `WorkingSetSize` | **Resident Physical Working Set** — Pages of physical RAM currently mapped into the process's page table. | Total process memory usage. | Used to measure observed live RAM consumption. |
| `PeakWorkingSetSize` | **Peak Resident Working Set** — Maximum physical RAM the process held at any point during execution. | Total virtual address space. | Evaluates maximum memory spikes during model loading. |
| `PrivateUsage` | **Private Commit** — Memory allocated privately by the process that cannot be shared or backed by named files. | Disk footprint or identical to working set. | Measures runtime allocations (heap, KV cache buffers, CUDA driver buffers). Set to `None` if unmeasured. |
| `PagefileUsage` | **Process Commit Charge** — Total commit charge allocated to the process by the OS. | **Disk I/O to pagefile.sys** (INCORRECT). | Reflects committed memory against the system commit limit; does NOT represent disk writes. |
| `PageFaultCount` | **Total Page Faults** — Cumulative soft faults (resolved in RAM/file cache) plus hard faults (resolved from disk). | Unhandled memory exceptions or exclusively hard disk faults. | Diagnostic counter for virtual memory activity; soft faults are normal during mmap access. |

---

## 3. Windows PDH Performance Counters Integration

To observe physical disk paging activity that cannot be measured via process-specific APIs alone, `nova.resource_steward.monitor` interfaces directly with `ctypes.windll.pdh`:

### Instrumented Counters
- `\Memory\Pages Input/sec`: Number of pages read from disk to resolve hard page faults across the OS.
- `\Memory\Pages Output/sec`: Number of pages written to disk to free space in physical memory (pagefile write traffic).
- `\Memory\Page Reads/sec`: Number of read disk I/O requests issued to resolve page faults.
- `\Memory\Page Writes/sec`: Number of write disk I/O requests issued to write dirty pages to disk.
- `\PhysicalDisk(_Total)\Disk Read Bytes/sec` & `Disk Write Bytes/sec`: Cumulative disk transfer bandwidth.

---

## 4. Windows mmap and Prefetch Mechanics

For the Qwen3-30B-A3B-Q4_K_M model under Configuration D:
- **Total Model File:** 17.35 GiB (18,632,184,480 bytes).
- **GPU-Offloaded Tensors:** 48 dense layers + 8 expert layers ($\approx 4,286\text{ MiB}$ VRAM).
- **CPU-Resident Tensors:** 40 expert layers ($\approx 13.48\text{ GiB}$ mapped file data).
- **Active Expert Footprint:** $\approx 862.6\text{ MiB}$ per token (8 active experts out of 128 across 40 layers).

### How Windows Handles mmap Model Files
1. **Virtual Address Space Mapping:** `llama.cpp` maps the GGUF file using `CreateFileMappingW` and `MapViewOfFile`. This reserves address space in virtual memory; it does **not** commit physical RAM.
2. **Advisory Prefetch (`PrefetchVirtualMemory`):** On model initialization with mmap enabled, `llama.cpp` invokes `PrefetchVirtualMemory` across mapped tensor ranges. Under memory pressure, Windows **partially satisfies or ignores** this request. It does **not** fail model loading or cause out-of-memory errors.
3. **Clean Page Reclaimability:** Because the GGUF is opened read-only, all mapped pages are clean. If the system experiences memory pressure, Windows can immediately discard clean file-backed pages from RAM without writing anything to `pagefile.sys`.

---

## 5. Execution Harness Safety Remediations

The execution harness CLI in `scripts/run_inference_with_telemetry.py` has been stabilized:

1. **Mutually Exclusive Arguments:**
   - `--execute`, `--preflight`, `--dry-run`, and `--memory-report` are mutually exclusive. Default invocation strictly defaults to **read-only preflight**.
2. **Path Anchoring:**
   - All runtime binary, model GGUF, and output report paths are anchored to `PROJECT_ROOT`.
3. **Ordered Sample Queue & Decoupled Supervisor:**
   - The supervisor reads from `sample_queue`, detects gaps, verifies monotonic timestamps, and resets low-RAM timers upon RAM recovery.
4. **Explicit 8-Stage Teardown Lifecycle:**
   - Abort triggers child shutdown before pipe closure. Reader and sampler threads are joined with timeouts and verified stopped.
5. **Strict Inference Outcome Verification:**
   - `verify_inference_success` requires proof of generated tokens, telemetry validity, and verified recovery. Ambiguous outputs are marked `UNVERIFIED`.
