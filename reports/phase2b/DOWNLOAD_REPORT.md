# NOVA Compute — Phase 2B: Download and Verification Report

**Date:** 2026-11-04  
**Artifact:** `Qwen_Qwen3-30B-A3B-Q4_K_M.gguf`  
**Pinned Revision:** `fb6ccf61e476d3077e10333e98e794f960ceea2a`  
**Source:** `bartowski/Qwen_Qwen3-30B-A3B-GGUF` (Hugging Face)  
**Implemented by:** Antigravity (Implementation Engineer)  

---

## Acquisition Method

Model acquired via **direct browser download** from the authorized pinned URL. File was subsequently moved to the project destination directory:

```
models/qwen3-30b/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf
```

No `.part` file promotion was required (browser download wrote the complete file directly).

---

## Cryptographic Verification Results

| Verification Step | Expected | Measured | Result |
|---|---|---|---|
| **File size (bytes)** | `18,632,184,480` | `18,632,184,480` | ✅ PASS |
| **File size (GiB)** | 17.353 GiB | 17.353 GiB | ✅ PASS |
| **GGUF magic header** | `b'GGUF'` | `b'GGUF'` | ✅ PASS |
| **SHA256 checksum** | `a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32` | `a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32` | ✅ PASS |
| **SHA256 hashing throughput** | — | 860.4 MB/s | — |
| **SHA256 hashing duration** | — | 20.7 seconds | — |

---

## Post-Acquisition Resource State

| Resource | Measured Value |
|---|---|
| Available Physical RAM | ~494 MB (system loaded; inference not attempted) |
| Free NVMe Storage (C:) | 130.13 GB |
| Available VRAM | 5,920 MiB (5.78 GB) — GPU idle |
| GPU Temperature | 42°C |

---

## Phase 2B Phase Gate Status

**PHASE 2B: COMPLETE AND VERIFIED.**

Awaiting ChatGPT (Lead Architect) review and Phase 2C inference directive.
