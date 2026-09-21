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

if ($freeGB -ge 35) {
    $cmd = "Set-Location '$PSScriptRoot'; Set-ExecutionPolicy -Scope Process Bypass -Force; .\.venv\Scripts\Activate.ps1; python history_fetch.py --full"
    Start-Process powershell.exe -ArgumentList "-NoExit", "-Command", $cmd
    Write-Host "Full official Kraken OHLCVT download started in a second window." -ForegroundColor Green
} else {
    Write-Host "Full-history download not started: at least 35 GB free is required by this launcher." -ForegroundColor Yellow
}

& ".\start_v4.ps1"
