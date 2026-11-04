# NOVA Compute — Phase 2C-A: Initial Inference Configuration Recommendation

**Date:** 2026-11-04  
**Phase:** 2C-A — Memory Placement Engineering  
**Author:** Antigravity (Implementation Engineer)  
**Reviewer:** ChatGPT (Lead Architect)  
**Status:** AWAITING LEAD ARCHITECT APPROVAL BEFORE EXECUTION

---

## 1. User Preference vs. Revised Engineering Recommendation

> *"My initial preference is to evaluate all-CPU MoE placement (Config A) with GPU dense-layer offloading before trying the 36/12 hybrid arrangement."*

After completing the exact tensor inventory and memory placement analysis, **Config A (all-CPU-experts, `-cmoe`) is not feasible on this hardware:**

- Config A requires **~16.7 GB** estimated resident RAM for CPU expert tensors.
- Total physical RAM: **15.65 GB**.
- This exceeds the machine's physical RAM capacity by ~1.1 GB before any OS overhead, background processes, or llama.cpp allocations are counted.
- Attempting Config A will guarantee OS-level pagefile thrashing, likely producing OOM process termination rather than a usable baseline measurement.

**Engineering recommendation: Begin with Config D (40 CPU / 8 GPU layers).**

---

## 2. Recommended First Configuration: Config D

### Rationale

Config D provides the best safety margin that keeps CPU RAM within physical limits while giving the GPU enough expert layers to establish meaningful GPU–CPU co-execution telemetry:

| Factor | Config D | Config E (original) | Config A (user pref.) |
|---|---|---|---|
| CPU expert bytes | 13.478 GiB | 12.164 GiB | 16.348 GiB |
| Est. resident RAM | ~13.9 GB | ~12.6 GB | ~16.7 GB |
| Feasible w/ apps closed? | ✅ Likely | ✅ Yes | ❌ Exceeds physical RAM |
| GPU expert layers | 8 | 12 | 0 |
| VRAM total @ctx=512/Q8 | ~4,286 MiB | ~5,631 MiB | 1,347 MiB |
| VRAM headroom @ctx=512 | ~1,855 MiB | **~510 MiB** | ~4,794 MiB |

Config E has only ~510 MiB VRAM headroom at ctx=512 — the 300 MiB overhead estimate is conservative and may be exceeded for a 30B model. Config D provides ~1,855 MiB headroom, dramatically reducing VRAM OOM risk.

---

## 3. Pre-Execution Prerequisites

**Before the first inference attempt is authorized and attempted, the user MUST:**

1. **Close Microsoft Edge** (~914 MB, largest background consumer).
2. **Close any other non-essential applications** (SQL Server, Telegram, etc.) to bring available RAM to at least **13.0 GB free** (target for Config D).
3. Verify: `nvidia-smi` shows 0 compute applications, free VRAM ≥ 5,920 MiB.
4. Verify: Available physical RAM ≥ 13.0 GB in Task Manager before launching.

> [!IMPORTANT]
> These prerequisites must be satisfied **before** running llama-cli. The engineer will not close applications without user instruction.

---

## 4. Recommended First Inference CLI

```powershell
.\runtime\windows\llama-cpp\llama-cli.exe `
  -m .\models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf `
  -ngl 48 `
  -ncmoe 40 `
  -c 512 `
  -b 512 `
  -ub 64 `
  -ctk q8_0 `
  -ctv q8_0 `
  -fa auto `
  -lm mmap `
  -n 1 `
  --simple-io `
  --perf `
  -p "Hello"
```

### Flag-by-Flag Justification

| Flag | Value | Reason |
|---|---|---|
| `-ngl 48` | 48 | Offload all dense attention/router/global tensors to GPU. |
| `-ncmoe 40` | 40 | Keep layers 0–39 expert weights in CPU RAM (~13.5 GiB); layers 40–47 experts go to GPU. |
| `-c 512` | 512 | Minimum context. KV cache = 24 MiB (saves 24 MiB vs 1024 — meaningful for VRAM margin). |
| `-b 512` | 512 | Conservative logical batch size. |
| `-ub 64` | 64 | Minimal physical batch. Reduces compute buffer VRAM scratch allocation. |
| `-ctk q8_0` | q8_0 | 8-bit KV K cache. Halves KV VRAM vs F16. |
| `-ctv q8_0` | q8_0 | 8-bit KV V cache. |
| `-fa auto` | auto | FlashAttention auto-enables when available. Reduces VRAM activation spikes during attention computation. |
| `-lm mmap` | mmap | Memory-maps the GGUF. Expert pages are demand-paged from NVMe. Do NOT use `mlock`. |
| `-n 1` | 1 | Generate exactly one token. Establishes: does the model initialize without OOM? Does it produce a valid token? What are prefill and decode latencies? |
| `--simple-io` | — | Clean output, no ANSI codes. |
| `--perf` | — | Print prefill and decode timing breakdown. |
| `-p "Hello"` | "Hello" | Minimal single-token prompt. Minimizes prefill compute and mmap page faults during first run. |

---

## 5. Measurements to Collect After Authorization

When Phase 2C-B (initialization) is authorized and executed, record:

1. **Initialization duration** (time from process start to first token ready).
2. **Tensor placement log** (llama.cpp prints which tensors went to GPU vs CPU).
3. **Peak VRAM** after initialization (`nvidia-smi` during first token generation).
4. **Peak RAM** in Task Manager / Process Monitor during initialization.
5. **Prefill time** (prompt evaluation for "Hello" = 1 token).
6. **Decode time** (time-to-first-token for the generated token).
7. **Process exit code** (0 = success; non-zero = OOM or crash).
8. **Any CUDA error messages** (record verbatim).

---

## 6. Fallback Sequence

If Config D fails initialization (RAM OOM or VRAM OOM):

| Fallback | Action | Expected RAM |
|---|---|---|
| Step 1 | Reduce to `-ncmoe 42` | ~14.5 GB resident (may still OOM) |
| Step 2 | Reduce to `-ncmoe 44` | ~15.3 GB (exceeds free RAM; pagefile required) |
| Step 3 | Increase swap, reduce context to `256` | Last resort before declaring infeasible |

> [!NOTE]
> Config E (`-ncmoe 36`) is **not** recommended as the first experiment due to only ~510 MiB VRAM headroom. If Config D succeeds, Config E can be tested as a second experiment to increase GPU expert utilization and potentially improve decode speed.

---

## 7. Governance Phase Gate

**STOP.** This report is submitted for ChatGPT (Lead Architect) review.

Phase 2C-B (first model initialization and inference execution) requires:
1. ChatGPT's explicit written approval of the recommended configuration.
2. User confirmation that background applications have been closed.
3. Fresh hardware qualification confirming available RAM ≥ 13.0 GB.
