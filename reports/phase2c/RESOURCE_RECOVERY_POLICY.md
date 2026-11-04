# NOVA Compute — Phase 2C Resource Recovery Policy

**Document ID:** `NOVA-POL-P2C-RECOVERY-01`  
**Date:** 2026-11-04  
**Author:** Antigravity (Implementation Engineer)  
**Governance:** Reviewed by ChatGPT (Lead Architect) & User (Project Owner)  
**Status:** ACTIVE POLICY  

---

## 1. Purpose & Core Principles

This policy establishes strict operating boundaries for process management, memory reclamation, and post-execution cleanup in NOVA Compute. Because the platform runs on a resource-constrained consumer laptop (16 GB physical RAM, 6 GB VRAM) alongside the user's active applications and operating system tasks, NOVA must act as a well-behaved guest.

The governing principles are:
1. **Zero Impact on User Applications:** Never kill, suspend, or alter the priority of applications belonging to the user or system.
2. **Explicit Child Ownership:** Cleanup actions apply strictly to processes spawned directly by NOVA.
3. **Transparency over Automation:** When memory is insufficient, advise the user; do not force cleanup.
4. **Clean Exit Guarantees:** Ensure GPU contexts, file handles, and pipe descriptors are systematically reclaimed upon completion or abort.

---

## 2. Permitted vs. Prohibited Actions

### 2.1 Strictly Prohibited Actions (Non-Negotiable)

- **Prohibited Process Termination:**
  - Running `taskkill /F /IM ...` or `Stop-Process` against browsers (`msedge`, `chrome`, `firefox`), development environments (`code`, `cursor`), communication tools, or system binaries.
  - Terminating any process whose PID is not the active child process spawned by `llama-cli`.
- **Prohibited Memory Manipulations:**
  - Invoking `EmptyWorkingSet` or global working set trim on external processes. Trimming working sets globally causes severe disk paging spikes across Windows when user apps re-fault their pages.
  - Disabling, modifying, or shrinking the Windows Pagefile (`pagefile.sys`).
  - Purging the Windows standby list or file system cache globally. Clean file-backed cache pages accelerate mmap model loading and must be managed solely by the Windows memory manager.
- **Prohibited Elevation & Driver Actions:**
  - Requiring or using Windows Administrator privileges (`runas` / elevated shells).
  - Resetting the GPU driver (`nvidia-smi -r` or driver restarts).

### 2.2 Permitted and Required Actions

- **Bounded Child Process Management:**
  - Spawning `llama-cli` with `BELOW_NORMAL_PRIORITY_CLASS` (`0x00004000`) on Windows to ensure OS responsiveness and UI fluidity.
  - Capturing and storing the child process PID.
  - Terminating the child process gracefully via `proc.terminate()` on abort or timeout, allowing runtime cleanup handlers to execute.
  - Escalating to `proc.kill()` if the child process fails to terminate within 5.0 seconds.
- **Resource Recovery Verification:**
  - Closing all standard I/O pipes (`stdin`, `stdout`, `stderr`) and subprocess handles.
  - Sampling post-run GPU VRAM to confirm all CUDA allocations have been released back to the OS.
  - Reporting any lingering child processes or leaked memory in `RecoveryReport`.
- **User-Centric Memory Advising:**
  - Inspecting resident memory of user-mode processes in read-only mode via `psutil`.
  - Computing the RAM deficit against the $\ge 10.0\text{ GiB}$ gate.
  - Listing candidate applications and their resident memory in terminal output, advising the user to save work and close them manually.

---

## 3. Post-Run Verification Protocol

Following any inference run (whether successful, failed, or aborted), the `SafeReclamationManager` executes the following sequence:

```mermaid
sequenceDiagram
    participant H as Harness
    participant C as Child Process (llama-cli)
    participant M as SafeReclamationManager
    participant G as GPU (nvidia-smi)

    H->>M: cleanup() triggered
    M->>C: Check if process is running
    alt Process is running
        M->>C: proc.terminate() (SIGTERM/WM_CLOSE)
        M->>C: wait(timeout=5.0s)
        opt Timeout Exceeded
            M->>C: proc.kill() (TerminateProcess)
        end
    end
    M->>C: Close pipe handles (stdout, stderr)
    M->>G: Query dedicated VRAM
    G-->>M: Return post-run free VRAM
    M->>H: Generate RecoveryReport (reclaimed=True)
```

1. **Child State Verification:** Confirms `poll()` returns an integer returncode.
2. **Handle Cleanup:** Explicitly closes pipe handles to prevent handle leaks.
3. **GPU Memory Verification:** Queries `nvidia-smi` to confirm VRAM usage has returned to the pre-run baseline ($\pm 100\text{ MiB}$). If lingering VRAM is detected, a warning is emitted to advise restarting the terminal session.
4. **Audit Trail:** Appends recovery status, returncode, and VRAM delta to the execution telemetry log.
