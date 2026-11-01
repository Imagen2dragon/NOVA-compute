# NOVA Compute — Phase 2A: Memory Budget Analysis

**Target Model:** `Qwen/Qwen3-30B-A3B` (48 Layers, 128 Experts, 8 Active Experts per Token)  
**Hardware Baseline:** HP Victus Laptop (RTX 4050 6GB VRAM, 16GB DDR4-3200 System RAM)  
**Role:** Implementation Engineer (Antigravity)  
**Reviewer:** Lead Architect & Technical Reviewer (ChatGPT)  

---

## 1. Principles of Memory Partitioning

To avoid catastrophic system freezing, memory exhaustion, and sustained pagefile thrashing, **GPU VRAM and System RAM must never be treated as an interchangeable single pool**. 

- **VRAM (6,140 MiB GDDR6):** High bandwidth (~192 GB/s), extremely low latency, but strictly limited capacity (~5,700 MB usable).
- **RAM (16,024 MiB DDR4):** Moderate bandwidth (~35–42 GB/s sustained dual-channel), large working capacity (~11,800 MB usable when clean), but shared with the operating system and background tasks.
- **NVMe SSD (WD PC SN810):** High capacity (171 GB free), high sequential burst (~6.6 GB/s), but slow random 4K–64K page-in bandwidth (~100–400 MB/s). Useful for mmap backing, but disastrous for active per-token random page faults.

---

## 2. Parameter Decomposition of Qwen3-30B-A3B

Based on the official model configuration:

$$\begin{aligned}
d_{model} &= 2048, \quad L = 48, \quad H_{q} = 32, \quad H_{kv} = 4, \quad d_{head} = 128 \\
E_{total} &= 128, \quad E_{active} = 8, \quad d_{ffn\_exp} = 768, \quad V_{vocab} = 151,936
\end{aligned}$$

### 2.1 Exact Component Parameter Breakdown
1. **Dense Self-Attention Projections (per layer):**
   - $W_q$: $2048 \times (32 \times 128) = 8,388,608$
   - $W_k$: $2048 \times (4 \times 128) = 1,048,576$
   - $W_v$: $2048 \times (4 \times 128) = 1,048,576$
   - $W_o$: $(32 \times 128) \times 2048 = 8,388,608$
   - Norms: $2048 + 128 + 128 = 2,304$
   - **Total Attention per Layer:** $18,876,672$ parameters (~18.88M).
   - **Attention across 48 layers:** **906,080,256 parameters (~0.906B)**.
2. **Dense MoE Routers & Gating (per layer):**
   - $W_{gate\_inp}$: $2048 \times 128 = 262,144$ parameters.
   - **Routers across 48 layers:** **12,582,912 parameters (~0.013B)**.
3. **Embeddings & LM Output Head:**
   - $tok\_embd$: $151,936 \times 2048 = 311,164,928$ parameters (~0.311B).
   - $output$ (LM head): $151,936 \times 2048 = 311,164,928$ parameters (~0.311B).
   - Final Norm: $2,048$ parameters.
   - **Total Embeddings & Head:** **622,331,904 parameters (~0.622B)**.
4. **MoE Sparse Experts (SwiGLU):**
   - Single Expert: $3 \times (2048 \times 768) = 4,718,592$ parameters (~4.72M).
   - 128 Experts per Layer: $128 \times 4.7186\text{M} = 603,979,776$ parameters (~604.0M).
   - **Experts across 48 layers:** **28,991,029,248 parameters (~28.99B)**.

$$\mathbf{Total\ Parameters} = 0.906\text{B} + 0.013\text{B} + 0.622\text{B} + 28.991\text{B} = \mathbf{30.532\text{ Billion}}$$

- **Dense Parameters (Shared across all tokens):** **1.541 Billion (5.0%)**
- **MoE Expert Parameters (Paging / Swappable):** **28.991 Billion (95.0%)**
- **Active Parameters per Forward Pass:** $1.541\text{B} + (48 \times 8 \times 4.72\text{M}) = \mathbf{3.353\text{ Billion (11.0%)}}$

---

## 3. Weight Sizing Across Target Quantizations

| Component | Parameter Count | Q2_K (~2.8 bpw) | Q3_K_M (~3.5 bpw) | Q4_K_M (~4.5 bpw) |
|---|---|---|---|---|
| Attention + Norms (48 Layers) | 906.08 M | ~490 MB | ~540 MB | ~610 MB |
| Routers (48 Layers) | 12.58 M | ~8 MB | ~9 MB | ~11 MB |
| Embeddings + LM Head | 622.33 M | ~340 MB | ~390 MB | ~450 MB |
| **Total Dense Weights (GPU-Resident)** | **1.541 B** | **~838 MB** | **~939 MB** | **~1,071 MB** |
| Single Expert Instance | 4.719 M | ~1.65 MB | ~2.08 MB | ~2.65 MB |
| Layer Experts (128 experts) | 603.98 M | ~211 MB | ~266 MB | ~339 MB |
| **Total MoE Weights (48 Layers)** | **28.991 B** | **~9.85 GB** | **~12.45 GB** | **~15.85 GB** |
| **Complete Model Footprint** | **30.532 B** | **~10.69 GB** | **~13.39 GB** | **~16.92 GB** |

---

## 4. KV Cache & Runtime Buffer Budgets

### 4.1 KV Cache Sizing
Qwen3-30B-A3B features Grouped Query Attention with 4 Key/Value heads ($H_{kv} = 4$) and head dimension 128 ($d_{head} = 128$).
$$\text{Elements per Token} = 2 \times L \times H_{kv} \times d_{head} = 2 \times 48 \times 4 \times 128 = 49,152\text{ values/token}$$

| Quantization / Precision | Bytes per Value | Context = 1,024 tokens | Context = 2,048 tokens | Context = 4,096 tokens |
|---|---|---|---|---|
| **FP16 (`-ctk f16 -ctv f16`)** | 2.0 | **96.0 MB** | **192.0 MB** | 384.0 MB |
| **Q8_0 (`-ctk q8_0 -ctv q8_0`)** | 1.0 | **48.0 MB** | **96.0 MB** | 192.0 MB |
| **Q4_0 (`-ctk q4_0 -ctv q4_0`)** | 0.5 | **24.0 MB** | **48.0 MB** | 96.0 MB |

> [!TIP]
> The KV cache footprint at 2048 context is only **192 MB** in FP16 and **96 MB** in Q8_0. KV cache is **not** a primary bottleneck on this model; expert weight placement is the dominant factor.

### 4.2 Compute Scratch & Activation Buffers
- FlashAttention activation buffer: ~150–250 MB
- Router logits and MoE gating scratch: ~100–150 MB
- General graph tensor allocation: ~100–150 MB
- **Total Compute Buffer Overhead:** **~350–550 MB** in VRAM.

---

## 5. Comprehensive Hardware Tier Budgets

### 5.1 VRAM Tier (RTX 4050 6GB)
- **Total Physical VRAM:** 6,140 MB
- **Windows DWM / Display Consumption:** ~250–350 MB
- **Effective Usable VRAM Budget:** **~5,700 MB**

### 5.2 System RAM Tier (16GB Physical)
- **Total OS-Visible RAM:** 16,024 MB (~15.65 GB)
- **Clean Windows Baseline (Background Apps Closed):** ~3,500–4,000 MB
- **Effective Usable RAM Budget (Clean):** **~11,800 MB**
- **Current Dirty State (Browsers, IDEs, SQL Server open):** ~12,300 MB used (**only ~3,700 MB available**)

---

## 6. Tiered Allocation Feasibility by Quantization

### Scenario 1: Q4_K_M (~16.92 GB total model size)
- Dense components on GPU: ~1.07 GB weights + ~0.20 GB KV (2K) + ~0.45 GB buffers = **~1.72 GB VRAM** (Feasible on GPU).
- Expert components on CPU RAM: **15.85 GB**.
- **Deficit:** 15.85 GB exceeds clean usable RAM (11.8 GB) by **4.05 GB**!
- **Consequence:** Immediate heavy OS swapping to NVMe `pagefile.sys`. Sustained page faults on every token forward pass. Decode speed will collapse to $<0.5$ tokens/s.

### Scenario 2: Q3_K_M (~13.39 GB total model size)
- Dense components on GPU: ~0.94 GB weights + ~0.20 GB KV (2K) + ~0.45 GB buffers = **~1.59 GB VRAM** (Leaves ~4.1 GB VRAM available).
- Expert components on CPU RAM: **12.45 GB**.
- **Deficit:** 12.45 GB exceeds clean usable RAM (11.8 GB) by **~0.65 GB**.
- **Mitigation:**
  1. Offload 3 to 4 layers of experts to GPU VRAM (each layer = 266 MB; 4 layers = 1.06 GB VRAM), reducing RAM burden to 11.39 GB (fits completely inside clean RAM!).
  2. Alternatively, allocate a 3,072 MB GPU expert cache (`--moe-cache-mib 3072`).

### Scenario 3: Q2_K (~10.69 GB total model size)
- Dense components on GPU: ~0.84 GB weights + ~0.20 GB KV + ~0.40 GB buffers = **~1.44 GB VRAM**.
- Expert components on CPU RAM: **~9.85 GB**.
- **Headroom:** 9.85 GB fits into clean usable RAM (11.8 GB) with **~1.95 GB of safe operating margin**!
- **Consequence:** Zero OS page thrashing. Fastest and safest baseline for initial feasibility testing, with the trade-off of higher quantization perplexity.

---

## 7. Budget Summary Matrix (Context = 2,048 Tokens)

| Memory Partition | Q2_K Placement | Q3_K_M Placement | Q4_K_M Placement |
|---|---|---|---|
| **GPU VRAM — Dense Weights** | 838 MB | 939 MB | 1,071 MB |
| **GPU VRAM — KV Cache (2K, FP16)** | 192 MB | 192 MB | 192 MB |
| **GPU VRAM — Compute Buffers** | 400 MB | 450 MB | 500 MB |
| **GPU VRAM — Dedicated Expert Cache** | 2,500 MB (optional) | 3,072 MB (optional) | 2,000 MB (optional) |
| **Total VRAM Allocated** | **3,930 MB** (69% cap) | **4,653 MB** (82% cap) | **3,763 MB** (66% cap) |
| **RAM — Operating System (Clean)** | 3,800 MB | 3,800 MB | 3,800 MB |
| **RAM — Resident Expert Weights** | 9,850 MB | 11,390 MB (4L on GPU) | 15,850 MB |
| **Total System RAM Allocated** | **13,650 MB** | **15,190 MB** | **19,650 MB (OVERFLOW)** |
| **NVMe Pagefile Pressure** | **None (Zero Thrash)** | **Minimal (<1 GB)** | **Critical (~4 GB Thrash)** |
| **Feasibility Verdict** | **High Feasibility** | **Conditionally Feasible** | **Impractical / Unstable** |
