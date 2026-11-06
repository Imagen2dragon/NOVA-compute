# NOVA Chat - UI Screenshot Capture Automation via Headless Chromium Renderer
param(
    [string]$OutputDir = "reports\desktop\screenshots",
    [int]$Port = 14210
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
}

$edgePath = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edgePath)) {
    Write-Error "Microsoft Edge executable not found at: $edgePath"
    exit 1
}

$rootDir = (Resolve-Path .).Path
$distDir = Join-Path $rootDir "desktop\dist"
if (-not (Test-Path $distDir)) {
    Write-Error "Frontend distribution directory not found: $distDir. Run 'npm run build' first."
    exit 1
}

Write-Host "=========================================================="
Write-Host "NOVA Chat - Production UI Screenshot Capture Engine"
Write-Host "Frontend Dist: $distDir"
Write-Host "Output Directory: $OutputDir"
Write-Host "=========================================================="

# Start local static server for frontend assets
$serverProc = Start-Process python -ArgumentList "-m http.server $Port -d `"$distDir`"" -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 2

$views = @(
    @{ Name = "01_welcome_screen_unloaded.png"; View = "welcome"; Desc = "Welcome screen with model unloaded" },
    @{ Name = "02_resource_dashboard.png"; View = "dashboard"; Desc = "Resource dashboard modal" },
    @{ Name = "03_resource_recovery_assistant.png"; View = "recovery"; Desc = "Resource recovery assistant modal" },
    @{ Name = "04_recovery_instructions_ollama.png"; View = "recovery_instructions"; Desc = "Application recovery instructions (Ollama/Edge)" },
    @{ Name = "05_alternative_profiles.png"; View = "alternative_profiles"; Desc = "Alternative runtime profiles cards" },
    @{ Name = "06_model_load_confirmation.png"; View = "confirmation"; Desc = "Model load confirmation with integrity decision" },
    @{ Name = "07_chat_interface_unloaded.png"; View = "chat"; Desc = "Chat interface in safe unloaded state" },
    @{ Name = "08_settings_modal.png"; View = "settings"; Desc = "Settings modal" },
    @{ Name = "09_error_and_recovery_states.png"; View = "error"; Desc = "Error and recovery states" }
)

try {
    foreach ($v in $views) {
        $outFile = Join-Path $rootDir "$OutputDir\$($v.Name)"
        $url = "http://127.0.0.1:$Port/?view=$($v.View)"
        Write-Host "Capturing view: $($v.Desc) ($($v.View))..."
        Write-Host "  URL: $url"

        $argsList = @(
            "--headless=new",
            "--screenshot=$outFile",
            "--window-size=1080,780",
            "--hide-scrollbars",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            $url
        )

        $p = Start-Process $edgePath -ArgumentList $argsList -PassThru -Wait
        if (Test-Path $outFile) {
            $len = (Get-Item $outFile).Length
            Write-Host "  Successfully captured $($v.Name) ($len bytes)"
        } else {
            Write-Warning "  Failed to generate $outFile (exit code: $($p.ExitCode))"
        }
        Start-Sleep -Milliseconds 400
    }
} finally {
    Write-Host "Stopping static asset server (PID: $($serverProc.Id))..."
    Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue
}

Write-Host "=========================================================="
Write-Host "Screenshot capture complete. Verifying generated files:"
Get-ChildItem $OutputDir | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
Write-Host "=========================================================="
