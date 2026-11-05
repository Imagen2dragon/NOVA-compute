"""
NOVA Compute — Phase 2C-B: Controlled Inference Runner & Resource Steward CLI
Enforces strict fail-closed safety gates, mutually exclusive command-line modes,
ordered telemetry sample queues, decoupled supervisor integration, truthful recovery verification,
workspace preservation, and safe reclamation.

EXECUTION MODES:
  Default (no args) : PREFLIGHT ONLY (Exit code 0 on pass, 1 on unmet gates; NEVER launches inference)
  --preflight       : Explicit read-only hardware and process diagnostics
  --execute         : Executes inference ONLY if a fresh live preflight passes all gates
  --memory-report   : Generates detailed memory and recommendation report
  --dry-run         : Validates command syntax and file existence without starting llama-cli
  --guided-start    : Interactive guided low-memory startup and progressive admission
"""

import os
import sys
import time
import re
import argparse
import subprocess
import json
import queue
import threading
from typing import Optional, List, Dict, Any, Tuple

# Ensure project root is in sys.path and anchor all paths to PROJECT_ROOT
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from nova.resource_steward import (
    ResourceSteward,
    ResourcePolicy,
    DEFAULT_POLICY,
    ExecutionMode,
    TelemetryStatus,
    ProcessCategory,
    MemoryAdvisor,
    PreflightReport,
    RecoveryReport,
    SupervisorResult,
    InferenceOutcome,
    LiveTelemetrySampler,
    SafeReclamationManager,
    supervise_child_process,
    StartupState,
    GuidedStartupController
)

LLAMA_CLI = os.path.join(PROJECT_ROOT, "runtime", "windows", "llama-cpp", "llama-cli.exe")
MODEL_GGUF = os.path.join(PROJECT_ROOT, "models", "qwen3-30b", "Qwen_Qwen3-30B-A3B-Q4_K_M.gguf")

OUTPUT_DIR = os.environ.get("NOVA_REPORT_DIR", os.path.join(PROJECT_ROOT, "reports", "phase2c", "first_inference"))
PREFLIGHT_LATEST_JSON = os.path.join(OUTPUT_DIR, "PREFLIGHT_LATEST.json")
TELEMETRY_JSON = os.path.join(OUTPUT_DIR, "RESOURCE_TELEMETRY.json")
EXECUTION_LOG = os.path.join(OUTPUT_DIR, "EXECUTION_LOG.txt")
LOAD_RESULTS = os.path.join(OUTPUT_DIR, "LOAD_RESULTS.md")
FAILURE_ANALYSIS = os.path.join(OUTPUT_DIR, "FAILURE_ANALYSIS.md")

# Windows Process Creation Flags
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NEW_PROCESS_GROUP = 0x00000200

def get_workspace_safe_command() -> List[str]:
    """Generates the verified Configuration D command with Workspace-Safe constraints."""
    return [
        LLAMA_CLI,
        "-m", MODEL_GGUF,
        "-ngl", "48",
        "-ncmoe", "40",
        "-c", "512",
        "-b", "64",
        "-ub", "16",
        "-ctk", "q8_0",
        "-ctv", "q8_0",
        "-fa", "auto",
        "-lm", "mmap",
        "-n", "1",
        "--simple-io",
        "--perf",
        "-p", "Hello"
    ]

def save_preflight_json(report: PreflightReport, path: str = PREFLIGHT_LATEST_JSON):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    gates_dict = {}
    for k, g in report.gates.items():
        gates_dict[k] = {
            "passed": g.passed,
            "required": g.required_str,
            "measured": g.measured_str,
            "failure_reason": g.failure_reason
        }
    data = {
        "timestamp_utc": report.timestamp_utc,
        "all_gates_passed": report.passed,
        "gates": gates_dict,
        "system_ram": {
            "avail_phys_gib": report.system_ram.avail_phys_gib,
            "total_phys_gib": report.system_ram.total_phys_gib,
            "committed_gib": report.system_ram.committed_gib,
            "commit_total_gib": report.system_ram.commit_total_gib,
            "commit_pct": report.system_ram.commit_pct,
            "memory_load_pct": report.system_ram.memory_load_pct,
            "system_cache_bytes": report.system_ram.system_cache_bytes,
            "pages_input_per_sec": report.system_ram.pages_input_per_sec,
            "pages_output_per_sec": report.system_ram.pages_output_per_sec,
            "status": report.system_ram.status.value,
            "error": report.system_ram.error_message
        },
        "gpu": {
            "name": report.gpu.name,
            "vram_total_mib": report.gpu.vram_total_mib,
            "vram_free_mib": report.gpu.vram_free_mib,
            "vram_used_mib": report.gpu.vram_used_mib,
            "utilization_pct": report.gpu.utilization_pct,
            "temperature_c": report.gpu.temperature_c,
            "power_draw_w": report.gpu.power_draw_w,
            "driver_version": report.gpu.driver_version,
            "active_compute_apps": len(report.gpu.compute_processes) if report.gpu.compute_processes is not None else None,
            "status": report.gpu.status.value,
            "error": report.gpu.error_message
        },
        "disk": {
            "drive": report.disk.drive,
            "free_bytes": report.disk.free_bytes,
            "total_bytes": report.disk.total_bytes,
            "free_gib": report.disk.free_gib,
            "total_gib": report.disk.total_gib,
            "free_gb": report.disk.free_gb,
            "total_gb": report.disk.total_gb,
            "status": report.disk.status.value,
            "error": report.disk.error_message
        },
        "top_processes": [
            {
                "pid": p.pid,
                "name": p.name,
                "pids": p.pids,
                "working_set_mb": p.working_set_mb,
                "private_commit_mb": p.private_commit_mb,
                "instances_total": p.instances_total,
                "instances_measured": p.instances_measured,
                "is_partial_private_commit": p.is_partial_private_commit,
                "is_user_app": p.is_user_application,
                "status": p.status.value
            }
            for p in report.top_processes
        ],
        "recommendations": [
            {
                "process_name": r.process_name,
                "pids": r.pids,
                "observed_working_set_mb": r.observed_working_set_mb,
                "total_private_commit_mb": r.total_private_commit_mb,
                "instances_total": r.instances_total,
                "instances_measured": r.instances_measured,
                "is_partial": r.is_partial,
                "is_user_facing": r.is_user_facing,
                "unverified_heuristic_reclaim_mb": r.unverified_heuristic_reclaim_mb,
                "recommendation_text": r.recommendation_text
            }
            for r in report.recommendations
        ]
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)

def print_preflight_summary(report: PreflightReport):
    print("=" * 72)
    print("NOVA Compute — Preflight Hardware Qualification Audit")
    print(f"Timestamp (UTC): {report.timestamp_utc}")
    print("=" * 72)
    for name, gate in report.gates.items():
        status = "[PASS]" if gate.passed else "[FAIL]"
        print(f"  {status:<6} {name:<22}: {gate.measured_str} (Req: {gate.required_str})")
        if not gate.passed and gate.failure_reason:
            print(f"         Reason: {gate.failure_reason}")
    print("-" * 72)
    print(f"PREFLIGHT STATUS: {'ALL GATES PASSED' if report.passed else 'GATE CONDITIONS UNMET — INFERENCE GATED'}")
    print("=" * 72)

    if report.recommendations:
        print("\n--- Memory Preservation & Recovery Recommendations ---")
        for idx, rec in enumerate(report.recommendations[:5], 1):
            print(f"{idx}. {rec.recommendation_text}")
        print("Note: Close applications through their normal interface. NOVA never force-terminates external apps.")
        print("Working set reflects resident physical memory; private commit reflects dedicated virtual commit charge.")

def run_preflight_only(steward: ResourceSteward, output_dir: Optional[str] = None) -> int:
    print("[MODE] PREFLIGHT ONLY (Inference execution will NOT be launched)\n")
    report = steward.run_preflight()
    out_path = os.path.join(output_dir, "PREFLIGHT_LATEST.json") if output_dir else PREFLIGHT_LATEST_JSON
    save_preflight_json(report, path=out_path)
    print_preflight_summary(report)
    return 0 if report.passed else 1

def run_dry_run() -> int:
    print("[MODE] DRY RUN VALIDATION\n")
    cmd = get_workspace_safe_command()
    print("Configuration D Command Line:")
    print(" ".join(cmd))
    print("\nValidating binary and artifact presence...")
    print(f"  Runtime binary: {LLAMA_CLI} => {'FOUND' if os.path.exists(LLAMA_CLI) else 'MISSING'}")
    print(f"  Model GGUF:     {MODEL_GGUF} => {'FOUND' if os.path.exists(MODEL_GGUF) else 'MISSING'}")
    print("\nWorkspace Preservation Parameters:")
    print("  Mode: WORKSPACE_SAFE")
    print("  Process Priority: BELOW_NORMAL_PRIORITY_CLASS (0x00004000)")
    print("  Process Group:    CREATE_NEW_PROCESS_GROUP (0x00000200)")
    print("  Context: 512 | Batch: 64 | Micro-batch: 16 | Tokens: 1")
    print("  KV Cache: Q8_0 (24.0 MiB) | Flash Attention: auto | mmap: enabled | mlock: disabled")
    print("  GPU Expert Caching: DISABLED")
    print("\nDry run completed successfully. (No inference was started).")
    return 0

def run_memory_report(steward: ResourceSteward) -> int:
    print("[MODE] INTELLIGENT MEMORY ADVISOR REPORT\n")
    report = steward.run_preflight()
    save_preflight_json(report)
    print_preflight_summary(report)

    advice = steward.get_memory_advice()
    print("\n--- Protected System Infrastructure (Excluded from Recommendations) ---")
    if advice.protected_processes_observed:
        print(f"  Observed active protected processes: {', '.join(advice.protected_processes_observed)}")
    else:
        print("  No major protected system services in top process list.")
    print("  Policy: Protected processes are never manipulated, terminated, or recommended for closure.")

    print("\n--- Detailed Process Working Set & Private Commit Inventory ---")
    for p in report.top_processes:
        # Canonical classification is strictly authoritative and overrides any conflicting snapshot category
        cat_enum = MemoryAdvisor.classify_process(p.name)
        if cat_enum == ProcessCategory.PROTECTED_SYSTEM:
            cat_label = "Protected System"
        elif cat_enum == ProcessCategory.USER_APPLICATION:
            cat_label = "User Application"
        elif cat_enum == ProcessCategory.BACKGROUND_APPLICATION:
            cat_label = "Background App"
        else:
            cat_label = "Unclassified"
        part_str = " (partial)" if p.is_partial_private_commit else ""
        priv_str = f"{p.private_commit_mb:>7.1f} MB{part_str}" if p.private_commit_mb is not None else "    N/A          "
        print(f"  {p.name:<30} WS: {p.working_set_mb:>7.1f} MB  Priv: {priv_str:<17}  [{cat_label}] (Instances: {p.instances_measured}/{p.instances_total})")

    print("\n" + "=" * 72)
    print(f"ADVISOR NOTICE: {advice.disclaimer}")
    print("=" * 72)
    return 0 if report.passed else 1

def start_output_reader(pipe, out_queue: queue.Queue, stop_event: Optional[threading.Event] = None) -> threading.Thread:
    """Dedicated background thread reading child stdout to prevent blocking the supervisor until EOF."""
    def _reader():
        try:
            for line in iter(pipe.readline, ''):
                out_queue.put(line)
            t.eof_reached = True
        except Exception as e:
            t.read_failed = True
            t.error_message = str(e)
        finally:
            if stop_event is not None:
                stop_event.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.eof_reached = False
    t.read_failed = False
    t.error_message = None
    t.start()
    return t

def verify_inference_success(
    supervisor_res: SupervisorResult,
    recovery_report: RecoveryReport
) -> Tuple[InferenceOutcome, str]:
    """
    Strictly verifies inference smoke test outcome against:
      - Exit code zero
      - No safety abort
      - Confirmed generated-token/evaluation evidence:
          * eval time = XX ms / N runs where N >= 1
          * eval time = XX ms / 0 runs -> UNVERIFIED
          * eval time = XX ms without a count -> UNVERIFIED
          * prompt eval alone -> UNVERIFIED
          * tokens generated: N where N > 0
          * model load time or print_timings alone -> UNVERIFIED
      - Valid execution telemetry
      - Successful required recovery verification (including verified reader thread shutdown)
    """
    raw_output = "".join(supervisor_res.output_lines)

    if supervisor_res.abort_triggered:
        return InferenceOutcome.ABORTED, supervisor_res.abort_reason

    if supervisor_res.exit_code != 0:
        return InferenceOutcome.FAILED, f"Child process exited with non-zero code {supervisor_res.exit_code}"

    # Strict regex: MUST require explicit count after '/' (e.g. "eval time = 25.00 ms / 1 runs")
    eval_match = re.search(r"(?<!prompt )eval time\s*=\s*[\d\.]+\s*ms\s*/\s*(\d+)\s*(?:runs|tokens)\b", raw_output)
    token_gen_match = re.search(r"\btokens generated\s*:\s*(\d+)\b", raw_output, re.IGNORECASE)
    token_gen_match_alt = re.search(r"\b(\d+)\s*tokens generated\b", raw_output, re.IGNORECASE)

    has_positive_eval = False
    generated_count = 0

    if eval_match:
        try:
            count = int(eval_match.group(1))
            if count >= 1:
                generated_count = max(generated_count, count)
                has_positive_eval = True
        except (ValueError, TypeError):
            pass

    if token_gen_match:
        try:
            count = int(token_gen_match.group(1))
            if count >= 1:
                generated_count = max(generated_count, count)
                has_positive_eval = True
        except (ValueError, TypeError):
            pass

    if token_gen_match_alt:
        try:
            count = int(token_gen_match_alt.group(1))
            if count >= 1:
                generated_count = max(generated_count, count)
                has_positive_eval = True
        except (ValueError, TypeError):
            pass

    # Model load time or print_timings alone must never establish token generation
    has_token_gen = has_positive_eval and ("print_timings" in raw_output or "load time =" in raw_output)

    if not has_token_gen:
        if re.search(r"(?<!prompt )eval time\s*=\s*[\d\.]+\s*ms(?!\s*/\s*\d+)", raw_output):
            return InferenceOutcome.UNVERIFIED, "Inference output has ambiguous eval timing without explicit run count."
        if "prompt eval time =" in raw_output and not has_positive_eval:
            return InferenceOutcome.UNVERIFIED, "Inference executed prompt evaluation only (0 generated tokens evaluated)."
        return InferenceOutcome.UNVERIFIED, "Inference output lacks verified token generation / evaluation timing markers (eval runs >= 1)."

    if supervisor_res.processed_samples_count == 0:
        return InferenceOutcome.UNVERIFIED, "Execution telemetry lacks valid processed samples."

    # Explicit reader completion and EOF verification
    reader_eof = getattr(supervisor_res, "reader_eof_reached", True)
    reader_stopped = getattr(supervisor_res, "reader_thread_stopped", True) and recovery_report.reader_thread_stopped
    reader_timed_out = getattr(supervisor_res, "reader_timed_out", False)
    reader_read_failed = getattr(supervisor_res, "reader_read_failed", False)

    if reader_timed_out:
        return InferenceOutcome.UNVERIFIED, "Output reader thread timed out waiting for EOF."
    if reader_read_failed:
        err = getattr(supervisor_res, "reader_exception", None) or "I/O error"
        return InferenceOutcome.UNVERIFIED, f"Output reader encountered an exception ({err}); EOF could not be verified."
    if not reader_eof:
        return InferenceOutcome.UNVERIFIED, "Output reader stopped without reaching verified EOF."
    if not reader_stopped or not recovery_report.recovery_successful:
        return InferenceOutcome.UNVERIFIED, "Full resource recovery could not be verified (residual resources, telemetry failure, or unverified thread shutdown)."

    return InferenceOutcome.SUCCESS, f"Inference smoke test succeeded: token generated ({generated_count} run(s)), telemetry valid, and clean recovery verified."

def execute_inference(steward: ResourceSteward, output_dir: Optional[str] = None):
    """Executes inference ONLY after a fresh live preflight confirms all safety gates pass."""
    out_dir = output_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    load_results_path = os.path.join(out_dir, "LOAD_RESULTS.md")
    failure_analysis_path = os.path.join(out_dir, "FAILURE_ANALYSIS.md")
    execution_log_path = os.path.join(out_dir, "EXECUTION_LOG.txt")
    telemetry_json_path = os.path.join(out_dir, "RESOURCE_TELEMETRY.json")
    preflight_json_path = os.path.join(out_dir, "PREFLIGHT_LATEST.json")

    print("[MODE] EXECUTE AUTHORIZED INFERENCE\n")
    print("Running fresh preflight check immediately prior to launch...")
    report = steward.run_preflight()
    save_preflight_json(report, path=preflight_json_path)
    print_preflight_summary(report)

    if not report.passed:
        print("\n" + "!" * 72)
        print("EXECUTION ABORTED: Preflight safety gates failed. Halting launch.")
        print("Per strict safety governance: Inference cannot start when hardware gates fail.")
        print("!" * 72)

        # Write failure reports
        with open(load_results_path, "w", encoding="utf-8") as f:
            f.write("# NOVA Compute — Phase 2C-B: Load Results\n\n")
            f.write("**Status:** **EXECUTION GATED (PREFLIGHT STOP)**  \n\n")
            f.write("Inference launch aborted because fresh preflight gates were not satisfied:\n\n")
            for name, g in report.gates.items():
                status = "PASS" if g.passed else "FAIL"
                f.write(f"- **{name}:** {status} ({g.measured_str}; required: {g.required_str})\n")
            f.write("\nSee `FAILURE_ANALYSIS.md` for root cause and memory recovery path.\n")

        with open(failure_analysis_path, "w", encoding="utf-8") as f:
            f.write("# NOVA Compute — Phase 2C-B: Preflight Gate Analysis\n\n")
            f.write(f"**Timestamp (UTC):** {report.timestamp_utc}  \n")
            f.write(f"**Execution Status:** **GATED AT PREFLIGHT**  \n\n")
            f.write("## 1. Failed Gates Breakdown\n\n")
            for name, g in report.gates.items():
                if not g.passed:
                    f.write(f"- **{name}:** Measured `{g.measured_str}` vs Required `{g.required_str}`\n")
                    f.write(f"  *Reason:* {g.failure_reason}\n")
            f.write("\n## 2. Safe Recovery Recommendations\n\n")
            for rec in report.recommendations[:5]:
                f.write(f"- {rec.recommendation_text}\n")
            f.write("\n*Governance Rule:* Never terminate unrelated user applications to clear the gate.\n")

        with open(execution_log_path, "w", encoding="utf-8") as f:
            f.write(f"NOVA Compute Execution Gated at Preflight.\nTimestamp (UTC): {report.timestamp_utc}\n")
            f.write("Inference process was not spawned due to unmet preflight gate conditions.\n")

        sys.exit(1)

    cmd = get_workspace_safe_command()
    print("\nAll gates PASSED. Spawning NOVA inference child process...")
    print("Command:", " ".join(cmd))

    pre_ram = report.system_ram
    pre_gpu = report.gpu
    peak_ram_gib = pre_ram.avail_phys_gib
    peak_vram_mib = pre_gpu.vram_used_mib

    # Launch process with CREATE_NEW_PROCESS_GROUP and BELOW_NORMAL_PRIORITY_CLASS on Windows
    creation_flags = CREATE_NEW_PROCESS_GROUP | BELOW_NORMAL_PRIORITY_CLASS
    t_start = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=creation_flags
    )
    pid = proc.pid
    print(f"Child process launched (PID: {pid}, Priority: BELOW_NORMAL, Group: NEW_PROCESS_GROUP).")

    # Start asynchronous output reader thread
    out_queue: queue.Queue = queue.Queue()
    reader_stop = threading.Event()
    reader_thread = start_output_reader(proc.stdout, out_queue, reader_stop)

    # Start live telemetry sampler
    sampler = LiveTelemetrySampler(target_pid=pid, interval_ms=steward.policy.sampling_interval_ms)
    sampler.start()

    # Supervise child execution using production supervisor engine
    supervisor_res = supervise_child_process(
        proc=proc,
        sample_queue=sampler.sample_queue,
        output_queue=out_queue,
        policy=steward.policy,
        clock=time.monotonic,
        abort_callback=lambda reason: print(f"\n[ABORT] {reason}"),
        on_output_line=lambda line: (sys.stdout.write(line), sys.stdout.flush()),
        stale_timeout_sec=3.0,
        reader_thread=reader_thread,
        reader_timeout_sec=2.0
    )

    # REFACTORED LIFECYCLE ORDER (EOF Handling & Output Integrity):
    # Stage 1: If abort was triggered, initiate child teardown FIRST
    if supervisor_res.abort_triggered:
        print(f"[RECOVERY] Abort triggered: {supervisor_res.abort_reason}. Initiating child teardown...")
        SafeReclamationManager.terminate_child_process(
            proc, grace_period_sec=steward.policy.child_termination_grace_period_seconds
        )

    # Stage 2: Wait for reader thread to reach true EOF and join
    reader_stop.set()
    reader_thread.join(timeout=2.0)
    reader_stopped = not reader_thread.is_alive()
    reader_timed_out = not reader_stopped
    reader_eof = getattr(reader_thread, "eof_reached", False) and reader_stopped
    reader_failed = getattr(reader_thread, "read_failed", False) or reader_timed_out
    supervisor_res.reader_thread_stopped = reader_stopped
    supervisor_res.reader_eof_reached = reader_eof
    supervisor_res.reader_read_failed = reader_failed
    supervisor_res.reader_timed_out = reader_timed_out
    if getattr(reader_thread, "error_message", None):
        supervisor_res.reader_exception = getattr(reader_thread, "error_message", None)

    # Stage 3: Drain ALL remaining queued lines after reader has finished
    while True:
        try:
            trailing_line = out_queue.get_nowait()
            supervisor_res.output_lines.append(trailing_line)
            sys.stdout.write(trailing_line)
            sys.stdout.flush()
            if "CUDA error" in trailing_line or "out of memory" in trailing_line.lower():
                if not supervisor_res.abort_triggered:
                    supervisor_res.abort_triggered = True
                    supervisor_res.abort_reason = f"CUDA OOM / Error detected in trailing output: {trailing_line.strip()}"
        except queue.Empty:
            break

    # Stage 4: Stop and join telemetry sampler
    sampler_stopped = sampler.stop(timeout_sec=2.0)

    # Stage 5: Close remaining stdio pipes and handles (ONLY AFTER reader has completed EOF drain)
    pipes_closed = SafeReclamationManager.close_process_pipes(proc)

    # Stage 6: Verify resource release and produce recovery report
    recovery_report = steward.reclamation.perform_post_inference_recovery(
        pre_ram=pre_ram,
        pre_gpu=pre_gpu,
        peak_ram_gib=supervisor_res.peak_ram_gib,
        peak_vram_mib=supervisor_res.peak_vram_mib,
        child_proc=proc,
        grace_period_sec=steward.policy.child_termination_grace_period_seconds,
        reader_thread_stopped=reader_stopped,
        sampler_thread_stopped=sampler_stopped
    )

    t_end = time.time()
    total_dur = round(t_end - t_start, 2)
    exit_code = proc.returncode if proc.returncode is not None else -1

    # Record EXECUTION_LOG.txt
    with open(execution_log_path, "w", encoding="utf-8") as f:
        f.writelines(supervisor_res.output_lines)
        if supervisor_res.abort_triggered:
            f.write(f"\n[STEWARD_ABORT] {supervisor_res.abort_reason}\n")
        f.write(f"\nProcess exited with code {exit_code} after {total_dur} seconds.\n")

    # Record RESOURCE_TELEMETRY.json
    telemetry_data = {
        "timestamp_utc": report.timestamp_utc,
        "command": cmd,
        "exit_code": exit_code,
        "duration_seconds": total_dur,
        "abort_triggered": supervisor_res.abort_triggered,
        "abort_reason": supervisor_res.abort_reason,
        "preflight": {
            "all_gates_passed": report.passed,
            "pre_ram_gib": pre_ram.avail_phys_gib,
            "pre_vram_free_mib": pre_gpu.vram_free_mib
        },
        "recovery": {
            "post_ram_gib": recovery_report.post_avail_ram_gib,
            "post_vram_used_mib": recovery_report.post_vram_used_mib,
            "residual_vram_leak_mib": recovery_report.residual_vram_leak_mib,
            "child_terminated_successfully": recovery_report.child_terminated_successfully,
            "reader_thread_stopped": recovery_report.reader_thread_stopped,
            "sampler_thread_stopped": recovery_report.sampler_thread_stopped,
            "pipes_closed": recovery_report.pipes_closed,
            "gpu_verification_status": recovery_report.gpu_verification_status,
            "process_verification_status": recovery_report.process_verification_status,
            "recovery_successful": recovery_report.recovery_successful,
            "termination_status": recovery_report.termination_status,
            "shutdown_method": recovery_report.shutdown_method,
            "notes": recovery_report.notes
        },
        "samples_count": len(sampler.samples),
        "timeline": sampler.samples
    }
    with open(telemetry_json_path, "w", encoding="utf-8") as f:
        json.dump(telemetry_data, f, indent=2)

    # Evaluate inference outcome with truthfulness
    outcome, outcome_reason = verify_inference_success(supervisor_res, recovery_report)

    with open(load_results_path, "w", encoding="utf-8") as f:
        f.write("# NOVA Compute — Phase 2C-B: Load Results\n\n")
        f.write(f"**Outcome:** `{outcome.value}`  \n")
        f.write(f"**Reason:** {outcome_reason}  \n")
        f.write(f"**Exit Code:** {exit_code}  \n")
        f.write(f"**Duration:** {total_dur}s  \n\n")
        f.write("## Command Line\n```powershell\n" + " ".join(cmd) + "\n```\n\n")
        f.write("## Recovery Summary\n")
        f.write(f"- Shutdown Method: `{recovery_report.shutdown_method}`\n")
        f.write(f"- Child Terminated: `{recovery_report.child_terminated_successfully}`\n")
        f.write(f"- Pipes Closed: `{recovery_report.pipes_closed}`\n")
        f.write(f"- Threads Stopped: Reader={recovery_report.reader_thread_stopped}, Sampler={recovery_report.sampler_thread_stopped}\n")
        f.write(f"- VRAM Leak (MiB): `{recovery_report.residual_vram_leak_mib}`\n")
        f.write(f"- Full Recovery Verified: `{recovery_report.recovery_successful}`\n")
        for n in recovery_report.notes:
            f.write(f"- {n}\n")

    with open(failure_analysis_path, "w", encoding="utf-8") as f:
        f.write("# NOVA Compute — Phase 2C-B: Analysis Report\n\n")
        f.write(f"**Outcome:** `{outcome.value}`: {outcome_reason}\n")

    print(f"\nExecution finished. Outcome: {outcome.value}. Telemetry and reports saved to: {out_dir}")

def run_guided_startup(steward: ResourceSteward) -> int:
    """Runs interactive guided low-memory startup and admission workflow."""
    controller = GuidedStartupController(steward=steward)
    return controller.run_interactive()

def main():
    parser = argparse.ArgumentParser(
        description="NOVA Compute — Phase 2C-B Inference Runner & Resource Steward",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--execute", action="store_true", help="Execute inference ONLY after fresh preflight verification.")
    group.add_argument("--preflight", action="store_true", help="Run read-only hardware and process preflight diagnostics.")
    group.add_argument("--dry-run", action="store_true", help="Validate command syntax without launching inference.")
    group.add_argument("--memory-report", action="store_true", help="Generate detailed memory and process recommendation report.")
    group.add_argument("--advisor", action="store_true", help="Run read-only Intelligent Memory Advisor diagnostics and recommendations.")
    group.add_argument("--guided-start", action="store_true", help="Launch interactive guided low-memory startup and progressive admission.")

    args = parser.parse_args()
    steward = ResourceSteward()

    if args.execute:
        execute_inference(steward)
        sys.exit(0)
    elif args.dry_run:
        code = run_dry_run()
        sys.exit(code)
    elif args.advisor or args.memory_report:
        code = run_memory_report(steward)
        sys.exit(code)
    elif args.guided_start:
        code = run_guided_startup(steward)
        sys.exit(code)
    elif args.preflight:
        code = run_preflight_only(steward)
        sys.exit(code)
    else:
        # DEFAULT MODE: PREFLIGHT ONLY — NEVER LAUNCHES INFERENCE
        code = run_preflight_only(steward)
        sys.exit(code)

if __name__ == "__main__":
    main()
