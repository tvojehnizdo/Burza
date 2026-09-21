$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "IMPULSE MAX 5K - MARKET NEUTRAL RELATIVE VALUE" -ForegroundColor Cyan
Write-Host "PAPER / RESEARCH only. LIVE orders are disabled." -ForegroundColor Yellow

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
$env:WS_SYMBOLS = "BTC/USD,ETH/USD,SOL/USD,XRP/USD,DOGE/USD,ADA/USD,LINK/USD,LTC/USD,BCH/USD,AVAX/USD,DOT/USD,XLM/USD"
$env:MICRO_SNAPSHOT_MS = "1000"

# Sequential opportunity hunter: one position at a time across a wider universe.
$env:ALPHA_INTERVAL_S = "2"
$env:ALPHA_MODEL_REFRESH_S = "15"
$env:SHADOW_PAPER_ENABLED = "1"
$env:SHADOW_ALLOW_UNVALIDATED = "0"
$env:SHADOW_HORIZONS = "15,30,60,120,300"
$env:SHADOW_MIN_TRAIN = "8"
$env:SHADOW_MIN_VALID = "4"
$env:SHADOW_MIN_GROSS_BPS = "0.25"
$env:SHADOW_MIN_HIT = "0.48"
$env:SHADOW_MIN_NET_BPS = "0"
$env:SHADOW_MAX_OPEN = "1"
$env:SHADOW_MAX_PER_SYMBOL = "1"
$env:SHADOW_REENTRY_COOLDOWN_S = "1"
$env:SHADOW_ALLOC_PCT = "100"
$env:SHADOW_UNVALIDATED_ALLOC_PCT = "0"
$env:SHADOW_MAX_DRAWDOWN_PCT = "35"

# Lower-cost futures-maker economic lane, also strictly sequential.
$env:SCENARIO_PAPER_ENABLED = "1"
$env:SCENARIO_HORIZONS = "60,120,300"
$env:SCENARIO_MIN_NET_EDGE_BPS = "5"
$env:SCENARIO_MIN_TRAIN = "20"
$env:SCENARIO_MIN_VALID = "12"
$env:SCENARIO_MIN_HIT = "0.52"
$env:SCENARIO_MAX_OPEN = "1"
$env:SCENARIO_ALLOC_PCT = "100"
$env:SCENARIO_MAX_DRAWDOWN_PCT = "20"

# Primary engine: delta-neutral PF <-> FF basis/funding discovery.
$env:RV_SCAN_INTERVAL_S = "5"
$env:RV_MAKER_FEE_BPS = "2"
$env:RV_ADVERSE_BUFFER_BPS = "4"
$env:RV_MIN_NET_EDGE_BPS = "5"
$env:RV_MIN_HISTORY = "60"
$env:RV_ENTRY_Z = "1.5"
$env:RV_EXIT_Z = "0.5"
$env:RV_STOP_Z = "3.5"
$env:RV_MIN_DAYS_TO_EXPIRY = "0.5"
$env:RV_MAX_DAYS_TO_EXPIRY = "220"
$env:RV_HISTORY_WINDOW = "500"
$env:RV_HISTORY_SAMPLE_S = "30"
$env:RV_MAX_PAIR_SPREAD_BPS = "15"
$env:RV_DB = "data/relative_value_v2.db"

# Automatically exploit qualifying opportunities in isolated PAPER.
$env:RV_PAPER_ENABLED = "1"
$env:RV_PAPER_START_EQUITY = "5000"
$env:RV_PAPER_ALLOC_PCT = "15"
$env:RV_PAPER_MAX_OPEN = "3"
$env:RV_PAPER_TAKE_BPS = "6"
$env:RV_PAPER_STOP_BPS = "60"
$env:RV_PAPER_MAX_HOLD_H = "4"
$env:RV_PAPER_REENTRY_COOLDOWN_S = "180"

Write-Host ""
Write-Host "Primary strategy: PF/FF market-neutral relative value" -ForegroundColor Green
Write-Host "Eligible opportunities: AUTO-EXECUTE in PAPER (two-leg)" -ForegroundColor Green
Write-Host "Scanner cadence: 5 s (FAST PAPER)" -ForegroundColor Green
Write-Host "Round-trip maker fee floor: 8 bps + 4 bps adverse-selection buffer" -ForegroundColor Green
Write-Host "Entry: robust basis deviation >= 1.5 sigma after >=60 quality observations; min net edge 5 bps" -ForegroundColor Green
Write-Host "History: 30 s decimation; pair spread quality cap 15 bps" -ForegroundColor Green
Write-Host "Sequential SHADOW hunter: ON | 12 spot pairs | max 1 trade | 100% simulated allocation" -ForegroundColor Green
Write-Host "Futures-maker scenario: ON | max 1 trade | 100% simulated allocation" -ForegroundColor Green
Write-Host "Unvalidated/no-cost-edge churn: OFF" -ForegroundColor Yellow
Write-Host "LIVE orders: DISABLED" -ForegroundColor Yellow
Write-Host "Dashboard: http://127.0.0.1:8765" -ForegroundColor Green
Write-Host "Relative value: http://127.0.0.1:8765/api/v4/relative-value" -ForegroundColor Green
Write-Host ""

Start-Process "http://127.0.0.1:8765"
python -m uvicorn app:app --host 127.0.0.1 --port 8765
