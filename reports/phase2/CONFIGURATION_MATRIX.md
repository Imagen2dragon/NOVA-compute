# NOVA Compute — Phase 2A: Configuration Matrix

**Target Model:** `Qwen/Qwen3-30B-A3B` (MoE: 48 layers, 128 experts, 8 active per token)  
**Host Hardware:** HP Victus Laptop (RTX 4050 6GB GDDR6, 16GB DDR4-3200 RAM, WD SN810 NVMe)  
**Installed Runtime:** `llama.cpp` Windows x64 CUDA 12.4 (`b12032`)  
**Role:** Implementation Engineer (Antigravity)  
**Reviewer:** Lead Architect & Technical Reviewer (ChatGPT)  

---

## 1. Overview of Execution Strategies

Because `Qwen3-30B-A3B` contains 30.5 billion parameters (13.4 GB in Q3_K_M, 16.9 GB in Q4_K_M) while the RTX 4050 GPU has only 6 GB of VRAM, running this model requires hybrid tiering across GPU VRAM, System RAM, and NVMe storage. 

Below are four viable candidate configurations designed to maximize stability, respect strict hardware safety boundaries, and leverage the exact flags verified in Task 1.

---

## 2. Detailed Configuration Profiles

### Configuration A: Pure CPU MoE + Full GPU Dense Layer Execution
- **Core Concept:** Offload 100% of the dense components (token embeddings, attention projections, RMSNorms, gating routers, output LM head) and KV cache to GPU VRAM. Force all 128 MoE expert weights across all 48 layers to reside in Host CPU RAM.
- **Exact Command Syntax:**
  ```powershell
  .\runtime\windows\llama-cpp\llama-cli.exe `
    -m .\models\qwen3-30b\Qwen3-30B-A3B-Q3_K_M.gguf `
    -ngl 48 `
    -cmoe `
    -c 2048 `
    --batch-size 512 `
    -fa auto `
    -ctk f16 `
    -ctv f16 `
    -st --simple-io --perf
  ```
- **Resource Footprint:**
  - **VRAM Required:** **~1.55 GB** (Dense weights: ~939 MB, KV cache @ 2K: 192 MB, Compute buffers: ~420 MB). Leaves ~4.15 GB VRAM headroom.
  - **System RAM Required:** **~12.45 GB** (Expert weights) + **~3.80 GB** (Clean OS baseline) = **~16.25 GB**.
- **SSD Access Characteristics:** Initial mmap read; slight paging overflow (~0.4 GB) to `pagefile.sys` unless background applications are completely stripped.
- **Primary Bottlenecks:** Host CPU compute throughput and DDR4-3200 dual-channel memory bandwidth (~38 GB/s real). For each token, the CPU must stream and compute 8 active experts (~1.4 GB data) across 48 layers.
- **Potential Throughput:** **~4.0 to 8.0 tokens/second** *(unmeasured estimate)*.
- **Failure Risks:** High probability of OS pagefile thrashing if any desktop browser, IDE, or service is left running during benchmark execution.

---

### Configuration B: CPU MoE Placement + Dynamic GPU Expert Cache
- **Core Concept:** In addition to GPU dense layers, allocate a dedicated **3,072 MB (3.0 GB) dynamic MoE expert cache** directly in GPU VRAM (`--moe-cache-mib 3072`). When an active expert is called, if cached in VRAM, it executes on CUDA cores at 192 GB/s. On cache miss, it is streamed from CPU RAM over PCIe Gen4 x8 (~15.75 GB/s).
- **Exact Command Syntax:**
  ```powershell
  .\runtime\windows\llama-cpp\llama-cli.exe `
    -m .\models\qwen3-30b\Qwen3-30B-A3B-Q3_K_M.gguf `
    -ngl 48 `
    -cmoe `
    --moe-cache-mib 3072 `
    -c 2048 `
    --batch-size 512 `
    -fa auto `
    -ctk f16 `
    -ctv f16 `
    -st --simple-io --perf
  ```
- **Resource Footprint:**
  - **VRAM Required:** **~4.65 GB** (Dense weights: ~939 MB, KV cache: 192 MB, Compute buffers: ~450 MB, Expert Cache: 3,072 MB). Total utilization: 82% of usable VRAM (Safe ~1.05 GB margin).
  - **System RAM Required:** **~12.45 GB** (mmap backing) + **~3.80 GB** (Clean OS) = **~16.25 GB**.
- **Expert Cache Capacity:**
  - Single expert size in Q3_K_M: **~2.08 MB**.
  - Cache size: 3,072 MB $\implies$ **holds ~1,476 expert instances** (~24% of all 6,144 expert instances, or ~30 experts per layer).
- **SSD Access Characteristics:** Initial mmap population; subsequent disk I/O occurs only if host RAM pages out un-cached expert slices.
- **Primary Bottlenecks:** Cache hit rate during generation and PCIe Gen4 x8 transfer latency on cache misses.
- **Potential Throughput:**
  - **High Cache Hit (Domain Warm Cache):** **~12.0 to 20.0 tokens/second** *(unmeasured estimate)*.
  - **Low Cache Hit (Cold / Diverse Prompt):** **~4.0 to 7.0 tokens/second** *(unmeasured estimate)*.
- **Failure Risks:** `--moe-cache-mib` is supported in `llama-cli.exe` and `llama-server.exe`, but is **not** present in `llama-bench.exe`.

---

### Configuration C: Hybrid Layer Split (First 36 Layers CPU MoE + Last 12 Layers GPU MoE)
- **Core Concept:** Offload all 48 layers of dense attention, embeddings, and output head to GPU VRAM (`-ngl 48`). Pin the MoE expert weights of the **first 36 layers (layers 0 to 35)** to CPU host RAM via `-ncmoe 36`. The MoE expert weights of the **remaining 12 layers (layers 36 to 47)** remain offloaded to GPU VRAM.
- **Exact Command Syntax:**
  ```powershell
  .\runtime\windows\llama-cpp\llama-cli.exe `
    -m .\models\qwen3-30b\Qwen3-30B-A3B-Q3_K_M.gguf `
    -ngl 48 `
    -ncmoe 36 `
    -c 2048 `
    --batch-size 512 `
    -fa auto `
    -ctk f16 `
    -ctv f16 `
    -st --simple-io --perf
  ```
- **Resource Footprint:**
  - **VRAM Required:** **~4.65 GB** (Dense weights: ~939 MB, 12 layers of GPU experts [layers 36–47]: $12 \times 266\text{ MB} = 3.19\text{ GB}$, KV cache: 192 MB, Buffers: ~350 MB).
  - **System RAM Required:** **~9.58 GB** (36 layers of CPU experts [layers 0–35]: $36 \times 266\text{ MB}$) + **~3.80 GB** (Clean OS) = **~13.38 GB**.
- **SSD Access Characteristics:** **Zero SSD paging.** The 9.58 GB expert requirement fits 100% inside the 11.8 GB clean system RAM with ~2.2 GB of headroom!
- **Primary Bottlenecks:** Execution pipeline structure: for layers 0–35, attention runs on GPU while MoE runs on CPU; for layers 36–47, both attention and MoE run directly on GPU.
- **Potential Throughput:** **~5.5 to 10.0 tokens/second** *(unmeasured estimate)*.
- **Failure Risks:** Very low. Guarantees that neither VRAM nor RAM overflows physical boundaries.

---

### Configuration D: Conservative Baseline (Q2_K Quantization + Full CPU MoE)
- **Core Concept:** Maximum stability baseline using `Q2_K` quantization (~10.7 GB total model size) with context limited to 1,024 tokens and Q8_0 KV cache.
- **Exact Command Syntax:**
  ```powershell
  .\runtime\windows\llama-cpp\llama-cli.exe `
    -m .\models\qwen3-30b\Qwen3-30B-A3B-Q2_K.gguf `
    -ngl 48 `
    -cmoe `
    -c 1024 `
    --batch-size 512 `
    -ctk q8_0 `
    -ctv q8_0 `
    -st --simple-io --perf
  ```
- **Resource Footprint:**
  - **VRAM Required:** **~1.30 GB** (Dense weights: ~838 MB, KV cache @ 1K Q8: 48 MB, Buffers: ~400 MB).
  - **System RAM Required:** **~9.85 GB** (Expert weights) + **~3.80 GB** (Clean OS) = **~13.65 GB**.
- **SSD Access Characteristics:** Zero disk paging; fits completely inside physical memory.
- **Primary Bottlenecks:** CPU MoE matrix multiplication; lower output quality due to aggressive 2-bit quantization.
- **Potential Throughput:** **~6.0 to 11.0 tokens/second** *(unmeasured estimate)*.
- **Failure Risks:** Lowest risk of all configurations.

---

## 3. Comparison Summary Matrix

| Metric | Config A (CPU MoE Baseline) | Config B (GPU Expert Cache) | Config C (Hybrid Layer Split) | Config D (Safe Q2_K Baseline) |
|---|---|---|---|---|
| **Target Quantization** | Q3_K_M (~13.4 GB) | Q3_K_M (~13.4 GB) | Q3_K_M (~13.4 GB) | Q2_K (~10.7 GB) |
| **VRAM Allocated** | ~1.55 GB (27%) | **~4.65 GB (82%)** | **~4.65 GB (82%)** | ~1.30 GB (23%) |
| **RAM Allocated (Model)** | ~12.45 GB | ~12.45 GB | **~9.58 GB** | **~9.85 GB** |
| **Total System RAM Load** | ~16.25 GB (Tight) | ~16.25 GB (Tight) | **~13.38 GB (Safe)** | **~13.65 GB (Safe)** |
| **NVMe Paging Risk** | Moderate (~0.5 GB) | Moderate (~0.5 GB) | **Zero Paging** | **Zero Paging** |
| **GPU Utilization** | Low (~15–25%) | Medium-High (~60–85%) | Medium (~50–65%) | Low (~15–20%) |
| **Expected Throughput** | ~4–8 tok/s *(est)* | **~12–20 tok/s (warm)* | ~5.5–10 tok/s *(est)* | ~6–11 tok/s *(est)* |
| **Tool Support** | `llama-cli` & `bench` | `llama-cli` only | `llama-cli` & `bench` | `llama-cli` & `bench` |
| **Recommended Use** | Secondary Reference | Primary Stretch Target | Recommended Workhorse | Safe Initial Baseline |

*(All throughput numbers are engineering estimates and labeled as unmeasured until empirical testing in Phase 2B).*
