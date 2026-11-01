# Antigravity Task 00 — Repository review and hardware qualification

You are the builder for NOVA Compute. Read `AGENTS.md`, `README.md`, `docs/architecture.md`, `docs/experiments.md`, `configs/models.yaml`, and both diagnostic scripts before doing anything else.

## Objective

Establish a trustworthy baseline for the user's actual machine before installing software or downloading models.

## Allowed actions in this task

1. Inspect the repository and summarize its current architecture and phase plan.
2. Review `scripts/windows/collect-hardware.ps1` and `scripts/linux/collect-hardware.sh` line by line. Confirm whether they are read-only and identify any privacy considerations.
3. Run only safe, read-only hardware queries if the corresponding environment is available. The supplied scripts are intended to collect hardware/software inventory; do not modify the OS.
4. Save command output in `reports/hardware/` as timestamped text files, excluding secrets and personal files. Do not upload or transmit reports.
5. If Windows and WSL are both available, collect both reports. If a command is unavailable, record it as unavailable rather than installing it.
6. Summarize exact known values and distinguish them from assumptions.

## Explicitly prohibited in this task

- Do not download any model weights.
- Do not install drivers, CUDA, Python packages, inference runtimes, or system dependencies.
- Do not compile llama.cpp or InferSSD.
- Do not change WSL settings, swap, pagefile, registry, partitions, firmware, or system services.
- Do not run administrator or `sudo` commands.
- Do not start model training or inference benchmarks.
- Do not replace Qwen3-30B-A3B or change the model strategy without proposing the reason and waiting for approval.

## Required report

Create `reports/hardware/BASELINE.md` with:

- CPU model, physical/logical cores, and relevant instruction-set information if available.
- Installed and available RAM; memory-module layout, speed, and channel information if detectable (mark unknown if not).
- GPU model, dedicated VRAM, driver version, CUDA visibility, and `nvidia-smi` output if available.
- SSD model, bus/interface, capacity, health status, and free space. Do not claim PCIe generation unless detected reliably.
- Windows version, WSL version/distributions, Linux distribution/kernel, cgroup mode, and systemd status where relevant.
- Any missing tools or inaccessible data.
- A resource feasibility assessment for testing a 1.5B–4B baseline model and then Qwen3-30B-A3B. Treat the 30B test as potentially memory-constrained on 16 GB RAM.
- A clear recommendation for the next phase, with the smallest safe set of actions and estimated disk/RAM/VRAM impact. Estimates must be labeled as estimates.

Do not invent results. If the agent cannot access the machine or cannot run a command, explain what the user should run manually. Stop after producing this report and wait for review before installing or downloading anything.
