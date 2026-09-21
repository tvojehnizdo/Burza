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

if (-not (Test-Path $Python)) { throw "Python venv chybi." }
$fp = Load-EncryptedPair $FuturesStore
if (-not $fp) { throw "Kraken Futures DPAPI credentials chybi." }

$env:KRAKEN_FUTURES_API_KEY = Secure-ToPlain $fp[0]
$env:KRAKEN_FUTURES_API_SECRET = Secure-ToPlain $fp[1]

Write-Host "RV LIVE BRIDGE" -ForegroundColor Cyan
& $Python rv_live.py --status
Write-Host "Bridge bezi oddelene od V4 serveru. CTRL+C proces ukonci." -ForegroundColor Yellow
& $Python rv_live.py --daemon

Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
