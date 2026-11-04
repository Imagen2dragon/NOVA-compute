# NOVA Compute — Phase 2C-A: Runtime CLI Flag Audit

**Binary:** `runtime/windows/llama-cpp/llama-cli.exe`  
**Version:** b12032 (commit dde879bc4)  
**Help source:** `reports/phase2/llama-cli-help.txt` (captured Phase 2A)  
**Re-verified:** Phase 2C-A

---

## Verified Flag Syntax (b12032)

### GPU Offloading
| Flag | Long Form | Argument | Description |
|---|---|---|---|
| `-ngl N` | `--gpu-layers N` / `--n-gpu-layers N` | integer | Max layers in VRAM. Use `-ngl 48` to offload all 48 transformer layers. |

### MoE CPU Expert Placement
| Flag | Long Form | Argument | Description |
|---|---|---|---|
| `-cmoe` | `--cpu-moe` | (none) | Keep **all** MoE expert weights in CPU RAM. Dense layers still offloaded per `-ngl`. |
| `-ncmoe N` | `--n-cpu-moe N` | integer | Keep the first **N layers'** expert weights in CPU RAM. Remaining layers' experts go to GPU. |

> [!IMPORTANT]
> `-cmoe` and `-ncmoe` control **MoE expert weight placement only**. They are independent of `-ngl`. When using `-ngl 48 -ncmoe 36`, attention/router tensors for all 48 layers go to GPU; expert tensors for layers 0–35 go to CPU; expert tensors for layers 36–47 go to GPU.

### Context and Batching
| Flag | Long Form | Default | Description |
|---|---|---|---|
| `-c N` | `--ctx-size N` | 0 (from model) | Prompt context window size in tokens. Use `512` or `1024` for first experiment. |
| `-b N` | `--batch-size N` | 2048 | Logical maximum batch size (prefill). |
| `-ub N` | `--ubatch-size N` | 512 | Physical micro-batch size (actual CUDA batch per kernel call). |

> [!NOTE]
> `-ub` (micro-batch) directly affects VRAM compute buffer size. Use `-ub 64` or `-ub 128` for conservative first experiment to minimize scratch VRAM.

### KV Cache Type
| Flag | Long Form | Accepted Values | Description |
|---|---|---|---|
| `-ctk TYPE` | `--cache-type-k TYPE` | `f32`, `f16`, `q8_0`, `q4_0`, `q4_1`, `iq4_nl` | Quantization for K cache. Use `q8_0` to halve VRAM vs F16. |
| `-ctv TYPE` | `--cache-type-v TYPE` | same as above | Quantization for V cache. |

### Flash Attention
| Flag | Long Form | Accepted Values | Description |
|---|---|---|---|
| `-fa [on\|off\|auto]` | `--flash-attn` | `on`, `off`, `auto` (default: `auto`) | Enables FlashAttention CUDA kernel. `auto` enables when available. Use `auto` for first experiment. |

### Model Loading
| Flag | Long Form | Accepted Values | Description |
|---|---|---|---|
| `-lm MODE` | `--load-mode MODE` | `auto`, `mmap`, `mlock`, `mmap+mlock` | Default `auto` = mmap unless device unsupported. Use `mmap` explicitly for demand-paged CPU experts. Do **not** use `mlock` as it will attempt to pin 18.63 GB in RAM and fail. |
| `-lzm MODE` | `--lazy-mode MODE` | `auto`, `all` (default: `auto`) | On-demand reading of certain large tensors. `auto` activates for tensors > 4 GiB. |

### GPU Expert Cache (MoE)
| Flag | Argument | Description |
|---|---|---|
| `--moe-cache-mib N` | integer (MiB) | GPU cache for CPU-resident MoE experts (caches recently-used expert slices in VRAM). **Do not use in first experiment — disable by omitting this flag entirely.** |

### Inference Control
| Flag | Description |
|---|---|
| `-n N` | Number of tokens to generate. Use `-n 1` for first experiment. |
| `-p "TEXT"` | Prompt string. Use short prompt: `"Test"` or `"Hello"`. |
| `--simple-io` | Disables ANSI escape codes — required for clean log capture. |
| `--perf` | Print timing breakdown after generation. |
| `-st` | Print per-step timing during generation. |

---

## First Experiment Recommended CLI (see INITIAL_CONFIGURATION_RECOMMENDATION.md)

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

> [!CAUTION]
> Do not use `-lm mlock` or `-lm mmap+mlock` — both attempt to pin the full model in physical RAM. With 15.65 GB total RAM and an 18.63 GB model, `mlock` will fail or cause the OS to terminate the process.

---

## Flags That Were NOT Found in b12032

| Flag | Status |
|---|---|
| `--lazy-mode` | Present as `-lzm` in b12032 |
| `--load-mode` | Present as `-lm` in b12032 |
| `--moe-cache-mib` | Present in b12032 ✅ |
| `-cmoe` | Present in b12032 ✅ |
| `-ncmoe` | Present in b12032 ✅ |
