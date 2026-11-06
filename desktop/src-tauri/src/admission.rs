use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::ffi::c_void;
use std::fs::File;
use std::io::Read;
use std::path::Path;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct GateResult {
    pub gate_name: String,
    pub passed: bool,
    pub required_str: String,
    pub measured_str: String,
    pub failure_reason: Option<String>,
}

pub use crate::resource_advisor::ProcessCandidate;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GgufHeaderInfo {
    pub magic: String,
    pub version: u32,
    pub tensor_count: u64,
    pub kv_count: u64,
    pub file_size_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PreflightReport {
    pub all_passed: bool,
    pub timestamp_utc: String,
    pub timestamp_eat: String,
    pub monotonic_timestamp_secs: u64,
    #[serde(skip, default = "Instant::now")]
    pub captured_at: Instant,
    pub gates: Vec<GateResult>,
    pub avail_ram_bytes: u64,
    pub total_ram_bytes: u64,
    pub ram_deficit_bytes: i64,
    pub ram_surplus_bytes: i64,
    pub avail_ram_gib: f64,
    pub total_ram_gib: f64,
    pub ram_deficit_gib: f64,
    pub ram_surplus_gib: f64,
    pub commit_pct: f64,
    pub free_vram_mib: f64,
    pub total_vram_mib: f64,
    pub competing_gpu_apps: u32,
    pub free_disk_gib: f64,
    pub top_processes: Vec<ProcessCandidate>,
    pub model_valid: bool,
    pub model_info: Option<GgufHeaderInfo>,
    pub error_message: Option<String>,
    #[serde(default)]
    pub budget: Option<crate::memory_budget::ResourceBudgetCalculation>,
    #[serde(default)]
    pub recovery_summary: Option<crate::resource_advisor::ResourceRecoverySummary>,
    #[serde(default)]
    pub selected_profile: Option<crate::memory_budget::RuntimeProfile>,
    #[serde(default)]
    pub integrity_state: Option<crate::model_integrity::ModelIntegrityState>,
}

impl PreflightReport {
    pub fn failure_summary(&self) -> String {
        let mut failed: Vec<String> = self
            .gates
            .iter()
            .filter(|g| !g.passed)
            .map(|g| {
                format!(
                    "{}: {}",
                    g.gate_name,
                    g.failure_reason.as_deref().unwrap_or("Failed")
                )
            })
            .collect();
        if !self.model_valid {
            failed.push("model_file: Model file verification failed".to_string());
        }
        if failed.is_empty() {
            "All gates passed".to_string()
        } else {
            failed.join("; ")
        }
    }

    /// Checks freshness using monotonic in-process Instant without integer-second truncation.
    pub fn is_stale(&self, max_age: Duration) -> bool {
        self.captured_at.elapsed() > max_age
    }
}

// ---------------------------------------------------------------------------
// GGUF Model Header Validator
// ---------------------------------------------------------------------------

pub fn validate_gguf_file(path_str: &str) -> Result<GgufHeaderInfo, String> {
    let p = Path::new(path_str);
    if !p.exists() {
        return Err(format!("Model file not found at: {}", path_str));
    }
    if !p.is_file() {
        return Err(format!("Model path is not a file: {}", path_str));
    }

    let metadata = std::fs::metadata(p)
        .map_err(|e| format!("Failed to read metadata for {}: {}", path_str, e))?;
    let file_size_bytes = metadata.len();

    // Model must be a non-trivial file (at least 10 MiB)
    if file_size_bytes < 10 * 1024 * 1024 {
        return Err(format!(
            "Model file size ({} bytes) is too small to be a valid LLM weight file.",
            file_size_bytes
        ));
    }

    let mut f =
        File::open(p).map_err(|e| format!("Failed to open model file {}: {}", path_str, e))?;
    let mut header = [0u8; 24];
    f.read_exact(&mut header)
        .map_err(|e| format!("Failed to read GGUF header from {}: {}", path_str, e))?;

    // Byte 0-3: Magic 'GGUF' (0x47, 0x47, 0x55, 0x46)
    if &header[0..4] != b"GGUF" {
        return Err(format!(
            "Invalid GGUF header magic: expected 'GGUF', found {:?}",
            &header[0..4]
        ));
    }

    let version = u32::from_le_bytes(header[4..8].try_into().unwrap());
    if version < 2 || version > 3 {
        return Err(format!(
            "Unsupported GGUF version {}. Supported versions are 2 and 3.",
            version
        ));
    }

    let tensor_count = u64::from_le_bytes(header[8..16].try_into().unwrap());
    let kv_count = u64::from_le_bytes(header[16..24].try_into().unwrap());

    if tensor_count == 0 {
        return Err("GGUF header reports zero tensors.".to_string());
    }

    Ok(GgufHeaderInfo {
        magic: "GGUF".to_string(),
        version,
        tensor_count,
        kv_count,
        file_size_bytes,
    })
}

/// Retrieves validated model metadata from cache if file metadata has not changed,
/// avoiding expensive repeated disk I/O on application startup.
pub fn get_or_validate_model_info(path_str: &str, force_revalidate: bool) -> Result<GgufHeaderInfo, String> {
    let p = Path::new(path_str);
    if !p.exists() || !p.is_file() {
        return Err(format!("Model file not found at: {}", path_str));
    }

    let meta = std::fs::metadata(p).map_err(|e| format!("Failed to read metadata: {}", e))?;
    let file_size = meta.len();
    let mtime = meta
        .modified()
        .map(|t| t.duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs())
        .unwrap_or(0);

    if !force_revalidate {
        if let Some(cached) = crate::storage::load_model_cache() {
            if cached.path == path_str
                && cached.file_size_bytes == file_size
                && cached.modified_timestamp_secs == mtime
            {
                return Ok(GgufHeaderInfo {
                    magic: cached.magic,
                    version: cached.version,
                    tensor_count: cached.tensor_count,
                    kv_count: cached.kv_count,
                    file_size_bytes: cached.file_size_bytes,
                });
            }
        }
    }

    let info = validate_gguf_file(path_str)?;

    let existing_cache = crate::storage::load_model_cache();
    let (sha256_to_save, is_verified) = if let Some(ref c) = existing_cache {
        if c.path == path_str
            && c.file_size_bytes == file_size
            && c.modified_timestamp_secs == mtime
        {
            (c.sha256.clone(), c.is_cryptographically_verified)
        } else {
            (String::new(), false)
        }
    } else {
        (String::new(), false)
    };

    let cache_entry = crate::storage::ModelCacheEntry {
        path: path_str.to_string(),
        file_size_bytes: file_size,
        modified_timestamp_secs: mtime,
        sha256: sha256_to_save,
        magic: info.magic.clone(),
        version: info.version,
        tensor_count: info.tensor_count,
        kv_count: info.kv_count,
        last_validated_utc: Utc::now().to_rfc3339(),
        is_cryptographically_verified: is_verified,
        volume_serial_number: None,
        file_index: None,
        last_write_time_raw: None,
    };
    let _ = crate::storage::save_model_cache(&cache_entry);

    Ok(info)
}

// ---------------------------------------------------------------------------
// Win32 System Memory & Disk Queries
// ---------------------------------------------------------------------------

#[cfg(windows)]
pub fn query_system_memory_raw() -> Result<(u64, u64, f64), String> {
    use windows_sys::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};

    unsafe {
        let mut mem: MEMORYSTATUSEX = std::mem::zeroed();
        mem.dwLength = std::mem::size_of::<MEMORYSTATUSEX>() as u32;

        if GlobalMemoryStatusEx(&mut mem) == 0 {
            return Err("GlobalMemoryStatusEx failed".to_string());
        }

        if mem.ullTotalPageFile == 0 {
            return Err("Invalid system commit limit: 0 bytes reported".to_string());
        }
        if mem.ullAvailPageFile > mem.ullTotalPageFile {
            return Err(
                "Inconsistent commit metrics: available exceeds total page file".to_string(),
            );
        }

        let committed = mem.ullTotalPageFile.saturating_sub(mem.ullAvailPageFile);
        let commit_pct = (committed as f64 / mem.ullTotalPageFile as f64) * 100.0;

        if commit_pct < 0.0 || commit_pct > 100.0 {
            return Err(format!(
                "Commit percentage out of valid range: {:.1}%",
                commit_pct
            ));
        }

        Ok((mem.ullAvailPhys, mem.ullTotalPhys, commit_pct))
    }
}

#[cfg(not(windows))]
pub fn query_system_memory_raw() -> Result<(u64, u64, f64), String> {
    Err("System memory telemetry is only supported on Windows".to_string())
}

pub fn query_system_memory() -> Result<(f64, f64, f64), String> {
    let (avail_bytes, total_bytes, commit_pct) = query_system_memory_raw()?;
    let avail_gib = avail_bytes as f64 / (1024.0 * 1024.0 * 1024.0);
    let total_gib = total_bytes as f64 / (1024.0 * 1024.0 * 1024.0);
    Ok((avail_gib, total_gib, commit_pct))
}

#[cfg(windows)]
pub fn query_disk_free(target_path: &str) -> Result<f64, String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;

    let path_str = if target_path.is_empty() {
        "C:\\".to_string()
    } else {
        Path::new(target_path)
            .components()
            .next()
            .map(|c| c.as_os_str().to_string_lossy().to_string())
            .unwrap_or_else(|| "C:\\".to_string())
    };

    let mut wide_path: Vec<u16> = std::ffi::OsStr::new(&path_str).encode_wide().collect();
    wide_path.push(0);

    unsafe {
        let mut free_bytes_avail: u64 = 0;
        let mut total_num_bytes: u64 = 0;
        let mut total_free_bytes: u64 = 0;

        let ret = GetDiskFreeSpaceExW(
            wide_path.as_ptr(),
            &mut free_bytes_avail,
            &mut total_num_bytes,
            &mut total_free_bytes,
        );

        if ret == 0 {
            return Err(format!("GetDiskFreeSpaceExW failed for {}", path_str));
        }

        let free_gib = free_bytes_avail as f64 / (1024.0 * 1024.0 * 1024.0);
        Ok(free_gib)
    }
}

#[cfg(not(windows))]
pub fn query_disk_free(_target_path: &str) -> Result<f64, String> {
    Err("Disk storage telemetry is only supported on Windows".to_string())
}

// ---------------------------------------------------------------------------
// Dynamic NVML GPU Telemetry Loader (Fail-Closed, RAII)
// ---------------------------------------------------------------------------

pub const NVML_SUCCESS: i32 = 0;
pub const NVML_ERROR_NOT_FOUND: i32 = 6;
pub const NVML_ERROR_INSUFFICIENT_SIZE: i32 = 7;

#[repr(C)]
struct NvmlMemory {
    total: u64,
    free: u64,
    used: u64,
}

#[repr(C)]
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq)]
pub struct NvmlProcessInfo {
    pub pid: u32,
    pub used_gpu_memory: u64,
    pub gpu_instance_id: u32,
    pub compute_instance_id: u32,
}

type NvmlReturn = i32;
type NvmlDevice = *mut c_void;

#[cfg(windows)]
#[link(name = "kernel32")]
extern "system" {
    fn FreeLibrary(
        h_lib_module: windows_sys::Win32::Foundation::HMODULE,
    ) -> windows_sys::Win32::Foundation::BOOL;
}

#[cfg(windows)]
struct NvmlLib(windows_sys::Win32::Foundation::HMODULE);

#[cfg(windows)]
impl Drop for NvmlLib {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                FreeLibrary(self.0);
            }
        }
    }
}

/// Evaluates documented two-call NVML process query behavior.
/// Call 1: pass null buffer or 0 count.
/// - If ret1 == 0 and count1 == 0: exactly 0 compute processes.
/// - If ret1 == 0 and count1 > 0: call2 fetches details. If call2 == 0: count1 processes.
/// - If ret1 == 7 (INSUFFICIENT_SIZE): call2 fetches with allocated buffer. If call2 == 0: count2 processes.
/// - If ret1 == 6 (NOT_FOUND) or any other code: fail closed!
pub fn parse_nvml_process_results<F>(ret1: i32, count1: u32, call2: F) -> Result<u32, String>
where
    F: FnOnce(u32) -> (i32, u32),
{
    match ret1 {
        NVML_SUCCESS => {
            if count1 == 0 {
                Ok(0)
            } else {
                let (ret2, count2) = call2(count1);
                if ret2 == NVML_SUCCESS {
                    Ok(count2)
                } else {
                    Err(format!(
                        "NVML process query call 2 failed with error code {}",
                        ret2
                    ))
                }
            }
        }
        NVML_ERROR_INSUFFICIENT_SIZE => {
            let req_size = count1.max(1);
            let (ret2, count2) = call2(req_size);
            if ret2 == NVML_SUCCESS {
                Ok(count2)
            } else {
                Err(format!(
                    "NVML process query call 2 after INSUFFICIENT_SIZE failed with error code {}",
                    ret2
                ))
            }
        }
        NVML_ERROR_NOT_FOUND => Err(
            "NVML device or compute process information not found (NVML_ERROR_NOT_FOUND = 6)"
                .to_string(),
        ),
        other => Err(format!(
            "NVML compute process query failed with error code {}",
            other
        )),
    }
}

/// Pure check for the GPU contention gate exemption (Project Owner decision 2026-11-06):
/// true only if `nt_image_path` is exactly
/// `<system drive device>\<SystemRoot without drive>\System32\dwm.exe`, case-insensitive.
/// Under Windows WDDM, dwm.exe (the desktop compositor) is always listed by NVML, so a
/// strict zero-process gate could never pass. No other process is exempt.
pub fn is_dwm_image_path(
    nt_image_path: Option<&str>,
    system_drive_device: Option<&str>,
    system_root: Option<&str>,
) -> bool {
    let (Some(path), Some(device), Some(root)) = (nt_image_path, system_drive_device, system_root)
    else {
        return false;
    };
    if path.is_empty() || device.is_empty() || root.len() < 3 || root.as_bytes()[1] != b':' {
        return false;
    }
    let expected = format!(
        "{}{}\\System32\\dwm.exe",
        device.trim_end_matches('\\'),
        root[2..].trim_end_matches('\\')
    );
    path.eq_ignore_ascii_case(&expected)
}

/// Reads a process's NT image path without opening the process (dwm.exe denies
/// OpenProcess to ordinary users), via NtQuerySystemInformation(SystemProcessIdInformation).
#[cfg(windows)]
fn query_nt_image_path(pid: u32) -> Option<String> {
    use windows_sys::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};

    #[repr(C)]
    struct UnicodeString {
        length: u16,
        maximum_length: u16,
        buffer: *mut u16,
    }
    #[repr(C)]
    struct SystemProcessIdInformation {
        process_id: *mut c_void,
        image_name: UnicodeString,
    }
    const SYSTEM_PROCESS_ID_INFORMATION: u32 = 88;

    if pid == 0 {
        return None;
    }
    unsafe {
        let ntdll = GetModuleHandleW(windows_sys::core::w!("ntdll.dll"));
        if ntdll.is_null() {
            return None;
        }
        let sym = GetProcAddress(ntdll, b"NtQuerySystemInformation\0".as_ptr())?;
        let nt_query: unsafe extern "system" fn(u32, *mut c_void, u32, *mut u32) -> i32 =
            std::mem::transmute(sym);

        let mut buf = vec![0u16; 1024];
        let mut info = SystemProcessIdInformation {
            process_id: pid as usize as *mut c_void,
            image_name: UnicodeString {
                length: 0,
                maximum_length: (buf.len() * 2) as u16,
                buffer: buf.as_mut_ptr(),
            },
        };
        let status = nt_query(
            SYSTEM_PROCESS_ID_INFORMATION,
            &mut info as *mut _ as *mut c_void,
            std::mem::size_of::<SystemProcessIdInformation>() as u32,
            std::ptr::null_mut(),
        );
        if status != 0 || info.image_name.length == 0 {
            return None;
        }
        let chars = (info.image_name.length / 2) as usize;
        Some(String::from_utf16_lossy(&buf[..chars.min(buf.len())]))
    }
}

#[cfg(windows)]
fn query_system_drive_device() -> Option<String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::QueryDosDeviceW;

    let drive = std::env::var("SystemDrive").ok()?;
    let wide: Vec<u16> = std::ffi::OsStr::new(&drive)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let mut buf = vec![0u16; 1024];
    let n = unsafe { QueryDosDeviceW(wide.as_ptr(), buf.as_mut_ptr(), buf.len() as u32) };
    if n == 0 {
        return None;
    }
    let end = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
    let s = String::from_utf16_lossy(&buf[..end]);
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

/// True only for the verified system dwm.exe; false on any failure (fail closed).
#[cfg(windows)]
pub fn is_exempt_system_gpu_process(pid: u32) -> bool {
    let path = query_nt_image_path(pid);
    let device = query_system_drive_device();
    let root = std::env::var("SystemRoot").ok();
    is_dwm_image_path(path.as_deref(), device.as_deref(), root.as_deref())
}

#[cfg(windows)]
pub fn query_nvml_gpu() -> Result<(f64, f64, u32), String> {
    use windows_sys::Win32::System::LibraryLoader::{GetProcAddress, LoadLibraryW};

    let nvml_dll = windows_sys::core::w!("nvml.dll");
    unsafe {
        let h_module = LoadLibraryW(nvml_dll);
        if h_module.is_null() {
            return Err("NVIDIA NVML library (nvml.dll) could not be loaded".to_string());
        }
        let lib = NvmlLib(h_module);

        let sym_init = GetProcAddress(lib.0, b"nvmlInit_v2\0".as_ptr());
        let sym_get_handle = GetProcAddress(lib.0, b"nvmlDeviceGetHandleByIndex_v2\0".as_ptr());
        let sym_get_mem = GetProcAddress(lib.0, b"nvmlDeviceGetMemoryInfo\0".as_ptr());
        let sym_get_procs =
            GetProcAddress(lib.0, b"nvmlDeviceGetComputeRunningProcesses_v2\0".as_ptr());
        let sym_shutdown = GetProcAddress(lib.0, b"nvmlShutdown\0".as_ptr());

        if sym_init.is_none()
            || sym_get_handle.is_none()
            || sym_get_mem.is_none()
            || sym_get_procs.is_none()
        {
            return Err("Required NVML symbols missing from nvml.dll".to_string());
        }

        let init_fn: unsafe extern "system" fn() -> NvmlReturn = std::mem::transmute(sym_init);
        let get_handle_fn: unsafe extern "system" fn(u32, *mut NvmlDevice) -> NvmlReturn =
            std::mem::transmute(sym_get_handle);
        let get_mem_fn: unsafe extern "system" fn(NvmlDevice, *mut NvmlMemory) -> NvmlReturn =
            std::mem::transmute(sym_get_mem);
        let get_procs_fn: unsafe extern "system" fn(
            NvmlDevice,
            *mut u32,
            *mut NvmlProcessInfo,
        ) -> NvmlReturn = std::mem::transmute(sym_get_procs);
        let shutdown_fn: Option<unsafe extern "system" fn() -> NvmlReturn> =
            sym_shutdown.map(|p| std::mem::transmute(p));

        if init_fn() != NVML_SUCCESS {
            return Err("nvmlInit_v2 failed to initialize GPU interface".to_string());
        }

        // RAII session guard to ensure nvmlShutdown is invoked
        struct NvmlSessionGuard(Option<unsafe extern "system" fn() -> NvmlReturn>);
        impl Drop for NvmlSessionGuard {
            fn drop(&mut self) {
                if let Some(shut) = self.0 {
                    unsafe {
                        shut();
                    }
                }
            }
        }
        let _guard = NvmlSessionGuard(shutdown_fn);

        let mut device: NvmlDevice = std::ptr::null_mut();
        if get_handle_fn(0, &mut device) != NVML_SUCCESS {
            return Err("nvmlDeviceGetHandleByIndex_v2(0) failed to get device 0".to_string());
        }

        let mut mem: NvmlMemory = std::mem::zeroed();
        if get_mem_fn(device, &mut mem) != NVML_SUCCESS {
            return Err("nvmlDeviceGetMemoryInfo failed to read VRAM telemetry".to_string());
        }

        // Call 1: pass null buffer to query compute process count
        let mut proc_count: u32 = 0;
        let ret1 = get_procs_fn(device, &mut proc_count, std::ptr::null_mut());

        let final_count = parse_nvml_process_results(ret1, proc_count, |needed_count| {
            let mut buf = vec![NvmlProcessInfo::default(); needed_count as usize];
            let mut actual_count = needed_count;
            let ret2 = get_procs_fn(device, &mut actual_count, buf.as_mut_ptr());
            // Count only non-exempt processes; only the verified system dwm.exe is exempt.
            let listed = (actual_count as usize).min(buf.len());
            let competing = buf[..listed]
                .iter()
                .filter(|p| !is_exempt_system_gpu_process(p.pid))
                .count() as u32;
            (ret2, competing)
        })?;

        let free_vram_mib = mem.free as f64 / (1024.0 * 1024.0);
        let total_vram_mib = mem.total as f64 / (1024.0 * 1024.0);

        Ok((free_vram_mib, total_vram_mib, final_count))
    }
}

#[cfg(not(windows))]
pub fn query_nvml_gpu() -> Result<(f64, f64, u32), String> {
    Err("NVML GPU telemetry is only supported on Windows".to_string())
}

// ---------------------------------------------------------------------------
// Win32 ToolHelp32 Process Enumeration (Guidance Only)
// ---------------------------------------------------------------------------

#[cfg(windows)]
pub fn query_candidate_processes() -> Vec<ProcessCandidate> {
    use std::mem::size_of;
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
        TH32CS_SNAPPROCESS,
    };
    use windows_sys::Win32::System::ProcessStatus::{
        GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS_EX,
    };
    use windows_sys::Win32::System::Threading::{
        OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_VM_READ,
    };

    let mut process_map: HashMap<String, (usize, Vec<u32>, f64, f64)> = HashMap::new();

    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snapshot.is_null() {
            return Vec::new();
        }

        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = size_of::<PROCESSENTRY32W>() as u32;

        if Process32FirstW(snapshot, &mut entry) != 0 {
            loop {
                let name = String::from_utf16_lossy(
                    &entry.szExeFile[..entry
                        .szExeFile
                        .iter()
                        .position(|&c| c == 0)
                        .unwrap_or(entry.szExeFile.len())],
                )
                .to_lowercase();

                let pid = entry.th32ProcessID;
                if pid > 4 {
                    let h_proc =
                        OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, 0, pid);
                    if !h_proc.is_null() {
                        let mut pmc: PROCESS_MEMORY_COUNTERS_EX = std::mem::zeroed();
                        pmc.cb = size_of::<PROCESS_MEMORY_COUNTERS_EX>() as u32;
                        if GetProcessMemoryInfo(h_proc, &mut pmc as *mut _ as *mut _, pmc.cb) != 0 {
                            let ws_mb = pmc.WorkingSetSize as f64 / (1024.0 * 1024.0);
                            let priv_mb = pmc.PrivateUsage as f64 / (1024.0 * 1024.0);

                            let item = process_map.entry(name).or_insert((0, Vec::new(), 0.0, 0.0));
                            item.0 += 1;
                            item.1.push(pid);
                            item.2 += ws_mb;
                            item.3 += priv_mb;
                        }
                        CloseHandle(h_proc);
                    }
                }

                if Process32NextW(snapshot, &mut entry) == 0 {
                    break;
                }
            }
        }
        CloseHandle(snapshot);
    }

    let mut candidates: Vec<ProcessCandidate> = process_map
        .into_iter()
        .filter(|(name, _)| {
            name.ends_with(".exe")
                && !name.contains("system")
                && !name.contains("registry")
                && !name.contains("idle")
                && !name.contains("csrss")
                && !name.contains("smss")
                && !name.contains("services")
                && !name.contains("svchost")
        })
        .map(|(name, (instances, pids, ws_mb, priv_mb))| {
            let vram_est = if name.contains("llama-server") || name.contains("ollama") {
                Some(4200.0)
            } else {
                None
            };
            ProcessCandidate::build(
                name,
                instances,
                pids,
                (ws_mb * 10.0).round() / 10.0,
                Some((priv_mb * 10.0).round() / 10.0),
                vram_est,
            )
        })
        .collect();

    candidates.sort_by(|a, b| {
        b.working_set_mb
            .partial_cmp(&a.working_set_mb)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    candidates.truncate(6);
    candidates
}

#[cfg(not(windows))]
pub fn query_candidate_processes() -> Vec<ProcessCandidate> {
    Vec::new()
}

// ---------------------------------------------------------------------------
// Canonical 6-Gate Preflight Evaluation Engine
// ---------------------------------------------------------------------------

pub fn run_preflight_audit(model_path: &str) -> PreflightReport {
    run_preflight_audit_with_profile(model_path, None)
}

pub fn run_preflight_audit_with_profile(
    model_path: &str,
    profile: Option<&crate::memory_budget::RuntimeProfile>,
) -> PreflightReport {
    let mut telemetry_ok = true;
    let mut err_msg = None;

    // Gate 1 & 2 & 3: Win32 Memory & Commit
    let (avail_ram_bytes, total_ram_bytes, commit_pct) = match query_system_memory_raw() {
        Ok(v) => v,
        Err(e) => {
            telemetry_ok = false;
            err_msg = Some(format!("Memory telemetry error: {}", e));
            (0, 0, 100.0)
        }
    };
    let avail_ram_gib = avail_ram_bytes as f64 / (1024.0 * 1024.0 * 1024.0);
    let total_ram_gib = total_ram_bytes as f64 / (1024.0 * 1024.0 * 1024.0);

    // Gate 4 & 5: NVML GPU
    let (free_vram_mib, total_vram_mib, competing_gpu_apps) = match query_nvml_gpu() {
        Ok(v) => v,
        Err(e) => {
            telemetry_ok = false;
            let current_err = err_msg.unwrap_or_default();
            err_msg = Some(format!("{} | GPU telemetry error: {}", current_err, e));
            (0.0, 0.0, 999)
        }
    };

    // Gate 6: Model Drive Free Storage
    let free_disk_gib = match query_disk_free(model_path) {
        Ok(v) => v,
        Err(e) => {
            telemetry_ok = false;
            let current_err = err_msg.unwrap_or_default();
            err_msg = Some(format!("{} | Disk telemetry error: {}", current_err, e));
            0.0
        }
    };

    // Fast cached validation of model file
    let (model_valid, model_info) = if !model_path.trim().is_empty() {
        match get_or_validate_model_info(model_path, false) {
            Ok(info) => (true, Some(info)),
            Err(e) => {
                let current_err = err_msg.unwrap_or_default();
                err_msg = Some(format!("{} | Model validation error: {}", current_err, e));
                (false, None)
            }
        }
    } else {
        (false, None)
    };

    evaluate_admission_from_metrics_with_profile(
        avail_ram_gib,
        total_ram_gib,
        commit_pct,
        free_vram_mib,
        total_vram_mib,
        competing_gpu_apps,
        free_disk_gib,
        telemetry_ok,
        model_valid,
        model_info,
        err_msg,
        profile,
    )
}

pub fn evaluate_admission_from_metrics(
    avail_ram_gib: f64,
    total_ram_gib: f64,
    commit_pct: f64,
    free_vram_mib: f64,
    total_vram_mib: f64,
    competing_gpu_apps: u32,
    free_disk_gib: f64,
    telemetry_ok: bool,
    model_valid: bool,
    model_info: Option<GgufHeaderInfo>,
    err_msg: Option<String>,
) -> PreflightReport {
    evaluate_admission_from_metrics_with_profile(
        avail_ram_gib,
        total_ram_gib,
        commit_pct,
        free_vram_mib,
        total_vram_mib,
        competing_gpu_apps,
        free_disk_gib,
        telemetry_ok,
        model_valid,
        model_info,
        err_msg,
        None,
    )
}

pub fn evaluate_admission_from_metrics_with_profile(
    avail_ram_gib: f64,
    total_ram_gib: f64,
    commit_pct: f64,
    free_vram_mib: f64,
    total_vram_mib: f64,
    competing_gpu_apps: u32,
    free_disk_gib: f64,
    telemetry_ok: bool,
    model_valid: bool,
    model_info: Option<GgufHeaderInfo>,
    err_msg: Option<String>,
    profile: Option<&crate::memory_budget::RuntimeProfile>,
) -> PreflightReport {
    let active_profile = profile
        .cloned()
        .unwrap_or_else(|| crate::memory_budget::RuntimeProfile::find_by_id(crate::memory_budget::RuntimeProfileId::GpuFocused));

    let mut gates = Vec::new();

    // Gate 1: Sensor Telemetry Integrity Gate
    gates.push(GateResult {
        gate_name: "telemetry_integrity".to_string(),
        passed: telemetry_ok,
        required_str: "All sensors SUCCESS".to_string(),
        measured_str: if telemetry_ok {
            "Healthy".to_string()
        } else {
            "ERROR / UNAVAILABLE".to_string()
        },
        failure_reason: if telemetry_ok { None } else { err_msg.clone() },
    });

    // Gate 2: Available Physical RAM Gate (Configurable per profile, >= 10.00 GiB for Config D)
    let required_ram_bytes = (active_profile.required_ram_gib * 1024.0 * 1024.0 * 1024.0).round() as u64;
    let avail_ram_bytes = (avail_ram_gib * (1024.0 * 1024.0 * 1024.0)).round() as u64;
    let total_ram_bytes = (total_ram_gib * (1024.0 * 1024.0 * 1024.0)).round() as u64;

    let ram_passed = telemetry_ok && avail_ram_bytes >= required_ram_bytes;
    let (ram_deficit_bytes, ram_surplus_bytes) = if !telemetry_ok {
        (required_ram_bytes as i64, 0i64)
    } else if avail_ram_bytes >= required_ram_bytes {
        (0i64, (avail_ram_bytes - required_ram_bytes) as i64)
    } else {
        ((required_ram_bytes - avail_ram_bytes) as i64, 0i64)
    };
    let ram_deficit_gib = ram_deficit_bytes as f64 / (1024.0 * 1024.0 * 1024.0);
    let ram_surplus_gib = ram_surplus_bytes as f64 / (1024.0 * 1024.0 * 1024.0);

    gates.push(GateResult {
        gate_name: "avail_ram".to_string(),
        passed: ram_passed,
        required_str: format!(">= {:.2} GiB", active_profile.required_ram_gib),
        measured_str: if telemetry_ok {
            format!("{:.2} GiB", avail_ram_gib)
        } else {
            "UNAVAILABLE".to_string()
        },
        failure_reason: if ram_passed {
            None
        } else if !telemetry_ok {
            Some("Telemetry failure: cannot verify available physical RAM".to_string())
        } else {
            Some(format!(
                "Available RAM deficit: {:.2} GiB below required {:.2} GiB",
                ram_deficit_gib, active_profile.required_ram_gib
            ))
        },
    });

    // Gate 3: System Commit Limit Gate (< 90.0%)
    let commit_passed = telemetry_ok && (0.0..90.0).contains(&commit_pct);
    gates.push(GateResult {
        gate_name: "system_commit".to_string(),
        passed: commit_passed,
        required_str: "< 90.0%".to_string(),
        measured_str: if telemetry_ok {
            format!("{:.1}%", commit_pct)
        } else {
            "UNAVAILABLE".to_string()
        },
        failure_reason: if commit_passed {
            None
        } else if !telemetry_ok {
            Some("Telemetry failure: cannot verify system commit limit".to_string())
        } else {
            Some(format!(
                "System commit charge {:.1}% exceeds 90.0% safety threshold",
                commit_pct
            ))
        },
    });

    // Gate 4: Dedicated VRAM Gate (Configurable per profile, >= 5500.0 MiB for Config D)
    let required_vram_mib = active_profile.required_vram_mib;
    let vram_passed = telemetry_ok && free_vram_mib >= required_vram_mib;
    gates.push(GateResult {
        gate_name: "avail_vram".to_string(),
        passed: vram_passed,
        required_str: format!(">= {:.1} MiB", required_vram_mib),
        measured_str: if telemetry_ok {
            format!("{:.1} MiB", free_vram_mib)
        } else {
            "UNAVAILABLE".to_string()
        },
        failure_reason: if vram_passed {
            None
        } else if !telemetry_ok {
            Some("Telemetry failure: cannot verify dedicated VRAM".to_string())
        } else {
            Some(format!(
                "Free VRAM {:.1} MiB is below required {:.1} MiB",
                free_vram_mib, required_vram_mib
            ))
        },
    });

    // Gate 5: Competing GPU Compute Processes Gate (== 0)
    let gpu_procs_passed = telemetry_ok && competing_gpu_apps == 0;
    gates.push(GateResult {
        gate_name: "no_competing_gpu".to_string(),
        passed: gpu_procs_passed,
        required_str: "0 active compute apps".to_string(),
        measured_str: if telemetry_ok {
            format!("{} active apps", competing_gpu_apps)
        } else {
            "UNAVAILABLE".to_string()
        },
        failure_reason: if gpu_procs_passed {
            None
        } else if !telemetry_ok {
            Some("Telemetry failure: cannot verify competing GPU processes".to_string())
        } else {
            Some(format!(
                "{} competing GPU compute applications detected",
                competing_gpu_apps
            ))
        },
    });

    // Gate 6: Free Model Drive Storage Gate (>= 25.0 GiB)
    let disk_passed = telemetry_ok && free_disk_gib >= 25.0;
    gates.push(GateResult {
        gate_name: "disk_free".to_string(),
        passed: disk_passed,
        required_str: ">= 25.00 GiB".to_string(),
        measured_str: if telemetry_ok {
            format!("{:.2} GiB", free_disk_gib)
        } else {
            "UNAVAILABLE".to_string()
        },
        failure_reason: if disk_passed {
            None
        } else if !telemetry_ok {
            Some("Telemetry failure: cannot verify disk free space".to_string())
        } else {
            Some(format!(
                "Free disk space {:.2} GiB is below required 25.00 GiB",
                free_disk_gib
            ))
        },
    });

    let all_passed = gates.iter().all(|g| g.passed) && model_valid;
    let top_processes = query_candidate_processes();
    let recovery_summary = crate::resource_advisor::summarize_recovery(&top_processes);
    let budget = crate::memory_budget::ResourceBudgetCalculation::evaluate(
        &active_profile,
        avail_ram_gib,
        total_ram_gib,
        commit_pct,
        free_vram_mib,
        free_disk_gib,
        recovery_summary.potential_ram_recovery_gib,
        recovery_summary.potential_vram_recovery_mib,
        telemetry_ok,
    );

    let mono_now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);

    let single_now_utc = Utc::now();
    let timestamp_utc = single_now_utc.to_rfc3339();
    let eat_tz = chrono::FixedOffset::east_opt(3 * 3600).unwrap();
    let timestamp_eat = single_now_utc.with_timezone(&eat_tz).to_rfc3339();

    PreflightReport {
        all_passed,
        timestamp_utc,
        timestamp_eat,
        monotonic_timestamp_secs: mono_now,
        captured_at: Instant::now(),
        gates,
        avail_ram_bytes,
        total_ram_bytes,
        ram_deficit_bytes,
        ram_surplus_bytes,
        avail_ram_gib: (avail_ram_gib * 1000.0).round() / 1000.0,
        total_ram_gib: (total_ram_gib * 1000.0).round() / 1000.0,
        ram_deficit_gib: (ram_deficit_gib * 1000.0).round() / 1000.0,
        ram_surplus_gib: (ram_surplus_gib * 1000.0).round() / 1000.0,
        commit_pct: (commit_pct * 10.0).round() / 10.0,
        free_vram_mib: (free_vram_mib * 10.0).round() / 10.0,
        total_vram_mib: (total_vram_mib * 10.0).round() / 10.0,
        competing_gpu_apps,
        free_disk_gib: (free_disk_gib * 100.0).round() / 100.0,
        top_processes,
        model_valid,
        model_info,
        error_message: err_msg,
        budget: Some(budget),
        recovery_summary: Some(recovery_summary),
        selected_profile: Some(active_profile),
        integrity_state: crate::storage::load_model_cache().map(|c| crate::model_integrity::evaluate_startup_integrity(&c.path, Some(&c))),
    }
}
