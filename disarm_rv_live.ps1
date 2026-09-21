$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Python venv chybi." }
& $Python rv_live.py --disarm
Write-Host "RV LIVE DISARMED. Existujici live par musi byt pred ukoncenim procesu uzavren." -ForegroundColor Green
