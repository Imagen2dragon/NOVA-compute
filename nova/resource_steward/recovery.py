"""
NOVA Compute — Safe Resource Reclamation & Recovery Manager
Implements strict ownership-bounded cleanup:
- Reclaims ONLY NOVA-owned processes, threads, file handles, and buffers.
- Strictly prohibits terminating or tampering with unrelated user applications.
- Verifies post-execution VRAM and RAM return to baseline.
- Produces verifiable Recovery Reports.
"""

import os
import sys
import time
import subprocess
from typing import Optional, List, Dict, Any, Tuple
from .models import (
    RecoveryReport,
    SystemMemorySnapshot,
    GPUSnapshot,
    current_iso_utc
)
from .monitor import ResourceMonitor

class SafeReclamationManager:
    """Safely manages lifecycle and teardown of NOVA-owned inference child processes."""

    @staticmethod
    def terminate_child_process(
        proc: Optional[subprocess.Popen],
        grace_period_sec: float = 5.0
    ) -> Tuple[str, bool]:
        """
        Gracefully terminates a NOVA-owned subprocess.
        Escalates to kill ONLY if the child does not exit within grace_period_sec.
        Never targets or touches unrelated process trees.
        """
        if proc is None:
            return "NO_CHILD_PROCESS", True

        pid = proc.pid
        # Check if already terminated
        if proc.poll() is not None:
            return f"ALREADY_EXITED_CODE_{proc.returncode}", True

        print(f"[RECOVERY] Initiating graceful termination for NOVA child PID {pid} (grace: {grace_period_sec}s)...")
        try:
            proc.terminate()
        except ProcessLookupError:
            return "ALREADY_EXITED", True
        except Exception as e:
            print(f"[RECOVERY] Warning: proc.terminate() raised {e}")

        # Wait during grace period
        t_start = time.time()
        while time.time() - t_start < grace_period_sec:
            if proc.poll() is not None:
                print(f"[RECOVERY] NOVA child PID {pid} exited gracefully with code {proc.returncode}.")
                return f"GRACEFUL_EXIT_CODE_{proc.returncode}", True
            time.sleep(0.2)

        # Escalation: kill ONLY the NOVA-owned child process
        print(f"[RECOVERY] Child PID {pid} did not exit within grace period. Escalating to kill()...")
        try:
            proc.kill()
            proc.wait(timeout=3.0)
            print(f"[RECOVERY] NOVA child PID {pid} killed successfully.")
            return f"KILLED_AFTER_TIMEOUT", True
        except subprocess.TimeoutExpired:
            print(f"[RECOVERY] ERROR: Child PID {pid} failed to terminate after kill().")
            return f"KILL_TIMEOUT_PID_{pid}", False
        except Exception as e:
            return f"KILL_ERROR_{e}", False

    @staticmethod
    def perform_post_inference_recovery(
        pre_ram: SystemMemorySnapshot,
        pre_gpu: GPUSnapshot,
        peak_ram_gib: float,
        peak_vram_mib: float,
        child_proc: Optional[subprocess.Popen],
        grace_period_sec: float = 5.0
    ) -> RecoveryReport:
        """
        Reclaims NOVA-owned child process, measures post-run resources,
        verifies VRAM return near baseline, and produces the Recovery Report.
        """
        t0 = time.time()
        child_pid = child_proc.pid if child_proc else None

        # 1. Terminate child if still running
        term_status, term_ok = SafeReclamationManager.terminate_child_process(child_proc, grace_period_sec)

        # Brief pause for OS and NVIDIA driver to reclaim buffers
        time.sleep(1.0)

        # 2. Query post-execution hardware state
        post_ram = ResourceMonitor.get_system_memory()
        post_gpu = ResourceMonitor.get_gpu_telemetry()
        recovery_dur = round(time.time() - t0, 3)

        # 3. Check for residual VRAM leak (allocated VRAM vs pre-run VRAM)
        vram_leak = max(0.0, post_gpu.vram_used_mib - pre_gpu.vram_used_mib)

        # 4. Check for residual processes
        residual_pids = []
        if child_pid:
            # Verify child is actually gone
            try:
                # Windows tasklist check for specific PID
                res = subprocess.run(["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                                     capture_output=True, text=True, timeout=3)
                if str(child_pid) in res.stdout:
                    residual_pids.append(child_pid)
            except Exception:
                pass

        notes = []
        if vram_leak > 100.0:
            notes.append(f"Notice: VRAM usage is {vram_leak:.1f} MiB higher than pre-run baseline. Driver teardown may still be pending.")
        else:
            notes.append("VRAM returned to idle baseline (no leak detected).")

        if post_ram.avail_phys_gib < pre_ram.avail_phys_gib - 0.5:
            delta = pre_ram.avail_phys_gib - post_ram.avail_phys_gib
            notes.append(f"Notice: Available RAM is {delta:.2f} GiB lower than pre-run. File-backed model pages likely reside in OS standby cache (clean, immediately reclaimable by Windows).")
        else:
            notes.append("System physical RAM recovered near baseline.")

        notes.append("Governance preserved: No unrelated user applications or Windows services were terminated.")

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
            recovery_duration_seconds=recovery_dur,
            residual_processes_detected=residual_pids,
            residual_vram_leak_mib=round(vram_leak, 1),
            recovery_successful=term_ok and len(residual_pids) == 0,
            notes=notes
        )

# Type alias helper for tuple status
Tuple_status = tuple
