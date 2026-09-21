$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:RV_DB = "data/relative_value_v2.db"
$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Python venv chybi." }
& $Python rv_live.py --disarm
Write-Host "RV LIVE: nove vstupy jsou zakazane. Pokud je par otevreny, bridge zustane pouze v exit-managementu a po uzavreni vypne futures live gateway." -ForegroundColor Green
