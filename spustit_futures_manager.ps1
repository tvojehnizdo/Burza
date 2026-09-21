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
    Write-Host "FUTURES POSITION MANAGER" -ForegroundColor Cyan
    Write-Host "Automaticky hlida otevrene pozice, profit/no-progress/time exit a cleanup." -ForegroundColor Yellow
    Write-Host ""
    $arm = Read-Host "Pro spusteni napis presne ARM MANAGER"
    if ($arm -ne "ARM MANAGER") {
        Write-Host "Manager nebyl spusten." -ForegroundColor Yellow
        exit 1
    }
    & $Python "futures_autopilot.py" --run --confirm ARM-LIVE-MANAGER
}
finally {
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
}
