# NOVA Compute — Phase 2B: Model Integrity and Preflight Audit

**Date:** 2026-11-02  
**Target Model:** `Qwen/Qwen3-30B-A3B`  
**Authorized Primary Artifact:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` *(supersedes Q3_K_M)*  
**Host Machine:** HP Victus Laptop (RTX 4050 6GB GDDR6, 16GB DDR4-3200 RAM, WD SN810 NVMe)  
**Role:** Implementation Engineer (Antigravity)  
**Reviewer:** Lead Architect & Technical Reviewer (ChatGPT)  
**Approver:** User (Project Owner)  

---

## 1. Governance & Model Selection Evolution

- **Original Candidate (Phase 2A Audit):** `Qwen_Qwen3-30B-A3B-Q3_K_M.gguf` (~14.08 GB).
- **Authorized Decision (Phase 2B):** The Lead Architect (ChatGPT) and Project Owner approved advancing to **`Q4_K_M`** (18.63 GB) as the primary generalist artifact to ensure higher reasoning fidelity and code generation accuracy.
- **Supersession Notice:** `Q3_K_M` is marked as superseded in [`configs/models.yaml`](../../configs/models.yaml). No download of `Q3_K_M` or any other quantization has been performed.
- **Scope Limit:** Phase 2B is strictly bounded to **model acquisition and cryptographic integrity verification**. No model initialization, execution, or inference will take place until Phase 2B verification is reviewed and ChatGPT issues the Phase 2C inference directive.

---

## 2. Remote Metadata & Cryptographic Integrity Target

Queried and verified against the Hugging Face repository API and Git LFS pointers:

| Metadata Attribute | Specification | Verification Source |
|---|---|---|
| **Hugging Face Repository** | [`bartowski/Qwen_Qwen3-30B-A3B-GGUF`](https://huggingface.co/bartowski/Qwen_Qwen3-30B-A3B-GGUF) | Official Bartowski GGUF quant repository |
| **Pinned Revision (Commit ID)** | `fb6ccf61e476d3077e10333e98e794f960ceea2a` | Pinned commit hash |
| **Exact Artifact Filename** | `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` | Exact binary name |
| **Destination Path** | `models/qwen3-30b/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf` | Local target directory |
| **Exact File Size (Bytes)** | **`18,632,184,480` bytes** (~18.63 GB / 17.35 GiB) | HTTP `Content-Length` & `Content-Range` |
| **Official Git LFS SHA256 (OID)** | `a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32` | Git LFS pointer OID |
| **Hugging Face Xet Hash** | `c1d94f7c5a14a24f6c637499fbff9ccf5102d312ff68d73a86a8e3f2761918ba` | Remote ETag header |
| **HTTP Range Resumption** | Verified (`Accept-Ranges: bytes`, Status `206`) | HTTP preflight range request |
| **GGUF Magic Header** | `b'GGUF'` (`0x46554747`) | First 4 bytes of stream verified |

---

## 3. Hardware Preflight Audit (Download Readiness)

Operating system memory, GPU, and NVMe storage were audited prior to initiating the streaming acquisition:

| Resource / Parameter | Measured Preflight Value | Requirement for Download | Status |
|---|---|---|---|
| **Available Physical RAM** | **3.29 GB** (3,451,576,320 bytes) | $\ge \mathbf{1.0\text{ GB}}$ ($\ge 750\text{ MB}$ abort gate) | ✅ **PASS** |
| **Dedicated VRAM** | **5.78 GB** free (5,918 MB free / 6,140 MB total) | Unutilized by downloader (0 MB VRAM) | ✅ **PASS** |
| **GPU Temperature** | **48.0°C – 53.0°C** | Normal idle thermal operating range | ✅ **PASS** |
| **SSD Free Storage (C:)** | **157.90 GB** free out of 952.57 GB | $\ge \mathbf{25.0\text{ GB}}$ ($\ge 10\text{ GB}$ abort gate) | ✅ **PASS** |
| **Windows Commit Limit** | 40.65 GB total (17.1 GB committed, 23.5 GB available) | Stable system commit pool | ✅ **PASS** |

---

## 4. Streaming Downloader Architecture & Safeguards

The download is implemented via [`scripts/download_model.py`](../../scripts/download_model.py) adhering to strict engineering constraints:

1. **Bounded Memory Footprint:** Streams in fixed 8 MiB buffers with unbuffered direct file writes (`buffering=0`), maintaining Python memory consumption below 25 MB throughout the entire transfer.
2. **Resumption via HTTP Range:** Downloads to a temporary `.part` file (`Qwen_Qwen3-30B-A3B-Q4_K_M.gguf.part`). In the event of network interruption, it automatically requests `Range: bytes={current_size}-` without losing progress.
3. **Resilience & Exponential Backoff:** Re-establishes dropped connections with up to 10 retries and progressive backoff, avoiding failure from transient DNS (`Errno 11001`) or remote CDN timeouts.
4. **Dynamic OS Memory Safety Gate:** Polling `GlobalMemoryStatusEx` on every chunk:
   - If available RAM falls below 1,000 MB, stream throttles for 5 seconds.
   - If available RAM falls below 750 MB, the process safely pauses and aborts to preserve system stability.
5. **SSD Exhaustion Gate:** Polling `GetDiskFreeSpaceExW` periodically; aborts if free space on `C:\` drops below 10.0 GB.
6. **Promotion & Validation Sequence:**
   - Byte count verification (`18,632,184,480` bytes).
   - Magic bytes verification (`b'GGUF'`).
   - Cryptographic SHA256 checksum verification across all 18.63 GB using streaming 16 MiB blocks.
   - Atomic rename from `.part` to `.gguf` only upon 100% cryptographic match.

---

## 5. Upcoming Phase 2C Execution Candidate

*Note: For architectural planning only. No execution authorized in Phase 2B.*

```powershell
.\runtime\windows\llama-cpp\llama-cli.exe `
  -m .\models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q4_K_M.gguf `
  -ngl 48 `
  -ncmoe 36 `
  -c 1024 `
  -b 128 `
  -ctk q8_0 `
  -ctv q8_0 `
  -fa on `
  -st `
  --simple-io `
  --perf `
  -n 32 `
  -p "Building NOVA Compute local inference engine"
```
