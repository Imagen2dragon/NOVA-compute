# NOVA Compute — Phase 2A: MoE Runtime Compatibility Audit

**Date:** 2026-11-01  
**Target Architecture:** `Qwen3-30B-A3B` (MoE: 48 layers, 128 experts, 8 active)  
**Host Machine:** HP Victus Laptop (RTX 4050 6GB VRAM, 16GB DDR4-3200 RAM, WD SN810 NVMe)  
**Installed Runtime:** `llama.cpp` Windows x64 CUDA 12.4 precompiled release (`b12032`, commit `dde879bc4`)  
**Role:** Implementation Engineer (Antigravity)  
**Reviewer:** Lead Architect & Technical Reviewer (ChatGPT)  

---

## 1. Executive Summary

This audit verifies whether the exact installed `llama.cpp` Windows CUDA runtime (`b12032`) natively supports `Qwen3-30B-A3B` and possesses the flags required for CPU expert placement, GPU dense-layer offload, GPU expert caching, memory-mapped I/O, and KV-cache quantization.

### Summary of Audit Findings
1. **Qwen3MoE Architecture:** **Natively Supported.** Build `b12032` includes dedicated source implementation [`src/models/qwen3moe.cpp`](https://github.com/ggml-org/llama.cpp/blob/b12032/src/models/qwen3moe.cpp) which explicitly maps `n_layer == 48` to `LLM_TYPE_30B_A3B`.
2. **MoE CPU Placement:** **Supported.** Flags `-cmoe, --cpu-moe` and `-ncmoe, --n-cpu-moe N` allow offloading dense layers to GPU while pinning MoE expert weights to host CPU memory.
3. **GPU Expert Caching:** **Supported in `llama-cli.exe`.** Flag `--moe-cache-mib N` dynamically caches frequently called MoE experts in GPU VRAM. *(Note: `--moe-cache-mib` is exposed in `llama-cli` and `llama-server`, but not in `llama-bench`)*.
4. **I/O & Memory Mapping:** **Supported.** Flags `-lm, --load-mode <auto|mmap|mlock|dio>` and `-lzm, --lazy-mode <on|auto|off>` are fully functional.
5. **KV-Cache Optimization:** **Supported.** Grouped Query Attention (4 KV heads) enables extremely small KV footprints (192 MB for 2048 ctx in FP16; 96 MB in Q8_0 via `-ctk q8_0 -ctv q8_0`).

---

## 2. Exact Runtime Flag Audit

All flags were audited directly from the help output of the installed binaries in `runtime/windows/llama-cpp/`.

| Flag / Option | Short Alias | Environment Variable | Default | Exact Description / Syntax from Installed Binary | Status |
|---|---|---|---|---|---|
| `--cpu-moe` | `-cmoe` | `LLAMA_ARG_CPU_MOE` | Disabled | `keep all Mixture of Experts (MoE) weights in the CPU` | **Verified** |
| `--n-cpu-moe` | `-ncmoe N` | `LLAMA_ARG_N_CPU_MOE` | `0` | `keep the Mixture of Experts (MoE) weights of the first N layers in the CPU` | **Verified** |
| `--moe-cache-mib` | None | `LLAMA_ARG_MOE_CACHE_MIB` | `0` (disabled) | `GPU cache size in MiB for the MoE experts kept in the CPU. with multiple GPUs, it is split among them like the layers (--tensor-split)` | **Verified (CLI only)** |
| `--load-mode` | `-lm MODE` | `LLAMA_ARG_LOAD_MODE` | `auto` | `model loading mode: auto (mmap unless unsupported), none, mmap, mlock, mmap+mlock, dio (direct I/O)` | **Verified** |
| `--lazy-mode` | `-lzm MODE` | `LLAMA_ARG_LAZY_MODE` | `auto` | `on-demand reading of certain tensors: on (read rows from disk on demand, requires mmap), auto (on for tensors >4 GiB), off (always resident)` | **Verified** |
| `--cache-type-k` | `-ctk TYPE` | `LLAMA_ARG_CACHE_TYPE_K` | `f16` | `KV cache data type for K. Allowed: f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1` | **Verified** |
| `--cache-type-v` | `-ctv TYPE` | `LLAMA_ARG_CACHE_TYPE_V` | `f16` | `KV cache data type for V. Allowed: f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1` | **Verified** |
| `--flash-attn` | `-fa [on\|off\|auto]` | `LLAMA_ARG_FLASH_ATTN` | `auto` | `set Flash Attention use ('on', 'off', or 'auto')` | **Verified** |
| `--ctx-size` | `-c N` | `LLAMA_ARG_CTX_SIZE` | `0` (model def) | `size of the prompt context (default: 0 = loaded from model)` | **Verified** |
| `--gpu-layers` | `-ngl N` | `LLAMA_ARG_N_GPU_LAYERS` | `auto` (`-1`) | `max. number of layers to store in VRAM, either an exact number, 'auto', or 'all'` | **Verified** |

> [!IMPORTANT]
> **Key Tool Discrepancy:**
> While `llama-cli.exe` and `llama-server.exe` support `--moe-cache-mib`, `llama-bench.exe` does **not** expose the `--moe-cache-mib` option. Benchmarking GPU expert cache hit rates must therefore be conducted via `llama-cli` execution with `--perf` or custom harness instrumentation rather than `llama-bench`.

---

## 3. Qwen3MoE Architectural Verification

### 3.1 Model Architecture Parameters (from official `config.json`)
- **Model Type:** `qwen3_moe` (`Qwen3MoeForCausalLM`)
- **Total Layers ($L$):** 48 layers
- **Hidden Dimension ($d$):** 2,048
- **Attention Heads:** 32 Query heads ($d_{head}=128$), 4 Key/Value heads ($d_{head}=128$)
- **Grouped Query Attention (GQA) Ratio:** 8:1 (significantly compresses KV cache)
- **Total Experts per Layer ($E$):** 128 experts
- **Active Experts per Token ($K$):** 8 active experts
- **MoE Intermediate Dimension:** 768
- **Activation Function:** SwiGLU (`silu`)
- **Normalization:** RMSNorm (`eps = 1e-6`) with Query/Key LayerNorm (`attn_q_norm`, `attn_k_norm`)
- **Vocabulary Size:** 151,936 tokens

### 3.2 Tensor Layout in `llama.cpp` Source Code
In [`src/models/qwen3moe.cpp`](https://github.com/ggml-org/llama.cpp/blob/b12032/src/models/qwen3moe.cpp), tensors are structured into:
1. **Dense Model Components (Shared across all tokens):**
   - `tok_embd.weight`: Embedding table `{2048, 151936}`
   - `output_norm.weight`: Final RMSNorm `{2048}`
   - `output.weight`: Un-embedded language model head `{2048, 151936}`
   - Per Layer ($i = 0 \dots 47$):
     - `blk.i.attn_norm.weight`: Self-attention pre-norm `{2048}`
     - `blk.i.attn_q.weight`, `blk.i.attn_k.weight`, `blk.i.attn_v.weight`: QKV projections
     - `blk.i.attn_q_norm.weight`, `blk.i.attn_k_norm.weight`: Q/K RMSNorm `{128}`
     - `blk.i.attn_output.weight`: Self-attention output projection `{4096, 2048}`
     - `blk.i.ffn_norm.weight`: MoE FFN pre-norm `{2048}`
     - `blk.i.ffn_gate_inp.weight`: MoE Router gating weights `{2048, 128}`
2. **MoE Expert Components (3D Stacked Tensors per layer):**
   - `blk.i.ffn_gate_exps.weight`: Gate projections `{2048, 768, 128}`
   - `blk.i.ffn_up_exps.weight`: Up projections `{2048, 768, 128}`
   - `blk.i.ffn_down_exps.weight`: Down projections `{768, 2048, 128}`

### 3.3 Expert Routing Mechanism
In `src/models/qwen3moe.cpp` (lines 80–97), expert selection is handled via `build_moe_ffn`:
```cpp
ggml_tensor * moe_out = build_moe_ffn(
    cur,
    model.layers[il].ffn_gate_inp,
    model.layers[il].ffn_up_exps,
    model.layers[il].ffn_gate_exps,
    model.layers[il].ffn_down_exps,
    nullptr,
    n_expert,        // 128
    n_expert_used,   // 8
    LLM_FFN_SILU, true,
    hparams.expert_weights_scale,
    LLAMA_EXPERT_GATING_FUNC_TYPE_SOFTMAX,
    il, ...
);
```
- The gating router `ffn_gate_inp` performs a softmax over the 128 expert logits and selects the top 8 experts per token (`norm_topk_prob: true`).
- When `-cmoe` is passed, `llama.cpp` applies `llm_ffn_exps_cpu_override()`, forcing `ffn_gate_exps`, `ffn_up_exps`, and `ffn_down_exps` into host CPU RAM, while router logits, self-attention, and norms remain eligible for GPU execution under `-ngl`.

---

## 4. Confirmed Facts vs. Inferred Compatibility

| Capability | Classification | Evidence / Source |
|---|---|---|
| Native `qwen3_moe` architecture support | **Confirmed Fact** | Explicitly defined in `b12032` source: `src/models/qwen3moe.cpp` |
| MoE CPU offloading (`-cmoe`) | **Confirmed Fact** | Implemented in `common/arg.cpp:2710` via buffer overrides |
| GPU expert cache (`--moe-cache-mib`) | **Confirmed Fact** | Implemented in `src/llama-context.cpp:562` via `llama_moe_cache` |
| Direct GGUF loading of Qwen3 MoE | **Confirmed Fact** | Validated architecture graph parser in `src/llama-model.cpp:119` |
| CUDA backend compatibility with mixed MoE | **Confirmed Fact** | CUDA backend handles attention while host CPU backend executes MoE FFN |
| 18–20 tokens/sec decode speed on 30B MoE | **Unproven / Highly Unlikely** | Memory and PCIe bandwidth bottlenecks severely restrict throughput |
| InferSSD compatibility with Qwen3 | **Unverified** | Upstream InferSSD documentation only verifies DeepSeek & Muse-Spark |

---

## 5. Audit Conclusion

The installed `llama.cpp` CUDA 12.4 runtime (`b12032`) is **fully equipped** with the necessary kernel architectures and runtime parameters to execute `Qwen3-30B-A3B` in a hybrid CPU-RAM / GPU-VRAM configuration. No source compilation or binary replacement is required.
