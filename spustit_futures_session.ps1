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
    Write-Host "Cil/max 5 USD/obchod | max 4 pozice | portfolio max 20 USD / 95 % equity." -ForegroundColor Yellow
    Write-Host "Kill-switch: max 50 % vycleneneho kapitalu (max 11 USD pri 22 USD)." -ForegroundColor Yellow
    Write-Host "SETUP V2: bez permanentniho INVERSE." -ForegroundColor Magenta
    Write-Host "Siroky PF trh -> top 48 prefilter / 28 deep | BASE trend/breakout | anti-chase | volatility sizing." -ForegroundColor Yellow
    Write-Host "Market breadth + recent taker flow | adaptive tempo BASE 90s / STRONG 45s / ELITE 20s." -ForegroundColor Yellow
    Write-Host "3m probe je SHADOW; LIVE az po 15m potvrzeni." -ForegroundColor Yellow
    Write-Host "Po uzavreni prvniho obchodu je povolen max 1 potvrzeny reversal." -ForegroundColor Yellow
    Write-Host "Risk ochrany zustavaji, korelacni pary jsou SHADOW-only." -ForegroundColor Yellow
    Write-Host "STOP ~45 bps | backup TP 300 bps | trailing ratchet 45+ bps, utahovani az na 6 bps." -ForegroundColor Yellow
    Write-Host ""

    & $Python "futures_session.py" --run --confirm RUN-BOUNDED-LIVE-SESSION
    if ($LASTEXITCODE -ne 0) { throw "Bounded futures session skoncila s chybou." }
}
finally {
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
}
