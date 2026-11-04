# NOVA Compute — Phase 2C-B: Adaptive Memory Reclaimer (AMR) Architecture Design

**Reference:** Phase 2C-B Final Ownership & Action Integrity Fix  
**Author:** Antigravity (Implementation Engineer)  
**Governance:** User (Project Owner) & ChatGPT (Lead Architect / Technical Reviewer)  
**Status:** IMPLEMENTED & VERIFIED (Software Only — Model Inference Not Launched)

---

## 1. Executive Summary & Core Objective

The **NOVA Adaptive Memory Reclaimer (AMR)** is an intelligent, resource-aware memory management subsystem embedded within NOVA's Resource Steward architecture (`nova/resource_steward/adaptive_memory.py`).

Its primary design objective is to enable NOVA to monitor memory pressure tiers proactively, throttle reclamation operations, safely reclaim unused process-local runtime allocations, and provide verifiable before/after accounting—**without endangering Windows host stability, evicting user applications, or purging critical operating system standby caches**.

---

## 2. Open-Source Architectural Study & Comparative Analysis

To design AMR rigorously, we analyzed two prominent open-source Windows memory management utilities:
1. **Mem Reduct** (`https://github.com/henrypp/memreduct`) by Henry++
2. **WinMemClean** (`https://github.com/nusu-github/WinMemClean`) by nusu-github

### 2.1 Patterns Analyzed

Both utilities operate by issuing Windows native API calls to forcefully reduce working sets and flush system-wide memory lists:
- **`EmptyWorkingSet` (`K32EmptyWorkingSet`)**: Iterates through all accessible process IDs on the machine and requests the NT memory manager to trim the process working set down to zero.
- **`NtSetSystemInformation` with `SystemFileCacheInformation`**:
  - `MemoryPurgeStandbyList` (Command 4): Purges the OS standby list (cached files, executables, mmap pages).
  - `MemoryFlushModifiedList` (Command 3): Flushes dirty file-backed pages to storage.
  - `MemoryEmptyWorkingSets` (Command 2): Trims working sets across all user and system processes.
- **`SetSystemFileCacheSize`**: Shrinks the file system cache working set.

### 2.2 Comparative Decision Matrix: Adopt vs. Reject

| Technique / Feature | Mem Reduct / WinMemClean Approach | NOVA Compute AMR Implementation | Engineering Rationale |
| :--- | :--- | :--- | :--- |
| **Working Set Trimming (`EmptyWorkingSet`)** | Iterates over ALL system and user processes (`OpenProcess(PROCESS_SET_QUOTA)`) | **STRICTLY FORBIDDEN** (Static AST verified across `nova/` and `scripts/`) | Causes massive hard-page-fault storms across active user applications (e.g., Edge, VS Code, IDE), freezing the desktop and harming user experience. |
| **System Standby List Purging (`NtSetSystemInformation(4)`)** | Clears standby pages to inflate "Available RAM" metric | **STRICTLY FORBIDDEN** (Static AST verified across `nova/` and `scripts/`) | In SSD/mmap-backed MoE inference, model weights reside in the standby page cache! Purging standby destroys model cache locality and kills token decode speed. |
| **Modified Page List Flushing (`NtSetSystemInformation(3)`)** | Flushes dirty pages to disk | **STRICTLY FORBIDDEN** | Forces unnecessary NVMe disk write bursts, increasing disk latency. |
| **System Privileges (`SeProfileSingleProcessPrivilege` / `SeIncreaseQuotaPrivilege`)** | Requires Administrator / elevated token | **UNPRIVILEGED USER-SPACE ONLY** | NOVA must never require elevated privileges to run safely on workstation laptops. |
| **Multi-Tier Pressure Monitoring** | Monitored via percentage threshold triggers | **ADOPTED & ADAPTED** | Hierarchical operational pressure tiers based on physical GiB and commit ratio, reusing primary policy fields (`preferred_inference_ram_gib`, `minimum_inference_ram_gib`, `critical_ram_gib`). |
| **Cooldown Throttling** | Timer-based periodic cleanups | **ADOPTED** | Enforces a strict 30-second cooldown to prevent CPU and allocator thrashing. |
| **Before / After Delta Verification** | Computes freed memory percentage | **ADOPTED & HARDENED** | Dual-layer accounting: captures exact `avail_phys_bytes` system delta as an observational metric, while tracking process-local private commit and working set deltas. |

---

## 3. The `SAFE_AUTO` Process-Local Boundary

AMR operates exclusively under the **`SAFE_AUTO`** execution policy. The operational boundary of `SAFE_AUTO` is strictly limited to NOVA's own process-local memory:

1. **Python Generational Garbage Collection**:
   - Executes `gc.collect(2)` to reclaim cyclical references and orphaned telemetry/diagnostics objects across all Python runtime generations.
2. **Strictly Process-Local Ownership**:
   - `SafeMemoryReclaimer` does not invoke or register arbitrary cleaners. It is bounded solely to NOVA process runtime memory.
   - Production operations exclusively query `os.getpid()` (via a constructor `pid_provider` defaulting to `os.getpid`).
   - Callers cannot pass arbitrary target PIDs to `evaluate_and_reclaim()`, preventing accidental or malicious targeting of foreign processes.
   - External application memory, system caches, and file standby lists are completely untouched.
3. **Zero Host Side-Effects**:
   - Never calls `OpenProcess` with modifying rights against non-NOVA PIDs.
   - Never terminates or suspends external applications.
   - Never requests NT kernel cache purges.

---

## 4. Multi-Tier Memory Pressure Classification & Input Validation

The `MemoryPressureDetector` categorizes system memory into four deterministic operational tiers with strict fail-closed input validation:

```mermaid
flowchart TD
    A["System Memory Telemetry"] --> B{"Status == SUCCESS?"}
    B -- No --> C["CRITICAL Tier (Fail-Closed)"]
    B -- Yes --> VAL{"Input Valid?\n(non-null, non-bool, finite, in bounds)"}
    VAL -- No --> C
    VAL -- Yes --> D{"Avail RAM < 2.0 GiB OR Commit >= 90%?"}
    D -- Yes --> C
    D -- No --> E{"Avail RAM < 10.0 GiB?"}
    E -- Yes --> F["CONSTRAINED Tier"]
    E -- No --> G{"Avail RAM < 12.0 GiB?"}
    G -- Yes --> H["OPTIMIZATION Tier"]
    G -- No --> I["NORMAL Tier (>= 12.0 GiB)"]
```

### Input Validation
If `avail_phys_gib`, `avail_phys_bytes`, or `commit_pct` is missing (`None`), non-numeric, boolean, non-finite (`NaN`, `inf`, `-inf`), negative, or `commit_pct > 100.0`, `evaluate_pressure` treats this as a sensor anomaly and returns `CRITICAL` (fail-closed).

### Operational Tiers
1. **NORMAL Tier ($\ge 12.0\text{ GiB}$)**:
   - System memory is abundant. Inference can execute with comfortable workspace reserves.
   - Action: `NO_ACTION_REQUIRED`. Reclamation bypassed.
2. **OPTIMIZATION Tier ($10.0\text{ GiB} \le \text{RAM} < 12.0\text{ GiB}$)**:
   - Hardware satisfies minimum inference requirements ($10.0\text{ GiB}$), but workspace headroom is constrained.
   - Action: Executes `SAFE_AUTO` reclamation pass to prune process-local runtime overhead.
3. **CONSTRAINED Tier ($2.0\text{ GiB} \le \text{RAM} < 10.0\text{ GiB}$)**:
   - System memory is below the minimum threshold required for Configuration D ($10.0\text{ GiB}$).
   - Action: Executes `SAFE_AUTO` pass. If post-reclamation RAM remains below $10.0\text{ GiB}$, the preflight gate **fails closed**, completely gating inference launch.
4. **CRITICAL Tier ($< 2.0\text{ GiB}$ or Commit $\ge 90.0\%$)**:
   - Machine is under severe paging or commit exhaustion risk.
   - Action: Immediate preflight rejection or live supervisor abort.

---

## 5. Cooldown Management & Thrashing Prevention

The `ReclamationCooldownManager` enforces a configurable cooldown window (default: $30.0\text{ seconds}$):
- Uses monotonic clock tracking (`clock()`).
- Guarantees that repetitive diagnostic passes do not cause allocator churn.
- Reports `cooldown_applied = True` and `overall_status = NO_ACTION_REQUIRED` when invoked during an active cooldown.
- Allows explicit override only when `force=True` is provided (e.g., during critical preflight checks).

---

## 6. Verifiable Telemetry & Delta Accounting

Every AMR execution follows a strict transactional flow:
1. **Pre-reclamation snapshot**:
   - Captures system memory via `monitor.get_system_memory()` (`system_avail_bytes_before = avail_phys_bytes`).
   - Captures NOVA process-local memory via `monitor.get_process_memory(os.getpid())` (`nova_process_working_set_bytes_before`, `nova_process_private_commit_bytes_before`).
2. **Reclamation invocation**:
   - `SafeMemoryReclaimer.reclaim()` executes `gc.collect(2)` and measures execution duration in seconds.
3. **Post-reclamation snapshot**:
   - Captures fresh system memory (`system_avail_bytes_after = avail_phys_bytes`).
   - Captures fresh process-local memory (`nova_process_working_set_bytes_after`, `nova_process_private_commit_bytes_after`).
4. **Validation Before Delta Calculation**:
   - Pre-snapshot must exist, have `status == TelemetryStatus.SUCCESS`, match expected PID, and have non-zero non-boolean byte counters.
   - Post-snapshot must exist, have `status == TelemetryStatus.SUCCESS`, match expected PID, and have non-zero non-boolean byte counters.
   - If either measurement fails, is malformed, or PID mismatches:
     - `nova_process_working_set_delta_bytes = None`
     - `nova_process_private_delta_bytes = None`
     - `process_telemetry_verified = False`
     - `overall_status = RECLAMATION_UNVERIFIED`
     *(Zero default fields from failed snapshots are never interpreted as valid measurements).*
5. **Action Integrity & Status Semantics**:
   - **Working Set Delta**: $\text{ws}_{\text{pre}} - \text{ws}_{\text{post}}$ is retained for diagnostics. Working set measures physical memory residency; it can decline because Windows trims working sets or pages memory, so **working set reduction alone NEVER classifies `RECLAMATION_COMPLETED`**.
   - **Action Success Required**: Completion strictly requires internal reclamation action success (`len(failed) == 0 and len(completed) > 0`). If `gc.collect(2)` fails or encounters an exception, it is recorded in `actions_failed` and reports `RECLAMATION_UNVERIFIED` even if private commit dropped due to concurrent factors.
   - **Private Commit Delta**: $\text{priv}_{\text{pre}} - \text{priv}_{\text{post}}$ is the required causal indicator.
   - If action succeeded and $\Delta_{\text{priv}} \ge 1\text{ MiB}$ ($1,048,576\text{ bytes}$) with both snapshots verified:
     `overall_status = RECLAMATION_COMPLETED`.
   - If action succeeded but $\Delta_{\text{priv}} < 1\text{ MiB}$:
     `overall_status = NO_MEASURABLE_IMPROVEMENT`.
   - **Explicit Verification Flags**: `MemoryReclaimReport` records distinct boolean flags:
     - `process_working_set_verified`: True if working-set measurement succeeded and matches PID.
     - `process_private_commit_verified`: True if private-commit counter succeeded and matches PID.
     - `process_telemetry_verified`: True only if BOTH working-set and private-commit measurements are verified. In normal and cooldown paths, `process_telemetry_verified` requires both counters to be verified.
6. **Atomic Audit Logging**:
   - Every outcome (`RECLAMATION_COMPLETED`, `NO_ACTION_REQUIRED`, `NO_MEASURABLE_IMPROVEMENT`, `RECLAMATION_FAILED`, `RECLAMATION_UNVERIFIED`) writes `MemoryReclaimReport` atomically to disk using a temporary `.tmp` file, `fsync()`, and `os.replace()`.
   - Respects `NOVA_AMR_REPORT_DIR` environment variable to ensure test runs do not pollute tracked project reports.
