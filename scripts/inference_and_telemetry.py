"""
NOVA Compute - Phase 1 Real Inference & Telemetry Harness
Executes real inference questions on Qwen3-1.7B with GPU offloading and captures live telemetry.
"""

import subprocess
import time
import json
import os
import threading
import ctypes

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LLAMA_CLI = os.path.join(BASE_DIR, "runtime", "windows", "llama-cpp", "llama-cli.exe")
MODEL_PATH = os.path.join(BASE_DIR, "models", "qwen3-1.7b", "Qwen3-1.7B-Q8_0.gguf")
REPORTS_DIR = os.path.join(BASE_DIR, "reports", "phase1", "runtime")

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ('dwLength', ctypes.c_ulong),
        ('dwMemoryLoad', ctypes.c_ulong),
        ('ullTotalPhys', ctypes.c_ulonglong),
        ('ullAvailPhys', ctypes.c_ulonglong),
        ('ullTotalPageFile', ctypes.c_ulonglong),
        ('ullAvailPageFile', ctypes.c_ulonglong),
        ('ullTotalVirtual', ctypes.c_ulonglong),
        ('ullAvailVirtual', ctypes.c_ulonglong),
        ('sullAvailExtendedVirtual', ctypes.c_ulonglong),
    ]

def get_ram_mb():
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    total_mb = stat.ullTotalPhys // (1024 * 1024)
    avail_mb = stat.ullAvailPhys // (1024 * 1024)
    used_mb = total_mb - avail_mb
    return total_mb, used_mb, avail_mb

def get_gpu_telemetry():
    cmd = [
        "nvidia-smi",
        "--query-gpu=memory.used,memory.free,utilization.gpu,power.draw,temperature.gpu",
        "--format=csv,noheader,nounits"
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=True)
        parts = [x.strip() for x in p.stdout.strip().split(",")]
        return {
            "vram_used_mb": float(parts[0]),
            "vram_free_mb": float(parts[1]),
            "gpu_util_percent": float(parts[2]),
            "power_draw_w": float(parts[3]),
            "temperature_c": float(parts[4])
        }
    except Exception as e:
        return {"error": str(e)}

class TelemetryCollector:
    def __init__(self, interval=0.2):
        self.interval = interval
        self.stop_signal = False
        self.records = []
        self.thread = None

    def _loop(self):
        while not self.stop_signal:
            gpu = get_gpu_telemetry()
            tot_ram, used_ram, avail_ram = get_ram_mb()
            rec = {
                "timestamp": time.time(),
                "ram_total_mb": tot_ram,
                "ram_used_mb": used_ram,
                "ram_avail_mb": avail_ram,
                "gpu": gpu
            }
            self.records.append(rec)
            time.sleep(self.interval)

    def start(self):
        self.stop_signal = False
        self.records = []
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_signal = True
        if self.thread:
            self.thread.join(timeout=1.0)
        return self.records

def run_inference_question(title, prompt):
    print(f"\n=======================================================")
    print(f"Running Test: {title}")
    print(f"Prompt: {prompt}")
    print(f"=======================================================")
    
    cmd = [
        LLAMA_CLI,
        "-m", MODEL_PATH,
        "-ngl", "99",
        "-c", "2048",
        "-n", "128",
        "-p", prompt,
        "-st",
        "--simple-io",
        "--perf"
    ]
    
    collector = TelemetryCollector(interval=0.15)
    
    ram_before = get_ram_mb()
    gpu_before = get_gpu_telemetry()
    
    t0 = time.time()
    collector.start()
    
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE_DIR)
    
    t1 = time.time()
    telemetry_records = collector.stop()
    
    ram_after = get_ram_mb()
    gpu_after = get_gpu_telemetry()
    
    total_elapsed = t1 - t0
    
    stdout = p.stdout
    stderr = p.stderr
    
    # Peak stats from collector
    max_vram_used = max([r["gpu"].get("vram_used_mb", 0) for r in telemetry_records], default=gpu_after.get("vram_used_mb", 0))
    max_gpu_util = max([r["gpu"].get("gpu_util_percent", 0) for r in telemetry_records], default=0)
    max_power = max([r["gpu"].get("power_draw_w", 0) for r in telemetry_records], default=0)
    max_temp = max([r["gpu"].get("temperature_c", 0) for r in telemetry_records], default=0)
    max_ram_used = max([r.get("ram_used_mb", 0) for r in telemetry_records], default=ram_after[1])
    
    result = {
        "title": title,
        "prompt": prompt,
        "exit_code": p.returncode,
        "total_wall_time_s": round(total_elapsed, 3),
        "stdout": stdout,
        "stderr": stderr,
        "telemetry": {
            "ram_before_mb": {"total": ram_before[0], "used": ram_before[1], "avail": ram_before[2]},
            "ram_peak_mb": max_ram_used,
            "ram_after_mb": {"total": ram_after[0], "used": ram_after[1], "avail": ram_after[2]},
            "gpu_before": gpu_before,
            "gpu_peak": {
                "vram_used_mb": max_vram_used,
                "gpu_util_percent": max_gpu_util,
                "power_draw_w": max_power,
                "temperature_c": max_temp
            },
            "gpu_after": gpu_after
        }
    }
    
    print(f"Elapsed: {total_elapsed:.2f}s | Peak VRAM: {max_vram_used:.1f} MB | Peak GPU Util: {max_gpu_util:.1f}%")
    print("\n--- Model Output ---")
    print(stdout.strip())
    print("--------------------\n")
    return result

def main():
    os.makedirs(REPORTS_DIR, exist_ok=True)
    
    tests = [
        ("Coding Question", "Write a Python function to compute the moving average of a numeric list with window size k, and explain its time complexity."),
        ("Reasoning Question", "A box contains 3 red balls and 5 blue balls. If you draw two balls at random without replacement, what is the probability that both are blue? Show your step-by-step reasoning.")
    ]
    
    all_results = []
    for title, prompt in tests:
        res = run_inference_question(title, prompt)
        all_results.append(res)
        time.sleep(1.0)
        
    # Generate metadata report
    metadata = {
        "project": "NOVA Compute",
        "phase": 1,
        "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hardware": {
            "host_os": "Microsoft Windows 11 Home (10.0.26200)",
            "cpu": "13th Gen Intel(R) Core(TM) i7-13700H (14 cores / 20 threads)",
            "ram_installed_gb": 16.0,
            "gpu": "NVIDIA GeForce RTX 4050 Laptop GPU (6140 MiB VRAM)",
            "driver_version": "592.82",
            "cuda_driver_api": "13.1",
            "pcie_link": "PCIe Gen4 x8"
        },
        "runtime": {
            "name": "llama.cpp",
            "version": "0.8.0-dev",
            "build_number": 12032,
            "build_commit": "dde879bc4",
            "backend": "CUDA 12.4 (ggml-cuda.dll, cudart64_12.dll, cublas64_12.dll)",
            "cpu_fallback_backend": "ggml-cpu-alderlake.dll",
            "execution_mode": "native-windows-portable"
        },
        "model": {
            "repository": "Qwen/Qwen3-1.7B-GGUF",
            "file": "Qwen3-1.7B-Q8_0.gguf",
            "size_bytes": 1834426016,
            "sha256": "061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a",
            "architecture": "qwen3",
            "quantization": "Q8_0",
            "n_params": 1720574976,
            "block_count": 28,
            "context_length": 40960
        },
        "benchmarks": {
            "cpu_json_report": "reports/phase1/runtime/cpu-benchmark.json",
            "cuda_json_report": "reports/phase1/runtime/cuda-benchmark.json"
        },
        "real_inference_runs": all_results
    }
    
    meta_path = os.path.join(REPORTS_DIR, "runtime-metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        
    print(f"Saved runtime metadata and inference results to {meta_path}")

if __name__ == "__main__":
    main()
