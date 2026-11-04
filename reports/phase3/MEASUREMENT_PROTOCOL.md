# NOVA Compute — Phase 3A: Measurement Protocol
## Standardized Telemetry, Benchmarking, and Evaluation Methodology

**Date:** 2026-11-05  
**Document:** `reports/phase3/MEASUREMENT_PROTOCOL.md`  
**Status:** Design and Static Analysis Only (Phase 3A) — **No benchmarks executed**  
**Target Hardware:** HP Victus Laptop (RTX 4050 6GB GDDR6, 16GB DDR4-3200 RAM, WD SN810 NVMe)  
**Target Model:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` ($18.63\text{ GB}$)  
**Installed Runtime:** `llama.cpp` Windows x64 CUDA 12.4 (`b12032`)  
**Project Owner:** User  
**Lead Architect:** ChatGPT  
**Implementation Engineer:** Antigravity  

---

## 1. Objective and Scientific Principles

The objective of this protocol is to define reproducible, standardized measurement procedures for future authorized inference tasks. 

Every metric must be:
1. **Source-Identified:** Explicitly bound to an underlying OS API, hardware sensor, or runtime log output.
2. **Synchronized:** Telemetry samples are timestamped in UTC using monotonic high-resolution clocks.
3. **Statistically Sound:** Benchmarks must differentiate single-run smoke tests from multi-trial statistical throughput evaluations. Cold-cache states must be strictly separated from warm-cache states.
4. **Local and Transparent:** All metrics are written strictly to local structured JSON files. Zero telemetry is transmitted over network sockets.

---

## 2. Telemetry Metric Definitions & Acquisition Interfaces

| Category | Metric Name | Unit | Collection Interface / API | Sample Interval | Operational Meaning & Boundary Conditions |
|---|---|---|---|---|---|
| **System Memory** | `avail_phys_bytes` | Bytes / GiB | Win32 `GlobalMemoryStatusEx()` (`ullAvailPhys`) | 500 ms | Instantaneous unallocated or clean standby physical RAM. Preflight gate requires $\ge 10.0\text{ GiB}$. Critical abort if $<2.0\text{ GiB}$ for $>5\text{s}$. |
| **System Memory** | `total_phys_bytes` | Bytes / GiB | Win32 `GlobalMemoryStatusEx()` (`ullTotalPhys`) | Preflight | Physical RAM hardware capacity (16,802,582,528 bytes; 15.65 GB visible). |
| **System Memory** | `commit_pct` | Percent (%) | Win32 `GlobalMemoryStatusEx()` (`ullTotalPageFile - ullAvailPageFile`) | 500 ms | System-wide virtual commit charge relative to commit limit. Preflight gate requires $<90.0\%$. |
| **System Memory** | `system_cache_bytes` | Bytes / MiB | Win32 `GetSystemFileCacheSize()` / PerfCounters | 500 ms | RAM occupied by Windows file cache / standby clean pages. |
| **Process Memory** | `working_set_bytes` | Bytes / MB | Win32 `GetProcessMemoryInfo()` (`WorkingSetSize`) | 500 ms | Subset of virtual memory pages currently resident in physical RAM for the child process. |
| **Process Memory** | `peak_working_set` | Bytes / MB | Win32 `GetProcessMemoryInfo()` (`PeakWorkingSetSize`) | Final | Maximum physical memory footprint reached during execution. |
| **Process Memory** | `private_commit_bytes`| Bytes / MB | Win32 `GetProcessMemoryInfo()` (`PrivateUsage`) | 500 ms | Anonymous memory allocated by process (heap, stack, activations). Must not page to `pagefile.sys`. |
| **Process Memory** | `page_fault_count` | Integer | Win32 `GetProcessMemoryInfo()` (`PageFaultCount`) | 500 ms | Cumulative total of soft and hard page faults incurred by the process. |
| **Storage Activity**| `pages_input_per_sec` | Pages / sec | Windows NT Performance Counters (`Memory\Pages Input/sec`) | 500 ms | Hard page faults resolved by reading from disk across the entire OS. Sustained values $>100$ indicate thrashing. |
| **GPU Subsystem** | `vram_used_mib` | MiB | NVML `nvmlDeviceGetMemoryInfo()` (`used`) | 500 ms | Dedicated GDDR6 memory allocated by all processes. |
| **GPU Subsystem** | `vram_free_mib` | MiB | NVML `nvmlDeviceGetMemoryInfo()` (`free`) | 500 ms | Dedicated GDDR6 memory unallocated. Preflight gate requires $\ge 5,500\text{ MiB}$. Abort if $<512\text{ MiB}$. |
| **GPU Subsystem** | `gpu_utilization_pct`| Percent (%) | NVML `nvmlDeviceGetUtilizationRates()` (`gpu`) | 500 ms | Percentage of time the CUDA graphics and tensor engines were active. |
| **GPU Subsystem** | `gpu_temperature_c` | °C | NVML `nvmlDeviceGetTemperature()` | 500 ms | GPU die temperature. Monitored to detect thermal throttling (>80°C). |
| **GPU Subsystem** | `gpu_power_w` | Watts | NVML `nvmlDeviceGetPowerUsage()` | 500 ms | Instantaneous power draw of the AD107 GPU die and memory (TGP max: ~74W). |
| **CPU Subsystem** | `cpu_utilization_pct`| Percent (%) | WMI / PerfCounters (`Processor(_Total)\% Processor Time`) | 500 ms | Overall CPU load across all 14 cores (6P + 8E). |
| **CPU Subsystem** | `cpu_temperature_c` | °C | WMI `MSAcpi_ThermalZoneTemperature` | 500 ms | CPU package temperature. Monitored to detect thermal throttling (>95°C). |
| **Inference Timing**| `time_to_first_token`| ms | High-resolution monotonic timer (`time.perf_counter()`) | Per run | Elapsed time from command launch until the first output token is generated (TTFT). |
| **Inference Timing**| `prompt_eval_throughput`| tokens/sec | Parsed from `llama.cpp` `--perf` log output | Per run | Prompt processing throughput (PP t/s): $\frac{\text{prompt tokens}}{\text{prompt eval time}}$. |
| **Inference Timing**| `eval_throughput` | tokens/sec | Parsed from `llama.cpp` `--perf` log output | Per run | Text generation / decode throughput (TG t/s): $\frac{\text{generated tokens}}{\text{eval time}}$. |

---

## 3. Distinction Between Smoke Tests and Throughput Benchmarks

To maintain rigorous scientific validity, testing activities are strictly categorized into two operational modes:

### 3.1 Smoke Test Protocol
- **Purpose:** Verifies hardware gate satisfaction, binary execution, model tensor mapping, CUDA context initialization, memory allocation ceilings, and safe shutdown mechanics without incurring thermal strain or extended runtimes.
- **Execution Parameters:**
  - Token Prediction: Strictly **one token** (`-n 1`).
  - Context Window: 512 tokens (`-c 512`).
  - Standard Prompt: Short prompt (e.g. `-p "Hello"` or single sentence).
  - Iterations: Single execution.
- **Success Criteria:**
  - Process exits cleanly with code 0.
  - Preflight hardware gates passed before launch.
  - Zero CUDA Out-of-Memory errors in stdout/stderr.
  - Memory supervisor detects no critical memory thresholds breached.
  - Telemetry log written to JSON with valid sample sequences.
- **Non-Goal:** **A smoke test is never a throughput benchmark.** Reporting tokens/second from a 1-token run is invalid because startup and initial graph compile times dominate the measurement.

### 3.2 Throughput Benchmark Protocol
- **Purpose:** Measures true steady-state prompt evaluation throughput (PP t/s) and token generation decode throughput (TG t/s).
- **Execution Parameters:**
  - Standardized Prompt: A fixed, reproducible 64-token coding/reasoning prompt:
    ```
    Prompt: "Write a complete, thread-safe Python implementation of an asynchronous Least-Recently-Used (LRU) cache with a maximum capacity, TTL eviction, and unit tests."
    ```
  - Target Token Prediction: Strictly **128 tokens** (`-n 128`).
  - Context Window: Sized to test scenario (e.g. 512 or 2,048 tokens).
  - Repetition: **3 sequential trials** under identical machine and background conditions:
    * **Trial 1 (Cold Cache):** Executed immediately after launch or after purging temporary file cache pages. Measures initial NVMe read latency and cold expert routing.
    * **Trial 2 (Warm Cache Run 1):** Executed without reloading model mappings. Measures steady-state resident memory execution.
    * **Trial 3 (Warm Cache Run 2):** Confirms measurement repeatability and checks for thermal drift.
- **Metrics Computed Across Trials:**
  - Cold-cache decode speed (Trial 1 t/s).
  - Warm-cache decode speed (Mean of Trials 2 and 3 t/s).
  - Standard deviation ($\sigma$) and min/max range across warm trials.
  - Pre-run vs. post-run RAM and VRAM residency deltas to detect memory leaks.

---

## 4. Benchmark Output Parsing Specification

`llama.cpp` outputs performance metrics via the `--perf` flag to `stderr`. The benchmark harness must extract metrics using exact regular expression parsing:

```
llama_perf_context_print:        load time =     842.15 ms
llama_perf_context_print: prompt eval time =     125.40 ms /    64 tokens (    1.96 ms per token,   510.37 tokens per second)
llama_perf_context_print:        eval time =    8245.80 ms /   128 runs   (   64.42 ms per token,    15.52 tokens per second)
llama_perf_context_print:       total time =    9213.35 ms /   192 tokens
```

### Regular Expressions:
1. **Prompt Processing Throughput:**
   `r"prompt eval time\s*=\s*[\d\.]+\s*ms\s*/\s*\d+\s*tokens\s*\(\s*[\d\.]+\s*ms per token,\s*([\d\.]+)\s*tokens per second\)"`
2. **Decode Throughput (Text Generation):**
   `r"eval time\s*=\s*[\d\.]+\s*ms\s*/\s*\d+\s*runs\s*\(\s*[\d\.]+\s*ms per token,\s*([\d\.]+)\s*tokens per second\)"`
3. **Model Load Time:**
   `r"load time\s*=\s*([\d\.]+)\s*ms"`

---

## 5. Performance Governance & Target Clarification

### The 18–20 Tokens/Second Stretch Target:
- The project baseline defines 18–20 tokens/second as a **stretch research target**, not a promised capability.
- **Current Evidence:** Zero benchmark trials have been conducted on this laptop for `Qwen3-30B-A3B-Q4_K_M`. Any statement claiming that 18–20 tokens/second is already achieved or easily achievable on an RTX 4050 Laptop GPU with dual-channel DDR4 memory is unfounded.
- **Reporting Rule:** In all future reports, decode throughput must be reported with:
  1. Exact quantization (e.g. Q4_K_M).
  2. Exact placement flags (e.g. `-ngl 48 -ncmoe 40 --moe-cache-mib 0`).
  3. Distinction between Cold Cache and Warm Cache.
  4. Standard deviation across trials.
