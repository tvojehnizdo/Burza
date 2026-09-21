$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "IMPULSE MAX 5K - Kraken Pulse Hunter V4" -ForegroundColor Cyan
$commit = (git rev-parse --short HEAD 2>$null)
if ($commit) { Write-Host "Git commit: $commit" -ForegroundColor DarkGray }

$oldListener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
if ($oldListener) {
    Write-Host "Stopping stale V4 listener on port 8765 (PID $oldListener)..." -ForegroundColor Yellow
    Stop-Process -Id $oldListener -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
Write-Host "PAPER/RESEARCH only. LIVE orders are disabled." -ForegroundColor Yellow

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    py -m venv .venv
}

Set-ExecutionPolicy -Scope Process Bypass -Force
& ".\.venv\Scripts\Activate.ps1"
python -m pip install -r requirements.txt

$env:AUTO_RECORD = "1"
if (-not $env:START_CAPITAL) { $env:START_CAPITAL = "5000" }
if (-not $env:WS_SYMBOLS) { $env:WS_SYMBOLS = "BTC/USD,ETH/USD,SOL/USD,XRP/USD" }
if (-not $env:ALPHA_INTERVAL_S) { $env:ALPHA_INTERVAL_S = "30" }
if (-not $env:MICRO_SNAPSHOT_MS) { $env:MICRO_SNAPSHOT_MS = "1000" }
if (-not $env:V4_EXECUTION_MODE) { $env:V4_EXECUTION_MODE = "market_taker" }

# 14 bps was the legacy research cost assumption and is too low for the
# current spot market-taker path. Remove only this known stale override so the
# engine derives the active cost model from fees/slippage/penalty.
if ($env:V4_EXEC_ROUNDTRIP_BPS -eq "14") {
    Remove-Item Env:V4_EXEC_ROUNDTRIP_BPS -ErrorAction SilentlyContinue
    Write-Host "Removed legacy V4_EXEC_ROUNDTRIP_BPS=14 override." -ForegroundColor Yellow
} elseif ($env:V4_EXEC_ROUNDTRIP_BPS) {
    Write-Host "Explicit V4_EXEC_ROUNDTRIP_BPS override: $env:V4_EXEC_ROUNDTRIP_BPS" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Recorder: Kraken Spot WS v2 L2 + trades (~1s snapshots)" -ForegroundColor Green
Write-Host "Decision/alpha cycle: 30 seconds" -ForegroundColor Green
Write-Host "Execution mode: $env:V4_EXECUTION_MODE" -ForegroundColor Green
Write-Host "Database: data\pulse_v4.db" -ForegroundColor Green
Write-Host "Dashboard: http://127.0.0.1:8765" -ForegroundColor Green
Write-Host ""

Start-Process "http://127.0.0.1:8765"
python -m uvicorn app:app --host 127.0.0.1 --port 8765
