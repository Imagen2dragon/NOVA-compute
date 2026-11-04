# NOVA Compute — Phase 3A: Experiment Matrix
## Controlled Experimental Strategy for Qwen3-30B-A3B Q4_K_M

**Date:** 2026-11-05  
**Document:** `reports/phase3/EXPERIMENT_MATRIX.md`  
**Status:** Design and Static Analysis Only (Phase 3A) — **No experiments executed**  
**Target Hardware:** HP Victus Laptop (RTX 4050 6GB GDDR6, 16GB DDR4-3200 RAM, WD SN810 NVMe)  
**Target Model:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` ($18,632,184,480\text{ bytes} \approx 17.35\text{ GiB}$)  
**Installed Runtime:** `llama.cpp` Windows x64 CUDA 12.4 (`b12032`, commit `dde879bc4`)  
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

### Runtime Option Support Status (Bundled Binary `b12032`):

| CLI Flag | Semantic Meaning | `llama-cli.exe` | `llama-server.exe` | `llama-bench.exe` | Scope & Operational Constraints |
|---|---|:---:|:---:|:---:|---|
| `-ngl N` (`--n-gpu-layers`) | Layers stored & computed on GPU | Supported | Supported | Supported | Offloads dense layers and MoE layers up to $N$. |
| `-ncmoe N` (`--no-cache-moe`) | CPU placement count for MoE weights | Supported | Supported | Supported | Keeps MoE expert weights of first $N$ layers on CPU host virtual memory (`mmap`). **Does NOT pin physical RAM.** |
| `--moe-cache-mib M` | Dynamic GPU MoE expert cache size | Supported | Supported | **NOT Supported** | Allocates $M$ MiB in GPU VRAM to cache CPU-resident MoE experts. Replacement heuristic is runtime-internal (do not assume strict LRU). |
| `-c N` (`--ctx-size`) | Context window size | Supported | Supported | Supported | Strictly sizes KV cache buffer. |
| `-b N` (`--batch-size`) | Logical batch size | Supported | Supported | Supported | Maximum tokens evaluated per logical iteration. |
| `-ub N` (`--ubatch-size`) | Physical micro-batch size | Supported | Supported | Supported | Controls GPU compute activation scratchpad size. |
| `-ctk T` / `-ctv T` | KV cache data types | Supported | Supported | Supported | Quantized cache format (`q8_0`, `f16`, `q4_0`). |
| `-fa M` (`--flash-attn`) | FlashAttention mode | Supported | Supported | Supported | Modes: `auto`, `off`, `on`. |
| `-lm M` (`--loading-mode`) | Model file loading mode | Supported | Supported | Supported | Modes: `auto`, `mmap`, `dio`, `none`. |
| `-lzm M` (`--lazy-mode`) | Lazy loading mode | Supported | Supported | Supported | Modes: `auto`, `off`, `on`. **In `b12032`, `auto` only applies lazy allocation to tensors $>4\text{ GiB}$.** Does NOT provide fine-grained MoE streaming. |
| `--mlock` | Physical RAM locking | Supported | Supported | Supported | Calls Win32 `VirtualLock()`. Fails or causes OS instability when model exceeds available physical RAM. |

---

## 2. Controlled Experimental Designs

---

### Experiment 1: CPU MoE Layer Partitioning (`-ncmoe`)

- **Objective:** Determine the optimal balance between GPU VRAM offload and host CPU memory bandwidth.
- **Hypothesis:** Offloading 8 MoE expert layers to GPU VRAM (`-ncmoe 40`) will maximize decode throughput by reducing the volume of expert weights streamed across the CPU memory bus, while maintaining at least 1.0 GiB of gross VRAM margin to prevent driver thrashing.
- **Model Quantization Structure:**
  - 24 layers have `Q6_K` down projections ($391,643,136\text{ bytes} = 373.50\text{ MiB}$ per layer): `[0, 1, 2, 3, 4, 5, 8, 11, 14, 17, 20, 23, 26, 29, 32, 35, 38, 41, 42, 43, 44, 45, 46, 47]`.
  - 24 layers have `Q4_K` down projections ($339,738,624\text{ bytes} = 324.00\text{ MiB}$ per layer): `[6, 7, 9, 10, 12, 13, 15, 16, 18, 19, 21, 22, 24, 25, 27, 28, 30, 31, 33, 34, 36, 37, 39, 40]`.
  - Under `-ncmoe 40`, GPU holds layers 40–47: 1 layer @ 324.00 MiB (layer 40) + 7 layers @ 373.50 MiB (layers 41–47) = $3,081,240,576\text{ bytes} = 2,938.50\text{ MiB} \approx 2.87\text{ GiB}$ (mean $367.31\text{ MiB/layer}$).
  - Under `-ncmoe 40`, CPU holds layers 0–39: 23 layers @ 324.00 MiB + 17 layers @ 373.50 MiB = $14,471,921,664\text{ bytes} = 13,801.50\text{ MiB} \approx 13.48\text{ GiB}$ (mean $345.04\text{ MiB/layer}$).
- **Independent Variable:** `-ncmoe` (Number of MoE layers kept on CPU):
  - Variant 1.1: `-ncmoe 48` (0 MoE layers on GPU, 48 on CPU) — GPU VRAM for MoE: $0\text{ MiB}$
  - Variant 1.2: `-ncmoe 44` (4 MoE layers on GPU: layers 44–47 @ 373.50 MiB each) — GPU VRAM for MoE: $1,494.00\text{ MiB}$
  - Variant 1.3: `-ncmoe 42` (6 MoE layers on GPU: layers 42–47 @ 373.50 MiB each) — GPU VRAM for MoE: $2,241.00\text{ MiB}$
  - Variant 1.4: `-ncmoe 40` (8 MoE layers on GPU: 1 @ 324.00 + 7 @ 373.50) — GPU VRAM for MoE: $2,938.50\text{ MiB}$ (Baseline)
  - Variant 1.5: `-ncmoe 38` (10 MoE layers on GPU: 2 @ 324.00 + 8 @ 373.50) — GPU VRAM for MoE: $3,636.00\text{ MiB}$ (Aggressive VRAM)
- **Controlled Variables:**
  - Model: `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
  - `-ngl 48` (all dense attention layers on GPU)
  - `-c 512` (context length; KV cache = 25.50 MiB)
  - `-b 64 -ub 16` (batch sizes)
  - `-ctk q8_0 -ctv q8_0` (KV cache quantization)
  - `--moe-cache-mib 0` (GPU expert cache disabled)
  - `-fa auto -lm mmap`
  - Fixed prompt and output length: 128 tokens (`-n 128`).
- **Expected Trade-Offs:**
  - Decreasing `-ncmoe` increases GPU VRAM consumption by either 324.00 MiB or 373.50 MiB per layer.
  - Decreasing `-ncmoe` reduces CPU memory bandwidth demand during decode by offloading expert routing to GPU.
  - Risk: Lowering `-ncmoe` below 40 erodes VRAM headroom below safe operational margins (~1.4 GiB gross), increasing the risk of CUDA OOM or WDDM driver segment paging.
- **Required Measurements:**
  - Peak and steady-state GPU VRAM usage (MiB).
  - VRAM headroom remaining (MiB).
  - Peak process Working Set and Private Commit (MB).
  - Available physical RAM throughout run (GiB).
  - Decode speed (tokens/second) and Prompt Processing speed (tokens/second).
- **Admission & Stop Conditions:**
  - Preflight admission: Available RAM $\ge 10.0\text{ GiB}$, Free VRAM $\ge 5,500\text{ MiB}$.
  - Abort during run: If free VRAM drops below 512 MiB or available RAM drops below 2.0 GiB for $>5.0\text{s}$.
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If `-ncmoe 40` achieves higher decode tokens/second than `-ncmoe 48` with zero CUDA OOM errors and VRAM headroom remains $>800\text{ MiB}$.
  - **Rejected:** If decode speed does not scale with GPU layer count (indicating the CPU host memory bus remains the complete gating bottleneck) or if memory fragmentation induces CUDA OOM.

---

### Experiment 2: Dynamic GPU Expert Cache Sizing (`--moe-cache-mib`)

- **Objective:** Evaluate whether caching recently selected MoE experts in GPU VRAM improves decode throughput on domain-specific prompts.
- **Hypothesis:** Allocating a modest dynamic expert cache in GPU VRAM (`--moe-cache-mib 256` to `512`) will increase warm-cache decode tokens/second by reducing PCIe transfer overhead for recurrent experts, provided VRAM headroom remains safely above the 512 MiB floor.
- **Critical Safety Constraint:** A 1,024 MiB or larger expert cache must **NOT** be configured before baseline measurements establish the exact empirical overhead of CUDA context initialization (~150–250 MiB) and compute scratchpad allocations (~200–450 MiB). At $3,986.5\text{ MiB}$ base allocation, adding $1,024\text{ MiB}$ pushes total static allocation to $5,010.5\text{ MiB}$, leaving less than $910\text{ MiB}$ gross margin from ~5,920 MiB free VRAM, which risks breaching the 512 MiB abort floor.
- **Independent Variable:** `--moe-cache-mib` (GPU cache size in MiB):
  - Variant 2.1: `--moe-cache-mib 0` (Disabled — Baseline)
  - Variant 2.2: `--moe-cache-mib 256` (~96 expert instances cached)
  - Variant 2.3: `--moe-cache-mib 512` (~193 expert instances cached)
  - Variant 2.4: `--moe-cache-mib 768` (~290 expert instances cached — Intermediate)
  - Variant 2.5: `--moe-cache-mib 1024` (~386 expert instances cached — High risk; gated on baseline headroom)
- **Controlled Variables:**
  - Model: `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
  - `-ngl 48`
  - `-ncmoe 40` (40 layers on CPU, 8 layers on GPU)
  - `-c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap`
  - Fixed prompt and output: 128 tokens (`-n 128`).
  - Executed across 3 consecutive trials to measure Cold Cache vs. Warm Cache behavior.
- **Expected Trade-Offs:**
  - Higher cache size consumes VRAM that directly reduces the safety buffer.
  - In Q4_K_M, a single expert slice (gate + up + down) averages $\approx 2.70\text{ MiB}$.
  - Cold-cache generation incurs PCIe copy latency on misses; warm-cache generation benefits if domain-specific prompts repeatedly route to the same experts.
- **Required Measurements:**
  - Cold-cache decode speed (Trial 1 t/s) vs. Warm-cache decode speed (Trial 2 & 3 t/s).
  - GPU VRAM consumption before, during, and after generation.
  - PCIe throughput and bus utilization (via `nvidia-smi dmon`).
  - System commit and RAM stability.
- **Admission & Stop Conditions:**
  - Preflight admission: Available RAM $\ge 10.0\text{ GiB}$, Free VRAM $\ge 5,500\text{ MiB}$.
  - Abort if free VRAM drops below 512 MiB.
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If `--moe-cache-mib 512` yields a $>20\%$ increase in warm-cache decode tokens/second over the baseline (`0 MiB`) while free VRAM remains $>700\text{ MiB}$.
  - **Rejected:** If warm-cache decode speed shows $<5\%$ improvement or if PCIe transfer overhead negates caching benefit.

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
  - `-ncmoe 48` (All MoE experts on CPU, isolating the dense attention effect)
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

- **Objective:** Evaluate the memory footprint, KV cache growth, and throughput impact of expanding the context window from 512 to 4,096 tokens.
- **Hypothesis:** Because Qwen3-30B-A3B utilizes Grouped Query Attention with only 4 KV heads ($H_{kv}=4$), expanding context length from 512 to 2,048 tokens consumes only an additional $76.50\text{ MiB}$ of VRAM in `q8_0`, having negligible impact on memory headroom while preserving decode throughput.
- **KV Cache Sizing Formula (GGML Q8_0):**
  $$\text{KV Bytes} = \text{Context} \times 48\text{ layers} \times 4\text{ heads} \times 128\text{ dim} \times 2\text{ (K+V)} \times \left(\frac{34\text{ bytes}}{32\text{ values}}\right) = \text{Context} \times 52,224\text{ bytes}$$
- **Independent Variable:** `-c` (Context window length):
  - Variant 4.1: `-c 512` (Baseline — $26,738,688\text{ bytes} = 25.50\text{ MiB}$ in Q8_0)
  - Variant 4.2: `-c 1024` ($53,477,376\text{ bytes} = 51.00\text{ MiB}$ in Q8_0)
  - Variant 4.3: `-c 2048` ($106,954,752\text{ bytes} = 102.00\text{ MiB}$ in Q8_0)
  - Variant 4.4: `-c 4096` ($213,909,504\text{ bytes} = 204.00\text{ MiB}$ in Q8_0 — Stress test)
- **Controlled Variables:**
  - Model: `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
  - `-ngl 48 -ncmoe 40 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -fa auto -lm mmap`
  - Fixed generation: 64 output tokens.
- **Expected Trade-Offs:**
  - Modest VRAM increase per token due to GQA compression ($51.00\text{ MiB}$ per 1,024 tokens).
  - Attention computation time increases quadratically ($O(N^2)$), partially mitigated by FlashAttention.
- **Required Measurements:**
  - Exact VRAM delta between `-c 512` and `-c 2048`.
  - Prompt processing speed at prompt lengths 256, 512, 1024.
  - Decode speed stability across context lengths.
- **Admission & Stop Conditions:**
  - Standard preflight safety gates. Abort if free VRAM drops below 512 MiB.
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If measured VRAM usage increases by $\approx 76.5\text{ MiB}$ between 512 and 2,048 context and decode tokens/second remains within 10% of baseline.
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
  - Preflight admission: Free VRAM $\ge 5,500\text{ MiB}$. Abort if free VRAM drops below 512 MiB.
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If `-b 256 -ub 64` achieves $>25\%$ higher PP tokens/second than `-b 64 -ub 16` while peak VRAM remains under 4,800 MiB.
  - **Rejected:** If increasing `-ub` triggers CUDA OOM or provides negligible (<5%) throughput benefit.

---

### Experiment 6A: File-Backed Memory-Mapping Modes (`-lm`)

- **Objective:** Verify operating system memory pressure and page-fault behavior under different memory mapping modes with single-variable isolation.
- **Hypothesis:** Using `-lm mmap` (Win32 `MapViewOfFile`) is the only viable configuration on a 16 GB host; disabling memory-mapping (`-lm none`, forcing explicit private memory allocation) will cause immediate process failure or severe paging thrashing due to physical RAM exhaustion.
- **Independent Variable:** Memory loading mode (`-lm`):
  - Variant 6A.1: `-lm mmap` (Standard OS file-backed mapping — Baseline)
  - Variant 6A.2: `-lm none` (Non-mmap heap allocation via buffered read — Negative control / theoretical failure test)
  - Variant 6A.3: `-lm dio` (Direct I/O unbuffered read, if supported by Windows runtime build)
- **Controlled Variables:**
  - Model: `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
  - `-ngl 48 -ncmoe 40 -c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -lzm auto`
  - Smoke test execution (`-n 1`).
- **Expected Trade-Offs:**
  - `-lm none` attempts to allocate the entire CPU-resident model portion ($13,801.50\text{ MiB}$) as private committed memory, instantly exhausting available physical RAM (~8.66–9.91 GiB) and forcing massive pagefile thrashing.
  - `-lm mmap` allows the OS to demand-page only active expert slices (~862.6 MiB logical working set per forward pass).
- **Required Measurements:**
  - Process Private Commit (`PrivateUsage` in MB).
  - Process Working Set (`WorkingSetSize` in MB).
  - System commit charge percentage.
  - Time required for model initialization (seconds).
- **Admission & Stop Conditions:**
  - For Variant 6A.2 (`-lm none`): Must be run under strict supervisor abort limits (kill process immediately if commit exceeds 90% or available RAM drops below 2.0 GiB).
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If `-lm mmap` maintains private commit below 500 MB while `-lm none` fails admission or triggers immediate supervisor memory constraint aborts.
  - **Rejected:** If non-mmap allocation somehow succeeds without paging (physically impossible given 16 GB RAM).

---

### Experiment 6B: Lazy Weight Loading Evaluation (`-lzm`)

- **Objective:** Evaluate the effect of lazy loading (`-lzm`) on model initialization time and runtime memory consumption.
- **Hypothesis:** In runtime build `b12032`, lazy loading defaults to `auto`, which only acts on individual tensors $>4\text{ GiB}$. Because the largest tensor in `Qwen3-30B-A3B-Q4_K_M` is `tok_embd.weight` (~656 MiB) and MoE expert blocks are ~373.5 MiB, `-lzm auto` and `-lzm off` will behave identically unless `-lzm on` explicitly forces lazy loading on smaller tensors.
- **Independent Variable:** Lazy loading mode (`-lzm`):
  - Variant 6B.1: `-lzm auto` (Default threshold evaluation — Baseline)
  - Variant 6B.2: `-lzm off` (Explicitly disable lazy evaluation)
  - Variant 6B.3: `-lzm on` (Force lazy evaluation across all tensors, if supported)
- **Controlled Variables:**
  - Model: `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`
  - `-ngl 48 -ncmoe 40 -c 512 -b 64 -ub 16 -ctk q8_0 -ctv q8_0 -lm mmap`
  - Smoke test execution (`-n 1`).
- **Expected Trade-Offs:**
  - If `-lzm on` defers weight tensor allocation until first access, model startup time should decrease, but the first token generation (TTFT) will absorb the initial allocation latency.
- **Required Measurements:**
  - Model load time (`load time` from `--perf` in ms).
  - Time to first token (TTFT in ms).
  - Peak private commit and working set during initialization vs. generation.
- **Admission & Stop Conditions:**
  - Standard preflight safety gates.
- **Hypothesis Evaluation Criteria:**
  - **Supported:** If `-lzm auto` and `-lzm off` produce identical startup and runtime memory profiles, confirming that the 4 GiB threshold prevents lazy evaluation from triggering on sub-4 GiB MoE weight blocks.
  - **Rejected:** If `-lzm auto` alters memory behavior on MoE expert weights.

---

## 3. Recommended Sequencing of Experiments

If and when Phase 3B is formally authorized by the Project Owner and Lead Architect, the experiments must be executed in strict sequence:

1. **Step 1: Configuration D Baseline Smoke Test (`-n 1`)** — Verifies zero regression in memory budget.
2. **Step 2: Experiment 1 (`-ncmoe` Layer Partitioning)** — Determines the optimal stable baseline for GPU vs. CPU layers.
3. **Step 3: Baseline Empirical Overhead Measurement** — Empirically measures CUDA context and compute scratchpad overheads to verify actual net VRAM headroom.
4. **Step 4: Experiment 2 (`--moe-cache-mib` GPU Expert Caching)** — Evaluates the dynamic expert cache ONLY after Step 3 confirms safe headroom above the 512 MiB floor.
5. **Step 5: Experiment 4 (`-c` Context Scaling)** — Verifies context expansion up to 2,048 tokens.
6. **Step 6: Experiment 5 (`-b` / `-ub` Batch Optimization)** — Tunes prompt processing throughput.
7. **Step 7: Experiment 6A and 6B** — Run secondary memory mapping and lazy-loading verification controls.
