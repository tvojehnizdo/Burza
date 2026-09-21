$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Store = "C:\TvojeHnizdo\Vault\Kraken\futures.credentials.dpapi.json"

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

if (-not (Test-Path $Python)) { throw "Python venv chybi. Spust start_v4_neutral.ps1." }
$pair = Load-EncryptedPair $Store
if (-not $pair) { throw "Kraken Futures DPAPI credentials chybi: $Store. Spust prepare_futures.ps1." }

$env:KRAKEN_FUTURES_API_KEY = Secure-ToPlain $pair[0]
$env:KRAKEN_FUTURES_API_SECRET = Secure-ToPlain $pair[1]

try {
    Write-Host ""
    Write-Host "FUTURES CANARY - PRIVATE PLAN" -ForegroundColor Cyan
    $planJson = & $Python futures_canary.py --plan
    if ($LASTEXITCODE -ne 0) { throw "Private futures canary plan selhal." }
    $planJson | Write-Host
    $plan = ($planJson -join [Environment]::NewLine) | ConvertFrom-Json

    if (-not $plan.ready -or [string]$plan.reason -ne "FUTURES_CANARY_EXECUTABLE") {
        Write-Host ""
        Write-Host ("LIVE NEODESLAN: plan neni executable. reason=" + [string]$plan.reason) -ForegroundColor Yellow
        exit 2
    }

    Write-Host ""
    Write-Host ("READY: " + $plan.candidate.symbol + " " + $plan.candidate.side +
        " | notional ~$" + [math]::Round([double]$plan.candidate.estimated_notional_usd, 2) +
        " | stop=" + [math]::Round([double]$plan.candidate.stop_price, 4) +
        " | take=" + [math]::Round([double]$plan.candidate.take_profit_price, 4)) -ForegroundColor Green

    $confirm = Read-Host "Pro odeslani prvniho LIVE futures canary napis presne SPUSTIT FUTURES CANARY"
    if ($confirm -ne "SPUSTIT FUTURES CANARY") {
        Write-Host "LIVE order nebyl odeslan." -ForegroundColor Yellow
        exit 1
    }

    & $Python futures_canary.py --execute --confirm SPUSTIT-FUTURES-CANARY
    if ($LASTEXITCODE -ne 0) { throw "Futures canary execute selhal." }
}
finally {
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
}
