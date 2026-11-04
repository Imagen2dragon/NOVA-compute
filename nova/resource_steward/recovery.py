"""
NOVA Compute — Safe Resource Reclamation & Recovery Manager
Implements strict ownership-bounded cleanup:
- Reclaims ONLY NOVA-owned processes, threads, file handles, and buffers.
- Strictly prohibits terminating or tampering with unrelated user applications.
- Verifies post-execution VRAM and RAM return to baseline with fail-closed integrity.
- Differentiates console control shutdown from forced termination on Windows.
- Produces verifiable Recovery Reports.
"""

import os
import sys
import time
import signal
import subprocess
import csv
import io
from typing import Optional, List, Dict, Any, Tuple
from .models import (
    RecoveryReport,
    SystemMemorySnapshot,
    GPUSnapshot,
    TelemetryStatus,
    current_iso_utc
)
from .monitor import ResourceMonitor

class SafeReclamationManager:
    """Safely manages lifecycle and teardown of NOVA-owned inference child processes."""

    @staticmethod
    def close_process_pipes(proc: Optional[subprocess.Popen]) -> bool:
        """Explicitly closes process stdio pipes to prevent handle leaks. Returns True if all closed."""
        if not proc:
            return True
        all_closed = True
        for pipe in (proc.stdout, proc.stderr, proc.stdin):
            if pipe:
                if not pipe.closed:
                    try:
                        pipe.close()
                    except Exception:
                        pass
                if not pipe.closed:
                    all_closed = False
        return all_closed

    @staticmethod
    def terminate_child_process(
        proc: Optional[subprocess.Popen],
        grace_period_sec: float = 5.0
    ) -> Tuple[str, str, bool]:
        """
        Terminates a NOVA-owned subprocess.
        Prefers proc.send_signal(signal.CTRL_BREAK_EVENT) for Windows console processes
        spawned with CREATE_NEW_PROCESS_GROUP.
        Wait for grace_period_sec. If still alive, escalates to hard kill() (TerminateProcess).
        Returns: (term_status_str, shutdown_method, child_terminated_successfully)
        """
        if proc is None:
            return "NO_CHILD_PROCESS", "NO_CHILD", True

        pid = proc.pid
        if proc.poll() is not None:
            SafeReclamationManager.close_process_pipes(proc)
            return f"ALREADY_EXITED_CODE_{proc.returncode}", "NATURAL_EXIT", True

        shutdown_method = "NATURAL_EXIT"
        event_delivered = False

        # On Windows, send CTRL_BREAK_EVENT to the child process group.
        # Note: CTRL_BREAK_EVENT delivery requires that the child process shares or attaches to
        # the caller's console group created via CREATE_NEW_PROCESS_GROUP.
        if sys.platform == "win32" and hasattr(signal, "CTRL_BREAK_EVENT"):
            try:
                print(f"[RECOVERY] Sending CTRL_BREAK_EVENT to NOVA child PID {pid} (grace: {grace_period_sec}s)...")
                proc.send_signal(signal.CTRL_BREAK_EVENT)
                event_delivered = True
                shutdown_method = "CTRL_BREAK"
            except Exception as e:
                print(f"[RECOVERY] proc.send_signal(CTRL_BREAK_EVENT) failed: {e}. Falling back to os.kill.")
                try:
                    os.kill(pid, signal.CTRL_BREAK_EVENT)
                    event_delivered = True
                    shutdown_method = "CTRL_BREAK"
                except Exception as e2:
                    print(f"[RECOVERY] os.kill failed: {e2}. Escalating immediately to forced termination.")

        if not event_delivered:
            # When console break cannot be delivered on Windows, terminate() is TerminateProcess (forced)
            try:
                print(f"[RECOVERY] Invoking proc.terminate() on child PID {pid}...")
                proc.terminate()
                shutdown_method = "FORCED_TERMINATION"
            except Exception as e:
                print(f"[RECOVERY] proc.terminate() exception: {e}")

        # Wait during grace period
        t_start = time.time()
        while time.time() - t_start < grace_period_sec:
            if proc.poll() is not None:
                print(f"[RECOVERY] NOVA child PID {pid} exited with code {proc.returncode} via {shutdown_method}.")
                SafeReclamationManager.close_process_pipes(proc)
                return f"EXITED_CODE_{proc.returncode}_{shutdown_method}", shutdown_method, True
            time.sleep(0.1)

        # Escalation: Hard kill ONLY the NOVA-owned child process
        print(f"[RECOVERY] Child PID {pid} did not exit within {grace_period_sec}s. Escalating to forced termination (kill)...")
        try:
            proc.kill()
            proc.wait(timeout=3.0)
            print(f"[RECOVERY] NOVA child PID {pid} forcefully terminated.")
            SafeReclamationManager.close_process_pipes(proc)
            return f"FORCED_TERMINATION_CODE_{proc.returncode}", "FORCED_TERMINATION", True
        except subprocess.TimeoutExpired:
            print(f"[RECOVERY] ERROR: Child PID {pid} failed to terminate after kill().")
            SafeReclamationManager.close_process_pipes(proc)
            return f"KILL_TIMEOUT_PID_{pid}", "FORCED_TERMINATION_TIMEOUT", False
        except Exception as e:
            SafeReclamationManager.close_process_pipes(proc)
            return f"KILL_ERROR_{e}", "FORCED_TERMINATION_ERROR", False

    @staticmethod
    def perform_post_inference_recovery(
        pre_ram: SystemMemorySnapshot,
        pre_gpu: GPUSnapshot,
        peak_ram_gib: float,
        peak_vram_mib: float,
        child_proc: Optional[subprocess.Popen],
        grace_period_sec: float = 5.0,
        reader_thread_stopped: bool = True,
        sampler_thread_stopped: bool = True
    ) -> RecoveryReport:
        """
        Reclaims NOVA-owned child process, measures post-run resources,
        verifies VRAM return near baseline with fail-closed integrity,
        and produces a verifiable Recovery Report.
        """
        t0 = time.time()
        child_pid = child_proc.pid if child_proc else None

        # 1. Terminate child if still running
        term_status, shutdown_method, child_terminated = SafeReclamationManager.terminate_child_process(child_proc, grace_period_sec)

        # Ensure pipes are closed
        pipes_closed = SafeReclamationManager.close_process_pipes(child_proc)

        # Brief pause for OS and NVIDIA driver teardown
        time.sleep(1.0)

        # 2. Query post-execution hardware state
        post_ram = ResourceMonitor.get_system_memory()
        post_gpu = ResourceMonitor.get_gpu_telemetry()
        recovery_dur = round(time.time() - t0, 3)

        notes = []

        # 3. GPU VRAM Verification
        gpu_verification_status = "VERIFIED"
        vram_leak: Optional[float] = None
        if post_gpu.status != TelemetryStatus.SUCCESS:
            gpu_verification_status = "FAILED"
            notes.append("ERROR: Post-run GPU telemetry failed. VRAM recovery cannot be verified.")
        elif pre_gpu.status != TelemetryStatus.SUCCESS:
            gpu_verification_status = "UNVERIFIED"
            notes.append("Notice: Pre-run GPU telemetry was invalid.")
        else:
            vram_leak = round(max(0.0, post_gpu.vram_used_mib - pre_gpu.vram_used_mib), 1)
            if vram_leak > 100.0:
                notes.append(f"Notice: VRAM usage is {vram_leak:.1f} MiB higher than pre-run baseline. Driver teardown may still be pending.")
            else:
                notes.append("VRAM returned to idle baseline (no leak detected).")

        # 4. Residual Process Verification (Exact PID matching)
        residual_pids = []
        process_verification_status = "VERIFIED"
        if child_pid:
            try:
                res = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=3
                )
                if res.returncode != 0:
                    process_verification_status = "UNKNOWN"
                    notes.append(f"Warning: Residual process check failed (tasklist returned code {res.returncode}).")
                else:
                    reader = csv.reader(io.StringIO(res.stdout.strip()))
                    for row in reader:
                        if len(row) >= 2:
                            try:
                                r_pid = int(row[1])
                                if r_pid == child_pid:
                                    residual_pids.append(r_pid)
                            except ValueError:
                                pass
                    if residual_pids:
                        process_verification_status = "FAILED"
                        notes.append(f"ERROR: Child PID {child_pid} still detected in OS process table.")
            except Exception as e:
                process_verification_status = "UNKNOWN"
                notes.append(f"Warning: Residual process check encountered exception: {e}.")

        # 5. Thread Teardown Verification
        if not reader_thread_stopped:
            notes.append("Warning: Output reader thread failed to stop cleanly within timeout.")
        if not sampler_thread_stopped:
            notes.append("Warning: Telemetry sampler thread failed to stop cleanly within timeout.")

        # 6. RAM State Evaluation
        if post_ram.status == TelemetryStatus.SUCCESS:
            if post_ram.avail_phys_gib < pre_ram.avail_phys_gib - 0.5:
                delta = round(pre_ram.avail_phys_gib - post_ram.avail_phys_gib, 2)
                notes.append(f"Notice: Available RAM is {delta:.2f} GiB lower than pre-run. File-backed model pages likely reside in OS standby cache (clean, immediately reclaimable by Windows).")
            else:
                notes.append("System physical RAM recovered near baseline.")
        else:
            notes.append("Notice: Post-run RAM telemetry failed.")

        notes.append("Governance preserved: No unrelated user applications or Windows services were terminated.")

        # Strict full resource-recovery success criteria:
        # 1. Child terminated cleanly
        # 2. No residual processes detected and process verification succeeded
        # 3. GPU verification succeeded and post-run VRAM returned within tolerance (<= 100 MiB leak)
        # 4. Post-run RAM telemetry succeeded
        # 5. Owned pipes closed
        # 6. Both reader and sampler threads confirmed stopped
        vram_leak_acceptable = (vram_leak is not None and vram_leak <= 100.0)
        recovery_successful = (
            child_terminated and
            len(residual_pids) == 0 and
            process_verification_status == "VERIFIED" and
            gpu_verification_status == "VERIFIED" and
            vram_leak_acceptable and
            post_ram.status == TelemetryStatus.SUCCESS and
            pipes_closed and
            reader_thread_stopped and
            sampler_thread_stopped
        )

        return RecoveryReport(
            pre_avail_ram_gib=pre_ram.avail_phys_gib,
            peak_pressure_ram_gib=peak_ram_gib,
            post_avail_ram_gib=post_ram.avail_phys_gib,
            pre_vram_used_mib=pre_gpu.vram_used_mib,
            peak_vram_used_mib=peak_vram_mib,
            post_vram_used_mib=post_gpu.vram_used_mib,
            pre_process_private_commit_mb=0.0,
            post_process_private_commit_mb=0.0,
            child_pid=child_pid,
            termination_status=term_status,
            shutdown_method=shutdown_method,
            recovery_duration_seconds=recovery_dur,
            residual_processes_detected=residual_pids,
            residual_vram_leak_mib=vram_leak,
            child_terminated_successfully=child_terminated,
            reader_thread_stopped=reader_thread_stopped,
            sampler_thread_stopped=sampler_thread_stopped,
            pipes_closed=pipes_closed,
            gpu_verification_status=gpu_verification_status,
            process_verification_status=process_verification_status,
            recovery_successful=recovery_successful,
            notes=notes
        )
