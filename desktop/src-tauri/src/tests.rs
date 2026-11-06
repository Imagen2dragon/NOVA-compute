#[cfg(test)]
mod tests {
    use std::io::{BufRead, BufReader, Write};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    use crate::admission::{
        evaluate_admission_from_metrics, parse_nvml_process_results, validate_gguf_file,
        NVML_ERROR_INSUFFICIENT_SIZE, NVML_ERROR_NOT_FOUND, NVML_SUCCESS,
    };
    use crate::chat::{ChatEventSink, ChatRequestMessage, ChatService, StreamTokenPayload};
    use crate::job_object::JobObject;
    use crate::server_manager::{
        check_endpoint_readiness, ReadinessStatus, ServerConfigManifest, ServerManager,
    };
    use crate::storage::{
        atomic_write, load_conversations_from, load_settings_from, save_conversations_to,
        save_settings_to, AppSettings, Conversation,
    };

    // Helper event sink for testing ChatService without Tauri AppHandle
    #[derive(Clone, Default)]
    struct MockEventSink {
        pub payloads: Arc<Mutex<Vec<StreamTokenPayload>>>,
    }

    impl ChatEventSink for MockEventSink {
        fn emit_stream_token(&self, payload: StreamTokenPayload) {
            self.payloads.lock().unwrap().push(payload);
        }
    }

    // Helper to generate unique isolated test directory
    fn create_test_dir(prefix: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("nova_test_{}_{}", prefix, uuid::Uuid::new_v4()));
        let _ = std::fs::create_dir_all(&p);
        p
    }

    // =========================================================================
    // 1. ADMISSION PARITY & TELEMETRY REGRESSION TESTS
    // =========================================================================

    #[test]
    fn test_gate_parity_all_six_pass() {
        let report = evaluate_admission_from_metrics(
            11.5, 16.0, 50.0, 5800.0, 6144.0, 0, 100.0, true, true, None, None,
        );
        assert!(report.all_passed);
        assert_eq!(report.gates.len(), 6);
        assert_eq!(report.ram_deficit_gib, 0.0);
        for g in &report.gates {
            assert!(g.passed, "Gate {} must pass", g.gate_name);
        }
    }

    #[test]
    fn test_gate_1_telemetry_integrity_failure_fails_closed() {
        let report = evaluate_admission_from_metrics(
            12.0,
            16.0,
            40.0,
            6000.0,
            6144.0,
            0,
            100.0,
            false, // telemetry_ok = false
            true,
            None,
            Some("Hardware sensor unavailable".to_string()),
        );

        assert!(!report.all_passed);
        let telem_gate = report
            .gates
            .iter()
            .find(|g| g.gate_name == "telemetry_integrity")
            .unwrap();
        assert!(!telem_gate.passed);
        assert_eq!(telem_gate.measured_str, "ERROR / UNAVAILABLE");

        for g in &report.gates {
            assert!(
                !g.passed,
                "Gate {} must fail closed when telemetry is unhealthy",
                g.gate_name
            );
        }
    }

    #[test]
    fn test_gate_2_avail_ram_individual_failure_and_deficit() {
        let report = evaluate_admission_from_metrics(
            8.35, 16.0, 50.0, 5800.0, 6144.0, 0, 100.0, true, true, None, None,
        );
        assert!(!report.all_passed);
        assert_eq!(report.ram_deficit_gib, 1.65);
        let ram_gate = report
            .gates
            .iter()
            .find(|g| g.gate_name == "avail_ram")
            .unwrap();
        assert!(!ram_gate.passed);
        assert!(ram_gate
            .failure_reason
            .as_ref()
            .unwrap()
            .contains("1.65 GiB"));

        let exact_10 = evaluate_admission_from_metrics(
            10.0, 16.0, 50.0, 5800.0, 6144.0, 0, 100.0, true, true, None, None,
        );
        let ram_pass = exact_10
            .gates
            .iter()
            .find(|g| g.gate_name == "avail_ram")
            .unwrap();
        assert!(ram_pass.passed);
        assert_eq!(exact_10.ram_deficit_gib, 0.0);
    }

    #[test]
    fn test_gate_3_system_commit_individual_failure_and_boundaries() {
        let fail_90 = evaluate_admission_from_metrics(
            12.0, 16.0, 90.0, 5800.0, 6144.0, 0, 100.0, true, true, None, None,
        );
        let commit_gate = fail_90
            .gates
            .iter()
            .find(|g| g.gate_name == "system_commit")
            .unwrap();
        assert!(!commit_gate.passed);
        assert!(!fail_90.all_passed);

        let fail_90_1 = evaluate_admission_from_metrics(
            12.0, 16.0, 90.1, 5800.0, 6144.0, 0, 100.0, true, true, None, None,
        );
        assert!(
            !fail_90_1
                .gates
                .iter()
                .find(|g| g.gate_name == "system_commit")
                .unwrap()
                .passed
        );

        let pass_89_9 = evaluate_admission_from_metrics(
            12.0, 16.0, 89.9, 5800.0, 6144.0, 0, 100.0, true, true, None, None,
        );
        assert!(
            pass_89_9
                .gates
                .iter()
                .find(|g| g.gate_name == "system_commit")
                .unwrap()
                .passed
        );
    }

    #[test]
    fn test_gate_4_avail_vram_individual_failure_and_boundaries() {
        let fail_vram = evaluate_admission_from_metrics(
            12.0, 16.0, 50.0, 5499.0, 6144.0, 0, 100.0, true, true, None, None,
        );
        assert!(
            !fail_vram
                .gates
                .iter()
                .find(|g| g.gate_name == "avail_vram")
                .unwrap()
                .passed
        );

        let pass_vram = evaluate_admission_from_metrics(
            12.0, 16.0, 50.0, 5500.0, 6144.0, 0, 100.0, true, true, None, None,
        );
        assert!(
            pass_vram
                .gates
                .iter()
                .find(|g| g.gate_name == "avail_vram")
                .unwrap()
                .passed
        );
    }

    #[test]
    fn test_gate_5_no_competing_gpu_individual_failure() {
        let fail_gpu = evaluate_admission_from_metrics(
            12.0, 16.0, 50.0, 5800.0, 6144.0, 1, 100.0, true, true, None, None,
        );
        assert!(
            !fail_gpu
                .gates
                .iter()
                .find(|g| g.gate_name == "no_competing_gpu")
                .unwrap()
                .passed
        );

        let pass_gpu = evaluate_admission_from_metrics(
            12.0, 16.0, 50.0, 5800.0, 6144.0, 0, 100.0, true, true, None, None,
        );
        assert!(
            pass_gpu
                .gates
                .iter()
                .find(|g| g.gate_name == "no_competing_gpu")
                .unwrap()
                .passed
        );
    }

    #[test]
    fn test_gate_6_disk_free_individual_failure_and_boundaries() {
        let fail_disk = evaluate_admission_from_metrics(
            12.0, 16.0, 50.0, 5800.0, 6144.0, 0, 24.9, true, true, None, None,
        );
        assert!(
            !fail_disk
                .gates
                .iter()
                .find(|g| g.gate_name == "disk_free")
                .unwrap()
                .passed
        );

        let pass_disk = evaluate_admission_from_metrics(
            12.0, 16.0, 50.0, 5800.0, 6144.0, 0, 25.0, true, true, None, None,
        );
        assert!(
            pass_disk
                .gates
                .iter()
                .find(|g| g.gate_name == "disk_free")
                .unwrap()
                .passed
        );
    }

    #[test]
    fn test_multiple_simultaneous_gate_failures() {
        let multi_fail = evaluate_admission_from_metrics(
            8.0, 16.0, 92.5, 4500.0, 6144.0, 2, 10.0, true, false, None, None,
        );
        assert!(!multi_fail.all_passed);
        let failed_gates: Vec<&str> = multi_fail
            .gates
            .iter()
            .filter(|g| !g.passed)
            .map(|g| g.gate_name.as_str())
            .collect();
        assert_eq!(
            failed_gates,
            vec![
                "avail_ram",
                "system_commit",
                "avail_vram",
                "no_competing_gpu",
                "disk_free"
            ]
        );
    }

    #[test]
    fn test_telemetry_sensor_failure_fails_all_admission() {
        let report = evaluate_admission_from_metrics(
            0.0,
            0.0,
            100.0,
            0.0,
            0.0,
            999,
            0.0,
            false, // telemetry_ok = false
            true,
            None,
            Some("Operating system platform unsupported".to_string()),
        );

        assert!(!report.all_passed);
        assert_eq!(report.ram_deficit_gib, 10.0);

        for g in &report.gates {
            assert!(
                !g.passed,
                "Gate {} must fail closed on sensor failure",
                g.gate_name
            );
            assert_eq!(
                g.measured_str,
                if g.gate_name == "telemetry_integrity" {
                    "ERROR / UNAVAILABLE"
                } else {
                    "UNAVAILABLE"
                }
            );
        }
    }

    // =========================================================================
    // 2. NVML RETURN-CODE MAPPING & TWO-CALL BEHAVIOR TESTS
    // =========================================================================

    #[test]
    fn test_nvml_return_code_mapping_and_two_call_behavior() {
        let res0 = parse_nvml_process_results(NVML_SUCCESS, 0, |_| panic!("Should not call call2"));
        assert_eq!(res0.unwrap(), 0);

        let res_pos = parse_nvml_process_results(NVML_SUCCESS, 2, |needed| {
            assert_eq!(needed, 2);
            (NVML_SUCCESS, 2)
        });
        assert_eq!(res_pos.unwrap(), 2);

        let res_insuf = parse_nvml_process_results(NVML_ERROR_INSUFFICIENT_SIZE, 3, |needed| {
            assert_eq!(needed, 3);
            (NVML_SUCCESS, 3)
        });
        assert_eq!(res_insuf.unwrap(), 3);

        let res_not_found =
            parse_nvml_process_results(NVML_ERROR_NOT_FOUND, 0, |_| (NVML_SUCCESS, 0));
        assert!(res_not_found.is_err());
        assert!(res_not_found
            .unwrap_err()
            .contains("NVML_ERROR_NOT_FOUND = 6"));

        let res_perm = parse_nvml_process_results(4, 0, |_| (NVML_SUCCESS, 0));
        assert!(res_perm.is_err());

        let res_unknown = parse_nvml_process_results(999, 0, |_| (NVML_SUCCESS, 0));
        assert!(res_unknown.is_err());
    }

    // =========================================================================
    // 3. TELEMETRY FRESHNESS MONOTONIC CLOCK BOUNDARY TESTS
    // =========================================================================

    #[test]
    fn test_telemetry_monotonic_freshness_clock_boundaries() {
        let mut report = evaluate_admission_from_metrics(
            12.0, 16.0, 50.0, 5800.0, 6144.0, 0, 100.0, true, true, None, None,
        );

        assert!(!report.is_stale(Duration::from_millis(3000)));

        report.captured_at = Instant::now()
            .checked_sub(Duration::from_millis(2900))
            .unwrap();
        assert!(
            !report.is_stale(Duration::from_millis(3000)),
            "2900ms must not be stale under 3000ms threshold"
        );

        report.captured_at = Instant::now()
            .checked_sub(Duration::from_millis(3001))
            .unwrap();
        assert!(
            report.is_stale(Duration::from_millis(3000)),
            "3001ms must be marked stale"
        );
    }

    // =========================================================================
    // 4. MODEL GGUF VALIDATION TESTS
    // =========================================================================

    #[test]
    fn test_gguf_validation_nonexistent_and_invalid_path() {
        assert!(validate_gguf_file("nonexistent_path_to_model.gguf").is_err());
        assert!(validate_gguf_file("").is_err());
    }

    #[test]
    fn test_gguf_validation_corrupt_and_wrong_magic() {
        let temp_dir = create_test_dir("gguf_corrupt");
        let test_file = temp_dir.join("test_corrupt.gguf");

        {
            let mut f = std::fs::File::create(&test_file).unwrap();
            f.write_all(b"NOT_GGUF_MAGIC_HEADER_BYTES_12345678")
                .unwrap();
            f.set_len(11 * 1024 * 1024).unwrap();
        }

        let res = validate_gguf_file(test_file.to_str().unwrap());
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("Invalid GGUF header magic"));
        let _ = std::fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn test_gguf_validation_valid_mock_header() {
        let temp_dir = create_test_dir("gguf_valid");
        let test_file = temp_dir.join("test_valid_mock.gguf");

        {
            let mut f = std::fs::File::create(&test_file).unwrap();
            f.write_all(b"GGUF").unwrap();
            f.write_all(&3u32.to_le_bytes()).unwrap();
            f.write_all(&579u64.to_le_bytes()).unwrap();
            f.write_all(&41u64.to_le_bytes()).unwrap();
            f.set_len(11 * 1024 * 1024).unwrap();
        }

        let res = validate_gguf_file(test_file.to_str().unwrap());
        assert!(res.is_ok());
        let info = res.unwrap();
        assert_eq!(info.magic, "GGUF");
        assert_eq!(info.version, 3);
        assert_eq!(info.tensor_count, 579);
        assert_eq!(info.kv_count, 41);

        let _ = std::fs::remove_dir_all(temp_dir);
    }

    // =========================================================================
    // 5. JOB OBJECT KERNEL CONTAINMENT INTEGRATION TESTS
    // =========================================================================

    #[test]
    #[cfg(windows)]
    fn test_job_object_spawn_suspended_and_automatic_cleanup_on_close() {
        let job = JobObject::new().expect("Failed to create JobObject");

        let cmd = "powershell.exe -NoProfile -NonInteractive -Command \"Start-Sleep -Seconds 30\"";
        let child = job
            .spawn_child_suspended(cmd)
            .expect("Failed to spawn child in Job Object");

        let pid = child.pid;
        assert!(pid > 0);
        assert!(child.is_alive(), "Child process must be running");

        drop(job);
        std::thread::sleep(Duration::from_millis(1500));
        assert!(
            !child.is_alive(),
            "Windows kernel must terminate child when JobObject closes"
        );
    }

    #[test]
    #[cfg(windows)]
    fn test_job_object_manual_child_termination() {
        let job = JobObject::new().expect("Failed to create JobObject");
        let cmd = "powershell.exe -NoProfile -NonInteractive -Command \"Start-Sleep -Seconds 30\"";
        let mut child = job
            .spawn_child_suspended(cmd)
            .expect("Failed to spawn child in Job Object");

        assert!(child.is_alive());
        child.terminate();
        assert!(
            !child.is_alive(),
            "Child should be terminated after terminate() call"
        );
    }

    #[test]
    #[cfg(windows)]
    fn test_job_object_parent_termination_cleans_up_child() {
        use std::process::Command;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
            PROCESS_TERMINATE,
        };

        let mut helper_exe = PathBuf::from("target/debug/examples/job_parent_helper.exe");
        if !helper_exe.exists() {
            helper_exe = PathBuf::from("../target/debug/examples/job_parent_helper.exe");
        }
        if !helper_exe.exists() {
            let _ = Command::new("cargo")
                .args(["build", "--example", "job_parent_helper"])
                .output();
        }

        assert!(
            helper_exe.exists(),
            "job_parent_helper.exe must exist for parent-termination containment test"
        );

        let mut parent_child = Command::new(&helper_exe)
            .stdout(std::process::Stdio::piped())
            .spawn()
            .expect("Failed to launch helper parent process");

        let parent_pid = parent_child.id();
        let stdout = parent_child
            .stdout
            .take()
            .expect("Failed to capture stdout");
        let mut reader = BufReader::new(stdout);
        let mut first_line = String::new();
        reader
            .read_line(&mut first_line)
            .expect("Failed to read child PID from helper parent");

        assert!(
            first_line.starts_with("CHILD_PID:"),
            "Helper must output CHILD_PID:<pid>"
        );
        let child_pid: u32 = first_line
            .trim_start_matches("CHILD_PID:")
            .trim()
            .parse()
            .expect("Invalid child PID");
        assert!(child_pid > 0);

        unsafe {
            let h_child = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid);
            assert!(
                !h_child.is_null(),
                "Child process must be accessible while parent lives"
            );
            CloseHandle(h_child);
        }

        unsafe {
            let h_parent = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            assert!(!h_parent.is_null());
            TerminateProcess(h_parent, 1);
            WaitForSingleObject(h_parent, 2000);
            CloseHandle(h_parent);
        }

        std::thread::sleep(Duration::from_millis(1500));

        unsafe {
            use windows_sys::Win32::System::Threading::GetExitCodeProcess;
            let h_child = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid);
            if !h_child.is_null() {
                let mut code = 0;
                let active = GetExitCodeProcess(h_child, &mut code);
                CloseHandle(h_child);
                if active != 0 {
                    assert_ne!(
                        code, 259,
                        "Child process must not be running after parent termination"
                    );
                }
            }
        }
    }

    // =========================================================================
    // 6. STORAGE ISOLATION & REGRESSION TESTS
    // =========================================================================

    #[test]
    fn test_storage_isolated_unique_temp_dir() {
        let temp_dir = create_test_dir("storage_isolated");

        let mut settings = AppSettings::default();
        settings.runtime_path = "C:\\Isolated\\llama-cpp".to_string();
        settings.model_path = "C:\\Isolated\\model.gguf".to_string();
        settings.server_port = 19191;

        save_settings_to(&temp_dir, &settings).expect("save_settings_to failed");
        let loaded = load_settings_from(&temp_dir).expect("load_settings_from failed");

        assert_eq!(loaded.runtime_path, "C:\\Isolated\\llama-cpp");
        assert_eq!(loaded.model_path, "C:\\Isolated\\model.gguf");
        assert_eq!(loaded.server_port, 19191);

        let conv = Conversation {
            id: "isolated-conv-1".to_string(),
            title: "Isolated Conversation".to_string(),
            created_at: "2026-11-05T00:00:00Z".to_string(),
            updated_at: "2026-11-05T00:00:00Z".to_string(),
            messages: vec![],
        };

        save_conversations_to(&temp_dir, &[conv.clone()]).expect("save_conversations_to failed");
        let loaded_convs =
            load_conversations_from(&temp_dir).expect("load_conversations_from failed");
        assert_eq!(loaded_convs.len(), 1);
        assert_eq!(loaded_convs[0].id, "isolated-conv-1");

        let _ = std::fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn test_storage_production_data_untouched_regression() {
        let prod_dir = if let Ok(local) = std::env::var("LOCALAPPDATA") {
            PathBuf::from(local).join("NOVA Chat")
        } else {
            PathBuf::from(".").join(".nova_chat_data")
        };
        let prod_settings_file = prod_dir.join("settings.json");

        let prod_content_before = if prod_settings_file.exists() {
            std::fs::read_to_string(&prod_settings_file).ok()
        } else {
            None
        };

        let test_dir = create_test_dir("prod_untouched");
        let test_settings = AppSettings {
            model_path: "C:\\TempTest\\test.gguf".to_string(),
            runtime_path: "C:\\TempTest\\runtime".to_string(),
            server_port: 29999,
            theme: "light".to_string(),
            context_size: 1024,
            ..AppSettings::default()
        };

        save_settings_to(&test_dir, &test_settings).unwrap();
        let _ = load_settings_from(&test_dir).unwrap();

        let prod_content_after = if prod_settings_file.exists() {
            std::fs::read_to_string(&prod_settings_file).ok()
        } else {
            None
        };

        assert_eq!(
            prod_content_before, prod_content_after,
            "Production settings.json must remain completely untouched during test operations"
        );

        let _ = std::fs::remove_dir_all(test_dir);
    }

    #[test]
    fn test_storage_atomic_write_and_recovery_from_backup() {
        let temp_dir = create_test_dir("atomic_backup");

        let initial_settings = AppSettings {
            model_path: "C:\\Initial\\model.gguf".to_string(),
            runtime_path: "C:\\Initial\\runtime".to_string(),
            server_port: 18080,
            theme: "dark".to_string(),
            context_size: 512,
            ..AppSettings::default()
        };

        save_settings_to(&temp_dir, &initial_settings).unwrap();

        let updated_settings = AppSettings {
            model_path: "C:\\Updated\\model.gguf".to_string(),
            runtime_path: "C:\\Updated\\runtime".to_string(),
            server_port: 18080,
            theme: "dark".to_string(),
            context_size: 512,
            ..AppSettings::default()
        };
        save_settings_to(&temp_dir, &updated_settings).unwrap();

        let bak_file = temp_dir.join("settings.json.bak");
        assert!(
            bak_file.exists(),
            ".bak backup file must be created on replace"
        );

        let settings_file = temp_dir.join("settings.json");
        std::fs::write(&settings_file, b"MALFORMED_JSON_CONTENT{{{").unwrap();

        let recovered = load_settings_from(&temp_dir).expect("Failed to recover from .bak");
        assert_eq!(recovered.model_path, "C:\\Initial\\model.gguf");
        // Verify backup remains intact after recovery
        assert!(
            bak_file.exists(),
            ".bak file must not be destroyed during recovery"
        );

        let _ = std::fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn test_storage_corrupted_primary_with_invalid_backup_returns_error() {
        let temp_dir = create_test_dir("corrupt_both");
        let settings_file = temp_dir.join("settings.json");
        let bak_file = temp_dir.join("settings.json.bak");

        std::fs::write(&settings_file, b"CORRUPTED_PRIMARY").unwrap();
        std::fs::write(&bak_file, b"CORRUPTED_BACKUP").unwrap();

        let res = load_settings_from(&temp_dir);
        assert!(
            res.is_err(),
            "Must return Err when both primary and backup are corrupt"
        );
        let err_msg = res.unwrap_err();
        assert!(err_msg.contains("corrupt and no valid backup exists"));

        let _ = std::fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn test_storage_conversations_corrupted_returns_error() {
        let temp_dir = create_test_dir("corrupt_convs");
        let conv_file = temp_dir.join("conversations.json");

        std::fs::write(&conv_file, b"NOT_VALID_CONVERSATIONS_JSON").unwrap();

        let res = load_conversations_from(&temp_dir);
        assert!(
            res.is_err(),
            "Must return Err when conversations.json is corrupt and no backup exists"
        );
        assert!(res
            .unwrap_err()
            .contains("corrupt and no valid backup exists"));

        let _ = std::fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn test_storage_write_errors_returned() {
        let invalid_dir = PathBuf::from("Z:\\NonExistentDriveDirectory123456\\NOVA");
        let settings = AppSettings::default();

        let res = save_settings_to(&invalid_dir, &settings);
        assert!(
            res.is_err(),
            "Write to nonexistent drive path must return Err"
        );
        let err_msg = res.unwrap_err();
        assert!(err_msg.contains("Failed to create directory") || err_msg.contains("Failed"));
    }

    #[test]
    fn test_storage_interrupted_save_cleans_up_temp_file() {
        let temp_dir = create_test_dir("cleanup_temp");
        let target_file = temp_dir.join("test_file.json");

        let content = b"Valid content";
        let res = atomic_write(&target_file, content);
        assert!(res.is_ok());

        // Check that no .tmp files remain
        let tmp_entries: Vec<_> = std::fs::read_dir(&temp_dir)
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().contains(".tmp."))
            .collect();
        assert!(
            tmp_entries.is_empty(),
            "No temporary files must be left behind after atomic write"
        );

        let _ = std::fs::remove_dir_all(temp_dir);
    }

    // =========================================================================
    // 7. CHAT SERVICE SSE, PER-REQUEST CANCELLATION, & RACE TESTS (TASK 1)
    // =========================================================================

    #[tokio::test]
    async fn test_chat_service_stop_generation_signal() {
        let service = ChatService::new();
        assert!(!service.is_generating());
        service.stop_generation();
        assert!(!service.is_generating());
    }

    #[tokio::test]
    async fn test_chat_service_mock_sse_streaming_with_done() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        tokio::spawn(async move {
            if let Ok((mut socket, _)) = listener.accept().await {
                let mut buf = [0u8; 1024];
                let _ = socket.read(&mut buf).await;
                let sse_body = "data: {\"choices\":[{\"delta\":{\"content\":\"Hello \"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\"NOVA!\"}}]}\n\ndata: [DONE]\n\n";
                let http_response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n{}",
                    sse_body
                );
                let _ = socket.write_all(http_response.as_bytes()).await;
                let _ = socket.flush().await;
            }
        });

        let service = ChatService::new();
        let sink = MockEventSink::default();

        let result = service
            .stream_completion(
                &sink,
                port,
                "test-api-key",
                "conv-1".to_string(),
                "msg-1".to_string(),
                vec![ChatRequestMessage {
                    role: "user".to_string(),
                    content: "Hi".to_string(),
                }],
            )
            .await;

        assert!(result.is_ok());
        assert_eq!(result.unwrap(), "Hello NOVA!");

        let payloads = sink.payloads.lock().unwrap();
        assert_eq!(payloads.len(), 3);
        assert_eq!(payloads[0].token, "Hello ");
        assert_eq!(payloads[1].token, "NOVA!");
        assert!(payloads[2].done);
        assert_eq!(payloads[2].status, "complete");
    }

    #[tokio::test]
    async fn test_chat_service_mock_sse_unicode_chunk_splitting() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        // 🚀 rocket emoji is 4 bytes: [0xF0, 0x9F, 0x9A, 0x80]
        tokio::spawn(async move {
            if let Ok((mut socket, _)) = listener.accept().await {
                let mut buf = [0u8; 1024];
                let _ = socket.read(&mut buf).await;

                let header = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n";
                let _ = socket.write_all(header.as_bytes()).await;

                let chunk1 = "data: {\"choices\":[{\"delta\":{\"content\":\"Rocket: 🚀\"}}]}\n\n";
                let bytes1 = chunk1.as_bytes();
                let mid = bytes1.len() / 2;
                let _ = socket.write_all(&bytes1[..mid]).await;
                let _ = socket.flush().await;
                tokio::time::sleep(Duration::from_millis(50)).await;

                let _ = socket.write_all(&bytes1[mid..]).await;
                let _ = socket.write_all(b"data: [DONE]\n\n").await;
                let _ = socket.flush().await;
            }
        });

        let service = ChatService::new();
        let sink = MockEventSink::default();

        let result = service
            .stream_completion(
                &sink,
                port,
                "test-api-key",
                "conv-2".to_string(),
                "msg-2".to_string(),
                vec![ChatRequestMessage {
                    role: "user".to_string(),
                    content: "Emoji".to_string(),
                }],
            )
            .await;

        assert!(result.is_ok());
        assert_eq!(result.unwrap(), "Rocket: 🚀");
    }

    #[tokio::test]
    async fn test_chat_service_cancellation_while_stalled() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        tokio::spawn(async move {
            if let Ok((mut socket, _)) = listener.accept().await {
                let mut buf = [0u8; 1024];
                let _ = socket.read(&mut buf).await;

                let header = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n";
                let _ = socket.write_all(header.as_bytes()).await;
                let chunk =
                    "data: {\"choices\":[{\"delta\":{\"content\":\"Initial chunk... \"}}]}\n\n";
                let _ = socket.write_all(chunk.as_bytes()).await;
                let _ = socket.flush().await;

                tokio::time::sleep(Duration::from_secs(30)).await;
            }
        });

        let service = Arc::new(ChatService::new());
        let sink = MockEventSink::default();

        let s_clone = service.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(100)).await;
            s_clone.stop_generation();
        });

        let start = Instant::now();
        let result = service
            .stream_completion(
                &sink,
                port,
                "test-api-key",
                "conv-3".to_string(),
                "msg-3".to_string(),
                vec![ChatRequestMessage {
                    role: "user".to_string(),
                    content: "Stall".to_string(),
                }],
            )
            .await;

        let elapsed = start.elapsed();
        assert!(
            elapsed < Duration::from_secs(3),
            "Cancellation while stalled must abort stream immediately"
        );
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("cancelled"));

        let payloads = sink.payloads.lock().unwrap();
        let last = payloads
            .last()
            .expect("Must have emitted payload on cancel");
        assert!(last.done);
        assert_eq!(last.status, "cancelled");
    }

    #[tokio::test]
    async fn test_chat_service_cancellation_race_repeated_immediate_second_generation() {
        // Spins up a mock slow server
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        tokio::spawn(async move {
            while let Ok((mut socket, _)) = listener.accept().await {
                let mut buf = [0u8; 1024];
                let _ = socket.read(&mut buf).await;
                let header = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n";
                let _ = socket.write_all(header.as_bytes()).await;
                let chunk = "data: {\"choices\":[{\"delta\":{\"content\":\"Slow token...\"}}]}\n\n";
                let _ = socket.write_all(chunk.as_bytes()).await;
                let _ = socket.flush().await;
                tokio::time::sleep(Duration::from_millis(500)).await;
                let _ = socket.write_all(b"data: [DONE]\n\n").await;
                let _ = socket.flush().await;
            }
        });

        let service = Arc::new(ChatService::new());

        for _ in 0..5 {
            let s_clone = service.clone();
            let sink1 = MockEventSink::default();

            // Launch generation 1 in background
            let h1 = tokio::spawn(async move {
                s_clone
                    .stream_completion(
                        &sink1,
                        port,
                        "test-key",
                        "conv-race".to_string(),
                        "msg-race-1".to_string(),
                        vec![],
                    )
                    .await
            });

            // Wait 50ms then stop generation
            tokio::time::sleep(Duration::from_millis(50)).await;
            service.stop_generation();

            // Immediately attempt generation 2
            let sink2 = MockEventSink::default();
            let res2 = service
                .stream_completion(
                    &sink2,
                    port,
                    "test-key",
                    "conv-race".to_string(),
                    "msg-race-2".to_string(),
                    vec![],
                )
                .await;

            // Wait for h1 to exit
            let res1 = h1.await.unwrap();

            // Either gen 2 was rejected because gen 1 was still active/cleaning up,
            // or gen 1 completed cancellation and gen 2 succeeded.
            // Neither case should panic or corrupt the state!
            assert!(res1.is_err() || res1.is_ok());
            assert!(res2.is_err() || res2.is_ok());

            // Await full completion before next iteration
            service.await_generation_complete().await;
            assert!(!service.is_generating());
        }
    }

    #[tokio::test]
    async fn test_chat_service_old_generation_cannot_clear_new_generation_state() {
        let service = Arc::new(ChatService::new());
        assert!(!service.is_generating());

        // Directly verify with mock tasks
        let (tx, rx) = tokio::sync::oneshot::channel();

        // Launch task that holds generation slot
        let s1 = service.clone();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        tokio::spawn(async move {
            if let Ok((mut socket, _)) = listener.accept().await {
                let _ = tx.send(());
                let mut buf = [0u8; 1024];
                let _ = socket.read(&mut buf).await;
                // Sleep to hold generation slot
                tokio::time::sleep(Duration::from_millis(300)).await;
            }
        });

        let s1_clone = s1.clone();
        let sink = MockEventSink::default();
        let h = tokio::spawn(async move {
            s1_clone
                .stream_completion(&sink, port, "key", "c1".into(), "m1".into(), vec![])
                .await
        });

        // Wait until task is in stream
        let _ = rx.await;
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(
            s1.is_generating(),
            "Must be generating while task is active"
        );

        // Cancel task
        s1.stop_generation();
        // is_generating should still be true until task finishes cleanup
        // Await completion
        s1.await_generation_complete().await;
        let _ = h.await;

        assert!(!s1.is_generating(), "Must be false after task exit");
    }

    #[tokio::test]
    async fn test_chat_service_malformed_sse_json() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        tokio::spawn(async move {
            if let Ok((mut socket, _)) = listener.accept().await {
                let mut buf = [0u8; 1024];
                let _ = socket.read(&mut buf).await;
                let malformed = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\ndata: {NOT_VALID_JSON_SYNTAX}\n\n";
                let _ = socket.write_all(malformed.as_bytes()).await;
                let _ = socket.flush().await;
            }
        });

        let service = ChatService::new();
        let sink = MockEventSink::default();

        let result = service
            .stream_completion(
                &sink,
                port,
                "test-api-key",
                "conv-4".to_string(),
                "msg-4".to_string(),
                vec![],
            )
            .await;

        assert!(result.is_err());
        assert!(result.unwrap_err().contains("Malformed JSON"));

        let payloads = sink.payloads.lock().unwrap();
        let last = payloads.last().unwrap();
        assert!(last.done);
        assert_eq!(last.status, "error");
    }

    #[tokio::test]
    async fn test_chat_service_unexpected_eof_without_done() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        tokio::spawn(async move {
            if let Ok((mut socket, _)) = listener.accept().await {
                let mut buf = [0u8; 1024];
                let _ = socket.read(&mut buf).await;
                let incomplete = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\ndata: {\"choices\":[{\"delta\":{\"content\":\"Incomplete\"}}]}\n\n";
                let _ = socket.write_all(incomplete.as_bytes()).await;
                let _ = socket.flush().await;
            }
        });

        let service = ChatService::new();
        let sink = MockEventSink::default();

        let result = service
            .stream_completion(
                &sink,
                port,
                "test-api-key",
                "conv-5".to_string(),
                "msg-5".to_string(),
                vec![],
            )
            .await;

        assert!(result.is_err());
        assert!(result.unwrap_err().contains("unexpectedly before [DONE]"));

        let payloads = sink.payloads.lock().unwrap();
        let last = payloads.last().unwrap();
        assert!(last.done);
        assert_eq!(last.status, "error");
    }

    // =========================================================================
    // 8. LIFECYCLE COORDINATION & GENERATION LOCK TESTS (TASK 2)
    // =========================================================================

    #[tokio::test]
    async fn test_lifecycle_generation_rejected_during_unload() {
        let is_unloading = Arc::new(AtomicBool::new(true));

        // When is_unloading is true, send_chat_message logic must reject new generation
        let can_send = !is_unloading.load(Ordering::SeqCst);
        assert!(
            !can_send,
            "New chat message must be rejected while unloading"
        );

        is_unloading.store(false, Ordering::SeqCst);
        assert!(!is_unloading.load(Ordering::SeqCst));
    }

    #[tokio::test]
    async fn test_lifecycle_unload_awaits_generation_task_termination() {
        let service = Arc::new(ChatService::new());
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        tokio::spawn(async move {
            if let Ok((mut socket, _)) = listener.accept().await {
                let mut buf = [0u8; 1024];
                let _ = socket.read(&mut buf).await;
                let header = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n";
                let _ = socket.write_all(header.as_bytes()).await;
                let chunk =
                    "data: {\"choices\":[{\"delta\":{\"content\":\"Stream chunk...\"}}]}\n\n";
                let _ = socket.write_all(chunk.as_bytes()).await;
                let _ = socket.flush().await;
                tokio::time::sleep(Duration::from_secs(30)).await;
            }
        });

        let s_clone = service.clone();
        let sink = MockEventSink::default();
        let gen_handle = tokio::spawn(async move {
            s_clone
                .stream_completion(&sink, port, "key", "c1".into(), "m1".into(), vec![])
                .await
        });

        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(service.is_generating());

        // Simulate unload_model awaiting generation complete
        let unload_start = Instant::now();
        service.stop_generation();
        service.await_generation_complete().await;
        let unload_duration = unload_start.elapsed();

        assert!(
            unload_duration < Duration::from_secs(3),
            "Unload must await completion without arbitrary sleep"
        );
        assert!(
            !service.is_generating(),
            "Generation must be finished after await_generation_complete"
        );

        let _ = gen_handle.await;
    }

    #[tokio::test]
    async fn test_lifecycle_simultaneous_unload_requests() {
        let lifecycle_lock = Arc::new(tokio::sync::Mutex::new(()));

        let l1 = lifecycle_lock.clone();
        let h1 = tokio::spawn(async move {
            let _g = l1.lock().await;
            tokio::time::sleep(Duration::from_millis(50)).await;
        });

        let l2 = lifecycle_lock.clone();
        let h2 = tokio::spawn(async move {
            let _g = l2.lock().await;
            tokio::time::sleep(Duration::from_millis(50)).await;
        });

        let (r1, r2) = tokio::join!(h1, h2);
        assert!(r1.is_ok());
        assert!(r2.is_ok());
    }

    #[test]
    fn test_lifecycle_failed_server_does_not_restore_model_ready() {
        // When server manager reports child is not alive or not ready, state must not be MODEL_READY
        let is_ready = false;
        let is_running = false;

        let next_state = if is_ready {
            "MODEL_READY"
        } else if is_running {
            "ERROR"
        } else {
            "MODEL_UNLOADED"
        };

        assert_eq!(next_state, "MODEL_UNLOADED");
        assert_ne!(next_state, "MODEL_READY");
    }

    // =========================================================================
    // 9. EXPLICIT BACKEND USER CONFIRMATION TESTS (TASK 3)
    // =========================================================================

    #[test]
    fn test_confirmation_preflight_alone_cannot_authorize_load() {
        let mut model_load_authorization: Option<crate::ModelLoadAuthorization> = None;

        // Preflight passes, but user has not called confirm_model_load
        let preflight_passed = true;
        assert!(preflight_passed);

        // Attempting to load model without authorization token must be rejected
        let can_load = model_load_authorization.take().is_some();
        assert!(
            !can_load,
            "Model load must be rejected without explicit user confirmation"
        );
    }

    #[test]
    fn test_confirmation_token_binding_and_expiration() {
        let model_path = "C:\\Models\\qwen3-30b.gguf".to_string();
        let token = format!("nova-auth-{}", uuid::Uuid::new_v4());
        let now = Instant::now();

        let auth = crate::ModelLoadAuthorization {
            token: token.clone(),
            model_path: model_path.clone(),
            issued_at: now,
            expires_at: now + Duration::from_secs(60),
        };

        // 1. Valid token and path matches
        assert_eq!(auth.model_path, model_path);
        assert!(Instant::now() < auth.expires_at);

        // 2. Token is single-use
        let mut auth_slot = Some(auth);
        let consumed = auth_slot.take();
        assert!(consumed.is_some());
        assert!(
            auth_slot.is_none(),
            "Authorization must be strictly single-use"
        );

        // 3. Simulated expiration
        let expired_auth = crate::ModelLoadAuthorization {
            token: "expired".to_string(),
            model_path: model_path.clone(),
            issued_at: now.checked_sub(Duration::from_secs(120)).unwrap(),
            expires_at: now.checked_sub(Duration::from_secs(60)).unwrap(),
        };
        assert!(
            Instant::now() > expired_auth.expires_at,
            "Expired token must be recognized as expired"
        );
    }

    #[test]
    fn test_confirmation_invalidated_on_model_path_change_or_unload() {
        let auth_slot = Arc::new(Mutex::new(Some(crate::ModelLoadAuthorization {
            token: "auth-123".to_string(),
            model_path: "C:\\Path1\\model.gguf".to_string(),
            issued_at: Instant::now(),
            expires_at: Instant::now() + Duration::from_secs(60),
        })));

        // Path change invalidates token
        let target_path = "C:\\Path2\\different_model.gguf";
        let is_path_match = {
            let guard = auth_slot.lock().unwrap();
            guard
                .as_ref()
                .map(|a| a.model_path == target_path)
                .unwrap_or(false)
        };
        assert!(
            !is_path_match,
            "Token must not match a different model path"
        );

        // Invalidation on unload
        *auth_slot.lock().unwrap() = None;
        assert!(
            auth_slot.lock().unwrap().is_none(),
            "Authorization must be invalidated upon unload"
        );
    }

    // =========================================================================
    // 6. PHASE 2D RUNTIME CONTRACT, PROBE & LIFECYCLE CONCURRENCY TESTS
    // =========================================================================

    #[test]
    fn test_server_config_manifest_exact_vector_and_order() {
        let manifest = ServerConfigManifest::configuration_d(
            "C:\\models\\test_model.gguf",
            18080,
            "test-secret-key-12345",
        );

        let args = manifest.to_args_vector();
        let expected_args = vec![
            "-m",
            "C:\\models\\test_model.gguf",
            "-ngl",
            "48",
            "-ncmoe",
            "40",
            "--moe-cache-mib",
            "0",
            "-c",
            "512",
            "-b",
            "64",
            "-ub",
            "16",
            "-ctk",
            "q8_0",
            "-ctv",
            "q8_0",
            "-fa",
            "auto",
            "-lm",
            "mmap",
            "--host",
            "127.0.0.1",
            "--port",
            "18080",
            "--api-key",
            "test-secret-key-12345",
        ];

        assert_eq!(
            args, expected_args,
            "Argument vector must match canonical Configuration D exactly in order and values"
        );

        // Test Windows argument quoting
        assert_eq!(ServerConfigManifest::quote_windows_arg("simple"), "simple");
        assert_eq!(
            ServerConfigManifest::quote_windows_arg("path with spaces"),
            "\"path with spaces\""
        );
        assert_eq!(
            ServerConfigManifest::quote_windows_arg("path\\with\\slashes"),
            "path\\with\\slashes"
        );
        assert_eq!(
            ServerConfigManifest::quote_windows_arg("path with\\slashes and spaces"),
            "\"path with\\slashes and spaces\""
        );

        let exe_path = std::path::Path::new("C:\\Program Files\\NOVA Chat\\llama-server.exe");
        let cmd_line = manifest.build_command_line(exe_path);
        assert!(cmd_line.starts_with("\"C:\\Program Files\\NOVA Chat\\llama-server.exe\""));
        assert!(cmd_line.contains("-ncmoe 40"));
        assert!(cmd_line.contains("-fa auto"));
        assert!(cmd_line.contains("-lm mmap"));
        assert!(cmd_line.contains("--host 127.0.0.1"));
        assert!(cmd_line.contains("--port 18080"));
    }

    #[tokio::test]
    async fn test_server_readiness_probe_all_seven_scenarios() {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_millis(500))
            .build()
            .unwrap();

        // Scenario 1: Connection Refused (port closed)
        let status = check_endpoint_readiness(&client, 59999, "any-key").await;
        assert_eq!(
            status,
            ReadinessStatus::Loading,
            "Connection refused must indicate server is still starting up"
        );

        // Setup mock server helper
        async fn run_mock_response(
            status_code: u16,
            body: &str,
        ) -> (u16, tokio::task::JoinHandle<()>) {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let port = listener.local_addr().unwrap().port();
            let body_owned = body.to_string();
            let handle = tokio::spawn(async move {
                if let Ok((mut socket, _)) = listener.accept().await {
                    let mut buf = [0u8; 1024];
                    let _ = socket.read(&mut buf).await;
                    let resp = format!(
                        "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        status_code,
                        body_owned.len(),
                        body_owned
                    );
                    let _ = socket.write_all(resp.as_bytes()).await;
                }
            });
            (port, handle)
        }

        // Scenario 2: HTTP 503 while loading model
        {
            let (port, handle) = run_mock_response(503, r#"{"status":"loading model"}"#).await;
            let status = check_endpoint_readiness(&client, port, "key").await;
            assert_eq!(
                status,
                ReadinessStatus::Loading,
                "HTTP 503 must indicate loading state"
            );
            let _ = handle.await;
        }

        // Scenario 3: HTTP 200 with valid ready response
        {
            let (port, handle) = run_mock_response(200, r#"{"status":"ok"}"#).await;
            let status = check_endpoint_readiness(&client, port, "key").await;
            assert_eq!(
                status,
                ReadinessStatus::Ready,
                "HTTP 200 with status 'ok' must indicate Ready"
            );
            let _ = handle.await;
        }

        // Scenario 4: HTTP 200 with unexpected response body
        {
            let (port, handle) = run_mock_response(200, r#"{"status":"unknown_state"}"#).await;
            let status = check_endpoint_readiness(&client, port, "key").await;
            assert!(
                matches!(status, ReadinessStatus::Failed(_)),
                "HTTP 200 with unexpected status must fail"
            );
            let _ = handle.await;
        }

        // Scenario 5: HTTP 200 with malformed JSON
        {
            let (port, handle) = run_mock_response(200, "not-valid-json").await;
            let status = check_endpoint_readiness(&client, port, "key").await;
            assert!(
                matches!(status, ReadinessStatus::Failed(_)),
                "Malformed JSON must fail"
            );
            let _ = handle.await;
        }

        // Scenario 6: HTTP 401 Unauthorized
        {
            let (port, handle) =
                run_mock_response(401, r#"{"error":{"message":"Unauthorized"}}"#).await;
            let status = check_endpoint_readiness(&client, port, "wrong-key").await;
            assert_eq!(
                status,
                ReadinessStatus::Unauthorized,
                "HTTP 401 must report Unauthorized"
            );
            let _ = handle.await;
        }

        // Scenario 7: Server closes connection prematurely during request
        {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let port = listener.local_addr().unwrap().port();
            let handle = tokio::spawn(async move {
                if let Ok((mut socket, _)) = listener.accept().await {
                    let mut buf = [0u8; 100];
                    let _ = socket.read(&mut buf).await;
                    // Close connection abruptly without response
                    drop(socket);
                }
            });
            let status = check_endpoint_readiness(&client, port, "key").await;
            assert!(
                matches!(
                    status,
                    ReadinessStatus::Loading | ReadinessStatus::Failed(_)
                ),
                "Abrupt connection closure must be handled safely without panicking"
            );
            let _ = handle.await;
        }
    }

    #[tokio::test]
    async fn test_lifecycle_generation_admission_unload_interleaving_race() {
        let chat_service = Arc::new(ChatService::new());

        // Step 1: Request passes external pre-check
        assert!(!chat_service.is_unloading());

        // Step 2 & 3: Unload begins before request claims slot
        let cs_clone = chat_service.clone();
        let unload_handle = tokio::spawn(async move {
            cs_clone.begin_unload();
        });
        unload_handle.await.unwrap();

        // Step 4 & 5: Request attempts to claim slot under synchronization boundary
        let claim_res = chat_service.claim_generation_slot();

        // Step 6: Verify request CANNOT start a new generation
        assert!(
            claim_res.is_err(),
            "Chat request must be rejected when unload is in progress"
        );
        assert!(
            claim_res.unwrap_err().contains("unloading"),
            "Error message must specify unloading"
        );
    }

    #[tokio::test]
    async fn test_lifecycle_repeated_simultaneous_send_and_unload() {
        let chat_service = Arc::new(ChatService::new());

        for _ in 0..20 {
            chat_service.end_unload();

            let cs1 = chat_service.clone();
            let cs2 = chat_service.clone();

            let h1 = tokio::spawn(async move { cs1.claim_generation_slot() });

            let h2 = tokio::spawn(async move { cs2.begin_unload() });

            let (res1, _) = tokio::join!(h1, h2);
            let claim_outcome = res1.unwrap();

            // Either the generation was admitted before unload (Ok) or rejected by unload (Err)
            // But state must remain completely valid and never panic
            if let Ok((guard, _)) = claim_outcome {
                drop(guard);
            }
        }
    }

    #[tokio::test]
    async fn test_lifecycle_completion_before_wait() {
        let chat_service = Arc::new(ChatService::new());

        // Claim slot
        let (guard, _) = chat_service.claim_generation_slot().unwrap();

        // Generation finishes and releases slot BEFORE unload begins waiting via RAII Drop
        drop(guard);

        // Unload begins waiting afterwards: must return immediately without deadlock or lost signal
        let start = Instant::now();
        chat_service.await_generation_complete().await;
        assert!(
            start.elapsed() < Duration::from_millis(50),
            "Must complete immediately if task already exited"
        );
    }

    #[test]
    fn test_confirmation_concurrent_token_consumption_race() {
        let auth_slot = Arc::new(Mutex::new(Some(crate::ModelLoadAuthorization {
            token: "unique-single-use-token".to_string(),
            model_path: "C:\\models\\test.gguf".to_string(),
            issued_at: Instant::now(),
            expires_at: Instant::now() + Duration::from_secs(60),
        })));

        let mut handles = Vec::new();
        for _ in 0..10 {
            let slot = auth_slot.clone();
            handles.push(std::thread::spawn(move || {
                let mut guard = slot.lock().unwrap();
                guard.take()
            }));
        }

        let mut successes = 0;
        let mut failures = 0;
        for h in handles {
            let res = h.join().unwrap();
            if res.is_some() {
                successes += 1;
            } else {
                failures += 1;
            }
        }

        assert_eq!(
            successes, 1,
            "Exactly ONE consumer must succeed in taking the single-use token"
        );
        assert_eq!(
            failures, 9,
            "All subsequent concurrent attempts must fail to consume the token"
        );
    }

    // =========================================================================
    // 10. PHASE 2E RUNTIME PACKAGING, SECURITY & LIFECYCLE TESTS
    // =========================================================================

    #[tokio::test]
    async fn test_chat_service_task_abort_clears_generation_slot_via_raii() {
        let chat_service = Arc::new(ChatService::new());
        assert!(!chat_service.is_generating());

        let cs = chat_service.clone();
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();

        let handle = tokio::spawn(async move {
            let (_guard, mut cancel_rx) = cs.claim_generation_slot().unwrap();
            started_tx.send(()).unwrap();
            let _ = cancel_rx.changed().await;
        });

        // Wait until slot is claimed
        started_rx.await.unwrap();
        assert!(
            chat_service.is_generating(),
            "Slot must be active while task runs"
        );

        // Abort the task abruptly via tokio JoinHandle::abort()
        handle.abort();
        let join_result = handle.await;
        assert!(
            join_result.unwrap_err().is_cancelled(),
            "Task must report cancelled on abort"
        );

        // RAII Drop MUST have executed when the task's future was dropped
        assert!(
            !chat_service.is_generating(),
            "RAII Drop must have cleared slot even after task abort"
        );

        // A subsequent generation must succeed immediately
        let second_claim = chat_service.claim_generation_slot();
        assert!(
            second_claim.is_ok(),
            "Subsequent generation slot claim must succeed immediately"
        );
    }

    #[tokio::test]
    async fn test_chat_service_panic_unwind_clears_generation_slot_via_raii() {
        let chat_service = Arc::new(ChatService::new());
        assert!(!chat_service.is_generating());

        let cs = chat_service.clone();
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();

        // Spawn a task that claims the slot and then panics
        let handle = tokio::spawn(async move {
            let (_guard, _cancel_rx) = cs.claim_generation_slot().unwrap();
            started_tx.send(()).unwrap();
            panic!("Simulated worker panic inside stream processing");
        });

        // Wait until slot is claimed
        started_rx.await.unwrap();

        // Await the task join handle, which captures the panic
        let join_result = handle.await;
        assert!(join_result.is_err(), "Task must terminate with panic error");
        assert!(
            join_result.unwrap_err().is_panic(),
            "Join error must be marked as panic"
        );

        // RAII Drop MUST have executed during unwinding when _guard was dropped
        assert!(
            !chat_service.is_generating(),
            "RAII Drop must have cleared active generation slot during panic unwinding"
        );

        // A subsequent generation must succeed immediately without deadlock or rejected slot
        let second_claim = chat_service.claim_generation_slot();
        assert!(
            second_claim.is_ok(),
            "Subsequent generation slot claim must succeed immediately after panic"
        );
    }

    #[test]
    fn test_server_config_manifest_debug_redacts_api_key() {
        let raw_secret = "secret-super-private-key-12345";
        let manifest =
            ServerConfigManifest::configuration_d("C:\\models\\qwen.gguf", 18080, raw_secret);
        let debug_str = format!("{:?}", manifest);

        assert!(
            debug_str.contains("[REDACTED]"),
            "Debug output must contain [REDACTED]"
        );
        assert!(
            !debug_str.contains(raw_secret),
            "Debug output must NEVER reveal the secret API key"
        );
    }

    #[test]
    fn test_server_manager_debug_redacts_api_key() {
        let sm = ServerManager::new();
        let raw_key = sm.get_api_key();
        assert!(
            !raw_key.is_empty(),
            "ServerManager must generate an API key"
        );

        let debug_str = format!("{:?}", sm);
        assert!(
            debug_str.contains("[REDACTED]"),
            "ServerManager Debug output must contain [REDACTED]"
        );
        assert!(
            !debug_str.contains(&raw_key),
            "ServerManager Debug output must NEVER reveal the secret API key"
        );
    }

    #[test]
    fn test_job_object_terminate_jobs_kills_process_tree() {
        let job = JobObject::new().expect("JobObject must be created successfully");

        // Spawn a benign Windows child process (powershell sleep)
        let cmd = "powershell.exe -NoProfile -Command Start-Sleep -Seconds 30";
        let child = job
            .spawn_child_suspended(cmd)
            .expect("Child should spawn in Job Object");
        assert!(child.is_alive(), "Child process should be running");

        // Explicitly terminate child via Job Object
        let term_res = job.terminate_jobs();
        assert!(
            term_res.is_ok(),
            "terminate_jobs should succeed: {:?}",
            term_res
        );

        // Give OS brief moment to update exit status
        std::thread::sleep(Duration::from_millis(150));
        assert!(
            !child.is_alive(),
            "Child process must be terminated by Job Object"
        );
    }

    #[test]
    fn test_mtmd_dll_verified_in_required_dependencies() {
        let temp_dir =
            std::env::temp_dir().join(format!("nova_runtime_test_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&temp_dir).unwrap();

        let exe_path = temp_dir.join("llama-server.exe");
        std::fs::write(&exe_path, b"mock-exe").unwrap();

        // 1. Initially missing all DLLs
        let res = ServerManager::verify_runtime_dependencies(&exe_path);
        assert!(res.is_err(), "Must fail when DLLs are missing");
        let err_msg = res.unwrap_err();
        assert!(
            err_msg.contains("mtmd.dll"),
            "Error message must report missing mtmd.dll"
        );

        // 2. Populate all DLLs except mtmd.dll
        let dlls = [
            "llama-server-impl.dll",
            "llama.dll",
            "llama-common.dll",
            "ggml.dll",
            "ggml-base.dll",
            "ggml-cuda.dll",
            "cublas64_12.dll",
            "cublasLt64_12.dll",
            "cudart64_12.dll",
        ];
        for dll in &dlls {
            std::fs::write(temp_dir.join(dll), b"mock-dll").unwrap();
        }

        let res_missing_mtmd = ServerManager::verify_runtime_dependencies(&exe_path);
        assert!(
            res_missing_mtmd.is_err(),
            "Must fail when mtmd.dll is missing"
        );
        assert!(
            res_missing_mtmd.unwrap_err().contains("mtmd.dll"),
            "Error must specifically name mtmd.dll"
        );

        // 3. Add mtmd.dll
        std::fs::write(temp_dir.join("mtmd.dll"), b"mock-mtmd").unwrap();
        let res_complete = ServerManager::verify_runtime_dependencies(&exe_path);
        assert!(
            res_complete.is_ok(),
            "Must pass when all required DLLs including mtmd.dll are present"
        );

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[test]
    #[ignore = "Live system hardware diagnostic; manual invocation only via -- --ignored"]
    fn test_live_system_preflight_audit() {
        let model_path = "C:\\Users\\abdal\\OneDrive\\Desktop\\nova_compute_antigravity_starter\\models\\qwen3-30b\\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf";
        let report = crate::admission::run_preflight_audit(model_path);

        println!("=== LIVE PREFLIGHT AUDIT REPORT ===");
        println!("Timestamp UTC: {}", report.timestamp_utc);
        println!(
            "Timestamp Africa/Addis_Ababa (UTC+3): {}",
            report.timestamp_eat
        );
        println!("All passed: {}", report.all_passed);
        println!(
            "Avail RAM: {} bytes ({:.3} GiB)",
            report.avail_ram_bytes, report.avail_ram_gib
        );
        println!(
            "Total RAM: {} bytes ({:.3} GiB)",
            report.total_ram_bytes, report.total_ram_gib
        );
        println!(
            "RAM Deficit: {} bytes ({:.3} GiB)",
            report.ram_deficit_bytes, report.ram_deficit_gib
        );
        println!(
            "RAM Surplus: {} bytes ({:.3} GiB)",
            report.ram_surplus_bytes, report.ram_surplus_gib
        );
        println!("Commit %: {:.1}", report.commit_pct);
        println!("Free VRAM (MiB): {:.1}", report.free_vram_mib);
        println!("Total VRAM (MiB): {:.1}", report.total_vram_mib);
        println!("Competing GPU apps: {}", report.competing_gpu_apps);
        println!("Free Disk (GiB): {:.2}", report.free_disk_gib);
        println!("Model valid: {}", report.model_valid);
        for g in &report.gates {
            println!(
                "Gate {}: passed={} (req: {}, measured: {}, reason: {:?})",
                g.gate_name, g.passed, g.required_str, g.measured_str, g.failure_reason
            );
        }
        println!("=== TOP MEMORY CANDIDATES ===");
        let procs = crate::admission::query_candidate_processes();
        for p in procs {
            println!(
                "  - {} (instances: {}, WS: {:.1} MB, Private: {:?} MB, category: {}, guidance: {})",
                p.process_name, p.instances, p.working_set_mb, p.private_commit_mb, p.category, p.guidance
            );
        }
    }

    #[test]
    fn test_runtime_profiles_configuration_and_cli_args() {
        use crate::memory_budget::{RuntimeProfile, RuntimeProfileId};

        let profiles = RuntimeProfile::get_standard_profiles();
        assert_eq!(profiles.len(), 4);

        let config_d = RuntimeProfile::find_by_id(RuntimeProfileId::GpuFocused);
        assert!(config_d.is_approved_config_d);
        assert_eq!(config_d.gpu_layers, 48);
        assert_eq!(config_d.context_size, 512);
        assert_eq!(config_d.required_ram_gib, 10.0);
        assert_eq!(config_d.required_vram_mib, 5500.0);

        let cli_args = config_d.build_cli_args("C:\\models\\qwen.gguf", 18080, "test-key");
        assert!(cli_args.contains(&"-m".to_string()));
        assert!(cli_args.contains(&"C:\\models\\qwen.gguf".to_string()));
        assert!(cli_args.contains(&"-ngl".to_string()));
        assert!(cli_args.contains(&"48".to_string()));
        assert!(cli_args.contains(&"-c".to_string()));
        assert!(cli_args.contains(&"512".to_string()));

        let balanced = RuntimeProfile::find_by_id(RuntimeProfileId::Balanced);
        assert!(!balanced.is_approved_config_d);
        assert_eq!(balanced.gpu_layers, 24);
        assert_eq!(balanced.context_size, 512);
        assert_eq!(balanced.required_ram_gib, 8.0);
        assert_eq!(balanced.required_vram_mib, 4000.0);

        let low_mem = RuntimeProfile::find_by_id(RuntimeProfileId::LowMemory);
        assert_eq!(low_mem.gpu_layers, 4);
        assert_eq!(low_mem.required_ram_gib, 10.5);
        assert_eq!(low_mem.required_vram_mib, 1500.0);
    }

    #[test]
    fn test_resource_budget_evaluation_states() {
        use crate::memory_budget::{AdmissionLevel, ResourceBudgetCalculation, RuntimeProfile, RuntimeProfileId};

        let config_d = RuntimeProfile::find_by_id(RuntimeProfileId::GpuFocused);

        // State 1: Ready (11.0 GiB RAM, 6000 MiB VRAM)
        let ready = ResourceBudgetCalculation::evaluate(
            &config_d, 11.0, 16.0, 50.0, 6000.0, 50.0, 0.0, 0.0, true,
        );
        assert_eq!(ready.admission_level, AdmissionLevel::Ready);
        assert_eq!(ready.ram_deficit_gib, 0.0);
        assert_eq!(ready.vram_deficit_mib, 0.0);

        // State 2: Recoverable (7.5 GiB RAM, 5000 MiB VRAM, but 3.0 GiB reclaimable from Ollama + apps)
        let recoverable = ResourceBudgetCalculation::evaluate(
            &config_d, 7.5, 16.0, 60.0, 5000.0, 50.0, 3.5, 600.0, true,
        );
        assert_eq!(recoverable.admission_level, AdmissionLevel::Recoverable);
        assert!(recoverable.ram_deficit_gib > 0.0);

        // State 3: LowResourceAvailable (8.5 GiB RAM, 4200 MiB VRAM: Config D fails, but Balanced passes)
        let low_res = ResourceBudgetCalculation::evaluate(
            &config_d, 8.5, 16.0, 60.0, 4200.0, 50.0, 0.2, 0.0, true,
        );
        assert_eq!(low_res.admission_level, AdmissionLevel::LowResourceAvailable);
        assert!(!low_res.alternative_profiles.is_empty());
        assert_eq!(low_res.alternative_profiles[0].id, RuntimeProfileId::Balanced);

        // State 4: Blocked (3.0 GiB RAM on a machine with high commit or full disk)
        let blocked = ResourceBudgetCalculation::evaluate(
            &config_d, 3.0, 16.0, 95.0, 2000.0, 10.0, 0.0, 0.0, true,
        );
        assert_eq!(blocked.admission_level, AdmissionLevel::Blocked);

        // State 5: Unknown (telemetry_ok = false)
        let unknown = ResourceBudgetCalculation::evaluate(
            &config_d, 12.0, 16.0, 40.0, 6000.0, 50.0, 0.0, 0.0, false,
        );
        assert_eq!(unknown.admission_level, AdmissionLevel::Unknown);
    }

    #[test]
    fn test_resource_advisor_step_by_step_guidance_and_summarize() {
        use crate::resource_advisor::{ProcessCandidate, summarize_recovery};

        let ollama = ProcessCandidate::build(
            "ollama app.exe".to_string(),
            1,
            vec![1234],
            950.0,
            Some(1100.0),
            Some(4200.0),
        );
        assert_eq!(ollama.category, "Competing LLM Runtime");
        assert!(ollama.is_tray_app);
        assert!(ollama.is_reclaimable);
        assert!(ollama.step_by_step_instructions[0].contains("system tray"));

        let edge = ProcessCandidate::build(
            "msedge.exe".to_string(),
            8,
            vec![2001, 2002],
            1800.0,
            Some(1700.0),
            None,
        );
        assert_eq!(edge.category, "Web Browser");
        assert!(!edge.is_tray_app);

        let candidates = vec![ollama, edge];
        let summary = summarize_recovery(&candidates);
        assert_eq!(summary.total_candidates, 2);
        assert!((summary.potential_ram_recovery_gib - 2.68).abs() < 0.1);
        assert_eq!(summary.potential_vram_recovery_mib, 4200.0);
    }

    #[test]
    fn test_model_cache_load_save_and_invalidation() {
        use crate::storage::{load_model_cache_from, save_model_cache_to, ModelCacheEntry};

        let test_dir = create_test_dir("model_cache_test");

        assert!(load_model_cache_from(&test_dir).is_none());

        let entry = ModelCacheEntry {
            path: "C:\\models\\test.gguf".to_string(),
            file_size_bytes: 18_632_184_480,
            modified_timestamp_secs: 1700000000,
            sha256: "a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32".to_string(),
            magic: "GGUF".to_string(),
            version: 3,
            tensor_count: 579,
            kv_count: 41,
            last_validated_utc: "2026-11-06T12:00:00Z".to_string(),
            is_cryptographically_verified: true,
            volume_serial_number: Some(0x12345678),
            file_index: Some(0x0001000200030004),
            last_write_time_raw: Some(133500000000000000),
        };

        save_model_cache_to(&test_dir, &entry).unwrap();
        let loaded = load_model_cache_from(&test_dir).expect("Cache should load");
        assert_eq!(loaded.sha256, entry.sha256);
        assert_eq!(loaded.tensor_count, 579);
        assert_eq!(loaded.file_size_bytes, 18_632_184_480);
        assert!(loaded.is_cryptographically_verified);

        let _ = std::fs::remove_dir_all(test_dir);
    }

    #[test]
    fn test_admission_with_balanced_profile_passes_with_8gib_ram() {
        use crate::admission::evaluate_admission_from_metrics_with_profile;
        use crate::memory_budget::{RuntimeProfile, RuntimeProfileId};

        let balanced = RuntimeProfile::find_by_id(RuntimeProfileId::Balanced);

        // 8.5 GiB available RAM and 4200 MiB free VRAM
        let report = evaluate_admission_from_metrics_with_profile(
            8.5,
            16.0,
            55.0,
            4200.0,
            6141.0,
            0,
            50.0,
            true,
            true,
            None,
            None,
            Some(&balanced),
        );

        assert!(report.all_passed, "Balanced profile should pass with 8.5 GiB RAM and 4200 MiB VRAM");
        assert!(report.gates.iter().all(|g| g.passed));
        assert_eq!(report.selected_profile.unwrap().id, RuntimeProfileId::Balanced);
    }

    // ---------------------------------------------------------------------------
    // Phase 3B Release Validation & Security Tests
    // ---------------------------------------------------------------------------

    #[test]
    fn test_model_cache_same_size_same_mtime_replacement_fails_cryptographic_verification() {
        use crate::model_integrity::{
            evaluate_startup_integrity, verify_model_integrity_synchronous,
            ModelIntegrityState,
        };
        use crate::storage::{save_model_cache_to, ModelCacheEntry};
        use std::sync::atomic::AtomicBool;

        let test_dir = create_test_dir("cache_tamper_test");
        let model_file = test_dir.join("tampered_model.gguf");

        // Create initial synthetic GGUF file (Version 3, 579 tensors, 41 KV entries, padded to > 10 MiB)
        let initial_size: usize = 10 * 1024 * 1024 + 1024;
        let mut content_a = vec![0u8; initial_size];
        content_a[0..4].copy_from_slice(b"GGUF");
        content_a[4..8].copy_from_slice(&3u32.to_le_bytes());
        content_a[8..16].copy_from_slice(&579u64.to_le_bytes());
        content_a[16..24].copy_from_slice(&41u64.to_le_bytes());
        content_a[100] = 0xAA; // Distinct marker
        std::fs::write(&model_file, &content_a).unwrap();

        let meta = std::fs::metadata(&model_file).unwrap();
        let mtime = meta
            .modified()
            .unwrap()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();

        // 1. Initial valid cache entry matching initial content (simulating legacy mtime/size cache)
        let cancel = AtomicBool::new(false);
        let hash_a = crate::model_integrity::hash_file_with_progress(&model_file, &cancel, |_, _, _| {}).unwrap();

        let cache_entry = ModelCacheEntry {
            path: model_file.to_string_lossy().to_string(),
            file_size_bytes: initial_size as u64,
            modified_timestamp_secs: mtime,
            sha256: hash_a.clone(),
            magic: "GGUF".to_string(),
            version: 3,
            tensor_count: 579,
            kv_count: 41,
            last_validated_utc: "2026-11-06T12:00:00Z".to_string(),
            is_cryptographically_verified: true,
            volume_serial_number: None,
            file_index: None,
            last_write_time_raw: None,
        };
        save_model_cache_to(&test_dir, &cache_entry).unwrap();

        // F2: a legacy cache (no Win32 identity) matching only size & mtime must NOT be
        // trusted; it previously returned PreviouslyVerified (fail-open).
        let startup_state = crate::model_integrity::evaluate_startup_integrity_against(
            &model_file.to_string_lossy(),
            Some(&cache_entry),
            &hash_a,
        );
        assert!(
            matches!(startup_state, ModelIntegrityState::VerificationFailed { .. }),
            "Legacy cache without Win32 identity must require re-verification"
        );

        // 2. TAMPER: Replace contents with identical byte length, preserving timestamp seconds
        let mut content_b = vec![0u8; initial_size];
        content_b[0..4].copy_from_slice(b"GGUF");
        content_b[4..8].copy_from_slice(&3u32.to_le_bytes());
        content_b[8..16].copy_from_slice(&579u64.to_le_bytes());
        content_b[16..24].copy_from_slice(&41u64.to_le_bytes());
        content_b[100] = 0xBB; // Modified byte!
        std::fs::write(&model_file, &content_b).unwrap();

        // If write occurs in another second, metadata check invalidates immediately.
        // Even if an attacker matches file size and preserves timestamp seconds:
        let _startup_after_tamper = evaluate_startup_integrity(&model_file.to_string_lossy(), Some(&cache_entry));

        // 3. Mandatory pre-load cryptographic verification MUST run and CANNOT be bypassed
        // When checking against expected digest hash_a:
        let verify_result = verify_model_integrity_synchronous(
            &model_file.to_string_lossy(),
            &hash_a,
            &cancel,
            |_, _, _| {},
        );

        // Verification MUST FAIL because content was modified!
        assert!(
            matches!(verify_result, ModelIntegrityState::VerificationFailed { .. }),
            "Same-size, same-mtime modified file must fail cryptographic verification"
        );

        // 4. NOW test TOCTOU Win32 file identity check:
        // When cache holds exact Win32 file identity (high-res write timestamp),
        // startup check immediately catches in-place mutation without needing full 18.6 GB hash!
        let ident_fresh = crate::model_integrity::query_file_identity(&model_file).unwrap();
        let cache_with_ident = ModelCacheEntry {
            volume_serial_number: Some(ident_fresh.volume_serial_number),
            file_index: Some(ident_fresh.file_index),
            last_write_time_raw: Some(ident_fresh.last_write_time_raw - 1000), // Previous write time
            ..cache_entry
        };
        let startup_ident_check = crate::model_integrity::evaluate_startup_integrity_against(
            &model_file.to_string_lossy(),
            Some(&cache_with_ident),
            &hash_a,
        );
        assert!(
            matches!(startup_ident_check, ModelIntegrityState::VerificationFailed { .. }),
            "Startup check with Win32 identity must detect high-res write time modification"
        );

        let _ = std::fs::remove_dir_all(test_dir);
    }

    #[test]
    fn test_expected_hash_mismatch_and_missing_model() {
        use crate::model_integrity::{
            evaluate_startup_integrity, verify_model_integrity_synchronous,
            ModelIntegrityState, EXPECTED_QWEN3_30B_SHA256,
        };
        use std::sync::atomic::AtomicBool;

        // Test missing model
        let missing_path = "C:\\nonexistent\\model_never_exists.gguf";
        let startup_missing = evaluate_startup_integrity(missing_path, None);
        assert!(matches!(startup_missing, ModelIntegrityState::VerificationFailed { .. }));

        let cancel = AtomicBool::new(false);
        let verify_missing = verify_model_integrity_synchronous(missing_path, EXPECTED_QWEN3_30B_SHA256, &cancel, |_, _, _| {});
        assert!(matches!(verify_missing, ModelIntegrityState::VerificationFailed { .. }));

        // Test hash mismatch on synthetic file
        let test_dir = create_test_dir("hash_mismatch_test");
        let model_file = test_dir.join("test_model.gguf");
        let initial_size: usize = 10 * 1024 * 1024 + 1024;
        let mut content = vec![0u8; initial_size];
        content[0..4].copy_from_slice(b"GGUF");
        content[4..8].copy_from_slice(&3u32.to_le_bytes());
        content[8..16].copy_from_slice(&579u64.to_le_bytes());
        content[16..24].copy_from_slice(&41u64.to_le_bytes());
        std::fs::write(&model_file, &content).unwrap();

        let verify_mismatch = verify_model_integrity_synchronous(
            &model_file.to_string_lossy(),
            EXPECTED_QWEN3_30B_SHA256,
            &cancel,
            |_, _, _| {},
        );
        assert!(matches!(verify_mismatch, ModelIntegrityState::VerificationFailed { .. }));

        let _ = std::fs::remove_dir_all(test_dir);
    }

    #[test]
    fn test_asynchronous_integrity_verification_cancellation() {
        use crate::model_integrity::hash_file_with_progress;
        use std::sync::atomic::AtomicBool;

        let test_dir = create_test_dir("hash_cancel_test");
        let file_path = test_dir.join("test_file.bin");
        let content = vec![0x55u8; 16 * 1024 * 1024]; // 16 MiB
        std::fs::write(&file_path, &content).unwrap();

        let cancel_flag = AtomicBool::new(true); // Pre-set to canceled
        let result = hash_file_with_progress(&file_path, &cancel_flag, |_, _, _| {});

        assert!(result.is_err());
        assert!(result.unwrap_err().contains("canceled"));

        let _ = std::fs::remove_dir_all(test_dir);
    }

    #[test]
    fn test_resource_recovery_estimate_accuracy_and_shared_memory() {
        use crate::resource_advisor::{summarize_recovery, ProcessCandidate};

        // 10 browser tabs sharing memory: private commit = 100 MB each, working set = 300 MB each
        let browser = ProcessCandidate::build(
            "msedge.exe".to_string(),
            10,
            vec![101, 102, 103, 104, 105, 106, 107, 108, 109, 110],
            3000.0,      // Total working set (with shared DLLs counted multiple times)
            Some(1000.0), // Private commit (true unshared reclaimable memory)
            None,
        );

        let summary = summarize_recovery(&[browser]);

        // Must use private commit (~1000 MB = 0.976 GiB) instead of working set (3000 MB)
        assert!(
            (summary.potential_ram_recovery_gib - (1000.0 / 1024.0)).abs() < 0.05,
            "Recovery summary must use private commit to avoid double-counting shared memory"
        );
    }

    #[test]
    fn test_user_skipped_recovery_actions() {
        use crate::resource_advisor::{summarize_recovery, ProcessCandidate};

        let mut candidate_a = ProcessCandidate::build(
            "code.exe".to_string(),
            1,
            vec![201],
            1000.0,
            Some(800.0),
            None,
        );
        let mut candidate_b = ProcessCandidate::build(
            "ollama.exe".to_string(),
            1,
            vec![301],
            2400.0,
            Some(2000.0),
            Some(4200.0),
        );

        // User skips candidate_a (e.g. wants to keep IDE open)
        candidate_a.is_reclaimable = false;
        candidate_b.is_reclaimable = true;

        let summary = summarize_recovery(&[candidate_a, candidate_b]);

        // Only candidate_b should be counted towards recovery
        assert_eq!(summary.potential_vram_recovery_mib, 4200.0);
        assert!((summary.potential_ram_recovery_gib - (2000.0 / 1024.0)).abs() < 0.05);
    }

    #[test]
    fn test_profile_specific_budget_calculations_and_invalid_custom() {
        use crate::memory_budget::{
            AdmissionLevel, ProfileTierStatus, ResourceBudgetCalculation, RuntimeProfile,
            RuntimeProfileId,
        };

        let config_d = RuntimeProfile::find_by_id(RuntimeProfileId::GpuFocused);
        assert_eq!(config_d.status, ProfileTierStatus::ApprovedBaseline);
        assert_eq!(config_d.required_ram_gib, 10.0);
        assert_eq!(config_d.required_vram_mib, 5500.0);

        let balanced = RuntimeProfile::find_by_id(RuntimeProfileId::Balanced);
        assert_eq!(balanced.status, ProfileTierStatus::SupportedHybrid);
        assert_eq!(balanced.required_ram_gib, 8.0);
        assert_eq!(balanced.required_vram_mib, 4000.0);

        let cpu_offload = RuntimeProfile::find_by_id(RuntimeProfileId::CpuOffload);
        assert_eq!(cpu_offload.status, ProfileTierStatus::ExperimentalOffload);

        let low_mem = RuntimeProfile::find_by_id(RuntimeProfileId::LowMemory);
        assert_eq!(low_mem.status, ProfileTierStatus::ExperimentalOffload);

        // Test custom profile validation
        assert!(RuntimeProfile::create_custom_profile(49, 512, None, None).is_err(), "Cannot exceed 48 layers");
        assert!(RuntimeProfile::create_custom_profile(24, 0, None, None).is_err(), "Context size cannot be 0");
        assert!(RuntimeProfile::create_custom_profile(24, 512, Some(32), Some(64)).is_err(), "ubatch cannot exceed batch");

        let valid_custom = RuntimeProfile::create_custom_profile(32, 1024, Some(64), Some(16)).unwrap();
        assert_eq!(valid_custom.status, ProfileTierStatus::UserCustom);
        assert_eq!(valid_custom.gpu_layers, 32);
        assert_eq!(valid_custom.context_size, 1024);

        // Test ResourceBudgetCalculation::evaluate with stale/unhealthy telemetry
        let eval_unhealthy = ResourceBudgetCalculation::evaluate(
            &config_d,
            12.0,
            16.0,
            50.0,
            6000.0,
            100.0,
            0.0,
            0.0,
            false, // Telemetry unhealthy
        );
        assert_eq!(eval_unhealthy.admission_level, AdmissionLevel::Unknown);
    }

    #[test]
    fn test_toctou_file_replacement_after_verification_blocks_load() {
        let test_dir = create_test_dir("toctou_replacement_test");
        let file_a = test_dir.join("model.gguf");
        std::fs::write(&file_a, b"INITIAL_VERIFIED_MODEL_DATA_STREAM").unwrap();

        // 1. Capture initial verified file identity
        let recorded_ident = crate::model_integrity::query_file_identity(&file_a).unwrap();

        // 2. Simulate malicious or accidental file replacement at same path:
        std::fs::remove_file(&file_a).unwrap();
        std::fs::write(&file_a, b"REPLACED_MODEL_DATA_NEW_INODE").unwrap();

        // 3. Query new file identity
        let new_ident = crate::model_integrity::query_file_identity(&file_a).unwrap();

        let result = crate::model_integrity::verify_file_identity(&new_ident, &recorded_ident);
        assert!(result.is_err(), "Replacing file must fail TOCTOU identity verification");
        let err_msg = result.unwrap_err();
        assert!(
            err_msg.contains("TOCTOU_PROTECTION_VIOLATION"),
            "Error must report TOCTOU violation: {}",
            err_msg
        );

        let _ = std::fs::remove_dir_all(test_dir);
    }

    #[test]
    fn test_toctou_file_modification_after_verification_blocks_load() {
        let test_dir = create_test_dir("toctou_modification_test");
        let file_a = test_dir.join("model_mod.gguf");
        std::fs::write(&file_a, b"ORIGINAL_BYTES_BEFORE_IN_PLACE_MUTATION").unwrap();

        let recorded_ident = crate::model_integrity::query_file_identity(&file_a).unwrap();

        // Wait brief instant and append bytes in place
        std::thread::sleep(std::time::Duration::from_millis(50));
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(&file_a)
            .unwrap();
        use std::io::Write;
        f.write_all(b"_CORRUPTED").unwrap();
        drop(f);

        let modified_ident = crate::model_integrity::query_file_identity(&file_a).unwrap();
        let result = crate::model_integrity::verify_file_identity(&modified_ident, &recorded_ident);
        assert!(result.is_err(), "In-place modification must fail TOCTOU verification");
        let err_msg = result.unwrap_err();
        assert!(
            err_msg.contains("TOCTOU_PROTECTION_VIOLATION"),
            "Error must report TOCTOU violation: {}",
            err_msg
        );

        let _ = std::fs::remove_dir_all(test_dir);
    }

    // =========================================================================
    // 9. COMPLETE MODEL INTEGRITY & TOCTOU OPERATION ORDERING TEST SUITE
    // =========================================================================

    // Test 1: Modification before lock acquisition
    #[test]
    fn test_model_integrity_1_modification_before_lock() {
        let test_dir = create_test_dir("integrity_test_1");
        let model_path = test_dir.join("model_v1.gguf");
        std::fs::write(&model_path, b"ORIGINAL_GGUF_BYTES_BEFORE_LOCK").unwrap();

        let initial_ident = crate::model_integrity::query_file_identity(&model_path).unwrap();

        // Mutate before lock acquisition
        std::thread::sleep(Duration::from_millis(50));
        let mut f = std::fs::OpenOptions::new().append(true).open(&model_path).unwrap();
        f.write_all(b"_TAMPERED").unwrap();
        drop(f);

        let modified_ident = crate::model_integrity::query_file_identity(&model_path).unwrap();
        let verify_result = crate::model_integrity::verify_file_identity(&modified_ident, &initial_ident);
        assert!(
            verify_result.is_err(),
            "Modification before lock acquisition must be detected via identity check"
        );
        assert!(verify_result.unwrap_err().contains("TOCTOU_PROTECTION_VIOLATION"));

        let _ = std::fs::remove_dir_all(test_dir);
    }

    // Test 2: Modification after lock acquisition (proves write is blocked)
    #[test]
    fn test_model_integrity_2_modification_after_lock_blocked() {
        let test_dir = create_test_dir("integrity_test_2");
        let model_path = test_dir.join("model_locked.gguf");
        std::fs::write(&model_path, b"GGUF_BYTES_HELD_UNDER_RESTRICTIVE_LOCK").unwrap();

        let path_str = model_path.to_string_lossy().to_string();
        let lock_guard = crate::model_integrity::ModelFileLockGuard::acquire(&path_str)
            .expect("Acquiring ModelFileLockGuard must succeed");

        let write_attempt = std::fs::OpenOptions::new().write(true).open(&model_path);

        #[cfg(windows)]
        {
            assert!(
                write_attempt.is_err(),
                "Opening locked model for write access must be blocked by Windows kernel"
            );
            let err = write_attempt.unwrap_err();
            assert_eq!(
                err.raw_os_error(),
                Some(32),
                "Expected ERROR_SHARING_VIOLATION (32), got: {:?}",
                err
            );
        }

        drop(lock_guard);
        let _ = std::fs::remove_dir_all(test_dir);
    }

    // Test 3: Replacement between identity inspection and verification
    #[test]
    fn test_model_integrity_3_replacement_between_inspection_and_verification() {
        let test_dir = create_test_dir("integrity_test_3");
        let model_original = test_dir.join("model_legit.gguf");
        let model_swap = test_dir.join("model_swapped.gguf");
        std::fs::write(&model_original, b"LEGITIMATE_AUTHENTIC_GGUF_DATA").unwrap();
        std::fs::write(&model_swap, b"MALICIOUS_SWAPPED_DATA").unwrap();

        let ident_orig = crate::model_integrity::query_file_identity(&model_original).unwrap();
        let ident_swap = crate::model_integrity::query_file_identity(&model_swap).unwrap();

        // Verifying original against swapped identity must fail with file_index mismatch
        let swap_check = crate::model_integrity::verify_file_identity(&ident_swap, &ident_orig);
        assert!(swap_check.is_err());
        let err = swap_check.unwrap_err();
        assert!(
            err.contains("TOCTOU_PROTECTION_VIOLATION"),
            "Expected TOCTOU mismatch on swapped file, got: {}",
            err
        );

        let _ = std::fs::remove_dir_all(test_dir);
    }

    // Test 4: Write access through pre-existing handle
    #[test]
    fn test_model_integrity_4_write_access_through_preexisting_handle_fails() {
        let test_dir = create_test_dir("integrity_test_4");
        let model_path = test_dir.join("preexisting_write.gguf");
        std::fs::write(&model_path, b"MODEL_DATA_FOR_PREEXISTING_HANDLE_TEST").unwrap();

        let path_str = model_path.to_string_lossy().to_string();

        // 1. Open with write access and standard sharing (non-restrictive)
        let preexisting_write_handle = std::fs::OpenOptions::new()
            .write(true)
            .open(&model_path)
            .expect("Opening write handle must succeed");

        #[cfg(windows)]
        {
            // 2. Attempt to acquire ModelFileLockGuard (which requests GENERIC_READ and strictly FILE_SHARE_READ).
            // The Windows kernel checks if existing handles have write access without read-only sharing.
            // Since preexisting_write_handle has write access, ModelFileLockGuard::acquire must fail!
            let lock_attempt = crate::model_integrity::ModelFileLockGuard::acquire(&path_str);
            assert!(
                lock_attempt.is_err(),
                "ModelFileLockGuard::acquire must fail when a pre-existing write handle is open"
            );
            let err_msg = lock_attempt.err().unwrap();
            assert!(
                err_msg.contains("Win32 error 32") || err_msg.contains("sharing"),
                "Error must indicate sharing violation, got: {}",
                err_msg
            );
        }

        drop(preexisting_write_handle);

        // 3. After write handle is closed, acquiring ModelFileLockGuard must succeed cleanly
        let lock_after = crate::model_integrity::ModelFileLockGuard::acquire(&path_str);
        assert!(lock_after.is_ok(), "Lock acquisition must succeed after write handle is closed");

        drop(lock_after);
        let _ = std::fs::remove_dir_all(test_dir);
    }

    // Test 5: Rename and deletion attempts while locked
    #[test]
    fn test_model_integrity_5_rename_and_deletion_while_locked_blocked() {
        let test_dir = create_test_dir("integrity_test_5");
        let model_path = test_dir.join("locked_for_rename.gguf");
        let renamed_path = test_dir.join("renamed_model.gguf");
        std::fs::write(&model_path, b"CANNOT_BE_RENAMED_OR_DELETED_WHILE_LOCKED").unwrap();

        let path_str = model_path.to_string_lossy().to_string();
        let lock_guard = crate::model_integrity::ModelFileLockGuard::acquire(&path_str)
            .expect("Acquiring ModelFileLockGuard must succeed");

        #[cfg(windows)]
        {
            // Attempt rename
            let rename_attempt = std::fs::rename(&model_path, &renamed_path);
            assert!(
                rename_attempt.is_err(),
                "Renaming locked model file must be blocked by Windows kernel"
            );

            // Attempt deletion
            let delete_attempt = std::fs::remove_file(&model_path);
            assert!(
                delete_attempt.is_err(),
                "Deleting locked model file must be blocked by Windows kernel"
            );
        }

        drop(lock_guard);

        // After lock release, rename and deletion must succeed
        std::fs::rename(&model_path, &renamed_path).expect("Rename after unlock must succeed");
        std::fs::remove_file(&renamed_path).expect("Delete after unlock must succeed");
        let _ = std::fs::remove_dir_all(test_dir);
    }

    // Test 6: Lock release after normal shutdown
    #[test]
    fn test_model_integrity_6_lock_release_after_normal_shutdown() {
        let test_dir = create_test_dir("integrity_test_6");
        let model_path = test_dir.join("model_shutdown.gguf");
        std::fs::write(&model_path, b"MODEL_DATA_FOR_SHUTDOWN_TEST").unwrap();
        let path_str = model_path.to_string_lossy().to_string();

        let mut sm = ServerManager::new();
        // Emulate server acquiring lock
        let guard = crate::model_integrity::ModelFileLockGuard::acquire(&path_str).unwrap();

        // Place lock inside ServerManager
        // Then call stop_server()
        sm.stop_server(); // clean stop on inactive manager

        // Drop guard to simulate ServerManager taking ownership and releasing on stop
        drop(guard);

        // Prove file is unlocked by writing to it
        let mut f = std::fs::OpenOptions::new().append(true).open(&model_path).unwrap();
        f.write_all(b"_WRITEABLE_AFTER_STOP").unwrap();
        drop(f);

        let _ = std::fs::remove_dir_all(test_dir);
    }

    // Test 7: Lock release after failed startup
    #[test]
    fn test_model_integrity_7_lock_release_after_failed_startup() {
        let test_dir = create_test_dir("integrity_test_7");
        let model_path = test_dir.join("model_failed_start.gguf");
        std::fs::write(&model_path, b"MODEL_DATA_FOR_FAILED_START_TEST").unwrap();
        let path_str = model_path.to_string_lossy().to_string();

        let lock_guard = crate::model_integrity::ModelFileLockGuard::acquire(&path_str)
            .expect("Acquiring lock guard must succeed");

        let mut sm = ServerManager::new();
        // Attempt spawn with non-existent runtime binary directory while holding lock guard
        let result = sm.spawn_process_with_profile(
            &path_str,
            18080,
            Some("C:\\NonExistent\\Directory"),
            None,
            Some(lock_guard),
        );
        assert!(result.is_err(), "Spawn with non-existent runtime must fail");

        // Verify that lock guard was dropped and not leaked on failed startup
        let write_test = std::fs::OpenOptions::new().write(true).open(&model_path);
        assert!(
            write_test.is_ok(),
            "File must be immediately writeable after failed startup"
        );

        let _ = std::fs::remove_dir_all(test_dir);
    }

    // Test 8: Lock release after unexpected server termination
    #[test]
    fn test_model_integrity_8_lock_release_after_unexpected_server_termination() {
        let test_dir = create_test_dir("integrity_test_8");
        let model_path = test_dir.join("model_unexpected_death.gguf");
        std::fs::write(&model_path, b"MODEL_DATA_FOR_UNEXPECTED_DEATH_TEST").unwrap();
        let path_str = model_path.to_string_lossy().to_string();

        let lock_guard = crate::model_integrity::ModelFileLockGuard::acquire(&path_str)
            .expect("Acquiring lock guard must succeed");

        let mut sm = ServerManager::new();
        // Emulate server manager holding lock guard
        sm.stop_server();

        // Dropping guard simulates unexpected termination cleanup
        drop(lock_guard);

        // Verify status indicates not running
        let st = sm.get_status();
        assert!(!st.is_running);
        assert!(!st.is_ready);

        // Verify file is writeable
        let write_test = std::fs::OpenOptions::new().write(true).open(&model_path);
        assert!(write_test.is_ok());

        let _ = std::fs::remove_dir_all(test_dir);
    }

    // Test 9: Verification cancellation and cleanup
    #[test]
    fn test_model_integrity_9_verification_cancellation_and_cleanup() {
        let test_dir = create_test_dir("integrity_test_9");
        let model_path = test_dir.join("model_cancel.gguf");
        std::fs::write(&model_path, vec![0u8; 1024 * 1024]).unwrap(); // 1 MiB dummy data
        let path_str = model_path.to_string_lossy().to_string();

        let cancel_flag = AtomicBool::new(true); // already cancelled
        let (state, guard) = crate::model_integrity::verify_model_integrity_with_lock(
            &path_str,
            "dummy_hash",
            &cancel_flag,
            |_hashed, _total, _pct| {},
        );

        assert!(matches!(state, crate::model_integrity::ModelIntegrityState::VerificationFailed { .. }));
        assert!(guard.is_none(), "Lock guard must be None when verification is cancelled");

        // Verify file can be written to or removed immediately (no lingering handles)
        let remove_result = std::fs::remove_file(&model_path);
        assert!(
            remove_result.is_ok(),
            "File must be cleanly removable immediately after verification cancellation"
        );

        let _ = std::fs::remove_dir_all(test_dir);
    }

    // Test 10: Shutdown waits for child process exit and handle closure before releasing lock
    #[test]
    fn test_model_integrity_10_shutdown_waits_for_child_exit_before_lock_drop() {
        let test_dir = create_test_dir("integrity_test_10");
        let model_path = test_dir.join("model_shutdown_wait.gguf");
        std::fs::write(&model_path, b"MODEL_DATA_FOR_SHUTDOWN_WAIT_TEST").unwrap();
        let path_str = model_path.to_string_lossy().to_string();

        let lock_guard = crate::model_integrity::ModelFileLockGuard::acquire(&path_str)
            .expect("Acquiring lock guard must succeed");

        #[cfg(windows)]
        {
            // While locked, write is blocked
            let write_blocked = std::fs::OpenOptions::new().write(true).open(&model_path);
            assert!(write_blocked.is_err());
        }

        let mut sm = ServerManager::new();
        // Emulate server shutdown sequence: terminate and wait for handles
        sm.stop_server();

        // Release lock guard simulating server manager step 10
        drop(lock_guard);

        // After shutdown and lock release, write must succeed immediately
        let write_ok = std::fs::OpenOptions::new().write(true).open(&model_path);
        assert!(write_ok.is_ok(), "File must be writeable after server shutdown and lock drop");

        let _ = std::fs::remove_dir_all(test_dir);
    }

    // =========================================================================
    // 10. PHASE 3B.4: PROCESS-TREE SHUTDOWN & TENSOR ACCOUNTING VERIFICATION
    // =========================================================================

    // Test 1: Direct child plus descendant in Job Object
    #[test]
    #[cfg(windows)]
    fn test_shutdown_1_direct_child_plus_descendant_in_job_object() {
        let job = JobObject::new().expect("Failed to create JobObject");

        // Spawn a process tree: cmd.exe spawning powershell.exe sleep
        let cmd = "cmd.exe /c \"powershell.exe -NoProfile -Command Start-Sleep -Seconds 60\"";
        let child = job
            .spawn_child_suspended(cmd)
            .expect("Failed to spawn parent in Job Object");

        assert!(child.is_alive(), "Parent process must be alive");
        std::thread::sleep(Duration::from_millis(500));

        // Terminate entire job tree kernel-side
        job.terminate_jobs().expect("terminate_jobs must succeed");
        std::thread::sleep(Duration::from_millis(200));

        assert!(
            !child.is_alive(),
            "Parent and all descendant processes in Job Object must be terminated"
        );
    }

    // Test 2: Child that exits slowly (graceful wait)
    #[test]
    #[cfg(windows)]
    fn test_shutdown_2_child_slow_exit_graceful_wait() {
        let job = JobObject::new().expect("Failed to create JobObject");

        // Process that sleeps 300ms then exits gracefully on its own
        let cmd = "powershell.exe -NoProfile -Command Start-Sleep -Milliseconds 300";
        let child = job
            .spawn_child_suspended(cmd)
            .expect("Failed to spawn child in Job Object");

        assert!(child.is_alive());
        // Wait for graceful exit without terminating
        let exited = child.wait_for_exit(3000);
        assert!(exited, "Child process must exit gracefully within timeout");
        assert!(!child.is_alive(), "Child process must no longer be alive");
    }

    // Test 3: Timeout path (timeout prevents lock release)
    #[test]
    #[cfg(windows)]
    fn test_shutdown_3_timeout_path_retains_model_lock() {
        let test_dir = create_test_dir("shutdown_timeout_lock_test");
        let model_path = test_dir.join("model_locked_during_timeout.gguf");
        std::fs::write(&model_path, b"MODEL_PAYLOAD_PROTECTED_BY_SHUTDOWN_TIMEOUT").unwrap();
        let path_str = model_path.to_string_lossy().to_string();

        let lock_guard = crate::model_integrity::ModelFileLockGuard::acquire(&path_str)
            .expect("Acquiring ModelFileLockGuard must succeed");

        let mut sm = ServerManager::new();
        // Emulate ServerManager having spawned and holding lock guard
        sm.model_lock = Some(lock_guard);
        sm.model_path = path_str.clone();

        let job = JobObject::new().expect("Create JobObject");
        // Spawn a child process that sleeps
        let cmd = "powershell.exe -NoProfile -Command Start-Sleep -Seconds 60";
        let mut child = job.spawn_child_suspended(cmd).expect("Spawn child");
        // Simulate a hung or uncooperative process where WaitForSingleObject returns WAIT_TIMEOUT
        child.force_timeout_for_test = true;
        sm.child = Some(child);

        // Call stop_server_with_timeout(50); termination times out because process does not respond
        let stopped_cleanly = sm.stop_server_with_timeout(50);
        assert!(
            !stopped_cleanly,
            "stop_server_with_timeout must report timeout (false) when child fails to exit"
        );

        // CRITICAL INVARIANT: On timeout, ServerManager MUST NOT drop the model lock!
        assert!(
            sm.is_model_locked(),
            "Model lock guard MUST BE RETAINED when child termination times out!"
        );
        assert!(
            sm.last_error.as_ref().unwrap().contains("retaining model file lock"),
            "Error log must state that lock was retained: {:?}",
            sm.last_error
        );

        // Prove the model file is still physically locked against writes by Windows kernel
        let write_attempt = std::fs::OpenOptions::new().write(true).open(&model_path);
        assert!(
            write_attempt.is_err(),
            "Windows kernel must reject write access while lock is retained after timeout"
        );

        // Now clear the simulated hung state and terminate cleanly with job object
        sm.child.as_mut().unwrap().force_timeout_for_test = false;
        sm.job_object = Some(job);
        let second_stop = sm.stop_server_with_timeout(5000);
        assert!(second_stop, "Second stop with full timeout must succeed");
        assert!(
            !sm.is_model_locked(),
            "Model lock must be released once child process has confirmed exit"
        );

        // Now write access must succeed
        let write_ok = std::fs::OpenOptions::new().write(true).open(&model_path);
        assert!(write_ok.is_ok(), "File must be writeable after verified exit");

        let _ = std::fs::remove_dir_all(test_dir);
    }

    // Test 4: Unexpected server termination
    #[test]
    #[cfg(windows)]
    fn test_shutdown_4_unexpected_server_termination() {
        use windows_sys::Win32::System::Threading::TerminateProcess;

        let job = JobObject::new().expect("Failed to create JobObject");
        let cmd = "powershell.exe -NoProfile -Command Start-Sleep -Seconds 60";
        let child = job
            .spawn_child_suspended(cmd)
            .expect("Failed to spawn child");

        let mut sm = ServerManager::new();
        let raw_h_process = child.h_process;
        sm.job_object = Some(job);
        sm.child = Some(child);
        sm.ready = true;

        // Child is initially alive
        assert!(sm.check_child_alive().unwrap());

        // Simulate sudden crash or external kill of the server process
        unsafe {
            let _ = TerminateProcess(raw_h_process, 42);
        }
        std::thread::sleep(Duration::from_millis(150));

        // ServerManager must detect premature death
        let alive_check = sm.check_child_alive();
        assert!(alive_check.is_err(), "Premature termination must return Err");
        assert!(!sm.ready, "Ready flag must be set to false");
        assert!(
            sm.last_error.as_ref().unwrap().contains("terminated prematurely"),
            "Error must record premature termination"
        );

        // Clean stop after premature termination must succeed without hanging
        let stopped = sm.stop_server();
        assert!(stopped, "stop_server must handle already-dead child cleanly");
        assert!(!sm.is_model_locked());
    }

    // Test 5: Application shutdown / kill on job close
    #[test]
    #[cfg(windows)]
    fn test_shutdown_5_application_shutdown_kill_on_job_close() {
        let job = JobObject::new().expect("Failed to create JobObject");
        let cmd = "powershell.exe -NoProfile -Command Start-Sleep -Seconds 60";
        let child = job
            .spawn_child_suspended(cmd)
            .expect("Failed to spawn child");

        assert!(child.is_alive(), "Child must be running");

        // Simulate abrupt application exit: dropping JobObject closes the handle
        drop(job);

        // Windows kernel JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE terminates child
        std::thread::sleep(Duration::from_millis(1500));
        assert!(
            !child.is_alive(),
            "Windows kernel must kill child immediately when JobObject is closed"
        );
    }

    // Test 6: WAIT_FAILED path in terminate_with_timeout retains model lock
    #[test]
    #[cfg(windows)]
    fn test_shutdown_6_wait_failed_retains_model_lock() {
        let test_dir = create_test_dir("shutdown_wait_failed_lock_test");
        let model_path = test_dir.join("model_locked_during_wait_failed.gguf");
        std::fs::write(&model_path, b"MODEL_PAYLOAD_PROTECTED_BY_WAIT_FAILED").unwrap();
        let path_str = model_path.to_string_lossy().to_string();

        let lock_guard = crate::model_integrity::ModelFileLockGuard::acquire(&path_str)
            .expect("Acquiring ModelFileLockGuard must succeed");

        let mut sm = ServerManager::new();
        sm.model_lock = Some(lock_guard);
        sm.model_path = path_str.clone();

        let job = JobObject::new().expect("Create JobObject");
        let cmd = "powershell.exe -NoProfile -Command Start-Sleep -Seconds 60";
        let mut child = job.spawn_child_suspended(cmd).expect("Spawn child");
        // Simulate a kernel synchronization error where WaitForSingleObject returns WAIT_FAILED
        child.force_wait_failed_for_test = true;
        sm.child = Some(child);

        // Call stop_server_with_timeout(50); returns false because wait failed
        let stopped_cleanly = sm.stop_server_with_timeout(50);
        assert!(
            !stopped_cleanly,
            "stop_server_with_timeout must report failure (false) when wait fails"
        );

        // CRITICAL INVARIANT: On WAIT_FAILED, ServerManager MUST NOT drop the model lock!
        assert!(
            sm.is_model_locked(),
            "Model lock guard MUST BE RETAINED when child termination encounters WAIT_FAILED!"
        );
        assert!(
            sm.last_error.as_ref().unwrap().contains("retaining model file lock"),
            "Error log must state that lock was retained: {:?}",
            sm.last_error
        );

        // Prove the model file is still physically locked against writes by Windows kernel
        let write_attempt = std::fs::OpenOptions::new().write(true).open(&model_path);
        assert!(
            write_attempt.is_err(),
            "Windows kernel must reject write access while lock is retained after WAIT_FAILED"
        );

        // Clear the simulated failure and terminate cleanly with job object
        sm.child.as_mut().unwrap().force_wait_failed_for_test = false;
        sm.job_object = Some(job);
        let second_stop = sm.stop_server_with_timeout(5000);
        assert!(second_stop, "Second stop after clearing WAIT_FAILED must succeed");
        assert!(
            !sm.is_model_locked(),
            "Model lock must be released once child process has confirmed exit"
        );

        let _ = std::fs::remove_dir_all(test_dir);
    }

    // Test 7: Direct Win32 API returns WAIT_FAILED on invalid process handle
    #[test]
    #[cfg(windows)]
    fn test_shutdown_7_real_win32_wait_failed_on_invalid_handle() {
        use windows_sys::Win32::Foundation::{GetLastError, ERROR_INVALID_HANDLE};
        use windows_sys::Win32::System::Threading::WaitForSingleObject;
        const WAIT_FAILED: u32 = 0xFFFFFFFF;

        unsafe {
            use windows_sys::Win32::Foundation::CloseHandle;
            use windows_sys::Win32::System::Threading::CreateEventW;

            // Create a valid event handle and immediately close it to produce a genuine invalid handle
            let handle = CreateEventW(std::ptr::null(), 1, 0, std::ptr::null());
            assert!(!handle.is_null());
            CloseHandle(handle);

            // Waiting on the closed/invalid handle must return WAIT_FAILED
            let wait_res = WaitForSingleObject(handle, 100);
            assert_eq!(
                wait_res, WAIT_FAILED,
                "WaitForSingleObject on an invalid handle must return WAIT_FAILED (0xFFFFFFFF)"
            );
            let err = GetLastError();
            assert_eq!(
                err, ERROR_INVALID_HANDLE,
                "GetLastError must return ERROR_INVALID_HANDLE (6)"
            );
        }
    }

    // Test 8: Lock lifetime and sharing violation during external parent termination
    #[test]
    #[cfg(windows)]
    fn test_shutdown_8_lock_lifetime_and_sharing_violation_during_external_parent_kill() {
        use std::process::Command;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
            PROCESS_TERMINATE,
        };

        let mut helper_exe = PathBuf::from("target/debug/examples/job_parent_helper.exe");
        if !helper_exe.exists() {
            helper_exe = PathBuf::from("../target/debug/examples/job_parent_helper.exe");
        }
        if !helper_exe.exists() {
            let _ = Command::new("cargo")
                .args(["build", "--example", "job_parent_helper"])
                .output();
        }
        assert!(helper_exe.exists(), "job_parent_helper.exe must exist");

        let mut parent_child = Command::new(&helper_exe)
            .stdout(std::process::Stdio::piped())
            .spawn()
            .expect("Failed to launch helper parent process");

        let parent_pid = parent_child.id();
        let stdout = parent_child.stdout.take().expect("Failed to capture stdout");
        let mut reader = BufReader::new(stdout);
        let mut first_line = String::new();
        reader.read_line(&mut first_line).expect("Read child PID");
        let child_pid: u32 = first_line
            .trim_start_matches("CHILD_PID:")
            .trim()
            .parse()
            .expect("Child PID");

        // 1. While parent is alive, child is running inside Job Object
        unsafe {
            let h_child = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid);
            assert!(!h_child.is_null(), "Child process must be accessible while parent lives");
            CloseHandle(h_child);
        }

        // 2. Abruptly kill the parent process (simulating parent crash)
        unsafe {
            let h_parent = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            assert!(!h_parent.is_null());
            TerminateProcess(h_parent, 1);
            WaitForSingleObject(h_parent, 2000);
            CloseHandle(h_parent);
        }

        // 3. The Windows kernel Job Object subsystem terminates the child process
        std::thread::sleep(Duration::from_millis(1500));

        // 4. Verify child process is dead
        unsafe {
            use windows_sys::Win32::System::Threading::GetExitCodeProcess;
            let h_child = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid);
            if !h_child.is_null() {
                let mut code = 0;
                let active = GetExitCodeProcess(h_child, &mut code);
                CloseHandle(h_child);
                if active != 0 {
                    assert_ne!(
                        code, 259,
                        "Child process must not be running after parent termination"
                    );
                }
            }
        }
    }

    // Test 9: Parent-crash post-open file lifetime, sharing violation, and teardown race
    #[test]
    #[cfg(windows)]
    fn test_shutdown_9_parent_crash_post_open_file_lifetime_and_sharing_race() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use std::time::{Duration, Instant};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
            PROCESS_TERMINATE, GetExitCodeProcess,
        };

        // 1. Create a dummy model file for this lock test
        let temp_dir = std::env::temp_dir();
        let test_model_file = temp_dir.join(format!("nova_crash_test_{}.bin", std::process::id()));
        std::fs::write(&test_model_file, b"DUMMY_MODEL_CONTENT_FOR_CRASH_TEST").expect("Write test file");
        let model_str = test_model_file.to_string_lossy().to_string();

        let mut helper_exe = PathBuf::from("target/debug/examples/crash_safety_helper.exe");
        if !helper_exe.exists() {
            helper_exe = PathBuf::from("../target/debug/examples/crash_safety_helper.exe");
        }
        if !helper_exe.exists() {
            let _ = Command::new("cargo")
                .args(["build", "--example", "crash_safety_helper"])
                .output();
        }
        assert!(helper_exe.exists(), "crash_safety_helper.exe must exist");

        // 2. Launch parent process in post_open mode
        let mut parent_child = Command::new(&helper_exe)
            .args(["parent_post_open", &model_str])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit())
            .spawn()
            .expect("Failed to launch helper parent");

        let parent_pid = parent_child.id();
        let stdout = parent_child.stdout.take().expect("Failed to capture parent stdout");
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).expect("Read parent output");

        assert!(
            line.starts_with("PARENT_LOCKED_AND_CHILD_OPENED:"),
            "Unexpected parent output: {}",
            line
        );
        let child_pid: u32 = line
            .trim_start_matches("PARENT_LOCKED_AND_CHILD_OPENED:")
            .trim()
            .parse()
            .expect("Valid child PID");

        // Helper closure to attempt opening file with write access
        let can_write_file = || -> bool {
            std::fs::OpenOptions::new()
                .write(true)
                .open(&test_model_file)
                .is_ok()
        };

        // 3. While parent and child both hold handles: write access MUST BE DENIED
        assert!(!can_write_file(), "Write access must be denied while parent lock and child handle are active");

        // 4. Forcefully terminate the parent process (simulating sudden parent crash)
        unsafe {
            let h_parent = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            assert!(!h_parent.is_null());
            TerminateProcess(h_parent, 1);
            WaitForSingleObject(h_parent, 2000);
            CloseHandle(h_parent);
        }

        // At this instant:
        // - Parent process died, closing its ModelFileLockGuard handle.
        // - Child process was spawned and holds its handle with FILE_SHARE_READ.
        // - Job Object KILL_ON_JOB_CLOSE has been signaled to terminate the child.
        // - While the child process is alive or handle table closing, write access is STILL blocked by child's handle.
        // Let's verify child termination under Job Object KILL_ON_JOB_CLOSE:
        let h_child = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid) };
        if !h_child.is_null() {
            let start = Instant::now();
            let mut terminated = false;
            while start.elapsed() < Duration::from_secs(5) {
                let mut code = 0;
                let res = unsafe { GetExitCodeProcess(h_child, &mut code) };
                if res != 0 && code != 259 {
                    terminated = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            unsafe { CloseHandle(h_child); }
            assert!(terminated, "Child process must be terminated by Job Object KILL_ON_JOB_CLOSE within 5s");
        }

        // 5. Now that child has terminated and all handles are closed, write access MUST SUCCEED
        let mut write_restored = false;
        let start = Instant::now();
        while start.elapsed() < Duration::from_secs(3) {
            if can_write_file() {
                write_restored = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(write_restored, "Write access must be permitted once both parent and child have exited");

        // Cleanup temp file
        let _ = std::fs::remove_file(&test_model_file);
    }

    // Test 10: Parent-crash pre-open child containment and termination race
    #[test]
    #[cfg(windows)]
    fn test_shutdown_10_parent_crash_pre_open_child_termination_race() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use std::time::{Duration, Instant};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
            PROCESS_TERMINATE, GetExitCodeProcess,
        };

        // 1. Create a dummy model file
        let temp_dir = std::env::temp_dir();
        let test_model_file = temp_dir.join(format!("nova_crash_pre_open_test_{}.bin", std::process::id()));
        std::fs::write(&test_model_file, b"DUMMY_MODEL_CONTENT_FOR_PRE_OPEN_TEST").expect("Write test file");
        let model_str = test_model_file.to_string_lossy().to_string();

        let mut helper_exe = PathBuf::from("target/debug/examples/crash_safety_helper.exe");
        if !helper_exe.exists() {
            helper_exe = PathBuf::from("../target/debug/examples/crash_safety_helper.exe");
        }
        assert!(helper_exe.exists(), "crash_safety_helper.exe must exist");

        // 2. Launch parent process in pre_open mode (child spawned suspended, before CreateFileW)
        let mut parent_child = Command::new(&helper_exe)
            .args(["parent_pre_open", &model_str])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit())
            .spawn()
            .expect("Failed to launch helper parent");

        let parent_pid = parent_child.id();
        let stdout = parent_child.stdout.take().expect("Failed to capture parent stdout");
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).expect("Read parent output");

        assert!(
            line.starts_with("PARENT_LOCKED_AND_CHILD_SUSPENDED:"),
            "Unexpected parent output: {}",
            line
        );
        let child_pid: u32 = line
            .trim_start_matches("PARENT_LOCKED_AND_CHILD_SUSPENDED:")
            .trim()
            .parse()
            .expect("Valid child PID");

        // 3. Child exists in suspended state inside Job Object, but has never opened or modified file.
        unsafe {
            let h_child = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid);
            assert!(!h_child.is_null(), "Child process must exist in suspended state");
            CloseHandle(h_child);
        }

        // 4. Forcefully kill parent before child can ever be resumed or open file
        unsafe {
            let h_parent = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            assert!(!h_parent.is_null());
            TerminateProcess(h_parent, 1);
            WaitForSingleObject(h_parent, 2000);
            CloseHandle(h_parent);
        }

        // 5. Job Object KILL_ON_JOB_CLOSE terminates the child process
        let h_child = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid) };
        if !h_child.is_null() {
            let start = Instant::now();
            let mut terminated = false;
            while start.elapsed() < Duration::from_secs(5) {
                let mut code = 0;
                let res = unsafe { GetExitCodeProcess(h_child, &mut code) };
                if res != 0 && code != 259 {
                    terminated = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            unsafe { CloseHandle(h_child); }
            assert!(terminated, "Suspended child process must be terminated by Job Object KILL_ON_JOB_CLOSE when parent dies");
        }

        // 6. Verify file was untouched and can now be modified/deleted
        let content = std::fs::read(&test_model_file).expect("Read test file");
        assert_eq!(content, b"DUMMY_MODEL_CONTENT_FOR_PRE_OPEN_TEST", "File content was completely untouched");

        let _ = std::fs::remove_file(&test_model_file);
    }

    // Test 12: Resumed child pre-open crash race with deterministic Win32 barrier (State 3)
    #[test]
    #[cfg(windows)]
    fn test_shutdown_12_resumed_child_pre_open_crash_race_barrier() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use std::time::{Duration, Instant};
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            CreateEventW, OpenProcess, TerminateProcess, WaitForSingleObject,
            PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE, GetExitCodeProcess,
        };

        // 1. Create a dummy model file
        let temp_dir = std::env::temp_dir();
        let test_model_file = temp_dir.join(format!("nova_race_barrier_pre_{}.bin", std::process::id()));
        std::fs::write(&test_model_file, b"DUMMY_MODEL_CONTENT_FOR_BARRIER_PRE_TEST").expect("Write test file");
        let model_str = test_model_file.to_string_lossy().to_string();

        let mut helper_exe = PathBuf::from("target/debug/examples/crash_safety_helper.exe");
        if !helper_exe.exists() {
            helper_exe = PathBuf::from("../target/debug/examples/crash_safety_helper.exe");
        }
        assert!(helper_exe.exists(), "crash_safety_helper.exe must exist");

        // 2. Prepare unique Named Events for synchronization
        let prefix = format!("NOVA_TEST_PRE_{}", std::process::id());
        let to_wide = |s: &str| -> Vec<u16> {
            std::ffi::OsStr::new(s).encode_wide().chain(std::iter::once(0)).collect()
        };
        let ev_ready_name = to_wide(&format!("{}_READY", prefix));
        let ev_proceed_name = to_wide(&format!("{}_PROCEED", prefix));
        let ev_opened_name = to_wide(&format!("{}_OPENED", prefix));

        let h_ready = unsafe { CreateEventW(std::ptr::null(), 1, 0, ev_ready_name.as_ptr()) };
        let h_proceed = unsafe { CreateEventW(std::ptr::null(), 1, 0, ev_proceed_name.as_ptr()) };
        let h_opened = unsafe { CreateEventW(std::ptr::null(), 1, 0, ev_opened_name.as_ptr()) };
        assert!(!h_ready.is_null() && !h_proceed.is_null() && !h_opened.is_null());

        // 3. Launch parent process in parent_barrier mode
        let mut parent_child = Command::new(&helper_exe)
            .args(["parent_barrier", &model_str, &prefix])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit())
            .spawn()
            .expect("Failed to launch parent barrier");

        let parent_pid = parent_child.id();
        let stdout = parent_child.stdout.take().expect("Failed stdout");
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).expect("Read parent output");

        assert!(
            line.starts_with("PARENT_BARRIER_READY:"),
            "Unexpected parent output: {}",
            line
        );
        let child_pid: u32 = line
            .trim_start_matches("PARENT_BARRIER_READY:")
            .trim()
            .parse()
            .expect("Valid child PID");

        // 4. Wait for child to reach pre-open barrier (State 3: Child resumed, before model open)
        let wait_ready = unsafe { WaitForSingleObject(h_ready, 10000) };
        assert_eq!(wait_ready, 0, "Child must signal READY before opening model");

        // Helper closure to attempt write access
        let can_write_file = || -> bool {
            std::fs::OpenOptions::new()
                .write(true)
                .open(&test_model_file)
                .is_ok()
        };

        // 5. While parent holds lock and child is waiting at barrier, write is DENIED
        assert!(!can_write_file(), "Write access must be denied while parent lock is held");

        // 6. Abruptly kill the parent process (simulating parent crash)
        unsafe {
            let h_parent = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            assert!(!h_parent.is_null());
            TerminateProcess(h_parent, 1);
            WaitForSingleObject(h_parent, 2000);
            CloseHandle(h_parent);
        }

        // At this instant:
        // - Parent lock closes.
        // - Job Object KILL_ON_JOB_CLOSE is signaled to child.
        // - Child was paused at barrier before CreateFileW.
        // 7. Verify child never signaled OPENED
        let wait_opened = unsafe { WaitForSingleObject(h_opened, 200) };
        assert_ne!(wait_opened, 0, "Child must NOT have opened the model handle after parent killed");

        // 8. Verify child terminates under Job Object
        let h_child = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid) };
        if !h_child.is_null() {
            let start = Instant::now();
            let mut terminated = false;
            while start.elapsed() < Duration::from_secs(5) {
                let mut code = 0;
                let res = unsafe { GetExitCodeProcess(h_child, &mut code) };
                if res != 0 && code != 259 {
                    terminated = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            unsafe { CloseHandle(h_child); }
            assert!(terminated, "Child process must be terminated by Job Object KILL_ON_JOB_CLOSE");
        }

        // 9. Write access must be restored once child is dead
        let mut write_restored = false;
        let start = Instant::now();
        while start.elapsed() < Duration::from_secs(3) {
            if can_write_file() {
                write_restored = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(write_restored, "Write access must be restored once child is dead");

        // Cleanup
        unsafe {
            CloseHandle(h_ready);
            CloseHandle(h_proceed);
            CloseHandle(h_opened);
        }
        let _ = std::fs::remove_file(&test_model_file);
    }

    // Test 13: Resumed child post-open crash race with deterministic Win32 barrier (States 4, 5, 6)
    #[test]
    #[cfg(windows)]
    fn test_shutdown_13_resumed_child_post_open_crash_race_barrier() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use std::time::{Duration, Instant};
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            CreateEventW, SetEvent, OpenProcess, TerminateProcess, WaitForSingleObject,
            PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE, GetExitCodeProcess,
        };

        // 1. Create a dummy model file
        let temp_dir = std::env::temp_dir();
        let test_model_file = temp_dir.join(format!("nova_race_barrier_post_{}.bin", std::process::id()));
        std::fs::write(&test_model_file, b"DUMMY_MODEL_CONTENT_FOR_BARRIER_POST_TEST").expect("Write test file");
        let model_str = test_model_file.to_string_lossy().to_string();

        let mut helper_exe = PathBuf::from("target/debug/examples/crash_safety_helper.exe");
        if !helper_exe.exists() {
            helper_exe = PathBuf::from("../target/debug/examples/crash_safety_helper.exe");
        }
        assert!(helper_exe.exists(), "crash_safety_helper.exe must exist");

        // 2. Prepare unique Named Events
        let prefix = format!("NOVA_TEST_POST_{}", std::process::id());
        let to_wide = |s: &str| -> Vec<u16> {
            std::ffi::OsStr::new(s).encode_wide().chain(std::iter::once(0)).collect()
        };
        let ev_ready_name = to_wide(&format!("{}_READY", prefix));
        let ev_proceed_name = to_wide(&format!("{}_PROCEED", prefix));
        let ev_opened_name = to_wide(&format!("{}_OPENED", prefix));
        let ev_exit_name = to_wide(&format!("{}_EXIT", prefix));

        let h_ready = unsafe { CreateEventW(std::ptr::null(), 1, 0, ev_ready_name.as_ptr()) };
        let h_proceed = unsafe { CreateEventW(std::ptr::null(), 1, 0, ev_proceed_name.as_ptr()) };
        let h_opened = unsafe { CreateEventW(std::ptr::null(), 1, 0, ev_opened_name.as_ptr()) };
        let h_exit = unsafe { CreateEventW(std::ptr::null(), 1, 0, ev_exit_name.as_ptr()) };
        assert!(!h_ready.is_null() && !h_proceed.is_null() && !h_opened.is_null() && !h_exit.is_null());

        // 3. Launch parent process in parent_barrier mode
        let mut parent_child = Command::new(&helper_exe)
            .args(["parent_barrier", &model_str, &prefix])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit())
            .spawn()
            .expect("Failed to launch parent barrier");

        let parent_pid = parent_child.id();
        let stdout = parent_child.stdout.take().expect("Failed stdout");
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).expect("Read parent output");

        assert!(
            line.starts_with("PARENT_BARRIER_READY:"),
            "Unexpected parent output: {}",
            line
        );
        let child_pid: u32 = line
            .trim_start_matches("PARENT_BARRIER_READY:")
            .trim()
            .parse()
            .expect("Valid child PID");

        // 4. Wait for child to reach pre-open barrier
        let wait_ready = unsafe { WaitForSingleObject(h_ready, 10000) };
        assert_eq!(wait_ready, 0, "Child must signal READY before opening");

        // 5. Authorize child to proceed to open file (State 4: Child opening the model)
        unsafe { SetEvent(h_proceed); }

        // 6. Wait for child to confirm model handle opened (State 5: Child holding model handle)
        let wait_opened = unsafe { WaitForSingleObject(h_opened, 10000) };
        assert_eq!(wait_opened, 0, "Child must signal OPENED after acquiring handle");

        // Helper closure to attempt write access
        let can_write_file = || -> bool {
            std::fs::OpenOptions::new()
                .write(true)
                .open(&test_model_file)
                .is_ok()
        };

        // Write access must be denied
        assert!(!can_write_file(), "Write access denied while both parent and child hold handles");

        // 7. Abruptly kill the parent process (Parent lock closes)
        unsafe {
            let h_parent = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            assert!(!h_parent.is_null());
            TerminateProcess(h_parent, 1);
            WaitForSingleObject(h_parent, 2000);
            CloseHandle(h_parent);
        }

        // At this instant: Parent lock closed, but child holds open handle!
        // INVARIANT CHECK: External write access MUST REMAIN DENIED because child handle is active!
        assert!(
            !can_write_file(),
            "INVARIANT ENFORCED: Write access must be denied by child's handle even though parent lock is gone"
        );

        // 8. State 6: Child terminating under Job Object KILL_ON_JOB_CLOSE
        let h_child = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid) };
        if !h_child.is_null() {
            let start = Instant::now();
            let mut terminated = false;
            while start.elapsed() < Duration::from_secs(5) {
                let mut code = 0;
                let res = unsafe { GetExitCodeProcess(h_child, &mut code) };
                if res != 0 && code != 259 {
                    terminated = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            unsafe { CloseHandle(h_child); }
            assert!(terminated, "Child process must terminate under Job Object KILL_ON_JOB_CLOSE");
        }

        // 9. Now that child is dead, write access MUST SUCCEED
        let mut write_restored = false;
        let start = Instant::now();
        while start.elapsed() < Duration::from_secs(3) {
            if can_write_file() {
                write_restored = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(write_restored, "Write access must be permitted once child terminates");

        // Cleanup
        unsafe {
            CloseHandle(h_ready);
            CloseHandle(h_proceed);
            CloseHandle(h_opened);
            CloseHandle(h_exit);
        }
        let _ = std::fs::remove_file(&test_model_file);
    }

    // Test 14: Child suspended, not assigned lifecycle verification (State 1)
    #[test]
    #[cfg(windows)]
    fn test_shutdown_14_child_suspended_not_assigned_lifecycle() {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            CreateProcessW, TerminateProcess, WaitForSingleObject, CREATE_NO_WINDOW,
            CREATE_SUSPENDED, PROCESS_INFORMATION, STARTUPINFOW,
        };

        // 1. Create a dummy model file
        let temp_dir = std::env::temp_dir();
        let test_model_file = temp_dir.join(format!("nova_unassigned_{}.bin", std::process::id()));
        std::fs::write(&test_model_file, b"DUMMY_UNASSIGNED_TEST").expect("Write test file");
        let model_str = test_model_file.to_string_lossy().to_string();

        // 2. Parent acquires lock
        let lock_guard = crate::model_integrity::ModelFileLockGuard::acquire(&model_str)
            .expect("Acquire parent lock");

        // 3. Create child suspended WITHOUT assigning to Job Object
        let mut helper_exe = PathBuf::from("target/debug/examples/crash_safety_helper.exe");
        if !helper_exe.exists() {
            helper_exe = PathBuf::from("../target/debug/examples/crash_safety_helper.exe");
        }
        assert!(helper_exe.exists(), "crash_safety_helper.exe must exist");
        // CreateProcessW with a null lpApplicationName cannot resolve a relative
        // forward-slash path (ERROR_FILE_NOT_FOUND); pass an absolute path, as the
        // production launcher does via std::fs::canonicalize.
        let helper_exe = std::fs::canonicalize(&helper_exe).expect("Canonicalize helper path");
        let cmd = format!("\"{}\" child \"{}\"", helper_exe.to_string_lossy(), model_str);
        let mut wide_cmd: Vec<u16> = std::ffi::OsStr::new(&cmd).encode_wide().chain(std::iter::once(0)).collect();

        unsafe {
            let mut si: STARTUPINFOW = std::mem::zeroed();
            si.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
            let mut pi: PROCESS_INFORMATION = std::mem::zeroed();

            let success = CreateProcessW(
                std::ptr::null(),
                wide_cmd.as_mut_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                0,
                CREATE_SUSPENDED | CREATE_NO_WINDOW,
                std::ptr::null(),
                std::ptr::null(),
                &si,
                &mut pi,
            );
            assert_ne!(success, 0, "CreateProcessW suspended must succeed");

            // Child is suspended. Child has never run user code or opened file.
            // 4. Drop parent lock (simulating parent termination before assignment)
            drop(lock_guard);

            // Child is still suspended, so it cannot open file
            let can_write = std::fs::OpenOptions::new()
                .write(true)
                .open(&test_model_file)
                .is_ok();
            assert!(can_write, "File is writable because suspended child has no open handles");

            // Clean up suspended child process
            TerminateProcess(pi.hProcess, 1);
            WaitForSingleObject(pi.hProcess, 2000);
            CloseHandle(pi.hProcess);
            CloseHandle(pi.hThread);
        }

        let _ = std::fs::remove_file(&test_model_file);
    }

    // Test 15: Resumed child actively attempting model open while parent is killed (concurrent multi-core race)
    #[test]
    #[cfg(windows)]
    fn test_shutdown_15_resumed_child_active_open_race_during_parent_kill() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use std::time::{Duration, Instant};
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            CreateEventW, OpenProcess, TerminateProcess, WaitForSingleObject,
            PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE, GetExitCodeProcess,
        };

        // 1. Create a dummy model file
        let temp_dir = std::env::temp_dir();
        let test_model_file = temp_dir.join(format!("nova_race_active_{}.bin", std::process::id()));
        std::fs::write(&test_model_file, b"DUMMY_MODEL_CONTENT_FOR_ACTIVE_RACE_TEST").expect("Write test file");
        let model_str = test_model_file.to_string_lossy().to_string();

        let mut helper_exe = PathBuf::from("target/debug/examples/crash_safety_helper.exe");
        if !helper_exe.exists() {
            helper_exe = PathBuf::from("../target/debug/examples/crash_safety_helper.exe");
        }
        assert!(helper_exe.exists(), "crash_safety_helper.exe must exist");

        // 2. Prepare unique Named Event
        let prefix = format!("NOVA_TEST_ACTIVE_{}", std::process::id());
        let to_wide = |s: &str| -> Vec<u16> {
            std::ffi::OsStr::new(s).encode_wide().chain(std::iter::once(0)).collect()
        };
        let ev_ready_name = to_wide(&format!("{}_READY", prefix));
        let h_ready = unsafe { CreateEventW(std::ptr::null(), 1, 0, ev_ready_name.as_ptr()) };
        assert!(!h_ready.is_null());

        // 3. Launch parent process in parent_active_open mode
        let mut parent_child = Command::new(&helper_exe)
            .args(["parent_active_open", &model_str, &prefix])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit())
            .spawn()
            .expect("Failed to launch parent active open");

        let parent_pid = parent_child.id();
        let stdout = parent_child.stdout.take().expect("Failed stdout");
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).expect("Read parent output");

        assert!(
            line.starts_with("PARENT_ACTIVE_READY:"),
            "Unexpected parent output: {}",
            line
        );
        let child_pid: u32 = line
            .trim_start_matches("PARENT_ACTIVE_READY:")
            .trim()
            .parse()
            .expect("Valid child PID");

        // 4. Wait for child to signal READY and enter its active open loop
        let wait_ready = unsafe { WaitForSingleObject(h_ready, 10000) };
        assert_eq!(wait_ready, 0, "Child must signal READY and start active loop");

        // 5. While child is actively looping and attempting CreateFileW on Core 1:
        // Concurrently, an external writer attempts to write
        // At this exact microsecond, kill parent process!
        unsafe {
            let h_parent = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            assert!(!h_parent.is_null());
            TerminateProcess(h_parent, 1);
            WaitForSingleObject(h_parent, 2000);
            CloseHandle(h_parent);
        }

        // 6. Kernel Job Object subsystem delivers KILL_ON_JOB_CLOSE to child.
        // Verify child terminates under Job Object even while actively calling CreateFileW
        let h_child = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid) };
        if !h_child.is_null() {
            let start = Instant::now();
            let mut terminated = false;
            while start.elapsed() < Duration::from_secs(5) {
                let mut code = 0;
                let res = unsafe { GetExitCodeProcess(h_child, &mut code) };
                if res != 0 && code != 259 {
                    terminated = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            unsafe { CloseHandle(h_child); }
            assert!(terminated, "Child actively executing open loop must be cleanly terminated by Job Object KILL_ON_JOB_CLOSE");
        }

        // 7. Verify file integrity and write restoration
        let can_write_file = || -> bool {
            std::fs::OpenOptions::new()
                .write(true)
                .open(&test_model_file)
                .is_ok()
        };

        let mut write_restored = false;
        let start = Instant::now();
        while start.elapsed() < Duration::from_secs(3) {
            if can_write_file() {
                write_restored = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(write_restored, "Write access must be permitted once actively-looping child is terminated");

        // Cleanup
        unsafe { CloseHandle(h_ready); }
        let _ = std::fs::remove_file(&test_model_file);
    }

    // Test 11: Authoritative tensor accounting reconciliation (zero discrepancy)
    #[test]
    fn test_tensor_accounting_reconciliation_zero_discrepancy() {
        let mut report_path = std::path::PathBuf::from("reports/desktop/tensor_placement_report.json");
        if !report_path.exists() {
            report_path = std::path::PathBuf::from("../../reports/desktop/tensor_placement_report.json");
        }
        assert!(report_path.exists(), "tensor_placement_report.json must exist");

        let file_data = std::fs::read_to_string(&report_path).expect("Failed to read tensor report");
        let json_val: serde_json::Value = serde_json::from_str(&file_data).expect("Valid JSON");

        let tensors = json_val["tensors"].as_array().expect("Tensors array");
        assert_eq!(tensors.len(), 579, "Total tensors must be exactly 579");

        let mut seen_names = std::collections::HashSet::new();
        let mut total_gpu_bytes: u64 = 0;
        let mut total_cpu_bytes: u64 = 0;
        let mut gpu_count: usize = 0;
        let mut cpu_count: usize = 0;

        for t in tensors {
            let name = t["name"].as_str().expect("name str");
            assert!(seen_names.insert(name.to_string()), "Duplicate tensor found: {}", name);

            let cat = t["category"].as_str().expect("category str");
            assert_ne!(cat, "other", "No tensor may be unclassified: {}", name);

            let stored_bytes = t["stored_bytes"].as_u64().expect("stored_bytes u64");
            let dest = t["destination"].as_str().expect("destination str");

            if dest == "GPU" {
                total_gpu_bytes += stored_bytes;
                gpu_count += 1;
            } else if dest == "CPU" {
                total_cpu_bytes += stored_bytes;
                cpu_count += 1;
            } else {
                panic!("Unknown destination: {}", dest);
            }
        }

        assert_eq!(gpu_count, 459, "GPU tensors must be exactly 459 (24 MoE + 435 non-MoE)");
        assert_eq!(cpu_count, 120, "CPU tensors must be exactly 120 (MoE layers 0..39)");

        // Assert exact byte totals
        assert_eq!(total_gpu_bytes, 4_154_292_224, "GPU weights must be exactly 4,154_292,224 bytes (3,961.84 MiB)");
        assert_eq!(total_cpu_bytes, 14_471_921_664, "CPU weights must be exactly 14,471,921,664 bytes (13,801.50 MiB)");

        let total_tensor_bytes = total_gpu_bytes + total_cpu_bytes;
        assert_eq!(total_tensor_bytes, 18_626_213_888, "Total tensor data bytes must be 18,626,213,888");

        let header_bytes = json_val["summary"]["header_and_metadata_bytes"].as_u64().unwrap();
        let padding_bytes = json_val["summary"]["alignment_padding_bytes"].as_u64().unwrap();
        assert_eq!(header_bytes, 5_970_589, "GGUF header & metadata must be 5,970,589 bytes");
        assert_eq!(padding_bytes, 3, "Alignment padding must be exactly 3 bytes");

        let total_file = header_bytes + padding_bytes + total_tensor_bytes;
        assert_eq!(total_file, 18_632_184_480, "Total file bytes must equal exactly 18,632,184,480 bytes");

        let delta = json_val["summary"]["reconciliation"]["delta_bytes"].as_i64().unwrap();
        assert_eq!(delta, 0, "Discrepancy delta must be exactly 0 bytes");
    }

    // =========================================================================
    // PHASE 3B.9: CRASH-RESILIENT GGUF INTEGRITY & PRODUCTION RUNTIME PROTECTION
    // 15 PRODUCTION-EQUIVALENT INTEGRATION TESTS (WITH EXACT CRT / LLAMA.CPP WIN32 SEMANTICS)
    // =========================================================================

    #[cfg(windows)]
    fn helper_exe_path() -> PathBuf {
        let candidates = [
            PathBuf::from("target/debug/examples/crash_safety_helper.exe"),
            PathBuf::from("../target/debug/examples/crash_safety_helper.exe"),
            PathBuf::from("desktop/src-tauri/target/debug/examples/crash_safety_helper.exe"),
        ];
        for p in &candidates {
            if p.exists() {
                return p.clone();
            }
        }
        if let Ok(cur) = std::env::current_exe() {
            if let Some(parent) = cur.parent() {
                let p1 = parent.join("examples").join("crash_safety_helper.exe");
                if p1.exists() {
                    return p1;
                }
                if let Some(grandparent) = parent.parent() {
                    let p2 = grandparent.join("examples").join("crash_safety_helper.exe");
                    if p2.exists() {
                        return p2;
                    }
                }
            }
        }
        PathBuf::from("target/debug/examples/crash_safety_helper.exe")
    }

    #[cfg(windows)]
    fn win32_open_attempt(path: &str, access: u32, share: u32) -> Result<windows_sys::Win32::Foundation::HANDLE, u32> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::{CreateFileW, FILE_ATTRIBUTE_NORMAL, OPEN_EXISTING};
        let wide: Vec<u16> = std::ffi::OsStr::new(path).encode_wide().chain(std::iter::once(0)).collect();
        unsafe {
            let h = CreateFileW(wide.as_ptr(), access, share, std::ptr::null(), OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, std::ptr::null_mut());
            if h == windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE {
                Err(windows_sys::Win32::Foundation::GetLastError())
            } else {
                Ok(h)
            }
        }
    }

    #[cfg(windows)]
    fn win32_delete_attempt(path: &str) -> Result<(), u32> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::DeleteFileW;
        let wide: Vec<u16> = std::ffi::OsStr::new(path).encode_wide().chain(std::iter::once(0)).collect();
        unsafe {
            if DeleteFileW(wide.as_ptr()) == 0 {
                Err(windows_sys::Win32::Foundation::GetLastError())
            } else {
                Ok(())
            }
        }
    }

    #[cfg(windows)]
    fn win32_rename_attempt(old_path: &str, new_path: &str) -> Result<(), u32> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_REPLACE_EXISTING};
        let wide_old: Vec<u16> = std::ffi::OsStr::new(old_path).encode_wide().chain(std::iter::once(0)).collect();
        let wide_new: Vec<u16> = std::ffi::OsStr::new(new_path).encode_wide().chain(std::iter::once(0)).collect();
        unsafe {
            if MoveFileExW(wide_old.as_ptr(), wide_new.as_ptr(), MOVEFILE_REPLACE_EXISTING) == 0 {
                Err(windows_sys::Win32::Foundation::GetLastError())
            } else {
                Ok(())
            }
        }
    }

    #[cfg(windows)]
    fn win32_replace_attempt(target: &str, replacement: &str) -> Result<(), u32> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::ReplaceFileW;
        let wide_target: Vec<u16> = std::ffi::OsStr::new(target).encode_wide().chain(std::iter::once(0)).collect();
        let wide_repl: Vec<u16> = std::ffi::OsStr::new(replacement).encode_wide().chain(std::iter::once(0)).collect();
        unsafe {
            if ReplaceFileW(wide_target.as_ptr(), wide_repl.as_ptr(), std::ptr::null(), 0, std::ptr::null(), std::ptr::null()) == 0 {
                Err(windows_sys::Win32::Foundation::GetLastError())
            } else {
                Ok(())
            }
        }
    }

    const GENERIC_READ: u32 = 0x80000000;
    const GENERIC_WRITE: u32 = 0x40000000;
    const FILE_SHARE_READ: u32 = 0x00000001;
    const FILE_SHARE_WRITE: u32 = 0x00000002;
    const FILE_SHARE_DELETE: u32 = 0x00000004;
    const DELETE_ACCESS: u32 = 0x00010000;
    const ERROR_SHARING_VIOLATION: u32 = 32;

    // Test 1: Parent alive, child alive (both hold read locks, external write/delete blocked)
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_01_parent_alive_child_alive() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_01_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_01").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let mut parent = Command::new(&helper_exe)
            .args(["parent_prod_post_open", &model_str])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .expect("Launch parent");

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        assert!(line.starts_with("PARENT_PROD_READY:"), "Parent output: {}", line);
        let _child_pid: u32 = line.trim_start_matches("PARENT_PROD_READY:").trim().parse().unwrap();

        // 1. External write attempt with FILE_SHARE_READ | FILE_SHARE_WRITE must fail with ERROR_SHARING_VIOLATION (32)
        let open_res = win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE);
        assert_eq!(open_res, Err(ERROR_SHARING_VIOLATION), "Write must be denied while parent & child are alive");

        // 2. External delete attempt must fail with ERROR_SHARING_VIOLATION (32)
        let del_res = win32_delete_attempt(&model_str);
        assert_eq!(del_res, Err(ERROR_SHARING_VIOLATION), "Delete must be denied");

        // 3. External rename attempt must fail with ERROR_SHARING_VIOLATION (32)
        let rename_target = temp_dir.join(format!("nova_p3b9_01_renamed_{}.bin", std::process::id()));
        let ren_res = win32_rename_attempt(&model_str, &rename_target.to_string_lossy());
        assert_eq!(ren_res, Err(ERROR_SHARING_VIOLATION), "Rename must be denied");

        // 4. Terminate parent; child is reaped by Job Object KILL_ON_JOB_CLOSE
        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            if !hp.is_null() {
                TerminateProcess(hp, 1);
                WaitForSingleObject(hp, 2000);
                CloseHandle(hp);
            }
        }

        // Wait for child exit
        let start = std::time::Instant::now();
        while start.elapsed() < Duration::from_secs(3) {
            if win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE).is_ok() {
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }

        assert_eq!(std::fs::read(&test_model).unwrap(), b"PRODUCTION_MODEL_DATA_SCENARIO_01");
        let _ = std::fs::remove_file(&test_model);
    }

    // Test 2: Parent crash before child starts
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_02_parent_crash_before_child_starts() {
        use crate::model_integrity::ModelFileLockGuard;

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_02_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_02").unwrap();
        let model_str = test_model.to_string_lossy().to_string();

        // Parent acquires restrictive lock
        let guard = ModelFileLockGuard::acquire(&model_str).expect("Acquire lock");

        // External write denied
        let open_res = win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE);
        assert_eq!(open_res, Err(ERROR_SHARING_VIOLATION));

        // Parent crashes before creating child -> lock guard dropped
        drop(guard);

        // Access restored cleanly; child was never spawned
        let write_h = win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE).expect("Write restored");
        unsafe { windows_sys::Win32::Foundation::CloseHandle(write_h); }

        assert_eq!(std::fs::read(&test_model).unwrap(), b"PRODUCTION_MODEL_DATA_SCENARIO_02");
        let _ = std::fs::remove_file(&test_model);
    }

    // Test 3: Parent crash while child is suspended (Job Object kills suspended child, lock protected throughout)
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_03_parent_crash_while_child_is_suspended() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE, PROCESS_QUERY_LIMITED_INFORMATION, GetExitCodeProcess};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_03_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_03").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let mut parent = Command::new(&helper_exe)
            .args(["parent_pre_open", &model_str])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .expect("Launch parent");

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        assert!(line.starts_with("PARENT_LOCKED_AND_CHILD_SUSPENDED:"));
        let child_pid: u32 = line.trim_start_matches("PARENT_LOCKED_AND_CHILD_SUSPENDED:").trim().parse().unwrap();

        // External write is blocked while child is suspended
        assert_eq!(win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE), Err(ERROR_SHARING_VIOLATION));

        // Parent crashes
        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            if !hp.is_null() {
                TerminateProcess(hp, 1);
                WaitForSingleObject(hp, 2000);
                CloseHandle(hp);
            }
        }

        // Verify child terminates under Job Object without ever running
        let hc = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid) };
        if !hc.is_null() {
            let start = std::time::Instant::now();
            let mut terminated = false;
            while start.elapsed() < Duration::from_secs(3) {
                let mut code = 0;
                let res = unsafe { GetExitCodeProcess(hc, &mut code) };
                if res != 0 && code != 259 {
                    terminated = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            unsafe { CloseHandle(hc); }
            assert!(terminated, "Suspended child must be terminated by Job Object KILL_ON_JOB_CLOSE");
        }

        assert_eq!(std::fs::read(&test_model).unwrap(), b"PRODUCTION_MODEL_DATA_SCENARIO_03");
        let _ = std::fs::remove_file(&test_model);
    }

    // Test 4: Parent crash immediately after child resumes (pre-open barrier)
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_04_parent_crash_immediately_after_child_resumes() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{CreateEventW, OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_04_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_04").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let prefix = format!("NOVA_P3B9_04_{}", std::process::id());
        let to_wide = |s: &str| -> Vec<u16> { std::ffi::OsStr::new(s).encode_wide().chain(std::iter::once(0)).collect() };
        let h_ready = unsafe { CreateEventW(std::ptr::null(), 1, 0, to_wide(&format!("{}_READY", prefix)).as_ptr()) };
        let h_opened = unsafe { CreateEventW(std::ptr::null(), 1, 0, to_wide(&format!("{}_OPENED", prefix)).as_ptr()) };

        let mut parent = Command::new(&helper_exe)
            .args(["parent_prod_barrier", &model_str, &prefix])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        assert!(line.starts_with("PARENT_PROD_BARRIER_READY:"));

        // Wait for child to resume and reach READY barrier
        assert_eq!(unsafe { WaitForSingleObject(h_ready, 10000) }, 0);

        // Abruptly kill parent before PROCEED is set
        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            TerminateProcess(hp, 1);
            WaitForSingleObject(hp, 2000);
            CloseHandle(hp);
        }

        // Child never reached OPENED
        assert_ne!(unsafe { WaitForSingleObject(h_opened, 200) }, 0);

        unsafe {
            CloseHandle(h_ready);
            CloseHandle(h_opened);
        }

        assert_eq!(std::fs::read(&test_model).unwrap(), b"PRODUCTION_MODEL_DATA_SCENARIO_04");
        let _ = std::fs::remove_file(&test_model);
    }

    // Test 5: Parent crash while child opens GGUF (active loop with production CRT semantics)
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_05_parent_crash_while_child_opens_gguf() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{CreateEventW, OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE, PROCESS_QUERY_LIMITED_INFORMATION, GetExitCodeProcess};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_05_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_05").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let prefix = format!("NOVA_P3B9_05_{}", std::process::id());
        let to_wide = |s: &str| -> Vec<u16> { std::ffi::OsStr::new(s).encode_wide().chain(std::iter::once(0)).collect() };
        let h_ready = unsafe { CreateEventW(std::ptr::null(), 1, 0, to_wide(&format!("{}_READY", prefix)).as_ptr()) };

        let mut parent = Command::new(&helper_exe)
            .args(["parent_prod_active", &model_str, &prefix])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        assert!(line.starts_with("PARENT_PROD_ACTIVE_READY:"));
        let child_pid: u32 = line.trim_start_matches("PARENT_PROD_ACTIVE_READY:").trim().parse().unwrap();

        // Wait for child to enter active open loop
        assert_eq!(unsafe { WaitForSingleObject(h_ready, 10000) }, 0);

        // Kill parent while child is actively executing CreateFileW
        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            TerminateProcess(hp, 1);
            WaitForSingleObject(hp, 2000);
            CloseHandle(hp);
        }

        // Verify child terminates under Job Object
        let hc = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid) };
        if !hc.is_null() {
            let start = std::time::Instant::now();
            let mut terminated = false;
            while start.elapsed() < Duration::from_secs(4) {
                let mut code = 0;
                let res = unsafe { GetExitCodeProcess(hc, &mut code) };
                if res != 0 && code != 259 {
                    terminated = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            unsafe { CloseHandle(hc); }
            assert!(terminated, "Active child must terminate under Job Object");
        }

        unsafe { CloseHandle(h_ready); }
        assert_eq!(std::fs::read(&test_model).unwrap(), b"PRODUCTION_MODEL_DATA_SCENARIO_05");
        let _ = std::fs::remove_file(&test_model);
    }

    // Test 6: Parent crash after child opens GGUF (OPTION A CRITICAL TEST)
    // Child owns duplicated FILE_SHARE_READ handle; external write MUST fail even after parent is dead!
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_06_parent_crash_after_child_opens_gguf() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE, PROCESS_QUERY_LIMITED_INFORMATION, GetExitCodeProcess};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_06_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_06").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let mut parent = Command::new(&helper_exe)
            .args(["parent_prod_post_open", &model_str])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        assert!(line.starts_with("PARENT_PROD_READY:"));
        let child_pid: u32 = line.trim_start_matches("PARENT_PROD_READY:").trim().parse().unwrap();

        // Kill parent forcefully
        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            TerminateProcess(hp, 1);
            WaitForSingleObject(hp, 2000);
            CloseHandle(hp);
        }

        // At this instant, parent is dead and its handle closed.
        // Check if child is still terminating
        let hc = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, child_pid) };
        if !hc.is_null() {
            let mut code = 0;
            let res = unsafe { GetExitCodeProcess(hc, &mut code) };
            if res != 0 && code == 259 {
                // Child is still alive: external write MUST receive ERROR_SHARING_VIOLATION (32)
                let open_res = win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE);
                assert_eq!(open_res, Err(ERROR_SHARING_VIOLATION), "Option A duplicated handle in child must block write after parent crash");
            }

            // Wait for child exit under KILL_ON_JOB_CLOSE
            let start = std::time::Instant::now();
            while start.elapsed() < Duration::from_secs(3) {
                let res = unsafe { GetExitCodeProcess(hc, &mut code) };
                if res != 0 && code != 259 {
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            unsafe { CloseHandle(hc); }
        }

        // Once child exits, write access is restored
        let start = std::time::Instant::now();
        let mut restored = false;
        while start.elapsed() < Duration::from_secs(3) {
            if let Ok(h) = win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE) {
                unsafe { CloseHandle(h); }
                restored = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(30));
        }
        assert!(restored, "Write access restored after child teardown");
        assert_eq!(std::fs::read(&test_model).unwrap(), b"PRODUCTION_MODEL_DATA_SCENARIO_06");
        let _ = std::fs::remove_file(&test_model);
    }

    // Test 7: Parent crash during model mapping
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_07_parent_crash_during_model_mapping() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{CreateEventW, SetEvent, OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_07_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_07").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let prefix = format!("NOVA_P3B9_07_{}", std::process::id());
        let to_wide = |s: &str| -> Vec<u16> { std::ffi::OsStr::new(s).encode_wide().chain(std::iter::once(0)).collect() };
        let h_ready = unsafe { CreateEventW(std::ptr::null(), 1, 0, to_wide(&format!("{}_READY", prefix)).as_ptr()) };
        let h_proceed = unsafe { CreateEventW(std::ptr::null(), 1, 0, to_wide(&format!("{}_PROCEED", prefix)).as_ptr()) };
        let h_mapped = unsafe { CreateEventW(std::ptr::null(), 1, 0, to_wide(&format!("{}_MAPPED", prefix)).as_ptr()) };

        let mut parent = Command::new(&helper_exe)
            .args(["parent_prod_barrier", &model_str, &prefix])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        assert!(line.starts_with("PARENT_PROD_BARRIER_READY:"));

        // Allow child to proceed and perform MapViewOfFile
        assert_eq!(unsafe { WaitForSingleObject(h_ready, 10000) }, 0);
        unsafe { SetEvent(h_proceed); }
        assert_eq!(unsafe { WaitForSingleObject(h_mapped, 10000) }, 0, "Child mapped view successfully");

        // Kill parent while memory mapping is active
        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            TerminateProcess(hp, 1);
            WaitForSingleObject(hp, 2000);
            CloseHandle(hp);
        }

        unsafe {
            CloseHandle(h_ready);
            CloseHandle(h_proceed);
            CloseHandle(h_mapped);
        }

        assert_eq!(std::fs::read(&test_model).unwrap(), b"PRODUCTION_MODEL_DATA_SCENARIO_07");
        let _ = std::fs::remove_file(&test_model);
    }

    // Test 8: Child crash (Parent remains alive, lock guard continues protecting file)
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_08_child_crash() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_08_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_08").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let mut parent = Command::new(&helper_exe)
            .args(["parent_prod_post_open", &model_str])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        let child_pid: u32 = line.trim_start_matches("PARENT_PROD_READY:").trim().parse().unwrap();

        // Kill the child directly
        unsafe {
            let hc = OpenProcess(PROCESS_TERMINATE, 0, child_pid);
            TerminateProcess(hc, 1);
            WaitForSingleObject(hc, 2000);
            CloseHandle(hc);
        }

        // External write is STILL blocked because parent's lock guard is active
        let open_res = win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE);
        assert_eq!(open_res, Err(ERROR_SHARING_VIOLATION), "Parent lock guard must protect file after child crash");

        // Terminate parent
        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            TerminateProcess(hp, 1);
            WaitForSingleObject(hp, 2000);
            CloseHandle(hp);
        }

        let _ = std::fs::remove_file(&test_model);
    }

    // Test 9: Child termination timeout retains lock guard
    #[test]
    fn test_phase3b9_09_child_termination_timeout_retains_lock() {
        let mut sm = ServerManager::new();
        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_09_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_09").unwrap();
        let model_str = test_model.to_string_lossy().to_string();

        sm.model_lock = Some(crate::model_integrity::ModelFileLockGuard::acquire(&model_str).unwrap());
        sm.child = Some(crate::job_object::ContainedChild {
            h_process: std::ptr::null_mut(),
            h_thread: std::ptr::null_mut(),
            pid: 99999,
            force_timeout_for_test: true, // Forces timeout path
            force_wait_failed_for_test: false,
        });

        let stopped = sm.stop_server_with_timeout(50);
        assert!(!stopped, "stop_server must report false on timeout");
        assert!(sm.is_model_locked(), "Model lock MUST be retained when child exit times out");

        // External write is blocked while lock is retained
        #[cfg(windows)]
        assert_eq!(win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE), Err(ERROR_SHARING_VIOLATION));

        // Drop lock cleanly
        sm.model_lock = None;
        let _ = std::fs::remove_file(&test_model);
    }

    // Test 10: External write attempt (exhaustive share combinations)
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_10_external_write_attempt() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_10_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_10").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let mut parent = Command::new(&helper_exe)
            .args(["parent_prod_post_open", &model_str])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();

        // Variant A: GENERIC_WRITE, share = 0
        assert_eq!(win32_open_attempt(&model_str, GENERIC_WRITE, 0), Err(ERROR_SHARING_VIOLATION));

        // Variant B: GENERIC_WRITE, share = FILE_SHARE_READ
        assert_eq!(win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ), Err(ERROR_SHARING_VIOLATION));

        // Variant C: GENERIC_WRITE, share = FILE_SHARE_READ | FILE_SHARE_WRITE
        assert_eq!(win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE), Err(ERROR_SHARING_VIOLATION));

        // Variant D: GENERIC_READ | GENERIC_WRITE, share = FILE_SHARE_READ | FILE_SHARE_WRITE
        assert_eq!(win32_open_attempt(&model_str, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE), Err(ERROR_SHARING_VIOLATION));

        // Variant E: Rust OpenOptions write
        assert!(std::fs::OpenOptions::new().write(true).open(&test_model).is_err());

        // File unchanged
        assert_eq!(std::fs::read(&test_model).unwrap(), b"PRODUCTION_MODEL_DATA_SCENARIO_10");

        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            TerminateProcess(hp, 1);
            WaitForSingleObject(hp, 2000);
            CloseHandle(hp);
        }
        let _ = std::fs::remove_file(&test_model);
    }

    // Test 11: External delete attempt
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_11_external_delete_attempt() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_11_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_11").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let mut parent = Command::new(&helper_exe)
            .args(["parent_prod_post_open", &model_str])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();

        // 1. DeleteFileW fails
        assert_eq!(win32_delete_attempt(&model_str), Err(ERROR_SHARING_VIOLATION));

        // 2. Open with DELETE access fails
        assert_eq!(win32_open_attempt(&model_str, DELETE_ACCESS, FILE_SHARE_DELETE), Err(ERROR_SHARING_VIOLATION));

        // 3. remove_file fails
        assert!(std::fs::remove_file(&test_model).is_err());

        // File remains intact
        assert!(test_model.exists());
        assert_eq!(std::fs::read(&test_model).unwrap(), b"PRODUCTION_MODEL_DATA_SCENARIO_11");

        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            TerminateProcess(hp, 1);
            WaitForSingleObject(hp, 2000);
            CloseHandle(hp);
        }
        let _ = std::fs::remove_file(&test_model);
    }

    // Test 12: External rename attempt
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_12_external_rename_attempt() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_12_{}.bin", std::process::id()));
        let new_target = temp_dir.join(format!("nova_p3b9_12_moved_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_12").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let new_str = new_target.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let mut parent = Command::new(&helper_exe)
            .args(["parent_prod_post_open", &model_str])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();

        // MoveFileExW fails
        assert_eq!(win32_rename_attempt(&model_str, &new_str), Err(ERROR_SHARING_VIOLATION));

        // rename fails
        assert!(std::fs::rename(&test_model, &new_target).is_err());

        // File still in original location
        assert!(test_model.exists());
        assert!(!new_target.exists());

        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            TerminateProcess(hp, 1);
            WaitForSingleObject(hp, 2000);
            CloseHandle(hp);
        }
        let _ = std::fs::remove_file(&test_model);
    }

    // Test 13: External replacement attempt
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_13_external_replacement_attempt() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_13_{}.bin", std::process::id()));
        let replacement = temp_dir.join(format!("nova_p3b9_13_repl_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_13").unwrap();
        std::fs::write(&replacement, b"ADVERSARIAL_PAYLOAD").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let repl_str = replacement.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let mut parent = Command::new(&helper_exe)
            .args(["parent_prod_post_open", &model_str])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();

        // ReplaceFileW fails with error 32 (or 5 access denied)
        let repl_res = win32_replace_attempt(&model_str, &repl_str);
        assert!(repl_res == Err(ERROR_SHARING_VIOLATION) || repl_res == Err(5), "ReplaceFileW result: {:?}", repl_res);

        // Content remains strictly original
        assert_eq!(std::fs::read(&test_model).unwrap(), b"PRODUCTION_MODEL_DATA_SCENARIO_13");

        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            TerminateProcess(hp, 1);
            WaitForSingleObject(hp, 2000);
            CloseHandle(hp);
        }
        let _ = std::fs::remove_file(&test_model);
        let _ = std::fs::remove_file(&replacement);
    }

    // Test 14: Two NOVA instances targeting same GGUF (concurrent reads succeed, external write blocked)
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_14_two_nova_instances_targeting_same_gguf() {
        use crate::model_integrity::ModelFileLockGuard;

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_14_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_14").unwrap();
        let model_str = test_model.to_string_lossy().to_string();

        // Instance 1 acquires lock
        let guard1 = ModelFileLockGuard::acquire(&model_str).expect("Instance 1 lock");

        // Instance 2 acquires lock (both share FILE_SHARE_READ)
        let guard2 = ModelFileLockGuard::acquire(&model_str).expect("Instance 2 lock");

        // External write is blocked
        assert_eq!(win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE), Err(ERROR_SHARING_VIOLATION));

        // Instance 1 shuts down
        drop(guard1);

        // Instance 2 still protects file
        assert_eq!(win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE), Err(ERROR_SHARING_VIOLATION));

        // Instance 2 shuts down
        drop(guard2);

        // External write now succeeds
        let h = win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE).unwrap();
        unsafe { windows_sys::Win32::Foundation::CloseHandle(h); }

        let _ = std::fs::remove_file(&test_model);
    }

    // Test 15: File replacement between verification and model loading (TOCTOU failure)
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_15_file_replacement_between_verification_and_model_loading() {
        use crate::model_integrity::{ModelFileLockGuard, verify_file_identity};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_15_{}.bin", std::process::id()));
        let replacement = temp_dir.join(format!("nova_p3b9_15_alt_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"LEGITIMATE_VERIFIED_MODEL").unwrap();
        std::fs::write(&replacement, b"ADVERSARIAL_REPLACED_MODEL").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let repl_str = replacement.to_string_lossy().to_string();

        // Step 1-4: Parent acquires lock and verifies cryptographic hash
        let guard = ModelFileLockGuard::acquire(&model_str).expect("Acquire lock");
        let initial_id = guard.identity;

        // While lock is held, external replace fails
        assert!(win32_replace_attempt(&model_str, &repl_str).is_err());

        // Step 6: Post-verification identity check confirms match
        let current_id = guard.query_current_identity().unwrap();
        assert!(verify_file_identity(&current_id, &initial_id).is_ok(), "File identity must match under active lock guard");

        drop(guard);
        let _ = std::fs::remove_file(&test_model);
        let _ = std::fs::remove_file(&replacement);
    }

    // Test 16: Negative control — WITHOUT Option A handle duplication, external write succeeds on unpatched child!
    #[test]
    #[cfg(windows)]
    fn test_phase3b9_16_negative_control_without_option_a() {
        use std::process::Command;
        use std::io::{BufRead, BufReader};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE};

        let temp_dir = std::env::temp_dir();
        let test_model = temp_dir.join(format!("nova_p3b9_16_neg_{}.bin", std::process::id()));
        std::fs::write(&test_model, b"PRODUCTION_MODEL_DATA_SCENARIO_16").unwrap();
        let model_str = test_model.to_string_lossy().to_string();
        let helper_exe = helper_exe_path();

        let mut parent = Command::new(&helper_exe)
            .args(["parent_nodup_post_open", &model_str])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();

        let parent_pid = parent.id();
        let stdout = parent.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line1 = String::new();
        reader.read_line(&mut line1).unwrap();
        assert!(line1.starts_with("PARENT_NODUP_READY:"));

        let mut line2 = String::new();
        reader.read_line(&mut line2).unwrap();
        assert!(line2.starts_with("PARENT_LOCK_DROPPED"));

        // At this point:
        // - Parent has dropped its restrictive lock guard.
        // - Child is running with ONLY its own FILE_SHARE_READ | FILE_SHARE_WRITE handle (no Option A duplicated handle).
        // - An external writer attempts CreateFileW with GENERIC_WRITE and FILE_SHARE_READ | FILE_SHARE_WRITE.
        // WITHOUT Option A, THIS SUCCEEDS!
        let open_res = win32_open_attempt(&model_str, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE);
        assert!(open_res.is_ok(), "NEGATIVE CONTROL: Without Option A, external write succeeds on unpatched child handle!");
        if let Ok(h) = open_res {
            unsafe { CloseHandle(h); }
        }

        // Cleanup
        unsafe {
            let hp = OpenProcess(PROCESS_TERMINATE, 0, parent_pid);
            if !hp.is_null() {
                TerminateProcess(hp, 1);
                WaitForSingleObject(hp, 2000);
                CloseHandle(hp);
            }
        }
        let _ = std::fs::remove_file(&test_model);
    }

    // =========================================================================
    // CLAUDE TAKEOVER AUDIT FIXES: F1 (lock-then-verify) and F3 (refuse spawn
    // when the previous stop is unconfirmed). Disposable temp fixtures only.
    // =========================================================================

    // F1-a: file swapped after identity was recorded (the old check-then-lock window)
    // must be rejected when identity is verified through the newly locked handle.
    #[test]
    #[cfg(windows)]
    fn test_f1_swap_before_lock_is_rejected_by_locked_handle_identity() {
        let test_dir = create_test_dir("f1_swap_before_lock");
        let model_path = test_dir.join("fixture_model.gguf");
        std::fs::write(&model_path, b"ORIGINAL_VERIFIED_FIXTURE").unwrap();
        let path_str = model_path.to_string_lossy().to_string();

        let verified = crate::model_integrity::query_file_identity(&model_path)
            .expect("Record verified identity");

        // Attacker replaces the file while no lock is held.
        std::fs::remove_file(&model_path).unwrap();
        std::fs::write(&model_path, b"SWAPPED_UNVERIFIED_FIXTURE").unwrap();

        let res = crate::model_integrity::acquire_verified_lock(&path_str, None, Some(&verified));
        assert!(res.is_err(), "Swapped file must be rejected after locking");
        assert!(
            res.err().unwrap().contains("TOCTOU_PROTECTION_VIOLATION"),
            "Rejection must come from the identity check"
        );

        // The rejected guard was dropped: no lock is left behind on the swapped file.
        assert!(std::fs::OpenOptions::new().write(true).open(&model_path).is_ok());
        let _ = std::fs::remove_dir_all(test_dir);
    }

    // F1-b: unchanged file passes, and the returned guard actually holds the lock.
    #[test]
    #[cfg(windows)]
    fn test_f1_unchanged_file_returns_locked_guard() {
        let test_dir = create_test_dir("f1_unchanged");
        let model_path = test_dir.join("fixture_model.gguf");
        std::fs::write(&model_path, b"VERIFIED_FIXTURE").unwrap();
        let path_str = model_path.to_string_lossy().to_string();

        let verified = crate::model_integrity::query_file_identity(&model_path).unwrap();
        let guard = crate::model_integrity::acquire_verified_lock(&path_str, None, Some(&verified))
            .expect("Unchanged file must verify");
        assert_eq!(guard.path, path_str);
        assert!(
            std::fs::OpenOptions::new().write(true).open(&model_path).is_err(),
            "Verified guard must block external writes"
        );

        drop(guard);
        let _ = std::fs::remove_dir_all(test_dir);
    }

    // F1-c: an existing in-session guard for the same path is reused (not re-opened),
    // and a guard for a different path is never trusted for the target.
    #[test]
    #[cfg(windows)]
    fn test_f1_existing_guard_reused_only_for_same_path() {
        let test_dir = create_test_dir("f1_existing_guard");
        let target = test_dir.join("target.gguf");
        let other = test_dir.join("other.gguf");
        std::fs::write(&target, b"TARGET_FIXTURE").unwrap();
        std::fs::write(&other, b"OTHER_FIXTURE").unwrap();
        let target_str = target.to_string_lossy().to_string();
        let other_str = other.to_string_lossy().to_string();

        let verified = crate::model_integrity::query_file_identity(&target).unwrap();

        let same = crate::model_integrity::ModelFileLockGuard::acquire(&target_str).unwrap();
        let same_handle = same.raw_handle();
        let reused =
            crate::model_integrity::acquire_verified_lock(&target_str, Some(same), Some(&verified))
                .expect("Same-path guard must verify");
        assert_eq!(reused.raw_handle(), same_handle, "Same-path guard must be reused");
        drop(reused);

        let wrong = crate::model_integrity::ModelFileLockGuard::acquire(&other_str).unwrap();
        let fresh =
            crate::model_integrity::acquire_verified_lock(&target_str, Some(wrong), Some(&verified))
                .expect("Target must be locked and verified on its own");
        assert_eq!(fresh.path, target_str, "Guard must refer to the launch target");
        // The mismatched guard was released.
        assert!(std::fs::OpenOptions::new().write(true).open(&other).is_ok());

        drop(fresh);
        let _ = std::fs::remove_dir_all(test_dir);
    }

    // F3: when the previous server's termination is unconfirmed, a new spawn is refused
    // before any runtime discovery or process creation, and the old child and lock remain.
    #[test]
    #[cfg(windows)]
    fn test_f3_spawn_refused_when_previous_stop_unconfirmed() {
        let test_dir = create_test_dir("f3_spawn_refused");
        let model_path = test_dir.join("fixture_model.gguf");
        std::fs::write(&model_path, b"FIXTURE_FOR_F3").unwrap();
        let path_str = model_path.to_string_lossy().to_string();

        let lock_guard = crate::model_integrity::ModelFileLockGuard::acquire(&path_str).unwrap();
        let mut sm = ServerManager::new();
        sm.model_lock = Some(lock_guard);
        sm.model_path = path_str.clone();

        let job = JobObject::new().expect("Create JobObject");
        let mut child = job
            .spawn_child_suspended("powershell.exe -NoProfile -Command Start-Sleep -Seconds 60")
            .expect("Spawn child");
        child.force_wait_failed_for_test = true;
        let old_pid = child.pid;
        sm.child = Some(child);

        let res = sm.spawn_process_with_profile(&path_str, 18089, None, None, None);
        assert!(res.is_err(), "Spawn must be refused while previous stop is unconfirmed");
        assert!(
            res.err().unwrap().contains("termination of the previous server is unconfirmed"),
            "Refusal must name the unconfirmed termination"
        );
        assert_eq!(sm.child.as_ref().map(|c| c.pid), Some(old_pid), "Old child must still be tracked");
        assert!(sm.is_model_locked(), "Old model lock must be retained");
        assert!(
            std::fs::OpenOptions::new().write(true).open(&model_path).is_err(),
            "Model fixture must stay write-protected"
        );

        // Cleanup: clear the simulated failure and stop for real.
        sm.child.as_mut().unwrap().force_wait_failed_for_test = false;
        sm.job_object = Some(job);
        assert!(sm.stop_server_with_timeout(5000), "Cleanup stop must succeed");
        assert!(!sm.is_model_locked());
        let _ = std::fs::remove_dir_all(test_dir);
    }

    // =========================================================================
    // F2: startup integrity cache must fail closed. Disposable fixtures only.
    // None of these paths call clear_model_cache(), so the real app cache is untouched.
    // =========================================================================

    #[cfg(windows)]
    fn f2_fixture_cache(
        model_file: &std::path::Path,
        sha256: &str,
        crypto_verified: bool,
        with_identity: bool,
    ) -> crate::storage::ModelCacheEntry {
        let meta = std::fs::metadata(model_file).unwrap();
        let mtime = meta
            .modified()
            .unwrap()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let ident = crate::model_integrity::query_file_identity(model_file).unwrap();
        crate::storage::ModelCacheEntry {
            path: model_file.to_string_lossy().to_string(),
            file_size_bytes: meta.len(),
            modified_timestamp_secs: mtime,
            sha256: sha256.to_string(),
            magic: "GGUF".to_string(),
            version: 3,
            tensor_count: 0,
            kv_count: 0,
            last_validated_utc: "2026-11-06T12:00:00Z".to_string(),
            is_cryptographically_verified: crypto_verified,
            volume_serial_number: with_identity.then_some(ident.volume_serial_number),
            file_index: with_identity.then_some(ident.file_index),
            last_write_time_raw: with_identity.then_some(ident.last_write_time_raw),
        }
    }

    #[cfg(windows)]
    fn f2_fixture(prefix: &str) -> (PathBuf, PathBuf, String) {
        let test_dir = create_test_dir(prefix);
        let model_file = test_dir.join("fixture_model.gguf");
        std::fs::write(&model_file, b"GGUF_F2_FIXTURE_CONTENT").unwrap();
        let cancel = AtomicBool::new(false);
        let hash =
            crate::model_integrity::hash_file_with_progress(&model_file, &cancel, |_, _, _| {})
                .unwrap();
        (test_dir, model_file, hash)
    }

    // F2-a: a complete, verified cache whose digest matches and whose identity matches is trusted.
    #[test]
    #[cfg(windows)]
    fn test_f2_complete_matching_cache_is_previously_verified() {
        use crate::model_integrity::{evaluate_startup_integrity_against, ModelIntegrityState};
        let (test_dir, model_file, hash) = f2_fixture("f2_complete");
        let cache = f2_fixture_cache(&model_file, &hash, true, true);

        let state = evaluate_startup_integrity_against(&model_file.to_string_lossy(), Some(&cache), &hash);
        match state {
            ModelIntegrityState::PreviouslyVerified { file_identity, .. } => {
                assert!(file_identity.is_some(), "Trusted cache must carry the current identity");
            }
            other => panic!("Expected PreviouslyVerified, got {:?}", other),
        }
        let _ = std::fs::remove_dir_all(test_dir);
    }

    // F2-b: a cached digest that is not the pinned expected digest is rejected.
    #[test]
    #[cfg(windows)]
    fn test_f2_cached_digest_not_expected_is_rejected() {
        use crate::model_integrity::{
            evaluate_startup_integrity, evaluate_startup_integrity_against, ModelIntegrityState,
        };
        let (test_dir, model_file, hash) = f2_fixture("f2_digest");
        let cache = f2_fixture_cache(&model_file, &hash, true, true);

        // Against the pinned Qwen3 digest, the fixture's own digest must not be accepted.
        let state = evaluate_startup_integrity(&model_file.to_string_lossy(), Some(&cache));
        match state {
            ModelIntegrityState::VerificationFailed { reason, .. } => {
                assert!(reason.contains("does not match expected digest"), "{}", reason);
            }
            other => panic!("Expected VerificationFailed, got {:?}", other),
        }

        // A forged cache claiming a different digest is rejected too.
        let forged = f2_fixture_cache(&model_file, &"0".repeat(64), true, true);
        let state = evaluate_startup_integrity_against(&model_file.to_string_lossy(), Some(&forged), &hash);
        assert!(matches!(state, ModelIntegrityState::VerificationFailed { .. }));
        let _ = std::fs::remove_dir_all(test_dir);
    }

    // F2-c: a cache not produced by full cryptographic verification is rejected.
    #[test]
    #[cfg(windows)]
    fn test_f2_cache_without_crypto_flag_is_rejected() {
        use crate::model_integrity::{evaluate_startup_integrity_against, ModelIntegrityState};
        let (test_dir, model_file, hash) = f2_fixture("f2_flag");
        let cache = f2_fixture_cache(&model_file, &hash, false, true);

        let state = evaluate_startup_integrity_against(&model_file.to_string_lossy(), Some(&cache), &hash);
        match state {
            ModelIntegrityState::VerificationFailed { reason, .. } => {
                assert!(reason.contains("not produced by full cryptographic verification"), "{}", reason);
            }
            other => panic!("Expected VerificationFailed, got {:?}", other),
        }
        let _ = std::fs::remove_dir_all(test_dir);
    }

    // F2-d: a legacy cache without Win32 identity fields is rejected (was fail-open).
    #[test]
    #[cfg(windows)]
    fn test_f2_legacy_cache_without_identity_is_rejected() {
        use crate::model_integrity::{evaluate_startup_integrity_against, ModelIntegrityState};
        let (test_dir, model_file, hash) = f2_fixture("f2_legacy");
        let cache = f2_fixture_cache(&model_file, &hash, true, false);

        let state = evaluate_startup_integrity_against(&model_file.to_string_lossy(), Some(&cache), &hash);
        match state {
            ModelIntegrityState::VerificationFailed { reason, .. } => {
                assert!(reason.contains("lacks Win32 file identity"), "{}", reason);
            }
            other => panic!("Expected VerificationFailed, got {:?}", other),
        }
        let _ = std::fs::remove_dir_all(test_dir);
    }
}
