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

if (-not (Test-Path $Python)) { throw "Python venv chybi." }
$pair = Load-EncryptedPair $Store
if (-not $pair) { throw "Futures credentials chybi: $Store" }

$env:KRAKEN_FUTURES_API_KEY = Secure-ToPlain $pair[0]
$env:KRAKEN_FUTURES_API_SECRET = Secure-ToPlain $pair[1]

try {
    Write-Host ""
    Write-Host "FUTURES BOUNDED LIVE SESSION" -ForegroundColor Cyan
    Write-Host "Spusteni tohoto skriptu = schvaleni cele relace." -ForegroundColor Yellow
    Write-Host "60 min | max 2 skutecne vstupy v jednom setupu." -ForegroundColor Yellow
    Write-Host "Base target 5 USD | scale nad 5 USD jen po auditovanem evidence gate | cap 10 USD / 35 % Futures equity." -ForegroundColor Yellow
    Write-Host "Soft brzda 1.5 % equity | hard kill-switch 5 % session kapitalu." -ForegroundColor Yellow
    Write-Host "SETUP V4 MICRO/MAKER: bez permanentniho INVERSE." -ForegroundColor Magenta
    Write-Host "Siroky PF trh -> 48 prefilter / 28 deep -> quality -> fresh flow -> orderbook -> BTC/ETH leaders." -ForegroundColor Yellow
    Write-Host "3m probe SHADOW | LIVE az po 15m potvrzeni | max 1 potvrzeny reversal." -ForegroundColor Yellow
    Write-Host "Maker-first post-only na touch; kratke cekani, overeny cancel, az potom market fallback." -ForegroundColor Yellow
    Write-Host "STOP ~45 bps | failed-breakout exit | trailing winner muze bezet az 30 min | backup TP 300 bps." -ForegroundColor Yellow
    Write-Host ""

    & $Python "futures_session.py" --run --confirm RUN-BOUNDED-LIVE-SESSION
    if ($LASTEXITCODE -ne 0) { throw "Bounded futures session skoncila s chybou." }
}
finally {
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
}
