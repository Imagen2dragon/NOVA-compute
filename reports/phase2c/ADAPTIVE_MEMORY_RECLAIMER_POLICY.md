# NOVA Compute — Phase 2C-B: Adaptive Memory Reclaimer (AMR) Governance Policy

**Document:** AMR Operational Policy & Safety Rules  
**Status:** ACTIVE SPECIFICATION (Final Ownership & Action Integrity Baseline)  
**Enforcement:** Programmatic (Static AST Analysis & Preflight Gating)

---

## 1. Core Principles

1. **User Ownership & Workspace Integrity**:
   - The user's machine belongs to the user. NOVA is an unprivileged guest application.
   - NOVA must NEVER optimize its own memory headroom by degrading the responsiveness of active user applications (e.g., Microsoft Edge, Visual Studio Code, Antigravity IDE).
2. **Deterministic Fail-Closed Gating**:
   - If available physical memory remains below the minimum safe requirement ($10.0\text{ GiB}$) after `SAFE_AUTO` reclamation, inference **MUST BE GATED**.
   - AMR is not an excuse to launch when memory is insufficient.
3. **Zero Standby Cache Sabotage**:
   - In SSD/mmap-backed MoE execution, Windows file cache (standby list) is critical to performance. Purging the standby list to make "Free RAM" look higher is an anti-pattern that slows down model execution by orders of magnitude.
4. **Process-Local Scope Boundary & Ownership**:
   - Current AMR operational scope is strictly confined to NOVA's process-local allocations via generational garbage collection (`gc.collect(2)`).
   - AMR exclusively targets NOVA's own runtime PID (`os.getpid()`). Arbitrary process ID redirection is forbidden.
   - Future architectural expansions may include internal tensor cache flushes (e.g., intermediate KV cache pools or expert weight cache manager buffers once implemented in subsequent phases). External application memory or system memory lists will remain strictly forbidden.
5. **Action Integrity**:
   - `RECLAMATION_COMPLETED` requires both that internal cleanup actions succeeded without failure and that private commit dropped by at least `min_reclamation_delta_mib`. Failed actions report `RECLAMATION_UNVERIFIED`.

---

## 2. Permitted Actions vs. Strictly Forbidden Actions

### 2.1 Permitted Actions (`SAFE_AUTO`)

| Action | Target | Mechanism | Safety Guarantee |
| :--- | :--- | :--- | :--- |
| **Python GC Collection** | NOVA Runtime | `gc.collect(2)` | Fully safe; frees unreachable cyclical objects within NOVA process. |
| **Process-Local Memory Tracking** | NOVA PID | `GetProcessMemoryInfo` | Read-only inspection of working set and private commit. |
| **Preflight Fresh Measurement** | Host Sensors | `GlobalMemoryStatusEx`, `nvmlDeviceGetMemoryInfo` | Read-only; zero host modification. |
| **Atomic Audit Logging** | Local Filesystem | Atomic JSON write (`.tmp` + `fsync` + `replace`) | Zero partial writes; local only; zero telemetry upload. |

### 2.2 Strictly Forbidden Actions (Absolute Prohibitions)

The following APIs, techniques, and behaviors are **strictly prohibited** in NOVA Compute and verified via static AST analysis:

1. **`EmptyWorkingSet` / `K32EmptyWorkingSet`**:
   - Prohibited across any third-party or system process.
   - Prohibited across all files in `nova/` and `scripts/`.
2. **`NtSetSystemInformation`**:
   - Class `SystemFileCacheInformation` (21)
   - Command `MemoryPurgeStandbyList` (4)
   - Command `MemoryFlushModifiedList` (3)
   - Command `MemoryEmptyWorkingSets` (2)
3. **`SetSystemFileCacheSize`**:
   - Prohibited. Modifying system cache quotas disrupts Windows I/O management.
4. **Foreign Process Termination**:
   - Prohibited. NOVA never calls `taskkill`, `TerminateProcess`, or `SIGKILL` against any PID that was not directly spawned by NOVA.
5. **System Configuration Modifications**:
   - Prohibited. NOVA never alters Windows pagefile size, registry keys, driver parameters, or power plans.
6. **Administrator Elevation**:
   - Prohibited. NOVA runs with standard user tokens and never requests UAC elevation.

---

## 3. Admission Control & Preflight Interaction

AMR integrates directly into the admission control phase of `ResourceSteward.run_preflight()`:

```
[User Request / CLI]
        │
        ▼
[AMR evaluate_and_reclaim()]
        │
        ├─► Checks RAM Pressure (e.g., CONSTRAINED at 9.27 GiB)
        ├─► Evaluates process-local gc.collect(2)
        └─► Logs atomic audit report (reclaim_latest.json)
        │
        ▼
[Fresh Hardware Sampling]
        │
        ▼
[Preflight Gates Evaluation]
        │
        ├─► Is Avail RAM >= 10.0 GiB?
        │     ├─► YES: Preflight Passes -> Inference Permitted
        │     └─► NO (e.g., 9.27 GiB):
        │           [FAIL GATE] -> INFERENCE HALTED (Exit Code 1)
        │           Generates actionable user guidance
        │           Halts without spawning child process
```

**Rule:** AMR cannot bypass or soften preflight gates. If available RAM after reclamation is 9.27 GiB, the system **must fail preflight closed**.

---

## 4. Policy Configuration Parameters

All AMR operational parameters in `nova/resource_steward/policy.py` (`ResourcePolicy`) reuse the primary policy thresholds to eliminate redundancy:

```python
# Primary policy thresholds reused for pressure tiers
preferred_inference_ram_gib: float = 12.0  # Threshold for NORMAL vs OPTIMIZATION tier
minimum_inference_ram_gib: float = 10.0    # Admission gate threshold (CONSTRAINED if below)
critical_ram_gib: float = 2.0             # CRITICAL tier threshold (sustained abort)

# AMR operational parameters
reclamation_cooldown_seconds: float = 30.0 # Minimum interval between automatic passes
min_reclamation_delta_mib: float = 1.0     # Minimum private commit reduction (1 MiB)
auto_reclaim_enabled: bool = True          # Master switch for AMR during preflight
```
