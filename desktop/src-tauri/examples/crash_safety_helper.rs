use desktop_lib::job_object::JobObject;
use desktop_lib::model_integrity::ModelFileLockGuard;
use std::env;
use std::io::{BufRead, BufReader, Write};
use std::time::Duration;

#[cfg(windows)]
fn open_gguf_production_llama(path: &str) -> Result<windows_sys::Win32::Foundation::HANDLE, String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, FILE_ATTRIBUTE_NORMAL, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
    };
    const GENERIC_READ: u32 = 0x80000000;

    let wide_path: Vec<u16> = std::ffi::OsStr::new(path)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    unsafe {
        // EXACT LLAMA.CPP / CRT _wfopen("rb") Win32 FILE-OPEN SEMANTICS
        let handle = CreateFileW(
            wide_path.as_ptr(),
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            std::ptr::null_mut(),
        );

        if handle == windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE {
            return Err(format!(
                "Failed to open file: code {}",
                windows_sys::Win32::Foundation::GetLastError()
            ));
        }
        Ok(handle)
    }
}

#[cfg(windows)]
fn mmap_gguf_production_llama(
    handle: windows_sys::Win32::Foundation::HANDLE,
) -> Result<windows_sys::Win32::System::Memory::MEMORY_MAPPED_VIEW_ADDRESS, String> {
    use windows_sys::Win32::System::Memory::{
        CreateFileMappingW, MapViewOfFile, FILE_MAP_READ, PAGE_READONLY,
    };

    unsafe {
        // EXACT LLAMA.CPP llama_mmap::init() MEMORY MAPPING SEMANTICS
        let h_map = CreateFileMappingW(
            handle,
            std::ptr::null(),
            PAGE_READONLY,
            0,
            0,
            std::ptr::null(),
        );
        if h_map.is_null() {
            return Err(format!(
                "CreateFileMappingW failed: code {}",
                windows_sys::Win32::Foundation::GetLastError()
            ));
        }

        let view = MapViewOfFile(h_map, FILE_MAP_READ, 0, 0, 0);
        // In llama.cpp, file mapping handle is closed immediately after mapping view;
        // the view remains mapped until UnmapViewOfFile.
        windows_sys::Win32::Foundation::CloseHandle(h_map);

        if view.Value.is_null() {
            return Err(format!(
                "MapViewOfFile failed: code {}",
                windows_sys::Win32::Foundation::GetLastError()
            ));
        }
        Ok(view)
    }
}

#[cfg(windows)]
pub struct Win32NamedEvent {
    pub handle: windows_sys::Win32::Foundation::HANDLE,
}

#[cfg(windows)]
impl Win32NamedEvent {
    pub fn create_or_open(name: &str) -> Result<Self, String> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::System::Threading::CreateEventW;
        let wide: Vec<u16> = std::ffi::OsStr::new(name)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        unsafe {
            // bManualReset = 1 (TRUE), bInitialState = 0 (FALSE)
            let handle = CreateEventW(std::ptr::null(), 1, 0, wide.as_ptr());
            if handle.is_null() {
                return Err(format!(
                    "CreateEventW failed: {}",
                    windows_sys::Win32::Foundation::GetLastError()
                ));
            }
            Ok(Self { handle })
        }
    }

    pub fn set(&self) {
        unsafe {
            windows_sys::Win32::System::Threading::SetEvent(self.handle);
        }
    }

    pub fn wait(&self, timeout_ms: u32) -> bool {
        unsafe {
            windows_sys::Win32::System::Threading::WaitForSingleObject(self.handle, timeout_ms) == 0
        }
    }
}

#[cfg(windows)]
impl Drop for Win32NamedEvent {
    fn drop(&mut self) {
        unsafe {
            if !self.handle.is_null() {
                windows_sys::Win32::Foundation::CloseHandle(self.handle);
            }
        }
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!("Usage: crash_safety_helper <mode> <model_path>");
        std::process::exit(1);
    }

    let mode = &args[1];
    let model_path = &args[2];

    #[cfg(windows)]
    match mode.as_str() {
        // Mode 1: Production Child — opens file with FILE_SHARE_READ | FILE_SHARE_WRITE and mmaps it
        "child_prod" => {
            let handle = match open_gguf_production_llama(model_path) {
                Ok(h) => h,
                Err(e) => {
                    eprintln!("CHILD_OPEN_ERROR:{}", e);
                    std::process::exit(10);
                }
            };

            let view = match mmap_gguf_production_llama(handle) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("CHILD_MMAP_ERROR:{}", e);
                    unsafe {
                        windows_sys::Win32::Foundation::CloseHandle(handle);
                    }
                    std::process::exit(11);
                }
            };

            println!("CHILD_PROD_OPENED");
            let _ = std::io::stdout().flush();

            // Keep handle and view open until terminated by Job Object
            std::thread::sleep(Duration::from_secs(60));

            unsafe {
                use windows_sys::Win32::System::Memory::UnmapViewOfFile;
                UnmapViewOfFile(view);
                windows_sys::Win32::Foundation::CloseHandle(handle);
            }
        }

        // Mode 2: Parent with Option A — duplicates restrictive handle into child before resume
        "parent_prod_post_open" => {
            let lock_guard = match ModelFileLockGuard::acquire(model_path) {
                Ok(g) => g,
                Err(e) => {
                    eprintln!("PARENT_LOCK_ERROR:{}", e);
                    std::process::exit(20);
                }
            };

            let job = match JobObject::new() {
                Ok(j) => j,
                Err(e) => {
                    eprintln!("PARENT_JOB_ERROR:{}", e);
                    std::process::exit(21);
                }
            };

            let self_exe = env::current_exe().unwrap();
            let child_cmd = format!(
                "\"{}\" child_prod \"{}\"",
                self_exe.to_string_lossy(),
                model_path
            );

            // OPTION A: Duplicate restrictive lock handle into child process table before resuming!
            let child = match job.spawn_child_suspended_with_handle(
                &child_cmd,
                Some(lock_guard.raw_handle()),
            ) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("SPAWN_OPTION_A_ERROR:{}", e);
                    std::process::exit(22);
                }
            };

            let child_pid = child.pid;
            println!("PARENT_PROD_READY:{}", child_pid);
            let _ = std::io::stdout().flush();

            // Sleep waiting for test runner to kill this parent process
            std::thread::sleep(Duration::from_secs(60));
        }

        // Mode 3: Child with synchronization barriers — production semantics
        "child_prod_barrier" => {
            if args.len() < 4 {
                eprintln!("Usage: crash_safety_helper child_prod_barrier <model_path> <event_prefix>");
                std::process::exit(40);
            }
            let event_prefix = &args[3];
            let ev_ready = Win32NamedEvent::create_or_open(&format!("{}_READY", event_prefix))
                .expect("Create ev_ready");
            let ev_proceed = Win32NamedEvent::create_or_open(&format!("{}_PROCEED", event_prefix))
                .expect("Create ev_proceed");
            let ev_opened = Win32NamedEvent::create_or_open(&format!("{}_OPENED", event_prefix))
                .expect("Create ev_opened");
            let ev_mapped = Win32NamedEvent::create_or_open(&format!("{}_MAPPED", event_prefix))
                .expect("Create ev_mapped");
            let ev_exit = Win32NamedEvent::create_or_open(&format!("{}_EXIT", event_prefix))
                .expect("Create ev_exit");

            // Signal barrier: Child is alive and initialized
            ev_ready.set();

            // Wait for authorization to open file
            if !ev_proceed.wait(30000) {
                eprintln!("Child timed out waiting for PROCEED event");
                std::process::exit(41);
            }

            // Open model file using production llama.cpp semantics
            let handle = match open_gguf_production_llama(model_path) {
                Ok(h) => h,
                Err(e) => {
                    eprintln!("CHILD_OPEN_ERROR:{}", e);
                    std::process::exit(42);
                }
            };
            ev_opened.set();

            // Mmap model file using production llama.cpp semantics
            let view = match mmap_gguf_production_llama(handle) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("CHILD_MMAP_ERROR:{}", e);
                    unsafe {
                        windows_sys::Win32::Foundation::CloseHandle(handle);
                    }
                    std::process::exit(43);
                }
            };
            ev_mapped.set();

            // Wait until signaled to exit or killed by Job Object
            let _ = ev_exit.wait(60000);

            unsafe {
                use windows_sys::Win32::System::Memory::UnmapViewOfFile;
                UnmapViewOfFile(view);
                windows_sys::Win32::Foundation::CloseHandle(handle);
            }
        }

        // Mode 4: Parent with barrier and Option A handle duplication
        "parent_prod_barrier" => {
            if args.len() < 4 {
                eprintln!("Usage: crash_safety_helper parent_prod_barrier <model_path> <event_prefix>");
                std::process::exit(50);
            }
            let event_prefix = &args[3];

            let lock_guard = match ModelFileLockGuard::acquire(model_path) {
                Ok(g) => g,
                Err(e) => {
                    eprintln!("PARENT_LOCK_ERROR:{}", e);
                    std::process::exit(51);
                }
            };

            let job = match JobObject::new() {
                Ok(j) => j,
                Err(e) => {
                    eprintln!("PARENT_JOB_ERROR:{}", e);
                    std::process::exit(52);
                }
            };

            let self_exe = env::current_exe().unwrap();
            let child_cmd = format!(
                "\"{}\" child_prod_barrier \"{}\" \"{}\"",
                self_exe.to_string_lossy(),
                model_path,
                event_prefix
            );

            // OPTION A: Spawn suspended and duplicate lock handle before resume
            let child = match job.spawn_child_suspended_with_handle(
                &child_cmd,
                Some(lock_guard.raw_handle()),
            ) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("SPAWN_SUSPENDED_ERROR:{}", e);
                    std::process::exit(53);
                }
            };

            let child_pid = child.pid;
            println!("PARENT_PROD_BARRIER_READY:{}", child_pid);
            let _ = std::io::stdout().flush();

            // Sleep waiting for test runner to kill this parent process
            std::thread::sleep(Duration::from_secs(60));
        }

        // Mode 5: Active open loop under production semantics
        "child_prod_active_loop" => {
            if args.len() < 4 {
                eprintln!("Usage: crash_safety_helper child_prod_active_loop <model_path> <event_prefix>");
                std::process::exit(60);
            }
            let event_prefix = &args[3];
            let ev_ready = Win32NamedEvent::create_or_open(&format!("{}_READY", event_prefix))
                .expect("Create ev_ready");

            ev_ready.set();

            loop {
                if let Ok(handle) = open_gguf_production_llama(model_path) {
                    if let Ok(view) = mmap_gguf_production_llama(handle) {
                        std::thread::sleep(Duration::from_millis(2));
                        unsafe {
                            windows_sys::Win32::System::Memory::UnmapViewOfFile(view);
                        }
                    }
                    unsafe {
                        windows_sys::Win32::Foundation::CloseHandle(handle);
                    }
                }
                std::thread::sleep(Duration::from_millis(1));
            }
        }

        // Mode 6: Parent active open with Option A
        "parent_prod_active" => {
            if args.len() < 4 {
                eprintln!("Usage: crash_safety_helper parent_prod_active <model_path> <event_prefix>");
                std::process::exit(70);
            }
            let event_prefix = &args[3];

            let lock_guard = match ModelFileLockGuard::acquire(model_path) {
                Ok(g) => g,
                Err(e) => {
                    eprintln!("PARENT_LOCK_ERROR:{}", e);
                    std::process::exit(71);
                }
            };

            let job = match JobObject::new() {
                Ok(j) => j,
                Err(e) => {
                    eprintln!("PARENT_JOB_ERROR:{}", e);
                    std::process::exit(72);
                }
            };

            let self_exe = env::current_exe().unwrap();
            let child_cmd = format!(
                "\"{}\" child_prod_active_loop \"{}\" \"{}\"",
                self_exe.to_string_lossy(),
                model_path,
                event_prefix
            );

            let child = match job.spawn_child_suspended_with_handle(
                &child_cmd,
                Some(lock_guard.raw_handle()),
            ) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("SPAWN_SUSPENDED_ERROR:{}", e);
                    std::process::exit(73);
                }
            };

            let child_pid = child.pid;
            println!("PARENT_PROD_ACTIVE_READY:{}", child_pid);
            let _ = std::io::stdout().flush();

            std::thread::sleep(Duration::from_secs(60));
        }

        // Mode 7: Negative control — NO Option A handle duplication
        "parent_nodup_post_open" => {
            let lock_guard = match ModelFileLockGuard::acquire(model_path) {
                Ok(g) => g,
                Err(e) => {
                    eprintln!("PARENT_LOCK_ERROR:{}", e);
                    std::process::exit(80);
                }
            };

            let job = match JobObject::new() {
                Ok(j) => j,
                Err(e) => {
                    eprintln!("PARENT_JOB_ERROR:{}", e);
                    std::process::exit(81);
                }
            };

            let self_exe = env::current_exe().unwrap();
            let child_cmd = format!(
                "\"{}\" child_prod \"{}\"",
                self_exe.to_string_lossy(),
                model_path
            );

            // NEGATIVE CONTROL: spawn child suspended WITHOUT passing handle_to_duplicate!
            let child = match job.spawn_child_suspended_with_handle(&child_cmd, None) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("SPAWN_NO_DUP_ERROR:{}", e);
                    std::process::exit(82);
                }
            };

            let child_pid = child.pid;
            println!("PARENT_NODUP_READY:{}", child_pid);
            let _ = std::io::stdout().flush();

            // Explicitly drop lock_guard to simulate parent crashing and dropping its lock
            // while the child continues running with ONLY its own FILE_SHARE_READ | FILE_SHARE_WRITE handle
            drop(lock_guard);
            println!("PARENT_LOCK_DROPPED");
            let _ = std::io::stdout().flush();

            std::thread::sleep(Duration::from_secs(60));
        }

        // Backward compatibility modes
        "child" => {
            let handle = match open_gguf_production_llama(model_path) {
                Ok(h) => h,
                Err(e) => {
                    eprintln!("CHILD_OPEN_ERROR:{}", e);
                    std::process::exit(10);
                }
            };
            println!("CHILD_HANDLE_OPENED");
            let _ = std::io::stdout().flush();
            std::thread::sleep(Duration::from_secs(60));
            unsafe {
                windows_sys::Win32::Foundation::CloseHandle(handle);
            }
        }
        "parent_post_open" => {
            let _lock_guard = match ModelFileLockGuard::acquire(model_path) {
                Ok(g) => g,
                Err(e) => {
                    eprintln!("PARENT_LOCK_ERROR:{}", e);
                    std::process::exit(20);
                }
            };
            let job = match JobObject::new() {
                Ok(j) => j,
                Err(e) => {
                    eprintln!("PARENT_JOB_ERROR:{}", e);
                    std::process::exit(21);
                }
            };
            let self_exe = env::current_exe().unwrap();
            let mut child_proc = std::process::Command::new(&self_exe)
                .args(["child", model_path])
                .stdout(std::process::Stdio::piped())
                .spawn()
                .expect("Failed to spawn child");
            let child_pid = child_proc.id();
            let stdout = child_proc.stdout.take().expect("Failed stdout");
            unsafe {
                use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_ALL_ACCESS};
                let h_child = OpenProcess(PROCESS_ALL_ACCESS, 0, child_pid);
                if !h_child.is_null() {
                    let _ = job.assign_process(h_child);
                    windows_sys::Win32::Foundation::CloseHandle(h_child);
                }
            }
            let mut reader = BufReader::new(stdout);
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            if !line.contains("CHILD_HANDLE_OPENED") {
                eprintln!("Child failed to open file: {}", line);
                std::process::exit(22);
            }
            println!("PARENT_LOCKED_AND_CHILD_OPENED:{}", child_pid);
            let _ = std::io::stdout().flush();
            std::thread::sleep(Duration::from_secs(60));
        }
        "parent_pre_open" => {
            let lock_guard = match ModelFileLockGuard::acquire(model_path) {
                Ok(g) => g,
                Err(e) => {
                    eprintln!("PARENT_LOCK_ERROR:{}", e);
                    std::process::exit(30);
                }
            };
            let job = match JobObject::new() {
                Ok(j) => j,
                Err(e) => {
                    eprintln!("PARENT_JOB_ERROR:{}", e);
                    std::process::exit(31);
                }
            };
            let self_exe = env::current_exe().unwrap();
            let child_cmd = format!("\"{}\" child \"{}\"", self_exe.to_string_lossy(), model_path);
            let child = match job.spawn_child_suspended_with_handle(&child_cmd, Some(lock_guard.raw_handle())) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("SPAWN_SUSPENDED_ERROR:{}", e);
                    std::process::exit(32);
                }
            };
            let child_pid = child.pid;
            println!("PARENT_LOCKED_AND_CHILD_SUSPENDED:{}", child_pid);
            let _ = std::io::stdout().flush();
            std::thread::sleep(Duration::from_secs(60));
        }
        "child_barrier" => {
            if args.len() < 4 {
                eprintln!("Usage: crash_safety_helper child_barrier <model_path> <event_prefix>");
                std::process::exit(40);
            }
            let event_prefix = &args[3];
            let ev_ready = Win32NamedEvent::create_or_open(&format!("{}_READY", event_prefix))
                .expect("Create ev_ready");
            let ev_proceed = Win32NamedEvent::create_or_open(&format!("{}_PROCEED", event_prefix))
                .expect("Create ev_proceed");
            let ev_opened = Win32NamedEvent::create_or_open(&format!("{}_OPENED", event_prefix))
                .expect("Create ev_opened");
            let ev_exit = Win32NamedEvent::create_or_open(&format!("{}_EXIT", event_prefix))
                .expect("Create ev_exit");

            ev_ready.set();
            if !ev_proceed.wait(30000) {
                eprintln!("Child timed out waiting for PROCEED event");
                std::process::exit(41);
            }
            let handle = match open_gguf_production_llama(model_path) {
                Ok(h) => h,
                Err(e) => {
                    eprintln!("CHILD_OPEN_ERROR:{}", e);
                    std::process::exit(42);
                }
            };
            ev_opened.set();
            let _ = ev_exit.wait(60000);
            unsafe {
                windows_sys::Win32::Foundation::CloseHandle(handle);
            }
        }
        "parent_barrier" => {
            if args.len() < 4 {
                eprintln!("Usage: crash_safety_helper parent_barrier <model_path> <event_prefix>");
                std::process::exit(50);
            }
            let event_prefix = &args[3];
            let lock_guard = match ModelFileLockGuard::acquire(model_path) {
                Ok(g) => g,
                Err(e) => {
                    eprintln!("PARENT_LOCK_ERROR:{}", e);
                    std::process::exit(51);
                }
            };
            let job = match JobObject::new() {
                Ok(j) => j,
                Err(e) => {
                    eprintln!("PARENT_JOB_ERROR:{}", e);
                    std::process::exit(52);
                }
            };
            let self_exe = env::current_exe().unwrap();
            let child_cmd = format!(
                "\"{}\" child_barrier \"{}\" \"{}\"",
                self_exe.to_string_lossy(),
                model_path,
                event_prefix
            );
            let child = match job.spawn_child_suspended_with_handle(&child_cmd, Some(lock_guard.raw_handle())) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("SPAWN_SUSPENDED_ERROR:{}", e);
                    std::process::exit(53);
                }
            };
            let child_pid = child.pid;
            println!("PARENT_BARRIER_READY:{}", child_pid);
            let _ = std::io::stdout().flush();
            std::thread::sleep(Duration::from_secs(60));
        }
        "child_active_open_loop" => {
            if args.len() < 4 {
                eprintln!("Usage: crash_safety_helper child_active_open_loop <model_path> <event_prefix>");
                std::process::exit(60);
            }
            let event_prefix = &args[3];
            let ev_ready = Win32NamedEvent::create_or_open(&format!("{}_READY", event_prefix))
                .expect("Create ev_ready");
            ev_ready.set();
            loop {
                if let Ok(handle) = open_gguf_production_llama(model_path) {
                    std::thread::sleep(Duration::from_millis(2));
                    unsafe {
                        windows_sys::Win32::Foundation::CloseHandle(handle);
                    }
                }
                std::thread::sleep(Duration::from_millis(1));
            }
        }
        "parent_active_open" => {
            if args.len() < 4 {
                eprintln!("Usage: crash_safety_helper parent_active_open <model_path> <event_prefix>");
                std::process::exit(70);
            }
            let event_prefix = &args[3];
            let lock_guard = match ModelFileLockGuard::acquire(model_path) {
                Ok(g) => g,
                Err(e) => {
                    eprintln!("PARENT_LOCK_ERROR:{}", e);
                    std::process::exit(71);
                }
            };
            let job = match JobObject::new() {
                Ok(j) => j,
                Err(e) => {
                    eprintln!("PARENT_JOB_ERROR:{}", e);
                    std::process::exit(72);
                }
            };
            let self_exe = env::current_exe().unwrap();
            let child_cmd = format!(
                "\"{}\" child_active_open_loop \"{}\" \"{}\"",
                self_exe.to_string_lossy(),
                model_path,
                event_prefix
            );
            let child = match job.spawn_child_suspended_with_handle(&child_cmd, Some(lock_guard.raw_handle())) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("SPAWN_SUSPENDED_ERROR:{}", e);
                    std::process::exit(73);
                }
            };
            let child_pid = child.pid;
            println!("PARENT_ACTIVE_READY:{}", child_pid);
            let _ = std::io::stdout().flush();
            std::thread::sleep(Duration::from_secs(60));
        }
        _ => {
            eprintln!("Unknown mode: {}", mode);
            std::process::exit(99);
        }
    }
}
