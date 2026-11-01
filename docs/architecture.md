# NOVA Compute architecture (v0.1)

## Design objective

Build a local inference service that can use GPU VRAM, system RAM and NVMe storage as managed tiers. The first implementation should reuse mature runtimes and instrument them. Custom expert paging/prefetching is a later research task.

## Hardware baseline (reported; verify)

| Resource | Reported value | Status |
|---|---:|---|
| GPU | NVIDIA RTX 4050 Laptop | 6 GB VRAM; verify exact device/driver |
| System RAM | 16 GB DDR4 | verify usable capacity/speed |
| Storage | 1 TB | interface, model and free space unknown |
| OS | Windows + WSL used previously | confirm current default environment |

## Model topology

### Primary generalist

Candidate: `Qwen/Qwen3-30B-A3B` (MoE, ~30.5B total / ~3.3B active).

Role: coding, reasoning, mathematics, engineering explanations and general planning.

Quantization experiments: Q3_K_M first if required by memory; compare Q4_K_M only after the baseline proves stable. Keep context small at the beginning (2K–4K) to limit KV-cache use.

### Small specialist

Candidate: `Qwen/Qwen3-1.7B` (Apache-2.0).

Roles: engineering or marketing specialist behavior. Begin with retrieval-augmented generation (RAG) and task-specific prompts. Add LoRA/QLoRA adapters only after preparing clean train/validation/test splits and a benchmark suite.

Use separate adapters such as `engineering-v1` and `marketing-v1`. The base model's parameter count remains fixed; adapter weights add task-specific trainable parameters. Genuine architecture expansion (new experts/layers) is a separate future research direction.

## Runtime strategy

1. **Baseline:** llama.cpp with CUDA, GGUF, short context, one request at a time.
2. **MoE placement:** test CPU expert placement and GPU layer/expert caching supported by the selected llama.cpp build.
3. **Storage tier:** measure mmap/page-cache behavior. Only then compare with InferSSD. InferSSD v0.1.0 officially lists tested DeepSeek-V4.1-Flash-1017 and Muse-Spark-Open models; Qwen3 compatibility is not assumed.
4. **Custom work:** implement telemetry, expert/cache instrumentation and request scheduling before changing low-level kernels.
5. **Network access:** expose an OpenAI-compatible local API only after single-machine inference is stable. Wireless remote GPU is a later phase and does not increase the laptop's local VRAM.

## Performance goals

- Record actual tokens/s with fixed prompt/context/output length.
- 18–20 tokens/s is a stretch target for a useful target model, not a guaranteed outcome for the 30B MoE model on this machine.
- Report prefill and decode separately. Do not mix cold-start results with warm-cache results.
- Track quality with repeatable coding, math and engineering tasks; speed alone is not success.

## Safety and operating limits

- Never disable Windows pagefile or Linux swap before measuring memory behavior.
- Never run destructive disk, partition or driver commands as part of baseline setup.
- Do not fill the system SSD with model files. Confirm free space and SSD type first.
- Keep a copy of exact model source, quantization, runtime commit and command line for each benchmark.
