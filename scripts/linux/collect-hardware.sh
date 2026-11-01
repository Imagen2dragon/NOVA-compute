#!/usr/bin/env bash
set +e
printf '=== NOVA Compute / Linux hardware report ===\n'
printf 'Collected: '; date --iso-8601=seconds
printf '\n--- OS / kernel ---\n'
cat /etc/os-release 2>/dev/null | sed -n '1,8p'
uname -a
printf '\n--- CPU ---\n'
lscpu 2>/dev/null | grep -E 'Model name|CPU\(s\)|Thread|Core|Socket|Architecture|Vendor ID'
printf '\n--- Memory ---\n'
free -h
printf '\n--- Disks and filesystems ---\n'
lsblk -o NAME,MODEL,SIZE,ROTA,TRAN,FSTYPE,MOUNTPOINTS 2>/dev/null
df -hT 2>/dev/null
printf '\n--- NVIDIA / CUDA ---\n'
if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi; else echo 'nvidia-smi not found'; fi
if command -v nvcc >/dev/null 2>&1; then nvcc --version; else echo 'nvcc not found'; fi
printf '\n--- cgroup / systemd ---\n'
stat -fc %T /sys/fs/cgroup 2>/dev/null
systemctl is-system-running 2>/dev/null || true
printf '\n--- WSL detection ---\n'
grep -qi microsoft /proc/version 2>/dev/null && echo 'Running under WSL' || echo 'Not detected as WSL'
printf '\nReport complete. Review before sharing; output is hardware/software inventory only.\n'
