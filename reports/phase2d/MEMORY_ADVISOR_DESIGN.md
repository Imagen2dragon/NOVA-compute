# NOVA Compute — Phase 2D: Intelligent Memory Advisor Architecture & Design

**Project:** NOVA Compute  
**Subsystem:** Intelligent Memory Advisor (`nova/resource_steward/advisor.py`)  
**Roles:** User = Project Owner; ChatGPT = Lead Architect; Antigravity = Implementation Engineer  
**Status:** IMPLEMENTED & VERIFIED (67 Automated Tests Passing; Model Inference Gated)

---

## 1. Executive Summary & Objective

The **Intelligent Memory Advisor** is a read-only diagnostic component integrated into NOVA Compute's Resource Steward architecture. Inspired by the transparent process monitoring capabilities of Mem Reduct, the Advisor assists workstation users in identifying unnecessary application memory consumption before launching resource-intensive MoE LLM inference workloads (e.g., Qwen3-30B-A3B under Configuration D).

### Core Operational Principles
1. **Strictly Read-Only:** The Advisor observes and reports; it never terminates, suspends, signals, or modifies external host processes.
2. **Complete User Control:** All application closure decisions remain under manual user control through normal Windows application interfaces.
3. **No Standby Cache Sabotage or Cleaner APIs:** Prohibits `EmptyWorkingSet`, `NtSetSystemInformation`, `SetSystemFileCacheSize`, and `SetProcessWorkingSetSize`.
4. **Transparent Telemetry Accounting:** Distinguishes resident physical memory (working set) from committed virtual memory (private commit), clearly flagging partial or restricted measurements.
5. **Fail-Closed Admission Fidelity:** Never bypasses or lowers the 10.0 GiB available physical RAM admission gate.

---

## 2. Safe Process Classification Taxonomy

The Memory Advisor partitions running processes into three deterministic operational tiers:

```mermaid
flowchart TD
    Proc["Running Process Candidate"] --> Classify{"Process Name Match?"}
    Classify -- "In PROTECTED_PROCESSES" --> Protected["Category C: Protected System\n(Windows Core, Defender, Drivers)\nSTRICTLY EXCLUDED FROM RECOMMENDATIONS"]
    Classify -- "In USER_APP_PROFILES" --> UserApp["Category A: User Application\n(Browsers, IDEs, Assistants, Media)\nRanked for Manual User Review"]
    Classify -- "In KNOWN_BACKGROUND_PROFILES" --> BgApp["Category B: Background Application\n(Sync clients, Launchers, Helpers)\nRanked with Usage Context"]
    Classify -- "No Match" --> Unknown["Unknown Process\nTreated Conservatively\nFlagged for Manual Verification"]
```

### Category A: User Applications (`ProcessCategory.USER_APPLICATION`)
Interactive desktop applications that users launch for tasks:
- **Web Browsers:** `msedge.exe`, `chrome.exe`, `firefox.exe`, `brave.exe`, `opera.exe`
- **Development IDEs & Editors:** `code.exe`, `devenv.exe`, `pycharm64.exe`, `idea64.exe`, `cursor.exe`, `antigravity.exe`
- **AI Assistants & Local LLMs:** `claude.exe`, `chatgpt.exe`, `ollama.exe`, `llama-server.exe`
- **Communication & Media:** `slack.exe`, `discord.exe`, `telegram.exe`, `teams.exe`, `zoom.exe`, `spotify.exe`
- **Launchers:** `steam.exe`, `epicgameslauncher.exe`

*Guidance:* "Save open files/tabs and close the application through its standard window interface."

### Category B: Background Applications (`ProcessCategory.BACKGROUND_APPLICATION`)
Optional background services, synchronization utilities, and development worker processes:
- **Cloud Storage Sync:** `onedrive.exe`, `dropbox.exe`, `googledrivesync.exe`
- **Development Workers:** `node.exe` (local dev servers), `language_server.exe` (IDE indexers)
- **Launcher Helpers:** `steamwebhelper.exe`, `epicwebhelper.exe`

*Guidance:* "Review whether this tool is actively required during inference; pause syncing or exit via system tray/terminal."

### Category C: Protected Processes (`ProcessCategory.PROTECTED_SYSTEM`)
Critical Windows infrastructure, security components, and hardware drivers:
- **Windows Core & Session:** `system`, `registry`, `smss.exe`, `csrss.exe`, `wininit.exe`, `services.exe`, `lsass.exe`, `winlogon.exe`, `svchost.exe`, `dwm.exe`, `explorer.exe`, `sihost.exe`, `taskhostw.exe`, `spoolsv.exe`, `ctfmon.exe`, `conhost.exe`, `runtimebroker.exe`
- **Security & Microsoft Defender:** `msmpeng.exe`, `nissrv.exe`, `securityhealthservice.exe`, `smartscreen.exe`, `securityhealthsystray.exe`, `sgrmbroker.exe`
- **Drivers & NVIDIA Services:** `nvcontainer.exe`, `nvdisplay.container.exe`, `nvsphelper64.exe`, `wlanext.exe`, `audiodg.exe`

*Governance Rule:* **Protected processes are strictly excluded from closure recommendations.** They are never manipulated, signaled, or terminated.

### Unknown Processes (`ProcessCategory.UNKNOWN`)
Unclassified processes exceeding memory thresholds are treated conservatively:
- Labeled as `[Unclassified Process]`.
- Advised for manual review; the user is explicitly warned not to terminate processes of unknown origin.

---

## 3. Working Set vs. Private Commit Telemetry Interpretation

The Advisor avoids misleading claims regarding reclaimable memory:

| Telemetry Metric | Technical Definition | Interpretation & Advisor Policy |
| :--- | :--- | :--- |
| **Working Set (`working_set_bytes`)** | Physical RAM pages currently resident in memory. | **Physical Memory Residency.** When an application closes, Windows releases its resident pages, but the OS memory manager may place pages into the standby list (file cache) rather than immediately increasing "Free" RAM. Actual physical RAM recovery must be confirmed via fresh system telemetry. |
| **Private Commit (`private_commit_bytes`)** | Virtual memory committed exclusively to this process against the system commit limit. | **Dedicated Process Virtual Commit Charge.** Closing an application directly releases this private commit charge against the system commit limit, reducing system commit pressure. **It is NOT guaranteed physical RAM recovery**, because pages may be paged out or shared with pagefile allocation. |
| **Measurement Coverage (`instances_measured / instances_total`)** | Accessible PIDs queried via `GetProcessMemoryInfo`. | **Explicitly Flagged Partial Coverage.** When user processes run with elevated instances (or child processes access-restricted), the Advisor marks the measurement as `partial` and displays queried coverage (e.g., `7/47 measured`). |

---

## 4. Transparent Memory Ranking & Telemetry Validation Methodology

### Telemetry Validation Before Aggregation
To prevent corrupted or failed process counters from skewing recommendations:
1. **Working Set Validation:** Each process snapshot must have `status == TelemetryStatus.SUCCESS`, `working_set_bytes` as a non-boolean integer (`isinstance(val, int) and not isinstance(val, bool)`), and `working_set_bytes >= 0`. Failed, negative, or boolean counters are strictly excluded from aggregates. Independent working-set validity is preserved even if private commit telemetry is unavailable, malformed, or contradictory.
2. **Private Commit Validation & Strict Non-Boolean Counter Integrity:**
   - Strict counter typing: Measured instance counts (`instances_measured`) and total expected counts (`instances_total`) must be strict non-boolean integers (`isinstance(c, int) and not isinstance(c, bool)`).
   - Boolean values (`True` or `False`), negative counts, and malformed non-integer types are rejected and clamped to 0. Missing counters never create artificial measurement coverage.
   - Measured instance counts must never exceed total expected instance counts (`proc_meas <= proc_exp`).
   - Private commit requires `proc_meas > 0` and non-boolean non-negative integer `private_commit_bytes`. If a snapshot explicitly reports 0 measured instances (or boolean/negative), private commit is classified as inconsistent/unavailable and excluded from aggregation.
3. **Instance Accounting & Strict Zero-Measurement Coverage:** Total expected instances (`instances_total`) and measured instances (`instances_measured`) are tracked truthfully. When `instances_total > 0` and `instances_measured == 0`, measurement coverage remains strictly 0 (never replaced with 1). Grouped measurements across multiple instances safely aggregate valid snapshots while excluding contradictory measurements.

### Mathematical Distinction: Why Summing WS and Private Commit is Invalid
A process's working set represents physical RAM pages currently mapped into its page table. Private commit represents virtual address space pages committed exclusively by the process (backed either by RAM or the paging file). Because resident private pages reside **simultaneously in both the working set and private commit**, summing them directly ($WS + PC$) counts resident private memory twice and creates a mathematically false metric.

### Candidate Ordering: 3-Tier Sort Key
Because NOVA Compute is constrained primarily by physical RAM residency for MoE weight paging, candidate processes holding $\ge 100\text{ MB}$ memory are ordered strictly by the following 3-tier tuple:

$$\text{SortKey} = (\text{WS}_{\text{MB}}, \text{Priority}_{\text{category}}, \text{PrivateCommit}_{\text{MB}})$$

1. **Primary Consideration ($\text{WS}_{\text{MB}}$):** Observed physical resident working set. Processes holding the largest resident physical working set are placed first to surface the greatest potential relief for physical RAM admission.
2. **Secondary Consideration ($\text{Priority}_{\text{category}}$):** Category priority. When physical footprints are equal, interactive User Applications (Priority 2) are prioritized over Background Applications (Priority 1) because closing interactive user software is safer than stopping system background daemons.
3. **Tertiary Consideration ($\text{PrivateCommit}_{\text{MB}}$):** Dedicated private virtual commit charge serves as the final tie-breaker (or 0.0 if unavailable), favoring processes whose closure also relieves system commit pressure.

### Advisory Heuristic Ranking Score (Informational Indicator Only)
The Advisor also computes an advisory prioritization index for informational tracking:

$$\text{RankingScore} = \text{WS}_{\text{MB}} + 0.25 \times (\text{PrivateCommit}_{\text{MB}} \text{ if available else } 0.0)$$

**Advisory Disclaimer:** The ranking score is strictly an informational composite indicator. It does **not** represent recoverable physical RAM, does **not** guarantee memory recovery, and is **not** used as the candidate sorting formula (candidate ordering is strictly governed by the 3-tier tuple above).

---

## 5. Re-Measurement & Fresh Telemetry Admission Enforcement

```
[Inference Launch Attempt / CLI Request]
                │
                ▼
  [1. Intelligent Memory Advisor]
        │   ├─► Read-only telemetry collection (RAM, commit, top processes)
        │   ├─► Strict Counter Validation (SUCCESS, non-boolean int, non-negative, meas > 0)
        │   ├─► Canonical 3-Tier Categorization (Protected System Always Excluded)
        │   └─► Candidate Ranking (Physical Working Set as Primary Consideration)
        ▼
  [2. User Manual Action]
        │   └─► User closes selected application(s) via normal Windows UI
        ▼
  [3. Fresh Re-Measurement (`remeasure_memory`)]
        │   ├─► Strict Fresh Telemetry Enforcement (No caller snapshot injection permitted)
        │   ├─► Strict Admission Evaluator Integrity (Always uses trusted MemoryBudgetManager & ResourcePolicy)
        │   ├─► Fresh hardware queries (RAM via GlobalMemoryStatusEx, GPU via NVML, Disk via GetDiskFreeSpaceEx)
        │   ├─► Truthful delta calculation (RAM gain, commit reduction)
        │   └─► Full Gate Suite Evaluation via MemoryBudgetManager.evaluate_gates()
        ▼
  [4. Preflight Gate Suite Evaluation]
        ├─► Gate 1: Telemetry Integrity (All fresh sensors SUCCESS, no NaN/inf, byte sum consistency)
        ├─► Gate 2: Available Physical RAM >= 10.0 GiB
        ├─► Gate 3: System Commit Charge < 90.0%
        ├─► Gate 4: Available VRAM >= 5,500 MiB
        ├─► Gate 5: Competing GPU Compute Apps == 0
        ├─► Gate 6: Free Disk Space >= 25.0 GiB (26,843,545,600 bytes)
        │
        ├─► ALL GATES PASSED:
        │     └─► ADMISSION GRANTED (Inference permitted subject to user authorization)
        └─► ANY GATE FAILS (e.g. RAM passes but GPU or disk sensor fails):
              └─► ADMISSION GATED (Fail-closed; reaching RAM threshold alone cannot grant admission)
```

**Rule:** `remeasure_memory()` cannot independently grant inference permission based on RAM alone. The public API strictly prohibits injecting caller-supplied GPU snapshots, disk snapshots, or arbitrary budget evaluators. It always queries fresh telemetry across all hardware tiers directly through the monitor and evaluates admission using NOVA's trusted `MemoryBudgetManager` and configured `ResourcePolicy`. Reaching $\ge 10.0\text{ GiB}$ RAM does not produce a passed preflight if VRAM, GPU contention, system commit, or disk space gates fail.

### Mandatory Non-Overridable Admission Floors, Defensive Policy Isolation & Evaluation-Time Admission-Floor Enforcement
To protect machine stability and prevent accidental or adversarial weakening of admission thresholds, the platform enforces mandatory project floors:
- **Available RAM:** $\ge 10.0\text{ GiB}$
- **System Commit:** $< 90.0\%$
- **Free VRAM:** $\ge 5,500\text{ MiB}$
- **Competing GPU Compute Apps:** $== 0$
- **Free Disk Space:** $\ge 25.0\text{ GiB}$ ($26,843,545,600$ bytes, $25 \times 1024^3$)
- **Telemetry Integrity:** All sensors reporting `SUCCESS`

A configurable `ResourcePolicy` is permitted to specify **stricter** criteria (e.g. $\ge 12.0\text{ GiB}$ RAM, $< 80\%$ commit, $\ge 5,800\text{ MiB}$ VRAM, or $\ge 40.0\text{ GiB}$ disk), but can **never weaken** these mandatory constraints.

**Defensive Policy Isolation & Evaluation-Time Admission-Floor Enforcement Guarantees:**
1. `enforce_mandatory_admission_floors()` always returns a new defensive copy and never returns the caller's mutable policy reference.
2. `MemoryBudgetManager.evaluate_gates()` re-applies `enforce_mandatory_admission_floors()` immediately before evaluating thresholds during every gate evaluation, ensuring that direct mutation of `manager.policy` or the original caller object cannot weaken mandatory admission floors.
3. Stricter user-configured settings (e.g. $\ge 12.0\text{ GiB}$ RAM, $< 80\%$ commit, $\ge 5,800\text{ MiB}$ VRAM, or $\ge 40.0\text{ GiB}$ disk) remain fully preserved and enforced.
4. Hard-coded unit semantics: Disk telemetry computes and verifies Gibibytes ($2^{30} = 1,073,741,824$ bytes per unit) via `GetDiskFreeSpaceExW`. The mandatory threshold is $25.0\text{ GiB}$ ($26,843,545,600$ bytes).

---

## 6. Static AST Safety Audit Compliance

Automated AST verification (`test_58_advisor_static_inspection_forbidden_apis_absent`) mathematically proves that none of the following identifiers exist in `advisor.py`:
- `EmptyWorkingSet`
- `K32EmptyWorkingSet`
- `NtSetSystemInformation`
- `MemoryPurgeStandbyList`
- `MemoryFlushModifiedList`
- `SetSystemFileCacheSize`
- `SetProcessWorkingSetSize`
- `TerminateProcess`
- `taskkill`
