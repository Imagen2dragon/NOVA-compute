# NOVA Chat — Phase 3B.11 Instructions for Antigravity
## Owner-Readiness Evidence Verification (revised after commit `a6509b7`)

**To:** Antigravity — Implementation Engineer
**From:** Claude — Lead Architect / Independent Reviewer
**CC:** Project Owner, ChatGPT
**Repository:** `Imagen2dragon/NOVA-compute`
**Branch:** `feature/nova-chat-desktop`
**Baseline HEAD:** `a6509b7620f7b477a9b1c733057179991eb9bf99`
**Date:** 2026-11-06

This replaces the earlier Phase 3B.11 draft. It keeps that draft's evidence-only scope and safety limits, updates the baseline, and adds the takeover-audit findings.

---

## 1. What changed since Phase 3B.10

Commit `a6509b7` committed all desktop work and added two fixes approved by the Project Owner:

- **F1 (fixed):** `load_model` (`lib.rs`) now acquires the restrictive model lock first and verifies file identity through the locked handle via the new `model_integrity::acquire_verified_lock`. The verified guard is passed to `spawn_process_with_profile`. A guard for a different path is now refused (`server_manager.rs`) instead of being replaced by an unverified lock.
- **F3 (fixed):** `spawn_process_with_profile` refuses to start when `stop_server()` returns `false` (previous termination unconfirmed), before runtime discovery or process creation.
- 4 new tests: `test_f1_*` (3) and `test_f3_*` (1). Reported result: **109 passed, 0 failed, 1 ignored.**

Your job is to **independently verify** these and the rest of the safety story. Do not take this summary or the takeover audit as proof.

Read first:
- `reports/desktop/NOVA_CHAT_CLAUDE_TAKEOVER_AUDIT.md` (Sections 6 and 14)
- `reports/desktop/NOVA_CHAT_PHASE3B_10_FINAL_SECURITY_AND_RELEASE_AUDIT.md`
- `docs/decisions/ADR-001-option-a-child-owned-file-handle.md`, `ADR-002-installer-runtime-packaging-strategy.md`

---

## 2. Hard limits (unchanged)

**Do not:**
- Start `llama-server.exe` for any reason, including with a dummy file.
- Load, memory-map for inference or write to the real GGUF.
- Generate any token or send any chat request.
- Kill user processes, change Windows settings, registry, pagefile, drivers or CUDA, or run elevated.
- Install or uninstall anything (including the NSIS installer).
- Commit, push, merge, switch branches, reset, stash, or `git clean`.

**Allowed:**
- Read-only source and Git inspection.
- `cargo test`, `cargo fmt --check`, `cargo clippy` (without `--fix`).
- `cargo build --release` and `npm run tauri build` only to refresh artifacts in `target/` (see Section 8).
- Fixture tests in `%TEMP%`.
- Read-only hashing of the real GGUF.
- **Documentation corrections** listed in Section 9 (Markdown only).

**Production code:** do not change it. If you find a defect, document it with file, line and a minimal proposed fix, and stop. The Project Owner decides.

---

## 3. Baseline evidence

Record exactly:

```powershell
git rev-parse HEAD
git branch --show-current
git status --short
git log -3 --format="%H %s"
cargo --version; rustc --version
```

Expected: HEAD `a6509b7…`, clean working tree. If not, record the difference; do not "fix" it.

---

## 4. Verify the F1 and F3 fixes from source

### 4.1 F1
Trace in `lib.rs` `load_model`, from the integrity gate (step 4b) to `spawn_process_with_profile`:
1. Is any file-identity check still done **by path before** the lock? (Expected: no.)
2. Is the lock acquired before identity is compared, and is identity read with `query_current_identity()` on that same guard?
3. Is that same guard passed to the spawn, with no other lock acquired in between?
4. When `expected_identity` is `None`, what happens? State whether this can occur on Windows and whether it should fail closed.
5. In `server_manager.rs`, confirm the path-mismatch branch now returns `Err` and that the `existing_lock = None` branch is only reachable from `spawn_process` (test use). List every caller.

### 4.2 F3
1. Confirm `spawn_process_with_profile` returns `Err` when `stop_server()` returns `false`, before `find_llama_server_binary` and before any `CreateProcessW`.
2. Confirm the old `child`, `job_object` and `model_lock` remain tracked in that case.
3. Confirm `load_model` surfaces this as `AppState::Error` and that a later unload can retry the stop.

### 4.3 Tests
Run in `desktop/src-tauri`:

```powershell
cargo test --lib -j 4
```

Record command, duration, counts, warnings. Expected: 110 test attributes; 109 passed, 0 failed, 1 ignored (`test_live_system_preflight_audit`).

For each of the 4 new tests, state what it proves and what it **does not** prove. In particular: `test_f1_*` exercise `acquire_verified_lock` directly; `load_model` itself is not unit-tested because it needs a Tauri `AppHandle`. Say whether that gap matters.

---

## 5. Re-verify Option A and lifecycle (from the earlier draft)

Do Sections 4–7 of ChatGPT's draft against the current source, with these corrections:

1. **Failure paths do not wait.** In `job_object.rs`, after `AssignProcessToJobObject`, `DuplicateHandle` or `ResumeThread` fails, the code calls `TerminateProcess` and closes handles **without waiting**. The 3B.10 report says the child is "reaped cleanly". Report what the code actually does, and whether the missing wait can cause any unsafe state (e.g. a caller believing the file is free while the dying child still holds the duplicated lock).
2. **Pre-assignment window.** A parent crash between `CreateProcessW` and `AssignProcessToJobObject` leaves a suspended, unassigned child with no lock and no file access. Do not claim it disappears at logout unless you test it. Document `PROC_THREAD_ATTRIBUTE_JOB_LIST` as the future fix; do not implement.
3. For all 16 `test_phase3b9_*` tests plus the 4 new ones, give a table: production spawn path used (yes/no), duplication exercised, real Windows share modes, Job Object behavior, cleanup confirmed.
4. Confirm `test_phase3b9_16` (negative control) only opens a handle on `%TEMP%\nova_p3b9_16_neg_<pid>.bin` and removes it.

---

## 6. Open findings — investigate and report, do not fix

For each: confirm or refute from source, give exact file and line, assess severity, propose the minimum fix, and say whether it blocks a smoke test.

| ID | Finding | What to establish |
|---|---|---|
| F2 | `evaluate_startup_integrity` (`model_integrity.rs`) returns `PreviouslyVerified` on size + mtime when the cache lacks identity fields or `query_file_identity` fails; the cached hash is not compared with the expected constant there. | Does this fail closed? Can a stale cache bypass hashing? |
| F4 | No single-instance enforcement. Two NOVA instances can both lock the model (compatible share mode) and both start `llama-server` on port 18080. | Confirm. Propose `tauri-plugin-single-instance` or a per-user named mutex; state dependency impact (needs owner approval). |
| O1 | Child opens the GGUF by path. Is renaming the parent directory, or replacing it with a junction, blocked while the lock is held? | Test **only** on a fixture directory in `%TEMP%`. |
| O5 | `load_model` stops the server after 60 s if `/health` is not ready. | Estimate whether a cold 18.6 GB load can exceed this (from disk speed evidence, no model load). |
| UI | The frontend never invokes `start_model_integrity_verification`, `get_model_integrity_status` or `cancel_model_integrity_verification`. | How does a user with no valid cache reach a verified state? List all registered but unused commands. |
| UI-2 | `ResourceDashboard.tsx:193` shows `PREVIOUSLY_VERIFIED` when status is missing. | Confirm; propose `UNKNOWN`. |
| UI-3 | `App.tsx` builds a mock `PreflightReport` (fake verified hash, fake RAM/VRAM) when the URL has `?view=`. | Confirm it is unreachable in the installed app. List which report screenshots used `?view=` mock data. |
| Key | `spawn_process_with_profile` returns the API key to `load_model`. | Confirm the key never reaches `ServerStatus`, frontend state, logs or reports. |

---

## 7. Model integrity (read-only)

```powershell
(Get-Item "models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf").Length
Get-FileHash "models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf" -Algorithm SHA256
```

Expected: `18632184480` bytes, SHA-256 `a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32`. This reads 18.6 GB; run it alone, not alongside builds. If it differs, stop and report. Do not touch the leftover `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf.part`.

---

## 8. Six admission gates and no-auto-load

Do Sections 9–11 of ChatGPT's draft: verify each gate's threshold and fail-closed behavior from `admission.rs` and its tests (telemetry freshness, RAM ≥ 10 GiB, commit < 90%, VRAM ≥ 5,500 MiB, GPU contention, disk ≥ 25 GiB), plus no auto-load at startup, single-use expiring authorization, loopback `127.0.0.1:18080`, Bearer auth, `/health` readiness. Quote the exact constants and the test names that cover each.

Note: `confirmation_token` is optional in `load_model`. Say whether the token check should be mandatory.

**Artifacts:** the release exe and installer predate `a6509b7`. Either rebuild them (`cargo build --release` / `npm run tauri build`, output in `target/` only) and record new size and SHA-256, or mark the old hashes as stale. Do not run the installer.

---

## 9. Documentation corrections (allowed, Markdown only)

1. `README.md`: replace "Phase 3B.10 Security Hardening Complete" with an accurate status (3B.10 audited; F1/F3 fixed in `a6509b7`; F2/F4/O1 open).
2. `README.md`: replace "Fast Startup (< 1s)" with "Warm-start window visible in ~50–60 ms (no model loaded); cold start not yet measured." Note the 400 ms sleep in `scripts/benchmark_startup.ps1` is not a UI-ready event.
3. `README.md`: test count → current verified count.
4. `docs/architecture.md`: add the F1 lock-then-verify ordering to the integrity description.

List every doc change in the report. Do not commit.

---

## 10. Deliverable

Create `reports/desktop/NOVA_CHAT_PHASE3B_11_OWNER_READINESS_EVIDENCE_REPORT.md` with:

1. Executive summary and verdict.
2. Baseline (HEAD, branch, status, toolchain).
3. F1 and F3 source verification (Section 4).
4. Option A, lifecycle ordering and failure-path matrix, with the "no wait" correction.
5. Pre-assignment crash limitation.
6. Test results and the 20-test production-equivalence table.
7. Open findings table (Section 6) with severity and "blocks smoke test?".
8. Model size and SHA-256.
9. Admission gates, no-auto-load, loopback/auth, key handling.
10. Artifact status (rebuilt hashes or marked stale).
11. Documentation changes made.
12. Exact commands run, labeled read-only or modifying.
13. Questions for the Project Owner.

Use one verdict: **READY FOR OWNER AUTHORIZATION**, **CONDITIONALLY READY**, **NOT READY**, or **NOT VERIFIED**. Do not choose READY if F2 is confirmed to bypass hashing, or if any finding shows the model can be modified while a process can access it.

---

## 11. Stop

Stop after the report. Do not run the smoke test, start `llama-server` or load weights.

The Project Owner must also decide, separately, the smoke-test scope: **raw `llama-server` CLI** or **through NOVA Chat**. They exercise different code. Include a recommendation in your report, not a decision.
