$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "IMPULSE MAX 5K - AGGRESSIVE SHADOW DISCOVERY" -ForegroundColor Cyan
Write-Host "PAPER / SHADOW only. LIVE orders stay disabled." -ForegroundColor Yellow

$oldListener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
if ($oldListener) {
    Write-Host "Stopping existing listener on port 8765 (PID $oldListener)..." -ForegroundColor Yellow
    Stop-Process -Id $oldListener -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    py -m venv .venv
}
Set-ExecutionPolicy -Scope Process Bypass -Force
& ".\.venv\Scripts\Activate.ps1"
python -m pip install -r requirements.txt

$env:AUTO_RECORD = "1"
$env:START_CAPITAL = "5000"
$env:WS_SYMBOLS = "BTC/USD,ETH/USD,SOL/USD,XRP/USD"
$env:MICRO_SNAPSHOT_MS = "1000"

# Fast decision lane; expensive model discovery remains throttled.
$env:ALPHA_INTERVAL_S = "3"
$env:ALPHA_MODEL_REFRESH_S = "30"

# Validated PAPER stays strict.
$env:VALIDATED_HORIZONS = "60,120,300,600"
$env:ALPHA_MIN_TRAIN = "40"
$env:ALPHA_MIN_VALID = "18"
$env:ALPHA_MIN_NET_BPS = "2.0"

# Aggressive SHADOW exploration.
$env:SHADOW_PAPER_ENABLED = "1"
$env:SHADOW_HORIZONS = "15,30,60,120"
$env:SHADOW_MIN_TRAIN = "6"
$env:SHADOW_MIN_VALID = "3"
$env:SHADOW_MIN_GROSS_BPS = "0.10"
$env:SHADOW_MIN_HIT = "0.46"
$env:SHADOW_MIN_NET_BPS = "0"
$env:SHADOW_MAX_OPEN = "16"
$env:SHADOW_MAX_PER_SYMBOL = "4"
$env:SHADOW_REENTRY_COOLDOWN_S = "2"
$env:SHADOW_ALLOW_UNVALIDATED = "1"
$env:SHADOW_ALLOC_PCT = "5"
$env:SHADOW_UNVALIDATED_ALLOC_PCT = "2.5"
$env:SHADOW_MAX_DRAWDOWN_PCT = "40"

# Keep the economically cheaper research lane active in parallel.
$env:SCENARIO_PAPER_ENABLED = "1"
$env:SCENARIO_HORIZONS = "15,30,60,120"
$env:SCENARIO_MIN_NET_EDGE_BPS = "0"
$env:SCENARIO_MIN_TRAIN = "6"
$env:SCENARIO_MIN_VALID = "3"
$env:SCENARIO_MIN_HIT = "0.46"
$env:SCENARIO_MAX_OPEN = "16"
$env:SCENARIO_ALLOC_PCT = "5"

# Never lower costs by accident on the spot execution model.
$env:V4_EXECUTION_MODE = "market_taker"
if ($env:V4_EXEC_ROUNDTRIP_BPS -eq "14") {
    Remove-Item Env:V4_EXEC_ROUNDTRIP_BPS -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Decision cycle: 3 s | model refresh: 30 s" -ForegroundColor Green
Write-Host "Shadow horizons: 15/30/60/120 s | max open: 16 | max per symbol: 4" -ForegroundColor Green
Write-Host "Unvalidated exploration: ON (SHADOW only)" -ForegroundColor Yellow
Write-Host "Futures-maker proxy: 15/30/60/120 s | 11 bps cost floor" -ForegroundColor Green
Write-Host "LIVE orders: DISABLED" -ForegroundColor Yellow
Write-Host "Dashboard: http://127.0.0.1:8765" -ForegroundColor Green
Write-Host ""

Start-Process "http://127.0.0.1:8765"
python -m uvicorn app:app --host 127.0.0.1 --port 8765
