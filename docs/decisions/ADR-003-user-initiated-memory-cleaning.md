# ADR-003: User-initiated memory cleaning in NOVA Chat ("Clean memory" button)

- **Date:** 2026-11-08
- **Status:** Accepted (Project Owner decision)
- **Supersedes in part:** the Phase 2C rule that NOVA never trims other processes' working sets or purges system memory lists (`nova/resource_steward/adaptive_memory.py`, `reports/phase2c/ADAPTIVE_MEMORY_RECLAIMER_POLICY.md`) — **for the desktop app's explicit button only**.

## Context

The owner runs required applications and often cannot reach the RAM needed to load Qwen3-30B-A3B. They asked for a button that works like Mem Reduct to free memory, including the admin-level cleaning.

The Phase 2C policy forbade `EmptyWorkingSet` on foreign processes and `NtSetSystemInformation` memory-list commands, so this is a deliberate reversal for one user-initiated action.

## Decision

NOVA Chat's loader (RAM tab) gets a **Clean memory** button that:

1. **Without admin:** calls `EmptyWorkingSet` on every process the user can open (excluding NOVA itself and the System Idle/System processes). Pages move to the standby/modified lists; no application is closed or suspended.
2. **With admin (UAC prompt every time):** starts `nova-chat.exe --nova-memory-purge` elevated via `ShellExecuteExW("runas")`. The helper enables `SeProfileSingleProcessPrivilege` and calls `NtSetSystemInformation(SystemMemoryListInformation)` with `MemoryEmptyWorkingSets`, `MemoryFlushModifiedList` and `MemoryPurgeStandbyList`, then exits. Declining the prompt leaves only step 1 done.
3. Reports free RAM before/after, apps trimmed, and the admin step result, then re-runs the admission check. Logged as `[MEMORY_CLEAN]`.

Constraints:
- **Only on explicit user click.** Never automatic, never on a timer.
- **Refused while a model is loading or loaded** (backend check), because it would evict the model's own pages and slow inference.
- **Never closes, suspends or terminates any process.**
- The Python CLI harness keeps its Phase 2C prohibition; its static-inspection tests still forbid these APIs in `nova/` and `scripts/`.

## What it cannot do

- **GPU memory:** Windows has no API to take GPU memory from another process. The loader instead names the GPU apps to close.
- **Standby purge does not raise "available" RAM:** Windows already counts standby pages as available, and NOVA's gate reads `ullAvailPhys`. The purge mainly increases *free* RAM, and it discards cached pages of the model file, which can make the next load slower. Step 1 is the part that raises NOVA's number.

## Alternatives considered

- **Safe trim only (no admin):** recommended by the lead architect; declined by the owner in favour of full Mem Reduct-like behaviour.
- **Automatic cleaning before load:** rejected; violates "no silent resource management".

## Consequences

- Other apps may be briefly sluggish after cleaning while their pages fault back in.
- One UAC prompt per click; the app itself stays non-elevated.
- New Rust module `desktop/src-tauri/src/memory_cleaner.rs`; `main.rs` handles `--nova-memory-purge`; windows-sys features `Win32_UI_Shell` and `Win32_System_Registry` added.
