# NOVA Compute — Antigravity-ready Starter Pack

**Status:** Phase 0 (Hardware Qualification), Phase 1 (Small-Model Runtime Baseline), & Phase 2B (Q4_K_M Acquisition) Complete | Phase 2C-B Final Stabilization & Adaptive Memory Reclaimer (AMR) Implemented & Verified (33 Unit Tests Passing; Awaiting Lead Architect Review; Model Inference Gated)

NOVA Compute is a resource-aware local inference platform for MoE LLMs and small domain-specialist models. The first reported target machine is an NVIDIA RTX 4050 Laptop GPU (6 GB VRAM) with 16 GB system RAM.

### Active Model Selection (Phase 2B / Phase 2C)
- **Primary Generalist:** `Qwen3-30B-A3B` in **`Q4_K_M`** quantization (18.63 GB, repository: `bartowski/Qwen_Qwen3-30B-A3B-GGUF`, pinned revision: `fb6ccf61e476d3077e10333e98e794f960ceea2a`). Verified SHA256 integrity. Controlled single-token inference under Configuration D is staged behind the Resource Steward safety gates.
- **Specialist Base:** `Qwen3-1.7B` in `Q8_0` quantization (1.83 GB, verified in Phase 1 at 83.6 t/s on CUDA).

## Team workflow

- **Antigravity:** builder. Inspects the repository, implements approved tasks, executes safe tests, and reports evidence.
- **ChatGPT:** engineering lead. Owns architecture guidance, algorithm design, technical research, code review, benchmark interpretation, and approval recommendations.
- **User:** project owner and final approver for system changes, installations, downloads, and architectural decisions.

Antigravity should load `AGENTS.md` as persistent workspace rules. Start with `docs/ANTIGRAVITY_FIRST_TASK.md`.

## First goals

1. Establish a trustworthy baseline on the actual laptop.
2. Validate a small-model runtime before attempting Qwen3-30B-A3B.
3. Measure cold-start, warm decode, prefill, RAM, VRAM, SSD traffic, temperature, power, and stability.
4. Evaluate 18–20 tokens/s as a stretch performance objective, not a promise.
5. Add a Qwen3-1.7B specialist using RAG first and LoRA/QLoRA only after creating curated data and a reliable evaluation suite.

## Important constraints

- MoE active parameter count is not total model storage. All experts must exist in some storage/memory tier; only a subset activates per token.
- SSD-backed inference may reduce the required resident memory footprint but does not guarantee speed. Storage latency, RAM pressure, cache hit rate, CPU performance, data transfer, and GPU utilization all matter.
- With 16 GB system RAM, a 30B MoE experiment will be tight. The OS, desktop, WSL, KV cache, and runtime buffers consume memory too.
- Do not download a 400+ GB frontier model during the first phases.
- Do not assume InferSSD supports Qwen3-30B-A3B. Verify current compatibility in source/docs and by a controlled test.
- Do not expect two large models to run concurrently on this machine. Start with managed loading/unloading and model routing.

## Folder structure

- `AGENTS.md` — persistent Antigravity project rules and safety constraints
- `nova/` — core NOVA platform subsystems (including `resource_steward`)
- `tests/` — automated unit and safety verification test suites
- `docs/ANTIGRAVITY_FIRST_TASK.md` — first builder task (hardware qualification only)
- `docs/architecture.md` — system design and decisions
- `docs/experiments.md` — benchmark protocol and result template
- `configs/models.yaml` — candidate models and expected roles
- `scripts/windows/collect-hardware.ps1` — Windows inventory script
- `scripts/linux/collect-hardware.sh` — Linux/WSL inventory script
- `scripts/run_inference_with_telemetry.py` — gated execution harness with fail-closed telemetry

## First action

Open this folder as an Antigravity workspace. Ask the agent to read `AGENTS.md` and complete `docs/ANTIGRAVITY_FIRST_TASK.md`. Do not install drivers, inference runtimes, or download model weights yet. Review the hardware report before proceeding.
