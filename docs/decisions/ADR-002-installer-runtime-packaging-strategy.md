# ADR-002: Installer & Inference Runtime Packaging Strategy

- **Date:** 2026-11-06
- **Status:** Proposed & Evaluated in Phase 3B.10
- **Context:**
  The current Tauri v2 NSIS bundle generates a lightweight 1.97 MiB installer (`NOVA Chat_0.1.0_x64-setup.exe`).
  However, this installer currently contains only the Webview2 frontend and the `nova-chat.exe` Tauri shell. It does not package the CUDA inference runtime (`llama-server.exe` and 10 companion DLLs: `cublas64_12.dll`, `cudart64_12.dll`, `ggml-cuda.dll`, `llama.dll`, `mtmd.dll`, etc., totalling ~1.5 GB).
  The 18.6 GB GGUF model file must remain completely decoupled from the installer under all circumstances.
  A clear architectural decision is required on how the CUDA runtime is packaged and discovered.
- **Alternatives Evaluated:**

  ### Option A: Bundled Runtime via Tauri Resources
  - *Mechanism:* Add `../../runtime/windows/llama-cpp/*` into `tauri.conf.json` under `bundle.resources`.
  - *Packaging:* NSIS extracts the complete runtime into `%LOCALAPPDATA%\Programs\NOVA Chat\resources\runtime\windows\llama-cpp\`.
  - *Discovery:* `ServerManager::find_llama_server_binary` automatically checks `exe_dir.join("resources").join("runtime").join("windows").join("llama-cpp").join("llama-server.exe")`.
  - *Installer Size:* ~650–750 MiB compressed (extracting to ~1.5 GB).
  - *Pros:* Self-contained desktop application; works out-of-the-box on clean machines without repository; uninstaller cleanly removes all runtime files.
  - *Cons:* Large initial installer download.

  ### Option B: Bundled Sidecar via Tauri `externalBin`
  - *Mechanism:* Configure `llama-server` as a Tauri sidecar in `tauri.conf.json` under `bundle.externalBin`.
  - *Packaging:* Tauri renames the binary with target triples (`llama-server-x86_64-pc-windows-msvc.exe`) and places it next to `nova-chat.exe`.
  - *Cons:* Incompatible with standard llama.cpp layout because companion CUDA DLLs (`cublas64_12.dll`, `ggml-cuda.dll`) must be co-located or present in PATH; breaks existing relative path expectations and directory structure.

  ### Option C: Separately Installed Runtime with Hash Validation
  - *Mechanism:* The installer remains lightweight (~2 MiB). On first launch, the user provides the path to their CUDA runtime directory via Settings, or a separate runtime zip is extracted to `%LOCALAPPDATA%\NOVA Chat\runtime\`.
  - *Verification:* `ServerManager::verify_runtime_dependencies` verifies presence of all 11 required binaries and checks cryptographic SHA-256 hashes against a pinned manifest.
  - *Pros:* Keeps installer small (~2 MiB); allows runtime updates without reinstalling GUI; allows users with pre-installed CUDA / llama.cpp to point directly to existing binaries.
  - *Cons:* Requires a two-step setup for new users.

- **Decision & Recommendation:**
  Adopt **Option A (Bundled Runtime via Tauri Resources)** as the default production release packaging:
  1. Bundle the pinned llama.cpp runtime (build 12032, commit `dde879bc4`) and required CUDA DLLs into `resources/runtime/windows/llama-cpp/`.
  2. Maintain `ServerManager`'s fallback search order:
     - 1. User-specified custom runtime path in Settings (permitting power-user override).
     - 2. Co-located `resources/runtime/windows/llama-cpp/` (installed application path).
     - 3. Development workspace `runtime/windows/llama-cpp/` (developer workflow).
  3. Validate runtime completeness via `verify_runtime_dependencies` and cryptographic SHA-256 manifests.
  4. The 18.6 GB GGUF model file strictly remains external and user-selected via the UI file picker or settings.
- **Installer Readiness Classification:**
  Until Option A is packaged and clean-install tested in an authorized test environment, the current installer must be documented accurately as:
  *"Unsigned per-user NSIS installer builds successfully; clean installation, runtime discovery, and uninstall behavior remain to be validated."*
