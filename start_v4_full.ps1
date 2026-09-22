$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    py -m venv .venv
}
Set-ExecutionPolicy -Scope Process Bypass -Force
& ".\.venv\Scripts\Activate.ps1"
python -m pip install -r requirements.txt

$drive = (Get-Item $PSScriptRoot).PSDrive.Name
$freeGB = [math]::Round((Get-PSDrive $drive).Free / 1GB, 1)
Write-Host "Free space on $($drive): $freeGB GB"

if ($freeGB -ge 45) {
    $cmd = "Set-Location '$PSScriptRoot'; Set-ExecutionPolicy -Scope Process Bypass -Force; .\.venv\Scripts\Activate.ps1; python history_fetch.py --full; python historical_research.py --root data/kraken_history/full --symbols XBTUSD ETHUSD SOLUSD XRPUSD --out reports/history-alpha.json; python futures_setup_research.py --root data/kraken_history/full --symbols XBTUSD ETHUSD SOLUSD XRPUSD --days 365 --out reports/setup-v3-research.json"
    Start-Process powershell.exe -ArgumentList "-NoExit", "-Command", $cmd
    Write-Host "Full official Kraken OHLCVT download started in a second window." -ForegroundColor Green
} else {
    Write-Host "Full-history download not started: at least 45 GB free is required by this launcher." -ForegroundColor Yellow
}

& ".\start_v4.ps1"
