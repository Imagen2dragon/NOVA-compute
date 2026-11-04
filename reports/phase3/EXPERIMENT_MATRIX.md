# NOVA Compute — Phase 3A: Experiment Matrix
## Controlled Experimental Strategy for Qwen3-30B-A3B Q4_K_M

**Date:** 2026-11-05  
**Document:** `reports/phase3/EXPERIMENT_MATRIX.md`  
**Status:** Design and Static Analysis Only (Phase 3A) — **No experiments executed**  
**Target Hardware:** HP Victus Laptop (RTX 4050 6GB GDDR6, 16GB DDR4-3200 RAM, WD SN810 NVMe)  
**Target Model:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` ($18.63\text{ GB}$)  
**Installed Runtime:** `llama.cpp` Windows x64 CUDA 12.4 (`b12032`)  
**Project Owner:** User  
**Lead Architect:** ChatGPT  
**Implementation Engineer:** Antigravity  

---

## 1. Experimental Methodology & Governance

The goal of Phase 3 experimentation is to establish empirical ground truth regarding memory hierarchy performance, resource contention, and throughput trade-offs on the target laptop.

### Core Testing Rules:
1. **Single-Variable Isolation:** In each experiment, exactly one architectural parameter (the independent variable) is altered. All other flags, background process states, and prompt parameters are held strictly constant.
2. **Safe Preflight Precondition:** No experiment may be launched unless all six Phase 2D safety gates pass.
3. **Supervisor Telemetry:** Every test run must be supervised with continuous 500ms sampling of RAM, VRAM, system commit, and process memory.
4. **Distinction Between Smoke Tests and Benchmarks:**
   - **Smoke Test:** Generates 1 token (`-n 1`). Purpose: Verifies allocation stability, absence of CUDA OOM, and clean process teardown.
   - **Throughput Benchmark:** Generates 128 tokens (`-n 128`) across 3 repeated trials using a standardized prompt. Distinguishes Cold Cache (trial 1) from Warm Cache (trials 2 & 3).
5. **No Speculative Performance Claims:** All throughput targets (including the 18–20 tokens/second stretch objective) are treated as empirical hypotheses to test, not promised outcomes.

---

## 2. Six Controlled Experimental Designs

---

### Experiment 1: CPU MoE Layer Partitioning (`-ncmoe`)

- **Objective:** Determine the optimal balance between GPU VRAM offload and host CPU memory bandwidth.
- **Hypothesis:** Offloading the maximum safe number of MoE expert layers to GPU VRAM (e.g. 8 to 10 layers; `-ncmoe 40` to `38`) will maximize decode throughput by reducing the volume of expert weights streamed across the CPU memory bus, while maintaining at least 1.0 GiB of VRAM headroom to prevent driver thrashing.
- **Independent Variable:** `-ncmoe` (Number of MoE layers kept on CPU):
  - Variant 1.1: `-ncmoe 48` (100% CPU MoE — 0 layers on GPU)
  - Variant 1.2: `-ncmoe 44` (4 layers on GPU, 44 on CPU)
  - Variant 1.3: `-ncmoe 42` (6 layers on GPU, 42 on CPU)
  - Variant 1.4: `-ncmoe 40` (8 layers on GPU, 40 on CPU — Baseline)
  - Variant 1.5: `-ncmoe 38` (10 layers on GPU, 38 on CPU — Aggressive VRAM)
- **Controlled Variables:**
  - Model: `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
  - `-ngl 48` (all dense attention layers on GPU)
  - `-c 512` (context length)
  - `-b 64 -ub 16` (batch sizes)
  - `-ctk q8_0 -ctv q8_0` (KV cache quantization)
  - `--moe-cache-mib 0` (GPU expert cache disabled)
  - `-fa auto -lm mmap`
  - Fixed prompt and output length: 128 tokens (`-n 128`).
- **Expected Trade-Offs:**
  - Decreasing `-ncmoe` increases GPU VRAM consumption by ~373.5 MiB per layer.
  - Decreasing `-ncmoe` decreases CPU memory bus traffic by ~22.6 MiB per token per layer ($8/128 \times 365.7\text{ MB}$).
  - Risk: Lowering `-ncmoe` below 38 may trigger CUDA Out-of-Memory or driver segment paging as VRAM headroom falls below 800 MiB.
- **Required Measurements:**
  - Peak and steady-state GPU VRAM usage (MiB).
  - VRAM headroom remaining (MiB).
  - Peak process Working Set and Private Commit (MB).
  - Available physical RAM throughout run (GiB).
  - Decode speed (tokens/second) and Prompt Processing speed (tokens/second).
- **Admission & Stop Conditions:**
  - Preflight admission: RAM $\ge 10.0\text{ GiB}$, Free VRAM $\ge 5,500\text{ MiB}$.
  - Abort during run: If VRAM free drops below 512 MiB or RAM free drops below 2.0 GiB for $>5.0\text{s}$.
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If `-ncmoe 40` or `38` achieves higher decode tokens/second than `-ncmoe 48` with zero CUDA OOM errors and VRAM headroom remains $>800\text{ MiB}$.
  - **Rejected:** If decode speed does not scale with GPU layer count (indicating the CPU remains the complete gating bottleneck) or if memory fragmentation induces CUDA OOM.

---

### Experiment 2: Dynamic GPU Expert Cache Sizing (`--moe-cache-mib`)

- **Objective:** Evaluate whether caching recently selected MoE experts in GPU VRAM improves decode throughput on domain-specific prompts.
- **Hypothesis:** Allocating a 1,024 to 1,536 MiB dynamic LRU expert cache in GPU VRAM (`--moe-cache-mib`) will substantially increase decode tokens/second during multi-token generation on structured prompts by achieving a high expert cache hit rate, reducing PCIe transfer overhead.
- **Independent Variable:** `--moe-cache-mib` (GPU cache size in MiB):
  - Variant 2.1: `--moe-cache-mib 0` (Disabled — Baseline)
  - Variant 2.2: `--moe-cache-mib 512` (~193 expert instances cached)
  - Variant 2.3: `--moe-cache-mib 1024` (~386 expert instances cached)
  - Variant 2.4: `--moe-cache-mib 1536` (~580 expert instances cached)
  - Variant 2.5: `--moe-cache-mib 2048` (~773 expert instances cached — High VRAM pressure)
- **Controlled Variables:**
  - Model: `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
  - `-ngl 48`
  - `-ncmoe 40` (40 layers on CPU, 8 layers on GPU)
  - `-c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap`
  - Fixed prompt and output: 128 tokens (`-n 128`).
  - Executed across 3 consecutive trials to measure Cold Cache vs. Warm Cache behavior.
- **Expected Trade-Offs:**
  - Higher cache size consumes VRAM that would otherwise serve as a safety buffer.
  - In Q4_K_M, a single expert slice (gate + up + down) is $\approx 2.65\text{ MB}$. A 1,024 MiB cache holds ~386 expert instances (~7.5% of all CPU experts).
  - Cold-cache generation will incur PCIe copy latency on misses; warm-cache generation should approach GPU-native tensor core compute speed.
- **Required Measurements:**
  - Cold-cache decode speed (Trial 1 t/s) vs. Warm-cache decode speed (Trial 2 & 3 t/s).
  - GPU VRAM consumption before, during, and after generation.
  - PCIe throughput and bus utilization (via `nvidia-smi dmon`).
  - System commit and RAM stability.
- **Admission & Stop Conditions:**
  - Preflight admission: RAM $\ge 10.0\text{ GiB}$, Free VRAM $\ge 5,500\text{ MiB}$.
  - Abort if free VRAM drops below 512 MiB.
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If `--moe-cache-mib 1024` or `1536` yields a $>30\%$ increase in warm-cache decode tokens/second over the baseline (`0 MiB`) without exceeding 5,500 MiB total VRAM.
  - **Rejected:** If warm-cache decode speed shows $<10\%$ improvement or if PCIe transfer overhead negates the caching advantage.

---

### Experiment 3: Dense Layer GPU Offload Boundary (`-ngl`)

- **Objective:** Quantify the performance penalty of failing to offload 100% of the dense attention layers to GPU VRAM.
- **Hypothesis:** Full offload of all 48 dense attention layers (`-ngl 48`) is mandatory for acceptable performance; retaining even a few dense attention layers on CPU will severely degrade prompt processing throughput due to CPU GEMM limitations.
- **Independent Variable:** `-ngl` (Number of offloaded layers):
  - Variant 3.1: `-ngl 48` (All 48 attention layers on GPU — Baseline)
  - Variant 3.2: `-ngl 40` (8 attention layers on CPU, 40 on GPU)
  - Variant 3.3: `-ngl 32` (16 attention layers on CPU, 32 on GPU)
- **Controlled Variables:**
  - Model: `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
  - `-cmoe` (All MoE experts on CPU, isolating the dense attention effect)
  - `-c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap`
  - Standard prompt: 64 input tokens, 32 output tokens.
- **Expected Trade-Offs:**
  - Reducing `-ngl` saves VRAM (~21.3 MiB per attention layer) but shifts heavy self-attention matrix multiplications ($W_q, W_k, W_v, W_o$) to the CPU AVX2 execution units.
- **Required Measurements:**
  - Prompt processing throughput (PP tokens/second).
  - Time to first token (TTFT in milliseconds).
  - CPU utilization (%) and GPU utilization (%).
- **Admission & Stop Conditions:**
  - Standard preflight safety gates.
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If `-ngl 40` drops prompt processing speed by $>40\%$ relative to `-ngl 48`, confirming that dense attention must remain 100% GPU-resident.
  - **Rejected:** If CPU attention computation maintains parity with GPU attention.

---

### Experiment 4: Context Length Scaling (`-c`)

- **Objective:** Evaluate the memory footprint, KV cache growth, and throughput impact of expanding the context window from 512 to 2,048 tokens.
- **Hypothesis:** Because Qwen3-30B-A3B utilizes Grouped Query Attention with only 4 KV heads ($H_{kv}=4$), expanding context length from 512 to 2,048 tokens will consume less than 170 MiB of additional VRAM in `q8_0`, having negligible impact on memory headroom while preserving decode throughput.
- **Independent Variable:** `-c` (Context window length):
  - Variant 4.1: `-c 512` (Baseline — 24.0 MiB KV cache in Q8_0)
  - Variant 4.2: `-c 1024` (48.0 MiB KV cache in Q8_0)
  - Variant 4.3: `-c 2048` (96.0 MiB KV cache in Q8_0)
  - Variant 4.4: `-c 4096` (192.0 MiB KV cache in Q8_0 — Stress test)
- **Controlled Variables:**
  - Model: `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
  - `-ngl 48 -ncmoe 40 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap`
  - Fixed generation: 64 output tokens.
- **Expected Trade-Offs:**
  - Negligible VRAM increase per token due to GQA compression.
  - Attention computation time increases quadratically ($O(N^2)$), partially mitigated by FlashAttention.
- **Required Measurements:**
  - Exact VRAM delta between `-c 512` and `-c 2048`.
  - Prompt processing speed at prompt lengths 256, 512, 1024.
  - Decode speed stability across context lengths.
- **Admission & Stop Conditions:**
  - Standard preflight safety gates. Abort if VRAM free drops below 512 MiB.
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If measured VRAM usage increases by $<100\text{ MiB}$ between 512 and 2,048 context and decode tokens/second remains within 10% of baseline.
  - **Rejected:** If context expansion triggers unforeseen compute buffer blowup exceeding 500 MiB VRAM.

---

### Experiment 5: Batch and Micro-Batch Sizing (`-b` and `-ub`)

- **Objective:** Identify the optimal logical batch size (`-b`) and physical micro-batch size (`-ub`) that maximizes prompt processing throughput without exceeding VRAM activation limits.
- **Hypothesis:** Increasing physical micro-batch size from 16 to 64 will improve prompt processing throughput on the GPU Tensor Cores by increasing arithmetic intensity, with a modest increase in activation scratch buffer size (<250 MiB).
- **Independent Variable:** Batch configuration (`-b` and `-ub`):
  - Variant 5.1: `-b 64 -ub 16` (Conservative Baseline)
  - Variant 5.2: `-b 128 -ub 32` (Balanced)
  - Variant 5.3: `-b 256 -ub 64` (High Throughput)
  - Variant 5.4: `-b 512 -ub 128` (Aggressive Activation Load)
- **Controlled Variables:**
  - Model: `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
  - `-ngl 48 -ncmoe 40 -c 512 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap`
  - Fixed prompt length: 256 tokens. Output: 16 tokens.
- **Expected Trade-Offs:**
  - Larger micro-batch sizes increase matrix multiplication efficiency on Ada Lovelace Tensor Cores.
  - Larger micro-batch sizes increase the memory allocated for intermediate activation tensors during prompt evaluation.
- **Required Measurements:**
  - Prompt processing throughput (PP tokens/second).
  - Peak VRAM allocation during the prompt processing phase.
  - VRAM delta between idle and peak prompt processing.
- **Admission & Stop Conditions:**
  - Preflight admission: Free VRAM $\ge 5,500\text{ MiB}$. Abort if VRAM free drops below 512 MiB.
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If `-b 256 -ub 64` achieves $>25\%$ higher PP tokens/second than `-b 64 -ub 16` while peak VRAM remains under 4,800 MiB.
  - **Rejected:** If increasing `-ub` triggers CUDA OOM or provides negligible (<5%) throughput benefit.

---

### Experiment 6: Memory-Mapping and Lazy-Loading Configuration (`-lm` and `-lzm`)

- **Objective:** Verify operating system memory pressure and hard page-fault behavior under different memory mapping modes.
- **Hypothesis:** Using `-lm mmap` with default lazy evaluation (`-lzm auto`) is the only viable configuration on a 16 GB host; attempting to disable mmap (`-lm none`, forcing explicit private memory allocation) will cause immediate process failure due to RAM exhaustion.
- **Independent Variable:** Memory loading mode:
  - Variant 6.1: `-lm mmap -lzm auto` (Standard OS file-backed mapping — Baseline)
  - Variant 6.2: `-lm mmap -lzm off` (Forced full residency of all tensors)
  - Variant 6.3: `-lm none` (Non-mmap heap allocation — Negative control / theoretical failure test)
- **Controlled Variables:**
  - Model: `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
  - `-ngl 48 -ncmoe 40 -c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0`
  - Smoke test execution (`-n 1`).
- **Expected Trade-Offs:**
  - `-lm none` requires allocating the entire CPU-resident model portion (~13.48 GB) as private committed memory, instantly exhausting physical RAM and triggering pagefile thrashing.
  - `-lm mmap` allows the OS to demand-page only active expert slices (~862.6 MiB).
- **Required Measurements:**
  - Process Private Commit (`PrivateUsage` in MB).
  - Process Working Set (`WorkingSetSize` in MB).
  - Hard page fault counts during startup and generation.
  - Time required for model initialization (seconds).
- **Admission & Stop Conditions:**
  - For Variant 6.3 (`-lm none`): Must be run under strict supervisor abort limits (kill process immediately if commit exceeds 85% or available RAM drops below 2.0 GiB).
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If `-lm mmap -lzm auto` maintains private commit below 500 MB while `-lm none` fails admission or triggers immediate memory constraint aborts.
  - **Rejected:** If non-mmap allocation somehow succeeds without paging (impossible given physical RAM limits).

---

## 3. Recommended Sequencing of Experiments

If and when Phase 3B is formally authorized by the Project Owner and Lead Architect, the experiments must be executed in strict sequence:

1. **Step 1: Configuration D Baseline Smoke Test (`-n 1`)** — Verifies zero regression in memory budget.
2. **Step 2: Experiment 1 (`-ncmoe` Layer Partitioning)** — Determines the optimal stable baseline for GPU vs. CPU layers.
3. **Step 3: Experiment 2 (`--moe-cache-mib` GPU Expert Caching)** — Evaluates the dynamic expert cache on top of the best `-ncmoe` configuration.
4. **Step 4: Experiment 4 (`-c` Context Scaling)** — Verifies context expansion up to 2,048 tokens.
5. **Step 5: Experiment 5 (`-b` / `-ub` Batch Optimization)** — Tunes prompt processing throughput.
6. **Step 6: Experiment 3 and 6** — Run secondary verification controls.
