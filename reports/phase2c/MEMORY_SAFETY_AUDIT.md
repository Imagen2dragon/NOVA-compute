# NOVA Compute — Phase 2C Memory Safety & Telemetry Audit

**Document ID:** `NOVA-AUD-P2C-MEM-01`  
**Date:** 2026-11-04  
**Author:** Antigravity (Implementation Engineer)  
**Governance:** Implementation submitted; **Awaiting Lead Architect (ChatGPT) & Project Owner Review**  
**Status:** AUDITED & REMEDIATED (FOLLOW-UP REMEDIATION)  

---

## 1. Executive Summary

This audit addresses critical memory accounting ambiguities, Windows virtual memory mechanics, and execution harness vulnerabilities identified during Phase 2C-B follow-up review.

Key remediation actions completed:
1. **Disambiguated Win32 Memory Metrics:** Replaced misleading descriptions of `PROCESS_MEMORY_COUNTERS_EX`. Working set is strictly distinguished from private commit. When private commit cannot be queried, it is recorded as `None` / unavailable, never populated with working set figures.
2. **Added Windows PDH Subsystem Instrumentation:** Integrated native Windows Performance Data Helper counters to track system page input/output rates and physical disk throughput.
3. **Preflight System Commit Gate:** Added an explicit preflight gate checking that system commit usage remains below 90% of the commit limit.
4. **Asynchronous Output Supervision:** Removed blocking `readline()` from the supervisor loop, reading child stdout into a thread-safe queue.
5. **Monotonic Timestamp Tracking:** Replaced loop iteration counting with monotonic timestamp tracking (`time.monotonic()`) and sample ID deduplication for the 5-second critical RAM abort rule.
6. **Hardened GPU CSV Parsing:** Implemented strict CSV validation for `nvidia-smi` queries. Malformed rows now fail closed instead of being discarded as empty workloads.

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

### Technical Scope & Limitations
- **System-Wide Metric:** PDH memory counters reflect operating-system-wide activity, including background Windows services and active browsers, not exclusively the NOVA child process.
- **Differential Calculation:** Counters measure rate per second; the sampler computes differences between consecutive sampling intervals.

---

## 4. Windows mmap and Prefetch Mechanics

For the Qwen3-30B-A3B-Q4_K_M model under Configuration D:
- **Total Model File:** 17.35 GiB (18,632,184,480 bytes).
- **GPU-Offloaded Tensors:** 48 dense layers + 8 expert layers ($\approx 4,286\text{ MiB}$ VRAM).
- **CPU-Resident Tensors:** 40 expert layers ($\approx 13.48\text{ GiB}$ mapped file data).
- **Active Expert Footprint:** $\approx 862.6\text{ MiB}$ per token (8 active experts out of 128 across 40 layers).

### How Windows Handles mmap Model Files
1. **Virtual Address Space Mapping:** `llama.cpp` maps the GGUF file using `CreateFileMappingW` and `MapViewOfFile`. This reserves address space in virtual memory; it does **not** commit physical RAM.
2. **Advisory Prefetch (`PrefetchVirtualMemory`):** On model initialization with mmap enabled, `llama.cpp` invokes `PrefetchVirtualMemory` across mapped tensor ranges.
   - This call is an **advisory hint** to the Windows Cache Manager.
   - It requests Windows to bring pages into the standby list / working set.
   - Under memory pressure, Windows **partially satisfies or ignores** this request. It does **not** fail model loading or cause out-of-memory errors.
3. **Clean Page Reclaimability:** Because the GGUF is opened read-only, all mapped pages are clean. If the system experiences memory pressure, Windows can immediately discard clean file-backed pages from RAM without writing anything to `pagefile.sys`. When needed again, the CPU re-reads the page from the NVMe SSD via standard soft/hard faults.

---

## 5. Execution Harness Safety Remediations

The execution harness CLI in `scripts/run_inference_with_telemetry.py` has been completely hardened:

1. **Mutually Exclusive Arguments:**
   - `--execute`, `--preflight`, `--dry-run`, and `--memory-report` are mutually exclusive.
   - Default invocation (no arguments) defaults strictly to **read-only preflight**.
2. **Path Anchoring:**
   - All runtime binary, model GGUF, and output report paths are anchored to `PROJECT_ROOT`, preventing path resolution failures when called from different directories.
3. **Fail-Closed Gate Enforcement:**
   - Sensor failures (`FAILED` or `UNKNOWN`) immediately reject execution.
   - Added preflight system commit gate: fails if commit load $\ge 90\%$.
4. **Asynchronous Process Supervision:**
   - Uses a background thread to drain `proc.stdout` into a non-blocking queue.
   - Supervisor continuously evaluates resource metrics without blocking on quiet child output.
5. **Monotonic Abort Timers:**
   - Evaluates sustained low-RAM conditions using monotonic clock durations (`time.monotonic()`), preventing loop iteration counting errors.
   - Drops duplicate samples via `sample_id` tracking.
   - Halts execution if telemetry becomes stale (> 3.0s).
6. **Windows Process Group & Control Break:**
   - Spawns in `CREATE_NEW_PROCESS_GROUP | BELOW_NORMAL_PRIORITY_CLASS`.
   - Sends `CTRL_BREAK_EVENT` for graceful console shutdown, escalating to `kill()` only on timeout.
