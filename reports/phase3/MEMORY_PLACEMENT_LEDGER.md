# NOVA Compute — Phase 3A: Memory Placement Ledger
## Source-Backed Allocation Table for Qwen3-30B-A3B Q4_K_M (Corrected)

**Date:** 2026-11-05  
**Document:** `reports/phase3/MEMORY_PLACEMENT_LEDGER.md`  
**Status:** Design and Static Analysis Only (Phase 3A) — Reconciled Ledger  
**Target Model:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` (SHA256: `a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32`)  
**File Size:** 18,632,184,480 bytes (17.353 GiB / 18.632 GB)  
**Total Tensors:** 579 top-level GGUF records  
**Total Parameters:** 30,532,122,624 (~30.53B total) / 3,353,247,744 active per token (~3.35B active)  
**Target Hardware:** HP Victus Laptop (RTX 4050 6GB [6,141 MiB GDDR6], 16.0 GB physical RAM [15.65 GB visible], WD SN810 1TB NVMe)  
**Bundled Runtime:** `llama.cpp` Windows x64 CUDA 12.4 build `b12032` (commit `dde879bc4`)  

---

## 1. Classification & Measurement Label Definitions

To ensure complete empirical transparency, every quantity in this ledger is strictly categorized:
- **[Measured]:** Obtained directly from verified GGUF binary headers, Win32 API system queries, or NVML hardware queries.
- **[Derived]:** Calculated from verified closed-form mathematical equations and exact architectural tensor specifications.
- **[Estimated]:** Approximated from typical runtime characteristics reported in community baselines or similar model architectures; subject to live measurement.
- **[Unknown]:** Cannot be known or verified without spawning an active inference execution and instrumenting live memory allocators.
- **[Controlled]:** Parameter set directly by command-line flag or runtime configuration.

---

## 2. Reconciled Per-Layer MoE Expert Ledger

The Qwen3-30B-A3B Q4_K_M model employs an importance-matrix (imatrix) calibrated quantization scheme. Consequently, **expert layers are not uniform in size**. 

### 2.1 The Two Expert Layer Profiles
Across the 48 transformer blocks, expert weights are stored as 144 three-dimensional stacked tensors (3 records per layer: `ffn_gate_exps`, `ffn_up_exps`, `ffn_down_exps`):

| Profile | Layer Count | Gate Projection (`ffn_gate_exps`) | Up Projection (`ffn_up_exps`) | Down Projection (`ffn_down_exps`) | Layer Total (Bytes) | Layer Total (MiB) |
|---|---|---|---|---|---|---|
| **Profile A (Q6_K Down)** | **24 layers** | `[2048, 768, 128]` @ Q4_K (113,246,208 B) | `[2048, 768, 128]` @ Q4_K (113,246,208 B) | `[768, 2048, 128]` @ **Q6_K** (165,150,720 B) | **391,643,136 B** | **373.50 MiB** |
| **Profile B (Q4_K Down)** | **24 layers** | `[2048, 768, 128]` @ Q4_K (113,246,208 B) | `[2048, 768, 128]` @ Q4_K (113,246,208 B) | `[768, 2048, 128]` @ **Q4_K** (113,246,208 B) | **339,738,624 B** | **324.00 MiB** |
| **Total / Average** | **48 layers** | 5,435,817,984 B | 5,435,817,984 B | 6,681,526,272 B | **17,553,162,240 B** | **16,740.00 MiB (Avg: 348.75 MiB)** |

- **Profile A Layer Indices (24 layers @ 373.50 MiB):**  
  `[0, 1, 2, 3, 4, 5, 8, 11, 14, 17, 20, 23, 26, 29, 32, 35, 38, 41, 42, 43, 44, 45, 46, 47]`
- **Profile B Layer Indices (24 layers @ 324.00 MiB):**  
  `[6, 7, 9, 10, 12, 13, 15, 16, 18, 19, 21, 22, 24, 25, 27, 28, 30, 31, 33, 34, 36, 37, 39, 40]`

### 2.2 Reconciliation of Configuration D Partitioning (`-ncmoe 40`)
In Configuration D, `-ncmoe 40` directs the runtime to keep the MoE expert weights of the first 40 layers (0 to 39) in host CPU memory space, while the remaining 8 layers (40 to 47) are placed in GPU VRAM:

1. **GPU-Resident MoE Experts (Layers 40 to 47: 8 Layers):**
   - Layer 40: Profile B ($1 \times 339,738,624\text{ bytes} = 324.00\text{ MiB}$)
   - Layers 41–47: Profile A ($7 \times 391,643,136\text{ bytes} = 2,614.50\text{ MiB}$)
   - **Total GPU Expert Footprint:** **$3,081,240,576\text{ bytes} = \mathbf{2,938.50\text{ MiB}} \approx \mathbf{2.8696\text{ GiB}}$**
   - **Mean Layer Size on GPU:** **$367.31\text{ MiB per layer}$** (differs from the 48-layer average because 7 of the 8 GPU layers have Q6_K down-projections).
2. **CPU-Resident MoE Experts (Layers 0 to 39: 40 Layers):**
   - 17 Profile A layers ($17 \times 391,643,136\text{ bytes} = 6,657.93\text{ MiB}$)
   - 23 Profile B layers ($23 \times 339,738,624\text{ bytes} = 7,143.57\text{ MiB}$)
   - **Total CPU Expert Footprint:** **$14,471,921,664\text{ bytes} = \mathbf{13,801.50\text{ MiB}} \approx \mathbf{13.4780\text{ GiB}}$**
   - **Mean Layer Size on CPU:** **$345.04\text{ MiB per layer}$**.
3. **Aggregate Check:**
   $$3,081,240,576\text{ bytes (GPU)} + 14,471,921,664\text{ bytes (CPU)} = 17,553,162,240\text{ bytes} = \mathbf{16,740.00\text{ MiB}} = \mathbf{16.3477\text{ GiB}}$$
   $$\text{Overall Model Average} = \frac{16,740.00\text{ MiB}}{48\text{ layers}} = \mathbf{348.75\text{ MiB per layer}}$$

### 2.3 Clarification of the Active Expert Payload Estimate
- **Logical Active Payload per Token:**  
  $$\text{Active CPU Expert Data} = \frac{8\text{ active}}{128\text{ total}} \times 14,471,921,664\text{ bytes} = 904,495,104\text{ bytes} = \mathbf{862.59\text{ MiB}}$$
- **Operational Reality:** This $862.6\text{ MiB}$ figure represents the **mathematical slice volume** of tensor weights selected by the routing function during a single forward pass across the 40 CPU layers.
- **Not Guaranteed RAM Residency:** This is **not** a physical RAM guarantee. Because expert weights are stored as stacked 3D tensors, accessing an expert slice touches 4 KiB memory pages across the file mapping. Depending on OS page management, cache hit history, and prior token paths, the actual physical RAM occupied by CPU experts may range from $\sim 863\text{ MiB}$ up to the full $13.48\text{ GiB}$ if all pages are touched and retained in the working set.

---

## 3. Comprehensive Placement Table

| Component / Subsystem | Placement Tier | Storage (Bytes) | Size (MiB / GiB) | Status | Derivation Source | Controlling Option | Memory-Pressure & Risk Factors |
|---|---|---|---|---|---|---|---|
| **Static GGUF Model File** | Tier 4 (NVMe) | 18,632,184,480 B | 17.353 GiB (18.632 GB) | **Measured** | File stat / SHA256 audit | `-m <path>` | Read-only backing store. Requires $\ge 25.0\text{ GiB}$ free disk gate. |
| **GGUF Header & Metadata** | Tier 3 (Virtual Mapping) / RAM | 5,970,560 B | 5.694 MiB | **Measured** | GGUF header parser | `-lm mmap` | Negligible size. Parsed at startup. |
| **Virtual Address Reservation** | Process Virtual Memory | 18,632,184,480 B | 17.353 GiB | **Derived** | Win32 `MapViewOfFile` | `-lm mmap` | Virtual reservation only. Consumes 0 bytes physical RAM until touched. |
| **Token Embeddings (`tok_embd`)** | Tier 1 (GPU VRAM) | 175,030,272 B | 166.922 MiB | **Measured** | GGUF record `[2048, 151936]` Q4_K | `-ngl 48` | Offloaded to VRAM. Essential for prompt ingestion. |
| **LM Output Head (`output`)** | Tier 1 (GPU VRAM) | 255,252,480 B | 243.428 MiB | **Measured** | GGUF record `[2048, 151936]` Q6_K | `-ngl 48` | Offloaded to VRAM. Evaluated on every generated token. |
| **Output RMSNorm (`output_norm`)** | Tier 1 (GPU VRAM) | 8,192 B | 0.008 MiB | **Measured** | GGUF record `[2048]` F32 | `-ngl 48` | Negligible size. |
| **Attention Q Projections (48L)** | Tier 1 (GPU VRAM) | 226,492,416 B | 216.000 MiB | **Measured** | 48 records `[2048, 4096]` Q4_K | `-ngl 48` | Locked in VRAM across all 48 layers. |
| **Attention K Projections (48L)** | Tier 1 (GPU VRAM) | 53,477,376 B | 51.000 MiB | **Measured** | 48 records `[2048, 512]` Q8_0 | `-ngl 48` | GQA compressed (4 heads). Locked in VRAM. |
| **Attention V Projections (48L)** | Tier 1 (GPU VRAM) | 34,799,616 B | 33.188 MiB | **Measured** | 48 records `[2048, 512]` Q6_K | `-ngl 48` | GQA compressed (4 heads). Locked in VRAM. |
| **Attention Out Projections (48L)**| Tier 1 (GPU VRAM) | 276,824,064 B | 264.000 MiB | **Measured** | 48 records `[4096, 2048]` Q5_K | `-ngl 48` | Locked in VRAM across all 48 layers. |
| **Router Gate Weights (48L)** | Tier 1 (GPU VRAM) | 50,331,648 B | 48.000 MiB | **Measured** | 48 records `[2048, 128]` F32 | `-ngl 48` | Dense gating must run on GPU for every token. |
| **Layer RMSNorms (48L)** | Tier 1 (GPU VRAM) | 835,584 B | 0.797 MiB | **Measured** | 192 records across 48 layers F32 | `-ngl 48` | Negligible size. |
| **Total Dense Model Weights** | **Tier 1 (GPU VRAM)** | **1,072,217,856 B** | **1,022.545 MiB (~1.00 GiB)**| **Derived** | Sum of dense GGUF records | `-ngl 48` | Stable, fixed VRAM allocation. |
| **GPU MoE Experts (Layers 40–47)**| **Tier 1 (GPU VRAM)** | **3,081,240,576 B** | **2,938.500 MiB (~2.87 GiB)**| **Derived** | Layers 40–47 stacked records | `-ngl 48 -ncmoe 40` | Layer 40 is 324 MiB; Layers 41–47 are 373.5 MiB. |
| **CPU MoE Experts (Layers 0–39)** | **Tier 2/3 (RAM / mmap)** | **14,471,921,664 B**| **13,801.500 MiB (~13.48 GiB)**|**Derived** | Layers 0–39 stacked records | `-ncmoe 40` | Stored in file-backed virtual memory. NOT pinned in RAM. |
| **KV Cache (`ctx=512`, `q8_0`)** | Tier 1 (GPU VRAM) | 26,738,688 B | 25.500 MiB | **Derived** | $512 \times 48 \times 4 \times 128 \times 2 \times \frac{34}{32}$ | `-c 512 -ctk q8_0 -ctv q8_0`| Exact Q8_0 block formula ($34\text{ B} / 32\text{ vals}$). |
| **KV Cache (`ctx=1024`, `q8_0`)** | Tier 1 (GPU VRAM) | 53,477,376 B | 51.000 MiB | **Derived** | $1024 \times 48 \times 4 \times 128 \times 2 \times \frac{34}{32}$| `-c 1024 -ctk q8_0 -ctv q8_0`| Scales linearly with context. |
| **KV Cache (`ctx=2048`, `q8_0`)** | Tier 1 (GPU VRAM) | 106,954,752 B | 102.000 MiB | **Derived** | $2048 \times 48 \times 4 \times 128 \times 2 \times \frac{34}{32}$| `-c 2048 -ctk q8_0 -ctv q8_0`| Ample headroom in VRAM. |
| **KV Cache (`ctx=4096`, `q8_0`)** | Tier 1 (GPU VRAM) | 213,909,504 B | 204.000 MiB | **Derived** | $4096 \times 48 \times 4 \times 128 \times 2 \times \frac{34}{32}$| `-c 4096 -ctk q8_0 -ctv q8_0`| Requires verification under high context. |
| **GPU Dynamic Expert Cache** | Tier 1 (GPU VRAM) | Controlled ($0\text{ B}$ in Config D) | $0\text{ to }2,048\text{ MiB}$ | **Controlled**| CLI option | `--moe-cache-mib N` | Directly consumes VRAM headroom. Disabled in baseline. |
| **CUDA Driver Context Overhead** | Tier 1 (GPU VRAM) | ~150–250 MiB (est.) | ~150 to 250 MiB | **UNKNOWN** | Driver baseline initialization | CUDA runtime | **UNKNOWN** until measured via NVML on active process. |
| **Compute Scratch / FlashAttn** | Tier 1 (GPU VRAM) | ~200–450 MiB (est.) | ~200 to 450 MiB | **UNKNOWN** | GGML CUDA graph allocator | `-fa auto -b 64 -ub 16` | **UNKNOWN** until measured during first forward pass. |
| **llama.cpp Process Private Commit**| Tier 2 (Host System RAM) | ~350–450 MB (est.) | ~350 to 450 MB | **UNKNOWN** | C++ heap / stack / tokenizer | Runtime executable | Anonymous memory (`PrivateUsage`). Must be monitored. |
| **Desktop / DWM VRAM Overhead** | Tier 1 (GPU VRAM) | ~200–350 MiB at idle | ~200 to 350 MiB | **Measured** | NVML baseline query | Windows DWM | Baseline display consumption. Increases with external apps. |

---

## 4. Reconciled VRAM Budget (Configuration D Baseline)

The VRAM budget must not rely on an unsupported nominal ceiling. Instead, it is evaluated strictly against:
- **Physical Total VRAM:** $6,141.0\text{ MiB}$ **[Measured]**
- **Current Measured Free VRAM (Idle Desktop):** $\sim 5,920.0\text{ MiB}$ **[Measured via NVML]**
- **Mandatory Runtime Safety Reserve:** $512.0\text{ MiB}$ **[Policy Floor]**

### 4.1 Known Model & KV Allocations (Configuration D: `-ngl 48 -ncmoe 40 -c 512`)
$$\begin{aligned}
\text{Dense Model Weights (All 48 Layers)} &= 1,022.5\text{ MiB} \quad \text{\textbf{[Derived]}} \\
\text{GPU MoE Experts (Layers 40–47: 8 Layers)} &= 2,938.5\text{ MiB} \quad \text{\textbf{[Derived]}} \\
\text{KV Cache (512 Context, Q8\_0: } 34/32\text{ bytes/value)} &= 25.5\text{ MiB} \quad \text{\textbf{[Derived]}} \\
\text{GPU Expert Cache (`--moe-cache-mib 0`)} &= 0.0\text{ MiB} \quad \text{\textbf{[Controlled]}} \\
\hline
\mathbf{Subtotal\ (Fixed\ Model\ +\ KV\ Allocation)} &= \mathbf{3,986.5\text{ MiB}} \quad \text{\textbf{[Derived]}}
\end{aligned}$$

### 4.2 Headroom Calculation Against Pre-Run Free VRAM
$$\begin{aligned}
\text{Current Measured Free VRAM} &\approx 5,920.0\text{ MiB} \quad \text{\textbf{[Measured]}} \\
\text{Subtotal (Fixed Model + KV)} &= -3,986.5\text{ MiB} \quad \text{\textbf{[Derived]}} \\
\hline
\mathbf{Gross\ Remaining\ Free\ VRAM} &\approx \mathbf{1,933.5\text{ MiB}} \quad \text{\textbf{[Derived]}} \\
\text{Mandatory Runtime Safety Reserve} &= -512.0\text{ MiB} \quad \text{\textbf{[Policy Floor]}} \\
\hline
\mathbf{Unallocated\ Buffer\ Available\ for\ Dynamic\ Overheads} &\approx \mathbf{1,421.5\text{ MiB}} \quad \text{\textbf{[Derived]}}
\end{aligned}$$

### 4.3 Material Dynamic Overheads (Unknowns)
The $1,421.5\text{ MiB}$ unallocated buffer must accommodate all remaining runtime components:
- **CUDA Context Overhead:** Typically $\sim 150\text{--}250\text{ MiB}$ **[UNKNOWN]**
- **Compute Scratchpad / FlashAttention Activations:** Typically $\sim 200\text{--}450\text{ MiB}$ **[UNKNOWN]**

$$\mathbf{Estimated\ Net\ Headroom\ Above\ Reserve} \approx 1,421.5\text{ MiB} - (150\text{ to }250\text{ MiB}) - (200\text{ to }450\text{ MiB}) \approx \mathbf{721.5\text{ to }1,071.5\text{ MiB}}$$

> [!WARNING]
> **Headroom Uncertainty:** While the mathematical subtotal of $3,986.5\text{ MiB}$ leaves an estimated $\sim 720\text{--}1,070\text{ MiB}$ above the safety reserve, **this headroom cannot be declared safe until CUDA context overhead and peak activation scratchpads are empirically measured under live execution**.

### 4.4 Governance on GPU Expert Cache (`--moe-cache-mib`)
- Setting `--moe-cache-mib 1024` would consume $1,024\text{ MiB}$ of VRAM.
- Subtracting $1,024\text{ MiB}$ from the estimated net buffer ($721.5\text{--}1,071.5\text{ MiB}$) would leave **between $-302.5\text{ MiB}$ (an immediate OOM/abort violation) and $+47.5\text{ MiB}$ (dangerously tight margin)**.
- **Strict Rule:** **Do not recommend or configure a 1,024 MiB expert cache before a clean baseline smoke test measures the exact unallocated VRAM headroom.**
