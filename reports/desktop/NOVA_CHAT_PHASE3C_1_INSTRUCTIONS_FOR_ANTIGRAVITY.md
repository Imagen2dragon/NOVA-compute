# NOVA Compute — Phase 3C.1 Instructions for Antigravity
## Runbook Reconciliation and Final Pre-Run Evidence (no model load)

**To:** Antigravity — Implementation Engineer
**From:** Claude — Lead Architect / Independent Reviewer
**CC:** Project Owner, ChatGPT
**Branch:** `feature/nova-chat-desktop`
**Baseline HEAD:** `cae2a811baf14eccfb3de1c7a03af1132a46f010`
**Date:** 2026-11-06

---

## 1. What changed since Phase 3C.0 (commit `cae2a81`)

- **GPU telemetry:** `[N/A]` per-process memory (always reported under Windows WDDM) is now "unknown", not malformed. The telemetry gate passes on this laptop.
- **GPU contention gate (owner decision):** only `dwm.exe` whose image path is verified as `%SystemRoot%\System32\dwm.exe` is exempt. Everything else still blocks. Applied in Python (`nova/resource_steward/system_processes.py`, `memory_budget.py`) and Rust (`admission.rs`).
- **Supervisor:** aborts after **600 s**, at **GPU ≥ 87 °C**, or on a missing temperature reading (`policy.py`, `supervisor.py`).
- **Containment:** the harness now joins a **kill-on-close Job Object before spawning `llama-cli.exe`** (`nova/resource_steward/job_containment.py`); it refuses to spawn if that fails.
- Tests: Python 132 passed; Rust 115 passed, 0 failed, 1 ignored.

The runbook `docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md` was written against `da5003c` and is now **out of date**. This phase fixes that and collects final evidence. It does **not** run the test.

## 2. Hard limits (unchanged)

**Do not** run `--execute` or `--guided-start`, start `llama-cli.exe` or `llama-server.exe`, load or map the GGUF, generate tokens, kill or close user processes, change Windows settings, run elevated, install anything, or commit/push/merge/switch branches/reset/stash/clean.

**Allowed:** read-only source and Git inspection; `--preflight`, `--dry-run`, `--memory-report`, `--advisor`; `python -m unittest discover -s tests -p "test_*.py"`; `cargo test --lib`; read-only hashing; edits to the runbook and your report only.

## 3. Verify the new code (read-only)

Confirm from source, with file and line:

1. `execute_inference` calls `contain_current_process()` **before** `subprocess.Popen`, and returns without spawning if it raises.
2. `job_containment.py` sets `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, assigns the **current** process, and never closes the handle.
3. Whether the harness or its launcher is already inside another Job Object that forbids nesting (Windows 8+ allows nesting). If `AssignProcessToJobObject` could fail in the owner's normal terminal, say so; the run would then stop safely before spawning.
4. The supervisor's three new aborts and that each leads to `SafeReclamationManager.terminate_child_process` (CTRL_BREAK → wait → kill → wait).
5. The dwm rule in both stacks matches exactly, and that an unresolvable PID is never exempt.

Run both test suites and record exact counts.

## 4. Reconcile the runbook

Update `docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md`:

1. **Baseline commit** → `cae2a81…`.
2. **Containment:** replace "no Job Object" with: the harness is contained; if Python dies, Windows kills `llama-cli.exe`. Keep the Task Manager procedure **only as a fallback** if a `llama-cli.exe` is still visible 10 s after the harness has exited.
3. **Temperature:** the code aborts at 87 °C; the runbook's manual stop says 82 °C. Keep the **stricter 82 °C as the operator's manual stop** and state that the code's automatic abort is 87 °C. Do not change the code value.
4. **Time limit:** add "automatic abort after 600 s".
5. **GPU contention preconditions:** list the apps the owner must close, from a fresh `--preflight` (currently OMEN LightStudio, OMEN Command Center ×2 processes, Telegram), and state that `dwm.exe` is exempt and must **not** be closed.
6. **RAM precondition:** ≥ 10 GiB available at `--preflight` time. Note that closing Claude Desktop ends any open Claude session.
7. **Success criteria** unchanged (one token, clean self-exit, RAM/commit/VRAM back within 5% in 60 s, GGUF size unchanged), plus: no `llama-cli.exe` left in Task Manager.
8. **Record:** commit `cae2a81…`, `llama-cli.exe` and `llama-cli-impl.dll` SHA-256, model size and SHA-256, cold or warm, load time, prefill and decode reported separately, peak RAM/commit/VRAM/GPU temperature, abort reason if any.
9. Keep "one execution attempt per authorization".

## 5. Final pre-run evidence

1. `git rev-parse HEAD`, `git status --short` (expect clean except your two files).
2. `--dry-run` output.
3. `--preflight` output, quoted in full. Expected: RAM and GPU-contention gates fail until the owner closes apps; all others pass. Name every competing process and PID it reports.
4. Runtime hashes: `llama-cli.exe`, `llama-cli-impl.dll`, `llama.dll`, `ggml*.dll`, CUDA DLLs. Compare with your Phase 3C.0 table and flag any change.
5. Model size (no full rehash needed; 3B.11 confirmed the SHA-256 at `a015794b…5d32`).

## 6. Deliverable

`reports/desktop/NOVA_CHAT_PHASE3C_1_PRE_RUN_EVIDENCE_REPORT.md`:

1. Verdict: **READY FOR OWNER AUTHORIZATION**, **CONDITIONALLY READY**, **NOT READY**, or **NOT VERIFIED**. "Ready" may depend only on the owner closing apps and freeing RAM; any code or evidence gap makes it Conditionally Ready or worse.
2. Section 3 findings with citations.
3. Test results.
4. Runbook diff summary.
5. Section 5 evidence.
6. The owner authorization text below, filled in with the exact commit.
7. Commands run, labeled read-only or modifying.

Stop after the report.

---

## Appendix — Owner authorization text (for the Project Owner to send, not for Antigravity to assume)

> I, the Project Owner, authorize **one** execution of the Configuration D single-token smoke test (Option A, `llama-cli.exe` via `scripts/run_inference_with_telemetry.py --execute`) exactly as written in `docs/runbooks/PHASE3C_OPTION_A_SINGLE_TOKEN_SMOKE_TEST.md` at commit `<commit>`.
> Operator: **<Project Owner / Antigravity>**.
> This authorizes a single attempt, `-n 1`, no retries, no parameter changes, no NOVA Chat or `llama-server` use, and no benchmarking. If any stop condition triggers, stop and report; do not re-run without new written authorization.
