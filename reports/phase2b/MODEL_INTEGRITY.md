# NOVA Compute — Phase 2B: Model Integrity and Preflight Audit

**Date:** 2026-11-02  
**Target Model:** `Qwen/Qwen3-30B-A3B`  
**Selected Artifact:** `Qwen_Qwen3-30B-A3B-Q3_K_M.gguf`  
**Host Machine:** HP Victus Laptop (RTX 4050 6GB GDDR6, 16GB DDR4-3200 RAM, WD SN810 NVMe)  
**Role:** Implementation Engineer (Antigravity)  
**Reviewer:** Lead Architect & Technical Reviewer (ChatGPT)  
**Approver:** User (Project Owner)  

---

## 1. Hardware Preflight Audit

Preflight checks were executed via direct read-only operating system and GPU queries (`GlobalMemoryStatusEx` and `nvidia-smi`):

| Resource / Parameter | Measured Current Value | Requirement for Phase 2B | Preflight Status |
|---|---|---|---|
| **Available Physical RAM** | **1.37 GB** (1,471,099,904 bytes) | $\ge \mathbf{11.5\text{ GB}}$ | ❌ **FAIL (Deficit: ~10.13 GB)** |
| **Available Dedicated VRAM** | **5.78 GB** (5,918 MB free / 6,140 MB total) | $\ge \mathbf{5.2\text{ GB}}$ | ✅ **PASS** |
| **Active GPU Compute Apps** | **None** (`nvidia-smi` reports 0 active compute apps) | Zero competing compute | ✅ **PASS** |
| **GPU Temperature** | **53.0°C** | Normal idle operating range | ✅ **PASS** |
| **SSD Free Storage (C:)** | **159.59 GB** free out of 952.57 GB | $\ge \mathbf{25.0\text{ GB}}$ | ✅ **PASS** |
| **Windows Commit Limit** | 40.65 GB total (17.08 GB committed, 23.57 GB available) | Stable system commit pool | ✅ **PASS** |

### Top Background Process Memory Consumers
Active desktop applications are currently holding ~10 GB of physical RAM:
1. **Claude Desktop (`claude.exe`):** Multiple instances consuming ~1,830 MB RAM.
2. **Microsoft Edge (`msedge.exe`):** Multiple tabs/instances consuming ~1,980 MB RAM.
3. **Mozilla Firefox (`firefox.exe`):** Consuming ~575 MB RAM.
4. **Microsoft SQL Server (`sqlservr.exe`):** Consuming ~380 MB RAM (532 MB private commit).
5. **HP OMEN Services (`OmenCommandCenterBackground`, `OMENOverlay`):** Consuming ~650 MB RAM.
6. **Windows Explorer, Language Server, Antigravity IDE, OS background:** Consuming ~4,500 MB RAM.

> [!CAUTION]
> **Preflight Condition Unmet:**
> Available RAM is currently **1.37 GB**. Loading a 14 GB model under this condition would trigger catastrophic OS thrashing and Out-Of-Memory termination. Per strict Phase 2B rules: *"If requirements are not satisfied, stop."*
> Non-essential memory consumers (Edge, Firefox, Claude desktop, SQL Server) must be closed by the user to free $\ge 11.5\text{ GB}$ of physical RAM before proceeding.

---

## 2. Target Model Artifact Integrity Specifications

Metadata queried directly from Hugging Face API:

- **Repository:** [`bartowski/Qwen_Qwen3-30B-A3B-GGUF`](https://huggingface.co/bartowski/Qwen_Qwen3-30B-A3B-GGUF)
- **Repository Commit Revision:** `46f17e079cba70b04390bef39b57d2783e9fd015`
- **Artifact File Name:** `Qwen_Qwen3-30B-A3B-Q3_K_M.gguf`
- **Destination Path:** `models/qwen3-30b/Qwen_Qwen3-30B-A3B-Q3_K_M.gguf`
- **Exact File Size:** **14,076,803,744 bytes** (~14.08 GB / 13.11 GiB)
- **Official Git LFS SHA256 (OID):** `1f3792eec1bb8559bf6fb49fec159cd85bff97ec262c21b8b59a43e9e2b29859`
- **Hugging Face xetHash:** `3e4c9d67952fc75d39b772a911f66cd9d535517f38cff9445ea200b9271991dc`
- **Download Source URL:** `https://huggingface.co/bartowski/Qwen_Qwen3-30B-A3B-GGUF/resolve/main/Qwen_Qwen3-30B-A3B-Q3_K_M.gguf`

---

## 3. Verified First Inference Configuration (Task 4)

CLI syntax verified against installed `llama.cpp` binary (`b12032`):

```powershell
.\runtime\windows\llama-cpp\llama-cli.exe `
  -m .\models\qwen3-30b\Qwen_Qwen3-30B-A3B-Q3_K_M.gguf `
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
  -p "Your prompt here"
```

### Parameter Breakdown & Architectural Semantics:
- `-ngl 48`: Offloads all 48 dense attention layers, token embeddings, and output head to GPU VRAM.
- `-ncmoe 36`: Pin the MoE expert weights of the **first 36 layers (layers 0–35)** to CPU host RAM. The expert weights of the **last 12 layers (layers 36–47)** reside on GPU VRAM.
- `-c 1024`: Initial conservative context window (KV cache = 48 MB in Q8_0).
- `-b 128`: Logical batch size.
- `-ctk q8_0 -ctv q8_0`: Quantizes Key/Value cache to 8-bit, reducing VRAM footprint by 50%.
- `-fa on`: FlashAttention enabled for reduced VRAM activation spikes.
- GPU Expert Cache: Disabled (omitted `--moe-cache-mib`).

---

## 4. Current Status & Governance Action

- **Task 1 (Phase 2A Corrections):** Completed.
- **Task 2 (Hardware Preflight):** Completed. Available RAM is currently **1.37 GB** (Requirements not yet satisfied).
- **Task 3 (Download Preparation):** Metadata recorded. No download performed.
- **Action Required:**
  1. The user must close background applications (Claude desktop, Edge, Firefox, SQL Server) to bring available RAM $\ge 11.5\text{ GB}$.
  2. The user must provide explicit approval to begin the download of `Qwen_Qwen3-30B-A3B-Q3_K_M.gguf` (14.08 GB).
