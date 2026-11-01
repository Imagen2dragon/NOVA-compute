# NOVA Compute — Phase 2A: Technical Recommendations & Maintenance Proposal

**Target Model:** `Qwen/Qwen3-30B-A3B` (MoE: 48 layers, 128 experts, 8 active per token)  
**Host Hardware:** HP Victus Laptop (RTX 4050 6GB GDDR6, 16GB DDR4-3200 RAM, WD SN810 NVMe)  
**Role:** Implementation Engineer (Antigravity)  
**Reviewer:** Lead Architect & Technical Reviewer (ChatGPT)  

---

## 1. Model Quantization Recommendation

### 1.1 Empirical Findings on Model Repositories
- **Official Repository (`Qwen/Qwen3-30B-A3B-GGUF`):**
  - Contains **only** high-precision quantizations: `Q4_K_M` (17.28 GiB / 18.56 GB), `Q5_0` (19.63 GiB), `Q5_K_M` (20.23 GiB), `Q6_K` (23.37 GiB), and `Q8_0` (30.25 GiB).
  - The official Qwen team did **not** publish `Q2_K`, `Q3_K_S`, or `Q3_K_M` GGUF artifacts.
- **Curated Community Repositories (`bartowski` & `unsloth`):**
  - [`bartowski/Qwen_Qwen3-30B-A3B-GGUF`](https://huggingface.co/bartowski/Qwen_Qwen3-30B-A3B-GGUF):
    - `Qwen_Qwen3-30B-A3B-Q2_K.gguf`: 10.16 GiB (10,908,648,096 bytes) | LFS: `792b0e6e73f4e1f76cf533b66aa8d08cb7ba24845180f9ef46db419c72e259e8`
    - `Qwen_Qwen3-30B-A3B-Q3_K_M.gguf`: 13.11 GiB (14,076,803,744 bytes) | LFS: `df04f4640d216515b67a1c0d5177aa0d6c483f9e992dc7a77d70fa7ee88e990c`
    - `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`: 17.35 GiB (18,632,184,480 bytes)

### 1.2 Quantization Selection Analysis
1. **`Q4_K_M` (17.28–17.35 GiB):** **Not Recommended for Initial Feasibility.**  
   Combined with OS overhead (~3.8 GB) and runtime buffers, total RAM+VRAM requirement exceeds 21.5 GB. On a 16GB RAM + 6GB VRAM laptop, this induces an immediate 4GB physical memory deficit, forcing continuous NVMe pagefile thrashing and degrading decode speed to $<0.5$ tokens/s.
2. **`Q3_K_M` (13.11 GiB):** **Primary Recommended Quantization.**  
   Strikes the optimal balance between reasoning accuracy and memory footprint. With ~1.5 GB of dense layers and KV cache placed on the GPU and 12 layers of experts placed on GPU VRAM (Configuration C) or cached (Configuration B), the remaining expert footprint in System RAM is reduced to **~9.6–11.4 GB**, fitting inside clean physical RAM (11.8 GB usable) without page thrashing.
3. **`Q2_K` (10.16 GiB):** **Safety Fallback Quantization.**  
   If the user cannot close sufficient desktop applications to recover 11.8 GB of clean physical RAM, `Q2_K` requires only ~9.85 GB in RAM and fits safely even under moderate desktop load (~10 GB free RAM).

---

## 2. Phase 1 Maintenance Proposal

The following maintenance improvements address observations from the Phase 1 qualification without altering benchmark integrity or rewriting Git history:

### 2.1 System-Wide RAM Telemetry Attribution
- **Observation:** In `reports/phase1/runtime/BASELINE.md` and `runtime-metadata.json`, the telemetry field `ram_peak_mb` was reported as `14,449 MB`.
- **Correction:** Clarify that this metric represents **total system-wide physical RAM in use by the OS and all concurrent processes**. At the time of testing, preexisting Windows desktop applications (Edge, IDEs, SQL Server) were already consuming ~12,375 MB. The actual net delta consumed by `llama.cpp` for Qwen3-1.7B was **~2,074 MB**.
- **Action:** Update Phase 1 report annotations and telemetry descriptions accordingly.

### 2.2 Telemetry Sampling Limitations
- **Observation:** Telemetry in Phase 1 was polled every 150ms using a background Python daemon calling `GlobalMemoryStatusEx` and `nvidia-smi`.
- **Documentation:** Instantaneous transient allocation spikes (such as scratch graph buffers allocated and freed in $<10$ms during prompt evaluation) cannot be captured by 150ms polling. For Phase 2, we will supplement polling with `llama.cpp`'s internal allocator statistics (`--perf` timings and memory breakdown logs).

### 2.3 Relative Path Normalization in Reports
- **Observation:** File links in `BASELINE.md` referenced machine-specific absolute paths (`file:///c:/Users/abdal/...`).
- **Correction:** Convert markdown links to relative repository links (e.g., `[cpu-benchmark.json](cpu-benchmark.json)` or `reports/phase1/runtime/...`), ensuring valid links across different machines, clones, and GitHub browsing.

### 2.4 Update `README.md`
- **Observation:** `README.md` status currently reads `research prototype / Phase 0 hardware qualification`.
- **Correction:** Update status to reflect that **Phase 0 (Hardware Qualification)** and **Phase 1 (Small-Model Runtime Baseline)** are successfully completed.

### 2.5 Output Encoding in Stored Metadata
- **Observation:** In `runtime-metadata.json`, the terminal ASCII banner output from `llama-cli.exe` contains UTF-8 block-drawing glyphs (`█`, `▄`, `▀`) that were encoded into Unicode escapes (`\u00e2\u2013\u201e`).
- **Correction:** In all subsequent telemetry generators, serialize JSON with `ensure_ascii=False` and explicit UTF-8 file streams to preserve native terminal glyphs without double-encoding artifacts.

---

## 3. Recommended Next Smallest Step (Phase 2B Proposal)

Before downloading any 30B model or running heavy benchmarks, we recommend the following disciplined sequence for **Phase 2B: MoE Feasibility Execution**:

1. **Pre-Flight Memory Cleanup Protocol:**
   - Define a checklist for the user to temporarily terminate non-essential memory consumers (`sqlservr.exe`, browser instances, Slack/Telegram) to free $\ge 11.5\text{ GB}$ of physical RAM.
2. **Single Quantization Download:**
   - Download **only** `Qwen_Qwen3-30B-A3B-Q3_K_M.gguf` (13.11 GiB) from `bartowski/Qwen_Qwen3-30B-A3B-GGUF` into `models/qwen3-30b/`. Verify SHA256 integrity against the Git LFS pointer.
3. **Controlled Single-Prompt Execution (Configuration C):**
   - Execute a single test prompt using **Configuration C** (`-ngl 48 -ncmoe 36 -c 2048`) with `-n 32` tokens.
   - Monitor live RAM and VRAM to ensure zero OS pagefile thrashing.
4. **Compare with Configuration B (Expert Cache):**
   - If Configuration C demonstrates stable memory behavior, test **Configuration B** (`-ngl 48 -cmoe --moe-cache-mib 3072`) on identical prompts to evaluate GPU expert cache hit rate and measure real-world decode throughput.

---

**Status:** Phase 2A audit complete. All 4 reports generated. Stopping here and awaiting architectural review and written approval from ChatGPT (Lead Architect) before proceeding to Phase 2B.
