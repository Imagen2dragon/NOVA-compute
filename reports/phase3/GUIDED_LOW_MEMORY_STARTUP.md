# NOVA Compute — Guided Low-Memory Startup Architecture
**Phase:** 3B.1 — Guided Low-Memory Startup & Progressive Model Loading  
**Status:** IMPLEMENTED AND VERIFIED  
**Target Hardware:** NVIDIA RTX 4050 Laptop GPU (6,141 MiB GDDR6), 16 GB Physical RAM (15.75 GiB usable), Windows 11 Home  

---

## 1. Executive Summary and Architecture Objectives

In resource-constrained environments such as the target 16 GB laptop, physical memory is continuously shared between operating system services, user applications (browsers, IDEs, desktop agents), and background processes. Under normal desktop workloads, available physical RAM frequently rests between 7.5 GiB and 9.5 GiB, below NOVA's mandatory **10.0 GiB inference admission floor**.

Previous versions of the runner halted immediately upon detecting any failed preflight gate, terminating the CLI process. While this strictly preserved machine safety, it created operational friction: the user had to manually guess which background applications were consuming memory, close them blind, and repeatedly re-launch the CLI runner.

**Phase 3B.1 introduces Guided Low-Memory Startup**:
1. **Lightweight Startup Allowed Under Low RAM**: NOVA starts safely in lightweight safe mode even when available physical RAM is below 10.0 GiB.
2. **Strict Decoupling of Startup from Inference**: Application startup is permitted under low RAM; model inference is **strictly gated and forbidden** until all six admission gates pass and explicit Project Owner authorization is granted.
3. **Transparent Deficit Reporting**: NOVA calculates and displays the exact physical RAM deficit (e.g., $10.00\text{ GiB} - 8.23\text{ GiB} = 1.77\text{ GiB}$).
4. **Intelligent, Read-Only Recovery Guidance**: The Memory Advisor identifies top candidate user applications for manual closure without ever killing, suspending, or altering external processes.
5. **Accurate Telemetry Interpretation**: Distinguishes resident physical working set from virtual private commit charge, avoiding false promises of guaranteed memory recovery.
6. **Explicit User-Controlled Recheck**: Provides an interactive `[Recheck Memory]` loop that re-evaluates all six hardware gates atomically on demand without high-frequency polling loops.
7. **Precise Language Separation**: Strictly separates physical RAM deficit warnings from storage/disk capacity warnings.

---

## 2. Fundamental Architectural Distinction

$$\text{NOVA Application Startup} \neq \text{Model Inference Execution}$$

| Lifecycle Phase | Low-Memory Status (< 10 GiB RAM) | llama-cli Subprocess | GGUF Weight Mapping | CUDA Allocation |
|---|---|---|---|---|
| **Standard Mode (`--preflight`)** | Exit code 1 (Gates unmet) | NOT CREATED | NONE | NONE |
| **Guided Startup (`--guided-start`)** | Allowed (`LOW_MEMORY_WAIT`) | NOT CREATED | NONE | NONE |
| **Execution Mode (`--execute`)** | Gated / Aborted | BLOCKED | BLOCKED | BLOCKED |
| **Authorized Smoke Test** | Allowed ONLY if all 6 gates pass | Created under Supervisor | File-backed mmap | Active |

Under `--guided-start`, NOVA initializes only:
- The `GuidedStartupController`.
- The `StartupStateMachine`.
- The read-only `ResourceSteward` and `MemoryAdvisor`.
- Standard I/O interfaces.

No model weights are mapped into host virtual address space, no CUDA contexts or VRAM buffers are allocated, and no worker threads or child processes are spawned.

---

## 3. Command-Line Interface and Startup Banner

Guided startup is launched via an explicit CLI argument:

```powershell
python scripts/run_inference_with_telemetry.py --guided-start
```

The CLI enforces mutual exclusivity with `--execute`, `--preflight`, `--dry-run`, `--memory-report`, and `--advisor`.

Upon invocation, NOVA immediately presents its operational banner:

```text
========================================================================
NOVA COMPUTE — GUIDED STARTUP
Startup mode: LOW-MEMORY SAFE MODE
Model state: NOT LOADED
Inference state: BLOCKED UNTIL QUALIFIED
========================================================================
```

---

## 4. Guided Low-Memory Recovery Workflow

When the initial preflight check detects that available physical RAM is below 10.0 GiB, NOVA transitions from `PREFLIGHT_RUNNING` to `PREFLIGHT_FAILED`, and immediately enters `LOW_MEMORY_WAIT`.

### 4.1 Exact Deficit Calculation
The exact deficit is computed directly from raw authoritative memory counters:

$$\text{RAM Deficit (GiB)} = \max\left(0.0, \, \text{Round}\left(\text{Policy}_{\text{MinRAM}} - \text{Telemetry}_{\text{AvailRAM}}, \, 2\right)\right)$$

Example output:
```text
[!] Available physical RAM is below the required threshold. Close unused applications to make more memory available.
Current available RAM: 8.23 GiB
Required RAM: 10.00 GiB
Additional available RAM required: 1.77 GiB

Suggested actions:
1. Save work and close unused browser tabs/windows.
2. Close unused IDE projects.
3. Exit unused development tools through their normal interfaces.
4. Review other recognized user applications.
```

### 4.2 Candidate Process Diagnostics
The Memory Advisor inspects active processes and ranks candidate user applications by observed physical footprint:

```text
Top candidate applications for manual closure:
  - msedge.exe (3 instances): Working Set: 840.5 MB, Private Commit: 920.0 MB
    Action: Save work and close active tabs or exit unused browser windows.
  - code.exe (1 instances): Working Set: 612.0 MB, Private Commit: 680.5 MB
    Action: Save open files and close unused editor windows or heavy extensions.
  - spotify.exe (1 instances): Working Set: 215.0 MB, Private Commit: 240.0 MB
    Action: Close Spotify desktop player if not actively streaming.
```

### 4.3 Telemetry Interpretation & Working Set Semantics
The user interface explicitly educates the operator on Windows memory mechanics:
- **Working Set**: Resident physical pages currently resident in RAM at sample time. Closing an application may release pages back to the operating system's standby/free lists, but dependent shared DLLs or cache pages may remain resident.
- **Private Commit**: Virtual memory guaranteed by the pagefile and RAM. Closing an application relieves commit limit pressure, but private commit bytes do **not** represent an immediate 1:1 physical RAM refund.
- **No False Recovery Promises**: NOVA never promises that closing an application will immediately produce its full measured working set in available RAM.
- **No Standby List Purging**: NOVA adheres strictly to non-destructive diagnostics. It never invokes undocumented Windows APIs (`NtSetSystemInformation`) to empty system working sets or purge standby lists.

---

## 5. Explicit User-Controlled Recheck (`[Recheck Memory]`)

Rather than burning CPU and I/O cycles in a high-frequency polling loop, the controller yields execution and awaits explicit user input:

```text
[Recheck Memory] Press Enter or 'R' to recheck all admission gates, or 'Q' to exit: 
```

When the user triggers a recheck (`Enter` or `R`):
1. **State Transition**: Transitions `LOW_MEMORY_WAIT -> RESOURCE_RECOVERY -> PREFLIGHT_RUNNING`.
2. **Full Telemetry Refresh**:
   - Collects fresh physical RAM and system commit via `GlobalMemoryStatusEx`.
   - Collects fresh GPU VRAM, compute processes, and temperature via NVML / `nvidia-smi`.
   - Collects fresh free storage on drive `C:\` via `GetDiskFreeSpaceExW`.
   - Enumerates active processes to verify absence of competing compute workloads.
   - Validates telemetry integrity across all sensors.
3. **Comprehensive Gate Evaluation**: Re-evaluates **all six mandatory admission gates**. It never evaluates RAM in isolation.
4. **Outcome Routing**:
   - If any gate fails: Transitions `PREFLIGHT_RUNNING -> PREFLIGHT_FAILED -> LOW_MEMORY_WAIT` and redisplays updated deficit and gate states.
   - If all six gates pass: Transitions `PREFLIGHT_RUNNING -> READY_FOR_AUTHORIZATION`.

---

## 6. Language Precision: Strict Separation of RAM and Disk Warnings

NOVA enforces strict separation between memory pressure and storage limits:

- **When Physical RAM Gate Fails**:
  ```text
  [!] Available physical RAM is below the required threshold. Close unused applications to make more memory available.
  ```
  NOVA **never** mentions "free disk space" or "storage deficit" when the RAM gate fails.
- **When Disk Space Gate Fails**:
  ```text
  [!] Storage admission gate failed: Free disk space is below the required threshold.
      Required: >= 25.0 GiB, Measured: 18.42 GiB
      Please free storage space on the model drive. Never delete files automatically.
  ```
  NOVA clearly identifies storage capacity on the target volume and **never** deletes files automatically.

---

## 7. Concrete Operational Scenarios

### Scenario A: 8.0 GiB Available RAM (Initial Startup)
- Controller starts in lightweight safe mode.
- Banner displays `Model state: NOT LOADED` and `Inference state: BLOCKED UNTIL QUALIFIED`.
- Deficit calculated: $10.00 - 8.00 = 2.00\text{ GiB}$.
- Memory Advisor lists candidate applications (e.g. Edge, VS Code).
- Enters `LOW_MEMORY_WAIT`. Model remains unloaded.

### Scenario B: User closes Edge, Available RAM reaches 9.5 GiB
- User presses `R` or `Enter` to trigger `[Recheck Memory]`.
- All six gates are remeasured.
- RAM gate fails ($9.50\text{ GiB} < 10.00\text{ GiB}$). All other gates pass.
- Deficit updated: $10.00 - 9.50 = 0.50\text{ GiB}$.
- NOVA continues blocking inference, displays remaining 0.50 GiB deficit, and returns to `LOW_MEMORY_WAIT`.

### Scenario C: User closes IDE project, Available RAM reaches 10.4 GiB (All gates pass)
- User triggers `[Recheck Memory]`.
- All six gates pass: RAM = 10.40 GiB, Commit = 52%, VRAM = 5,900 MiB, Competing Apps = 0, Disk = 85 GiB, Telemetry = SUCCESS.
- State machine transitions `PREFLIGHT_RUNNING -> READY_FOR_AUTHORIZATION`.
- System displays:
  ```text
  All 6 admission gates PASSED. System is qualified for Configuration D smoke test.
  CRITICAL SAFETY GOVERNANCE:
  Inference state: BLOCKED UNTIL AUTHORIZED
  A successful preflight NEVER automatically launches model inference.
  Waiting for separate Project Owner authorization before initiating model loading.
  ```
- Controller halts and exits cleanly with code 0 without launching `llama-cli.exe`.

### Scenario D: RAM reaches 10.4 GiB, but VRAM or Commit Fails
- Example: A 3D application starts, consuming VRAM (free VRAM drops to 4,800 MiB).
- User triggers `[Recheck Memory]`.
- RAM gate passes, but VRAM gate fails ($4,800\text{ MiB} < 5,500\text{ MiB}$).
- System blocks qualification, displays `[FAIL] avail_vram: 4800.0 MiB (Req: >= 5500 MiB)`, and returns to `LOW_MEMORY_WAIT`. Model is never loaded.

### Scenario E: Clean User Exit
- User enters `Q` or `quit`.
- Controller transitions state machine to `ABORTED`.
- Output: `Exiting guided startup. Safe shutdown complete.`
- Application terminates cleanly with exit code 0.
