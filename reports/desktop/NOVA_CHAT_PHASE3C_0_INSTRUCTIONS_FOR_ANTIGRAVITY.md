# NOVA Compute — Phase 3C.0 Instructions for Antigravity
## Smoke-Test Readiness Package (Option A, preparation only)

**To:** Antigravity — Implementation Engineer
**From:** Claude — Lead Architect / Independent Reviewer
**CC:** Project Owner, ChatGPT
**Branch:** `feature/nova-chat-desktop`
**Baseline HEAD:** `da5003c6c27ceb6f6642670f4a5b998253827639`
**Date:** 2026-11-06

---

## 1. Where we are

- F1, F3 (commit `a6509b7`) and F2 (commit `da5003c`) are fixed. Rust suite: **113 passed, 0 failed, 1 ignored**.
- O1 (parent-directory rename while locked) is closed: blocked with "access denied", independently re-tested on a `%TEMP%` fixture.
- **There is no `model_cache.json`** in `%LOCALAPPDATA%\NOVA Chat\`, and the UI has no "Verify model" action. A load through NOVA Chat (Option B) is therefore blocked at the integrity gate. That is fail-closed and correct, but it means **the first token test must be Option A: the standalone CLI harness.**
- The Project Owner has **not** authorized any model load. This phase prepares the test; it does not run it.

## 2. Key fact you must address

The Option A harness `scripts/run_inference_with_telemetry.py` launches **`llama-cli.exe`**, not `llama-server`, from Python. From a first read:

- It does **not** use the Rust model lock (Option A duplicated handle) or a Job Object. `nova/resource_steward/supervisor.py` has no Job Object or file-lock code.
- It uses `CREATE_NEW_PROCESS_GROUP` and `BELOW_NORMAL_PRIORITY_CLASS`.

So **none of the NOVA Chat file-protection and containment guarantees verified in Phases 3B.9–3B.11 apply to Option A.** That is acceptable for a one-token runtime check, but it must be stated plainly and its risks handled (Section 5).

## 3. Hard limits

**Do not:**
- Run `--execute` or `--guided-start`, or start `llama-cli.exe` / `llama-server.exe` with the real GGUF for any reason.
- Load, map or write the real GGUF. Generate no tokens.
- Kill user processes, change Windows settings, run elevated, install anything.
- Commit, push, merge, switch branches, reset, stash or `git clean`.

**Allowed:**
- Read-only source inspection; `git` read commands.
- `python scripts/run_inference_with_telemetry.py --preflight`, `--dry-run`, `--memory-report`, `--advisor` **after reading the code paths each mode runs and confirming none starts `llama-cli`**. Quote the code that proves it before running each.
- Python unit tests for `nova/` (`python -m pytest tests -q`), Rust `cargo test --lib`.
- The test-only and Markdown changes in Section 6.

If you find a production defect, document it with file, line and a minimal fix, and stop.

## 4. Audit the Option A harness

Report, with file and line for each:

1. **Exact command** built by `get_workspace_safe_command()`. Confirm it matches Configuration D (`-ngl 48 -ncmoe 40 -c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap -n 1`) and that `-ncmoe 40` means 40 MoE **layers** on CPU.
2. **Runtime binary:** path, SHA-256 of `llama-cli.exe` and its DLLs vs any pinned manifest. State if no pin exists for `llama-cli.exe`.
3. **Authorization flow:** how `--execute` requires `GuidedStartupController` / `READY_FOR_AUTHORIZATION`. Can it run non-interactively? Is approval single-use?
4. **Preflight gates:** which gates the Python steward enforces, their thresholds, and whether they match the Rust six gates (RAM ≥ 10 GiB, commit < 90%, VRAM ≥ 5,500 MiB, GPU contention, disk ≥ 25 GiB, fresh telemetry). List every mismatch.
5. **Integrity:** does the harness verify the GGUF SHA-256 or size before launch? If not, say so.
6. **Containment:** what happens to `llama-cli.exe` if the Python process crashes or is closed? Is there any mechanism that kills it? Expected answer: no; it would keep running and holding memory.
7. **Abort logic:** what telemetry triggers a stop (RAM, commit, VRAM, temperature, timeout), how it stops the child, and whether it waits for exit.
8. **Outputs:** which files it writes (`reports/phase2c/first_inference/...` by default) and whether any of them would contain secrets or user data.
9. **Measurements:** confirm it records model load time, prefill and decode separately, peak RAM, peak VRAM, commit, and marks the run cold or warm. List what is missing.

## 5. Risk assessment and proposals (do not implement)

For each, give severity, whether it blocks the smoke test, and the smallest mitigation:

| Risk | Note |
|---|---|
| Orphaned `llama-cli.exe` if the harness dies | Propose: wrap the child in a Job Object with `KILL_ON_JOB_CLOSE` (Python `ctypes` or `pywin32`), **or** an operator procedure to end it from Task Manager. State cost and risk of each. |
| No model lock during the run | Same-user modification during a one-token run is unlikely; propose either a read-share lock held by the harness for the run, or accept and document. |
| No pre-run integrity check | Propose a size check plus the existing full SHA-256 run alone beforehand (Phase 3B.11 already confirmed `a015794b…5d32`). |
| 16 GB RAM vs ~13.5 GiB CPU-resident experts | Expect heavy paging. Define a hard stop: commit ≥ 90% or available RAM < 1 GiB for > 10 s → stop. |
| OneDrive folder | Confirm OneDrive will not sync or dehydrate the GGUF during the run (file attributes, OneDrive status). Read-only checks only. |
| Thermal / power | Laptop must be on AC power; record GPU temperature and clocks; define a stop temperature. |

## 6. Small approved changes

1. **Test isolation (test code only):** some Rust tests can reach `storage::clear_model_cache()`, which targets the real `%LOCALAPPDATA%\NOVA Chat\` directory. The known path is the identity-mismatch branch hit by `test_model_cache_same_size_same_mtime_replacement_fails_cryptographic_verification`. Make the tests that can reach it use an isolated data directory **without** a process-wide environment variable race (tests run in parallel). If that needs a production-code change (for example passing a directory into the clear function), **propose it, don't make it.**
2. **README:** O1 is closed; update the status line. Test count → current verified count.
3. Do not touch any other file except your report.

## 7. Write the operator runbook

Create `docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md` containing:

1. **Purpose and scope:** one token, Configuration D, `llama-cli`, Option A. Not NOVA Chat. Not a benchmark.
2. **Who runs it:** the Project Owner or Antigravity **only after written owner authorization** naming this runbook and its commit.
3. **Preconditions checklist:** AC power; browsers, IDEs and other GPU apps closed by the owner (NOVA never closes them); `--preflight` passes all gates within 2 minutes of the run; model size verified; no other NOVA or llama process running.
4. **Exact commands**, in order, copy-pasteable.
5. **Live monitoring:** what to watch and where (Task Manager, `nvidia-smi`, harness output).
6. **Stop conditions** and exactly how to stop, including ending `llama-cli.exe` from Task Manager if the harness has died.
7. **Success criteria:** one token produced; process exits by itself; RAM, commit and VRAM return to within 5% of pre-run values within 60 s; GGUF size unchanged afterwards.
8. **What to record:** load time, prefill and decode reported separately, peak RAM/commit/VRAM, GPU temperature, cold or warm, exact runtime and model hashes, commit ID.
9. **After the run:** do not repeat or extend. Report back. Any second run needs new authorization.

## 8. Deliverable

Create `reports/desktop/NOVA_CHAT_PHASE3C_0_SMOKE_TEST_READINESS_REPORT.md` with:

1. Executive summary and verdict.
2. Baseline (HEAD, status, toolchain, Python version).
3. Harness audit (Section 4), with code citations.
4. Outputs of `--preflight` / `--dry-run` (and any other read-only mode run), quoted.
5. Python and Rust test results.
6. Risk table (Section 5) with proposals.
7. Changes made (Section 6), with diffs summarized.
8. The runbook path.
9. Exact commands run, labeled read-only or modifying.
10. Questions for the Project Owner, at least:
    - Approve the Job Object wrapper for the harness before the test, or accept the manual-stop procedure?
    - Who runs the test: Owner or Antigravity?
    - Is the current preflight result acceptable, or must RAM be freed first?

Verdicts: **READY FOR OWNER AUTHORIZATION**, **CONDITIONALLY READY**, **NOT READY**, or **NOT VERIFIED**.

## 9. Stop

Stop after the report and runbook. Do not run the smoke test. The Project Owner authorizes it separately, in writing, naming the runbook and commit.
