$ErrorActionPreference = 'Continue'
Write-Output '=== NOVA Compute / Windows hardware report ==='
Write-Output "Collected: $(Get-Date -Format o)"
Write-Output "OS: $((Get-CimInstance Win32_OperatingSystem).Caption)"
Write-Output "OS version: $((Get-CimInstance Win32_OperatingSystem).Version)"
Write-Output "CPU: $((Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name))"
Write-Output "Logical processors: $((Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum)"
Write-Output "Installed RAM (GB): $([math]::Round(((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB), 2))"
Write-Output '\n--- Memory modules ---'
Get-CimInstance Win32_PhysicalMemory | Select-Object Manufacturer, Capacity, Speed, ConfiguredClockSpeed, PartNumber | Format-Table -AutoSize
Write-Output '\n--- Physical disks ---'
Get-PhysicalDisk | Select-Object FriendlyName, MediaType, BusType, Size, HealthStatus | Format-Table -AutoSize
Write-Output '\n--- Volumes ---'
Get-Volume | Where-Object DriveLetter | Select-Object DriveLetter, FileSystemLabel, FileSystem, SizeRemaining, Size | Format-Table -AutoSize
Write-Output '\n--- NVIDIA ---'
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { nvidia-smi } else { Write-Output 'nvidia-smi not found in PATH' }
Write-Output '\n--- WSL ---'
if (Get-Command wsl.exe -ErrorAction SilentlyContinue) { wsl.exe --status; wsl.exe --list --verbose } else { Write-Output 'WSL command not found' }
Write-Output '\nReport complete. Review before sharing; it contains hardware/software inventory, not personal documents.'
