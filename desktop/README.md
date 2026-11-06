# NOVA Chat — Desktop Local AI Workstation

NOVA Chat is a high-performance, resource-aware Windows desktop application designed to run large local Mixture-of-Experts (MoE) LLMs—specifically **Qwen3-30B-A3B** (Q4_K_M quantization)—on consumer hardware such as 6 GB VRAM laptop GPUs and 16 GB system RAM.

Built with **Tauri v2**, **React 19**, **TypeScript**, and an embedded **Rust** systems core, NOVA Chat bridges user-facing chat interactions with strict memory governance, hardware admission gates, and process containment.

---

## Key Features

### 1. Sub-Second Instant Startup
- **Zero Blocking Pre-Scans:** Startup does not perform 18.6 GB whole-file reads or heavy SHA-256 recalculations.
- **Model Metadata Cache:** Model headers (magic, version, tensor count, KV count, file size, timestamp, SHA-256) are cached in `%LOCALAPPDATA%\NOVA Chat\model_cache.json`. Subsequent boots validate against filesystem `mtime` and size in under 20 ms.
- **Immediate Interactive UI:** The desktop interface appears immediately without splash delays or freezes.

### 2. Dynamic Memory Budget Engine & Runtime Profiles
NOVA Chat pairs customizable runtime profiles with strict, fail-closed hardware admission gates (Host RAM, Commit Charge, VRAM, GPU Contention, NVMe Disk, GGUF Integrity):

| Profile | GPU Offload | Context Window | Required RAM | Required VRAM | Target Hardware Scenario |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **GPU Focused (Config D)** | 48 layers | 512 tokens | 10.0 GiB | 5,500 MiB | Dedicated workstation (Ollama/browsers closed) |
| **Balanced** | 24 layers | 512 tokens | 8.0 GiB | 4,000 MiB | Multitasking with moderate RAM headroom |
| **CPU Offload** | 12 layers | 512 tokens | 6.5 GiB | 2,500 MiB | Systems with active VRAM pressure |
| **Low Memory** | 4 layers | 512 tokens | 5.5 GiB | 1,500 MiB | Background apps open (e.g., browsers, IDE) |
| **Custom** | User-defined | Configurable | Dynamic | Dynamic | Power-user tuning via Settings modal |

### 3. Intelligent User-Controlled Resource Recovery
- **No Destructive Actions:** NOVA Chat never executes invasive `taskkill`, service termination, or forced memory flushing.
- **Application Identification:** Detects heavy host processes (e.g., `ollama app.exe`, `chrome.exe`, `msedge.exe`, `code.exe`).
- **Step-by-Step Guidance:** Provides clear, expandable instructions (e.g., "Right-click the Ollama llama icon in the Windows Notification Area tray and select 'Quit Ollama'").
- **Real-Time Recovery Estimator:** Shows cumulative potential RAM and VRAM reclamation.
- **Alternative Profile Switcher:** When memory is tight, the modal suggests viable lower-tier profiles that can start immediately without closing background software.

### 4. Real-Time Telemetry & Containment Dashboard
- **Live Hardware Meters:** Visual gauges for Host Physical RAM, Dedicated GPU VRAM, System Commit Charge, and NVMe Disk Storage.
- **Admission Gates Matrix:** Instant audit of all six system gates (RAM, Commit, VRAM, GPU Contention, Disk, Model GGUF).
- **Process Isolation Details:** Shows Win32 Job Object containment (`KILL_ON_JOB_CLOSE`) ensuring zero orphaned `llama-server.exe` processes.
- **Zero Idle Footprint:** When unloaded, NOVA Chat consumes negligible host resources (~45 MB RAM for the Tauri shell).

### 5. Rich, Modern Chat Interface
- **Fast SSE Streaming:** Direct Server-Sent Events token streaming via local loopback (`127.0.0.1`).
- **Native Markdown Renderer:** Renders headings, bold, italic, lists, blockquotes, tables, inline code, and syntax-highlighted code blocks with language badges and one-click copy buttons.
- **Conversation Management:** Search conversations by title, double-click or click to inline rename, delete, and switch conversations.
- **Per-Request Cancellation Token:** Responsive generation abort that cleans up state without race conditions.
- **Light & Dark Theme:** Full styling system supporting high-contrast Dark mode and clean Light mode.

### 6. Strict Data Privacy & Isolation
- **100% Local Execution:** All settings and chat histories reside in `%LOCALAPPDATA%\NOVA Chat\`.
- **Zero Cloud Telemetry:** No analytical pings, model weights, or user inputs ever leave the machine.
- **Injectable Test Storage:** Backend tests use isolated temporary storage directories, guaranteeing production data is never touched or overwritten during automated tests.

---

## Directory Structure

```
desktop/
├── index.html                 # Desktop entry point
├── package.json               # Frontend dependencies & scripts
├── vite.config.ts             # Vite bundler configuration
├── src/                       # Frontend React 19 + TypeScript application
│   ├── App.tsx                # Main application orchestrator
│   ├── App.css                # Polished Dark/Light styling rules
│   ├── types.ts               # Core TypeScript interfaces & contracts
│   └── components/
│       ├── ChatArea.tsx       # Message stream & scroll management
│       ├── ChatInput.tsx      # Auto-resizing prompt input & stop button
│       ├── MarkdownRenderer.tsx # High-performance Markdown parser
│       ├── ResourceDashboard.tsx # Hardware telemetry & containment gauges
│       ├── ResourceRecoveryModal.tsx # Guided memory assistant & profile selector
│       ├── SettingsModal.tsx  # Runtime paths, loopback port & preferences
│       ├── Sidebar.tsx        # Conversation search, rename, & history
│       └── TopBar.tsx         # Profile chips, status indicator & navigation
└── src-tauri/                 # Embedded Rust systems core
    ├── Cargo.toml             # Rust crate configuration & dependencies
    ├── tauri.conf.json        # Tauri v2 bundle & NSIS packaging manifest
    └── src/
        ├── admission.rs       # Six-gate hardware admission audit & model caching
        ├── chat_service.rs    # SSE streaming & per-generation cancellation
        ├── gguf.rs            # Fast binary GGUF header parser
        ├── lib.rs             # Tauri IPC command registrations & app setup
        ├── main.rs            # Native binary entry point
        ├── memory_budget.rs   # Dynamic budget calculator & runtime profiles
        ├── resource_advisor.rs # Process candidate inspection & step-by-step guidance
        ├── server_manager.rs  # Win32 Job Object process containment & lifecycle
        ├── state.rs           # Shared application state
        ├── storage.rs         # Local settings, conversation, & model cache persistence
        ├── system_info.rs     # Win32 memory, commit, and GPU query utilities
        └── tests.rs           # Automated test suite (105 unit, lifecycle, and security regression tests)
```

---

## Development & Building

### Prerequisites
- Node.js 18+ (LTS recommended)
- Rust 1.77+ with `x86_64-pc-windows-msvc` toolchain
- Visual Studio 2022 C++ Build Tools

### Running in Development
```powershell
cd desktop
npm install
npm run tauri dev
```

### Running Backend Tests
```powershell
cd desktop
cargo test --manifest-path src-tauri/Cargo.toml
```

### Packaging Production Binary & Installer
```powershell
cd desktop
npm run tauri build
```
Build outputs:
- **Standalone Binary:** `desktop/src-tauri/target/release/nova-chat.exe`
- **Windows NSIS Installer:** `desktop/src-tauri/target/release/bundle/nsis/NOVA Chat_0.1.0_x64-setup.exe`

---

## Safety & Governance Protocol

1. **Model Loading Boundary:** NOVA Chat requires explicit user confirmation before spawning `llama-server.exe` with model weights.
2. **Process Safety:** No user background applications are terminated automatically.
3. **Loopback Isolation:** The inference backend strictly binds to `127.0.0.1`, rejecting external network connections.
