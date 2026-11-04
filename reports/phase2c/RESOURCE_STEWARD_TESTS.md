# NOVA Compute — Phase 2C Resource Steward Test Suite Report

**Document ID:** `NOVA-REP-P2C-STEWARD-TESTS-01`  
**Date:** 2026-11-04  
**Author:** Antigravity (Implementation Engineer)  
**Governance:** Implementation submitted; **Awaiting Lead Architect (ChatGPT) & Project Owner Review**  
**Status:** ALL 18 TESTS PASSING (18/18)  

---

## 1. Test Suite Overview

An expanded, automated test suite was implemented in [`tests/test_resource_steward.py`](file:///c:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/tests/test_resource_steward.py) to validate the second follow-up remediation.

The test suite validates:
1. **CLI Execution & Mutual Exclusivity:** Verifies that default invocation and `--dry-run` never spawn child processes, conflicting flags are rejected, and preflight gate failures immediately block `Popen`.
2. **Preflight Safety Gates:** Tests RAM, VRAM, Disk, GPU process query, and System Commit (90% threshold and fail-closed handling on missing telemetry).
3. **Supervisor Telemetry Integrity:** Tests fail-closed handling of RAM and GPU telemetry dropouts, stale sample timeouts, deduplicated sample IDs, and monotonic timer elapsed bounds.
4. **Child Process Lifecycle & Windows Teardown:** Tests asynchronous output draining, CUDA OOM queue interception, real Windows process group creation (`CREATE_NEW_PROCESS_GROUP`), console control break, timeout escalation, and pipe handle closure.
5. **Recovery Verification:** Verifies that failed post-run GPU queries or failed residual process queries cannot produce a successful recovery report.

---

## 2. Test Execution Command & Output

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

### Execution Log

```
Ran 18 tests in 4.602s

OK
[MODE] EXECUTE AUTHORIZED INFERENCE

Running fresh preflight check immediately prior to launch...
========================================================================
NOVA Compute — Preflight Hardware Qualification Audit
Timestamp (UTC): 2026-11-04T09:32:17.272349+00:00
========================================================================
  [PASS] telemetry_integrity   : Healthy (Req: All sensors SUCCESS)
  [FAIL] avail_ram             : 8.500 GiB (Req: >= 10.0 GiB)
         Reason: Available RAM deficit: 1.500 GiB below required 10.0 GiB
  [PASS] system_commit         : 40.0% (Req: < 90.0%)
  [PASS] avail_vram            : 5900.0 MiB (Req: >= 5500 MiB)
  [PASS] no_competing_gpu      : 0 active apps (Req: 0 competing compute apps)
  [PASS] disk_free             : 100.00 GB (Req: >= 25.0 GB)
------------------------------------------------------------------------
PREFLIGHT STATUS: GATE CONDITIONS UNMET — INFERENCE GATED
========================================================================

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
EXECUTION ABORTED: Preflight safety gates failed. Halting launch.
Per strict safety governance: Inference cannot start when hardware gates fail.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
[RECOVERY] Sending CTRL_BREAK_EVENT to NOVA child PID 5748 (grace: 2.0s)...
[RECOVERY] NOVA child PID 5748 exited with code 3221225794 via CTRL_BREAK.
[RECOVERY] Sending CTRL_BREAK_EVENT to NOVA child PID 99991 (grace: 0.2s)...
[RECOVERY] CTRL_BREAK_EVENT failed: [WinError 87] The parameter is incorrect. Falling back to terminate().
[RECOVERY] Invoking proc.terminate() on child PID 99991 (grace: 0.2s)...
[RECOVERY] Child PID 99991 did not exit within 0.2s. Escalating to forced termination (kill)...
[RECOVERY] NOVA child PID 99991 forcefully terminated.
```

---

## 3. Detailed Test Matrix (18 Scenarios)

| Test ID | Scenario | Input State / Fixture | Expected Behavior | Result |
| :--- | :--- | :--- | :--- | :--- |
| **01** | `test_01_default_invocation_never_starts_child` | CLI invocation with zero arguments | Executes preflight only; never launches child process | **PASS** |
| **02** | `test_02_dry_run_never_starts_child` | CLI invocation with `--dry-run` | Validates configuration and files; returns exit code 0 without launch | **PASS** |
| **03** | `test_03_failed_preflight_blocks_execution` | Preflight report with RAM deficit (8.5 GiB) | Halts launch immediately; `Popen` is not invoked; exits code 1 | **PASS** |
| **04** | `test_04_conflicting_cli_modes_rejected` | Mutually exclusive flags (`--execute --dry-run`) | `argparse` rejects invocation with error exit code | **PASS** |
| **05** | `test_05_commit_usage_at_90_fails_gate` | Commit load at 90.5% | `system_commit` gate evaluates to `False`; halts preflight | **PASS** |
| **06** | `test_06_missing_commit_telemetry_fails_closed` | Commit telemetry status `FAILED` | `system_commit` gate evaluates to `False` (`UNAVAILABLE`) | **PASS** |
| **07** | `test_07_ram_telemetry_failure_during_execution_aborts` | Live sampler reports `s_ram["status"] = FAILED` | Live supervisor triggers immediate fail-closed abort | **PASS** |
| **08** | `test_08_gpu_telemetry_failure_during_execution_aborts` | Live sampler reports `s_gpu["status"] = FAILED` | Live supervisor triggers immediate fail-closed abort | **PASS** |
| **09** | `test_09_stale_telemetry_causes_safe_abort` | No fresh sample received for $> 3.0\text{s}$ | Live supervisor detects hung sampler and triggers fail-closed abort | **PASS** |
| **10** | `test_10_repeated_sample_does_not_advance_timer` | Sample with duplicate `sample_id` | Supervisor drops duplicate sample without advancing timer | **PASS** |
| **11** | `test_11_low_ram_sustained_triggers_abort` | Monotonic timer exceeds 5.0s with RAM < 2.0 GiB | Supervisor triggers critical low RAM abort | **PASS** |
| **12** | `test_12_silent_child_output_does_not_block` | Child stdout is silent (empty queue) | Supervisor continues sampling telemetry without blocking | **PASS** |
| **13** | `test_13_cuda_oom_detected_from_queue` | `CUDA error: out of memory` in output queue | Drained immediately; triggers supervisor abort | **PASS** |
| **14** | `test_14_failed_post_gpu_telemetry_fails_recovery` | Post-run GPU query returns `status = FAILED` | `recovery_successful = False`, `gpu_verification_status = "FAILED"` | **PASS** |
| **15** | `test_15_failed_residual_process_check_fails_recovery` | `tasklist` check returns non-zero error code | `recovery_successful = False`, `process_verification_status = "UNKNOWN"` | **PASS** |
| **16** | `test_16_process_pipes_closed_after_termination` | Process handle with open stdio pipes | `close_process_pipes()` closes stdout and stderr | **PASS** |
| **17** | `test_17_malformed_gpu_process_rows_fail_closed` | `nvidia-smi` emits malformed CSV row | Sets `status = FAILED`, `compute_processes = None` | **PASS** |
| **18** | `test_18_safe_child_fixture_termination` | Real owned Python subprocess fixture | Tests console `CTRL_BREAK_EVENT` and timeout escalation to `kill()` | **PASS** |

---

## 4. Verification Evidence

- **Real Subprocess Teardown Tested:** Test 18 spawned benign Python subprocesses in dedicated process groups to verify that `CTRL_BREAK_EVENT` cleanly breaks Windows processes and that uncooperative processes escalate to `kill()` without orphan processes remaining.
- **Fail-Closed Verification:** Evaluated cases where telemetry returns empty lists or exceptions. Malformed CSV rows and unparseable fields consistently produce `TelemetryStatus.FAILED` and halt execution.
- **Mutual Exclusivity:** Verified that conflicting arguments cannot bypass preflight or execute in dry-run mode.
