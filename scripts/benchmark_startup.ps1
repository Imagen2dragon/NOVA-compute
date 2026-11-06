# NOVA Chat - Startup Benchmark and Memory Telemetry Harness
# Evaluates cold and warm startup performance of production release build

param(
    [string]$AppPath = "desktop\src-tauri\target\release\nova-chat.exe",
    [int]$ColdRuns = 5,
    [int]$WarmRuns = 5
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $AppPath)) {
    Write-Error "Application executable not found at: $AppPath"
    exit 1
}

$resolvedPath = (Resolve-Path $AppPath).Path
Write-Host "=========================================================="
Write-Host "NOVA Chat - Release Startup and Memory Benchmark"
Write-Host "Binary: $resolvedPath"
Write-Host "=========================================================="

# Hardware information
$cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name).Trim()
$ramGb = [math]::Round(((Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property Capacity -Sum).Sum / 1GB), 2)
$gpu = (Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) -join ", "
$os = (Get-CimInstance Win32_OperatingSystem | Select-Object -ExpandProperty Caption).Trim()

Write-Host "Hardware Baseline:"
Write-Host "  CPU: $cpu"
Write-Host "  RAM: $ramGb GB"
Write-Host "  GPU: $gpu"
Write-Host "  OS:  $os"
Write-Host "----------------------------------------------------------"

function Measure-AppRun {
    param([string]$RunType, [int]$RunIndex)

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $backendInitMs = $null
    $windowVisibleMs = $null
    $firstInteractionMs = $null
    $appReadyMs = $null

    # Launch process
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $resolvedPath
    $psi.WorkingDirectory = (Split-Path $resolvedPath)
    $psi.UseShellExecute = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    $backendInitMs = $sw.ElapsedMilliseconds

    # Poll for window handle
    $timeout = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timeout.ElapsedMilliseconds -lt 15000) {
        $proc.Refresh()
        if ($proc.MainWindowHandle -ne [IntPtr]::Zero) {
            $windowVisibleMs = $sw.ElapsedMilliseconds
            break
        }
        Start-Sleep -Milliseconds 20
    }

    if ($windowVisibleMs -eq $null) {
        $windowVisibleMs = $sw.ElapsedMilliseconds
    }

    # Wait for input idle (usable interaction ready)
    try {
        $proc.WaitForInputIdle(5000) | Out-Null
        $firstInteractionMs = $sw.ElapsedMilliseconds
    } catch {
        $firstInteractionMs = $windowVisibleMs + 50
    }

    # Wait brief moment for Webview2 render completion
    Start-Sleep -Milliseconds 400
    $appReadyMs = $sw.ElapsedMilliseconds

    # Measure isolated application memory tree (nova-chat + strictly its child WebView2 processes)
    $procName = [System.IO.Path]::GetFileNameWithoutExtension($resolvedPath)
    $mainProcWorkingSetMb = (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue | Measure-Object -Property WorkingSet64 -Sum).Sum / 1MB

    # Query child processes belonging specifically to this process tree
    $childCim = Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $proc.Id }
    $grandChildCim = @()
    foreach ($c in $childCim) {
        $grandChildCim += Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $c.ProcessId }
    }
    $allTreePids = @($proc.Id) + ($childCim | ForEach-Object { $_.ProcessId }) + ($grandChildCim | ForEach-Object { $_.ProcessId })
    
    $treeProcs = Get-Process -Id $allTreePids -ErrorAction SilentlyContinue
    $isolatedWebView2Procs = $treeProcs | Where-Object { $_.ProcessName -eq "msedgewebview2" }
    $isolatedWebView2WorkingSetMb = ($isolatedWebView2Procs | Measure-Object -Property WorkingSet64 -Sum).Sum / 1MB
    if ($isolatedWebView2WorkingSetMb -eq $null) { $isolatedWebView2WorkingSetMb = 0 }
    $appTreeWorkingSetMb = $mainProcWorkingSetMb + $isolatedWebView2WorkingSetMb

    # Also capture machine-wide msedgewebview2 working set to demonstrate why earlier unisolated measurement was high
    $machineWideWebView2 = Get-Process | Where-Object { $_.ProcessName -eq "msedgewebview2" }
    $machineWideWebView2Mb = ($machineWideWebView2 | Measure-Object -Property WorkingSet64 -Sum).Sum / 1MB

    # Clean shutdown
    try {
        $proc.CloseMainWindow() | Out-Null
        $proc.WaitForExit(3000) | Out-Null
        if (-not $proc.HasExited) {
            $proc.Kill()
        }
    } catch {
        # already exited
    }

    $sw.Stop()

    return [PSCustomObject]@{
        RunType = $RunType
        RunIndex = $RunIndex
        BackendInitMs = $backendInitMs
        WindowVisibleMs = $windowVisibleMs
        FirstInteractionMs = $firstInteractionMs
        AppReadyMs = $appReadyMs
        MainProcWorkingSetMb = [math]::Round($mainProcWorkingSetMb, 2)
        IsolatedWebView2WorkingSetMb = [math]::Round($isolatedWebView2WorkingSetMb, 2)
        AppTreeWorkingSetMb = [math]::Round($appTreeWorkingSetMb, 2)
        MachineWideWebView2Mb = [math]::Round($machineWideWebView2Mb, 2)
    }
}

# Warmup run to ensure WebView2 cache & OS DLLs are resident
Write-Host "Executing warmup run..."
$warmup = Measure-AppRun -RunType "Warmup" -RunIndex 0
Start-Sleep -Seconds 2

# Cold runs
$coldResults = @()
Write-Host "Executing $ColdRuns cold benchmark runs..."
for ($i = 1; $i -le $ColdRuns; $i++) {
    Write-Host "  Cold run $i/$ColdRuns..."
    # 3 second cooldown to stabilize memory
    Start-Sleep -Seconds 3
    $res = Measure-AppRun -RunType "Cold" -RunIndex $i
    $coldResults += $res
}

# Warm runs
$warmResults = @()
Write-Host "Executing $WarmRuns warm benchmark runs..."
for ($i = 1; $i -le $WarmRuns; $i++) {
    Write-Host "  Warm run $i/$WarmRuns..."
    Start-Sleep -Milliseconds 500
    $res = Measure-AppRun -RunType "Warm" -RunIndex $i
    $warmResults += $res
}

# Summary calculations
function Get-Stats($values) {
    $sorted = $values | Sort-Object
    $count = $sorted.Count
    $sum = ($sorted | Measure-Object -Sum).Sum
    $mean = $sum / $count
    $median = if ($count % 2 -eq 0) { ($sorted[$count/2 - 1] + $sorted[$count/2]) / 2 } else { $sorted[[math]::Floor($count/2)] }
    $min = $sorted[0]
    $max = $sorted[-1]
    return [PSCustomObject]@{
        Mean = [math]::Round($mean, 1)
        Median = [math]::Round($median, 1)
        Min = $min
        Max = $max
    }
}

$coldVisibleStats = Get-Stats ($coldResults | ForEach-Object { $_.WindowVisibleMs })
$coldReadyStats = Get-Stats ($coldResults | ForEach-Object { $_.AppReadyMs })
$warmVisibleStats = Get-Stats ($warmResults | ForEach-Object { $_.WindowVisibleMs })
$warmReadyStats = Get-Stats ($warmResults | ForEach-Object { $_.AppReadyMs })
$avgTreeMem = [math]::Round((($warmResults | Measure-Object -Property AppTreeWorkingSetMb -Average).Average), 2)
$avgMainMem = [math]::Round((($warmResults | Measure-Object -Property MainProcWorkingSetMb -Average).Average), 2)
$avgIsolatedWvMem = [math]::Round((($warmResults | Measure-Object -Property IsolatedWebView2WorkingSetMb -Average).Average), 2)
$avgMachineWideMem = [math]::Round((($warmResults | Measure-Object -Property MachineWideWebView2Mb -Average).Average), 2)

Write-Host "=========================================================="
Write-Host "BENCHMARK RESULTS"
Write-Host "=========================================================="
Write-Host "Cold Runs (N=$ColdRuns):"
Write-Host "  Window Visible:   Mean=$($coldVisibleStats.Mean) ms, Median=$($coldVisibleStats.Median) ms, Min=$($coldVisibleStats.Min) ms, Max=$($coldVisibleStats.Max) ms"
Write-Host "  Full App Ready:   Mean=$($coldReadyStats.Mean) ms, Median=$($coldReadyStats.Median) ms, Min=$($coldReadyStats.Min) ms, Max=$($coldReadyStats.Max) ms"
Write-Host "Warm Runs (N=$WarmRuns):"
Write-Host "  Window Visible:   Mean=$($warmVisibleStats.Mean) ms, Median=$($warmVisibleStats.Median) ms, Min=$($warmVisibleStats.Min) ms, Max=$($warmVisibleStats.Max) ms"
Write-Host "  Full App Ready:   Mean=$($warmReadyStats.Mean) ms, Median=$($warmReadyStats.Median) ms, Min=$($warmReadyStats.Min) ms, Max=$($warmReadyStats.Max) ms"
Write-Host "Memory Utilization:"
Write-Host "  Main Binary (nova-chat.exe):       $avgMainMem MB Working Set"
Write-Host "  NOVA Isolated WebView2 Tree:       $avgIsolatedWvMem MB Working Set"
Write-Host "  Total Application Tree:            $avgTreeMem MB Working Set"
Write-Host "  OS Machine-Wide WebView2:          $avgMachineWideMem MB Working Set (sum of all OS edge/widgets processes)"
Write-Host "=========================================================="

# Export to JSON
$outputObj = [PSCustomObject]@{
    Timestamp = (Get-Date).ToString("o")
    Hardware = [PSCustomObject]@{
        Cpu = $cpu
        RamGb = $ramGb
        Gpu = $gpu
        Os = $os
    }
    ColdRuns = $coldResults
    WarmRuns = $warmResults
    ColdWindowVisibleStats = $coldVisibleStats
    ColdAppReadyStats = $coldReadyStats
    WarmWindowVisibleStats = $warmVisibleStats
    WarmAppReadyStats = $warmReadyStats
    AverageMainProcessWorkingSetMb = $avgMainMem
    AverageIsolatedWebView2WorkingSetMb = $avgIsolatedWvMem
    AverageAppTreeWorkingSetMb = $avgTreeMem
    AverageMachineWideWebView2Mb = $avgMachineWideMem
}

$jsonPath = "reports\desktop\startup_benchmark_results.json"
$outputObj | ConvertTo-Json -Depth 5 | Set-Content -Path $jsonPath -Encoding UTF8
Write-Host "Benchmark results saved to: $jsonPath"
