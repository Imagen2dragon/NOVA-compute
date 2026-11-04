# NOVA Compute — Phase 3A: Memory-Hierarchy Architecture
## Four-Tier Memory Coordination for Qwen3-30B-A3B Q4_K_M on Windows Laptop

**Date:** 2026-11-05  
**Document:** `reports/phase3/MEMORY_HIERARCHY_ARCHITECTURE.md`  
**Status:** Design and Static Analysis Only (Phase 3A)  
**Target Hardware:** HP Victus Gaming Laptop 15-fa1xxx  
- CPU: Intel Core i7-13700H (14 cores / 20 threads: 6P + 8E, AVX2, AVX2-VNNI)  
- RAM: 16.0 GB physical installed (15.65 GB visible to OS, 2× 8 GB DDR4-3200 Dual-Channel, ~35–42 GB/s real bandwidth; available physical RAM is dynamic and was measured at 8.66–9.91 GiB during preflight)  
- GPU: NVIDIA GeForce RTX 4050 Laptop GPU (6,141 MiB GDDR6, ~192 GB/s, AD107; hardware interface is PCIe Gen4 x8 capable, observed as Gen1 x8 in idle power saving, dynamically scaling to Gen4 x8 under load)  
- Storage: WD PC SN810 1 TB NVMe SSD (PCIe Gen4 x4, NTFS, C: drive with >100 GiB free space)  
- OS: Microsoft Windows 11 Home (Build 26200)  
**Target Model:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` (18,632,184,480 bytes / ~17.35 GiB; 30.53B total parameters, 3.35B active parameters per token, 48 layers, 128 experts per layer, 8 active experts per token)  
**Runtime:** `llama.cpp` Windows x64 CUDA 12.4 build `b12032` (commit `dde879bc4`)  
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
- **Usable Budget:** Total dedicated VRAM is 6,141 MiB. Pre-run free VRAM on clean desktop idle is ~5,920 MiB (Windows DWM consumes ~200–350 MiB). The project enforces a mandatory safety reserve floor of 512 MiB, leaving an operational envelope of ~5,408 MiB. Headroom cannot be declared safe until CUDA context initialization overhead (~150–250 MiB) and compute scratchpad memory (~200–450 MiB) are empirically measured.
- **Residency:**
  - Dense model weights: embedding (`tok_embd.weight`), output head (`output.weight`), pre/post norms, and self-attention projections (`attn_q`, `attn_k`, `attn_v`, `attn_output`) across all 48 layers ($1,022.5\text{ MiB}$ in Q4_K_M/Q6_K [Derived — GGUF tensor inventory]; actual runtime VRAM residency and allocation overhead remain UNKNOWN until measured).
  - Dense MoE router gates (`ffn_gate_inp.weight`) across 48 layers ($50.3\text{ MiB}$, included within the dense total).
  - Key-Value (KV) Cache: At context length 512 with `q8_0` quantization (`-ctk q8_0 -ctv q8_0`), precisely **25.50 MiB** ($26,738,688\text{ bytes}$) across 48 layers (accounting for GGML 34-byte block overhead per 32 values). At ctx 1024: 51.00 MiB; at ctx 2048: 102.00 MiB; at ctx 4096: 204.00 MiB.
  - CUDA compute and activation scratch buffers: ~200–450 MiB [Estimated, to be measured].
  - Selected GPU MoE expert layers: If using `-ncmoe 40` (keeping 40 layers of experts on CPU), the remaining 8 layers of MoE experts (layers 40–47) reside in VRAM ($3,081,240,576\text{ bytes} = 2,938.50\text{ MiB} \approx 2.87\text{ GiB}$; mean $367.31\text{ MiB/layer}$).
  - Optional GPU Expert Cache: Dynamic expert cache via `--moe-cache-mib` (runtime-managed internal cache; eviction heuristics are internal to the runtime and must not be assumed to be strictly LRU without binary-level verification). A 1,024 MiB allocation must NOT be configured prematurely, as it risks breaching the 512 MiB safety reserve floor.
- **Operating Constraints:**
  - VRAM allocations via `cudaMalloc` are **locked physical GPU pages**.
  - Dedicated GPU memory allocated via `cudaMalloc` must remain within available physical VRAM. Exceeding available capacity triggers a CUDA Out-of-Memory (`cudaErrorMemoryAllocation`) failure or invokes OS/driver fallback memory eviction to host RAM, causing severe execution latency. Operating with a strict safety reserve ($\ge 512\text{ MiB}$) is required to prevent allocation failures.

### 2.2 Tier 2: Host System Physical RAM
- **Physical Hardware:** 16.0 GB total physical memory (16,802,582,528 bytes; 15.65 GB visible to OS; ~358 MB hardware reserved) across 2× 8 GB Micron DDR4-3200 SODIMMs operating in Dual-Channel mode.
- **Peak Bandwidth:** Theoretical peak of $51.2\text{ GB/s}$; real-world sustained dual-channel memory copy bandwidth is measured at **~35 to 42 GB/s**.
- **Latency:** ~50 to 80 nanoseconds (DDR4 CAS latency + memory controller overhead).
- **Usable Budget:**
  - Installed physical RAM is 16.0 GB (15.65 GB visible to OS; ~358 MB hardware reserved).
  - Clean Windows 11 desktop baseline (background browsers, IDEs, and external services closed) consumes **~3.5 to 4.0 GB**, theoretically yielding up to ~11.5–12.0 GB under clean laboratory conditions.
  - However, available physical RAM fluctuates dynamically with desktop tasks and background services. In actual project preflight tests, available RAM was measured between **8.66 GiB and 9.91 GiB**. Therefore, available RAM cannot be assumed as a fixed 11.8 GB and must be verified immediately prior to any run.
- **Residency:**
  - `llama.cpp` process private commit (heap, thread stacks, tokenizer, model graph metadata): ~350–450 MB [Estimated].
  - Active CPU MoE expert working sets: During token generation, only the 8 active experts per CPU layer are activated. For 40 CPU layers (totaling $13,801.5\text{ MiB}$), this corresponds to $\frac{8}{128} \times 13,801.5\text{ MiB} \approx 862.6\text{ MiB}$ ($904,495,104\text{ bytes}$). Note that this $862.6\text{ MiB}$ figure represents the **logical selected expert slice payload touched per forward pass under equal routing**, NOT a guaranteed physical RAM residency requirement or locked allocation. Without memory locking (`mlock`), pages are paged in on demand and may be discarded by the OS under memory pressure.
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
- **Random Read Performance:** Single-threaded, queue-depth-1 (QD1) 4K-64K random read latency is **~20 to 100 microseconds** (~15K–25K IOPS at QD1 single-thread, yielding ~60–100 MB/s, compared to ~750K IOPS peak at QD32 under multi-threaded synthetic workloads).
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

### 3.1 Runtime Memory Management and Option Mechanics

To prevent architectural confusion, `llama.cpp` CLI options and memory mechanisms must be clearly distinguished:
- `-ngl N` (`--n-gpu-layers`): Sets the maximum number of model layers whose dense components (attention, RMSNorms, routers) are stored in and computed by GPU VRAM.
- `-ncmoe N` (`--no-cache-moe` / CPU MoE offload count): Forces the MoE feed-forward expert weights of the first $N$ layers to remain in CPU memory space. **Crucially, `-ncmoe` does NOT pin physical RAM.** It merely directs the runtime to keep those layers mapped in host virtual memory rather than copying them to GPU VRAM.
- `--moe-cache-mib M`: Allocates a dedicated dynamic buffer of $M$ MiB in GPU VRAM to cache CPU-resident MoE experts during inference. When a token activates an expert that is already in this GPU cache, it computes on CUDA cores without host-to-device PCIe transfer. If missing, it is copied over PCIe. *Eviction behavior is internal to the runtime and must not be assumed to follow strict LRU without runtime source verification.* (Note: Supported in `llama-cli.exe` and `llama-server.exe`, but not in `llama-bench.exe`).
- `--mlock`: Calls Win32 `VirtualLock()` to lock mapped pages into physical RAM, preventing the OS from paging them to disk. *On this 16 GB laptop, `--mlock` on a 17.35 GiB model fails or causes system-wide OOM, because total physical RAM is smaller than the model.*
- `-lm mmap` / `-lm dio` / `-lm none`: Configures model file loading mode (`mmap` uses Win32 `MapViewOfFile`; `dio` uses unbuffered direct I/O; `none` loads via buffered read).
- `-lzm auto` / `off` / `on`: Configures lazy loading. In `b12032`, `auto` only applies lazy allocation to tensors $>4\text{ GiB}$. It does NOT provide fine-grained MoE expert streaming.

1. **Dense Weight Placement (`-ngl`):**
   - At startup, `llama.cpp` determines which layers are offloaded to GPU based on `-ngl 48`.
   - Offloaded dense tensors (attention projections, RMSNorms, gating routers, embeddings) are transferred from host mmap staging buffers over PCIe Gen4 x8 into locked VRAM allocations (`cudaMalloc`).
2. **MoE Expert Partitioning (`-ncmoe`):**
   - The `-ncmoe N` flag instructs `llama.cpp` to retain the MoE expert weights of the first $N$ layers on the CPU.
   - For layers $N$ to 47, the expert tensors are loaded directly into GPU VRAM allocations.
   - For layers 0 to $N-1$, expert tensors remain in CPU memory-mapped virtual address space.
3. **GPU Expert Caching (`--moe-cache-mib`):**
   - When `--moe-cache-mib` is enabled, `llama.cpp` maintains a dynamic cache buffer in GPU VRAM.
   - During the forward pass of a CPU-resident MoE layer, the router selects 8 active experts.
   - **Cache Hit:** If the selected expert slice already resides in the GPU expert cache, computation executes directly on the 2,048 CUDA cores at ~192 GB/s with zero PCIe transfer.
   - **Cache Miss:** The missing expert slice is copied from system RAM over PCIe Gen4 x8 into the GPU expert cache, evicting previous entries under the runtime's internal cache replacement heuristic.
4. **KV Cache Allocation (`-c`, `-ctk`, `-ctv`):**
   - The KV cache is allocated as a contiguous CUDA buffer in VRAM sized strictly to the context window:
     $$\text{KV Bytes} = \text{Context} \times 48\text{ layers} \times 4\text{ KV heads} \times 128\text{ head dim} \times 2\text{ (K+V)} \times \left(\frac{34\text{ bytes}}{32\text{ values}}\right)$$
   - At context length 512 with `q8_0` quantization (`-ctk q8_0 -ctv q8_0`), precisely **25.50 MiB** ($26,738,688\text{ bytes}$). At 1024: 51.00 MiB; at 2048: 102.00 MiB; at 4096: 204.00 MiB.

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
   - Unified Memory provides a single pointer to data that can be accessed by both CPU and GPU code.
   - On platforms that support hardware page faulting and concurrent access (such as Linux systems with native NVIDIA kernel drivers and `concurrentManagedAccess = 1`), the CUDA driver and GPU memory management unit can dynamically migrate individual pages between host RAM and GPU memory on demand, enabling fine-grained demand paging and memory oversubscription.
   - **Documented Windows Unified Memory Limitations:** On Windows platforms and devices where the `concurrentManagedAccess` device property is 0, Unified Memory operates in a documented limited mode:
     * **No Fine-Grained Page-Fault Migration:** Fine-grained GPU page-fault migration is not supported in this mode. The GPU MMU does not fault on demand during kernel execution to fetch missing pages across the PCIe bus.
     * **Pre-Kernel Bulk Transfers:** Managed data generally must be transferred to GPU memory when a kernel launches (or prior to execution), and synchronized back upon completion.
     * **No GPU Memory Oversubscription:** GPU-memory oversubscription is not supported in this mode. The cumulative size of all managed allocations, combined with other active device memory allocations, cannot exceed the physical capacity of dedicated GPU VRAM.
     * **CPU/GPU Access and Synchronization Restrictions:** Concurrent access to managed allocations between CPU and GPU is subject to additional restrictions. Host CPU access to managed memory while a GPU kernel is executing on that memory is disallowed or results in access violations unless explicit synchronization is performed.
     * **Platform and Device Specificity:** These constraints are determined by the underlying operating system driver model and hardware device properties, specifically `cudaDeviceProp::concurrentManagedAccess`. They must not be generalized to every WDDM device or CUDA configuration (e.g., enterprise hardware in TCC mode or specific specialized drivers).
3. **Practical Implication for NOVA:**
   - On Windows with `llama.cpp b12032`, memory tiering is **explicitly managed**:
     * Dense weights and GPU expert layers reside in dedicated VRAM allocations allocated via `cudaMalloc`.
     * CPU expert layers reside in host virtual memory backed by Win32 `mmap` (`MapViewOfFile`).
     * Active expert slices move between CPU RAM and GPU VRAM strictly via explicit PCIe staging or the dynamic `--moe-cache-mib` GPU buffer.
     * Unified Memory cannot serve as an automatic, transparent oversubscription mechanism on this target configuration.

---

## 5. Architectural Summary: The Four-Tier Coordination Strategy

To achieve safe and reproducible execution on the target hardware:

1. **Tier 1 (GPU VRAM):** Offload all dense layers (attention, norms, routers, embeddings, output head: $1,022.5\text{ MiB}$ [Derived — GGUF tensor inventory]), the entire KV cache (Q8_0, **25.50 MiB** at ctx 512), and 8 layers of MoE experts (layers 40–47: $2,938.50\text{ MiB}$ via `-ncmoe 40`), consuming $3,986.5\text{ MiB}$. Out of ~5,920 MiB clean free VRAM, this leaves ~1,933.5 MiB gross margin, comfortably covering the 512 MiB mandatory safety floor, CUDA context (~150–250 MiB), and compute scratchpad (~200–450 MiB), yielding ~720–1,070 MiB of estimated net safety headroom.
2. **Tier 2 (System RAM):** Host the remaining 40 layers of MoE expert weights ($13,801.50\text{ MiB}$) in memory-mapped virtual address space, ensuring that the logical active expert working set touched per token (~862.6 MiB) operates within measured available physical RAM (measured at 8.66–9.91 GiB during preflight; must be verified dynamically at runtime).
3. **Tier 3 (Windows File Cache):** Allow the OS Cache Manager to maintain unaccessed expert pages in the clean Standby list, knowing that Windows can discard them instantly without write overhead if memory pressure occurs.
4. **Tier 4 (NVMe SSD):** Serve as the static read-only backing store for the 17.35 GiB GGUF file. Zero reliance is placed on `pagefile.sys` for dynamic tensor swapping; anonymous private commit is kept strictly under ~400 MB.
