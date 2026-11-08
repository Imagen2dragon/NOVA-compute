# ADR-004: Automatic memory assist and revised watchdog thresholds

- **Date:** 2026-11-08
- **Status:** Accepted (Project Owner request)
- **Amends:** ADR-003 ("only on explicit click") and the 2026-11-07 watchdog thresholds.

## Context

First real NOVA Chat runs with the Low-Memory Streaming profile (from `app.log`):

- Load to `/health` ready: 11.8–13.3 s.
- Generation: 177 tokens at 4.80 tok/s; prompt 3.91 tok/s.
- The watchdog stopped every run within 12–60 s: "Free RAM stayed below 1.5 GiB for 5 s" (lowest free RAM 0.22–0.62 GiB). Commit stayed at 67–71%.

While experts stream from the SSD, Windows keeps free RAM low by design; the model's memory-mapped pages are clean and reclaimable. The owner asked NOVA to adapt to its memory demand automatically and free memory for its own use.

Also observed: the Clean memory button trimmed NOVA's own WebView2 window processes, `dwm.exe` and `explorer.exe`, and the admin step emptied every process's working set, so NOVA and the desktop stuttered afterwards.

## Decision

1. **Automatic memory assist (setting `auto_memory_assist`, default on, toggle in the loader's RAM tab):**
   - **Before a load:** if free RAM is below the selected profile's requirement when the hardware check runs, trim other apps' working sets once (30 s cooldown), then re-check.
   - **While the model runs:** when free RAM drops below 2.0 GiB, trim other apps' working sets, at most once per 30 s. Logged as `[AUTO_ASSIST]`; shown in the RAM tab.
   - Never uses admin rights, never closes or suspends an app.
2. **Watchdog stop rule:** free RAM < **0.5 GiB for 15 s** (was 1.5 GiB for 5 s). Commit ≥ 90% and 4 consecutive telemetry failures still stop immediately.
3. **Trimming never touches:** NOVA's own process tree (WebView2 window processes, llama-server), `dwm.exe`, `explorer.exe`, `csrss.exe`, `winlogon.exe`, `audiodg.exe`, `ctfmon.exe`, `textinputhost.exe`, `searchhost.exe`.
4. **Admin step of Clean memory:** no longer issues `MemoryEmptyWorkingSets` (it emptied every process); it only flushes the modified list and purges the standby list.

## Consequences

- Other apps may page back in after an assist and feel briefly slower; the 30 s cooldown limits repeated trimming.
- The laptop can run with less free RAM than before; if Windows becomes unresponsive, lower thresholds may need to be raised again (owner decision).
- Admission checks now run off the UI thread (async + `spawn_blocking`), removing the window freezes on "Check Again" and profile changes.
