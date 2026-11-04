# NOVA Compute — Phase 2C-B: Memory Reclamation Experiments & Empirical Telemetry

**Document:** Empirical Reclamation Telemetry & Analysis  
**Reference Run:** Live Preflight Qualification Run  
**Timestamp (UTC):** 2026-11-04T10:48:50.912215+00:00  
**Machine:** RTX 4050 Laptop GPU (6 GB VRAM) | 16 GB Physical RAM | Windows 11

---

## 1. Objective of Empirical Evaluation

The purpose of this evaluation is to verify the live behavior of the Adaptive Memory Reclaimer (AMR) on the physical host machine and demonstrate that:
1. AMR correctly identifies system memory pressure tiers under real workstation conditions.
2. The `SAFE_AUTO` reclamation routine runs cleanly without causing host instability, application disruption, or permission exceptions.
3. System memory accounting is accurate, truthful, and strictly avoids artificial reporting.
4. When external workstation applications (e.g., Edge browser, IDE) consume available RAM, NOVA truthfully reports that internal reclamation yields no significant improvement and halts before launching inference.

---

## 2. Live Empirical Telemetry

The following data was captured directly by `AdaptiveMemoryController` during preflight execution and saved to `reports/phase2c/memory_reclamation/reclaim_latest.json`:

### 2.1 Reclamation Telemetry Snapshot

| Telemetry Metric | Measured Value | Unit / Status |
| :--- | :--- | :--- |
| **Trigger Reason** | `Preflight admission check` | Structured Trigger |
| **Detected Pressure Level** | `CONSTRAINED` | RAM < 10.0 GiB |
| **Available RAM (Pre-Reclamation)** | `9.285` | GiB |
| **Available RAM (Post-Reclamation)** | `9.267` | GiB |
| **Net RAM Delta ($\Delta_{\text{RAM}}$)** | `-0.018` | GiB (-18.4 MB) |
| **System Commit (Pre / Post)** | `33.1% / 33.1%` | Stable |
| **Actions Attempted** | `["python_gc_collect"]` | Scoped to Runtime |
| **Actions Completed** | `["python_gc_collect (freed 3 unreachable objects)"]` | Executed Cleanly |
| **Actions Failed** | `[]` (None) | 0 Errors |
| **Reclamation Execution Time** | `0.000` | Seconds (< 1 ms) |
| **Cooldown Applied** | `false` | Fresh evaluation |
| **Paging Input / Output Rate** | `0.0 / 0.0` | Pages / second |
| **Overall Status** | `NO_MEASURABLE_IMPROVEMENT` | Truthful Accounting |

---

## 3. Analysis of Host Memory Distribution & Deficit

During the live preflight check, the hardware admission gates evaluated as follows:
- **Available RAM**: $9.267\text{ GiB}$ (Required: $\ge 10.0\text{ GiB}$) $\rightarrow$ **FAIL (Deficit: $0.733\text{ GiB}$)**
- **Available VRAM**: $5920.0\text{ MiB}$ (Required: $\ge 5500.0\text{ MiB}$) $\rightarrow$ **PASS**
- **System Commit**: $33.1\%$ (Required: $< 90.0\%$) $\rightarrow$ **PASS**
- **Competing GPU Processes**: $0$ (Required: $0$) $\rightarrow$ **PASS**
- **Disk Free**: $118.43\text{ GB}$ (Required: $\ge 25.0\text{ GB}$) $\rightarrow$ **PASS**

### 3.1 Host Process Memory Attribution

The Process Working Set Inventory captured the primary contributors to host RAM consumption:

```
Process Name                     Working Set (MB)    Private Commit (MB)   Category
-----------------------------------------------------------------------------------------
msedge.exe (26 instances)            ~1,666.0 MB         ~2,774.0 MB       User Application
antigravity.exe (14 instances)         ~568.0 MB         ~1,040.0 MB       User Application
svchost.exe (48 instances)             ~387.0 MB            ~42.0 MB       System/Background
language_server.exe (1 instance)       ~292.0 MB           ~367.0 MB       User Application
msmpeng.exe (1 instance)               ~187.0 MB                 N/A       System/Security
```

### 3.2 Engineering Insight & Root Cause

1. **Why `python_gc_collect` Yields No Measurable Global Improvement**:
   - The NOVA runtime process itself is minimal (holding only ~50 MB of memory). Running Python GC frees runtime garbage objects within NOVA, but cannot release memory held by third-party workstation applications (Edge, IDE, OS services).
   - The result `NO_MEASURABLE_IMPROVEMENT` is mathematically accurate and reflects the reality of a multi-tasking desktop.
2. **Why Aggressive Cleaners (Mem Reduct / WinMemClean) Create Illusionary Headroom**:
   - Utilities that call `EmptyWorkingSet` on `msedge.exe` or `antigravity.exe` force their active working sets out to the pagefile. While the Windows "Available RAM" metric temporarily rises, the moment the user interacts with Edge or the IDE, the OS experiences massive page faults, causing mouse stuttering, tab freezes, and severe disk paging.
   - Utilities that purge the standby list (`NtSetSystemInformation`) invalidate cached disk blocks, which destroys mmap efficiency for local LLM inference.
3. **Strict Safety Compliance**:
   - Because NOVA honors user sovereignty, it refused to evict user applications, truthfully reported the $0.733\text{ GiB}$ deficit, and **halted at preflight with exit code 1**.
   - No inference process was spawned. The machine remained 100% stable.
