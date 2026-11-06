use chrono::Utc;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::Read;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

pub const EXPECTED_QWEN3_30B_SHA256: &str =
    "a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Win32FileIdentity {
    pub volume_serial_number: u32,
    pub file_index: u64,
    pub file_size: u64,
    pub last_write_time_raw: u64,
}

#[cfg(windows)]
pub fn query_file_identity(path: &Path) -> Result<Win32FileIdentity, String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
        FILE_ATTRIBUTE_NORMAL, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
        OPEN_EXISTING,
    };

    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(std::iter::once(0)).collect();
    unsafe {
        let handle = CreateFileW(
            wide.as_ptr(),
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            std::ptr::null_mut(),
        );

        if handle == INVALID_HANDLE_VALUE {
            return Err(format!(
                "Failed to open handle for file identity query {:?}: Win32 error {}",
                path,
                std::io::Error::last_os_error()
            ));
        }

        let mut info: BY_HANDLE_FILE_INFORMATION = std::mem::zeroed();
        let ok = GetFileInformationByHandle(handle, &mut info);
        CloseHandle(handle);

        if ok == 0 {
            return Err(format!(
                "GetFileInformationByHandle failed for {:?}: Win32 error {}",
                path,
                std::io::Error::last_os_error()
            ));
        }

        let file_index = ((info.nFileIndexHigh as u64) << 32) | (info.nFileIndexLow as u64);
        let file_size = ((info.nFileSizeHigh as u64) << 32) | (info.nFileSizeLow as u64);
        let write_time = ((info.ftLastWriteTime.dwHighDateTime as u64) << 32)
            | (info.ftLastWriteTime.dwLowDateTime as u64);

        Ok(Win32FileIdentity {
            volume_serial_number: info.dwVolumeSerialNumber,
            file_index,
            file_size,
            last_write_time_raw: write_time,
        })
    }
}

#[cfg(not(windows))]
pub fn query_file_identity(path: &Path) -> Result<Win32FileIdentity, String> {
    let meta = std::fs::metadata(path).map_err(|e| e.to_string())?;
    Ok(Win32FileIdentity {
        volume_serial_number: 1,
        file_index: 1,
        file_size: meta.len(),
        last_write_time_raw: 1,
    })
}

#[derive(Debug)]
pub struct ModelFileLockGuard {
    #[cfg(windows)]
    handle: windows_sys::Win32::Foundation::HANDLE,
    pub path: String,
    pub identity: Win32FileIdentity,
}

unsafe impl Send for ModelFileLockGuard {}
unsafe impl Sync for ModelFileLockGuard {}

impl ModelFileLockGuard {
    #[cfg(windows)]
    pub fn acquire(path_str: &str) -> Result<Self, String> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
        use windows_sys::Win32::Storage::FileSystem::{
            CreateFileW, GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
            FILE_ATTRIBUTE_NORMAL, FILE_SHARE_READ, OPEN_EXISTING,
        };
        const GENERIC_READ: u32 = 0x80000000;

        let path = Path::new(path_str);
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain(std::iter::once(0)).collect();

        unsafe {
            // Open with GENERIC_READ and strictly FILE_SHARE_READ.
            // This grants read-only sharing (allowing llama-server to open/mmap),
            // but blocks ANY other process from writing, modifying, renaming, or deleting the file.
            let handle = CreateFileW(
                wide.as_ptr(),
                GENERIC_READ,
                FILE_SHARE_READ,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                std::ptr::null_mut(),
            );

            if handle == INVALID_HANDLE_VALUE {
                return Err(format!(
                    "Failed to acquire read-sharing lock on model file {:?}: Win32 error {}",
                    path_str,
                    std::io::Error::last_os_error()
                ));
            }

            let mut info: BY_HANDLE_FILE_INFORMATION = std::mem::zeroed();
            let ok = GetFileInformationByHandle(handle, &mut info);
            if ok == 0 {
                CloseHandle(handle);
                return Err(format!(
                    "Failed to query file information on locked model handle {:?}: Win32 error {}",
                    path_str,
                    std::io::Error::last_os_error()
                ));
            }

            let file_index = ((info.nFileIndexHigh as u64) << 32) | (info.nFileIndexLow as u64);
            let file_size = ((info.nFileSizeHigh as u64) << 32) | (info.nFileSizeLow as u64);
            let write_time = ((info.ftLastWriteTime.dwHighDateTime as u64) << 32)
                | (info.ftLastWriteTime.dwLowDateTime as u64);

            let identity = Win32FileIdentity {
                volume_serial_number: info.dwVolumeSerialNumber,
                file_index,
                file_size,
                last_write_time_raw: write_time,
            };

            Ok(Self {
                handle,
                path: path_str.to_string(),
                identity,
            })
        }
    }

    #[cfg(windows)]
    pub fn stream_hash<F>(&self, cancel_flag: &AtomicBool, mut progress_cb: F) -> Result<String, String>
    where
        F: FnMut(u64, u64, u32),
    {
        use std::io::{Read, Seek, SeekFrom};
        use std::os::windows::io::FromRawHandle;

        let total_bytes = self.identity.file_size;
        let mut file = std::mem::ManuallyDrop::new(unsafe {
            std::fs::File::from_raw_handle(self.handle as _)
        });

        if let Err(e) = (&mut *file).seek(SeekFrom::Start(0)) {
            return Err(format!("Failed to seek to start of locked model file: {}", e));
        }

        let mut hasher = Sha256::new();
        let mut buffer = vec![0u8; 8 * 1024 * 1024]; // 8 MiB streaming buffer
        let mut bytes_hashed: u64 = 0;
        let mut last_percent: u32 = 0;

        progress_cb(0, total_bytes, 0);

        loop {
            if cancel_flag.load(Ordering::Relaxed) {
                return Err("Cryptographic verification was canceled by user request".to_string());
            }

            let n = match (&mut *file).read(&mut buffer) {
                Ok(bytes) => bytes,
                Err(e) => return Err(format!("Error reading locked model file chunk: {}", e)),
            };

            if n == 0 {
                break;
            }

            hasher.update(&buffer[..n]);
            bytes_hashed += n as u64;

            let percent = if total_bytes > 0 {
                ((bytes_hashed as f64 / total_bytes as f64) * 100.0).floor() as u32
            } else {
                100
            };

            if percent != last_percent || bytes_hashed == total_bytes {
                last_percent = percent;
                progress_cb(bytes_hashed, total_bytes, percent);
            }
        }

        let result = hasher.finalize();
        let hex_digest = result
            .iter()
            .map(|b| format!("{:02x}", b))
            .collect::<String>();
        Ok(hex_digest)
    }

    #[cfg(windows)]
    pub fn query_current_identity(&self) -> Result<Win32FileIdentity, String> {
        use windows_sys::Win32::Storage::FileSystem::{
            GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
        };
        unsafe {
            let mut info: BY_HANDLE_FILE_INFORMATION = std::mem::zeroed();
            let ok = GetFileInformationByHandle(self.handle, &mut info);
            if ok == 0 {
                return Err(format!(
                    "GetFileInformationByHandle failed on locked handle: Win32 error {}",
                    std::io::Error::last_os_error()
                ));
            }

            let file_index = ((info.nFileIndexHigh as u64) << 32) | (info.nFileIndexLow as u64);
            let file_size = ((info.nFileSizeHigh as u64) << 32) | (info.nFileSizeLow as u64);
            let write_time = ((info.ftLastWriteTime.dwHighDateTime as u64) << 32)
                | (info.ftLastWriteTime.dwLowDateTime as u64);

            Ok(Win32FileIdentity {
                volume_serial_number: info.dwVolumeSerialNumber,
                file_index,
                file_size,
                last_write_time_raw: write_time,
            })
        }
    }

    #[cfg(windows)]
    pub fn raw_handle(&self) -> windows_sys::Win32::Foundation::HANDLE {
        self.handle
    }

    #[cfg(windows)]
    pub fn duplicate_to_process(
        &self,
        target_process: windows_sys::Win32::Foundation::HANDLE,
    ) -> Result<windows_sys::Win32::Foundation::HANDLE, String> {
        use windows_sys::Win32::Foundation::DuplicateHandle;
        use windows_sys::Win32::System::Threading::GetCurrentProcess;
        const DUPLICATE_SAME_ACCESS: u32 = 0x00000002;

        unsafe {
            let mut target_handle = std::ptr::null_mut();
            let ok = DuplicateHandle(
                GetCurrentProcess(),
                self.handle,
                target_process,
                &mut target_handle,
                0,
                0, // bInheritHandle = FALSE
                DUPLICATE_SAME_ACCESS,
            );
            if ok == 0 {
                return Err(format!(
                    "DuplicateHandle to child process failed: Win32 error {}",
                    std::io::Error::last_os_error()
                ));
            }
            Ok(target_handle)
        }
    }

    #[cfg(not(windows))]
    pub fn acquire(path_str: &str) -> Result<Self, String> {
        let identity = query_file_identity(Path::new(path_str))?;
        Ok(Self {
            path: path_str.to_string(),
            identity,
        })
    }

    #[cfg(not(windows))]
    pub fn stream_hash<F>(&self, cancel_flag: &AtomicBool, progress_cb: F) -> Result<String, String>
    where
        F: FnMut(u64, u64, u32),
    {
        hash_file_with_progress(Path::new(&self.path), cancel_flag, progress_cb)
    }

    #[cfg(not(windows))]
    pub fn query_current_identity(&self) -> Result<Win32FileIdentity, String> {
        query_file_identity(Path::new(&self.path))
    }
}

impl Drop for ModelFileLockGuard {
    fn drop(&mut self) {
        #[cfg(windows)]
        unsafe {
            use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
            if self.handle != INVALID_HANDLE_VALUE && self.handle != std::ptr::null_mut() {
                CloseHandle(self.handle);
            }
        }
    }
}

/// Validates that current file identity matches expected cryptographic identity.
/// Rejects file replacement, swap, in-place modification, or truncation.
pub fn verify_file_identity(
    current: &Win32FileIdentity,
    expected: &Win32FileIdentity,
) -> Result<(), String> {
    if current.volume_serial_number != expected.volume_serial_number {
        return Err(format!(
            "TOCTOU_PROTECTION_VIOLATION: Volume serial number mismatch (Current: 0x{:08X}, Expected: 0x{:08X}). File was moved across volumes.",
            current.volume_serial_number, expected.volume_serial_number
        ));
    }

    if current.file_index != expected.file_index {
        return Err(format!(
            "TOCTOU_PROTECTION_VIOLATION: NTFS File Index mismatch (Current: {}, Expected: {}). Model file was swapped or replaced on disk.",
            current.file_index, expected.file_index
        ));
    }

    if current.file_size != expected.file_size {
        return Err(format!(
            "TOCTOU_PROTECTION_VIOLATION: File size mismatch (Current: {} bytes, Expected: {} bytes). Model file was altered or truncated.",
            current.file_size, expected.file_size
        ));
    }

    if current.last_write_time_raw != expected.last_write_time_raw {
        return Err(format!(
            "TOCTOU_PROTECTION_VIOLATION: File modification timestamp mismatch (Current: {}, Expected: {}). Model file was modified in place.",
            current.last_write_time_raw, expected.last_write_time_raw
        ));
    }

    Ok(())
}

/// Obtains the restrictive model lock and verifies the file identity THROUGH the locked
/// handle (Claude takeover audit finding F1).
///
/// Checking identity by path and only then acquiring the lock leaves a window in which
/// the file can be swapped. Here the lock is taken first (reusing `existing` when it
/// guards the same path), so the identity read afterwards describes exactly the file
/// object that stays locked and is later duplicated into the child.
pub fn acquire_verified_lock(
    path_str: &str,
    existing: Option<ModelFileLockGuard>,
    expected: Option<&Win32FileIdentity>,
) -> Result<ModelFileLockGuard, String> {
    let guard = match existing {
        Some(g) if g.path == path_str => g,
        _ => ModelFileLockGuard::acquire(path_str)?,
    };

    if let Some(expected) = expected {
        let current = guard.query_current_identity()?;
        verify_file_identity(&current, expected)?;
    }

    Ok(guard)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", content = "details")]
pub enum ModelIntegrityState {
    PreviouslyVerified {
        cached_sha256: String,
        last_verified_utc: String,
        #[serde(default)]
        file_identity: Option<Win32FileIdentity>,
    },
    VerificationInProgress {
        bytes_hashed: u64,
        total_bytes: u64,
        percent: u32,
    },
    CryptographicallyVerified {
        sha256: String,
        verified_at_utc: String,
        #[serde(default)]
        file_identity: Option<Win32FileIdentity>,
    },
    VerificationFailed {
        reason: String,
        computed_sha256: Option<String>,
        expected_sha256: String,
    },
}

impl ModelIntegrityState {
    pub fn is_cryptographically_verified(&self) -> bool {
        matches!(self, ModelIntegrityState::CryptographicallyVerified { .. })
    }

    pub fn status_str(&self) -> &'static str {
        match self {
            ModelIntegrityState::PreviouslyVerified { .. } => "PREVIOUSLY_VERIFIED",
            ModelIntegrityState::VerificationInProgress { .. } => "VERIFICATION_IN_PROGRESS",
            ModelIntegrityState::CryptographicallyVerified { .. } => "CRYPTOGRAPHICALLY_VERIFIED",
            ModelIntegrityState::VerificationFailed { .. } => "VERIFICATION_FAILED",
        }
    }

    pub fn get_file_identity(&self) -> Option<Win32FileIdentity> {
        match self {
            ModelIntegrityState::PreviouslyVerified { file_identity, .. } => *file_identity,
            ModelIntegrityState::CryptographicallyVerified { file_identity, .. } => *file_identity,
            _ => None,
        }
    }
}

/// Evaluates fast-startup cache status without running synchronous SHA-256 over 18.6 GB
pub fn evaluate_startup_integrity(
    path_str: &str,
    cache_entry: Option<&crate::storage::ModelCacheEntry>,
) -> ModelIntegrityState {
    let p = Path::new(path_str);
    if !p.exists() || !p.is_file() {
        return ModelIntegrityState::VerificationFailed {
            reason: format!("Model file not found or inaccessible: {}", path_str),
            computed_sha256: None,
            expected_sha256: EXPECTED_QWEN3_30B_SHA256.to_string(),
        };
    }

    let meta = match std::fs::metadata(p) {
        Ok(m) => m,
        Err(e) => {
            return ModelIntegrityState::VerificationFailed {
                reason: format!("Failed to read model filesystem metadata: {}", e),
                computed_sha256: None,
                expected_sha256: EXPECTED_QWEN3_30B_SHA256.to_string(),
            };
        }
    };

    let file_size = meta.len();
    let mtime = meta
        .modified()
        .map(|t| {
            t.duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs()
        })
        .unwrap_or(0);

    if let Some(cached) = cache_entry {
        if cached.path == path_str
            && cached.file_size_bytes == file_size
            && cached.modified_timestamp_secs == mtime
            && !cached.sha256.trim().is_empty()
        {
            // If Win32 file identity was cached, verify it matches the current file
            let current_ident = query_file_identity(p).ok();
            if let (Some(curr), Some(vol), Some(idx), Some(wtime)) = (
                current_ident,
                cached.volume_serial_number,
                cached.file_index,
                cached.last_write_time_raw,
            ) {
                let cached_ident = Win32FileIdentity {
                    volume_serial_number: vol,
                    file_index: idx,
                    file_size: cached.file_size_bytes,
                    last_write_time_raw: wtime,
                };
                if let Err(e) = verify_file_identity(&curr, &cached_ident) {
                    let _ = crate::storage::clear_model_cache();
                    return ModelIntegrityState::VerificationFailed {
                        reason: format!("Startup cache invalidated: {}", e),
                        computed_sha256: None,
                        expected_sha256: EXPECTED_QWEN3_30B_SHA256.to_string(),
                    };
                }
            }

            return ModelIntegrityState::PreviouslyVerified {
                cached_sha256: cached.sha256.clone(),
                last_verified_utc: cached.last_validated_utc.clone(),
                file_identity: current_ident,
            };
        }
    }

    ModelIntegrityState::VerificationFailed {
        reason: "Model cache missing, outdated, or metadata modified. Cryptographic verification required.".to_string(),
        computed_sha256: None,
        expected_sha256: EXPECTED_QWEN3_30B_SHA256.to_string(),
    }
}

/// Computes SHA-256 over file bytes in 8 MiB chunks with progress callbacks and cancellation checks.
pub fn hash_file_with_progress<F>(
    path: &Path,
    cancel_flag: &AtomicBool,
    mut progress_cb: F,
) -> Result<String, String>
where
    F: FnMut(u64, u64, u32),
{
    let mut file =
        File::open(path).map_err(|e| format!("Failed to open file {:?}: {}", path, e))?;
    let total_bytes = file
        .metadata()
        .map_err(|e| format!("Failed to get metadata: {}", e))?
        .len();

    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 8 * 1024 * 1024]; // 8 MiB streaming buffer
    let mut bytes_hashed: u64 = 0;
    let mut last_percent: u32 = 0;

    progress_cb(0, total_bytes, 0);

    loop {
        if cancel_flag.load(Ordering::Relaxed) {
            return Err("Cryptographic verification was canceled by user request".to_string());
        }

        let n = file
            .read(&mut buffer)
            .map_err(|e| format!("Error streaming file chunk: {}", e))?;
        if n == 0 {
            break;
        }

        hasher.update(&buffer[..n]);
        bytes_hashed += n as u64;

        let percent = if total_bytes > 0 {
            ((bytes_hashed as f64 / total_bytes as f64) * 100.0).floor() as u32
        } else {
            100
        };

        if percent != last_percent || bytes_hashed == total_bytes {
            last_percent = percent;
            progress_cb(bytes_hashed, total_bytes, percent);
        }
    }

    let result = hasher.finalize();
    let hex_digest = result
        .iter()
        .map(|b| format!("{:02x}", b))
        .collect::<String>();
    Ok(hex_digest)
}

/// Performs full cryptographic model verification while holding a restrictive sharing lock throughout.
/// Returns both the verification result state and the acquired active lock guard.
pub fn verify_model_integrity_with_lock<F>(
    path_str: &str,
    expected_sha256: &str,
    cancel_flag: &AtomicBool,
    progress_cb: F,
) -> (ModelIntegrityState, Option<ModelFileLockGuard>)
where
    F: FnMut(u64, u64, u32),
{
    let p = Path::new(path_str);
    if !p.exists() || !p.is_file() {
        return (
            ModelIntegrityState::VerificationFailed {
                reason: format!("Model file not found at: {}", path_str),
                computed_sha256: None,
                expected_sha256: expected_sha256.to_string(),
            },
            None,
        );
    }

    // Step 1 & 2: Open file and acquire restrictive sharing lock (FILE_SHARE_READ, blocking write/delete)
    let lock_guard = match ModelFileLockGuard::acquire(path_str) {
        Ok(guard) => guard,
        Err(e) => {
            return (
                ModelIntegrityState::VerificationFailed {
                    reason: format!("Failed to acquire restrictive sharing lock for verification: {}", e),
                    computed_sha256: None,
                    expected_sha256: expected_sha256.to_string(),
                },
                None,
            );
        }
    };

    // Step 3: File identity already captured in lock_guard.identity
    let pre_ident = lock_guard.identity;

    // Validate GGUF format headers
    let header_info = match crate::admission::validate_gguf_file(path_str) {
        Ok(h) => h,
        Err(e) => {
            let _ = crate::storage::clear_model_cache();
            return (
                ModelIntegrityState::VerificationFailed {
                    reason: format!("GGUF header validation failed: {}", e),
                    computed_sha256: None,
                    expected_sha256: expected_sha256.to_string(),
                },
                None,
            );
        }
    };

    // Step 4: Stream SHA-256 calculation directly while locked
    let computed_hash = match lock_guard.stream_hash(cancel_flag, progress_cb) {
        Ok(h) => h,
        Err(e) => {
            return (
                ModelIntegrityState::VerificationFailed {
                    reason: e,
                    computed_sha256: None,
                    expected_sha256: expected_sha256.to_string(),
                },
                None,
            );
        }
    };

    // Step 5: Expected digest comparison
    if !computed_hash.eq_ignore_ascii_case(expected_sha256) {
        let _ = crate::storage::clear_model_cache();
        return (
            ModelIntegrityState::VerificationFailed {
                reason: format!(
                    "Cryptographic hash mismatch. Computed: {}, Expected: {}",
                    computed_hash, expected_sha256
                ),
                computed_sha256: Some(computed_hash),
                expected_sha256: expected_sha256.to_string(),
            },
            None,
        );
    }

    // Step 6: Post-verification identity check
    let post_ident = match lock_guard.query_current_identity() {
        Ok(id) => id,
        Err(e) => {
            let _ = crate::storage::clear_model_cache();
            return (
                ModelIntegrityState::VerificationFailed {
                    reason: format!("Post-verification identity query failed: {}", e),
                    computed_sha256: Some(computed_hash),
                    expected_sha256: expected_sha256.to_string(),
                },
                None,
            );
        }
    };

    if let Err(e) = verify_file_identity(&post_ident, &pre_ident) {
        let _ = crate::storage::clear_model_cache();
        return (
            ModelIntegrityState::VerificationFailed {
                reason: format!("Post-verification TOCTOU identity check failed: {}", e),
                computed_sha256: Some(computed_hash),
                expected_sha256: expected_sha256.to_string(),
            },
            None,
        );
    }

    let meta = std::fs::metadata(p).ok();
    let mtime = meta
        .and_then(|m| m.modified().ok())
        .map(|t| {
            t.duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs()
        })
        .unwrap_or(0);

    let cache_entry = crate::storage::ModelCacheEntry {
        path: path_str.to_string(),
        file_size_bytes: header_info.file_size_bytes,
        modified_timestamp_secs: mtime,
        sha256: computed_hash.clone(),
        magic: header_info.magic,
        version: header_info.version,
        tensor_count: header_info.tensor_count,
        kv_count: header_info.kv_count,
        last_validated_utc: Utc::now().to_rfc3339(),
        is_cryptographically_verified: true,
        volume_serial_number: Some(post_ident.volume_serial_number),
        file_index: Some(post_ident.file_index),
        last_write_time_raw: Some(post_ident.last_write_time_raw),
    };
    let _ = crate::storage::save_model_cache(&cache_entry);

    (
        ModelIntegrityState::CryptographicallyVerified {
            sha256: computed_hash,
            verified_at_utc: cache_entry.last_validated_utc,
            file_identity: Some(post_ident),
        },
        Some(lock_guard),
    )
}

/// Performs full cryptographic model verification, updating cache on success and invalidating on failure.
pub fn verify_model_integrity_synchronous<F>(
    path_str: &str,
    expected_sha256: &str,
    cancel_flag: &AtomicBool,
    progress_cb: F,
) -> ModelIntegrityState
where
    F: FnMut(u64, u64, u32),
{
    let (state, _guard) = verify_model_integrity_with_lock(
        path_str,
        expected_sha256,
        cancel_flag,
        progress_cb,
    );
    state
}
