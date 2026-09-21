$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Python venv chybi." }

Write-Host ""
Write-Host "FUTURES PAPER TRAILING + PAIRS" -ForegroundColor Cyan
Write-Host "PAPER ONLY - neposila zadne LIVE ordery a nepotrebuje API klic." -ForegroundColor Green
Write-Host "TOP-20 volatilita | single signaly + pary | tesny ratchet trailing | 60 min." -ForegroundColor Yellow
Write-Host ""

& $Python "futures_paper_trailing.py" --selftest
if ($LASTEXITCODE -ne 0) { throw "Selftest selhal." }

& $Python "futures_paper_trailing.py" --run --reset
if ($LASTEXITCODE -ne 0) { throw "Paper session selhala." }

Write-Host ""
Write-Host "Report:" -ForegroundColor Green
Write-Host "  C:\Burza\reports\futures_paper_trailing_latest.json"
