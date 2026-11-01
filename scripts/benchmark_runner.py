"""
NOVA Compute - Phase 1 Benchmark Runner
Executes reproducible CPU and CUDA benchmarks using llama-bench and saves structured JSON reports.
"""

import subprocess
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LLAMA_BENCH = os.path.join(BASE_DIR, "runtime", "windows", "llama-cpp", "llama-bench.exe")
MODEL_PATH = os.path.join(BASE_DIR, "models", "qwen3-1.7b", "Qwen3-1.7B-Q8_0.gguf")
REPORTS_DIR = os.path.join(BASE_DIR, "reports", "phase1", "runtime")

os.makedirs(REPORTS_DIR, exist_ok=True)

def run_bench(backend_name, ngl, out_filename):
    print(f"\n==========================================")
    print(f"Running {backend_name} Benchmark (ngl={ngl})...")
    print(f"==========================================")
    
    cmd = [
        LLAMA_BENCH,
        "-m", MODEL_PATH,
        "-ngl", str(ngl),
        "-p", "512",
        "-n", "128",
        "-r", "5",
        "-o", "json"
    ]
    
    print("Command:", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE_DIR)
    
    if p.returncode != 0:
        print(f"ERROR: {backend_name} benchmark failed with return code {p.returncode}")
        print("STDERR:\n", p.stderr)
        sys.exit(p.returncode)
        
    try:
        data = json.loads(p.stdout)
    except Exception as e:
        print("Failed to parse JSON output. Raw stdout:\n", p.stdout)
        raise e
        
    out_path = os.path.join(REPORTS_DIR, out_filename)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        
    print(f"Successfully saved {out_filename}")
    print("\nBenchmark Summary:")
    for entry in data:
        n_prompt = entry.get("n_prompt")
        n_gen = entry.get("n_gen")
        avg_ts = entry.get("avg_ts", 0.0)
        std_ts = entry.get("stddev_ts", 0.0)
        print(f"  Prompt: {n_prompt:4d} tokens | Gen: {n_gen:4d} tokens | Speed: {avg_ts:7.2f} +/- {std_ts:5.2f} t/s")
        
    return data

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    
    if mode in ("cpu", "all"):
        run_bench("CPU", 0, "cpu-benchmark.json")
        
    if mode in ("cuda", "all"):
        run_bench("CUDA", 99, "cuda-benchmark.json")
