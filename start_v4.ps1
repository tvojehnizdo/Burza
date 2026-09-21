$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "IMPULSE MAX 5K - Kraken Pulse Hunter V4" -ForegroundColor Cyan
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

Write-Host ""
Write-Host "Recorder: Kraken Spot WS v2 L2 + trades (~1s snapshots)" -ForegroundColor Green
Write-Host "Decision/alpha cycle: 30 seconds" -ForegroundColor Green
Write-Host "Database: data\pulse_v4.db" -ForegroundColor Green
Write-Host "Dashboard: http://127.0.0.1:8765" -ForegroundColor Green
Write-Host ""

Start-Process "http://127.0.0.1:8765"
python -m uvicorn app:app --host 127.0.0.1 --port 8765
