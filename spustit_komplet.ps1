$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "IMPULSE MAX 5K - KOMPLETNI ORCHESTRATOR" -ForegroundColor Cyan
Write-Host "Spot/margin + Futures readiness + Unified Wallet + AI Supervisor" -ForegroundColor Cyan
Write-Host ""

git pull --ff-only
if ($LASTEXITCODE -ne 0) {
    Write-Host "git pull neprošel; pokračuji s lokální verzí." -ForegroundColor Yellow
}

& ".\spustit_vse.ps1"

$Unified = Read-Host "Mas na Kraken zapnuty Unified Wallet? [A/n]"
if ($Unified.Trim().ToUpperInvariant() -eq "N") {
    Write-Host ""
    Write-Host "Unified Wallet na tomto uctu neni podminkou. Pokud se v Settings -> Account nezobrazuje, pokracujeme v klasickem rezimu oddelenych spot/futures penezene." -ForegroundColor Yellow
    Write-Host "Spot/margin zustava plne funkcni. Futures pripojime samostatnym Futures API klicem." -ForegroundColor Cyan
    $tryUnified = Read-Host "Chces jeste jednou otevrit Kraken Account settings kvuli Unified Wallet? [a/N]"
    if ($tryUnified.Trim().ToUpperInvariant() -in @("A","Y")) {
        try { Start-Process "https://pro.kraken.com/app/settings/account" } catch { }
        Read-Host "Po kontrole stiskni ENTER"
    }
}

$FuturesStore = "C:\TvojeHnizdo\Vault\Kraken\futures.credentials.dpapi.json"
if (-not (Test-Path $FuturesStore)) {
    $setupF = Read-Host "Nastavit ted i samostatny Kraken Futures API key? [A/n]"
    if ([string]::IsNullOrWhiteSpace($setupF) -or $setupF.Trim().ToUpperInvariant() -in @("A","Y")) {
        & ".\prepare_futures.ps1"
    }
}
else {
    Write-Host "Futures API credentials already stored locally with DPAPI." -ForegroundColor Green
}

Write-Host ""
Write-Host "Spoustim AI Supervisor v novem PowerShell okne..." -ForegroundColor Cyan
Start-Process pwsh.exe -ArgumentList "-NoExit","-ExecutionPolicy","Bypass","-File",(Join-Path $PSScriptRoot "spustit_supervisor.ps1")

Write-Host ""
Write-Host "KOMPLETNI SYSTÉM SPUŠTĚN." -ForegroundColor Green
Write-Host "V4:          http://127.0.0.1:8765" -ForegroundColor Green
$portFile = Join-Path $PSScriptRoot "reports\ai-supervisor-port.txt"
$supervisorText = "AI Supervisor: automaticky vybraný volný port (8771-8799)"
if (Test-Path $portFile) {
    $p = (Get-Content -Raw $portFile).Trim()
    if ($p) { $supervisorText = "AI Supervisor: http://127.0.0.1:$p" }
}
Write-Host $supervisorText -ForegroundColor Green
Write-Host ""
Write-Host "Spot/margin trading API: připraveno." -ForegroundColor Green
Write-Host "Futures trading API: podle samostatného Futures key." -ForegroundColor Green
Write-Host "Unified Wallet: doporučený způsob sdílení kapitálu bez povolení Withdraw Funds." -ForegroundColor Green
Write-Host "Externí výběry: zakázané." -ForegroundColor Yellow
Write-Host ""
Write-Host "LIVE trading zůstává samostatně řízený policy gate; AI ho sama zapnout neumí." -ForegroundColor Yellow
