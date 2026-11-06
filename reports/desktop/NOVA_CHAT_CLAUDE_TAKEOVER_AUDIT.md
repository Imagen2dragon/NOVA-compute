# NOVA Chat — Claude Takeover Audit

**Author:** Claude (successor lead architect / independent reviewer)
**For:** Project Owner, ChatGPT (previous lead architect), Antigravity (implementation engineer)
**Date:** 2026-11-06
**Scope:** Repository reconciliation and independent verification, per the handoff document's "First action". No new features implemented.

---

## 1. Executive summary

- The repository is at the expected baseline commit, but **almost all desktop work is uncommitted** (101 untracked files plus 3 modified tracked files). The Phase 3B.9 claim that Git status was "clean" is **false**.
- **Rust tests reproduce:** 105 passed, 0 failed, 1 ignored (106 tests). The earlier canonical-path fix in test 14 is still present.
- **Build artifacts match the Phase 3B.9 report:** release exe and NSIS installer hashes recomputed and identical. Runtime `llama-server.exe` hash matches the pinned value.
- **Phase 3B.9 model protection design is sound in its core ordering** (create suspended → assign to Job Object → duplicate lock → resume), and its tests drive the production spawn function. But I found **five lifecycle and integrity gaps** (Section 6), two of which should be fixed before any live test.
- **Phase 3B.10 report exists but does not cover F1–F4.** Correction: `NOVA_CHAT_PHASE3B_10_FINAL_SECURITY_AND_RELEASE_AUDIT.md` was written at 19:27, just after this audit listed the folder. It predates this audit, so it does not address F1–F4, and its claim that failure paths "reap" the child is not supported by the code (no wait after `TerminateProcess`). The README's "Phase 3B.10 Security Hardening Complete" is therefore premature.
- **Update (Section 14): F1 and F3 are now fixed** with Project Owner approval; 109 passed, 0 failed, 1 ignored.
- **Functional gap:** the UI never calls the backend's model-integrity verification command, so a model without a valid cache can never pass the integrity gate from the UI. This fails closed (safe), but blocks a first-time user.

**Verdicts**

| Area | Verdict |
|---|---|
| Model-integrity protection (Option A, duplicated handle) | **CONDITIONALLY ACCEPTED** |
| Job Object lifecycle | **CONDITIONALLY ACCEPTED** |
| Hardware admission (six gates) | **NOT VERIFIED** against live telemetry (code present; unit tests pass) |
| Installer / runtime deployment | **NOT VERIFIED** — runtime still not bundled |
| Release readiness overall | **NOT VERIFIED** |

---

## 2. Baseline and Git state

Commands (read-only): `git rev-parse HEAD`, `git branch --show-current`, `git status --short`, `git diff --stat`, `git ls-files --others --exclude-standard`, `git remote -v`.

| Item | Value |
|---|---|
| HEAD | `7d765f45888574751c8d674bba481fdaa5e2a01f` |
| Branch | `feature/nova-chat-desktop` |
| Remote | `origin https://github.com/Imagen2dragon/NOVA-compute.git` |
| Modified (tracked) | `.gitignore` (+5), `README.md` (+10/−2), `docs/architecture.md` (+29/−1) |
| Untracked | 101 files: `desktop/` 61, `reports/` 33, `scripts/` 4, `docs/` 3 |
| Untracked dirs | `desktop/`, `docs/architecture/`, `docs/decisions/`, `reports/desktop/` |
| Untracked scripts | `benchmark_startup.ps1`, `capture_ui_screenshots.ps1`, `generate_tensor_placement_report.py`, `inspect_tensors.py` |

**Implication:** the entire NOVA Chat desktop application, all Phase 2C–3B.9 reports and both ADRs exist only in this working copy. Nothing is backed up in Git. I did not commit, stash or clean anything.

`.gitignore` correctly excludes `desktop/node_modules/`, `desktop/dist/`, `desktop/src-tauri/target/`. `runtime/` and `models/` have 0 tracked files (expected; large binaries).

---

## 3. Project structure (verified)

```text
desktop/
  src/                       React 19 + TypeScript (≈3,300 lines TSX + 2,072 CSS)
    App.tsx (809)            app state, IPC calls, init, screenshot mock views
    components/              ChatArea, Composer, Sidebar, TopBar, MarkdownRenderer,
                             GuidedLoaderModal, ResourceDashboard, ResourceRecoveryModal,
                             SettingsModal
  src-tauri/
    tauri.conf.json          NSIS, currentUser install, strict CSP
    src/ (9,711 lines Rust)
      lib.rs (857)           Tauri commands, AppContext, load/unload flow
      server_manager.rs (644) runtime discovery, Configuration D manifest, spawn/stop
      job_object.rs (413)    Job Object, suspended spawn, handle duplication
      model_integrity.rs (720) lock guard, SHA-256 streaming, file identity, startup cache eval
      admission.rs (948)     six-gate preflight
      memory_budget.rs (381) runtime profiles
      resource_advisor.rs (160) process scan / guidance
      chat.rs (460)          SSE client, per-request cancellation
      storage.rs (365)       atomic JSON persistence with .bak
      state.rs, main.rs
      tests.rs (4,706)       106 tests
    examples/                crash_safety_helper.rs, job_parent_helper.rs
runtime/windows/llama-cpp/   pinned llama.cpp + CUDA DLLs (untracked)
models/qwen3-30b/            GGUF + leftover .part file (untracked)
docs/decisions/              ADR-001 (Option A handle), ADR-002 (runtime packaging, "Proposed")
```

Note: the handoff lists `src/examples/crash_safety_helper.rs`; the actual path is `src-tauri/examples/crash_safety_helper.rs`.

---

## 4. Functionality: reported vs verified

### 4.1 Frontend

| Function | Status | Evidence |
|---|---|---|
| Chat UI, streaming display, cancel | Present | `ChatArea.tsx`, `Composer.tsx`; invokes `send_chat_message`, `stop_generation` |
| Conversation list / save / delete / clear | Present | invokes `get_conversations`, `save_conversation`, `delete_conversation`, `clear_history` |
| Settings, model and runtime path validation | Present | `SettingsModal.tsx`; `validate_model_path`, `validate_runtime_path`, `save_settings_cmd` |
| Admission check and confirmation flow | Present | `check_admission`, `check_admission_v2`, `confirm_model_load`, `cancel_model_confirmation`, `load_model`, `unload_model` |
| **Model integrity verification (SHA-256)** | **Not wired** | No UI call to `start_model_integrity_verification`, `get_model_integrity_status` or `cancel_model_integrity_verification` |
| Live telemetry / recovery commands | Not wired | `get_system_telemetry`, `get_recovery_summary`, `get_resource_budget`, `get_top_processes`, `get_model_cache_info`, `revalidate_model_cache` are registered but never invoked by the UI |
| Screenshot mock data | Present in production bundle | `App.tsx` ~lines 182–345 build a fake `PreflightReport` (including `CRYPTOGRAPHICALLY_VERIFIED` and fake RAM/VRAM numbers) **only when the URL has `?view=`**. Not reachable in normal use, but UI screenshots in earlier reports may show mock values, not real telemetry. |
| Integrity badge fallback | Misleading | `ResourceDashboard.tsx:193` shows `PREVIOUSLY_VERIFIED` when the status is missing; should show `UNKNOWN`. |

### 4.2 Backend

| Function | Status | Evidence |
|---|---|---|
| No auto model load at startup | Verified by code | Startup only evaluates integrity cache (`evaluate_startup_integrity`); no spawn |
| Single-use, expiring load authorization | Verified by code | `lib.rs` `load_model`: `.take()` of authorization, expiry check, path match |
| Fresh admission audit right before launch | Verified by code | `run_preflight_audit_with_profile` in `load_model` |
| Integrity gate before launch | Verified by code, with gaps | See 6.2, 6.3 |
| Loopback-only, Bearer auth | Verified by code | `--host 127.0.0.1`, `--api-key`; `chat.rs:290` adds `Authorization: Bearer`; key generated `nova-<uuid v4>`; `Debug` redacts it |
| Readiness via `/health` | Verified by code | `check_endpoint_readiness`; 60 s timeout, 401 → stop |
| Lock released only after child + job empty | Verified by code | `stop_server_with_timeout` |
| Atomic persistence | Verified by code | `storage.rs`: temp file, `sync_all`, `.bak`, `rename`; `.bak` fallback on parse failure |
| Single NOVA instance | **Not implemented** | No single-instance plugin or named mutex (see 6.5) |

---

## 5. Tests and artifacts

### 5.1 Rust tests

Command: `cargo test -j 4` in `desktop/src-tauri` (modifies `target/` only).

```
test result: ok. 105 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 1.85s
```

- Ignored: `test_live_system_preflight_audit` (manual live-hardware diagnostic; correct to keep manual).
- Canonical-path fix from the earlier review is present at `tests.rs:3560` (`std::fs::canonicalize(&helper_exe)`). It only affects test code; the production launcher already canonicalizes (`server_manager.rs:355`), so it does not hide a production defect.
- The 16 `test_phase3b9_*` tests run `examples/crash_safety_helper.rs`, which calls the **production** `JobObject::spawn_child_suspended_with_handle` (7 call sites). They are production-equivalent for the spawn path.
- Negative control `test_phase3b9_16` uses a temp file (`%TEMP%\nova_p3b9_16_neg_<pid>.bin`), only **opens** a write handle (writes nothing) and deletes the fixture. It never touches the real GGUF. Accepted.
- Not run: `cargo fmt --check`, `cargo clippy`, Python tests under `tests/`, frontend tests (none found).

### 5.2 Artifact hashes (recomputed)

| Artifact | Size | SHA-256 | Matches 3B.9 report |
|---|---:|---|---|
| `target/release/nova-chat.exe` (2026-11-06 19:09) | 5,969,408 | `66a97c6157acef444a57f48c7411879d0378d634ad6d0ba5631a6b9be9f0c935` | Yes |
| `NOVA Chat_0.1.0_x64-setup.exe` (19:09) | 2,068,735 | `ca91f535ad84f30606600452a2c1560ffffd1302f1c7e38a3fc8af26f01417d2` | Yes |
| `runtime/windows/llama-cpp/llama-server.exe` | — | `fe2957c5fc9defb76279a513ea58153b5990c14af8e2c9d2150c2d194bf72668` | Yes (pinned value) |
| GGUF `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` | 18,632,184,480 | **not recomputed** (would read 18.6 GB) | Size matches |

No source file is newer than the installer. Installer is still unsigned (earlier check). A leftover **1,362,642,455-byte `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf.part`** sits next to the model; I did not delete it.

---

## 6. Security and lifecycle findings

### 6.1 What is correct (Option A)

`job_object.rs::spawn_child_suspended_with_handle` order:

1. `CreateProcessW(..., bInheritHandles = FALSE, CREATE_SUSPENDED | CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)`
2. `AssignProcessToJobObject` — on failure: `TerminateProcess`, close handles, error.
3. `DuplicateHandle(self → child, lock, DUPLICATE_SAME_ACCESS, inherit FALSE)` — on failure: terminate, error.
4. `ResumeThread` — on failure: terminate, error.

Assessment:
- **Assign before duplicate** is the right order: a parent crash after step 2 closes the only Job Object handle, and `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` kills the suspended child, so no suspended orphan can keep the lock.
- **Share-mode compatibility:** the duplicated handle is the same file object (`GENERIC_READ`, `FILE_SHARE_READ`). The child's later CRT open (`GENERIC_READ`, share `READ|WRITE`) is compatible both ways. Any external open asking for write needs every existing handle to grant `FILE_SHARE_WRITE` → sharing violation. Delete and rename need `DELETE` access, which needs `FILE_SHARE_DELETE` on every handle → blocked. Tests 10–13 confirm write/delete/rename/replace are blocked on fixtures.
- **What the duplicate actually buys:** if NOVA crashes, the Job Object kills the child anyway. The duplicate covers the asynchronous teardown window (child's own permissive handle and mapping still exist until the kernel finishes termination) and any path where the parent's guard is dropped while the child lives. That is a real but narrow benefit, and it is correctly implemented.
- Job handle is created without an inheritable security descriptor and the child gets `bInheritHandles = FALSE`, so the child cannot keep the job alive.

### 6.2 Finding F1 — identity check happens before the lock (TOCTOU) — **fix before live test**

In `load_model` (`lib.rs` ~lines 290–335), step 4c calls `query_file_identity(path)` (opens with share `READ|WRITE|DELETE`, then closes), and only later `spawn_process_with_profile` acquires the restrictive lock if no in-session lock exists (`server_manager.rs:476`). After acquiring a fresh lock, identity is **not re-checked through the locked handle**, even though `ModelFileLockGuard::query_current_identity()` exists for exactly this.

Window: file replaced between step 4c and lock acquisition → new file is locked and loaded. Applies whenever there is no pre-acquired lock, e.g. every session that relies on `PreviouslyVerified` from cache. The path-mismatch branch (`server_manager.rs:473`) has the same problem.

Fix: acquire the lock first, then compare `lock.query_current_identity()` with the verified identity, and pass that same guard to the spawn. Add a test that swaps the fixture between check and lock.

### 6.3 Finding F2 — cache fallback trusts size + mtime only

`evaluate_startup_integrity` (`model_integrity.rs` ~413–483) returns `PreviouslyVerified` when path, size, mtime (seconds) match and the cached hash is non-empty. The Win32 identity comparison runs only if `query_file_identity` succeeds **and** the cache has all identity fields; otherwise it silently skips it. The cached hash is not compared with `EXPECTED_QWEN3_30B_SHA256` here (`load_model` relies on the cache's `is_cryptographically_verified` flag).

Risk: in-place modification with restored mtime (`SetFileTime`) or an older cache format is accepted without rehash. Within a same-user threat model this is a documented limitation, not a break, but it should **fail closed** when identity cannot be compared.

### 6.4 Finding F3 — spawn ignores a failed stop — **fix before live test**

`spawn_process_with_profile` starts with `self.stop_server();` and ignores the `false` result (termination unconfirmed). It then overwrites `self.child`, `self.job_object` and `self.model_lock`. Dropping the old `JobObject` kills the old child via kill-on-close, but the code no longer tracks whether it exited, and a second 18 GB model can start loading while the first is still tearing down. File protection is preserved (the old child's duplicate handle lives until it dies), but the one-model-at-a-time memory invariant is not.

Fix: if `stop_server()` returns false, refuse to spawn and surface `SHUTDOWN_TIMEOUT`.

### 6.5 Finding F4 — two NOVA instances

`lifecycle_lock` is per process. Two NOVA instances can both acquire the read-share lock (compatible) and both start `llama-server` on the same model; the second will also collide on port 18080. Only the admission gates (which can race) stand in the way. Test `phase3b9_14` covers file protection, not memory/port safety.

Fix: single-instance enforcement (`tauri-plugin-single-instance` or a per-user named mutex). New dependency → needs Project Owner approval.

### 6.6 Finding F5 — create → assign window

Between `CreateProcessW` and `AssignProcessToJobObject`, a parent crash leaves a **suspended, unassigned** child. It holds no lock and has never run, so file integrity is safe (confirmed by test 14), but it is a leaked process. Windows supports assigning at creation via `PROC_THREAD_ATTRIBUTE_JOB_LIST` (Windows 10+), which removes the window. Low severity; recommended.

### 6.7 Other observations

| # | Observation | Severity |
|---|---|---|
| O1 | Directory rename / junction swap of `models\qwen3-30b\` while locked is untested; the child opens by **path**. | Medium (unproven) |
| O2 | Model lives under `OneDrive\Desktop`. Attributes show only `A` (not a cloud placeholder), but OneDrive sync/dehydration interacting with the lock is unverified. | Low |
| O3 | API key visible in the child command line to same-user tools (known, accepted). | Low |
| O4 | `confirmation_token` is optional in `load_model`; a missing token skips the match check (authorization is still single-use and expiring). | Low |
| O5 | 60 s readiness timeout may be short for a cold 18.6 GB mmap load from disk; on timeout the server is stopped. | Operational |
| O6 | Error paths after `DuplicateHandle` call `TerminateProcess` without waiting. Safe (more locking, not less) but the caller cannot tell when the file is free again. | Low |

---

## 7. Hardware admission

Code for the six gates exists in `admission.rs` and is invoked fresh inside `load_model`. Thresholds in the handoff (RAM ≥ 10 GiB, commit < 90%, VRAM ≥ 5,500 MiB, no competing GPU compute, disk ≥ 25 GiB, fresh telemetry) were **not** re-read line by line in this pass, and the live audit test is (correctly) ignored. Status: **NOT VERIFIED** against real telemetry. The earlier wording in `docs/architecture.md` that the budget engine "eliminates rigid binary admission gates" has been removed; the doc's only remaining "eliminate" refers to cancellation races.

---

## 8. Installer and runtime

- `tauri.conf.json` has **no `bundle.resources` or `externalBin`**. The installer ships only the shell (2.07 MB). An installed copy outside the repository will not find `llama-server.exe` unless the user sets a runtime path.
- ADR-002 evaluates bundling options but is still **"Proposed"**. A decision is needed.
- Installer unsigned; install/uninstall cycle never validated.

---

## 9. Documentation state

| Item | State |
|---|---|
| Q3 vs Q4 in `docs/architecture.md` | **Fixed** (now states Q4_K_M pinned) |
| ADRs | ADR-001 (Option A), ADR-002 (Proposed) exist; untracked |
| README test count | Now "105 … (1 ignored)" — **matches** |
| README "Phase 3B.10 Security Hardening Complete" | **Premature** — a 3B.10 report exists, but F1–F4 were unaddressed by it |
| README "Fast Startup (< 1s)" | Still present; benchmark includes a fixed 400 ms sleep and no real cold runs |

---

## 10. Commands run

| Command | Type |
|---|---|
| `git rev-parse`, `status`, `diff`, `ls-files`, `remote`, `branch` | Read-only |
| `ls`, `find`, `grep`, `sed -n`, `wc`, `attrib` | Read-only |
| `sha256sum` on exe, installer, `llama-server.exe` | Read-only |
| `cargo test -j 4` | Writes `target/` only |

Not done: model load, `llama-server` start, inference, installer build/install, GGUF rehash, fmt/clippy, Python tests, any commit/push/stash/branch change, any Windows setting change. Only file written: this report.

---

## 11. Next three engineering tasks (priority order)

1. **Close F1 and F3** (small, contained, testable):
   - Lock first, then verify identity through the locked handle; pass the same guard to spawn.
   - Refuse to spawn when the previous stop is unconfirmed.
   - Add fixture tests for both.
2. **Phase 3B.10 audit completion:**
   - Fail closed in the cache path (F2).
   - Add directory-rename and junction tests (O1).
   - Decide on single-instance enforcement (F4, needs dependency approval).
   - Optionally `PROC_THREAD_ATTRIBUTE_JOB_LIST` (F5).
   - Run `cargo fmt --check` and `clippy`.
   - Write `NOVA_CHAT_PHASE3B_10_FINAL_SECURITY_AND_RELEASE_AUDIT.md`.
3. **Make the product honest and usable:**
   - Wire `start_model_integrity_verification` and its status into the UI.
   - Fix the `PREVIOUSLY_VERIFIED` fallback.
   - Remove "3B.10 Complete" and the "< 1s" claim from the README until measured properly.
   - Decide ADR-002 runtime packaging.
   - **Commit the desktop work** (needs Project Owner approval) so it is not only in one working copy.

## 12. Questions for Antigravity

1. Why did the Phase 3B.9 report say Git status was clean when 101 files are untracked?
2. Who changed the README to "Phase 3B.10 Security Hardening Complete", and on what evidence?
3. Is there any UI path that triggers SHA-256 verification for a model with no cache? If not, how was a `CryptographicallyVerified` state reached in earlier reports?
4. Were any earlier UI screenshots produced with `?view=` mock data? If so, which reports include them?
5. Is the `.gguf.part` file safe to delete (Project Owner decision)?

## 13. Verdict

**CONDITIONALLY ACCEPTED** for the Phase 3B.9 model-protection design and Job Object ordering, conditional on fixing F1 and F3 before any live model test. (F1 and F3 are now fixed; see Section 14.)
**NOT VERIFIED** for release readiness. Live inference remains unauthorized.

---

## 14. Update — F1 and F3 fixed (Project Owner approved, 2026-11-06)

### 14.1 F1 — lock first, then verify through the locked handle

- **New function** `model_integrity::acquire_verified_lock(path, existing, expected)`: reuses `existing` only if it guards the same path, otherwise acquires a fresh `GENERIC_READ` / `FILE_SHARE_READ` lock; then reads identity with `guard.query_current_identity()` and checks it with `verify_file_identity`. Returns the guard only if identity matches.
- **`lib.rs` `load_model` step 4c:** replaced the path-based `query_file_identity` + `verify_file_identity` with `acquire_verified_lock`, taking the in-session lock from `active_model_lock` if present. On failure the state becomes `Error` and the load is rejected. The verified guard is passed to `spawn_process_with_profile` as `Some(verified_lock)`, so the file object that was verified is the one that stays locked and is duplicated into the child.
- **`server_manager.rs` path-mismatch branch:** previously acquired a fresh, unverified lock; now fails closed with "Model lock path mismatch … refusing to start unverified model."

Unchanged: when `expected_identity` is `None`, identity comparison is still skipped (same as before). The `spawn_process` wrapper with no guard still acquires an unverified lock; it is used only by tests.

### 14.2 F3 — refuse to spawn when the previous stop is unconfirmed

`spawn_process_with_profile` now checks `self.stop_server()`. If it returns `false`, it returns `Err("Refusing to start llama-server: termination of the previous server is unconfirmed; its model lock is retained. …")` **before** runtime discovery or process creation. The old child handle and model lock stay tracked.

### 14.3 New tests (disposable `%TEMP%` fixtures only; no `llama-server`, no real GGUF)

| Test | What it proves |
|---|---|
| `test_f1_swap_before_lock_is_rejected_by_locked_handle_identity` | File replaced after identity was recorded → rejected with `TOCTOU_PROTECTION_VIOLATION`; no lock left behind |
| `test_f1_unchanged_file_returns_locked_guard` | Unchanged file verifies; returned guard blocks external writes |
| `test_f1_existing_guard_reused_only_for_same_path` | Same-path guard is reused (same handle); different-path guard is released and the target is locked on its own |
| `test_f3_spawn_refused_when_previous_stop_unconfirmed` | With a simulated WAIT_FAILED child, spawn is refused, the old child stays tracked, the lock is retained and the fixture stays write-protected; cleanup stop then succeeds |

### 14.4 Results

Command: `cargo test --lib -j 4` in `desktop/src-tauri`.

```
test tests::tests::test_f1_unchanged_file_returns_locked_guard ... ok
test tests::tests::test_f1_swap_before_lock_is_rejected_by_locked_handle_identity ... ok
test tests::tests::test_f1_existing_guard_reused_only_for_same_path ... ok
test tests::tests::test_f3_spawn_refused_when_previous_stop_unconfirmed ... ok
test result: ok. 109 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 2.11s
```

No compiler warnings. `cargo fmt --check` reports differences across most files (`admission.rs`, `lib.rs`, `memory_budget.rs`, `model_integrity.rs`, `resource_advisor.rs`, `tests.rs`, `examples/crash_safety_helper.rs`); this was already the case before these changes, so the crate was not reformatted. Clippy not run.

Files changed: `desktop/src-tauri/src/model_integrity.rs`, `desktop/src-tauri/src/lib.rs`, `desktop/src-tauri/src/server_manager.rs`, `desktop/src-tauri/src/tests.rs`. Nothing committed.

### 14.5 Still open

- F2 (cache fallback to size + mtime), F4 (single instance), F5 (`PROC_THREAD_ATTRIBUTE_JOB_LIST`), O1 (directory rename / junction).
- `load_model` end-to-end is not unit-tested (it needs a Tauri `AppHandle`); the fix is covered through `acquire_verified_lock` and `spawn_process_with_profile`.
- The release exe and installer were built before these changes; their hashes in Section 5.2 no longer describe the current source.

---

## 15. Update — F2 fixed (Project Owner approved, 2026-11-06)

### 15.1 Change

`model_integrity.rs`: `evaluate_startup_integrity(path, cache)` now delegates to the new `evaluate_startup_integrity_against(path, cache, expected_sha256)` with the pinned `EXPECTED_QWEN3_30B_SHA256`. When path, size and mtime match, the cache is trusted **only if all** of these hold; otherwise the result is `VerificationFailed` and full SHA-256 verification is required:

1. `is_cryptographically_verified` is `true`.
2. The cached digest equals the expected digest (case-insensitive).
3. The current Win32 file identity can be read (previously a read failure was silently ignored).
4. The cache has all identity fields (`volume_serial_number`, `file_index`, `last_write_time_raw`). Legacy caches without them were previously accepted on size + mtime.
5. The cached identity matches the current file (unchanged behavior; still clears the cache on mismatch).

`PreviouslyVerified.file_identity` is now always `Some`, so `load_model`'s lock-then-verify step (F1) always compares against the identity recorded at verification time rather than whatever existed at app startup.

The new failure paths do **not** call `clear_model_cache()`; a later successful verification overwrites the cache.

### 15.2 Tests

New (temp fixtures only):

| Test | Proves |
|---|---|
| `test_f2_complete_matching_cache_is_previously_verified` | A complete, verified, matching cache is trusted and carries an identity |
| `test_f2_cached_digest_not_expected_is_rejected` | Fixture digest is rejected against the pinned Qwen3 digest; a forged digest is rejected |
| `test_f2_cache_without_crypto_flag_is_rejected` | Cache not from full verification is rejected |
| `test_f2_legacy_cache_without_identity_is_rejected` | Legacy cache without identity is rejected (was fail-open) |

Changed: `test_model_cache_same_size_same_mtime_replacement_fails_cryptographic_verification` previously **asserted the fail-open behavior** (legacy cache → `PreviouslyVerified`). It now asserts `VerificationFailed`, and its identity sub-check uses `evaluate_startup_integrity_against` with the fixture digest so it still exercises the identity comparison.

Result (`cargo test --lib -j 4`):

```
test result: ok. 113 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 2.07s
```

No compiler warnings.

### 15.3 Notes

- **No real cache exists today.** `%LOCALAPPDATA%\NOVA Chat\` has no `model_cache.json` (the Phase 3B.11 report said it was "pre-populated"; it is not). With no UI action to start verification, a load through NOVA Chat is currently blocked at the integrity gate. This is fail-closed, but it means a NOVA Chat smoke test needs a "Verify model" UI action first.
- **Test side effect (pre-existing):** the identity-mismatch path calls `storage::clear_model_cache()`, which targets the real `%LOCALAPPDATA%\NOVA Chat\` directory unless `NOVA_DATA_DIR` is set. The same-size/same-mtime test can reach it. Harmless today (no cache), but running the suite after a real verification could delete the user's cache. Recommend routing tests through an isolated data dir.
- Remaining open: F4, F5, UI verification action, UI-2 fallback label.

---

## 16. Update — Option A harness hardening (Project Owner approved, 2026-11-06)

Review of Antigravity's Phase 3C.0 report found that the Python preflight's telemetry gate could **never** pass on this laptop. Three fixes were applied to the Option A harness (Python only; the Rust app is unchanged).

### 16.1 GPU telemetry `[N/A]` (blocker)

- **Cause:** under Windows WDDM, `nvidia-smi --query-compute-apps` reports `used_memory` as `[N/A]` for **every** process. `monitor.py` did `float(p_parts[2])`, which raised on the first row (`dwm`, PID 1128, always present) and failed the whole telemetry gate. It was not a permissions problem as reported.
- **Fix:** `monitor.py` records `[N/A]` / `N/A` memory as `None`; `GPUProcessInfo.used_memory_mib` is now `Optional[float]`. Non-numeric PIDs and any other non-numeric memory value still fail closed.
- **The live sampler used the same parser**, so without this fix a run would also have aborted on its first telemetry sample.

### 16.2 Supervisor hard limits

`ResourcePolicy` gains `max_run_duration_seconds = 600.0` and `critical_gpu_temp_c = 87.0`. `supervise_child_process` now aborts when:
- the child runs longer than the limit (previously a hung child was supervised forever), or
- a sample's GPU temperature is ≥ the limit, or
- a sample's temperature is missing or non-finite (fail closed).

### 16.3 Job Object containment

New `nova/resource_steward/job_containment.py`: `contain_current_process()` puts the harness process in a `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` job **before** `llama-cli.exe` is spawned, so the child is born inside the job (no window). The handle is never closed; the kernel closes it when the harness exits for any reason and kills the child. `execute_inference` fails closed (state `ABORTED`, no spawn) if containment cannot be established.

### 16.4 Tests

New `tests/test_phase3c0_hardening.py` (10 tests): WDDM `[N/A]` rows parse as SUCCESS with unknown memory; numeric memory still parsed; garbage memory and non-numeric PID still fail; policy defaults; wall-clock abort; temperature abort; missing temperature aborts; containment kills a grandchild when its contained parent is killed. A manual negative control confirmed the grandchild **survives** parent death without the job. The shared sample builder in `test_resource_steward.py` now includes `temperature_c`.

```
python -m unittest discover -s tests -p "test_*.py"  ->  Ran 127 tests ... OK
cargo test --lib -j 4                                  ->  113 passed; 0 failed; 1 ignored
```

### 16.5 Live read-only preflight after the fix

```
[PASS] telemetry_integrity   : Healthy
[FAIL] avail_ram             : 7.932 GiB (Req: >= 10.0 GiB)
[PASS] system_commit         : 66.2%
[PASS] avail_vram            : 5591.0 MiB
[FAIL] no_competing_gpu      : 5 active apps (Req: 0 competing compute apps)
[PASS] disk_free             : 98.96 GiB
```

### 16.6 Still blocking (Project Owner decision)

- **GPU contention gate policy.** WDDM lists graphics users as "compute apps": `dwm` (always present), OMEN LightStudio, OMEN Command Center (two processes) and Telegram. The gate requires exactly 0, so it can never pass while `dwm` runs. A rule is needed, for example: exempt `dwm.exe` (system compositor) and treat other listed apps as blocking unless the owner closes them; or require 0 apps after the owner closes OMEN/Telegram and exempt only `dwm`. This is a safety-gate change and needs explicit approval.
- The Rust app counts processes via NVML (`nvmlDeviceGetComputeRunningProcesses_v2`), so it has no `[N/A]` parsing issue, but it likely counts the same processes. Not verified.
- RAM: 7.9 GiB available vs 10 GiB required; the owner must close applications.

---

## 17. Update — GPU contention gate: exempt verified dwm.exe only (Project Owner decision, 2026-11-06)

### 17.1 Rule

A GPU process is exempt from the "no competing GPU apps" gate **only** if its image path is exactly `<system drive device>\<SystemRoot without drive>\System32\dwm.exe` (case-insensitive), e.g. `\Device\HarddiskVolume3\WINDOWS\System32\dwm.exe`. Every other listed process, including any whose path cannot be resolved and any `dwm.exe` elsewhere, still counts as competing (fail closed). The threshold remains 0.

dwm.exe denies `OpenProcess` to ordinary users (error 5), and `nvidia-smi` shows it as `[Insufficient Permissions]`, so the path is read with `NtQuerySystemInformation(SystemProcessIdInformation)`, which needs no process handle, and compared against `QueryDosDeviceW(%SystemDrive%)` + `%SystemRoot%`.

### 17.2 Implementation (both stacks, kept in parity)

- **Python:** new `nova/resource_steward/system_processes.py` (`is_dwm_image_path`, `is_exempt_system_gpu_process`). `memory_budget.py` gate 5 excludes exempt processes, reports `N active apps (K exempt: dwm.exe)`, and names each competing process and PID in the failure reason.
- **Rust:** `admission.rs` gains `is_dwm_image_path`, `query_nt_image_path` (ntdll via `GetModuleHandleW`/`GetProcAddress`), `query_system_drive_device`, `is_exempt_system_gpu_process`. `query_nvml_gpu` now counts only non-exempt PIDs from `nvmlDeviceGetComputeRunningProcesses_v2`.

### 17.3 Tests

- Python (`tests/test_phase3c0_hardening.py`, +5): path rule; gate passes with only verified dwm; gate blocks Telegram alongside dwm and names it; unverified `dwm.exe` name still blocks; live check that every running `dwm.exe` is exempt and the test process / PID 0 are not.
- Rust (`tests.rs`, +2): `test_dwm_exemption_path_rule`; `test_dwm_exemption_live_process_checks` (Toolhelp enumeration of `dwm.exe`, plus self / PID 0 / nonexistent PID not exempt).

```
python -m unittest discover -s tests -p "test_*.py"  ->  Ran 132 tests ... OK
cargo test --lib -j 4                                  ->  115 passed; 0 failed; 1 ignored
```

### 17.4 Live read-only preflight

```
[PASS] telemetry_integrity   : Healthy
[FAIL] avail_ram             : 5.453 GiB (Req: >= 10.0 GiB)
[PASS] system_commit         : 67.4%
[PASS] avail_vram            : 5591.0 MiB
[FAIL] no_competing_gpu      : 4 active apps (1 exempt: dwm.exe)
[PASS] disk_free             : 98.73 GiB
```

Remaining blockers are operational, for the owner before an authorized run: close OMEN LightStudio, OMEN Command Center (two processes) and Telegram, and free RAM to ≥ 10 GiB available. The release exe and installer predate this change and need a rebuild before any NOVA Chat use.
