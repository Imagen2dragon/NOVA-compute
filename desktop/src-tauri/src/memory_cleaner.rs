//! User-initiated memory cleaning, similar to Mem Reduct (Project Owner decision 2026-11-08,
//! ADR-003). Runs ONLY when the user presses "Clean memory" and never while a model is
//! loading or loaded. It never closes or suspends any application.
//!
//! Step 1 (no admin): trims the working sets of processes the user can open
//! (`EmptyWorkingSet`). Pages move to the standby/modified lists; apps keep running and page
//! back in on demand. This raises Windows "available" RAM, which is what NOVA's gate reads.
//!
//! Step 2 (admin, Windows UAC prompt every time): a copy of nova-chat.exe is started elevated
//! with `--nova-memory-purge`. It enables SeProfileSingleProcessPrivilege and calls
//! `NtSetSystemInformation(SystemMemoryListInformation, ...)` to flush the modified page list
//! and purge the standby list. If the user declines the prompt, only step 1 has run.
//!
//! NOVA's own process tree (its WebView2 window processes, llama-server) and desktop-critical
//! processes (`NEVER_TRIM`) are never trimmed, so cleaning does not make NOVA or the desktop stutter.
//!
//! GPU memory cannot be freed this way: Windows offers no API to take GPU memory from another
//! process. The loader instead names the GPU apps to close.

use serde::Serialize;

/// Command-line flag that makes nova-chat.exe perform the privileged purge and exit.
pub const PURGE_FLAG: &str = "--nova-memory-purge";

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct MemoryCleanReport {
    pub avail_before_gib: f64,
    pub avail_after_gib: f64,
    pub freed_gib: f64,
    pub trimmed_processes: u32,
    pub skipped_processes: u32,
    /// "done", "declined", "skipped", or "failed: <reason>"
    pub admin_step: String,
}

/// Exit codes of the elevated helper.
pub mod exit_code {
    pub const OK: i32 = 0;
    pub const PRIVILEGE_FAILED: i32 = 2;
    pub const NTDLL_MISSING: i32 = 3;
    pub const PURGE_FAILED: i32 = 4;
}

pub fn describe_helper_exit(code: i32) -> String {
    match code {
        exit_code::OK => "done".to_string(),
        exit_code::PRIVILEGE_FAILED => "failed: could not enable the memory-management privilege".to_string(),
        exit_code::NTDLL_MISSING => "failed: NtSetSystemInformation not available".to_string(),
        exit_code::PURGE_FAILED => "failed: Windows rejected the memory purge request".to_string(),
        other => format!("failed: helper exited with code {}", other),
    }
}

/// Processes never trimmed: trimming them makes the whole desktop or audio stutter.
pub const NEVER_TRIM: [&str; 9] = [
    "dwm.exe",
    "explorer.exe",
    "csrss.exe",
    "winlogon.exe",
    "audiodg.exe",
    "ctfmon.exe",
    "textinputhost.exe",
    "searchhost.exe",
    "llama-server.exe",
];

/// PIDs of `root` and all its descendants, from (pid, parent_pid) pairs. NOVA's window is
/// rendered by WebView2 child processes; trimming them made the app stutter after cleaning.
pub fn process_tree(root: u32, pairs: &[(u32, u32)]) -> std::collections::HashSet<u32> {
    let mut tree = std::collections::HashSet::new();
    tree.insert(root);
    loop {
        let before = tree.len();
        for &(pid, ppid) in pairs {
            if pid != ppid && tree.contains(&ppid) {
                tree.insert(pid);
            }
        }
        if tree.len() == before {
            return tree;
        }
    }
}

/// Should this process be trimmed? Pure rule, unit-tested.
pub fn should_trim(
    pid: u32,
    exe_name: &str,
    protected: &std::collections::HashSet<u32>,
    exclude: &[u32],
) -> bool {
    pid > 4
        && !protected.contains(&pid)
        && !exclude.contains(&pid)
        && !NEVER_TRIM.iter().any(|n| n.eq_ignore_ascii_case(exe_name))
}

/// Trims the working set of every process this user can open, except NOVA's own process
/// tree, `NEVER_TRIM`, `exclude` and the System Idle/System processes. Returns (trimmed, skipped).
#[cfg(windows)]
pub fn trim_user_working_sets(exclude: &[u32]) -> (u32, u32) {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
        TH32CS_SNAPPROCESS,
    };
    use windows_sys::Win32::System::ProcessStatus::EmptyWorkingSet;
    use windows_sys::Win32::System::Threading::{
        OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SET_QUOTA,
    };

    // 1. Snapshot (pid, parent, name).
    let mut procs: Vec<(u32, u32, String)> = Vec::new();
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snap == INVALID_HANDLE_VALUE {
            return (0, 0);
        }
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        if Process32FirstW(snap, &mut entry) != 0 {
            loop {
                let end = entry.szExeFile.iter().position(|&c| c == 0).unwrap_or(0);
                procs.push((
                    entry.th32ProcessID,
                    entry.th32ParentProcessID,
                    String::from_utf16_lossy(&entry.szExeFile[..end]),
                ));
                if Process32NextW(snap, &mut entry) == 0 {
                    break;
                }
            }
        }
        CloseHandle(snap);
    }

    // 2. Protect NOVA itself and everything it started (WebView2, llama-server).
    let pairs: Vec<(u32, u32)> = procs.iter().map(|(p, pp, _)| (*p, *pp)).collect();
    let protected = process_tree(std::process::id(), &pairs);

    // 3. Trim the rest that this user is allowed to open.
    let mut trimmed = 0u32;
    let mut skipped = 0u32;
    for (pid, _, name) in &procs {
        if !should_trim(*pid, name, &protected, exclude) {
            continue;
        }
        unsafe {
            let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA, 0, *pid);
            if h.is_null() {
                skipped += 1;
                continue;
            }
            if EmptyWorkingSet(h) != 0 {
                trimmed += 1;
            } else {
                skipped += 1;
            }
            CloseHandle(h);
        }
    }
    (trimmed, skipped)
}

#[cfg(not(windows))]
pub fn trim_user_working_sets(_exclude: &[u32]) -> (u32, u32) {
    (0, 0)
}

/// Starts nova-chat.exe elevated with `PURGE_FLAG` (Windows shows a UAC prompt) and waits
/// up to 60 s. Returns the admin_step text for the report.
#[cfg(windows)]
pub fn run_elevated_purge() -> String {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_CANCELLED};
    use windows_sys::Win32::System::Threading::{GetExitCodeProcess, WaitForSingleObject};
    use windows_sys::Win32::UI::Shell::{ShellExecuteExW, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW};

    let exe = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => return format!("failed: cannot locate nova-chat.exe ({})", e),
    };
    let wide = |s: &std::ffi::OsStr| -> Vec<u16> { s.encode_wide().chain(std::iter::once(0)).collect() };
    let verb = wide(std::ffi::OsStr::new("runas"));
    let file = wide(exe.as_os_str());
    let params = wide(std::ffi::OsStr::new(PURGE_FLAG));

    unsafe {
        let mut info: SHELLEXECUTEINFOW = std::mem::zeroed();
        info.cbSize = std::mem::size_of::<SHELLEXECUTEINFOW>() as u32;
        info.fMask = SEE_MASK_NOCLOSEPROCESS;
        info.lpVerb = verb.as_ptr();
        info.lpFile = file.as_ptr();
        info.lpParameters = params.as_ptr();
        info.nShow = 0; // SW_HIDE

        if ShellExecuteExW(&mut info) == 0 {
            return if GetLastError() == ERROR_CANCELLED {
                "declined".to_string()
            } else {
                format!("failed: could not start the admin step (error {})", GetLastError())
            };
        }
        if info.hProcess.is_null() {
            return "failed: no process handle for the admin step".to_string();
        }
        let wait = WaitForSingleObject(info.hProcess, 60_000);
        let mut code: u32 = 0;
        let got = GetExitCodeProcess(info.hProcess, &mut code);
        CloseHandle(info.hProcess);
        if wait != 0 {
            return "failed: admin step did not finish within 60 s".to_string();
        }
        if got == 0 {
            return "failed: could not read the admin step result".to_string();
        }
        describe_helper_exit(code as i32)
    }
}

#[cfg(not(windows))]
pub fn run_elevated_purge() -> String {
    "skipped".to_string()
}

/// Runs inside the elevated helper process. Returns an exit code from `exit_code`.
#[cfg(windows)]
pub fn perform_privileged_purge() -> i32 {
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_SUCCESS, LUID};
    use windows_sys::Win32::Security::{
        AdjustTokenPrivileges, LookupPrivilegeValueW, LUID_AND_ATTRIBUTES, SE_PRIVILEGE_ENABLED,
        TOKEN_ADJUST_PRIVILEGES, TOKEN_PRIVILEGES, TOKEN_QUERY,
    };
    use windows_sys::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

    const SYSTEM_MEMORY_LIST_INFORMATION: u32 = 80;
    // MemoryEmptyWorkingSets (2) is deliberately NOT used: it empties every process,
    // including NOVA's own window and the desktop, which made the UI stutter after cleaning.
    // Working sets are trimmed selectively by `trim_user_working_sets` instead.
    const MEMORY_FLUSH_MODIFIED_LIST: i32 = 3;
    const MEMORY_PURGE_STANDBY_LIST: i32 = 4;

    unsafe {
        // Enable SeProfileSingleProcessPrivilege (required for the memory list commands).
        let mut token = std::ptr::null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &mut token) == 0 {
            return exit_code::PRIVILEGE_FAILED;
        }
        let name: Vec<u16> = "SeProfileSingleProcessPrivilege\0".encode_utf16().collect();
        let mut luid: LUID = std::mem::zeroed();
        if LookupPrivilegeValueW(std::ptr::null(), name.as_ptr(), &mut luid) == 0 {
            CloseHandle(token);
            return exit_code::PRIVILEGE_FAILED;
        }
        let tp = TOKEN_PRIVILEGES {
            PrivilegeCount: 1,
            Privileges: [LUID_AND_ATTRIBUTES { Luid: luid, Attributes: SE_PRIVILEGE_ENABLED }],
        };
        let ok = AdjustTokenPrivileges(token, 0, &tp, 0, std::ptr::null_mut(), std::ptr::null_mut());
        let last = GetLastError();
        CloseHandle(token);
        if ok == 0 || last != ERROR_SUCCESS {
            return exit_code::PRIVILEGE_FAILED;
        }

        let ntdll = GetModuleHandleW(windows_sys::core::w!("ntdll.dll"));
        if ntdll.is_null() {
            return exit_code::NTDLL_MISSING;
        }
        let Some(sym) = GetProcAddress(ntdll, b"NtSetSystemInformation\0".as_ptr()) else {
            return exit_code::NTDLL_MISSING;
        };
        let nt_set: unsafe extern "system" fn(u32, *mut std::ffi::c_void, u32) -> i32 =
            std::mem::transmute(sym);

        let mut all_ok = true;
        for cmd in [MEMORY_FLUSH_MODIFIED_LIST, MEMORY_PURGE_STANDBY_LIST] {
            let mut c = cmd;
            let status = nt_set(
                SYSTEM_MEMORY_LIST_INFORMATION,
                &mut c as *mut i32 as *mut std::ffi::c_void,
                std::mem::size_of::<i32>() as u32,
            );
            if status != 0 {
                all_ok = false;
            }
        }
        if all_ok {
            exit_code::OK
        } else {
            exit_code::PURGE_FAILED
        }
    }
}

#[cfg(not(windows))]
pub fn perform_privileged_purge() -> i32 {
    exit_code::PURGE_FAILED
}

pub fn round2(v: f64) -> f64 {
    (v * 100.0).round() / 100.0
}

/// Builds the report from before/after free-RAM readings.
pub fn build_report(
    avail_before_gib: f64,
    avail_after_gib: f64,
    trimmed: u32,
    skipped: u32,
    admin_step: String,
) -> MemoryCleanReport {
    MemoryCleanReport {
        avail_before_gib: round2(avail_before_gib),
        avail_after_gib: round2(avail_after_gib),
        freed_gib: round2((avail_after_gib - avail_before_gib).max(0.0)),
        trimmed_processes: trimmed,
        skipped_processes: skipped,
        admin_step,
    }
}
