# ADR-001: Child-Owned Duplicated Restrictive File Handle (Option A) for Crash-Resilient GGUF Integrity

- **Date:** 2026-11-06
- **Status:** Approved & Implemented in Phase 3B.9 / Audited in Phase 3B.10
- **Context:**
  NOVA Chat protects the 18.6 GB GGUF model file by opening it with `GENERIC_READ` and restrictive `FILE_SHARE_READ` (0x00000001), preventing any other process on the machine from opening the file with write permissions (`GENERIC_WRITE`), deleting it, or renaming/replacing it.
  However, the pinned `llama.cpp` runtime (commit `dde879bc4`) opens model files using MSVC CRT `_wfopen(..., "rb")` (`_SH_DENYNO`), which translates to Win32 `CreateFileW` with `FILE_SHARE_READ | FILE_SHARE_WRITE` (0x00000003).
  If the NOVA parent process were to crash or be terminated abruptly, the parent's process-private handle table is destroyed by the Windows kernel. If only the child's own handle remained open, an external process could open the file with write access during child teardown because the child's handle permitted write sharing.
- **Alternatives Considered:**
  1. *Option A (Child-Owned Duplicated Handle):* Duplicate the parent's `FILE_SHARE_READ` handle into the child's private handle table via `DuplicateHandle(..., DUPLICATE_SAME_ACCESS)` while the child is suspended before `ResumeThread`.
  2. *Option B (Supervisor Daemon):* Launch a third independent supervisor process whose sole responsibility is to hold the lock handle.
  3. *Option C (Runtime Binary Fork / Patch):* Modify llama.cpp source to use `_sopen_s` with `_SH_DENYWR` on Windows and recompile.
  4. *Option D (Windows ACLs / Read-Only File Attribute):* Mutate the filesystem DACLs or set `FILE_ATTRIBUTE_READONLY` on disk.
- **Decision:**
  Adopt **Option A**.
  When spawning `llama-server.exe` suspended inside the Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), the parent duplicates its `FILE_SHARE_READ` handle directly into the child process's handle table (`pi.hProcess`) via `DuplicateHandle(GetCurrentProcess(), src_handle, pi.hProcess, &mut target_handle, 0, 0, DUPLICATE_SAME_ACCESS)` before calling `ResumeThread`.
- **Rationale:**
  1. *Kernel-Enforced Protection:* In Windows NTFS, sharing checks evaluate all open handles across all processes. Because the child process owns an active handle specifying `FILE_SHARE_READ` (no write sharing), any external `GENERIC_WRITE`, `DeleteFileW`, or rename attempt is rejected with `ERROR_SHARING_VIOLATION` (32) by the kernel, even if the parent process dies.
  2. *Zero Binary Mutations:* Uses the pinned upstream `llama.cpp` binary (build 12032, commit `dde879bc4`) without patching or forking.
  3. *Zero Persistent Disk State:* Does not modify file attributes or ACLs, preventing corrupt disk states after abrupt power failure.
  4. *Zero Administrator Elevation:* Uses standard user-mode Win32 process and handle manipulation APIs.
  5. *Automatic Kernel Cleanup:* When the Job Object terminates the child process upon parent exit (`KILL_ON_JOB_CLOSE`), the Windows kernel purges the child's handle table, releasing the lock only after all child threads and mappings have terminated.
- **Consequences:**
  - `ModelFileLockGuard` must expose its underlying Win32 `HANDLE`.
  - Process creation in `JobObject` must support handle duplication into the suspended child process.
  - Integration tests must verify child-side handle retention and bilateral sharing compatibility across all crash and lifecycle states.
