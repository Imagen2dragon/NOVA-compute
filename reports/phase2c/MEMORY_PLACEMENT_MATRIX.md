# NOVA Compute — Phase 2C-A: Memory Placement Matrix

**Date:** 2026-11-04  
**Model:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` — verified  
**Runtime:** llama.cpp b12032 (CUDA 12.4, Windows x64)  
**Hardware:** RTX 4050 Laptop GPU — 6,141 MiB VRAM total / 15.65 GB physical RAM  
**Reviewer:** ChatGPT (Lead Architect)

> [!IMPORTANT]
> These are **engineering estimates** derived from the parsed tensor inventory, GGML block size formulae, and conservative llama.cpp overhead constants. They are labelled as estimates throughout. Actual allocations must be measured after initialization. Do not equate these estimates with measured CUDA or RAM allocations.

---

## 1. Model Tensor Budget Summary

| Component | Bytes | GiB | % of Model |
|---|---|---|---|
| **Expert tensors (all 48 layers)** | 17,553,162,240 | 16.348 | 94.2% |
| Expert down (`ffn_down_exps`) | 6,681,526,272 | 6.224 | 35.9% |
| Expert gate (`ffn_gate_exps`) | 5,435,817,984 | 5.062 | 29.2% |
| Expert up (`ffn_up_exps`) | 5,435,817,984 | 5.062 | 29.2% |
| **Dense attention + router (all 48 layers)** | 642,760,704 | 0.599 | 3.4% |
| **Global (embedding + output head + norms)** | 430,290,944 | 0.401 | 2.3% |
| **Total on-disk** | 18,626,213,888 | 17.347 | 100% |

**Per-layer expert weight cost:** ~365.7 MB (median; each layer holds 128 stacked gate+up+down experts)

---

## 2. Memory Terminology

| Term | Meaning |
|---|---|
| **mmap virtual space** | The entire GGUF file (18.63 GB) is mapped as a virtual address range. This does **not** immediately consume physical RAM. |
| **Resident physical RAM** | Pages that the OS has actually loaded into physical RAM. For CPU-resident experts, this is demand-paged on first access. Only the pages for active/recently-used experts are resident at any moment. |
| **Active working set (CPU experts)** | At any single decode step, only 8 of 128 experts are selected per layer. The estimate `8/128 × expert_bytes` approximates the minimum resident expert pages under perfect expert locality. Real behavior depends on expert routing diversity across tokens. |
| **VRAM allocation** | GPU tensors are copied from mmap into CUDA device memory at load time. This is a measured, resident allocation — not virtual. |
| **KV cache** | Allocated separately in VRAM (or RAM). Q8_0 KV = 1 byte/element. F16 KV = 2 bytes/element. |
| **Compute buffers** | Temporary CUDA scratch space for matrix operations. Included in the 300 MiB overhead estimate. |
| **llama.cpp process overhead** | Tokenizer, metadata, C++ runtime, Python bindings (if any): ~400 MB RAM. |

> [!WARNING]
> **VRAM and RAM are not interchangeable.** GPU tensors allocated via CUDA are locked in VRAM. CPU expert tensors reside in RAM (demand-paged from mmap). The two pools serve different components of the same model and cannot substitute for each other.

---

## 3. Five Placement Configurations

### Configuration A — All Experts CPU (`-cmoe -ngl 48`)
*All 48 layers' expert weights remain in CPU RAM. Dense attention and global tensors offloaded to GPU.*

| Component | GPU VRAM | CPU RAM |
|---|---|---|
| Dense model tensors (attn + router + globals) | **1,023.3 MiB** (est.) | — |
| Expert tensors | 0 MiB | **16.348 GiB** (mmap) |
| KV cache @ ctx=1024, Q8_0 | **48.0 MiB** | — |
| KV cache @ ctx=2048, Q8_0 | **96.0 MiB** | — |
| llama.cpp overhead (est.) | **300 MiB** | **400 MB** |
| **Total VRAM @ ctx=1024/Q8** | **1,371 MiB / 6,141 MiB** | — |
| **VRAM headroom @ ctx=1024/Q8** | **+4,770 MiB** | — |
| **Est. resident CPU RAM** | — | **~16.7 GB** |
| **Active working set (8/128 fraction)** | — | **~1.02 GiB** |

> [!WARNING]
> **Config A exceeds total physical RAM (15.65 GB).** The mmap maps 18.63 GB virtually but requires ~16.7 GB resident for expert pages to be accessible without heavy demand-paging. This machine has 15.65 GB total RAM. At current available RAM (10.49 GB), this will trigger sustained pagefile thrashing. **Config A is not feasible on this hardware without closing all other applications and accepting significant paging overhead.**

CLI:
```powershell
.\runtime\windows\llama-cpp\llama-cli.exe -m .\models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf `
  -ngl 48 -cmoe -c 1024 -b 512 -ub 128 -ctk q8_0 -ctv q8_0 -fa auto `
  -lm mmap -n 1 --simple-io --perf -p "Test"
```

---

### Configuration B — 44 CPU / 4 GPU Layers (`-ncmoe 44 -ngl 48`)
*First 44 layers' experts in CPU RAM. Final 4 layers' experts in GPU VRAM.*

| Component | GPU VRAM | CPU RAM |
|---|---|---|
| Dense model + 4 GPU expert layers | **2,517.3 MiB** (est.) | — |
| CPU expert tensors (layers 0–43) | 0 MiB | **14.889 GiB** (mmap) |
| KV cache @ ctx=1024, Q8_0 | **48.0 MiB** | — |
| Overhead | **300 MiB** | **400 MB** |
| **Total VRAM @ ctx=1024/Q8** | **2,865 MiB / 6,141 MiB** | — |
| **VRAM headroom** | **+3,276 MiB** | — |
| **Est. resident CPU RAM** | — | **~15.3 GB** |

> [!WARNING]
> **Config B CPU RAM still exceeds total physical RAM (15.65 GB) by ~0.3 GB under ideal conditions**, and exceeds currently available RAM (10.49 GB) by ~4.8 GB. Expect paging pressure. Not recommended as first experiment.

---

### Configuration C — 42 CPU / 6 GPU Layers (`-ncmoe 42 -ngl 48`)

| Component | GPU VRAM | CPU RAM |
|---|---|---|
| Dense model + 6 GPU expert layers | **3,264.3 MiB** (est.) | — |
| CPU expert tensors (layers 0–41) | 0 MiB | **14.159 GiB** (mmap) |
| KV @ ctx=1024, Q8_0 | **48.0 MiB** | — |
| Overhead | **300 MiB** | **400 MB** |
| **Total VRAM @ ctx=1024/Q8** | **3,612 MiB / 6,141 MiB** | — |
| **VRAM headroom** | **+2,529 MiB** | — |
| **Est. resident CPU RAM** | — | **~14.5 GB** |

> [!NOTE]
> Config C is marginally below total physical RAM. With all other apps closed (freeing ~2.1 GB), available RAM would reach ~12.6 GB. Resident expert pages may fit within physical RAM at light workloads.

---

### Configuration D — 40 CPU / 8 GPU Layers (`-ncmoe 40 -ngl 48`)

| Component | GPU VRAM | CPU RAM |
|---|---|---|
| Dense model + 8 GPU expert layers | **3,961.8 MiB** (est.) | — |
| CPU expert tensors (layers 0–39) | 0 MiB | **13.478 GiB** (mmap) |
| KV @ ctx=1024, Q8_0 | **48.0 MiB** | — |
| Overhead | **300 MiB** | **400 MB** |
| **Total VRAM @ ctx=1024/Q8** | **4,310 MiB / 6,141 MiB** | — |
| **VRAM headroom** | **+1,831 MiB** | — |
| **Est. resident CPU RAM** | — | **~13.9 GB** |

---

### Configuration E — 36 CPU / 12 GPU Layers (`-ncmoe 36 -ngl 48`)
*The original Phase 2A recommended configuration.*

| Component | GPU VRAM | CPU RAM |
|---|---|---|
| Dense model + 12 GPU expert layers | **5,307.3 MiB** (est.) | — |
| CPU expert tensors (layers 0–35) | 0 MiB | **12.164 GiB** (mmap) |
| KV @ ctx=1024, Q8_0 | **48.0 MiB** | — |
| KV @ ctx=2048, Q8_0 | **96.0 MiB** | — |
| Overhead | **300 MiB** | **400 MB** |
| **Total VRAM @ ctx=1024/Q8** | **5,655 MiB / 6,141 MiB** | — |
| **VRAM headroom @ ctx=1024** | **+486 MiB** | — |
| **Total VRAM @ ctx=2048/Q8** | **5,703 MiB / 6,141 MiB** | — |
| **VRAM headroom @ ctx=2048** | **+438 MiB** | — |
| **Est. resident CPU RAM** | — | **~12.6 GB** |

> [!CAUTION]
> **Config E: VRAM headroom is only +486 MiB at ctx=1024.** Any CUDA allocator overhead beyond the 300 MiB estimate, or unexpected compute buffer growth, could cause VRAM OOM. The 300 MiB overhead estimate is based on Phase 1 (1.7B model) experience and may not scale to 30B. This configuration is high-risk for VRAM OOM.

---

## 4. Comparative Matrix

| Config | CPU Expert Layers | GPU Expert Layers | GPU Model MiB (est.) | VRAM Total @1024/Q8 (est.) | VRAM Headroom | CPU RAM Est. (est.) | Feasible? |
|---|---|---|---|---|---|---|---|
| A | 48 | 0 | 1,023 | 1,371 MiB | +4,770 MiB | ~16.7 GB | ❌ RAM OOM |
| B | 44 | 4 | 2,517 | 2,865 MiB | +3,276 MiB | ~15.3 GB | ❌ RAM pressure |
| C | 42 | 6 | 3,264 | 3,612 MiB | +2,529 MiB | ~14.5 GB | ⚠️ Marginal |
| D | 40 | 8 | 3,962 | 4,310 MiB | +1,831 MiB | ~13.9 GB | ⚠️ Likely paging |
| E | 36 | 12 | 5,307 | 5,655 MiB | **+486 MiB** | ~12.6 GB | ⚠️ VRAM tight |

> [!NOTE]
> All "est." figures are pre-initialization estimates. All five configurations fit within VRAM at ctx=1024/Q8_0. The limiting factor for all configurations is **CPU RAM**, not VRAM. Config E is best for VRAM utilization; Config C/D is best for RAM safety margin. None of these will initialize successfully at the current system RAM state (10.49 GB free) without closing background applications first.

---

## 5. KV Cache Reference Table

| Context Tokens | KV Quant | Bytes | MiB |
|---|---|---|---|
| 512 | Q8_0 (1 byte) | 25,165,824 | 24.0 MiB |
| 1,024 | Q8_0 (1 byte) | 50,331,648 | 48.0 MiB |
| 2,048 | Q8_0 (1 byte) | 100,663,296 | 96.0 MiB |
| 1,024 | F16 (2 bytes) | 100,663,296 | 96.0 MiB |
| 2,048 | F16 (2 bytes) | 201,326,592 | 192.0 MiB |

Formula: `ctx × n_layers × n_kv_heads × head_dim × 2 × bytes_per_elem`  
= `ctx × 48 × 4 × 128 × 2 × bytes_per_elem`

> [!TIP]
> Starting with ctx=512 (not ctx=1024) saves 24 MiB VRAM — meaningful for Config E which has only +486 MiB headroom.
