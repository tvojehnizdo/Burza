$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $Python -c "from kraken_live_control import save_policy; print(save_policy({'live_execution':False}))"
Write-Host "LIVE EXECUTION DISARMED." -ForegroundColor Green
