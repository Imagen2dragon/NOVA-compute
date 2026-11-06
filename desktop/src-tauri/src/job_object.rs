// Windows Job Object for bulletproof child process containment
// Guarantees that when NOVA Chat exits or terminates for any reason,
// the Windows kernel terminates llama-server and any spawned children immediately.

#[cfg(windows)]
pub struct JobObject {
    handle: windows_sys::Win32::Foundation::HANDLE,
}

#[cfg(windows)]
pub struct ContainedChild {
    pub h_process: windows_sys::Win32::Foundation::HANDLE,
    pub h_thread: windows_sys::Win32::Foundation::HANDLE,
    pub pid: u32,
    #[cfg(test)]
    pub force_timeout_for_test: bool,
    #[cfg(test)]
    pub force_wait_failed_for_test: bool,
}

#[cfg(windows)]
impl ContainedChild {
    pub fn is_alive(&self) -> bool {
        use windows_sys::Win32::System::Threading::GetExitCodeProcess;
        const STILL_ACTIVE: u32 = 259;
        unsafe {
            if self.h_process.is_null() {
                return false;
            }
            let mut exit_code: u32 = 0;
            if GetExitCodeProcess(self.h_process, &mut exit_code) != 0 {
                exit_code == STILL_ACTIVE
            } else {
                false
            }
        }
    }

    pub fn wait_for_exit(&self, timeout_ms: u32) -> bool {
        use windows_sys::Win32::System::Threading::WaitForSingleObject;
        const WAIT_OBJECT_0: u32 = 0;
        unsafe {
            if self.h_process.is_null() {
                return true;
            }
            WaitForSingleObject(self.h_process, timeout_ms) == WAIT_OBJECT_0
        }
    }

    pub fn terminate(&mut self) -> bool {
        self.terminate_with_timeout(5000)
    }

    pub fn terminate_with_timeout(&mut self, timeout_ms: u32) -> bool {
        #[cfg(test)]
        if self.force_timeout_for_test || self.force_wait_failed_for_test {
            return false;
        }

        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{TerminateProcess, WaitForSingleObject};
        const WAIT_OBJECT_0: u32 = 0;
        const WAIT_TIMEOUT: u32 = 258;
        const WAIT_FAILED: u32 = 0xFFFFFFFF;
        unsafe {
            if self.h_process.is_null() {
                return true;
            }
            let _ = TerminateProcess(self.h_process, 0);
            let wait_res = WaitForSingleObject(self.h_process, timeout_ms);
            match wait_res {
                WAIT_OBJECT_0 => {
                    CloseHandle(self.h_process);
                    self.h_process = std::ptr::null_mut();
                    if !self.h_thread.is_null() {
                        CloseHandle(self.h_thread);
                        self.h_thread = std::ptr::null_mut();
                    }
                    true
                }
                WAIT_TIMEOUT => {
                    // Timeout: process has not terminated within timeout_ms
                    // Retain process handle; caller retains model lock
                    false
                }
                WAIT_FAILED => {
                    // Wait call failed (e.g. invalid handle or synchronization error)
                    // Retain process handle; fail closed, caller retains model lock
                    false
                }
                _ => {
                    // Any other return code (e.g. WAIT_ABANDONED): fail closed
                    false
                }
            }
        }
    }
}

#[cfg(windows)]
impl Drop for ContainedChild {
    fn drop(&mut self) {
        self.terminate();
    }
}

unsafe impl Send for ContainedChild {}
unsafe impl Sync for ContainedChild {}

#[cfg(windows)]
impl JobObject {
    pub fn new() -> Result<Self, String> {
        use windows_sys::Win32::System::JobObjects::{
            CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };

        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return Err("Failed to create Win32 Job Object".to_string());
            }

            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

            let res = SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );

            if res == 0 {
                windows_sys::Win32::Foundation::CloseHandle(handle);
                return Err("Failed to set JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE".to_string());
            }

            Ok(Self { handle })
        }
    }

    pub fn assign_process(
        &self,
        process_handle: windows_sys::Win32::Foundation::HANDLE,
    ) -> Result<(), String> {
        use windows_sys::Win32::System::JobObjects::AssignProcessToJobObject;

        unsafe {
            let res = AssignProcessToJobObject(self.handle, process_handle);
            if res == 0 {
                return Err(format!(
                    "Failed to assign process to Job Object (code: {})",
                    windows_sys::Win32::Foundation::GetLastError()
                ));
            }
            Ok(())
        }
    }

    /// Terminates all processes assigned to this Job Object kernel-side
    pub fn terminate_jobs(&self) -> Result<(), String> {
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;

        unsafe {
            if !self.handle.is_null() {
                let res = TerminateJobObject(self.handle, 1);
                if res == 0 {
                    return Err(format!(
                        "TerminateJobObject failed (code: {})",
                        windows_sys::Win32::Foundation::GetLastError()
                    ));
                }
            }
            Ok(())
        }
    }

    /// Queries the kernel for the count of currently active processes in the Job Object
    pub fn get_active_processes(&self) -> Result<u32, String> {
        use windows_sys::Win32::System::JobObjects::{
            JobObjectBasicAccountingInformation, QueryInformationJobObject,
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
        };

        unsafe {
            if self.handle.is_null() {
                return Ok(0);
            }
            let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = std::mem::zeroed();
            let mut returned_len: u32 = 0;
            let res = QueryInformationJobObject(
                self.handle,
                JobObjectBasicAccountingInformation,
                &mut info as *mut _ as *mut _,
                std::mem::size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                &mut returned_len,
            );
            if res == 0 {
                return Err(format!(
                    "QueryInformationJobObject failed (code: {})",
                    windows_sys::Win32::Foundation::GetLastError()
                ));
            }
            Ok(info.ActiveProcesses)
        }
    }

    /// Polls until the Job Object's active process population reaches exactly zero, or timeout expires
    pub fn wait_for_job_empty(&self, timeout_ms: u32) -> bool {
        let start = std::time::Instant::now();
        let timeout = std::time::Duration::from_millis(timeout_ms as u64);
        loop {
            match self.get_active_processes() {
                Ok(0) => return true,
                Ok(_) => {
                    if start.elapsed() >= timeout {
                        return false;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(15));
                }
                Err(_) => {
                    if start.elapsed() >= timeout {
                        return false;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(15));
                }
            }
        }
    }

    pub fn spawn_child_suspended(&self, command_line: &str) -> Result<ContainedChild, String> {
        self.spawn_child_suspended_with_handle(command_line, None)
    }

    pub fn spawn_child_suspended_with_handle(
        &self,
        command_line: &str,
        handle_to_duplicate: Option<windows_sys::Win32::Foundation::HANDLE>,
    ) -> Result<ContainedChild, String> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::{CloseHandle, DuplicateHandle};
        use windows_sys::Win32::System::JobObjects::AssignProcessToJobObject;
        use windows_sys::Win32::System::Threading::{
            CreateProcessW, GetCurrentProcess, ResumeThread, TerminateProcess,
            BELOW_NORMAL_PRIORITY_CLASS, CREATE_NO_WINDOW, CREATE_SUSPENDED, PROCESS_INFORMATION,
            STARTUPINFOW,
        };
        const DUPLICATE_SAME_ACCESS: u32 = 0x00000002;

        let mut wide_cmd: Vec<u16> = std::ffi::OsStr::new(command_line).encode_wide().collect();
        wide_cmd.push(0);

        unsafe {
            let mut si: STARTUPINFOW = std::mem::zeroed();
            si.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
            let mut pi: PROCESS_INFORMATION = std::mem::zeroed();

            let flags = CREATE_SUSPENDED | CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS;

            let success = CreateProcessW(
                std::ptr::null(),
                wide_cmd.as_mut_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                0, // bInheritHandles = FALSE
                flags,
                std::ptr::null(),
                std::ptr::null(),
                &si,
                &mut pi,
            );

            if success == 0 {
                return Err(format!(
                    "CreateProcessW failed with error code: {}",
                    windows_sys::Win32::Foundation::GetLastError()
                ));
            }

            // Assign process to Job Object BEFORE resuming thread
            if AssignProcessToJobObject(self.handle, pi.hProcess) == 0 {
                let err = format!(
                    "AssignProcessToJobObject failed with error: {}",
                    windows_sys::Win32::Foundation::GetLastError()
                );
                let _ = TerminateProcess(pi.hProcess, 1);
                CloseHandle(pi.hProcess);
                CloseHandle(pi.hThread);
                return Err(err);
            }

            // If a restrictive model handle was provided, duplicate it into the child process's
            // private handle table BEFORE resuming thread execution!
            // This guarantees the child directly owns a FILE_SHARE_READ lock throughout its lifetime,
            // persisting even if the parent crashes.
            if let Some(src_handle) = handle_to_duplicate {
                let mut target_handle = std::ptr::null_mut();
                let dup_ok = DuplicateHandle(
                    GetCurrentProcess(),
                    src_handle,
                    pi.hProcess,
                    &mut target_handle,
                    0,
                    0, // bInheritHandle = FALSE
                    DUPLICATE_SAME_ACCESS,
                );
                if dup_ok == 0 {
                    let err = format!(
                        "DuplicateHandle of restrictive model lock into child failed: Win32 error {}",
                        windows_sys::Win32::Foundation::GetLastError()
                    );
                    let _ = TerminateProcess(pi.hProcess, 1);
                    CloseHandle(pi.hProcess);
                    CloseHandle(pi.hThread);
                    return Err(err);
                }
            }

            // Successfully assigned and handle duplicated; now resume primary thread
            if ResumeThread(pi.hThread) == u32::MAX {
                let err = format!(
                    "ResumeThread failed with error: {}",
                    windows_sys::Win32::Foundation::GetLastError()
                );
                let _ = TerminateProcess(pi.hProcess, 1);
                CloseHandle(pi.hProcess);
                CloseHandle(pi.hThread);
                return Err(err);
            }

            Ok(ContainedChild {
                h_process: pi.hProcess,
                h_thread: pi.hThread,
                pid: pi.dwProcessId,
                #[cfg(test)]
                force_timeout_for_test: false,
                #[cfg(test)]
                force_wait_failed_for_test: false,
            })
        }
    }
}

#[cfg(windows)]
impl Drop for JobObject {
    fn drop(&mut self) {
        unsafe {
            if !self.handle.is_null() {
                windows_sys::Win32::Foundation::CloseHandle(self.handle);
            }
        }
    }
}

unsafe impl Send for JobObject {}
unsafe impl Sync for JobObject {}

#[cfg(not(windows))]
pub struct JobObject;

#[cfg(not(windows))]
pub struct ContainedChild {
    pub pid: u32,
    #[cfg(test)]
    pub force_timeout_for_test: bool,
    #[cfg(test)]
    pub force_wait_failed_for_test: bool,
}

#[cfg(not(windows))]
impl ContainedChild {
    pub fn is_alive(&self) -> bool {
        false
    }
    pub fn wait_for_exit(&self, _timeout_ms: u32) -> bool {
        true
    }
    pub fn terminate(&mut self) -> bool {
        true
    }
    pub fn terminate_with_timeout(&mut self, _timeout_ms: u32) -> bool {
        true
    }
}

#[cfg(not(windows))]
impl JobObject {
    pub fn new() -> Result<Self, String> {
        Ok(Self)
    }
    pub fn assign_process(&self, _handle: std::os::raw::c_ulong) -> Result<(), String> {
        Ok(())
    }
    pub fn spawn_child_suspended(&self, _cmd: &str) -> Result<ContainedChild, String> {
        Ok(ContainedChild {
            pid: 0,
            #[cfg(test)]
            force_timeout_for_test: false,
            #[cfg(test)]
            force_wait_failed_for_test: false,
        })
    }
    pub fn terminate_jobs(&self) -> Result<(), String> {
        Ok(())
    }
    pub fn get_active_processes(&self) -> Result<u32, String> {
        Ok(0)
    }
    pub fn wait_for_job_empty(&self, _timeout_ms: u32) -> bool {
        true
    }
}
