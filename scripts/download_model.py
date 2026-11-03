"""
NOVA Compute — Phase 2B: Resumable Model Downloader & Verification Script
Target Model: Qwen_Qwen3-30B-A3B-Q4_K_M.gguf
Enforces HTTP Range resumption, retry backoff, bounded memory streaming, RAM/SSD safety gates,
streaming SHA256 calculation, and GGUF header validation. Emits live JSON telemetry for the dashboard.
"""

import os
import sys
import time
import urllib.request
import urllib.error
import socket
import hashlib
import ctypes
import json

URL = "https://huggingface.co/bartowski/Qwen_Qwen3-30B-A3B-GGUF/resolve/fb6ccf61e476d3077e10333e98e794f960ceea2a/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf"
DEST_FINAL = os.path.abspath("models/qwen3-30b/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf")
DEST_PART = DEST_FINAL + ".part"
EXPECTED_SIZE = 18632184480
EXPECTED_SHA256 = "a015794bfb1d69cb03dbb86b185fb2b9b339f757df5f8f9dd9ebdab8f6ed5d32"
EXPECTED_XET_HASH = "c1d94f7c5a14a24f6c637499fbff9ccf5102d312ff68d73a86a8e3f2761918ba"
PINNED_REVISION = "fb6ccf61e476d3077e10333e98e794f960ceea2a"
TELEMETRY_PATH = os.path.abspath("reports/phase2b/DOWNLOAD_TELEMETRY.json")
STATUS_PATH = os.path.abspath("reports/phase2b/download_status.json")
ARTIFACT_STATUS_PATH = os.path.abspath(r"C:\Users\abdal\.gemini\antigravity\brain\25df00c5-a661-4b81-87d0-37b1603ab694\download_status.json")

CHUNK_SIZE = 512 * 1024  # 512 KiB buffer (avoids socket timeout on fluctuating connections)
MAX_RETRIES = 30
RETRY_BACKOFF_SECS = 5.0

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

def get_avail_ram_mb():
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    return stat.ullAvailPhys / (1024 * 1024)

def get_free_disk_gb(path):
    free_bytes = ctypes.c_ulonglong(0)
    total_bytes = ctypes.c_ulonglong(0)
    total_free = ctypes.c_ulonglong(0)
    drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        drive,
        ctypes.byref(free_bytes),
        ctypes.byref(total_bytes),
        ctypes.byref(total_free)
    )
    return free_bytes.value / (1024**3)

def update_status_file(status_str, current_bytes, total_bytes, speed_mb, ram_mb, ssd_gb, eta_sec=None):
    data = {
        "artifact": os.path.basename(DEST_FINAL),
        "status": status_str,
        "downloaded_bytes": current_bytes,
        "total_bytes": total_bytes,
        "downloaded_gib": round(current_bytes / (1024**3), 2),
        "total_gib": round(total_bytes / (1024**3), 2),
        "pct": round((current_bytes / total_bytes) * 100.0, 2),
        "speed_mbs": round(speed_mb, 2),
        "eta_seconds": int(eta_sec) if eta_sec and eta_sec > 0 else 0,
        "avail_ram_mb": round(ram_mb, 1),
        "free_ssd_gb": round(ssd_gb, 2),
        "timestamp": time.time()
    }
    for p in (STATUS_PATH, ARTIFACT_STATUS_PATH):
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as sf:
                json.dump(data, sf, indent=2)
        except Exception:
            pass

def compute_sha256(filepath):
    print(f"\nComputing SHA256 checksum for {os.path.basename(filepath)}...")
    sha = hashlib.sha256()
    total_bytes = os.path.getsize(filepath)
    processed = 0
    t0 = time.time()
    t_last = t0
    
    with open(filepath, "rb") as f:
        while True:
            buf = f.read(16 * 1024 * 1024)  # 16 MiB read buffer
            if not buf:
                break
            sha.update(buf)
            processed += len(buf)
            now = time.time()
            if now - t_last >= 2.0:
                pct = (processed / total_bytes) * 100.0
                speed = (processed / (now - t0)) / (1024 * 1024)
                eta = (total_bytes - processed) / (speed * 1024 * 1024) if speed > 0 else 0
                update_status_file("verifying_sha256", processed, total_bytes, speed, get_avail_ram_mb(), get_free_disk_gb(filepath), eta)
                print(f"Hashing: {processed / (1024**3):.2f}/{total_bytes / (1024**3):.2f} GiB ({pct:.1f}%) | {speed:.1f} MB/s", flush=True)
                t_last = now
                
    elapsed = time.time() - t0
    hex_digest = sha.hexdigest().lower()
    print(f"SHA256 computation complete in {elapsed:.1f}s ({total_bytes / elapsed / (1024**2):.1f} MB/s).")
    print(f"Calculated: {hex_digest}")
    print(f"Expected:   {EXPECTED_SHA256}")
    return hex_digest

def verify_gguf_magic(filepath):
    with open(filepath, "rb") as f:
        magic = f.read(4)
    return magic == b"GGUF"

def download_and_verify():
    os.makedirs(os.path.dirname(DEST_FINAL), exist_ok=True)
    os.makedirs(os.path.dirname(TELEMETRY_PATH), exist_ok=True)

    init_ram = get_avail_ram_mb()
    init_disk = get_free_disk_gb(DEST_FINAL)
    print("=" * 70)
    print("NOVA Compute — Phase 2B Model Acquisition & Verification")
    print(f"Target Artifact: {os.path.basename(DEST_FINAL)}")
    print(f"Expected Size:   {EXPECTED_SIZE:,} bytes ({EXPECTED_SIZE / (1024**3):.2f} GiB)")
    print(f"Initial Avail RAM: {init_ram:.1f} MB")
    print(f"Initial Free SSD: {init_disk:.2f} GB")
    print("=" * 70)

    if init_disk < 25.0:
        print(f"ERROR: Insufficient disk space ({init_disk:.2f} GB < 25 GB requirement). Aborting.")
        sys.exit(1)

    # Check if final file already exists and is verified
    if os.path.exists(DEST_FINAL):
        if os.path.getsize(DEST_FINAL) == EXPECTED_SIZE:
            print("Final artifact already exists on disk with correct size. Verifying integrity...")
            if not verify_gguf_magic(DEST_FINAL):
                print("ERROR: Existing file does not have valid GGUF header magic. Needs re-download.")
            else:
                digest = compute_sha256(DEST_FINAL)
                if digest == EXPECTED_SHA256:
                    print("SUCCESS: Existing file is complete and verified against official SHA256.")
                    update_status_file("completed", EXPECTED_SIZE, EXPECTED_SIZE, 0, init_ram, init_disk, 0)
                    return True
                else:
                    print(f"ERROR: Checksum mismatch on existing file: {digest} != {EXPECTED_SHA256}.")
        else:
            print(f"Final artifact exists with incorrect size ({os.path.getsize(DEST_FINAL)} bytes). Moving to .part.")
            if os.path.exists(DEST_PART):
                os.remove(DEST_PART)
            os.rename(DEST_FINAL, DEST_PART)

    # Check .part file state
    if os.path.exists(DEST_PART):
        part_sz = os.path.getsize(DEST_PART)
        if part_sz > EXPECTED_SIZE:
            print(f"Existing .part file ({part_sz} bytes) is larger than expected ({EXPECTED_SIZE}). Removing corrupted part.")
            os.remove(DEST_PART)

    t_start = time.time()
    total_bytes_transferred_session = 0
    min_avail_ram_mb = init_ram
    retries_used = 0

    while True:
        current_sz = os.path.getsize(DEST_PART) if os.path.exists(DEST_PART) else 0
        if current_sz >= EXPECTED_SIZE:
            break

        headers = {"User-Agent": "Mozilla/5.0 NOVA-Compute/Phase2B"}
        if current_sz > 0:
            headers["Range"] = f"bytes={current_sz}-"
            print(f"\nResuming download at byte offset {current_sz:,} / {EXPECTED_SIZE:,} ({current_sz / EXPECTED_SIZE * 100:.2f}%)...")
        else:
            print(f"\nInitiating download from byte 0...")

        req = urllib.request.Request(URL, headers=headers)
        
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except Exception as e:
            retries_used += 1
            if retries_used > MAX_RETRIES:
                print(f"ERROR: Exceeded maximum connection retries ({MAX_RETRIES}). Aborting download.")
                sys.exit(1)
            backoff = min(RETRY_BACKOFF_SECS * retries_used, 30.0)
            print(f"Connection failed ({e}). Retrying in {backoff:.1f}s (attempt {retries_used}/{MAX_RETRIES})...")
            update_status_file("reconnecting", current_sz, EXPECTED_SIZE, 0, get_avail_ram_mb(), get_free_disk_gb(DEST_PART))
            time.sleep(backoff)
            continue

        status = resp.status
        if current_sz > 0 and status != 206:
            print(f"ERROR: Server returned status {status} instead of 206 Partial Content during resume. Aborting.")
            resp.close()
            sys.exit(1)
        elif current_sz == 0 and status not in (200, 206):
            print(f"ERROR: Server returned unexpected status {status}. Aborting.")
            resp.close()
            sys.exit(1)

        print(f"HTTP Connection established (Status: {status}). Starting stream write...")
        mode = "ab" if current_sz > 0 else "wb"
        t_last_log = time.time()
        t_last_status = time.time()
        session_bytes = 0
        speed_window_bytes = 0
        speed_window_start = time.time()
        last_speed_mbs = 0.0

        with open(DEST_PART, mode, buffering=0) as f:
            try:
                while True:
                    # RAM safety gate
                    ram = get_avail_ram_mb()
                    if ram < min_avail_ram_mb:
                        min_avail_ram_mb = ram

                    if ram < 750.0:
                        print(f"\nNotice: Physical RAM dropped to {ram:.1f} MB (< 750 MB threshold). Pausing stream...")
                        update_status_file("paused_ram_threshold", current_sz + session_bytes, EXPECTED_SIZE, 0, ram, get_free_disk_gb(DEST_PART))
                        recovered = False
                        for wait_step in range(24):  # 24 * 5s = 120s
                            time.sleep(5.0)
                            current_ram = get_avail_ram_mb()
                            if current_ram >= 850.0:
                                print(f"RAM recovered to {current_ram:.1f} MB (> 850 MB). Resuming stream write...")
                                recovered = True
                                break
                            else:
                                print(f"Waiting for RAM recovery... (Current: {current_ram:.1f} MB, waited {(wait_step+1)*5}s/120s)", flush=True)

                        if not recovered:
                            print(f"\nCRITICAL: Physical RAM sustained below 750 MB for > 120 seconds.")
                            print("Pausing stream and aborting to prevent OS instability. Partial download safely saved.")
                            resp.close()
                            sys.exit(2)
                    elif ram < 1000.0:
                        print(f"\nNotice: Available RAM low ({ram:.1f} MB). Throttling download stream 5s...")
                        time.sleep(5.0)

                    # SSD safety gate
                    disk = get_free_disk_gb(DEST_PART)
                    if disk < 10.0:
                        print(f"\nCRITICAL: Free disk space dropped to {disk:.2f} GB (< 10 GB limit). Aborting.")
                        resp.close()
                        sys.exit(3)

                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        print("\nStream segment completed by server.")
                        break

                    # Reset retry counter on successful chunk transfer
                    retries_used = 0

                    f.write(chunk)
                    session_bytes += len(chunk)
                    total_bytes_transferred_session += len(chunk)
                    speed_window_bytes += len(chunk)

                    now = time.time()

                    # Periodic UI status file write (every 2.5s)
                    if now - t_last_status >= 2.5:
                        dt = now - speed_window_start
                        if dt > 0:
                            last_speed_mbs = (speed_window_bytes / dt) / (1024 * 1024)
                        written = current_sz + session_bytes
                        rem_bytes = max(0, EXPECTED_SIZE - written)
                        eta_sec = rem_bytes / (last_speed_mbs * 1024 * 1024) if last_speed_mbs > 0 else 0
                        update_status_file("downloading", written, EXPECTED_SIZE, last_speed_mbs, ram, disk, eta_sec)
                        t_last_status = now
                        speed_window_bytes = 0
                        speed_window_start = now

                    # Periodic console logging (every 10s)
                    if now - t_last_log >= 10.0:
                        written = current_sz + session_bytes
                        pct = (written / EXPECTED_SIZE) * 100.0
                        print(
                            f"Downloaded: {written / (1024**3):.2f}/{EXPECTED_SIZE / (1024**3):.2f} GiB "
                            f"({pct:.1f}%) | Speed: {last_speed_mbs:.1f} MB/s | Avail RAM: {ram:.0f} MB | Free SSD: {disk:.1f} GB",
                            flush=True
                        )
                        t_last_log = now

            except (socket.timeout, urllib.error.URLError, ConnectionResetError, ConnectionError) as err:
                retries_used += 1
                backoff = min(5.0 * retries_used, 30.0)
                print(f"\nStream interrupted: {err}. Retrying resumption in {backoff:.1f}s (attempt {retries_used}/{MAX_RETRIES})...")
                update_status_file("reconnecting", current_sz + session_bytes, EXPECTED_SIZE, 0, get_avail_ram_mb(), get_free_disk_gb(DEST_PART))
                resp.close()
                time.sleep(backoff)
                continue
            finally:
                resp.close()

    t_download_done = time.time()
    total_download_duration = t_download_done - t_start
    final_part_sz = os.path.getsize(DEST_PART)
    print("\n" + "=" * 70)
    print(f"Download Stream Completed. File size: {final_part_sz:,} bytes.")
    if final_part_sz != EXPECTED_SIZE:
        print(f"ERROR: Part file size {final_part_sz} != expected {EXPECTED_SIZE}. Aborting.")
        sys.exit(4)

    # Validate GGUF Magic Header
    print("\nVerifying GGUF header magic...")
    update_status_file("verifying_header", final_part_sz, EXPECTED_SIZE, 0, get_avail_ram_mb(), get_free_disk_gb(DEST_PART))
    if not verify_gguf_magic(DEST_PART):
        print("CRITICAL ERROR: First 4 bytes do not match 'GGUF' magic! Corrupted file.")
        sys.exit(5)
    print("Header verification: Magic bytes confirmed 'GGUF' (0x47475546).")

    # Cryptographic Checksum Verification
    sha256_result = compute_sha256(DEST_PART)
    if sha256_result != EXPECTED_SHA256:
        print("CRITICAL ERROR: SHA256 checksum does NOT match official Git LFS OID!")
        print(f"Expected: {EXPECTED_SHA256}")
        print(f"Received: {sha256_result}")
        sys.exit(6)

    print("\nCRYPTOGRAPHIC VERIFICATION SUCCESS: SHA256 matches official commit record perfectly.")

    # Atomic Rename
    if os.path.exists(DEST_FINAL):
        os.remove(DEST_FINAL)
    os.rename(DEST_PART, DEST_FINAL)
    print(f"Artifact finalized and promoted to: {DEST_FINAL}")

    final_ram = get_avail_ram_mb()
    final_disk = get_free_disk_gb(DEST_FINAL)
    avg_speed_mbs = (total_bytes_transferred_session / total_download_duration) / (1024 * 1024) if total_download_duration > 0 else 0

    update_status_file("completed", EXPECTED_SIZE, EXPECTED_SIZE, 0, final_ram, final_disk, 0)

    telemetry = {
        "artifact_name": os.path.basename(DEST_FINAL),
        "artifact_size_bytes": final_part_sz,
        "artifact_size_gib": round(final_part_sz / (1024**3), 2),
        "pinned_revision": PINNED_REVISION,
        "expected_sha256": EXPECTED_SHA256,
        "verified_sha256": sha256_result,
        "sha256_matched": True,
        "gguf_magic_verified": True,
        "download_duration_seconds": round(total_download_duration, 2),
        "average_speed_mbs": round(avg_speed_mbs, 2),
        "min_available_ram_mb": round(min_avail_ram_mb, 1),
        "post_download_available_ram_mb": round(final_ram, 1),
        "post_download_free_ssd_gb": round(final_disk, 2),
        "retries_count": retries_used,
        "status": "completed-verified"
    }

    with open(TELEMETRY_PATH, "w", encoding="utf-8") as f:
        json.dump(telemetry, f, indent=2)

    print(f"Telemetry recorded to {TELEMETRY_PATH}")
    print("=" * 70)
    return True

if __name__ == "__main__":
    download_and_verify()
