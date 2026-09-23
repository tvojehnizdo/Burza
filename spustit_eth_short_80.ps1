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
    Write-Host "ETH LONG-TERM SHORT 80/20" -ForegroundColor Cyan
    Write-Host "Max 80 % aktualni Futures equity do SHORT ETH; min 20 % zustava rezerva." -ForegroundColor Yellow
    Write-Host "Vychozi STOP +8 %, TAKE -20 %. Zadne automaticke prevody ze Spot/Main." -ForegroundColor Yellow
    Write-Host ""

    & $Python .\eth_longterm_short_80.py --plan
    if ($LASTEXITCODE -ne 0) { throw "Plan selhal." }

    Write-Host ""
    $confirm = Read-Host "Pro skutecne otevreni napis presne: SHORT-ETH-80"
    if ($confirm -ne "SHORT-ETH-80") {
        Write-Host "Obchod nebyl odeslan." -ForegroundColor Yellow
        exit 0
    }

    & $Python .\eth_longterm_short_80.py --execute --confirm SHORT-ETH-80
    if ($LASTEXITCODE -ne 0) { throw "ETH SHORT selhal." }
}
finally {
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
}
