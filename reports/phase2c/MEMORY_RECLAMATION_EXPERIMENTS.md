# NOVA Compute — Phase 2C-B: Memory Reclamation Experiments & Empirical Telemetry

**Document:** Empirical Reclamation Telemetry & Analysis  
**Reference Run:** Live Preflight Qualification Run  
**Machine:** RTX 4050 Laptop GPU (6 GB VRAM) | 16 GB Physical RAM | Windows 11  
**Status:** REVIEW REMEDIATION BASELINE

---

## 1. Objective of Empirical Evaluation

The purpose of this evaluation is to verify the live behavior of the Adaptive Memory Reclaimer (AMR) on the physical host machine and demonstrate that:
1. AMR correctly identifies system memory pressure tiers under real workstation conditions.
2. The `SAFE_AUTO` reclamation routine runs cleanly without causing host instability, application disruption, or permission exceptions.
3. System memory accounting is accurate, truthful, and strictly avoids artificial reporting.
4. When external workstation applications (e.g., Edge browser, IDE) consume available RAM, NOVA truthfully reports that internal reclamation yields no significant improvement and halts before launching inference.

---

## 2. Memory Accounting Architecture: Dual-Layer Verification

In accordance with architectural review findings, AMR uses a **dual-layer accounting model**:

1. **System-Wide Observed Metric (`observed_system_avail_delta_bytes`)**:
   - Computed directly from `avail_phys_bytes` before and after reclamation.
   - Represents the gross change in Windows available physical RAM.
   - **Crucial Engineering Reality:** In a multi-tasking desktop OS, system available RAM fluctuates dynamically due to background Windows Defender, browser workers, and disk cache flushing. Thus, an increase in system available RAM is strictly recorded as an *observational system metric*, **not** credited to NOVA.

2. **Process-Local Causal Metric (`nova_process_working_set_delta_bytes` & `nova_process_private_commit_delta_bytes`)**:
   - Sampled directly for the NOVA runtime PID via `GetProcessMemoryInfo`.
   - Captures the exact resident working set and private commit before and after `gc.collect(2)`.
   - **Qualification Rule:** `RECLAMATION_COMPLETED` is reported **if and only if** both pre- and post-snapshots succeed with PID verification, and NOVA's process private commit decreases by $\ge 1\text{ MiB}$ ($1,048,576\text{ bytes}$).
   - **Working Set Semantics:** Working set measures physical memory residency (which Windows can shift independently). Working set reduction alone NEVER qualifies as `RECLAMATION_COMPLETED`.
   - **Unverified & No Improvement Semantics:** If either process snapshot fails or is malformed, measured deltas are set to `None` and status is reported as `RECLAMATION_UNVERIFIED`. If private commit decrease is $< 1\text{ MiB}$, status is reported truthfully as `NO_MEASURABLE_IMPROVEMENT`. Concurrent process activity is acknowledged, and no exclusive causality is claimed.

---

## 3. Live Empirical Telemetry

During live preflight execution on the host machine, the following data was captured by `AdaptiveMemoryController`:

### 3.1 Telemetry Snapshot

| Telemetry Metric | Measured Value | Unit / Status |
| :--- | :--- | :--- |
| **Trigger Reason** | `Preflight admission check` | Structured Trigger |
| **Detected Pressure Level** | `CONSTRAINED` | RAM < 10.0 GiB |
| **System Avail RAM (Pre)** | `9.738` | GiB |
| **System Avail RAM (Post)** | `9.738` | GiB |
| **Observed System Delta** | `~0` | Bytes |
| **NOVA Process Working Set** | `~48.2` | MiB |
| **NOVA Working Set Delta** | `< 0.2` | MiB |
| **System Commit (Pre / Post)** | `33.5% / 33.5%` | Stable |
| **Actions Attempted** | `["python_gc_collect"]` | Scoped to Runtime |
| **Actions Completed** | `["python_gc_collect (freed 3 unreachable objects)"]` | Clean Execution |
| **Actions Failed** | `[]` (None) | 0 Errors |
| **Reclamation Execution Time** | `< 0.001` | Seconds (< 1 ms) |
| **Cooldown Applied** | `false` | Fresh evaluation |
| **Overall Status** | `NO_MEASURABLE_IMPROVEMENT` | Truthful Accounting |

---

## 4. Analysis of Host Memory Distribution & Deficit

During the live preflight check, the hardware admission gates evaluated as follows:
- **Available RAM**: $9.738\text{ GiB}$ (Required: $\ge 10.0\text{ GiB}$) $\rightarrow$ **FAIL (Deficit: $0.262\text{ GiB}$)**
- **Available VRAM**: $5920.0\text{ MiB}$ (Required: $\ge 5500.0\text{ MiB}$) $\rightarrow$ **PASS**
- **System Commit**: $33.5\%$ (Required: $< 90.0\%$) $\rightarrow$ **PASS**
- **Competing GPU Processes**: $0$ (Required: $0$) $\rightarrow$ **PASS**
- **Disk Free**: $118.21\text{ GB}$ (Required: $\ge 25.0\text{ GB}$) $\rightarrow$ **PASS**

### 4.1 Host Process Memory Attribution

The Process Working Set Inventory captured the primary contributors to host RAM consumption:

```
Process Name                     Working Set (MB)    Private Commit (MB)   Category
-----------------------------------------------------------------------------------------
msedge.exe (26 instances)            ~1,396.0 MB         ~2,942.0 MB       User Application
antigravity.exe (14 instances)         ~466.0 MB         ~1,070.0 MB       User Application
language_server.exe (1 instance)       ~281.0 MB           ~369.0 MB       User Application
svchost.exe (47 instances)             ~240.0 MB            ~41.0 MB       System/Background
```

### 4.2 Engineering Insight & Root Cause

1. **Why `python_gc_collect` Yields No Measurable Global Improvement**:
   - The NOVA runtime process itself is minimal (holding only ~48 MB of memory). Running Python GC frees runtime garbage objects within NOVA, but cannot release memory held by third-party workstation applications (Edge, IDE, OS services).
   - The result `NO_MEASURABLE_IMPROVEMENT` is mathematically accurate and reflects the reality of a multi-tasking desktop.
2. **Why Aggressive Cleaners (Mem Reduct / WinMemClean) Create Illusionary Headroom**:
   - Utilities that call `EmptyWorkingSet` on `msedge.exe` or `antigravity.exe` force their active working sets out to the pagefile. While the Windows "Available RAM" metric temporarily rises, the moment the user interacts with Edge or the IDE, the OS experiences massive page faults, causing mouse stuttering, tab freezes, and severe disk paging.
   - Utilities that purge the standby list (`NtSetSystemInformation`) invalidate cached disk blocks, which destroys mmap efficiency for local LLM inference.
3. **Strict Safety Compliance**:
   - Because NOVA honors user sovereignty, it refused to evict user applications, truthfully reported the $0.262\text{ GiB}$ deficit, and **halted at preflight with exit code 1**.
   - No inference process was spawned. The machine remained 100% stable.
