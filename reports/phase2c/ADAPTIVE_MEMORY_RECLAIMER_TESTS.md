# NOVA Compute — Phase 2C-B: Adaptive Memory Reclaimer (AMR) Test Report

**Document:** AMR Automated Verification & Test Results  
**Test Suite:** `tests/test_resource_steward.py`  
**Total Tests:** 50 Passed, 0 Failed, 0 Errors  
**Execution Time:** ~5.2 seconds  
**Status:** ALL 50 TESTS VERIFIED (Final Ownership & Action Integrity Baseline)

---

## 1. Overview & Verification Strategy

The test suite validates runtime supervision stability, recovery verification, and AMR subsystem behavior using:
1. **Deterministic Monotonic Clocks (`FakeClock`)** to test timing, timers, and cooldown intervals without real sleep delays.
2. **Mock Sensor telemetry** covering boundary conditions (healthy, optimization, constrained, critical, and sensor failures).
3. **Static AST Analysis** programmatically inspecting the codebase across `nova/` and `scripts/` to mathematically guarantee the complete absence of forbidden Windows memory APIs.
4. **End-to-End Admission Integration** verifying that preflight gates strictly block inference when post-reclamation RAM is insufficient.
5. **Controlled Stream EOF Fixture (`test_42`)** proving that in a controlled process-exit / reader-ordering test using a controlled stream and mocked process handle, trailing output arriving after direct child exit is fully retained and fatal markers are caught.
6. **Reader Exception and Timeout Guards (`test_43`, `test_44`)** proving that read failure or timeout yields `UNVERIFIED` rather than `SUCCESS`.
7. **Process-Local Accounting Verification (`test_35`–`test_40`)** proving strict fail-closed handling on failed snapshots, PID mismatches, zero default counters, and requiring private-commit reduction for `RECLAMATION_COMPLETED`.
8. **Enforced Process Ownership & Action Integrity (`test_46`–`test_50`)** proving that AMR exclusively operates on NOVA's runtime PID, rejects caller redirection, requires successful cleanup actions to report completion, and accurately asserts telemetry verification flags.

---

## 2. Comprehensive Test Catalog (50 Tests)

| Test ID | Test Name | Target Subsystem | Key Verification Condition | Result |
| :--- | :--- | :--- | :--- | :--- |
| `test_01` | `test_01_default_invocation_never_starts_child` | CLI Safety | Default mode runs preflight only, never starts child | **PASS** |
| `test_02` | `test_02_dry_run_never_starts_child` | CLI Safety | Dry run validates syntax without spawning process | **PASS** |
| `test_03` | `test_03_failed_preflight_blocks_execution` | Admission Gate | Unmet preflight gates halt execution immediately | **PASS** |
| `test_04` | `test_04_conflicting_cli_modes_rejected` | CLI Safety | Mutually exclusive flags rejected by argparse | **PASS** |
| `test_05` | `test_05_commit_usage_at_90_fails_gate` | Commit Gate | Commit at 90.0% fails admission gate | **PASS** |
| `test_06` | `test_06_missing_or_failed_commit_telemetry_fails_closed` | Commit Gate | Telemetry failure fails commit gate closed | **PASS** |
| `test_07` | `test_07_invalid_commit_counters_fail_closed` | Commit Gate | Zero commit limit, negative bytes, NaN fail closed | **PASS** |
| `test_08` | `test_08_supervisor_ram_telemetry_failure_aborts` | Supervisor | Sensor failure during execution triggers abort | **PASS** |
| `test_09` | `test_09_supervisor_gpu_telemetry_failure_aborts` | Supervisor | GPU sensor failure triggers abort | **PASS** |
| `test_10` | `test_10_supervisor_stale_telemetry_aborts` | Supervisor | > 3.0s without telemetry sample triggers abort | **PASS** |
| `test_11` | `test_11_supervisor_sample_ordering_and_gap_detection` | Supervisor | Sample ID gap (1 -> 3) triggers fail-closed abort | **PASS** |
| `test_12` | `test_12_supervisor_low_ram_recovered_resets_timer` | Supervisor | RAM recovering >= 2.0 GiB resets critical timer | **PASS** |
| `test_13` | `test_13_supervisor_low_ram_sustained_5s_aborts` | Supervisor | RAM < 2.0 GiB sustained 5.2s triggers abort | **PASS** |
| `test_14` | `test_14_supervisor_silent_stdout_does_not_block_telemetry` | Supervisor | Quiet child stdout never blocks telemetry supervision | **PASS** |
| `test_15` | `test_15_supervisor_cuda_oom_detected_from_queue` | Supervisor | CUDA OOM error message triggers immediate abort | **PASS** |
| `test_16` | `test_16_recovery_verification_strictness` | Recovery | Residual VRAM leak and thread timeouts fail recovery | **PASS** |
| `test_17` | `test_17_real_windows_fixture_a_graceful_acknowledgement` | Windows Process | Real Windows child cleanly handles CTRL_BREAK | **PASS** |
| `test_18` | `test_18_real_windows_fixture_b_forced_escalation` | Windows Process | Stubborn child forcefully killed after grace period | **PASS** |
| `test_19` | `test_19_inference_verifier_scenarios` | Verifier | Verifies SUCCESS, UNVERIFIED, FAILED, ABORTED | **PASS** |
| `test_20` | `test_20_supervisor_future_timestamp_aborts` | Supervisor | Sample timestamp in the future (> clock + 0.1) aborts | **PASS** |
| `test_21` | `test_21_supervisor_backward_timestamp_aborts` | Supervisor | Sample timestamp moving backward aborts | **PASS** |
| `test_22` | `test_22_supervisor_invalid_sample_id_types_abort` | Supervisor | Non-int sample IDs (bool, float, str, None) abort | **PASS** |
| `test_23` | `test_23_supervisor_non_finite_metrics_abort` | Supervisor | NaN, negative RAM/VRAM, invalid commit % abort | **PASS** |
| `test_24` | `test_24_commit_consistency_counter_mismatch_fails_gate` | Budget Gate | `committed + avail != total` beyond tolerance fails | **PASS** |
| `test_25` | `test_25_commit_consistency_pct_mismatch_fails_gate` | Budget Gate | `commit_pct` disagrees with byte % > 1.0% fails | **PASS** |
| `test_26` | `test_26_inference_verifier_zero_and_missing_runs_rejected` | Verifier | `0 runs`, missing count, prompt-eval-only rejected as UNVERIFIED | **PASS** |
| `test_27` | `test_27_amr_pressure_detection_levels` | AMR Detector | Classifies NORMAL, OPTIMIZATION, CONSTRAINED, CRITICAL; rejects invalid inputs | **PASS** |
| `test_28` | `test_28_amr_cooldown_throttling_and_reset` | AMR Cooldown | Throttles within 30s; allows after 30s; reset works | **PASS** |
| `test_29` | `test_29_amr_safe_reclaimer_no_exceptions` | AMR Reclaimer | Process-local `gc.collect(2)` runs without exception | **PASS** |
| `test_30` | `test_30_amr_delta_measurement_and_status` | AMR Controller | Verified private commit deltas ($\ge 1\text{ MiB}$ completed vs $< 1\text{ MiB}$ no improvement) | **PASS** |
| `test_31` | `test_31_amr_reports_saved_to_json_across_all_outcomes` | AMR Audit | Atomic JSON reports saved across action, cooldown, normal, and failed | **PASS** |
| `test_32` | `test_32_static_inspection_forbidden_apis_absent` | Static Safety | Programmatic AST audit of all files in `nova/` and `scripts/` | **PASS** |
| `test_33` | `test_33_preflight_auto_reclaim_integration` | Admission Control | AMR integrates into preflight; still gates if RAM < 10 GiB | **PASS** |
| `test_34` | `test_34_supervisor_retains_trailing_output_after_child_exit_and_detects_fatal_marker` | Production Regression | Proves EOF draining, trailing output retention, and trailing CUDA OOM abort | **PASS** |
| `test_35` | `test_35_amr_failed_pre_reclamation_process_snapshot` | AMR Accounting | Failed pre-snapshot sets deltas to None, reports RECLAMATION_UNVERIFIED | **PASS** |
| `test_36` | `test_36_amr_failed_post_reclamation_process_snapshot` | AMR Accounting | Failed post-snapshot sets deltas to None, reports RECLAMATION_UNVERIFIED | **PASS** |
| `test_37` | `test_37_amr_before_after_pid_mismatch` | AMR Accounting | PID mismatch sets deltas to None, reports RECLAMATION_UNVERIFIED | **PASS** |
| `test_38` | `test_38_amr_zero_default_working_set_from_failed_snapshot` | AMR Accounting | Zero default field from failed snapshot never calculates false positive delta | **PASS** |
| `test_39` | `test_39_amr_working_set_decrease_without_private_commit_decrease` | AMR Semantics | Working-set drop alone NEVER classifies COMPLETED; reports NO_MEASURABLE_IMPROVEMENT | **PASS** |
| `test_40` | `test_40_amr_private_commit_decrease_with_both_snapshots_successful` | AMR Semantics | Private commit decrease >= 1 MiB reports RECLAMATION_COMPLETED with verified deltas | **PASS** |
| `test_41` | `test_41_pressure_detector_missing_and_invalid_commit_pct` | AMR Pressure | `commit_pct=None`, bool, str, nan, inf, negative, > 100 all fail-closed (CRITICAL) | **PASS** |
| `test_42` | `test_42_deterministic_delayed_output_arriving_after_direct_child_exit` | EOF Verification | Controlled stream: child exits, trailing output arrives, drained, CUDA OOM aborts | **PASS** |
| `test_43` | `test_43_reader_exception_versus_successful_eof` | Reader Guard | Reader exception sets `read_failed=True`, verifier reports UNVERIFIED | **PASS** |
| `test_44` | `test_44_reader_timeout_results_in_unverified` | Reader Guard | Reader join timeout sets `reader_timed_out=True`, verifier reports UNVERIFIED | **PASS** |
| `test_45` | `test_45_amr_no_measurable_improvement_and_unverified_persistence` | AMR Persistence | Verifies atomic JSON persistence for NO_MEASURABLE_IMPROVEMENT & RECLAMATION_UNVERIFIED | **PASS** |
| `test_46` | `test_46_amr_default_operation_uses_nova_runtime_pid` | AMR Process Ownership | Default AMR operation always queries NOVA's own PID via `os.getpid()` | **PASS** |
| `test_47` | `test_47_amr_caller_cannot_redirect_to_unrelated_pid` | AMR Process Ownership | `evaluate_and_reclaim` does not accept arbitrary `target_pid` overrides | **PASS** |
| `test_48` | `test_48_amr_pre_snapshot_pid_mismatch_produces_unverified` | AMR Process Ownership | Pre-snapshot PID mismatch vs runtime PID reports RECLAMATION_UNVERIFIED | **PASS** |
| `test_49` | `test_49_amr_gc_failure_with_private_commit_drop_produces_unverified` | AMR Action Integrity | Failed GC action reports RECLAMATION_UNVERIFIED even if private commit dropped | **PASS** |
| `test_50` | `test_50_amr_normal_and_cooldown_telemetry_verified_flag_accuracy` | AMR Telemetry Verification | Process telemetry verified strictly requires both valid working set and private commit | **PASS** |

---

## 3. Detailed Static AST Code Inspection (`test_32`)

To eliminate any risk of accidental regressions or prohibited memory cleaning APIs, `test_32` programmatically inspects the Abstract Syntax Tree (AST) across all Python source code in:
- `nova/` (all submodules and files)
- `scripts/` (all CLI utilities and runners)

The scan inspects every function call (`ast.Call`), attribute access (`ast.Attribute`), import statement (`ast.Import`), and from-import statement (`ast.ImportFrom`), verifying that none of the following identifiers exist anywhere in the project:
- `EmptyWorkingSet`
- `K32EmptyWorkingSet`
- `NtSetSystemInformation`
- `MemoryPurgeStandbyList`
- `MemoryFlushModifiedList`
- `SetSystemFileCacheSize`

The scan inspected 12 source files and passed with 100% compliance.
