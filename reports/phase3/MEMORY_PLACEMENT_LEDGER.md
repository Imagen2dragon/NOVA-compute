# NOVA Compute — Phase 3A: Memory Placement Ledger
## Source-Backed Allocation Table for Qwen3-30B-A3B Q4_K_M

**Date:** 2026-11-05  
**Document:** `reports/phase3/MEMORY_PLACEMENT_LEDGER.md`  
**Status:** Design and Static Analysis Only (Phase 3A)  
**Target Model:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` (SHA256 verified: `a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32`)  
**File Size:** 18,632,184,480 bytes (17.353 GiB / 18.632 GB)  
**Tensors:** 579 top-level GGUF records  
**Parameters:** 30,532,122,624 total (~30.53B) / 3,353,247,744 active per token (~3.35B)  
**Hardware Baseline:** HP Victus Laptop (RTX 4050 6GB VRAM [6,141 MiB GDDR6], 16GB RAM [15.65 GB visible], WD SN810 1TB NVMe)  

---

## 1. Source-Backed Placement Table

The following ledger accounts for every architectural component of the model, runtime buffers, and operating system structures. 

> [!IMPORTANT]
> Values marked **Measured** originate directly from byte-exact GGUF headers, verified binary help output, or hardware qualification diagnostics.  
> Values marked **Calculated** are derived from closed-form mathematical formulae (e.g. tensor dimensions $\times$ quantization bit-rates or GQA head dimensions).  
> Values that cannot be known prior to executing an active runtime process are explicitly marked **UNKNOWN**.

| Component / Subsystem | Target Placement Tier | Size in Bytes | Size (MiB / GiB) | Derivation Source | Measurement Status | Controlling Runtime Option | Memory-Pressure & Allocation Risks |
|---|---|---|---|---|---|---|---|
| **GGUF File on Disk** | Tier 4 (NVMe Storage) | 18,632,184,480 bytes | 17.353 GiB (18.632 GB) | NTFS filesystem stat / SHA256 audit | **Measured** | `-m <path>` | Consumes 17.35 GiB disk space. Free disk gate requires $\ge 25.0\text{ GiB}$. |
| **GGUF Header & Metadata** | Tier 3 (Virtual Mapping) / Tier 2 (Host RAM) | 5,970,560 bytes | 5.694 MiB | GGUF binary header parse (`inspect_gguf_metadata.py`) | **Measured** | `-lm mmap` | Negligible footprint; read once during model initialization. |
| **Virtual Address Reservation** | Process Virtual Address Space | 18,632,184,480 bytes | 17.353 GiB | Win32 `MapViewOfFile` specification | **Calculated** | `-lm mmap` | Virtual address space reservation only; consumes 0 bytes physical RAM until touched. |
| **Embedding Weights (`tok_embd.weight`)** | Tier 1 (GPU VRAM) | 175,030,272 bytes | 166.922 MiB | GGUF record `[2048, 151936]`, type `Q4_K` | **Measured** | `-ngl 48` | Offloaded to VRAM. If offload fails, falls back to CPU RAM. |
| **LM Output Head (`output.weight`)** | Tier 1 (GPU VRAM) | 255,252,480 bytes | 243.428 MiB | GGUF record `[2048, 151936]`, type `Q6_K` | **Measured** | `-ngl 48` | Offloaded to VRAM. Requires un-embedding projection on every generated token. |
| **Output RMSNorm (`output_norm.weight`)** | Tier 1 (GPU VRAM) | 8,192 bytes | 0.008 MiB | GGUF record `[2048]`, type `F32` | **Measured** | `-ngl 48` | Negligible size. |
| **Attention Q Projections (`attn_q.weight`, 48 layers)** | Tier 1 (GPU VRAM) | 226,492,416 bytes | 216.000 MiB | 48 records `[2048, 4096]`, type `Q4_K` ($48 \times 4.72\text{ MB}$) | **Measured** | `-ngl 48` | Permanent resident in VRAM for all 48 layers. |
| **Attention K Projections (`attn_k.weight`, 48 layers)** | Tier 1 (GPU VRAM) | 53,477,376 bytes | 51.000 MiB | 48 records `[2048, 512]`, type `Q8_0` ($48 \times 1.11\text{ MB}$) | **Measured** | `-ngl 48` | GQA compressed (4 heads); permanent VRAM residency. |
| **Attention V Projections (`attn_v.weight`, 48 layers)** | Tier 1 (GPU VRAM) | 34,799,616 bytes | 33.188 MiB | 48 records `[2048, 512]`, type `Q6_K` ($48 \times 0.72\text{ MB}$) | **Measured** | `-ngl 48` | GQA compressed (4 heads); permanent VRAM residency. |
| **Attention Output Projections (`attn_output.weight`, 48 layers)** | Tier 1 (GPU VRAM) | 276,824,064 bytes | 264.000 MiB | 48 records `[4096, 2048]`, type `Q5_K` ($48 \times 5.77\text{ MB}$) | **Measured** | `-ngl 48` | Permanent resident in VRAM for all 48 layers. |
| **MoE Router Gate Weights (`ffn_gate_inp.weight`, 48 layers)** | Tier 1 (GPU VRAM) | 50,331,648 bytes | 48.000 MiB | 48 records `[2048, 128]`, type `F32` ($48 \times 1.05\text{ MB}$) | **Measured** | `-ngl 48` | Routers must reside on GPU to evaluate expert selection for every token. |
| **Layer RMSNorms (`attn_norm`, `ffn_norm`, `attn_q_norm`, `attn_k_norm`)** | Tier 1 (GPU VRAM) | 835,584 bytes | 0.797 MiB | 192 records across 48 layers, type `F32` | **Measured** | `-ngl 48` | Negligible size; permanently locked in VRAM. |
| **Dense Weights Total (GPU-Resident)** | **Tier 1 (GPU VRAM)** | **1,072,217,856 bytes** | **1,022.545 MiB (~1.00 GiB)** | Sum of embeddings, output head, attention, routers, and norms | **Measured** | `-ngl 48` | Safe VRAM footprint (~1.00 GiB out of 5.70 GiB usable VRAM). |
| **MoE Expert Stacked Weights (All 48 Layers)** | Split between Tier 1 (VRAM) & Tier 2/3 (RAM/File Cache) | 17,553,162,240 bytes | 16.348 GiB (17.553 GB) | 144 stacked records ($48 \times 3$): gate (`Q4_K`), up (`Q4_K`), down (`Q6_K`) | **Measured** | `-ncmoe N`, `-ngl 48` | Exceeds VRAM capacity ($2.7\times$). Must be partitioned across GPU and CPU. |
| **MoE Expert Weights Per Layer** | — | 391,643,136 bytes | 373.500 MiB (~365.7 MB) | Sum of gate ($113.25\text{M}$), up ($113.25\text{M}$), down ($165.15\text{M}$) | **Measured** | Architecture constant | Each layer offloaded to GPU consumes ~373.5 MiB VRAM. |
| **MoE GPU Layers (Config D: 8 Layers, 40 to 47)** | Tier 1 (GPU VRAM) | 3,133,145,088 bytes | 2,988.000 MiB (~2.92 GiB) | $8\text{ layers} \times 391,643,136\text{ bytes}$ | **Calculated** | `-ngl 48 -ncmoe 40` | Locked in VRAM. Combined with dense weights = ~3.91 GiB VRAM. |
| **MoE CPU Layers (Config D: 40 Layers, 0 to 39)** | Tier 2 (System RAM) / Tier 3 (File Cache) | 14,420,017,152 bytes | 13.430 GiB (14.420 GB) | $40\text{ layers} \times 391,643,136\text{ bytes}$ | **Calculated** | `-ncmoe 40` | If 100% resident in RAM, exceeds clean RAM (11.8 GB). Relies on dynamic demand-paging. |
| **Active CPU Expert Working Set Per Token (Config D)** | Tier 2 (Host Physical RAM) | 901,251,072 bytes | 859.500 MiB (~901.3 MB) | $40\text{ layers} \times \frac{8\text{ active}}{128\text{ total}} \times 391,643,136\text{ bytes}$ | **Calculated** | Runtime execution | Comfortably fits inside available physical RAM (~11.8 GB). |
| **KV Cache (`ctx = 512`, `q8_0`)** | Tier 1 (GPU VRAM) | 25,165,824 bytes | 24.000 MiB | $512 \times 48 \times 4 \times 128 \times 2 \times 1\text{ byte}$ | **Calculated** | `-c 512 -ctk q8_0 -ctv q8_0` | Extremely small footprint due to GQA ($H_{kv}=4$) and Q8_0 quantization. |
| **KV Cache (`ctx = 1024`, `q8_0`)** | Tier 1 (GPU VRAM) | 50,331,648 bytes | 48.000 MiB | $1024 \times 48 \times 4 \times 128 \times 2 \times 1\text{ byte}$ | **Calculated** | `-c 1024 -ctk q8_0 -ctv q8_0` | Fits easily in VRAM headroom. |
| **KV Cache (`ctx = 2048`, `f16`)** | Tier 1 (GPU VRAM) | 201,326,592 bytes | 192.000 MiB | $2048 \times 48 \times 4 \times 128 \times 2 \times 2\text{ bytes}$ | **Calculated** | `-c 2048 -ctk f16 -ctv f16` | Moderate footprint; leaves ample VRAM headroom. |
| **GPU Expert Cache (`--moe-cache-mib`)** | Tier 1 (GPU VRAM) | Configurable ($0$ in Config D; up to $2\text{--}3\text{ GiB}$) | $0\text{ to }3,072\text{ MiB}$ | CLI runtime parameter | **Controlled** | `--moe-cache-mib N` | Directly reduces VRAM headroom. Must not exceed $2,000\text{ MiB}$ under `-ncmoe 40`. |
| **CUDA Compute & Graph Scratch Buffers** | Tier 1 (GPU VRAM) | Estimated 300–500 MiB | ~300 to 500 MiB | Dynamic runtime allocation | **UNKNOWN** *(Measured only after init)* | `-fa auto`, `-b 64`, `-ub 16` | Exact allocation depends on batch size and FlashAttention kernel selection. |
| **llama.cpp Host Process Heap & Stack** | Tier 2 (System RAM Private Commit) | Estimated 350–450 MB | ~350 to 450 MB | Dynamic C++ runtime heap allocation | **UNKNOWN** *(Measured only after init)* | Runtime executable | Anonymous private commit (`PrivateUsage`). Must be monitored via `GetProcessMemoryInfo`. |
| **Windows Desktop & Driver VRAM Overhead** | Tier 1 (GPU VRAM) | ~200–350 MiB at idle | ~200 to 350 MiB | `nvidia-smi` idle baseline | **Measured** | OS display driver | Increases if external monitors, browser hardware acceleration, or 3D apps are active. |
| **Windows OS Clean Working Set** | Tier 2 (System RAM) | ~3,500–4,000 MB | ~3.4 to 3.9 GB | Win32 `GlobalMemoryStatusEx` clean baseline | **Measured** | Windows Services | Degrades if user applications (Edge, IDEs, SQL Server) are left open. |

---

## 2. Configuration D Total Allocation Rollup

Configuration D represents the baseline configuration authorized for preliminary safety validation:
- Flags: `-ngl 48 -ncmoe 40 -c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap -n 1 --simple-io --perf -p Hello`

### 2.1 GPU VRAM Allocation Rollup (Tier 1)
$$\begin{aligned}
\text{Dense Model Weights} &= 1,022.5\text{ MiB} \\
\text{8 MoE Layers on GPU (Layers 40–47)} &= 2,988.0\text{ MiB} \\
\text{KV Cache (512 ctx, Q8\_0)} &= 24.0\text{ MiB} \\
\text{GPU Expert Cache} &= 0.0\text{ MiB (Disabled)} \\
\text{Compute Scratch Buffers (Estimated)} &\approx 350.0\text{ MiB} \\
\hline
\mathbf{Total\ Model\ VRAM\ Requirement} &\approx \mathbf{4,384.5\text{ MiB}}~(\mathbf{\sim 4.28\text{ GiB}}) \\
\text{Usable VRAM Capacity} &\approx 5,700.0\text{ MiB} \\
\mathbf{Expected\ VRAM\ Headroom} &\approx \mathbf{+1,315.5\text{ MiB}}~(\mathbf{\sim 1.28\text{ GiB}})
\end{aligned}$$

**VRAM Verdict:** **Feasible and Safe.** The estimated allocation of ~4.28 GiB operates at ~77% of usable VRAM, maintaining an ample ~1.3 GiB safety buffer against activation spikes and preventing WDDM driver eviction.

### 2.2 System RAM Allocation Rollup (Tier 2 & Tier 3)
$$\begin{aligned}
\text{Clean OS Baseline Working Set} &\approx 3,800.0\text{ MB} \\
\text{llama.cpp Private Process Commit} &\approx 400.0\text{ MB} \\
\text{Active MoE Expert Working Set (8 active per token)} &\approx 901.3\text{ MB} \\
\hline
\mathbf{Minimum\ Required\ Physical\ RAM\ for\ Steady\ Generation} &\approx \mathbf{5,101.3\text{ MB}}~(\mathbf{\sim 5.1\text{ GB}}) \\
\text{Total Visible Physical RAM} &\approx 16,024.0\text{ MB}~(\mathbf{15.65\text{ GB}}) \\
\mathbf{Physical\ RAM\ Safety\ Margin\ for\ Active\ Working\ Set} &\approx \mathbf{+10,922.7\text{ MB}}~(\mathbf{\sim 10.7\text{ GB}})
\end{aligned}$$

$$\begin{aligned}
\text{Theoretical Worst-Case RAM Load (If all 40 CPU layers paged in)} &\approx 3,800\text{ MB (OS)} + 400\text{ MB (Heap)} + 14,420\text{ MB (Experts)} \\
&\approx \mathbf{18,620.0\text{ MB}}~(\mathbf{\sim 18.2\text{ GB}}) \\
\text{Physical RAM Deficit in Worst-Case Fully-Resident Scenario} &\approx \mathbf{-2,596.0\text{ MB}}~(\mathbf{\sim 2.5\text{ GB Over Physical Limit}})
\end{aligned}$$

**System RAM Verdict:**  
- **Steady-State Generation:** Fully feasible provided unaccessed expert pages are not forced into physical RAM. The active working set per token generation step requires under 1.0 GB of expert weights.
- **Worst-Case Vulnerability:** If sequential generation across long contexts touches all 128 experts across all 40 layers without page eviction, physical RAM will be exhausted, forcing Windows to discard standby pages and page-fault repeatedly from the NVMe SSD.

---

## 3. Explicit Gaps and Unknowns

The following quantities cannot be verified by static file inspection and must be empirically measured during future authorized tasks:
1. **Actual CUDA Context Initialization Overhead:** The exact memory reserved by the CUDA driver (`nvcuda.dll`) upon runtime initialization (typically 150–250 MiB on Ada Lovelace). Marked **UNKNOWN**.
2. **FlashAttention Activation Buffer Size:** The exact scratchpad memory allocated by GGML CUDA backend for FlashAttention with batch size 64 and ubatch 16. Marked **UNKNOWN**.
3. **Exact Cache Miss Penalty:** The empirical round-trip latency (in milliseconds) required to fault an un-cached expert from NVMe through Windows file cache into CPU RAM. Marked **UNKNOWN**.
4. **Thermal Throttling Thresholds:** The sustained temperature under combined CPU AVX2 and GPU CUDA compute before thermal throttling engages on the HP Victus chassis. Marked **UNKNOWN**.
