# Experiment protocol and result log

## Phase 0 — hardware qualification

Collect:
- exact CPU model and core count
- installed and usable RAM (and speed if available)
- GPU name, VRAM, driver and CUDA visibility
- SSD model/interface (NVMe/SATA), capacity and free space
- Windows build / Linux distribution / WSL version
- whether the environment sees `nvidia-smi`

## Phase 1 — runtime baseline

Start with a small 1.5B–4B GGUF model so the runtime and CUDA path can be validated without memory pressure. Record:
- model + quantization + file hash
- llama.cpp commit/build flags
- context length, batch size, number of GPU layers
- prompt tokens and generated tokens
- prefill tokens/s and decode tokens/s
- peak RAM, VRAM and SSD read throughput
- GPU temperature, power and clock behavior

## Phase 2 — Qwen3-30B-A3B feasibility

Try low context first. Use a known-good GGUF and verify the current llama.cpp command-line options with `llama-server --help` before using CPU MoE or expert-cache flags. Test Q3_K_M before Q4_K_M if capacity is tight.

Stop if the OS becomes unresponsive, sustained paging makes the machine unusable, or GPU/CPU thermals throttle heavily.

## Phase 3 — compare storage-backed inference

Only after baseline:
- inspect InferSSD v0.1.0 source and model compatibility
- test with a small, supported test model if available
- evaluate whether a local Qwen3 GGUF is accepted and produces correct outputs
- compare warm-cache and cold-cache decoding

## Phase 4 — specialist model

Start with Qwen3-1.7B. First use RAG over licensed, curated engineering or marketing documents. Then build a train/validation/test dataset and use QLoRA/LoRA. Measure task success and regression against the untuned model.

## Result template

| Field | Value |
|---|---|
| Date/time | |
| OS/runtime version | |
| Model / quant | |
| Context / prompt / output | |
| Decode tokens/s | |
| Prefill tokens/s | |
| Peak system RAM | |
| Peak VRAM | |
| SSD reads / cold vs warm | |
| Temperature / power | |
| Quality tests passed | |
| Notes / failure mode | |
