# NOVA Chat — Phase 3D.0 Instructions for Antigravity
## Review the 4 GiB path, make the first run measurable, fix the installer build

**To:** Antigravity — Implementation Engineer
**From:** Claude — Lead Architect / Independent Reviewer
**CC:** Project Owner, ChatGPT
**Branch:** `feature/nova-chat-desktop`
**Baseline HEAD:** `cae2a811baf14eccfb3de1c7a03af1132a46f010` **plus uncommitted working-tree changes** (listed in Section 1). Do not discard, stash or reset them.
**Date:** 2026-11-07

---

## 1. Context: the owner changed the plan

The owner cannot free 10 GiB RAM while running required work and decided (2026-11-07):

- **Skip the separate Option A CLI test.** The first run happens in **NOVA Chat**, which has the stronger protections (model lock, Job Object, single-use authorization).
- **New profile "Low-Memory Streaming (4 GiB, slower)"** — same llama-server arguments as Configuration D, RAM gate 4.0 GiB. All other gates unchanged.
- **Live memory watchdog** stops llama-server if free RAM < 1.5 GiB for 5 s, commit ≥ 90%, or memory telemetry fails 4× in a row.
- **"Verify model" button** in the loader; loading is disabled until the model is verified.

Claude implemented this (uncommitted). Read `reports/desktop/NOVA_CHAT_CLAUDE_TAKEOVER_AUDIT.md` Section 18 first.

Uncommitted files: `desktop/src-tauri/src/{lib.rs, memory_budget.rs, memory_watchdog.rs (new), model_integrity.rs, server_manager.rs, storage.rs, tests.rs}`, `desktop/src/{App.tsx, types.ts, components/ResourceRecoveryModal.tsx, components/ResourceDashboard.tsx}`, `reports/desktop/NOVA_CHAT_CLAUDE_TAKEOVER_AUDIT.md`, plus your own 3C.1 report and runbook edits.

## 2. Hard limits

**Do not:**
- Load the model, start `llama-server.exe` or `llama-cli.exe` with the real GGUF, or send chat requests. The **owner** does the first run.
- Click "Verify model" or otherwise hash the real GGUF through the app (the owner does it; it writes the cache).
- Kill user processes, change Windows or OneDrive settings, run elevated, install the NSIS installer.
- Commit, push, merge, switch branches, reset, stash or clean.

**Allowed:** source review; `cargo test --lib`, `cargo clippy` (no `--fix`), `npx tsc --noEmit`; release and NSIS builds; launching `nova-chat.exe` **without loading a model** to check the UI; the code changes in Sections 4 and 5; your report.

## 3. Independent review of the 4 GiB changes (no edits unless a defect is confirmed)

For each item give file and line, and say **correct / defect / risk**:

1. `memory_budget.rs`: `LowMemoryStreaming` produces exactly Configuration D's arguments (test `test_low_memory_streaming_profile_matches_config_d_placement`). Only the RAM gate differs.
2. `memory_watchdog.rs` decision logic, and `lib.rs::start_memory_watchdog`:
   - Is it started for every successful spawn and retired on unload and on the next start (epoch counter)?
   - Can two watchdogs run at once? Can it stop a **newer** server than the one it was started for?
   - It holds `server_manager`'s std `Mutex` only briefly, and does the stop in `spawn_blocking`. Check for deadlock with `load_model` (which holds `lifecycle_lock` and briefly `server_manager`) and with `unload_model`.
   - After a watchdog stop, does the UI end in a consistent state (state `ERROR`, model lock released only if termination confirmed, `send_chat_message` rejected)?
3. `load_model`: readiness timeout 300 s for the streaming profile; a watchdog reason is returned as the error.
4. `model_integrity.rs`: `rename_all = "SCREAMING_SNAKE_CASE"` on `ModelIntegrityState`. Confirm nothing persisted or parsed depends on the old variant names (cache file, reports, frontend).
5. `storage.rs`: test builds use `%TEMP%\nova_chat_test_appdata`; release builds still use `%LOCALAPPDATA%\NOVA Chat\`.
6. Frontend: Verify/Cancel flow, progress events (`nova://integrity-progress`, `nova://integrity-status`), load disabled until verified, `nova://watchdog-abort` handling, "Use 4 GiB profile" button.
7. Note: `ResourceRecoveryModal` calls React hooks after `if (!isOpen) return null;` (pre-existing). Say whether it can break and propose a fix.

## 4. Make the first run measurable (approved small changes)

**Problem:** llama-server is started with no stdout/stderr handles and `CREATE_NO_WINDOW`, so its load time and tokens/s are **not recorded anywhere**. The owner's first run would give no speed numbers.

Implement the smallest safe option and test it:
- **Preferred:** in `chat.rs`, read the `timings` object that llama-server includes in the final streamed chunk of `/v1/chat/completions` (e.g. `prompt_n`, `prompt_ms`, `prompt_per_second`, `predicted_n`, `predicted_ms`, `predicted_per_second` — verify the exact field names against the pinned build's `/v1/chat/completions` handling in the llama.cpp source for commit `dde879bc4`). Log them with `log_diagnostic` and attach them to the completed message/stream payload so the UI can show "N tokens · X tok/s".
- Also log, in `load_model`: profile, time from spawn to `/health` ready, and the watchdog's lowest free RAM and highest commit seen during load (add those two maxima to `MemoryWatchdog`).
- Do **not** redirect llama-server's stdout into a pipe that nobody drains (it can block the child). If you capture its output, write it to a file handle.
- Never log the API key or prompt text.

Add unit tests with a mocked SSE stream containing a `timings` object, and for the watchdog min/max tracking.

## 5. Fix the installer build

`npm run tauri build -- --bundles nsis` built `nova-chat.exe` but bundling failed:
`The object manager encountered a reparse point while retrieving an object. (os error 4395)`. The project is under `OneDrive\Desktop`.

1. Find which path triggers it (Tauri bundler, NSIS download cache under `%LOCALAPPDATA%\tauri`, or a OneDrive placeholder in `target\`). Quote evidence.
2. Propose the fix (e.g. build from a copy outside OneDrive, or set `CARGO_TARGET_DIR` to a non-OneDrive folder for the build). You may run a build with `CARGO_TARGET_DIR` set **for that one command only**. Do not change OneDrive settings or global environment variables.
3. If it succeeds, record the new installer size and SHA-256. Do not run the installer.

## 6. Pre-existing issue to assess (do not fix)

`ServerConfigManifest::from_profile` maps `Balanced` (24 GPU layers) to `-ncmoe 0`, which puts all experts of those 24 layers on the GPU. Estimate VRAM against 6 GB and recommend whether to hide `Balanced` until fixed.

## 7. UI check without a model

Launch the new `nova-chat.exe`, open the loader, and confirm (screenshots to `reports/desktop/screenshots/phase3d0/`):
- the integrity panel shows "Not verified" or "Unknown" (not a fake "verified");
- "Low-Memory Streaming (4 GiB, slower)" is listed;
- the RAM tab shows the "free about X GiB / Use 4 GiB profile" guidance when RAM is short;
- "Confirm & Load Model" is disabled while the model is unverified.

Close the app afterwards. Do not click Verify or Load.

## 8. Deliverable

`reports/desktop/NOVA_CHAT_PHASE3D_0_REVIEW_AND_MEASUREMENT_REPORT.md`:
1. Verdict on the 4 GiB changes: **ACCEPTED / CONDITIONALLY ACCEPTED / REJECTED / NOT VERIFIED**.
2. Section 3 findings.
3. Section 4 changes, diffs summarized, tests added.
4. Section 5 root cause and result.
5. Section 6 assessment.
6. Section 7 screenshots and observations.
7. Test results: `cargo test --lib`, `tsc --noEmit`, clippy summary.
8. Exact commands run, labeled read-only or modifying.
9. A short **owner run checklist** for the first NOVA Chat run with the streaming profile, including what numbers will now be recorded and where.

Stop after the report. Do not run the model.
