# NOVA Compute — Phase 2C-A: Tensor Inventory Report

**Date:** 2026-11-04  
**Artifact:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` (18,632,184,480 bytes, SHA256 verified)  
**Script:** `scripts/inspect_gguf_metadata.py` (Phase 2C-A extended version)  
**Validation:** 0 issues — no offsets out-of-bounds, no overlapping tensor ranges

---

## 1. File Structure

| Attribute | Value |
|---|---|
| GGUF Version | 3 |
| Total Top-Level Tensor Records | **579** |
| Metadata KV Pairs | 41 |
| Header Size | 5,970,560 bytes (~5.70 MB) |
| Data Section Start (after alignment) | byte **5,970,592** |
| Calculated Tensor Data | 18,626,213,888 bytes (17.347 GiB) |
| Actual Data Section | 18,626,213,888 bytes (17.347 GiB) |
| Alignment Padding in Data Section | **0 bytes** |
| File Size | 18,632,184,480 bytes (17.353 GiB) |
| Difference (header + alignment) | 5,970,592 bytes |

---

## 2. Architecture Metadata (from GGUF KV)

| Key | Value |
|---|---|
| `general.architecture` | `qwen3moe` |
| `qwen3moe.block_count` | **48** layers |
| `qwen3moe.context_length` | 32,768 tokens |
| `qwen3moe.embedding_length` | 2,048 |
| `qwen3moe.feed_forward_length` | 6,144 (dense shared FFN, unused in pure-MoE layers) |
| `qwen3moe.expert_feed_forward_length` | **768** (per individual expert's inner dim) |
| `qwen3moe.expert_count` | **128** experts per layer |
| `qwen3moe.expert_used_count` | **8** experts activated per token per layer |
| `qwen3moe.attention.head_count` | 32 (GQA query heads) |
| `qwen3moe.attention.head_count_kv` | 4 (GQA key-value heads) |
| `qwen3moe.attention.key_length` | 128 |
| `qwen3moe.attention.value_length` | 128 |
| `quantize.imatrix.entries_count` | 384 |
| `quantize.imatrix.chunks_count` | 209 |

---

## 3. Quantization Type Breakdown

> [!NOTE]
> Bytes computed using exact GGML block definitions (Q4_K: 256 elements / 144 bytes; Q6_K: 256 elements / 210 bytes; Q5_K: 256 elements / 176 bytes; Q8_0: 32 elements / 34 bytes; F32: 1 element / 4 bytes).

| Type | Tensor Count | Parameters | On-Disk Bytes | % of Total |
|---|---|---|---|---|
| **Q4_K** | 193 | 24,898,174,976 | 14,005,223,424 | **75.2%** |
| **Q6_K** | 49 | 5,168,168,960 | 4,239,513,600 | **22.8%** |
| **Q5_K** | 48 | 402,653,184 | 276,824,064 | 1.5% |
| **Q8_0** | 48 | 50,331,648 | 53,477,376 | 0.3% |
| **F32** | 241 | 12,793,856 | 51,175,424 | 0.3% |
| **Total** | **579** | **30,532,122,624** | **18,626,213,888** | 100% |

---

## 4. Expert Tensor Structure: Why 579 Records, Not 6,144

> [!IMPORTANT]
> **Correction to Phase 2B report (Task 2):** A previous note stated "128 × 48 = 6,144 expert weight tensors stored individually." This was incorrect.
>
> **The GGUF file contains 579 top-level tensor records, not 6,144.**

### How Expert Weights Are Stored

Qwen3-MoE stores expert weights as **stacked 3-dimensional tensors** — not as one record per individual expert:

| Concept | Value | Explanation |
|---|---|---|
| Experts per layer | 128 | Defined in architecture |
| Layers | 48 | Transformer blocks |
| Stacked expert matrix types | 3 | gate_exps, up_exps, down_exps |
| **Top-level GGUF expert records** | **48 × 3 = 144** | One 3D tensor per (layer, matrix_type) |
| Elements in each record | 128 | The 3rd dimension indexes the 128 experts |
| Total conceptual expert slices | 128 × 48 × 3 = 18,432 | Indexable at runtime, not separate records |

**Stacked tensor dimensions (from actual GGUF):**

| Record Name Pattern | Dims | Type | Bytes/Record |
|---|---|---|---|
| `blk.N.ffn_gate_exps.weight` | `[2048, 768, 128]` | Q4_K | 113,246,208 |
| `blk.N.ffn_up_exps.weight` | `[2048, 768, 128]` | Q4_K | 113,246,208 |
| `blk.N.ffn_down_exps.weight` | `[768, 2048, 128]` | Q6_K | 165,150,720 |

The third dimension (128) is the **expert index**. At runtime llama.cpp selects 8 expert slices along this dimension by computing row offsets — it does not maintain 6,144 separate allocations.

---

## 5. Semantic Category Inventory

| Category | Count | On-Disk Bytes | % Total | Layer Range |
|---|---|---|---|---|
| `expert_down_stacked` | 48 | 6,681,526,272 | **35.9%** | layers 0–47 |
| `expert_gate_stacked` | 48 | 5,435,817,984 | **29.2%** | layers 0–47 |
| `expert_up_stacked` | 48 | 5,435,817,984 | **29.2%** | layers 0–47 |
| `attention_out_proj` | 48 | 276,824,064 | 1.5% | layers 0–47 |
| `output_head` | 1 | 255,252,480 | 1.4% | global |
| `attention_q_proj` | 48 | 226,492,416 | 1.2% | layers 0–47 |
| `embedding` | 1 | 175,030,272 | 0.9% | global |
| `attention_k_proj` | 48 | 53,477,376 | 0.3% | layers 0–47 |
| `router_gate` | 48 | 50,331,648 | 0.3% | layers 0–47 |
| `attention_v_proj` | 48 | 34,799,616 | 0.2% | layers 0–47 |
| `attention_norm` | 48 | 393,216 | 0.0% | layers 0–47 |
| `ffn_norm` | 48 | 393,216 | 0.0% | layers 0–47 |
| `attention_k_norm` | 48 | 24,576 | 0.0% | layers 0–47 |
| `attention_q_norm` | 48 | 24,576 | 0.0% | layers 0–47 |
| `output_norm` | 1 | 8,192 | 0.0% | global |

**Expert weights total: 17,553,162,240 bytes (16.348 GiB) = 94.2% of model**  
**Dense attention + router + global: 1,073,051,648 bytes (0.999 GiB) = 5.8% of model**

---

## 6. Representative Tensor Samples

### Expert Stacked Tensors
```
blk.0.ffn_gate_exps.weight  dims=[2048, 768, 128]  Q4_K  113,246,208 bytes  offset=607,910,912
blk.0.ffn_up_exps.weight    dims=[2048, 768, 128]  Q4_K  113,246,208 bytes  offset=722,213,888
blk.0.ffn_down_exps.weight  dims=[768, 2048, 128]  Q6_K  165,150,720 bytes  offset=442,760,192
```

### Attention Projections
```
blk.0.attn_q.weight         dims=[2048, 4096]  Q4_K    4,718,592 bytes  offset=437,180,928
blk.0.attn_k.weight         dims=[2048, 512]   Q8_0    1,114,112 bytes  offset=430,290,944
blk.0.attn_v.weight         dims=[2048, 512]   Q6_K      860,160 bytes  offset=441,900,032
blk.0.attn_output.weight    dims=[4096, 2048]  Q5_K    5,767,168 bytes  offset=431,413,760
```

### Router and Normalization
```
blk.0.ffn_gate_inp.weight   dims=[2048, 128]   F32     1,048,576 bytes  offset=721,157,120
blk.0.attn_norm.weight      dims=[2048]         F32         8,192 bytes  offset=431,405,568
blk.0.ffn_norm.weight       dims=[2048]         F32         8,192 bytes  offset=722,205,696
```

### Global Tensors
```
token_embd.weight           dims=[2048, 151936]  Q4_K  175,030,272 bytes  offset=255,260,672
output.weight               dims=[2048, 151936]  Q6_K  255,252,480 bytes  offset=0
output_norm.weight          dims=[2048]           F32       8,192 bytes  offset=255,252,480
```

---

## 7. Validation Results

| Check | Result |
|---|---|
| All tensor offsets within file bounds | ✅ PASS (0 overruns) |
| No overlapping tensor data ranges | ✅ PASS (0 overlaps) |
| Calculated storage == actual data section | ✅ PASS (exact match: 0 bytes padding) |
| File size = header + data section | ✅ PASS (5,970,592 + 18,626,213,888 = 18,632,184,480) |

> Full tensor list (579 entries) is in `reports/phase2c/TENSOR_PLACEMENT.json`.
