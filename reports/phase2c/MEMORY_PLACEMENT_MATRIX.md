# NOVA Compute — Phase 2C: Memory Placement Matrix & Prefetch Audit

**Date:** 2026-11-04  
**Model:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` — verified  
**Runtime:** llama.cpp b12032 (CUDA 12.4, Windows x64)  
**Hardware:** RTX 4050 Laptop GPU — 6,141 MiB VRAM total / 15.65 GB physical RAM  
**Reviewer:** ChatGPT (Lead Architect)  
**Status:** Updated for Phase 2C-B (Memory Model Corrections & Windows Prefetch Audit)

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

## 2. Memory Terminology & Classification

To avoid conflating distinct operating system and hardware memory mechanisms, the following categories are strictly distinguished:

| Memory Tier / Mechanism | Definition & Operational Mechanics | Behavior Under Memory Pressure |
|---|---|---|
| **Virtual Mapping (`mmap`)** | The entire GGUF file (18.63 GB) is mapped into the 64-bit virtual address space of the process. This reserves address ranges only and consumes virtually zero physical RAM until pages are touched. | N/A — virtual address space is abundant on 64-bit Windows. |
| **Prefetch Request** | `llama.cpp` calls `PrefetchVirtualMemory()` during `init_mappings(true, ...)` as an advisory hint to the Windows Cache Manager. | **Advisory optimization only.** Windows may fulfill partially or reject prefetch requests if physical RAM is constrained. |
| **Resident Working Set** | The subset of virtual memory pages currently residing in physical RAM. For CPU experts, only pages actively read during token evaluation enter the working set. | Actively protected by OS LRU page replacement. |
| **Cached File-Backed Pages** | Pages holding model weights loaded from disk into the OS file cache / standby list. | **Clean pages:** When RAM is needed, Windows immediately drops file-backed pages without writing to disk. |
| **Private Committed Memory** | Anonymous process memory allocated for runtime heap, stack, tokenizer, and KV cache (if on CPU). | **Dirty memory:** Cannot be dropped; must be paged to `pagefile.sys` if evicted. Kept minimal (~400 MB). |
| **CUDA Allocations** | Tensors, KV cache, and compute scratch buffers allocated directly in GPU dedicated VRAM (via `cudaMalloc`). | **Locked memory:** Managed strictly by NVIDIA driver; cannot page to system RAM or disk without severe driver-level degradation. |

> [!WARNING]
> **VRAM and RAM are not interchangeable.** GPU tensors allocated via CUDA are locked in dedicated VRAM. CPU expert tensors reside in RAM (demand-paged via `mmap`). The two pools serve different components of the same model and cannot substitute for each other.

---

## 3. Windows Prefetch Audit (llama.cpp b12032)

Based on direct source audit of `llama.cpp` b12032 on Windows:

1. **Mapping Initialization:** Model loading invokes `init_mappings(true, ...)` in `llama_model_loader`.
2. **Prefetch Call:** Windows `mmap` initialization calls the Windows API `PrefetchVirtualMemory` for the mapped address ranges.
3. **Prefetch Coverage:** The prefetch request may cover the complete model file.
4. **Qwen3MoE Lazy Status:** Qwen3MoE expert tensors are **not** explicitly marked `TENSOR_READ_LAZY` in the current model implementation.
5. **Non-Mandatory Allocation:** `PrefetchVirtualMemory` is an asynchronous hint to the Windows Cache Manager to populate the standby page list. It is **not** a mandatory memory commitment or locking mechanism.
6. **OS Behavior Under Pressure:** If physical RAM is insufficient, Windows Cache Manager will only read what fits comfortably into free/standby memory, discarding or skipping remaining prefetch requests without error.
7. **Important Distinctions:**
   - The entire 18.63 GB model is **never** committed to private physical RAM.
   - CPU expert weights are **not** permanently resident; unaccessed experts remain on NVMe or in reclaimable file cache.
   - Flag `-lzm on` (lazy mode) is **not** an assumed solution for lazy-loading Qwen3MoE expert tensors, as b12032's lazy mode operates primarily on tensors $>4\text{ GiB}$ or designated embedding matrices, not MoE slices.

---

## 4. Five Placement Configurations

### Configuration A — All Experts CPU (`-cmoe -ngl 48`)
*All 48 layers' expert weights remain in CPU RAM. Dense attention and global tensors offloaded to GPU.*

| Component | GPU VRAM | CPU RAM |
|---|---|---|
| Dense model tensors (attn + router + globals) | **1,023.3 MiB** (est.) | — |
| Expert tensors | 0 MiB | **16.348 GiB** (mmap) |
| KV cache @ ctx=512, Q8_0 | **24.0 MiB** | — |
| KV cache @ ctx=1024, Q8_0 | **48.0 MiB** | — |
| llama.cpp overhead (est.) | **300 MiB** | **400 MB** |
| **Total VRAM @ ctx=512/Q8** | **1,347.3 MiB / 6,141 MiB** | — |
| **Total VRAM @ ctx=1024/Q8** | **1,371.3 MiB / 6,141 MiB** | — |
| **VRAM headroom @ ctx=512** | **+4,793.7 MiB** | — |
| **Worst-case if all CPU expert pages resident** | — | **~16.738 GiB** |
| **Active working set (8/128 experts per token)** | — | **~1,047.2 MiB** |

> [!WARNING]
> **Config A worst-case resident RAM exceeds total physical RAM (15.65 GB).** While demand-paging active experts (~1.05 GB) theoretically fits, any aggressive prefetching or sequential access across diverse tokens will force constant file-page evictions and NVMe read spikes.

---

### Configuration B — 44 CPU / 4 GPU Layers (`-ncmoe 44 -ngl 48`)

| Component | GPU VRAM | CPU RAM |
|---|---|---|
| Dense model + 4 GPU expert layers | **2,517.3 MiB** (est.) | — |
| CPU expert tensors (layers 0–43) | 0 MiB | **14.889 GiB** (mmap) |
| KV cache @ ctx=512, Q8_0 | **24.0 MiB** | — |
| Overhead | **300 MiB** | **400 MB** |
| **Total VRAM @ ctx=512/Q8** | **2,841.3 MiB / 6,141 MiB** | — |
| **VRAM headroom @ ctx=512** | **+3,299.7 MiB** | — |
| **Worst-case if all CPU expert pages resident** | — | **~15.279 GiB** |
| **Active working set per token** | — | **~952.9 MiB** |

---

### Configuration C — 42 CPU / 6 GPU Layers (`-ncmoe 42 -ngl 48`)

| Component | GPU VRAM | CPU RAM |
|---|---|---|
| Dense model + 6 GPU expert layers | **3,264.3 MiB** (est.) | — |
| CPU expert tensors (layers 0–41) | 0 MiB | **14.159 GiB** (mmap) |
| KV @ ctx=512, Q8_0 | **24.0 MiB** | — |
| Overhead | **300 MiB** | **400 MB** |
| **Total VRAM @ ctx=512/Q8** | **3,588.3 MiB / 6,141 MiB** | — |
| **VRAM headroom @ ctx=512** | **+2,552.7 MiB** | — |
| **Worst-case if all CPU expert pages resident** | — | **~14.550 GiB** |
| **Active working set per token** | — | **~906.2 MiB** |

---

### Configuration D — 40 CPU / 8 GPU Layers (`-ncmoe 40 -ngl 48`)
*Selected Authorized Configuration for Phase 2C-B Smoke Test.*

| Component | GPU VRAM | CPU RAM |
|---|---|---|
| Dense model + 8 GPU expert layers | **3,961.8 MiB** (est.) | — |
| CPU expert tensors (layers 0–39) | 0 MiB | **13.478 GiB** (mmap) |
| KV cache @ ctx=512, Q8_0 | **24.0 MiB** | — |
| KV cache @ ctx=1024, Q8_0 | **48.0 MiB** | — |
| llama.cpp overhead (est.) | **300 MiB** | **400 MB** |
| **Total VRAM @ ctx=512/Q8** | **4,285.8 MiB / 6,141 MiB** (~4,286 MiB) | — |
| **Total VRAM @ ctx=1024/Q8** | **4,309.8 MiB / 6,141 MiB** | — |
| **VRAM headroom @ ctx=512** | **+1,855.2 MiB** (~1.86 GB) | — |
| **Worst-case if all CPU expert pages resident** | — | **~13.869 GiB** |
| **Active working set per token (8/128 experts)** | — | **~862.6 MiB** |

**Configuration D Specific Metrics:**
- **CPU expert tensor footprint:** `14,471,921,664` bytes (**~13.478 GiB**).
- **Estimated activated CPU expert data per token:** $\frac{8}{128} \times 13.478\text{ GiB} =$ **~862.6 MiB** (`904,495,104` bytes).
- **CPU runtime overhead estimate:** **~400 MB** (private anonymous process commit).
- **Estimated VRAM usage at context 512:** **~4,286 MiB** (`4,285.8 MiB`).
- **Estimated VRAM headroom at context 512:** **~1,855 MiB** (ample safety buffer against allocation spikes).

---

### Configuration E — 36 CPU / 12 GPU Layers (`-ncmoe 36 -ngl 48`)

| Component | GPU VRAM | CPU RAM |
|---|---|---|
| Dense model + 12 GPU expert layers | **5,307.3 MiB** (est.) | — |
| CPU expert tensors (layers 0–35) | 0 MiB | **12.164 GiB** (mmap) |
| KV @ ctx=512, Q8_0 | **24.0 MiB** | — |
| KV @ ctx=1024, Q8_0 | **48.0 MiB** | — |
| Overhead | **300 MiB** | **400 MB** |
| **Total VRAM @ ctx=512/Q8** | **5,631.3 MiB / 6,141 MiB** | — |
| **VRAM headroom @ ctx=512** | **+509.7 MiB** (~510 MiB) | — |
| **Worst-case if all CPU expert pages resident** | — | **~12.555 GiB** |
| **Active working set per token** | — | **~778.5 MiB** |

---

## 5. Comparative Placement Matrix

| Config | CPU Expert Layers | GPU Expert Layers | Est. GPU Model VRAM | Est. Total VRAM (@512/Q8) | Est. VRAM Headroom | Active Working Set / Token | Worst-Case If All Expert Pages Resident | Status |
|---|---|---|---|---|---|---|---|---|
| **A** | 48 | 0 | 1,023 MiB | 1,347 MiB | +4,794 MiB | ~1,047 MiB | ~16.738 GiB | ❌ Infeasible (>15.65 GB RAM) |
| **B** | 44 | 4 | 2,517 MiB | 2,841 MiB | +3,300 MiB | ~953 MiB | ~15.279 GiB | ❌ High RAM Risk |
| **C** | 42 | 6 | 3,264 MiB | 3,588 MiB | +2,553 MiB | ~906 MiB | ~14.550 GiB | ⚠️ Marginal |
| **D** | **40** | **8** | **3,962 MiB** | **4,286 MiB** | **+1,855 MiB** | **~862.6 MiB** | **~13.869 GiB** | ✅ **Selected for Phase 2C-B** |
| **E** | 36 | 12 | 5,307 MiB | 5,631 MiB | +510 MiB | ~778.5 MiB | ~12.555 GiB | ⚠️ High VRAM Risk |

---

## 6. KV Cache Budget Reference

| Context Tokens | KV Quant | Bytes | MiB |
|---|---|---|---|
| **512** | **Q8_0 (1 byte)** | **25,165,824** | **24.0 MiB** |
| 1,024 | Q8_0 (1 byte) | 50,331,648 | 48.0 MiB |
| 2,048 | Q8_0 (1 byte) | 100,663,296 | 96.0 MiB |
| 512 | F16 (2 bytes) | 50,331,648 | 48.0 MiB |
| 1,024 | F16 (2 bytes) | 100,663,296 | 96.0 MiB |
| 2,048 | F16 (2 bytes) | 201,326,592 | 192.0 MiB |

Formula: $\text{tokens} \times 48\text{ layers} \times 4\text{ KV heads} \times 128\text{ head dim} \times 2\text{ (K+V)} \times \text{bytes per element}$.
Using context 512 with `Q8_0` KV cache requires only **24.0 MiB**, reserving maximum VRAM headroom for compute scratch and GPU expert weights.
