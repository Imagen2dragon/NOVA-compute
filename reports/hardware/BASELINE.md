# NOVA Compute — Hardware Qualification Baseline Report (Phase 0)

**Date of Collection:** 2026-11-01  
**Target Machine:** HP Victus Gaming Laptop 15-fa1xxx  
**Status:** Completed Phase 0 Hardware Audit  
**Agent:** Antigravity (Implementation Agent)  
**Reviewer:** ChatGPT (Engineering Lead / Technical Reviewer)  

---

## 1. Executive Summary

This qualification report establishes the empirical hardware baseline for the NOVA Compute research prototype. All measurements were obtained using safe, non-modifying, read-only system queries.

### Key Hardware Inventory
- **CPU:** 13th Gen Intel Core i7-13700H (14 cores / 20 threads: 6P + 8E), AVX2/AVX2-VNNI support.
- **System RAM:** 16.0 GB total (2x 8 GB Micron DDR4-3200 SODIMMs, Dual-Channel, ~51.2 GB/s theoretical peak bandwidth).
- **GPU:** NVIDIA GeForce RTX 4050 Laptop GPU (6,141 MiB VRAM GDDR6, Ada Lovelace, Compute Capability 8.9, PCIe Gen4 x8).
- **Driver / CUDA:** NVIDIA Display Driver 592.82 (Driver CUDA API 13.1 supported). Standalone CUDA Toolkit (`nvcc`) is **not** installed in PATH.
- **Storage:** WD PC SN810 NVMe SSD 1 TB (GPT, Healthy, C: drive NTFS with 174.46 GB free space out of 952.57 GB).
- **Operating Environment:** Microsoft Windows 11 Home (Build 26200). WSL2 distributions exist (`Ubuntu`, `docker-desktop`), but WSL2 failed to launch due to `HCS_E_HYPERV_NOT_INSTALLED` ("Virtual Machine Platform" component not enabled in Windows, though CPU firmware virtualization is active).

---

## 2. Detailed Hardware Specifications

### 2.1 CPU & Architecture
| Metric | Measured Value | Verification Method |
|---|---|---|
| Processor Name | 13th Gen Intel(R) Core(TM) i7-13700H | `Win32_Processor.Name` |
| Physical Cores | 14 cores (6 Performance + 8 Efficient) | `Win32_Processor.NumberOfCores` |
| Logical Processors | 20 threads (12 P-threads + 8 E-threads) | `Win32_Processor.NumberOfLogicalProcessors` |
| Base / Max Clock | 2.40 GHz base (up to 5.0 GHz P-core Turbo per Intel spec) | `Win32_Processor.MaxClockSpeed` |
| L2 Cache | 11.5 MB (11,776 KB) | `Win32_Processor.L2CacheSize` |
| L3 Cache | 24.0 MB (24,576 KB) | `Win32_Processor.L3CacheSize` |
| Instruction Sets | x86-64-v3: SSE4.2, AVX, AVX2, FMA3, F16C, BMI1, BMI2, Intel DL Boost (AVX2-VNNI). Note: AVX-512 is fused off on consumer Raptor Lake hybrid CPUs. | Hardware architecture / CPUID spec |
| Virtualization | `VirtualizationFirmwareEnabled: True`, SLAT: `True`, VM Monitor Extensions: `True` | `Win32_Processor` query |

### 2.2 System Memory (RAM)
| Metric | Measured Value | Verification Method |
|---|---|---|
| Total Physical Memory | 16.0 GB installed (16,802,582,528 bytes; 15.65 GB visible to OS, ~358 MB hardware reserved) | `Win32_ComputerSystem.TotalPhysicalMemory` |
| Module 1 | 8 GB Micron Technology (`4ATF1G64HZ-3G2F1`), DDR4-3200, 3200 MT/s | `Win32_PhysicalMemory` |
| Module 2 | 8 GB Micron Technology (`4ATF1G64HZ-3G2F1`), DDR4-3200, 3200 MT/s | `Win32_PhysicalMemory` |
| Memory Topology | 2 physical SODIMM slots populated; Dual-Channel DDR4-3200 | Physical layout confirmed |
| Theoretical Peak Bandwidth | ~51.2 GB/s (2 channels × 8 bytes × 3.2 GT/s) | Mathematical calculation |
| Real-World Bandwidth (est.) | ~35 to 42 GB/s sustained memory copy | Standard DDR4 dual-channel estimate |
| Current Physical Memory In-Use | **~13.44 GB** in use by background desktop processes (SQL Server, Edge, Telegram, Claude, Defender, DWM) | `Win32_OperatingSystem.FreePhysicalMemory` |
| Free Physical Memory (Measured) | **~2.21 GB** (2,318,028 KB) free at time of collection | `Win32_OperatingSystem.FreePhysicalMemory` |
| Allocated Pagefile | 15.65 GB allocated on C: drive (`SizeStoredInPagingFiles = 16,408,772 KB`) | `Win32_OperatingSystem` |
| Total Virtual Memory | 31.29 GB (13.68 GB free virtual memory) | `Win32_OperatingSystem` |

> [!WARNING]
> At current idle/desktop state, only ~2.2 GB of physical RAM is free. For local model execution, non-essential background processes (e.g. `sqlservr.exe`, browser instances, communication clients) must be closed to recover ~9-11 GB of physical memory budget.

### 2.3 GPU & Graphics Subsystem
| Metric | Measured Value | Verification Method |
|---|---|---|
| GPU Model | NVIDIA GeForce RTX 4050 Laptop GPU | `nvidia-smi` |
| Architecture / Silicon | Ada Lovelace (AD107) | Device specification |
| Compute Capability | 8.9 (4th Gen Tensor Cores, FP8 execution support) | `nvidia-smi --query-gpu=compute_cap` |
| Dedicated VRAM | 6,141 MiB (~6.0 GB) GDDR6 | `nvidia-smi` |
| VRAM Used (Idle) | 208 MiB (display attached, running DWM and background UI) | `nvidia-smi` |
| Usable VRAM Budget | **~5,700 MiB (~5.57 GB)** available for model weights & KV cache | `nvidia-smi` |
| Bus Interface | PCIe Gen4 x8 (Current: Gen1 x8 at idle P8; Max: Gen4 x8) | `nvidia-smi --query-gpu=pcie...` |
| Peak PCIe Bandwidth | ~15.75 GB/s unidirectional theoretical bandwidth (PCIe Gen4 x8) | PCIe specification |
| Memory Bus Width / Bandwidth | 96-bit GDDR6 @ ~16 Gbps = ~192 GB/s theoretical VRAM bandwidth | Hardware specification |
| TGP (Power Cap) | 69W–74W maximum TGP (currently idling at 3W–5W in P8) | `nvidia-smi` |
| Thermals (Idle) | 46°C – 50°C | `nvidia-smi` |
| Driver Version | 592.82 (WDDM 3.x driver model) | `nvidia-smi` |
| Driver CUDA Support | CUDA 13.1 API supported via `nvcuda.dll` | `nvidia-smi` |
| Standalone CUDA Toolkit (`nvcc`)| **Not installed** in PATH; `CUDA` directory not present in Program Files | `Get-Command nvcc` |

### 2.4 Storage & NVMe Subsystem
| Metric | Measured Value | Verification Method |
|---|---|---|
| Model Name | WD PC SN810 SDCPNRY-1T00-1006 | `Get-PhysicalDisk.FriendlyName` |
| Bus / Interface | NVMe (Western Digital OEM PCIe Gen4 x4 client SSD, BiCS5 TLC NAND) | `Get-PhysicalDisk.BusType` |
| Total Drive Capacity | 1,024,209,543,168 bytes (~1,024.2 GB / 953.87 GiB raw) | `Get-Disk.TotalSize` |
| Partition Style | GPT (System EFI: 260 MB, MSR: 16 MB, Basic C: 952.57 GB, Recovery: 1.03 GB) | `Get-Partition` |
| Volume C: Format | NTFS | `Get-Volume` |
| Volume C: Total Size | 952.57 GB (1,022,810,386,432 bytes) | `Get-Volume.Size` |
| Volume C: Free Space | **174.46 GB** (187,420,327,936 bytes; 18.3% free) | `Get-Volume.SizeRemaining` |
| Health Status | Healthy | `Get-PhysicalDisk.HealthStatus` |
| SMART / Wear Counters | Inaccessible without administrator elevation (`PermissionDenied`) | `Get-StorageReliabilityCounter` |
| Manufacturer Rated Read/Write | Up to 6,600 MB/s sequential read / 5,000 MB/s sequential write (nominal PCIe 4.0 x4) | WD PC SN810 Specification Sheet |

### 2.5 OS & Execution Environments
| Environment | Measured Status | Notes / Limitations |
|---|---|---|
| Host OS | Microsoft Windows 11 Home (10.0.26200) | Fully operational host environment. |
| Python Environment | Python 3.12.10 (`C:\Users\abdal\AppData\Local\Programs\Python\Python312\python.exe`) | Installed and verified in user space. |
| Developer Tools | Git 2.48.1.1 present. `cmake`, `cl.exe`, `gcc`, `nvcc` not in PATH. | Precompiled binaries preferred for early phases. |
| WSL2 Status | Installed (`Ubuntu` and `docker-desktop` registered). | **Execution Blocked:** Fails on launch with `HCS_E_HYPERV_NOT_INSTALLED`. Firmware virtualization is enabled, but the Windows optional feature "Virtual Machine Platform" is disabled. Modifying this requires admin/reboot and was not touched per Phase 0 rules. |
| Linux Environment | Inaccessible via WSL until user enables Virtual Machine Platform. | `scripts/linux/collect-hardware.sh` could not be executed directly. |

---

## 3. Audit of Diagnostics Scripts

### 3.1 `scripts/windows/collect-hardware.ps1`
- **Safety & Mutation:** 100% read-only. Uses standard PowerShell CIM queries (`Win32_OperatingSystem`, `Win32_Processor`, `Win32_ComputerSystem`, `Win32_PhysicalMemory`), storage queries (`Get-PhysicalDisk`, `Get-Volume`), `nvidia-smi`, and `wsl.exe`. No files, services, registry keys, or hardware parameters are modified.
- **Privacy Review:** Outputs hardware descriptors and volume drive letters. Does not inspect personal directories, documents, credentials, browser cookies, or network logs.
- **Execution Result:** Completed successfully in ~5 seconds. Raw capture archived at [`reports/hardware/windows-inventory-20261101T141500.txt`](file:///c:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/reports/hardware/windows-inventory-20261101T141500.txt).

### 3.2 `scripts/linux/collect-hardware.sh`
- **Safety & Mutation:** 100% read-only. Gathers metadata via `lscpu`, `free -h`, `lsblk`, `df -hT`, `nvidia-smi`, `nvcc`, `cgroup`, and `/proc/version`.
- **Privacy Review:** `uname -a` displays machine hostname; no personal data or files are scanned.
- **Execution Result:** Inaccessible due to WSL2 hypervisor initialization failure. Attempt and error output archived at [`reports/hardware/wsl-diagnostic-20261101T141500.txt`](file:///c:/Users/abdal/OneDrive/Desktop/nova_compute_antigravity_starter/reports/hardware/wsl-diagnostic-20261101T141500.txt).

---

## 4. Feasibility Evaluation: Qwen3-30B-A3B Inference

### 4.1 Model Characteristics
- **Total Parameters:** ~30.5 Billion (all expert weights must reside in some tier: VRAM, RAM, or SSD).
- **Active Parameters:** ~3.3 Billion per token (~10.8% active experts per layer).
- **Estimated Weight Footprint:**
  - **Q4_K_M (~4.5 bpw):** ~17.5 GB – 18.5 GB.
  - **Q3_K_M (~3.5 bpw):** ~14.0 GB – 15.0 GB.
  - **Q2_K (~2.8 bpw):** ~11.5 GB – 12.5 GB.
- **KV Cache Footprint (FP16, 2K context, 1 req):** ~0.4 GB – 0.8 GB.

### 4.2 Resource Bottleneck Analysis

#### Memory Capacity Ceiling
1. **GPU VRAM (6 GB total / ~5.6 GB usable):**
   - Can fit at most ~25%–30% of model layers (e.g., attention weights and router layers, or ~1.5 GB of KV cache + 4 GB of weights).
   - Remaining 10–13 GB of model weights must reside in system RAM or be paged from SSD.
2. **System RAM (16 GB total / ~15.65 GB visible):**
   - Baseline OS footprint + essential background services require ~3.5 GB to 4.5 GB of RAM even after cleaning user apps.
   - Available physical RAM budget for inference runtime: **~11.0 GB to 12.0 GB maximum**.
   - **Combined VRAM + free RAM budget:** ~5.6 GB VRAM + ~11.5 GB RAM = **~17.1 GB total physical memory budget**.
   - Under **Q4_K_M** (~18 GB weights + ~1 GB runtime/KV): Exceeds available physical memory. Will force heavy OS pagefile thrashing on Windows.
   - Under **Q3_K_M** (~14.5 GB weights + ~1 GB runtime/KV): Leaves less than 1.5 GB of headroom. Feasible only if background apps are aggressively minimized.
   - Under current dirty desktop state (~2.2 GB free RAM): Immediate OOM or extreme disk paging.

#### Bandwidth & Latency Limits vs. The 18–20 Tokens/s Objective
- To decode at **18 tokens/second** with **3.3B active parameters** (~1.8 GB active weights in Q4_K_M, or ~1.4 GB in Q3_K_M):
  $$\text{Required Active Weight Bandwidth} \approx 1.4\text{ GB/token} \times 18\text{ tokens/s} = \mathbf{25.2\text{ GB/s}}$$
  (or $\approx 32.4\text{ GB/s}$ for Q4_K_M).
- **Hardware bandwidth ceiling comparison:**
  - **GPU VRAM:** ~192 GB/s theoretical (plenty of bandwidth, but only 6 GB capacity).
  - **Dual-Channel DDR4-3200:** ~51.2 GB/s theoretical peak; ~35–40 GB/s practical achievable.
  - **PCIe Gen4 x8 Host-to-Device (CPU $\leftrightarrow$ GPU):** ~15.75 GB/s theoretical maximum.
  - **NVMe SSD (WD PC SN810):** ~6.6 GB/s sequential burst; random 4K–64K page-in random bandwidth drops to ~100–400 MB/s.

#### Latency Implications
1. **Tier 1 (Weights in VRAM + System RAM):**
   - If active experts are in System RAM and dispatched to GPU over PCIe Gen4 x8, transferring 1.4 GB of active weights per token over PCIe takes at least:
     $$1.4\text{ GB} / 15.75\text{ GB/s} \approx 89\text{ ms} \implies \mathbf{\le 11.2\text{ tokens/s}}$$
     even assuming zero GPU compute time and 100% PCIe saturation.
   - If CPU handles the expert MoE computation directly in RAM (CPU offload via llama.cpp): System RAM bandwidth (~38 GB/s real) limits 1.4 GB streaming to:
     $$1.4\text{ GB} / 38\text{ GB/s} \approx 37\text{ ms} \implies \mathbf{\le 15–20\text{ tokens/s theoretical upper limit}}$$
     before adding CPU compute overhead, thread synchronization, and cache misses across 14 cores (6P + 8E).
2. **Tier 2 (Weights cold-faulted from NVMe SSD via mmap or InferSSD):**
   - Because MoE routing varies dynamically per token, non-cached expert weights faulted from NVMe at ~200–500 MB/s would take 1–3 seconds per token:
     $$\mathbf{0.3\text{ to }1.5\text{ tokens/second}}$$
3. **Conclusion on 18–20 tok/s Goal:**
   - 18–20 tokens/s is **not achievable** if active expert weights must be faulted cold from SSD.
   - It is an **extreme stretch** even with weights in RAM due to PCIe transfer and dual-channel DDR4 bandwidth limits.
   - The primary generalist test is **technically feasible as a functional and memory-tier experiment** under Q3_K_M with low context (2K), but decode speed will likely fall in the 4–10 tokens/s range when warm in RAM, and <1 token/s when cold-paging from SSD.

---

## 5. Summary of Knowns vs. Assumptions

| Attribute | Classification | Source / Proof |
|---|---|---|
| CPU Model & Core Count (14C / 20T) | **Measured Fact** | Windows CIM query (`13th Gen Intel Core i7-13700H`) |
| RAM Modules & Dual-Channel Config | **Measured Fact** | Windows CIM query (2x 8 GB Micron DDR4-3200 SODIMMs) |
| GPU Model & Dedicated VRAM (6,141 MiB) | **Measured Fact** | `nvidia-smi` query |
| GPU PCIe Gen & Width (Gen4 x8) | **Measured Fact** | `nvidia-smi` query |
| Driver Version (592.82) & CUDA API (13.1)| **Measured Fact** | `nvidia-smi` query |
| Standalone CUDA SDK (`nvcc`) missing | **Measured Fact** | PATH scan & Program Files filesystem check |
| NVMe SSD Model & Health (SN810, Healthy)| **Measured Fact** | Windows Storage CIM query |
| SSD Free Space (174.46 GB) | **Measured Fact** | `Get-Volume` query |
| WSL2 Inaccessible (`HCS_E_HYPERV_NOT_INSTALLED`)| **Measured Fact** | `wsl.exe` invocation test |
| CPU Virtualization in Firmware (`True`)| **Measured Fact** | `Win32_Processor.VirtualizationFirmwareEnabled` |
| NVMe Sequential Read/Write Specs | **Manufacturer Specification** | WD PC SN810 datasheet (not empirically benchmarked) |
| DDR4 Real-World Sustained Bandwidth | **Engineering Assumption** | ~35–42 GB/s typical for dual-channel DDR4-3200 |
| Qwen3-30B-A3B Exact GGUF Quant Sizes | **Estimate** | Calculated from standard k-quants (~14.5 GB for Q3_K_M) |
| Expected Decode Speed of 30B MoE | **Estimate / Projection** | Bandwidth-limited calculation (4–10 tok/s warm; <1 tok/s cold) |

---

## 6. Unknowns & Inaccessible Data

1. **NVMe SMART Wear & Temperature Telemetry:**
   - `Get-StorageReliabilityCounter` was blocked with `PermissionDenied` (requires administrator privilege). The exact drive temperature and percentage life remaining cannot be read without elevation.
2. **WSL2 / Linux Kernel Environment:**
   - Could not query Linux kernel version, cgroup v2 status, or Linux `nvidia-smi` pass-through because WSL2 hypervisor component is not enabled in Windows.
3. **Hardware NVMe Sustained Random-Read Bandwidth:**
   - SSD performance is currently known from manufacturer specifications, not active benchmark I/O.
4. **Current InferSSD Upstream Compatibility with Qwen3 Architecture:**
   - InferSSD v0.1.0 documentation specifically validated DeepSeek-V4.1-Flash-1017 and Muse-Spark-Open; support for Qwen3 MoE tensor structures is unverified.

---

## 7. Recommended Next Steps (Phase 1 Proposal)

### Proposed Immediate Milestone: Phase 1 — Small-Model Runtime Baseline

To comply with the project governance rules, we should **not** jump directly to Qwen3-30B-A3B. We must first validate the CUDA runtime, verify GPU offloading, and calibrate our telemetry harness using a small dense model.

#### Recommended Small-Model Candidates
- **Candidate A:** `Qwen2.5-1.5B-Instruct` (Q4_K_M, ~1.1 GB file size).
- **Candidate B:** `Qwen2.5-3B-Instruct` (Q4_K_M, ~2.0 GB file size).
- *(Note: When official Qwen3-1.7B GGUF weights become accessible, it will serve as the Phase 4 specialist base model).*

#### Resource & Environment Impact for Phase 1
- **Disk Impact:** ~1.1 GB to 2.0 GB download (fits comfortably within the 174.46 GB free space; consumes <1.2% of free disk).
- **RAM Impact:** ~1.5 GB system RAM overhead.
- **VRAM Impact:** ~1.5 GB to 2.5 GB dedicated VRAM (fits 100% inside the 5.6 GB usable VRAM, allowing pure GPU execution to measure peak CUDA performance and establish an upper-bound baseline).
- **Environment Strategy:**
  - Option 1 (Recommended for zero-mutation): Use a pre-compiled Windows release of `llama.cpp` with cuBLAS/CUDA 12/13 support (runs directly against the installed NVIDIA display driver `nvcuda.dll` without requiring MSVC or CUDA toolkit installation).
  - Option 2: Ask the user if they wish to enable Windows "Virtual Machine Platform" (requires admin elevation and reboot) to unlock WSL2 if Linux is preferred as the primary development environment.

### Stop Point
**Phase 0 is complete.** In accordance with `AGENTS.md` and `ANTIGRAVITY_FIRST_TASK.md`, all actions have ceased. Awaiting technical review and architectural guidance from ChatGPT (engineering lead) and approval from the user before proceeding to Phase 1.
