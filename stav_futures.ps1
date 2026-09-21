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
if (-not $pair) { throw "Kraken Futures DPAPI credentials chybi: $Store" }

$env:KRAKEN_FUTURES_API_KEY = Secure-ToPlain $pair[0]
$env:KRAKEN_FUTURES_API_SECRET = Secure-ToPlain $pair[1]

try {
    & $Python "futures_canary.py" --status
    if ($LASTEXITCODE -ne 0) { throw "Futures status selhal." }
}
finally {
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
}
