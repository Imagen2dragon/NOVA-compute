"""
NOVA Compute — Process Supervisor Engine
Supervises child process execution via ordered telemetry sample queues,
asynchronous stdout draining, monotonic timestamp tracking, and fail-closed abort triggers.
Decoupled and testable with mock/injected clocks, queues, and child handles.
"""

import time
import math
import queue
import subprocess
from typing import Optional, List, Dict, Any, Callable
from .models import (
    TelemetryStatus,
    SupervisorResult
)
from .policy import ResourcePolicy, DEFAULT_POLICY

def supervise_child_process(
    proc: subprocess.Popen,
    sample_queue: queue.Queue,
    output_queue: queue.Queue,
    policy: Optional[ResourcePolicy] = None,
    clock: Callable[[], float] = time.monotonic,
    abort_callback: Optional[Callable[[str], None]] = None,
    on_sample_processed: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_output_line: Optional[Callable[[str], None]] = None,
    stale_timeout_sec: float = 3.0,
    poll_sleep_sec: float = 0.01,
    reader_thread: Optional[Any] = None,
    reader_timeout_sec: float = 2.0
) -> SupervisorResult:
    """
    Supervises a running child process.
    Consumes every telemetry sample in order from sample_queue.
    Drains output_queue asynchronously.
    Enforces monotonic critical RAM timers, commit limits, VRAM bounds, and stale timeouts.
    """
    pol = policy or DEFAULT_POLICY
    t_start = clock()
    last_sample_monotonic = t_start
    expected_sample_id = 1
    critical_ram_start_monotonic: Optional[float] = None
    peak_ram_gib = 999.0
    peak_vram_mib = 0.0
    processed_samples_count = 0
    abort_triggered = False
    abort_reason = ""
    output_lines: List[str] = []
    workspace_reserve_warned = False

    while proc.poll() is None and not abort_triggered:
        now = clock()

        # 1. Drain output queue (non-blocking)
        while True:
            try:
                line = output_queue.get_nowait()
                output_lines.append(line)
                if on_output_line:
                    on_output_line(line)
                if "CUDA error" in line or "out of memory" in line.lower():
                    abort_triggered = True
                    abort_reason = f"CUDA OOM / Error detected: {line.strip()}"
                    if abort_callback:
                        abort_callback(abort_reason)
                    break
            except queue.Empty:
                break

        if abort_triggered:
            break

        # 2. Consume and process every telemetry sample in the queue
        while True:
            try:
                sample = sample_queue.get_nowait()
            except queue.Empty:
                break

            sample_id = sample.get("sample_id")
            if sample_id is None or not isinstance(sample_id, int) or isinstance(sample_id, bool):
                abort_triggered = True
                abort_reason = f"CRITICAL ABORT: Telemetry sample has invalid sample_id {sample_id!r} (must be int). Fail-closed."
                if abort_callback:
                    abort_callback(abort_reason)
                break

            # Strictly validate sequential sample ID ordering
            if sample_id != expected_sample_id:
                if sample_id < expected_sample_id:
                    abort_triggered = True
                    abort_reason = (
                        f"CRITICAL ABORT: Out of order or duplicate telemetry sample ID {sample_id} "
                        f"(expected {expected_sample_id}). Fail-closed."
                    )
                else:
                    abort_triggered = True
                    abort_reason = (
                        f"CRITICAL ABORT: Telemetry sample gap detected: received {sample_id}, "
                        f"expected {expected_sample_id}. Fail-closed."
                    )
                if abort_callback:
                    abort_callback(abort_reason)
                break

            sample_mono = sample.get("timestamp_monotonic")
            if sample_mono is None or not isinstance(sample_mono, (int, float)) or not math.isfinite(sample_mono):
                abort_triggered = True
                abort_reason = f"CRITICAL ABORT: Telemetry sample has invalid timestamp_monotonic {sample_mono!r}. Fail-closed."
                if abort_callback:
                    abort_callback(abort_reason)
                break

            # Reject future timestamps
            if sample_mono > now + 0.1:
                abort_triggered = True
                abort_reason = (
                    f"CRITICAL ABORT: Telemetry sample timestamp is in the future "
                    f"({sample_mono:.4f} > clock {now:.4f} + 0.1). Fail-closed."
                )
                if abort_callback:
                    abort_callback(abort_reason)
                break

            # Reject backward timestamps
            if processed_samples_count > 0 and sample_mono < (last_sample_monotonic - 1e-6):
                abort_triggered = True
                abort_reason = (
                    f"CRITICAL ABORT: Telemetry sample timestamp moved backward "
                    f"({sample_mono:.4f} < previous {last_sample_monotonic:.4f}). Fail-closed."
                )
                if abort_callback:
                    abort_callback(abort_reason)
                break

            expected_sample_id += 1
            processed_samples_count += 1
            last_sample_monotonic = sample_mono

            s_ram = sample.get("system_ram", {})
            s_gpu = sample.get("gpu", {})

            # Fail-closed sensor status validation
            if s_ram.get("status") != TelemetryStatus.SUCCESS.value:
                abort_triggered = True
                abort_reason = f"CRITICAL ABORT: Required system RAM telemetry failed ({s_ram.get('error') or 'Unknown'}). Fail-closed."
                if abort_callback:
                    abort_callback(abort_reason)
                break

            if s_gpu.get("status") != TelemetryStatus.SUCCESS.value:
                abort_triggered = True
                abort_reason = f"CRITICAL ABORT: Required GPU telemetry failed ({s_gpu.get('error') or 'Unknown'}). Fail-closed."
                if abort_callback:
                    abort_callback(abort_reason)
                break

            cur_ram = s_ram.get("avail_phys_gib")
            cur_vram_free = s_gpu.get("vram_free_mib")
            cur_vram_used = s_gpu.get("vram_used_mib", 0.0)
            commit_pct = s_ram.get("commit_pct")

            if (cur_ram is None or not isinstance(cur_ram, (int, float)) or not math.isfinite(cur_ram) or cur_ram < 0 or
                cur_vram_free is None or not isinstance(cur_vram_free, (int, float)) or not math.isfinite(cur_vram_free) or cur_vram_free < 0 or
                commit_pct is None or not isinstance(commit_pct, (int, float)) or not math.isfinite(commit_pct) or not (0.0 <= commit_pct <= 100.0)):
                abort_triggered = True
                abort_reason = (
                    f"CRITICAL ABORT: Telemetry sample contains invalid, negative, or non-finite metrics "
                    f"(RAM={cur_ram}, VRAM_free={cur_vram_free}, commit_pct={commit_pct}). Fail-closed."
                )
                if abort_callback:
                    abort_callback(abort_reason)
                break

            if cur_ram < peak_ram_gib:
                peak_ram_gib = cur_ram
            if cur_vram_used > peak_vram_mib:
                peak_vram_mib = cur_vram_used

            # Workspace reserve warning
            if cur_ram < pol.workspace_reserve_gib and not workspace_reserve_warned:
                workspace_reserve_warned = True

            # Monotonic Low-RAM timer: reset if RAM recovered >= critical threshold
            if cur_ram < pol.critical_ram_gib:
                if critical_ram_start_monotonic is None:
                    critical_ram_start_monotonic = sample_mono
                elapsed_crit = sample_mono - critical_ram_start_monotonic
                if elapsed_crit >= pol.low_ram_abort_duration_seconds:
                    abort_triggered = True
                    abort_reason = (
                        f"CRITICAL ABORT: Available RAM sustained < {pol.critical_ram_gib} GiB "
                        f"for {elapsed_crit:.2f}s (Threshold: {pol.low_ram_abort_duration_seconds}s)."
                    )
                    if abort_callback:
                        abort_callback(abort_reason)
                    break
            else:
                critical_ram_start_monotonic = None

            # System commit threshold check
            if commit_pct >= pol.critical_commit_pct:
                abort_triggered = True
                abort_reason = f"CRITICAL ABORT: System commit reached {commit_pct}% of limit (>= {pol.critical_commit_pct}%)."
                if abort_callback:
                    abort_callback(abort_reason)
                break

            # Free VRAM threshold check
            if cur_vram_free < pol.critical_vram_free_mib:
                abort_triggered = True
                abort_reason = f"CRITICAL ABORT: Free VRAM dropped to {cur_vram_free:.1f} MiB (< {pol.critical_vram_free_mib} MiB)."
                if abort_callback:
                    abort_callback(abort_reason)
                break

            if on_sample_processed:
                on_sample_processed(sample)

        if abort_triggered:
            break

        # 3. Check for stale telemetry (missing fresh samples)
        if now - last_sample_monotonic > stale_timeout_sec:
            abort_triggered = True
            abort_reason = (
                f"CRITICAL ABORT: Live telemetry sampler is unresponsive or stale "
                f"(> {stale_timeout_sec}s without fresh sample). Fail-closed."
            )
            if abort_callback:
                abort_callback(abort_reason)
            break

        if poll_sleep_sec > 0:
            time.sleep(poll_sleep_sec)

    # Wait for reader thread to reach EOF if provided and not aborted
    reader_thread_stopped = True
    reader_eof_reached = True
    reader_read_failed = False
    reader_timed_out = False
    reader_exception = None

    if reader_thread is not None and not abort_triggered:
        reader_thread.join(timeout=reader_timeout_sec)
        reader_thread_stopped = not reader_thread.is_alive()
        reader_timed_out = not reader_thread_stopped
        reader_eof_reached = getattr(reader_thread, "eof_reached", False) and reader_thread_stopped
        reader_read_failed = getattr(reader_thread, "read_failed", False) or reader_timed_out
        reader_exception = getattr(reader_thread, "error_message", None)

    # Final post-loop output drain (drains any trailing lines from EOF)
    while True:
        try:
            line = output_queue.get_nowait()
            output_lines.append(line)
            if on_output_line:
                on_output_line(line)
            if "CUDA error" in line or "out of memory" in line.lower():
                if not abort_triggered:
                    abort_triggered = True
                    abort_reason = f"CUDA OOM / Error detected in trailing output: {line.strip()}"
                    if abort_callback:
                        abort_callback(abort_reason)
        except queue.Empty:
            break

    dur = round(clock() - t_start, 3)
    exit_code = proc.poll()

    return SupervisorResult(
        abort_triggered=abort_triggered,
        abort_reason=abort_reason,
        exit_code=exit_code,
        peak_ram_gib=peak_ram_gib if peak_ram_gib != 999.0 else 0.0,
        peak_vram_mib=peak_vram_mib,
        processed_samples_count=processed_samples_count,
        duration_seconds=dur,
        output_lines=output_lines,
        reader_thread_stopped=reader_thread_stopped,
        reader_eof_reached=reader_eof_reached,
        reader_read_failed=reader_read_failed,
        reader_timed_out=reader_timed_out,
        reader_exception=reader_exception
    )
