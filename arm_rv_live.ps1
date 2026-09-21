$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$FuturesStore = "C:\TvojeHnizdo\Vault\Kraken\futures.credentials.dpapi.json"

function Secure-ToPlain([Security.SecureString]$Secure) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Load-EncryptedPair([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    $obj = Get-Content -Raw $Path | ConvertFrom-Json
    return @(
        (ConvertTo-SecureString -String ([string]$obj.api_key)),
        (ConvertTo-SecureString -String ([string]$obj.api_secret))
    )
}

if (-not (Test-Path $Python)) { throw "Python venv chybi. Nejdrive spust start_v4_neutral.ps1." }
$fp = Load-EncryptedPair $FuturesStore
if (-not $fp) { throw "Kraken Futures DPAPI credentials chybi: $FuturesStore" }

$env:KRAKEN_FUTURES_API_KEY = Secure-ToPlain $fp[0]
$env:KRAKEN_FUTURES_API_SECRET = Secure-ToPlain $fp[1]

Write-Host ""
Write-Host "RV LIVE READINESS" -ForegroundColor Cyan
& $Python rv_live.py --readiness
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "LIVE NEZAPNUTO: evidence/API/account gate neprosel." -ForegroundColor Yellow
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
    exit 2
}

Write-Host ""
Write-Host "Canary limit: max 1 RV par; tvrdy futures cap plati na kazdou nohu." -ForegroundColor Yellow
Write-Host "Kraken transfer API musi zustat NO_ACCESS." -ForegroundColor Yellow
Write-Host "Pri selhani druhe nohy bridge zkusi okamzitou kompenzacni redukci prvni nohy." -ForegroundColor Yellow
$confirm = Read-Host "Pro skutecnou aktivaci napis presne ARM RV LIVE"
if ($confirm -ne "ARM RV LIVE") {
    Write-Host "Neaktivovano."
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
    exit 1
}

& $Python rv_live.py --arm
if ($LASTEXITCODE -ne 0) { throw "RV LIVE arm selhal." }

Write-Host ""
Write-Host "RV LIVE ARMED. Spoustim samostatny bridge proces..." -ForegroundColor Green
Start-Process pwsh.exe -ArgumentList "-NoExit","-ExecutionPolicy","Bypass","-File",(Join-Path $PSScriptRoot "spustit_rv_live.ps1")

Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
