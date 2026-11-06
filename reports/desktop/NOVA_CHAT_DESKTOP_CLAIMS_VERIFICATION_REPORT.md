# NOVA Chat — Desktop Claims Verification Report

**Prepared by:** Claude (independent verification)
**For:** ChatGPT (Lead Architect) and the Project Owner
**Date:** 2026-11-06
**Branch:** `feature/nova-chat-desktop` (HEAD `7d765f4`, plus uncommitted `desktop/` work)
**Machine:** Intel i7-13700H, 16 GB RAM, NVIDIA RTX 4050 Laptop GPU (6 GB) + Intel Iris Xe, Windows 11 Home 10.0.26200
**Toolchain:** cargo 1.93.1, rustc 1.93.1

---

## 1. Purpose and scope

I independently checked three claims in `README.md` about the NOVA Chat desktop app (`desktop/`, Tauri v2):

1. "56 Rust Unit & Hardening Tests Passing"
2. "Production NSIS Installer Ready"
3. "Fast Startup (< 1s)"

**Out of scope:** no model weights loaded, no `llama-server` started, no inference, no installer build, no install/uninstall, no Windows settings changed, no commits or pushes. The Phase 3B.9 security review (crash-resilient model file integrity) was **not** done here and is still open.

---

## 2. Summary of verdicts

| Claim | Verdict | One-line reason |
|---|---|---|
| 56 Rust tests passing | **Incorrect count; now passing after a test fix** | The suite has 90 tests. As found: 88 passed, 1 failed, 1 ignored (exit 101). After a test-only fix: 89 passed, 0 failed, 1 ignored. |
| Production NSIS installer ready | **Partly true** | Installer exists and matches current source, but it is unsigned and has not been install/uninstall tested. |
| Fast startup (< 1s) | **True as measured, but the method is weak** | "App ready" ≈ 448–513 ms, but that figure includes a fixed 400 ms sleep, and the "cold" runs are not cold. |

---

## 3. Claim 1 — Rust test suite

### 3.1 Count

`desktop/src-tauri/src/tests.rs` contains **90** `#[test]` / `#[tokio::test]` attributes. No other test files were found under `src-tauri/src` or `src-tauri/tests`. The README figure of 56 is out of date.

### 3.2 First run (as found)

Command: `cargo test -j 4` in `desktop/src-tauri` (incremental on an existing 6.9 GB debug `target/`; compile took 13.89 s).

```
test result: FAILED. 88 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 1.69s
error: test failed, to rerun pass `--lib`
exit=101
```

Failing test:

```
test tests::tests::test_shutdown_14_child_suspended_not_assigned_lifecycle ... FAILED
panicked at src\tests.rs:3576:13:
assertion `left != right` failed: CreateProcessW suspended must succeed
```

The failure was **deterministic**: it failed when run alone (`cargo test --lib test_shutdown_14`) and in two further full-suite runs.

Ignored test (expected and correct):

```
test tests::tests::test_live_system_preflight_audit ... ignored, Live system hardware diagnostic; manual invocation only via -- --ignored
```

It points at the real 30B GGUF path, so keeping it manual-only is right.

### 3.3 Root cause

Test 14 launched its helper with raw `CreateProcessW`, `lpApplicationName = NULL`, and a command line containing a **relative forward-slash path**:

```
"target/debug/examples/crash_safety_helper.exe" child "<file>"
```

The helper binary did exist (`target/debug/examples/crash_safety_helper.exe`, 470,528 bytes, 2026-11-06 18:32).

I reproduced the behavior with a small P/Invoke check (process started suspended, then terminated immediately):

| Command-line form | Result |
|---|---|
| `"target/debug/examples/crash_safety_helper.exe" ...` (relative, forward slashes) | **FAIL, error 2 (ERROR_FILE_NOT_FOUND)** |
| `"target\debug\examples\crash_safety_helper.exe" ...` (relative, backslashes) | OK |
| `"\\?\C:\...\crash_safety_helper.exe" ...` (canonical verbatim path) | OK |

Other helper-based tests pass because they launch through Rust's `std::process::Command`, which resolves the path itself.

### 3.4 Production launcher is not affected

The real launcher does not have this bug:

- `server_manager.rs:355` resolves `llama-server.exe` candidates with `std::fs::canonicalize`, which produces an absolute `\\?\C:\...` path.
- `build_command_line` (`server_manager.rs:174`) quotes that absolute path into the command line passed to `job_object.rs::spawn_child_suspended` (`CreateProcessW` at `job_object.rs:251`, also with `lpApplicationName = NULL`).
- My check confirmed that the `\\?\` verbatim form launches successfully.

So the failure was a **test defect**, not evidence of a launcher defect.

### 3.5 Fix applied (approved by the Project Owner)

File: `desktop/src-tauri/src/tests.rs`, in test 14 (around line 3556). Test code only; no production code changed.

```rust
assert!(helper_exe.exists(), "crash_safety_helper.exe must exist");
// CreateProcessW with a null lpApplicationName cannot resolve a relative
// forward-slash path (ERROR_FILE_NOT_FOUND); pass an absolute path, as the
// production launcher does via std::fs::canonicalize.
let helper_exe = std::fs::canonicalize(&helper_exe).expect("Canonicalize helper path");
```

### 3.6 Result after the fix

Command: `cargo test --lib -j 4`

```
test tests::tests::test_shutdown_14_child_suspended_not_assigned_lifecycle ... ok
test result: ok. 89 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 1.91s
```

**What test 14 now proves:** after NOVA's parent lock is dropped, a child process that was created suspended (and never ran) holds no handle to the model file, so the file is writable again. This is exactly the "parent dies before the child opens the file" window — it confirms the window is real and unprotected, which is relevant to Phase 3B.9.

---

## 4. Claim 2 — NSIS installer

### 4.1 Evidence

| Item | Value |
|---|---|
| Path | `desktop/src-tauri/target/release/bundle/nsis/NOVA Chat_0.1.0_x64-setup.exe` |
| Size | 2,067,851 bytes (~2.0 MB) |
| Timestamp | 2026-11-06 18:36 |
| SHA256 | `6107cb2ec0a8aa3ea2f4e0f4ef488a2a137365b6cdc940546286ed5b6c5b1e15` |
| Authenticode | **NotSigned** |
| Release app binary | `target/release/nova-chat.exe`, 5,968,896 bytes, 18:36 |
| Staleness | No file in `desktop/src`, `src-tauri/src`, `Cargo.toml` or `tauri.conf.json` is newer than the installer (checked before the test fix, which touched only test code) |

### 4.2 Configuration (`tauri.conf.json`)

- `bundle.targets: ["nsis"]`, `bundle.active: true`
- `windows.nsis.installMode: "currentUser"` — per-user install, no admin elevation needed (consistent with AGENTS.md).
- Product "NOVA Chat", version 0.1.0, identifier `com.nova.chat`.
- CSP is restrictive: `connect-src` allows only `'self'`, `ipc:` and `ipc.localhost`. No remote endpoints.

### 4.3 Gaps before calling it "production ready"

1. **Not code-signed.** Windows SmartScreen will warn on first run.
2. **No install/uninstall test recorded** (clean install, launch, uninstall, leftover check under `%LOCALAPPDATA%\NOVA Chat\`).
3. **`llama-server.exe` is not bundled** by this config (no `resources`/`externalBin`). The launcher (`server_manager.rs` ~lines 320–390) searches a custom path, an environment-variable directory, relative dev-workspace paths and the exe directory. An installed copy outside the repo may not find the runtime — needs a decision on how the runtime is shipped.
4. Version is 0.1.0; no release notes or update channel.

Suggested wording: "NSIS installer builds (unsigned, per-user); install/uninstall not yet validated."

---

## 5. Claim 3 — Startup time (< 1 s)

### 5.1 Measured numbers

Source: `reports/desktop/startup_benchmark_results.json` (2026-11-06 15:52), produced by `scripts/benchmark_startup.ps1` against `target/release/nova-chat.exe`.

"Cold" runs (5):

| Run | BackendInitMs | WindowVisibleMs | FirstInteractionMs | AppReadyMs | App tree working set (MB) |
|---|---:|---:|---:|---:|---:|
| 1 | 15 | 60 | 68 | 488 | 358.78 |
| 2 | 11 | 47 | 60 | 468 | 359.70 |
| 3 | 13 | 61 | 61 | 466 | 361.64 |
| 4 | 7 | 47 | 47 | 448 | 359.48 |
| 5 | 14 | 61 | 64 | 468 | 357.86 |

Main process working set ≈ 34–37 MB; the app's own WebView2 processes ≈ 323–325 MB. Machine-wide WebView2 (all apps) was 1,141–1,272 MB and is not attributable to NOVA. Warm runs are similar (e.g. warm run 1: window 46 ms, app ready 464 ms). An earlier file, `startup_benchmarks.json` (15:16), shows app-ready 462–513 ms.

### 5.2 Methodology problems

1. **"AppReadyMs" includes a fixed `Start-Sleep -Milliseconds 400`.** It measures `WaitForInputIdle` + 400 ms, not when the UI finished rendering. Real UI-ready time is unmeasured; the window appears in ~47–61 ms.
2. **"Cold" runs are not cold.** The script does a warm-up run first and then only waits 3 s between "cold" runs. No reboot, no file-cache flush, no WebView2 cold start. Both "cold" and "warm" columns are effectively warm.
3. **"BackendInitMs" is just the time for `Process.Start` to return**, not Rust backend initialization.
4. **No model involved.** These numbers say nothing about model load or time-to-first-token.
5. Five runs per mode, no variance/percentile reporting in the excerpt reviewed.

### 5.3 Verdict

"Window visible in ~50–60 ms and interactive in < 100 ms, warm, without a model" is supported. "< 1 s startup" is true under these conditions but the headline number is padded with a sleep and the cold label is misleading.

Suggested fixes:
- Have the frontend emit a "first render complete" event over IPC and time to that instead of sleeping.
- Measure true cold start after a reboot (or after clearing the standby list with an approved tool), reported separately.
- Report median and p95 over ≥ 10 runs.

---

## 6. Other observations from this pass

1. **Doc inconsistency:** `docs/architecture.md` still says "Q3_K_M first"; `configs/models.yaml` records Q4_K_M as selected (Lead Architect decision in Phase 2B).
2. **No decision log:** `docs/decisions/` does not exist, although AGENTS.md requires one. The Q4_K_M choice and the desktop architecture have no decision records.
3. **Wording risk:** the new desktop section of `docs/architecture.md` says the budget engine "eliminates rigid binary admission gates". That reads as softening the fail-closed admission stance used elsewhere and should be reviewed explicitly.
4. **Uncommitted work:** `desktop/`, `docs/architecture/`, `reports/desktop/` and four scripts (`benchmark_startup.ps1`, `capture_ui_screenshots.ps1`, `generate_tensor_placement_report.py`, `inspect_tensors.py`) are untracked on `feature/nova-chat-desktop`. Modified: `.gitignore`, `README.md`, `docs/architecture.md`.
5. **Disk:** C: has ~100 GB free (90% used). `desktop/src-tauri/target` is 6.9 GB.

---

## 7. Relevance to Phase 3B.9 (model file integrity)

Not reviewed in depth here, but this pass produced two relevant facts:

- **Test 14 confirms the gap:** once the parent lock is released and before the child opens the GGUF, nothing protects the file from external write access.
- The child is created with `CREATE_SUSPENDED | CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS` (`job_object.rs`) and assigned to a Job Object afterwards. The behavior when the parent crashes between creation and assignment, and the child's own GGUF share mode, still need the independent analysis requested for Phase 3B.9.

---

## 8. Commands run

| Command | Type |
|---|---|
| `ls`, `find`, `cat`, `head`, `grep`, `sed -n`, `du`, `df` | Read-only |
| `git log`, `git diff`, `git status` | Read-only |
| `sha256sum` on the installer; `Get-AuthenticodeSignature` | Read-only |
| `cargo test -j 4`, `cargo test --lib ...` (×4) | Modifying build output in `target/` only |
| PowerShell P/Invoke `CreateProcessW` check (helper started suspended, terminated immediately, invalid file arg) | Spawned and killed a test helper; no system changes |
| Edit to `desktop/src-tauri/src/tests.rs` (test 14 only) | Modifying (approved by Project Owner) |

Not run: model loading, `llama-server`, inference, installer build or install, any admin or system-settings change, commit, push.

---

## 9. Resource impact

- Incremental Rust test builds: ~14 s compile each, test runtime ~2 s. No noticeable pressure.
- No downloads. No model files touched.

---

## 10. Not yet verified

1. Installer install / launch / uninstall cycle and leftovers.
2. How `llama-server.exe` is located from an installed (non-repo) location.
3. True cold startup time and real UI-ready time.
4. The README still says "56 tests"; it should say "89 passing, 1 ignored (manual live-hardware audit)". Not edited.
5. The test fix is uncommitted (all of `desktop/` is untracked).
6. Phase 3B.9 crash-resilient model integrity review.

---

## 11. Recommended next steps (smallest first)

1. Update the README: test count (89 passing, 1 ignored) and installer wording (unsigned, not install-tested).
2. Review and commit the `desktop/` work, including the test 14 fix.
3. Create `docs/decisions/` with records for the Q4_K_M selection and the desktop architecture; fix the Q3/Q4 text in `docs/architecture.md`.
4. Fix the startup benchmark (render-complete event instead of sleep; real cold runs; median/p95).
5. Run a supervised install/uninstall test of the NSIS installer and decide how the runtime binary ships.
6. Proceed with the Phase 3B.9 independent security review before any live smoke test.

## 12. Questions for the Lead Architect

1. Should the shipped app bundle `llama-server.exe` (via Tauri `resources`/`externalBin`) with a pinned hash check, or require a separately installed runtime?
2. Is code signing in scope for 0.1.0, or should the README drop "production" until it is?
3. Is "eliminates rigid binary admission gates" an approved change to the admission policy, or should the doc be reworded to keep fail-closed gates?
4. Should startup benchmarks gate releases, and if so, which metric (window visible, first render, or time-to-model-ready)?
