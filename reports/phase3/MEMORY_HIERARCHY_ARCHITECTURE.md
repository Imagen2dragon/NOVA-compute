# NOVA Compute — Phase 3A: Memory-Hierarchy Architecture
## Four-Tier Memory Coordination for Qwen3-30B-A3B Q4_K_M on Windows Laptop

**Date:** 2026-11-05  
**Document:** `reports/phase3/MEMORY_HIERARCHY_ARCHITECTURE.md`  
**Status:** Design and Static Analysis Only (Phase 3A)  
**Target Hardware:** HP Victus Gaming Laptop 15-fa1xxx  
- CPU: Intel Core i7-13700H (14 cores / 20 threads: 6P + 8E, AVX2, AVX2-VNNI)  
- RAM: 16.0 GB physical (15.65 GB visible, 2× 8 GB DDR4-3200 Dual-Channel, ~35–42 GB/s real bandwidth)  
- GPU: NVIDIA GeForce RTX 4050 Laptop GPU (6,141 MiB GDDR6, ~192 GB/s, AD107, PCIe Gen4 x8)  
- Storage: WD PC SN810 1 TB NVMe SSD (PCIe Gen4 x4, NTFS, C: drive with >100 GiB free space)  
- OS: Microsoft Windows 11 Home (Build 26200)  
**Target Model:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` (18,632,184,480 bytes / ~17.35 GiB; 30.53B total parameters, 3.35B active parameters per token, 48 layers, 128 experts per layer, 8 active experts per token)  
**Runtime:** `llama.cpp` Windows x64 CUDA 12.4 build `b12032`  
**Project Owner:** User  
**Lead Architect:** ChatGPT  
**Implementation Engineer:** Antigravity  

---

## 1. Executive Overview

Running a 30.5-billion-parameter MoE model (~17.35 GiB file size) on a laptop equipped with 6 GB of VRAM and 16 GB of system RAM is fundamentally a **memory-hierarchy coordination problem**. The model payload exceeds dedicated GPU VRAM by nearly $3\times$ and exceeds available system RAM under typical desktop conditions.

Feasible local execution without catastrophic system thrashing or out-of-memory (OOM) termination requires treating the machine not as a uniform memory pool, but as a **tightly coupled four-tier memory hierarchy**:

```
+--------------------------------------------------------------------------+
| Tier 1: GPU Dedicated VRAM (6,141 MiB GDDR6, ~192 GB/s, Low Latency)      |
| Resides: Dense attention & norms, KV cache, GPU compute scratch, GPU MoE  |
|          layers (if offloaded), and optional dynamic GPU expert cache.   |
+--------------------------------------------------------------------------+
                                    ▲
                                    │ PCIe Gen4 x8 (~15.75 GB/s)
                                    │ Managed by llama.cpp & CUDA Driver
                                    ▼
+--------------------------------------------------------------------------+
| Tier 2: Host System RAM (15.65 GB Visible DDR4-3200, ~35-42 GB/s Real)    |
| Resides: OS working set (~3.5-4 GB clean), llama.cpp process heap (~400M),|
|          and active CPU-resident MoE expert working sets in physical RAM. |
+--------------------------------------------------------------------------+
                                    ▲
                                    │ OS Page Frame Management
                                    │ Soft Page Faults (Re-indexing Standby)
                                    ▼
+--------------------------------------------------------------------------+
| Tier 3: Windows File Cache / Standby List (Clean File-Backed Mapped Pages)|
| Resides: Read-only memory-mapped pages of Qwen3-30B-A3B-Q4_K_M.gguf.      |
|          Clean pages dropped instantly under RAM pressure without writes. |
+--------------------------------------------------------------------------+
                                    ▲
                                    │ Hard Page Faults / Disk I/O
                                    │ NVMe 4K-64K Random Reads (20-100 us)
                                    ▼
+--------------------------------------------------------------------------+
| Tier 4: NVMe Storage Subsystem (WD PC SN810 PCIe Gen4 x4 SSD, NTFS)       |
| Resides: GGUF artifact on disk (17.35 GiB) and Windows pagefile.sys.      |
+--------------------------------------------------------------------------+
```

---

## 2. In-Depth Characterization of the Four Tiers

### 2.1 Tier 1: GPU Dedicated VRAM
- **Physical Hardware:** 6,141 MiB GDDR6 on NVIDIA AD107 (RTX 4050 Laptop).
- **Peak Bandwidth:** 96-bit bus at ~16 Gbps $\approx$ **192 GB/s** theoretical bandwidth.
- **Latency:** Sub-nanosecond to single-digit nanoseconds for register and L2 cache access; ~10–20 ns for GDDR6 fetches.
- **Usable Budget:** Under clean desktop idle with Windows Desktop Window Manager (DWM) consuming ~200–350 MiB, usable VRAM is strictly bounded to **~5,500–5,700 MiB**.
- **Residency:**
  - Dense model weights: embedding (`tok_embd.weight`), output head (`output.weight`), pre/post norms, and self-attention projections (`attn_q`, `attn_k`, `attn_v`, `attn_output`) across all 48 layers ($\approx 1,023.3\text{ MiB}$ in Q4_K_M/Q6_K).
  - Dense MoE router gates (`ffn_gate_inp.weight`) across 48 layers ($\approx 50.3\text{ MiB}$).
  - Key-Value (KV) Cache: At context length 512 with `q8_0` quantization (`-ctk q8_0 -ctv q8_0`), precisely **24.0 MiB** across 48 layers.
  - CUDA compute and activation scratch buffers: ~300–450 MiB.
  - Selected GPU MoE expert layers: If using `-ncmoe 40` (keeping 40 layers of experts on CPU), the remaining 8 layers of MoE experts reside in VRAM ($8 \times \sim 365.7\text{ MiB} \approx 2,925.6\text{ MiB}$).
  - Optional GPU Expert Cache: Dynamic Least-Recently-Used (LRU) expert cache via `--moe-cache-mib`.
- **Operating Constraints:**
  - VRAM allocations via `cudaMalloc` are **locked physical GPU pages**.
  - WDDM driver paging (evicting GPU buffers to system RAM) causes severe execution stalls ($10\times\text{--}50\times$ slowdown) and must be strictly avoided. If total VRAM demand exceeds available capacity, a CUDA Out-of-Memory (`cudaErrorMemoryAllocation`) abort occurs immediately.

### 2.2 Tier 2: Host System Physical RAM
- **Physical Hardware:** 16.0 GB total physical memory (16,802,582,528 bytes; 15.65 GB visible to OS; ~358 MB hardware reserved) across 2× 8 GB Micron DDR4-3200 SODIMMs operating in Dual-Channel mode.
- **Peak Bandwidth:** Theoretical peak of $51.2\text{ GB/s}$; real-world sustained dual-channel memory copy bandwidth is measured at **~35 to 42 GB/s**.
- **Latency:** ~50 to 80 nanoseconds (DDR4 CAS latency + memory controller overhead).
- **Usable Budget:**
  - Clean Windows 11 desktop baseline (background browsers, IDEs, and external services closed): consumes **~3.5 to 4.0 GB**.
  - Effective available physical RAM for inference: **~11.5 to 12.0 GB**.
- **Residency:**
  - `llama.cpp` process private commit (heap, thread stacks, tokenizer, model graph metadata): ~350–450 MB.
  - Active CPU MoE expert working sets: During token generation, only the 8 active experts per CPU layer need to be read. For 40 CPU layers, this corresponds to $\frac{8}{128} \times 13.48\text{ GiB} \approx 862.6\text{ MiB}$ of active weight tensors touched per forward pass.
- **Operating Constraints:**
  - Physical RAM is shared with all running operating system services and the user desktop environment.
  - If anonymous dirty memory exceeds available physical RAM, Windows is forced to page dirty frames out to `pagefile.sys`.

### 2.3 Tier 3: Windows File Cache and Memory-Mapped Pages (Clean Standby List)
- **Subsystem:** Windows NT Memory Manager, Cache Manager, and Virtual Memory System (`ntoskrnl.exe`).
- **Nature of Allocation:**
  - Model file loading with `-lm mmap` uses the Win32 `CreateFileMapping` and `MapViewOfFile` APIs.
  - The entire 17.35 GiB GGUF file is mapped into the process's 64-bit virtual address space ($128\text{ TB}$ address limit).
  - **Virtual reservation consumes zero physical RAM initially.** Memory pages enter physical RAM only when touched by CPU instructions or explicit prefetch calls.
- **Clean vs. Dirty Page Semantics:**
  - Model weight pages mapped from the GGUF file are strictly **read-only, file-backed pages**.
  - In Windows memory accounting, these pages reside in the **Standby Page List** or the process's **Resident Working Set**.
  - **Zero-Write Discardability:** When system memory pressure rises, the Windows Memory Manager can immediately drop clean file-backed pages from physical RAM without writing a single byte to disk.
  - When a dropped page is subsequently re-read, a **hard page fault** occurs, fetching the 4K–64K cluster directly from the NVMe file.
- **Windows Prefetch Behavior:**
  - During `init_mappings(true, ...)` in `llama.cpp`, the runtime issues an advisory `PrefetchVirtualMemory()` call to the OS.
  - On Windows, `PrefetchVirtualMemory` is an **asynchronous optimization hint**. The OS Cache Manager populates free standby frames with file pages up to available headroom. If physical RAM is constrained, Windows silently skips or truncates the prefetch request without raising an error.

### 2.4 Tier 4: NVMe Storage Subsystem
- **Physical Hardware:** Western Digital PC SN810 SDCPNRY-1T00-1006 1 TB NVMe SSD (PCIe Gen4 x4, BiCS5 112-layer TLC NAND).
- **Sequential Burst Throughput:** Up to ~6,600 MB/s sequential read (at queue depth 32).
- **Random Read Performance:** 4K random read latency is **~20 to 100 microseconds** (~750K IOPS peak at QD32; ~15K–25K IOPS at QD1 single-thread).
- **Residency:**
  - The static GGUF model file on NTFS: `models/qwen3-30b/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` ($18,632,184,480\text{ bytes}$).
  - Windows system pagefile: `C:\pagefile.sys` (dynamically managed by Windows, typically 15.65 GB committed).
- **Operating Constraints:**
  - NVMe throughput is orders of magnitude slower than RAM for random access patterns.
  - While reading large contiguous chunks during initial load is efficient (~800–1200 MB/s sustained from NTFS), random per-token page-ins from disk incur severe latency penalties.

---

## 3. Component Movement and Tier Transitions

Understanding how data moves between tiers is crucial for analyzing bottlenecks:

```
               [ NVMe Storage (Tier 4) ]
                          │
                          │ Hard Page Fault (4K-64K disk read, 20-100 us)
                          │ [Managed by Windows Cache Manager]
                          ▼
           [ Windows File Cache / Standby (Tier 3) ]
                          │
                          │ Soft Page Fault (Working Set Re-indexing, <1 us)
                          │ [Managed by Windows NT Memory Manager]
                          ▼
            [ System Physical RAM (Tier 2) ]
                          │
                          │ Host-to-Device Copy over PCIe Gen4 x8 (~15.75 GB/s)
                          │ [Managed by llama.cpp / CUDA Runtime API]
                          ▼
            [ GPU Dedicated VRAM (Tier 1) ]
```

### 3.1 Movements Managed by `llama.cpp` and CUDA Runtime
1. **Dense Weight Placement (`-ngl`):**
   - At startup, `llama.cpp` determines which layers are offloaded to GPU based on `-ngl 48`.
   - Offloaded dense tensors (attention projections, RMSNorms, gating routers, embeddings) are transferred from host mmap staging buffers over PCIe Gen4 x8 into locked VRAM allocations (`cudaMalloc`).
2. **MoE Expert Partitioning (`-ncmoe`):**
   - The `-ncmoe N` flag instructs `llama.cpp` to retain the MoE expert weights of the first $N$ layers on the CPU.
   - For layers $N$ to 47, the expert tensors are loaded directly into GPU VRAM.
   - For layers 0 to $N-1$, expert tensors remain mapped in CPU memory space.
3. **GPU Expert Caching (`--moe-cache-mib`):**
   - When `--moe-cache-mib` is enabled, `llama.cpp` dynamically maintains an LRU cache buffer in GPU VRAM.
   - During the forward pass of a CPU-pinned MoE layer, the router selects 8 active experts.
   - **Cache Hit:** If the selected expert slice already resides in the GPU expert cache, computation executes directly on the 2,048 CUDA cores at ~192 GB/s with zero PCIe transfer.
   - **Cache Miss:** The missing expert slice is copied from system RAM over PCIe Gen4 x8 into the GPU expert cache, evicting the least-recently-used expert.
4. **KV Cache Allocation (`-c`, `-ctk`, `-ctv`):**
   - The KV cache is allocated as a contiguous CUDA buffer in VRAM sized strictly to the context window (e.g. 512 tokens $\times$ 48 layers $\times$ 4 heads $\times$ 128 dim $\times$ 2 values $\times$ 1 byte = 24.0 MiB in `q8_0`).

### 3.2 Movements Managed by Windows Operating System
1. **Demand Page-Ins (Hard Page Faults):**
   - When the CPU executes instructions reading an address inside a memory-mapped expert tensor that is not currently present in physical RAM, the CPU hardware MMU triggers a page fault exception.
   - The Windows Memory Manager intercepts the fault, allocates a physical RAM page frame from the Free or Zero page list, issues a synchronous non-cached read to the NVMe storage driver, populates the frame, updates the process page table entry (PTE), and resumes thread execution.
2. **Working Set Trimming and Standby Transition:**
   - Under memory pressure, the Windows Memory Manager trims pages that have not been accessed recently from the process's working set and places them onto the Standby list.
   - If accessed again before being overwritten, a **soft page fault** restores the page to the working set in under 1 microsecond without disk I/O.
3. **Clean Page Discard:**
   - When another application or driver requests physical RAM and the Free list is exhausted, Windows immediately reclaims page frames from the Standby list containing clean GGUF model pages. No disk write is performed.
4. **Anonymous Page Swapping to `pagefile.sys`:**
   - If dirty process memory (heap, private commit, CUDA driver staging memory) exceeds available RAM, Windows writes those dirty pages to `C:\pagefile.sys`. This causes synchronous disk write queues and severe system latency.

---

## 4. Fundamental Architectural Clarifications

### 4.1 Why NVMe Storage is Not Equivalent to RAM
A common misconception in oversized model inference is that fast NVMe SSDs (such as PCIe Gen4 drives rated at 6,600 MB/s) can act as an extension of system RAM for real-time token generation. This is physically and architecturally incorrect for the following reasons:

1. **Latency Differential ($1,000\times$ to $2,000\times$):**
   - System RAM latency: **~50 to 80 nanoseconds**.
   - NVMe read latency: **~20 to 100 microseconds** ($20,000\text{ to }100,000\text{ ns}$).
   - Even the fastest enterprise NVMe SSD is three orders of magnitude slower to respond to an individual page read request than DDR4 RAM.
2. **Sequential Burst vs. Random 4K IOPS Gap:**
   - The manufacturer's 6,600 MB/s rating reflects deep-queue sequential transfers (e.g. queue depth 32 with 1 MB blocks).
   - In MoE token generation, 8 active experts out of 128 are dynamically selected per layer. Their access pattern across stacked tensors represents sparse, non-sequential reads.
   - Single-threaded, queue-depth-1 random read throughput on NVMe SSDs is typically **~50 to 120 MB/s** (rarely exceeding 250 MB/s), which is $300\times$ slower than the ~38 GB/s sustained bandwidth of dual-channel DDR4.
3. **Page-Fault Interrupt and Context-Switch Overhead:**
   - Accessing RAM occurs via direct hardware memory load instructions without operating system intervention.
   - Accessing a missing mmap page requires:
     * CPU page-fault trap (interrupt 14).
     * OS kernel entry into `KiPageFault`.
     * File system driver traversal (NTFS).
     * StorNVMe driver queue dispatch.
     * PCIe bus transfer.
     * SSD controller lookup and NAND flash read.
     * Interrupt handling, thread unblocking, and PTE invalidation.
   - This serialization cost stalls the worker thread completely for the entire duration of the I/O.

### 4.2 Why CUDA Unified Memory (UVM) Must Not Be Confused with Ordinary `mmap`
Another critical distinction is the difference between CUDA Unified Memory (`cudaMallocManaged`) and Win32 file mapping (`MapViewOfFile` / `mmap`):

1. **Ordinary `mmap` (What `llama.cpp` Uses on Windows):**
   - Win32 `MapViewOfFile` maps the GGUF file into the **CPU host virtual address space only**.
   - The GPU memory controller (MMU) on the RTX 4050 has **zero visibility** into these host virtual mappings.
   - The GPU cannot page-fault into a Win32 mmap address. Any tensor executed on CUDA cores must be explicitly copied across the PCIe bus into dedicated VRAM allocated via `cudaMalloc` or `cudaMemcpyAsync`.
2. **CUDA Unified Memory (`cudaMallocManaged`):**
   - UVM allocates a unified virtual address pointer accessible to both CPU and GPU.
   - On Linux systems with Hardware Memory Management (HMM), the NVIDIA driver and PCIe bus can theoretically migrate pages between host RAM and GPU VRAM on demand via GPU hardware page faults.
   - **Windows WDDM Limitation:** Under Microsoft Windows, the Windows Display Driver Model (WDDM 3.x) governs all GPU memory allocations. WDDM does **not** support fine-grained, low-latency GPU page-fault migration over PCIe in the same manner as Linux native kernel drivers. Attempting to oversubscribe VRAM via WDDM results in coarse-grained segment eviction to system RAM, inducing severe driver-level lock contention and execution freezes.
3. **Practical Implication for NOVA:**
   - On Windows with `llama.cpp b12032`, memory tiering is **explicitly managed**:
     * Dense weights and GPU expert layers reside in locked VRAM allocations.
     * CPU expert layers reside in host virtual memory backed by mmap.
     * Active expert slices move between CPU RAM and GPU VRAM strictly via explicit PCIe staging or the dynamic `--moe-cache-mib` LRU buffer.
     * No magical unified memory pool exists on this operating system.

---

## 5. Architectural Summary: The Four-Tier Coordination Strategy

To achieve safe and reproducible execution on the target hardware:

1. **Tier 1 (GPU VRAM):** Offload all dense layers (attention, norms, routers, embeddings, output head), the entire KV cache (Q8_0, 24 MiB at ctx 512), and a calibrated subset of MoE layers (e.g. 8 layers via `-ncmoe 40`), leaving at least 1.5–1.8 GB of VRAM headroom for compute scratch and preventing WDDM paging.
2. **Tier 2 (System RAM):** Host the remaining 40 layers of MoE expert weights in memory-mapped space, ensuring that the active working set touched per token (~862.6 MiB) fits comfortably within clean available physical RAM without triggering OS memory pressure.
3. **Tier 3 (Windows File Cache):** Allow the OS Cache Manager to maintain unaccessed expert pages in the clean Standby list, knowing that Windows can discard them instantly without write overhead if memory pressure occurs.
4. **Tier 4 (NVMe SSD):** Serve as the static read-only backing store for the 17.35 GiB GGUF file. Zero reliance is placed on `pagefile.sys` for dynamic tensor swapping; anonymous private commit is kept strictly under ~400 MB.
